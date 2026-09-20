# TapoHubSync on Unraid

Run TapoHubSync on Unraid using the **stock `python:3.12-slim` image** from
Docker Hub — no custom build or published image required. The container mounts
the repo scripts and, on first start, [`entrypoint.sh`](../entrypoint.sh)
installs `ffmpeg` + Python dependencies and then runs
[`sync_loop.py`](../sync_loop.py) on a sync loop.

## 1. Put the scripts on the server

Copy the repository (or at least `hub_sync.py`, `sync_loop.py`,
`requirements.txt`, and `entrypoint.sh`) to a folder on the Unraid box, e.g.:

```text
/mnt/user/appdata/tapohubsync/app/
```

## 2. Add the container

Either import [`unraid-template.xml`](unraid-template.xml) (copy it to
`/boot/config/plugins/dockerMan/templates-user/my-TapoHubSync.xml`) or use
**Docker → Add Container** and enter the values below.

| Field | Value |
|-------|-------|
| Repository | `python:3.12-slim` |
| Network Type | `bridge` |
| Post Arguments | `bash /app/entrypoint.sh` |

> **Network ports:** the hub must be reachable from the container on **TCP 443**
> (control API) **and TCP 8800** (media stream). Downloads use 8800 — if only 443
> is open, listing works but every clip fails with a timeout. Open both in any
> VLAN/firewall between Unraid and the hub.

Path / variable mappings:

| Type | Container | Host / value |
|------|-----------|--------------|
| Path | `/app` | `/mnt/user/appdata/tapohubsync/app` |
| Path | `/config` | `/mnt/user/appdata/tapohubsync/config` |
| Path | `/recordings` | `/mnt/user/media/tapo` |
| Variable | `TAPO_SYNC_INTERVAL` | `3600` (seconds; `0` = sync once and exit) |
| Variable | `TAPO_UMASK` | *Optional.* `000` (folders `777`/files `666` so SMB users can delete; `002` = group-writable only) |
| Variable | `TAPO_CONFIG` | *Optional.* `/config/tapohubsync.local.json` — only change to rename the config file |
| Variable | `TAPO_KEY_FILE` | *Optional.* `/config/tapohubsync.local.key` — only change to rename the key file |

The three paths and `TAPO_SYNC_INTERVAL` are all you need; the *Optional* rows
have working defaults baked into the scripts and can be left out entirely.

### Container icon

Unraid shows the icon from the template's `<Icon>` field, which must be a URL to
a square PNG (ideally 256x256). The template points at `unraid/icon.png` in the
repo:

1. Add a square PNG at `unraid/icon.png` and push it.
2. Set `<Icon>` to its **raw** URL, replacing `OWNER` with your GitHub user:
   `https://raw.githubusercontent.com/kramwell/TapoHubSync/main/unraid/icon.png`

Or in the Docker UI: edit the container → **Icon URL** field → paste any square
PNG URL. If you change the icon, Unraid may cache the old one — force a refresh
by removing/re-adding the container or clearing `/boot/config/plugins/dockerMan/images`.

## 3. First-time setup

Start the container. It will log a "config not found" message and wait.

Open the container **Console** (the `>_` icon on the Docker tab) and run setup
once:

```bash
python3 /app/hub_sync.py --setup
```

Follow the prompts (hub IP + credentials). The encrypted config is written to
`/config` (your appdata folder) and the sync loop starts automatically — no
restart needed.

## Persistence

- `/config` holds the encrypted credentials and survives reboots, container
  updates, and reinstalls.
- `/recordings` holds the downloaded MKV files.
- The `/app` scripts come from your appdata folder, so updating the tool is just
  replacing those files and restarting the container.

## Notes on the no-build approach

- On first start — and whenever Unraid **recreates** the container (e.g. "force
  update" or editing the template) — `ffmpeg` and the Python deps are
  re-installed, which needs internet. Plain restarts skip this.
- To avoid the reinstall entirely, build the self-contained image from
  [`../docker/Dockerfile`](../docker/Dockerfile) and point the Repository at your
  own image instead of `python:3.12-slim`.

## Troubleshooting

- **Downloads fail after ~10s with a `TimeoutError`** — listing works but every
  clip times out. This means the container can reach the hub's control API
  (**443**) but not its media-stream port (**8800**). Open **TCP 8800** in the
  VLAN/firewall between Unraid and the hub. Failed clips are retried on the next
  sync pass.
- **`No route to host`** — network issue, not credentials. The hub must be
  reachable from Unraid. Check VLANs, guest/AP isolation, and whether the hub's
  IP changed (re-run `--setup` with the new IP).
- **Container keeps waiting** — you haven't run `--setup` yet, or `/config`
  isn't mapped to a writable path.
- **`can't open file '//hub_sync.py'`** — the console opened at `/`. Use the full
  path (`python3 /app/hub_sync.py --setup`) or ensure `--workdir /app` is set in
  Extra Parameters (the template sets it).
