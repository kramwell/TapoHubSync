#!/usr/bin/env python3
"""Unattended entrypoint: run `hub_sync.py --sync` on a loop.

Used by containers and standalone services (systemd/cron) alike.
Set TAPO_SYNC_INTERVAL (seconds) to control the gap between passes.
0 runs a single sync and exits (for cron or a restart policy).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "hub_sync.py"

# --- Settings ---------------------------------------------------------------
# Reads from an environment variable (e.g. Docker) for now. To hardcode it,
# replace this with a plain integer (seconds between passes; 0 = run once).
SYNC_INTERVAL = os.environ.get("TAPO_SYNC_INTERVAL", "3600")

# Must match the paths hub_sync.py uses so the preflight check stays in sync.
CONFIG_PATH = ROOT / os.environ.get("TAPO_CONFIG", "tapohubsync.local.json")
KEY_PATH = ROOT / os.environ.get("TAPO_KEY_FILE", "tapohubsync.local.key")
# How often (seconds) to re-check for the config while waiting for --setup.
SETUP_POLL_SECONDS = 15


def sync_interval_seconds() -> int:
    try:
        return max(0, int(SYNC_INTERVAL))
    except (TypeError, ValueError):
        print(f"Invalid TAPO_SYNC_INTERVAL={SYNC_INTERVAL!r}; using 3600.", flush=True)
        return 3600


def config_ready() -> bool:
    """True only when both encrypted config files exist (created by --setup)."""
    return CONFIG_PATH.is_file() and KEY_PATH.is_file()


def print_setup_instructions() -> None:
    missing = [str(p) for p in (CONFIG_PATH, KEY_PATH) if not p.is_file()]
    print(
        "Tapo config not found; cannot sync yet.\n"
        f"  Missing: {', '.join(missing)}\n"
        "\n"
        "First-time setup is interactive. Shell into this running container and\n"
        "run setup once:\n"
        "  docker exec -it <container> python3 hub_sync.py --setup\n"
        "\n"
        "The container will detect the new config automatically and start syncing\n"
        "(a restart also works). This container stays up and waits so you can exec in.\n"
        "\n"
        "Tip: mount a /config volume so the generated config persists across\n"
        "reboots/rebuilds, e.g.  -v ./config:/config  (Unraid: /mnt/user/appdata/tapohubsync).",
        file=sys.stderr,
        flush=True,
    )


def wait_for_config() -> None:
    """Block (keeping the container alive) until setup produces the config."""
    if config_ready():
        return
    print_setup_instructions()
    while not config_ready():
        time.sleep(SETUP_POLL_SECONDS)
    print("Config detected; starting sync.", flush=True)


def main() -> int:
    wait_for_config()

    interval = sync_interval_seconds()
    print(f"Hubs sync loop starting (interval={interval}s).", flush=True)

    while True:
        result = subprocess.run([sys.executable, str(SCRIPT), "--sync"])
        if result.returncode != 0:
            print(
                f"Sync exited with code {result.returncode}.",
                file=sys.stderr,
                flush=True,
            )
        if interval <= 0:
            return result.returncode
        print(f"Sleeping {interval}s until next sync...", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
