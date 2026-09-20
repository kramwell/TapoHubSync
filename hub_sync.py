from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import warnings
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pytapo import Tapo
from pytapo.const import CONNECTION_TIMEOUT
from pytapo.media_stream.downloader import Downloader
from pytapo.media_stream.convert import Convert
from pytapo.media_stream._utils import (
    generate_nonce,
    parse_http_headers,
    parse_http_response,
)
from pytapo.media_stream.crypto import AESHelper
from pytapo.media_stream.error import HttpStatusCodeException, KeyExchangeMissingException
from pytapo.media_stream.session import HttpMediaSession


ROOT = Path(__file__).resolve().parent


# --- Settings ---------------------------------------------------------------
# These read from environment variables (e.g. Docker) for now. To hardcode them,
# replace each os.environ.get(...) with a plain value here.
CONFIG_PATH = os.environ.get("TAPO_CONFIG", "tapohubsync.local.json")
KEY_FILE = os.environ.get("TAPO_KEY_FILE", "tapohubsync.local.key")
# Where recordings are mirrored; point at a mounted share for containers.
OUTPUT_DIR = os.environ.get("TAPO_OUTPUT_DIR", "recordings")

DEFAULT_CONFIG_PATH = ROOT / "tapohubsync.local.json"
DEFAULT_KEY_PATH = ROOT / "tapohubsync.local.key"
# Wide enough to cover any clip the hub still retains; only dates with video are fetched.
SYNC_START_DATE = "20200101"
# Media-download tuning.
WINDOW_SIZE = 50
STALL_TIMEOUT = 10.0
# Clips that ended within this many seconds are still being written; skip them.
FRESH_RECORDING_SECONDS = 60

log = logging.getLogger("tapohubsync")


def configure_logging() -> None:
    level = os.environ.get("TAPO_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )


class PlainMediaCrypto:
    def decrypt(self, data: bytes) -> bytes:
        return data

    def encrypt(self, data: bytes) -> bytes:
        return data


_ORIGINAL_AES_FROM_KEYEXCHANGE = AESHelper.from_keyexchange_and_password


def h200_media_crypto_from_keyexchange(
    cls,
    key_exchange,
    cloud_password,
    super_secret_key,
    encryptionMethod,
):
    raw = (
        key_exchange
        if isinstance(key_exchange, str)
        else key_exchange.decode("utf-8", errors="ignore")
    )
    if 'nonce=""' in raw:
        return PlainMediaCrypto()
    return _ORIGINAL_AES_FROM_KEYEXCHANGE(
        key_exchange,
        cloud_password,
        super_secret_key,
        encryptionMethod,
    )


AESHelper.from_keyexchange_and_password = classmethod(h200_media_crypto_from_keyexchange)


async def h200_media_start(self: HttpMediaSession) -> None:
    req_line = f"POST /stream{self.query_params_str} HTTP/1.1".encode()
    headers = {
        b"Content-Type": "multipart/mixed;boundary={}".format(
            self.client_boundary.decode(),
        ).encode(),
        b"Connection": b"keep-alive",
        b"Content-Length": b"-1",
    }
    if self.query_params_str and "playerId" in self.query_params:
        headers[b"X-Client-UUID"] = self.query_params["playerId"].encode()

    try:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.ip, self.port),
            timeout=CONNECTION_TIMEOUT,
        )

        await self._send_http_request(req_line, headers)
        data = await self._reader.readuntil(b"\r\n\r\n")
        _res_line, headers_block = data.split(b"\r\n", 1)
        res_headers = parse_http_headers(headers_block)

        content_length = int(res_headers.get("Content-Length", "0"))
        if content_length > 0:
            await self._reader.readexactly(content_length)

        self._auth_data = {
            i[0].strip().replace('"', ""): i[1].strip().replace('"', "")
            for i in (
                j.split("=")
                for j in res_headers["WWW-Authenticate"].split(" ", 1)[1].split(",")
            )
        }
        self._auth_data.update(
            {
                "username": self.username,
                "cnonce": generate_nonce(24).decode(),
                "nc": "00000001",
                "qop": "auth",
            }
        )

        challenge1 = hashlib.md5(
            ":".join(
                (self.username, self._auth_data["realm"], self.hashed_password)
            ).encode(),
        ).hexdigest()
        challenge2 = hashlib.md5(b"POST:/stream").hexdigest()

        self._auth_data["response"] = hashlib.md5(
            b":".join(
                (
                    challenge1.encode(),
                    self._auth_data["nonce"].encode(),
                    self._auth_data["nc"].encode(),
                    self._auth_data["cnonce"].encode(),
                    self._auth_data["qop"].encode(),
                    challenge2.encode(),
                ),
            ),
        ).hexdigest()

        self._authorization = (
            'Digest username="{username}",realm="{realm}"'
            ',uri="/stream",algorithm=MD5,'
            'nonce="{nonce}",nc={nc},cnonce="{cnonce}",qop={qop},'
            'response="{response}",opaque="{opaque}"'.format(
                **self._auth_data,
            ).encode()
        )
        headers[b"Authorization"] = self._authorization
        if "Set-Cookie" in res_headers:
            headers[b"Cookie"] = res_headers["Set-Cookie"].split(";", 1)[0].encode()

        if res_headers.get("Connection", "").lower() == "close":
            self._writer.close()
            await self._writer.wait_closed()
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.ip, self.port),
                timeout=CONNECTION_TIMEOUT,
            )

        await self._send_http_request(req_line, headers)

        data = await self._reader.readuntil(b"\r\n\r\n")
        res_line, headers_block = data.split(b"\r\n", 1)
        _, status_code, _ = parse_http_response(res_line)
        if status_code != 200:
            raise HttpStatusCodeException(status_code)

        res_headers = parse_http_headers(headers_block)
        if "Key-Exchange" not in res_headers:
            raise KeyExchangeMissingException

        boundary = None
        if "Content-Type" in res_headers:
            try:
                boundary = filter(
                    lambda chunk: chunk.startswith("boundary="),
                    res_headers["Content-Type"].split(";"),
                ).__next__()
                boundary = boundary.split("=")[1].encode()
            except Exception:
                boundary = None
        if not boundary:
            warnings.warn(
                "Server did not provide a multipart/mixed boundary. Assuming default.",
            )
        else:
            self._device_boundary = boundary

        self._key_exchange = res_headers["Key-Exchange"]
        self._aes = AESHelper.from_keyexchange_and_password(
            self._key_exchange.encode(),
            self.cloud_password.encode(),
            self.super_secret_key.encode(),
            self.encryptionMethod,
        )

        self._started = True
        self._response_handler_task = asyncio.create_task(
            self._device_response_handler_loop(),
        )
    except Exception:
        try:
            self._writer.close()
        except Exception:
            pass
        self._started = False
        raise


HttpMediaSession.start = h200_media_start


def h200_calculate_media_length(self: Convert) -> float | bool:
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_name = tmp.name
            tmp.write(self.writer.getvalue())
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "fatal",
                "-f",
                "mpegts",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                tmp_name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        value = result.stdout.strip()
        if result.returncode != 0 or not value:
            return False
        duration = float(value)
        self.known_lengths[self.addedChunks] = duration
        self.lengthLastCalculatedAtChunk = self.addedChunks
        return duration
    except (OSError, ValueError):
        return False
    finally:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)


Convert.calculateLength = h200_calculate_media_length


async def h200_convert_save(self, fileLocation, fileLength, method="ffmpeg"):
    # Mux the native H.264 video and G.711 audio into MKV with no re-encode.
    if method != "ffmpeg":
        raise Exception("Method not supported")

    temp_video = f"{fileLocation}.ts"
    with open(temp_video, "wb") as handle:
        handle.write(self.writer.getvalue())
    audio_format = self._get_audio_format()
    audio_rate = self._get_audio_rate()
    temp_audio = f"{fileLocation}.{audio_format}"
    with open(temp_audio, "wb") as handle:
        handle.write(self.audioWriter.getvalue())

    cmd = [
        "ffmpeg",
        "-hide_banner",
        # "fatal" hides benign raw-audio demux warnings (e.g. mp3 "Header
        # missing"); real mux failures still surface via the non-zero exit code.
        "-loglevel",
        "fatal",
        "-y",
        "-ss",
        "00:00:00",
        "-i",
        temp_video,
        "-f",
        audio_format,
        "-ar",
        str(audio_rate),
        "-i",
        temp_audio,
        "-t",
        str(fileLength),
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        str(fileLocation),
    ]
    try:
        subprocess.run(cmd, check=True)
    finally:
        Path(temp_video).unlink(missing_ok=True)
        Path(temp_audio).unlink(missing_ok=True)


Convert.save = h200_convert_save


_ORIGINAL_GET_BASIC_INFO = Tapo.getBasicInfo


def h200_get_basic_info(self: Tapo):
    # H200 general cameras (e.g. C410) don't answer getDeviceInfo over the child
    # passthrough; fall back to a minimal descriptor so construction succeeds.
    try:
        return _ORIGINAL_GET_BASIC_INFO(self)
    except Exception:
        return {"type": "SMART.IPCAMERA"}


Tapo.getBasicInfo = h200_get_basic_info


@dataclass
class Credentials:
    host: str
    user: str
    password: str
    cloud_password: str


def resolve_config_path(path: str | None) -> Path:
    if not path:
        return DEFAULT_CONFIG_PATH
    config_path = Path(path).expanduser()
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    return config_path


def resolve_key_path(path: str | None) -> Path:
    if not path:
        return DEFAULT_KEY_PATH
    key_path = Path(path).expanduser()
    if not key_path.is_absolute():
        key_path = ROOT / key_path
    return key_path


def load_or_create_key(path: Path) -> bytes:
    if path.exists():
        key = path.read_bytes().strip()
        os.chmod(path, 0o600)
        return key

    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key + b"\n")
    os.chmod(path, 0o600)
    return key


def load_key(path: Path) -> bytes:
    if not path.exists():
        raise SystemExit(
            f"Encrypted config needs key file, but it does not exist: {path}"
        )
    key = path.read_bytes().strip()
    os.chmod(path, 0o600)
    return key


def encrypt_config_payload(data: dict[str, Any], key: bytes) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return Fernet(key).encrypt(raw).decode("ascii")


def decrypt_config_payload(token: str, key: bytes) -> dict[str, Any]:
    try:
        raw = Fernet(key).decrypt(token.encode("ascii"))
    except (InvalidToken, ValueError) as err:
        raise SystemExit("Failed to decrypt local Tapo config.") from err

    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("Decrypted config must be a JSON object.")
    return data


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as err:
        raise SystemExit(f"Invalid JSON in {path}: {err}") from err
    if not isinstance(data, dict):
        raise SystemExit(f"Config must be a JSON object: {path}")
    if data.get("storage") == "fernet-local-key":
        key_file = data.get("key_file")
        key_path = resolve_key_path(key_file) if key_file else DEFAULT_KEY_PATH
        return decrypt_config_payload(data["payload"], load_key(key_path))
    return data


def save_config(path: Path, key_path: Path, creds: Credentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "host": creds.host,
        "user": creds.user,
        "password": creds.password,
        "cloud_password": creds.cloud_password,
    }
    data = {
        "version": 2,
        "storage": "fernet-local-key",
        "key_file": str(key_path.relative_to(ROOT))
        if key_path.is_relative_to(ROOT)
        else str(key_path),
        "payload": encrypt_config_payload(payload, load_or_create_key(key_path)),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.chmod(path, 0o600)
    print(f"Saved encrypted local credentials to {path}")
    print(f"Saved local encryption key to {key_path}")


def host_from_sources(config: dict[str, Any]) -> str:
    return config.get("host") or ""


def require_host(host: str) -> bool:
    if host:
        return True
    print("Hub IP address is required.", file=sys.stderr)
    return False


def prompt_text(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default or ""


def user_from_sources(config: dict[str, Any]) -> str:
    return config.get("user") or "admin"


def credentials_from_config(config: dict[str, Any]) -> Credentials | None:
    """Ready-to-use credentials from the encrypted config, or None if not configured."""
    host = host_from_sources(config)
    password = config.get("password") or config.get("hub_password")
    if not host or not password:
        return None
    cloud_password = (
        config.get("cloud_password")
        or config.get("cloudPassword")
        or password
    )
    return Credentials(
        host=host,
        user=user_from_sources(config),
        password=password,
        cloud_password=cloud_password,
    )


def prompt_credentials(host: str, config: dict[str, Any]) -> Credentials:
    """Interactive credential prompt used by --setup."""
    password = config.get("password") or config.get("hub_password")
    cloud_password = config.get("cloud_password") or config.get("cloudPassword")

    if not password:
        password = getpass.getpass(
            "Hub/device account password (hidden; try Tapo cloud password if unsure): "
        )
    if not cloud_password:
        cloud_password = getpass.getpass(
            "Tapo cloud password (hidden; Enter to reuse previous password): "
        )
        if not cloud_password:
            cloud_password = password

    return Credentials(
        host=host,
        user=user_from_sources(config),
        password=password,
        cloud_password=cloud_password,
    )


def connect_hub(creds: Credentials) -> Tapo:
    return Tapo(creds.host, creds.user, creds.password, creds.cloud_password)


def connect_device(
    creds: Credentials, device: dict[str, Any], user_id: str | None = None
) -> Tapo:
    device_client = Tapo(
        creds.host,
        creds.user,
        creds.password,
        creds.cloud_password,
        childID=device["device_id"],
    )
    # pytapo's downloader calls getUserID() synchronously from inside its own
    # asyncio loop, which deadlocks that loop. Pre-cache the account user id (the
    # hub answers this; the general-camera passthrough does not) so the in-loop
    # call is a no-op.
    device_client.userID = user_id or device_client.playerID
    return device_client


def can_connect(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str | None]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except OSError as err:
        return False, str(err)


def preflight_hub(host: str) -> bool:
    ok, error = can_connect(host, 443, timeout=3.0)
    if ok:
        return True

    print()
    print(f"Cannot reach the hubs control API at {host}:443")
    print(f"Error: {error}")
    print()
    print("Check:")
    print("- The hub is powered on and online in the Tapo app")
    print("- This PC is on the same LAN/VLAN as the hub")
    print("- The hub IP has not changed")
    print("- Guest Wi-Fi / AP isolation is disabled")
    print()
    return False


def unwrap_child_list(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        if "child_device_list" in result:
            return result["child_device_list"]
        if "childControl" in result:
            return result["childControl"].get("child_device_list", [])
    return []


def normalize_mac(value: str | None) -> str:
    return (value or "").replace(":", "").replace("-", "").upper()


def list_paired_devices(hub: Tapo) -> list[dict[str, Any]]:
    # The hub splits devices across two calls: general cameras (e.g. C410) come
    # from getGeneralDeviceList, while doorbells (e.g. D230) and other children
    # come from getChildDevices. The lists are disjoint, so merge both.
    devices: list[dict[str, Any]] = []

    try:
        result = hub.executeFunction(
            "getGeneralDeviceList",
            {"general_camera_manage": {"paired_general_device_list": {}}},
        )
        devices.extend(
            result.get("general_camera_manage", {})
            .get("paired_general_device_list", [])
        )
    except Exception:
        pass

    try:
        devices.extend(unwrap_child_list(hub.getChildDevices()))
    except Exception:
        pass

    normalized = []
    seen: set[str] = set()
    for device in devices:
        device_id = device.get("device_id") or device.get("deviceId")
        if not device_id or device_id in seen:
            continue
        seen.add(device_id)
        alias = device.get("alias") or device.get("device_name") or device.get("nickname")
        model = device.get("device_model") or device.get("model")
        mac = normalize_mac(device.get("mac") or device.get("device_mac"))
        normalized.append({
            **device,
            "device_id": device_id,
            "alias": alias,
            "device_model": model,
            "mac": mac,
        })

    return normalized


def local_timezone():
    return datetime.now().astimezone().tzinfo


def local_today() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d")


def date_to_utc_range(date: str) -> tuple[int, int]:
    day = datetime.strptime(date, "%Y%m%d").replace(tzinfo=local_timezone())
    start = int(day.timestamp())
    return start, start + 86399


def list_recording_dates(
    hub: Tapo,
    device: dict[str, Any],
    start_date: str,
    end_date: str,
) -> list[str]:
    result = hub.executeFunction(
        "searchDateWithVideo",
        {
            "playback": {
                "search_year_utility": {
                    "channel": [0],
                    "child_device_id": device["device_id"],
                    "child_device_mac": device["mac"],
                    "start_date": start_date,
                    "end_date": end_date,
                }
            }
        },
    )

    dates: list[str] = []
    for row in result.get("playback", {}).get("search_results", []):
        for value in row.values():
            if isinstance(value, dict) and "date" in value:
                dates.append(value["date"])
    return sorted(set(dates))


def list_recordings_for_day(
    hub: Tapo,
    device: dict[str, Any],
    date: str,
) -> list[dict[str, Any]]:
    start_time, end_time = date_to_utc_range(date)
    result = hub.executeFunction(
        "searchVideoWithUTC",
        {
            "playback": {
                "search_video_with_utc": {
                    "channel": 0,
                    "child_device_id": device["device_id"],
                    "child_device_mac": device["mac"],
                    "start_time": start_time,
                    "end_time": end_time,
                    "start_index": 0,
                    "end_index": 999,
                    "player_id": uuid.uuid4().hex.upper(),
                }
            }
        },
    )

    clips: list[dict[str, Any]] = []
    for row in result.get("playback", {}).get("search_video_results", []):
        for value in row.values():
            if isinstance(value, dict):
                value = {**value, "date": date}
                clips.append(value)
    return clips


def list_recordings(
    hub: Tapo,
    device: dict[str, Any],
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    clips: list[dict[str, Any]] = []
    for date in list_recording_dates(hub, device, start_date, end_date):
        clips.extend(list_recordings_for_day(hub, device, date))
    return sorted(clips, key=lambda item: int(item.get("startTime", 0)))


async def download_recording(
    device_client: Tapo,
    start_time: int,
    end_time: int,
    output: Path,
    window_size: int,
    stall_timeout: float,
) -> bool:
    final_output = output.with_suffix(".mkv")
    final_output.parent.mkdir(parents=True, exist_ok=True)
    final_output.unlink(missing_ok=True)

    downloader = Downloader(
        device_client,
        start_time,
        end_time,
        0,
        outputDirectory=str(final_output.parent) + os.sep,
        fileName=final_output.name,
        padding=0,
        overwriteFiles=True,
        window_size=window_size,
        stall_timeout=stall_timeout,
    )
    await downloader.downloadFile()
    if not final_output.exists() or final_output.stat().st_size == 0:
        final_output.unlink(missing_ok=True)
        return False

    log.info("saved MKV: %s", final_output)
    return final_output.stat().st_size > 0


def print_children(devices: list[dict[str, Any]]) -> None:
    for idx, device in enumerate(devices):
        print(
            f"{idx}: alias={device.get('alias')} "
            f"model={device.get('device_model')} "
            f"device_id={device.get('device_id')} "
            f"mac={device.get('mac')}"
        )


def run_first_setup() -> int:
    config_path = resolve_config_path(CONFIG_PATH)
    key_path = resolve_key_path(KEY_FILE)
    config = load_config(config_path)
    default_host = host_from_sources(config)

    print("TapoHubSync first setup")
    print()
    print("This will:")
    print("1. Check the hubs IP address")
    print("2. Save local encrypted credentials")
    print("3. List every paired device")
    print()

    host = prompt_text("Hub IP address", default_host)
    if not require_host(host):
        return 1
    if not preflight_hub(host):
        print()
        host = prompt_text("Hub IP address to use", host)
        if not preflight_hub(host):
            return 1

    creds = prompt_credentials(host, config)

    print()
    print(f"Connecting to hub at {creds.host} as {creds.user}...")
    try:
        hub = connect_hub(creds)
        devices = list_paired_devices(hub)
    except Exception as err:
        if "Invalid authentication data" in str(err):
            print(
                "Authentication failed. The hub/device account password or "
                "the Tapo cloud password is incorrect.",
                file=sys.stderr,
            )
            print("The configuration file was not changed.", file=sys.stderr)
            return 2

        print(f"The hubs API request failed: {err}", file=sys.stderr)
        print("The configuration file was not changed.", file=sys.stderr)
        return 1

    if not devices:
        print("No paired devices returned by the hub.")
        print("The configuration file was not changed.")
        return 1

    print()
    print("Paired devices:")
    print_children(devices)

    save_config(config_path, key_path, creds)
    print()
    print("Setup complete. You can now run:")
    print("python3 hub_sync.py --sync")
    return 0


def print_recordings(clips: list[dict[str, Any]]) -> None:
    if not clips:
        print("No recordings found.")
        return

    print(f"{'idx':>4} {'start_time':>10} {'end_time':>10} {'duration':>8} local_time")
    for idx, clip in enumerate(clips):
        start_time = int(clip["startTime"])
        end_time = int(clip["endTime"])
        when = datetime.fromtimestamp(start_time).astimezone().isoformat()
        print(
            f"{idx:>4} {start_time:>10} {end_time:>10} "
            f"{end_time - start_time:>7}s {when}"
        )


def output_path_for_clip(
    output_dir: str,
    device: dict[str, Any],
    start_time: int,
    extension: str = ".mkv",
) -> Path:
    local_start = datetime.fromtimestamp(start_time).astimezone()
    date_dir = local_start.strftime("%Y%m%d")
    timestamp = local_start.strftime("%Y%m%d_%H%M%S")
    alias = (device.get("alias") or "device").replace(" ", "_")
    device_id = (device.get("device_id") or "").replace(" ", "_")
    if not extension.startswith("."):
        extension = "." + extension
    device_dir = Path(output_dir) / alias
    if device_id:
        device_dir = device_dir / device_id
    return device_dir / date_dir / f"{alias}_{timestamp}{extension}"


def should_skip_output(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


async def _download_device_clips(
    device_client: Tapo,
    device: dict[str, Any],
    clips: list[dict[str, Any]],
) -> tuple[int, int, int, int]:
    ok_count = 0
    skip_count = 0
    fail_count = 0
    inprogress_count = 0
    total = len(clips)
    now = datetime.now().timestamp()
    for idx, clip in enumerate(clips):
        start_time = int(clip["startTime"])
        end_time = int(clip["endTime"])
        output = output_path_for_clip(OUTPUT_DIR, device, start_time)
        if should_skip_output(output):
            log.debug("[%d/%d] already downloaded: %s", idx + 1, total, output)
            skip_count += 1
            continue
        if end_time > now - FRESH_RECORDING_SECONDS:
            log.info(
                "[%d/%d] still recording (ended %ds ago), skipping: %s",
                idx + 1,
                total,
                int(now - end_time),
                output,
            )
            inprogress_count += 1
            continue
        log.info(
            "[%d/%d] download %ds -> %s",
            idx + 1,
            total,
            end_time - start_time,
            output,
        )
        try:
            ok = await download_recording(
                device_client,
                start_time,
                end_time,
                output,
                WINDOW_SIZE,
                STALL_TIMEOUT,
            )
        except Exception as err:
            log.error("[%d/%d] failed: %s", idx + 1, total, err)
            ok = False
        if ok:
            ok_count += 1
        else:
            fail_count += 1
    return ok_count, skip_count, fail_count, inprogress_count


def download_clips_for_device(
    creds: Credentials,
    device: dict[str, Any],
    clips: list[dict[str, Any]],
    user_id: str | None = None,
) -> tuple[int, int, int, int]:
    device_client = connect_device(creds, device, user_id)
    return asyncio.run(_download_device_clips(device_client, device, clips))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="TapoHubSync: list or download Tapo hub recordings for paired devices.",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="interactive first-run setup: hubs host and credentials",
    )
    parser.add_argument(
        "--devices",
        action="store_true",
        help="list paired devices (cameras and doorbells)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list every recording the hub holds for every device",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="mirror every clip the hub holds for all devices; skip already-downloaded, never delete",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging()

    if args.setup:
        return run_first_setup()

    if not (args.devices or args.list or args.sync):
        print(
            "Choose --setup, --devices, --list, or --sync.",
            file=sys.stderr,
        )
        return 2

    config = load_config(resolve_config_path(CONFIG_PATH))
    creds = credentials_from_config(config)
    if creds is None:
        print(
            "Hub is not configured. Run: python3 hub_sync.py --setup",
            file=sys.stderr,
        )
        return 1

    if not preflight_hub(creds.host):
        return 1

    log.info("connecting to hub at %s as %s", creds.host, creds.user)
    try:
        hub = connect_hub(creds)
        devices = list_paired_devices(hub)
    except Exception as err:
        if "Invalid authentication data" in str(err):
            log.error(
                "authentication failed. Stored credentials may be wrong; "
                "run: python3 hub_sync.py --setup"
            )
            return 2
        log.error("the hubs API request failed: %s", err)
        return 1

    if not devices:
        log.warning("no paired devices returned by the hub.")
        return 1

    if args.devices:
        print(f"Devices: {len(devices)}")
        print_children(devices)
        return 0

    start_date = SYNC_START_DATE
    end_date = local_today()

    if args.list:
        for device in devices:
            print(
                f"# device: alias={device.get('alias')} "
                f"device_id={device.get('device_id')}"
            )
            print_recordings(list_recordings(hub, device, start_date, end_date))
        return 0

    if args.sync:
        total_ok = 0
        total_skip = 0
        total_fail = 0
        total_inprogress = 0
        try:
            user_id = hub.getUserID()
        except Exception:
            user_id = None
        for device in devices:
            clips = list_recordings(hub, device, start_date, end_date)
            log.info(
                "device alias=%s device_id=%s clips=%d",
                device.get("alias"),
                device.get("device_id"),
                len(clips),
            )
            if not clips:
                continue
            try:
                ok, skip, fail, inprog = download_clips_for_device(
                    creds, device, clips, user_id
                )
            except Exception as err:
                log.error("device failed: %s", err)
                total_fail += len(clips)
                continue
            total_ok += ok
            total_skip += skip
            total_fail += fail
            total_inprogress += inprog
        log.info(
            "Done. downloaded=%d already_downloaded=%d in_progress=%d failed=%d",
            total_ok,
            total_skip,
            total_inprogress,
            total_fail,
        )
        return 0 if total_fail == 0 else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
