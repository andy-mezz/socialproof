# Real video rendering deployment

The application persists jobs; this standalone worker runs real FFmpeg on a container host. FFmpeg cannot run in the application's serverless runtime. No video-generation API or AI is used.

## Required configuration

1. Publish the application. Use its published HTTPS URL, not the editor's gated preview URL.
2. Add `VIDEO_RENDER_WORKER_TOKEN` as a shared secret in Lovable and on the worker host. Generate it in the secure secret form and copy the same value to the host; never commit it.
3. Deploy this folder as a **background worker**, not a web service, on Render, Railway, Fly.io, or your own Docker host. Build using this Dockerfile.
4. Set `VIDEO_RENDER_APP_URL` on that host to the published application URL. Set `VIDEO_RENDER_WORKER_TOKEN` there to the same shared secret.
5. Allocate at least 2 CPU cores, 2 GB RAM, and 5 GB temporary disk. Disable autosuspend/scale-to-zero. The worker must remain running to drain jobs. The host charges for compute; no service is provisioned automatically.
6. The private `clips` bucket must allow MP4 uploads up to 500 MB; the workspace storage cap must be at least this large. Keep it private.

The only app endpoint is `POST /api/public/render-worker`. It checks the shared bearer token before any privileged work. The worker polls every 10 seconds; no inbound port, webhook callback, provider API credential, scheduler, or database key is required on the host. Only scoped, short-lived media URLs are given to the worker.

## Processing and reliability

- Export is available only for approved testimonials and organization members. Requests atomically reuse an active or ready job.
- Job source clips are snapshotted in question order, with source sizes verified on download.
- An atomic SKIP LOCKED claim issues a unique five-minute lease; heartbeat every 20 seconds keeps it alive. Multiple workers can safely drain the queue.
- Expired leases are reclaimed. Failed attempts retry with 30/60-second backoff; after three attempts the job fails visibly and a user can retry.
- Every state write checks the lease; old workers cannot publish over a newer attempt. Outputs are attempt-specific private objects.
- Each clip is decoded and auto-rotated; aspect ratio is preserved with black letterboxing into 1920×1080 square-pixel 30 fps H.264/yuv420p. Mixed WebM/MP4 and portrait/landscape inputs are supported.
- Recorded audio gets two-pass -16 LUFS / -1.5 dBTP loudness normalization, stereo AAC at 48 kHz. Missing audio gets silence; already-silent audio remains silent. Joined audio has a peak limiter.
- MP4 uses fast-start layout. Full decode, duration and output profile are verified with FFmpeg/ffprobe before upload; storage existence, MIME and size are verified before marking ready.
- Output stays in private storage at `{org_id}/renders/{job_id}/{lease}.mp4`. Members receive a two-minute attachment URL; reapproval is required for downloads after approval is removed.

## Verification

Run `python -m unittest discover -s render-worker -p 'test_*.py'` with FFmpeg installed. For deployed end-to-end testing, record mixed inputs, approve, export, close the page, return after processing, download the MP4, and verify playback. Stop a worker mid-render and confirm another attempt recovers; run two workers and confirm jobs are not claimed twice. Disconnected configuration displays honestly rather than claiming a completed export.

## Operations

Rotate the shared token in both locations together. Monitor worker logs and failed jobs. Retain source clips; periodically remove orphaned attempt outputs that are not referenced by a ready job (no automatic cleanup is installed). Do not log signed URLs or the token. Rendering errors are deliberately generic in the app.