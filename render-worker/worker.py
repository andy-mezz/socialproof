"""Standalone FFmpeg worker. Never runs in the application's serverless runtime."""
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlparse

MAX_BYTES = 500 * 1024 * 1024


def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    if result.returncode:
        # FFmpeg stderr can contain private input paths; never send it to logs/API.
        raise RuntimeError("Media processing failed")
    return result


def probe(path):
    return json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]).stdout)


def audio_filter(path, has_audio):
    if not has_audio:
        return "anull"
    analysis = run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:a:0", "-vn", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"])
    match = re.search(r'\{\s*"input_i".*?\}', analysis.stderr, re.S)
    if not match:
        raise RuntimeError("Audio analysis failed")
    stats = json.loads(match.group())
    # All-silent recorded audio produces -inf: preserve silence, do not amplify noise.
    if not all(math.isfinite(float(stats[k])) for k in ["input_i", "input_tp", "input_lra", "input_thresh", "target_offset"]):
        return "aresample=48000,aformat=channel_layouts=stereo"
    return ("loudnorm=I=-16:TP=-1.5:LRA=11:linear=true:"
            f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
            f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:offset={stats['target_offset']},"
            "aresample=48000,aformat=channel_layouts=stereo")


def video_duration(source, info, video):
    duration = float(video.get("duration") or info["format"].get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        # Browser MediaRecorder WebM often has no duration header. Inspect decoded timestamps.
        frames = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames", "-show_entries", "frame=best_effort_timestamp_time,pkt_duration_time,duration_time", "-of", "json", str(source)]).stdout)["frames"]
        times = [float(f["best_effort_timestamp_time"]) for f in frames if "best_effort_timestamp_time" in f]
        if not times:
            raise RuntimeError("Video timestamps missing")
        last = frames[-1]
        tail = float(last.get("duration_time") or last.get("pkt_duration_time") or (times[-1] - times[-2] if len(times) > 1 else 1 / 30))
        duration = max(times) - min(times) + tail
    if not math.isfinite(duration) or not 0 < duration <= 125:
        raise RuntimeError("Invalid clip duration")
    return duration


def normalize(source, output):
    info = probe(source)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not video:
        raise RuntimeError("No video stream")
    duration = video_duration(source, info, video)
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    audio = audio_filter(source, has_audio)
    # Default FFmpeg autorotation applies phone display-matrix rotation before filters.
    args = ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-threads", "2", "-filter_threads", "1", "-i", str(source)]
    if not has_audio:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    vf = "scale=trunc(iw*sar/2)*2:ih,setsar=1,scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p,setpts=PTS-STARTPTS"
    af = audio + ",asetpts=PTS-STARTPTS,apad"
    args += ["-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0", "-vf", vf, "-af", af,
             "-t", str(duration), "-c:v", "libx264", "-threads", "2", "-preset", "medium", "-crf", "20", "-r", "30", "-fps_mode", "cfr",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-map_metadata", "-1", "-metadata:s:v:0", "rotate=0", "-movflags", "+faststart", str(output)]
    run(args)
    return duration


def render(sources, output, on_progress=lambda n: None):
    if not 1 <= len(sources) <= 7:
        raise RuntimeError("Invalid clip count")
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        normalized = []
        total = 0
        for i, source in enumerate(sources):
            target = folder / f"clip-{i:03d}.mp4"
            total += normalize(source, target)
            normalized.append(target)
            on_progress(round(10 + 65 * (i + 1) / len(sources)))
        listing = folder / "concat.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in normalized))
        # Concat actual encoded compatible streams, not file bytes. A final encode normalizes the joined audio.
        run(["ffmpeg", "-y", "-hide_banner", "-nostdin", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c:v", "libx264", "-threads", "2", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30", "-fps_mode", "cfr",
             "-af", "alimiter=limit=0.841395:level=false,aresample=48000", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
             "-map_metadata", "-1", "-movflags", "+faststart", str(output)])
        on_progress(90)
        return validate_output(output, total)


def validate_output(output, expected_duration):
    info = probe(output)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    duration = float(info["format"]["duration"])
    if (video["codec_name"], video["width"], video["height"], video["r_frame_rate"], video["pix_fmt"], audio["codec_name"], int(audio["sample_rate"]), audio["channels"]) != ("h264", 1920, 1080, "30/1", "yuv420p", "aac", 48000, 2):
        raise RuntimeError("Output profile mismatch")
    if abs(duration - expected_duration) > max(1, expected_duration * .02):
        raise RuntimeError("Output duration mismatch")
    size = Path(output).stat().st_size
    if not 0 < size <= MAX_BYTES:
        raise RuntimeError("Output too large")
    # Decode the entire file, not just its header, before publishing it.
    run(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", str(output), "-f", "null", "-"])
    return {"size": size, "duration": duration, "width": 1920, "height": 1080, "fps": 30, "videoCodec": "h264", "audioCodec": "aac", "sampleRate": 48000, "channels": 2}


def api(endpoint, token, data):
    request = urllib.request.Request(endpoint, data=json.dumps(data).encode(), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def download(url, path, expected_size):
    if urlparse(url).scheme != "https":
        raise RuntimeError("Private source must use HTTPS")
    count = 0
    with urllib.request.urlopen(url, timeout=120) as response, open(path, "wb") as target:
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            if count > MAX_BYTES:
                raise RuntimeError("Source too large")
            target.write(chunk)
    if count != expected_size:
        raise RuntimeError("Source size mismatch")


def process_job(endpoint, token, job):
    base = {"jobId": job["id"], "lease": job["lease"]}
    stop = threading.Event()
    lease_lost = threading.Event()
    progress = [0]

    def heartbeat():
        failures = 0
        while not stop.wait(20):
            try:
                api(endpoint, token, {**base, "action": "heartbeat", "progress": progress[0]})
                failures = 0
            except Exception:
                failures += 1
                if failures >= 3:
                    lease_lost.set()
                    return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as folder:
            sources = []
            for i, clip in enumerate(job["clips"]):
                path = Path(folder) / f"source-{i}"
                download(clip["url"], path, clip["size"])
                sources.append(path)
            output = Path(folder) / "testimonial.mp4"
            metadata = render(sources, output, lambda n: progress.__setitem__(0, n))
            if lease_lost.is_set():
                raise RuntimeError("Lease lost")
            signed = api(endpoint, token, {**base, "action": "upload"})
            if urlparse(signed["uploadUrl"]).scheme != "https":
                raise RuntimeError("Upload must use HTTPS")
            # A disk-backed file streams the upload; no full MP4 allocation in the app.
            with open(output, "rb") as body:
                request = urllib.request.Request(signed["uploadUrl"], data=body, method="PUT", headers={"Content-Type": "video/mp4", "Content-Length": str(metadata["size"]), "Cache-Control": "max-age=3600"})
                with urllib.request.urlopen(request, timeout=300) as response:
                    response.read()
            for attempt in range(3):
                try:
                    api(endpoint, token, {**base, "action": "complete", "output": metadata})
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
    except Exception:
        try:
            api(endpoint, token, {**base, "action": "fail"})
        except Exception:
            pass  # Expired leases are durably recovered by the next claim.
        print("Render attempt interrupted; durable queue will recover.", flush=True)
    finally:
        stop.set()
        thread.join(timeout=50)


def main():
    endpoint = os.environ["VIDEO_RENDER_APP_URL"].rstrip("/") + "/api/public/render-worker"
    token = os.environ["VIDEO_RENDER_WORKER_TOKEN"]
    if urlparse(endpoint).scheme != "https":
        raise RuntimeError("Use the published HTTPS application URL")
    while True:
        try:
            job = api(endpoint, token, {"action": "claim"})["job"]
            if job:
                process_job(endpoint, token, job)
            else:
                time.sleep(10)
        except Exception:
            print("Rendering service cannot reach application; retrying.", flush=True)
            time.sleep(15)


if __name__ == "__main__":
    main()