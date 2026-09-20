# TapoHubSync

A Python tool to list, download, and mirror recordings from cameras and doorbells paired to a Tapo hub locally.

This project does not use an official public Tapo API; it relies on community reverse-engineered behavior. Run `--setup`, `--devices`, and `--list` in that order first to confirm it works with your own hub and device combination.

## Supported hubs

Built and tested against the **Tapo H200** hub. The tool talks to the hub generically, so other Tapo hubs may work — support for additional models will be confirmed and documented as the community tests them. If you try it with another hub, please open an issue with your results so it can be added to the tested list.

## Features

- List cameras and doorbells paired to the Hub
- List every recording the hub holds for every device
- Download recordings as MKV
- Skip files that are already downloaded
- Mirror every clip the hub holds for all devices in one command
- Store the hubs IP and credentials together in a local encrypted config

## Install

```bash
git clone <repo-url>
cd TapoHubSync
sudo apt update
sudo apt install -y python3-pip ffmpeg
sudo pip3 install -r requirements.txt --break-system-packages
```

Python 3.12 or later is recommended. `ffmpeg`/`ffprobe` must be installed for muxing recordings (the `apt install` above covers it).

On Ubuntu/WSL, `pip` is not installed by default and is named `pip3`; `python3-pip` provides it. The `--break-system-packages` flag is required on newer Ubuntu (23.04+) where PEP 668 blocks system-wide `pip` installs.

## First-time setup

On a new machine or a new working folder, run setup first.

```bash
python3 hub_sync.py --setup
```

`--setup` handles the following automatically:

- Prompt for the hubs IP and verify the connection
- Prompt for hub/Tapo account credentials
- Store the hubs IP and encrypted credentials in `tapohubsync.local.json`
- Store the local decryption key in `tapohubsync.local.key`
- List every device paired to the hub

Local files that get created:

```text
tapohubsync.local.json
tapohubsync.local.key
```

The hubs IP, account, and passwords all live encrypted inside `tapohubsync.local.json`, unlocked by `tapohubsync.local.key`. As long as both files are present, `--sync` / `--list` / `--devices` run without prompting.

Runtime settings (`TAPO_OUTPUT_DIR` for a mounted share, `TAPO_SYNC_INTERVAL` for the container loop) come from environment variables, or you can hardcode them in the Settings block at the top of `hub_sync.py` / `sync_loop.py`.

Warning: anyone who has both `tapohubsync.local.json` and `tapohubsync.local.key` can decrypt the stored credentials. Do not share or commit those two files.

## Password entry guidance

```text
hub/device account password:
  If you created an Account in the Tapo app > Advanced Settings, enter that password.
  If you're not sure, enter your Tapo cloud password.

Tapo cloud password:
  Enter your TP-Link/Tapo account password.
  If it's the same as the first value, just press Enter.
```

## Listing paired devices

The tool works across every device paired to the hub; there is no single "active"
device to configure. To see what is paired:

```bash
python3 hub_sync.py --devices
```

Example output:

```text
Devices: 2
0: alias=CAMERA_ALIAS model=CAMERA_MODEL device_id=CAMERA_DEVICE_ID mac=CAMERA_MAC
1: alias=DOORBELL_ALIAS_2 model=DOORBELL_MODEL device_id=DOORBELL_DEVICE_ID_2 mac=DOORBELL_MAC_2
```

## CLI usage

List paired devices:

```bash
python3 hub_sync.py --devices
```

List every recording the hub holds (all devices, all dates):

```bash
python3 hub_sync.py --list
```

Downloading is handled by `--sync` (see below); it always mirrors everything and skips clips you already have.

Output files are saved to:

```text
recordings/CAMERA_ALIAS/DEVICE_ID/YYYYMMDD/CAMERA_ALIAS_YYYYMMDD_HHMMSS.mkv
```

Each device gets its own `alias/device_id` folder tree, so devices with the same name stay separate (the `device_id` is unique). Set the `TAPO_OUTPUT_DIR` environment variable (or `OUTPUT_DIR` in `hub_sync.py`) to save somewhere else, such as a mounted network share (handy for containers); it defaults to `recordings`.

### Output format

Recordings are written as MKV, the closest-to-raw option: the H.264 video **and**
the camera's native G.711 audio are both copied with no re-encode
(`-c:v copy -c:a copy`). Neither the video nor the audio track is ever transcoded.

## Mirroring the whole hub

To keep a local copy of everything the hub currently holds, for every paired device, use `--sync`. It automatically covers all devices and all available dates, skips clips that are already downloaded, and never deletes local files.

```bash
python3 hub_sync.py --sync
```

Run it again (for example on a cron job or systemd timer) to pull any new recordings. Files removed from the hub are kept locally; the tool never deletes anything.

## Container / unattended mode

`sync_loop.py` is the headless entrypoint for a container or a long-running service. It simply runs `--sync` on a loop, sleeping `TAPO_SYNC_INTERVAL` seconds between passes.

```bash
python3 sync_loop.py
```

- Credentials come from the encrypted config. In the Docker/Unraid images this path is `/config` (set via the `TAPO_CONFIG` / `TAPO_KEY_FILE` environment variables); when you run the scripts directly it defaults to the script's own folder (`tapohubsync.local.json`). If no config exists yet, the container stays up and waits, printing instructions to run `--setup`. Shell in once (`python3 /app/hub_sync.py --setup`); the container detects the new config and starts syncing automatically.
- `TAPO_SYNC_INTERVAL` sets the seconds between passes (default `3600`). Set it to `0` to sync once and exit — useful with a cron job or a container restart policy instead of the built-in loop.
- `TAPO_OUTPUT_DIR` points at where clips are written, e.g. a mounted share.

Because state lives in the mounted output dir and encrypted config, restarting the container just resumes syncing — already-downloaded clips are skipped.

Ready-to-use deployment files and step-by-step guides live in their own folders:

- [`docker/`](docker/README.md) — Docker / docker compose (stock `python:3.12-slim` image, no build required).
- [`unraid/`](unraid/README.md) — Unraid container template and instructions.

## Standalone unattended (no container)

You can run the same loop directly on a host (Linux PC, Raspberry Pi, NAS) without Docker. Complete [First-time setup](#first-time-setup) first so `tapohubsync.local.json` / `tapohubsync.local.key` exist in the project folder, then run:

```bash
python3 sync_loop.py
```

By default the config is read from the project folder — no `TAPO_CONFIG` needed. Optionally set environment variables to tune behavior:

```bash
export TAPO_SYNC_INTERVAL=3600      # seconds between passes; 0 = run once and exit
export TAPO_OUTPUT_DIR=/mnt/nas/tapo  # where recordings are written
python3 sync_loop.py
```

To keep it running across reboots, wrap it in a `systemd` service or a cron job. Example `systemd` unit (`/etc/systemd/system/tapohubsync.service`):

```ini
[Unit]
Description=TapoHubSync mirror loop
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/TapoHubSync
ExecStart=/usr/bin/python3 /opt/TapoHubSync/sync_loop.py
Environment=TAPO_SYNC_INTERVAL=3600
Environment=TAPO_OUTPUT_DIR=/mnt/nas/tapo
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl enable --now tapohubsync.service
journalctl -u tapohubsync.service -f
```

For a one-shot-per-run model instead of the built-in loop, set `TAPO_SYNC_INTERVAL=0` and drive it from `cron` or a `systemd` timer.


## Hub IP change or No Route To Host

If the hubs IP has changed, re-run `python3 hub_sync.py --setup` with the new address. You can find the current IP in the Tapo app or your router's DHCP client list.

`No route to host` is not a password problem; it means the PC cannot reach the hub over the network. Check the hubs power, the LAN/VLAN of the PC and hub, guest Wi-Fi/AP isolation, and whether the hubs IP changed.

## Files that must not be committed to Git

These are covered by `.gitignore`, but always double-check before committing.

```text
tapohubsync.local.json
tapohubsync.local.key
recordings/
*.mkv
*.log
__pycache__/
```

Check before committing:

```bash
git status --short
git status --ignored --short
```

## Credits

This project is a modified fork of [`tapo-h200-recording-downloader`](https://github.com/yimstar9/tapo-h200-recording-downloader) by yimstar9, who wrote the original script. Many thanks for the original work.

It is built on top of [`pytapo`](https://github.com/JurajNyiri/pytapo) by Juraj Nyíri, which provides the reverse-engineered Tapo API client used to communicate with the hub and its paired devives. Many thanks to the original author and contributors for their work.


