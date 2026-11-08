FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /worker
COPY worker.py /worker/worker.py
RUN useradd --create-home renderer
USER renderer
ENV PYTHONUNBUFFERED=1
CMD ["python", "/worker/worker.py"]