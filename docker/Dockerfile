# TapoHubSync container: runs sync_loop.py, which loops hub_sync.py --sync.
FROM python:3.12-slim

# ffmpeg/ffprobe are required to mux recordings.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so layer caching survives code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code is baked in so the image is self-contained (required for Unraid /
# any registry-pull workflow where the source isn't bind-mounted).
COPY hub_sync.py sync_loop.py ./

# Encrypted config lives in a mounted volume so it survives reboots/rebuilds.
ENV TAPO_CONFIG=/config/tapohubsync.local.json
ENV TAPO_KEY_FILE=/config/tapohubsync.local.key
# Where clips are written; override with a mounted volume.
ENV TAPO_OUTPUT_DIR=/recordings
# Seconds between sync passes (0 = run once and exit).
ENV TAPO_SYNC_INTERVAL=3600

VOLUME ["/config", "/recordings"]

CMD ["python3", "sync_loop.py"]
