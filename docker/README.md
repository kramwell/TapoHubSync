# TapoHubSync on Docker

Run TapoHubSync as a container that loops `hub_sync.py --sync`, mirroring every
recording the hub holds to a local folder.

This setup uses the **stock `python:3.12-slim` image** — no custom build or
published image required. On first start, [`entrypoint.sh`](../entrypoint.sh)
installs `ffmpeg` and the Python dependencies, then runs
[`sync_loop.py`](../sync_loop.py).

## Layout

The container mounts three host locations:

| Container path | Purpose | Persisted |
|----------------|---------|-----------|
| `/app` | The repo scripts (mounted from the project root) | n/a (source) |
| `/config` | Encrypted credentials created by `--setup` | Yes |
| `/recordings` | Downloaded MKV recordings | Yes |

## Quick start (docker compose)

From this `docker/` folder:

```bash
docker compose up -d
```

The container starts, sees there's no config yet, prints setup instructions, and
waits. Create the config once by shelling into the running container:

```bash
docker compose exec tapohubsync python3 /app/hub_sync.py --setup
```

Follow the prompts (hub IP + credentials). The encrypted
`tapohubsync.local.json` / `.key` are written to `/config` (the `../config`
folder on the host) and the sync loop starts automatically.

Check logs:

```bash
docker compose logs -f
```

## Configuration

Set these in [`docker-compose.yml`](docker-compose.yml) under `environment:`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `TAPO_SYNC_INTERVAL` | `3600` | Seconds between sync passes. `0` = sync once and exit. |
| `TAPO_CONFIG` | `/config/tapohubsync.local.json` | Encrypted config path. |
| `TAPO_KEY_FILE` | `/config/tapohubsync.local.key` | Decryption key path. |
| `TAPO_OUTPUT_DIR` | `/recordings` | Where recordings are written. |

Change the host recordings location by editing the `../recordings:/recordings`
volume line.

## Plain `docker run`

If you prefer not to use compose (run from the repo root):

```bash
docker run -d --name tapohubsync --restart unless-stopped \
  -w /app \
  -e TAPO_SYNC_INTERVAL=3600 \
  -e TAPO_CONFIG=/config/tapohubsync.local.json \
  -e TAPO_KEY_FILE=/config/tapohubsync.local.key \
  -e TAPO_OUTPUT_DIR=/recordings \
  -v "$(pwd):/app" \
  -v "$(pwd)/config:/config" \
  -v "$(pwd)/recordings:/recordings" \
  python:3.12-slim bash /app/entrypoint.sh
```

Then run setup once:

```bash
docker exec -it tapohubsync python3 /app/hub_sync.py --setup
```

## Optional: build a self-contained image

The stock-image approach re-installs `ffmpeg` + deps whenever the container is
**recreated** (needs internet). To avoid that, build the included
[`Dockerfile`](Dockerfile) into a self-contained image (run from the repo root
so the build context includes the scripts):

```bash
docker build -f docker/Dockerfile -t tapohubsync .
```

Then in [`docker-compose.yml`](docker-compose.yml) replace
`image: python:3.12-slim` + the `command:` line with `image: tapohubsync`, and
drop the `../:/app` mount (the code is baked in).

## Notes

- The hub must be reachable from the container on **TCP 443** (control API) and
  **TCP 8800** (media stream). Listing uses 443; downloads use 8800 — if only 443
  is open, listing works but every clip fails with a timeout. Open both in any
  VLAN/firewall between the Docker host and the hub.
- Files removed from the hub are kept locally; the tool never deletes anything.
- Never commit `config/` or `recordings/` — they hold credentials and media.
