#!/usr/bin/env bash
# Startup for running TapoHubSync on the stock python image (no custom build).
# Installs ffmpeg + Python deps once, then runs the sync loop.
set -euo pipefail

# ffmpeg/ffprobe are required to mux recordings; install only if missing.
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Installing ffmpeg..."
  apt-get update
  apt-get install -y --no-install-recommends ffmpeg
  rm -rf /var/lib/apt/lists/*
fi

# Install Python deps only if they aren't already present.
if ! python3 -c "import pytapo, aiofiles, cryptography" >/dev/null 2>&1; then
  echo "Installing Python dependencies..."
  pip install --no-cache-dir -r /app/requirements.txt
fi

exec python3 /app/sync_loop.py
