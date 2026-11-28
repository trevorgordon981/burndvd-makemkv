"""Zero-babysit multi-drive TV box-set watch mode (`burndvd --watch`).

Polls optical drives via MakeMKV `DRV:` enumeration, identifies each physical
drive by hardware serial (falling back to `/dev/diskN`), serializes disc
launches through `compute_next_tv_episode` so concurrent drives never collide
on TV episode numbers, guards against infinite re-rips of un-ejected discs,
and monitors child `ripqueue` processes until completion or shutdown.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import inspect
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, TextIO

from burndvd import config as burndvd_config
from burndvd import metadata as burndvd_metadata
from burndvd.config import (
    DISKUTIL_BIN,
    NFS_MEDIA_PREFIX,
    SMB_MEDIA_PREFIX,
    fmt_dur,
)
from burndvd.drive import (
    DRIVE_DEVICE_ENV,
    DRIVE_SERIAL_ENV,
    DRUTIL_DRIVE_ENV,
    _drutil_list_drives,
    _drutil_status_output,
    _normalize_device_node,
    _parse_disk_node_from_status,
    _status_has_media,
    active_busy_disc_claims,
    drive_released_marker_path,
    is_state_drive_released,
    parse_makemkv_drives,
)
from burndvd.metadata import (
    ACTIVE_TV_SLOTS_ENV,
    _compact,
    _norm_media_root,
    compute_next_tv_episode,
    episode_contract,
    find_episode_plan_conflict,
    scan_season_existing_episodes,
    volume_matches_show_intent,
    write_queue,
)

DEFAULT_WATCH_MAKEMKVCON = "/opt/homebrew/bin/makemkvcon"
DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_SCAN_WAIT_S = 120.0
USER_CONFIG_DEFAULT_PATH = Path.home() / ".config" / "burndvd" / "config"
DRIVE_TOPOLOGY_DEFAULT_PATH = Path.home() / ".cache" / "burndvd" / "drive-topology.json"

_WATCHER_REGISTRY_LOCK = threading.Lock()
_SEASON_THREAD_LOCKS: dict[str, threading.Lock] = {}
_LIVE_WATCHER_RIPS: dict[str, list[ActiveRip]] = {}


def _season_coord_key(
    show: str,
    season: int,
    target_root: str,
    media_smb_root: str = SMB_MEDIA_PREFIX,
    media_nfs_root: str = NFS_MEDIA_PREFIX,
) -> str:
    norm_root = _norm_media_root(target_root, media_smb_root, media_nfs_root)
    return f"{_compact(show)}|S{int(season)}|{norm_root}"


def _get_season_thread_lock(season_key: str) -> threading.Lock:
    with _WATCHER_REGISTRY_LOCK:
        lock = _SEASON_THREAD_LOCKS.get(season_key)
        if lock is None:
            lock = threading.Lock()
            _SEASON_THREAD_LOCKS[season_key] = lock
        return lock


@contextlib.contextmanager
def _watch_season_lock(work_dir: Path, season_key: str) -> Iterator[None]:
    """Serialize Watch episode planning + launch across threads and independent processes."""
    tlock = _get_season_thread_lock(season_key)
    locks_dir = Path(work_dir) / ".locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(season_key.encode("utf-8")).hexdigest()[:16]
    lock_file = locks_dir / f"watch-season-{digest}.lock"
    with tlock:
        fh = lock_file.open("a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                fh.close()
            except OSError:
                pass


def _spawn_accepts_start_new_session(spawn_fn: Callable[..., Any]) -> bool:
    """Return True if `spawn_fn` accepts `start_new_session=True` (always True for Popen)."""
    if spawn_fn is subprocess.Popen:
        return True
    try:
        sig = inspect.signature(spawn_fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == "start_new_session":
            return True
    return False


def _parse_bool(value: Any) -> bool | None:
    """Parse common truthy/falsy representations into bool, or None if unset."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        cleaned = value.strip().strip('"').strip("'").lower()
        if cleaned in ("1", "true", "yes", "on"):
            return True
        if cleaned in ("0", "false", "no", "off"):
            return False
    return None


def _read_user_config_fallback(path: Path) -> dict[str, Any]:
    """Read `~/.config/burndvd/config` supporting JSON or simple key=value/INI."""
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    stripped = raw.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return data
        except ValueError:
            return {}
    parsed: dict[str, Any] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            key = k.strip().lower().replace("-", "_")
            val = v.split("#", 1)[0].strip().strip('"').strip("'")
            if key:
                parsed[key] = val
    return parsed


def load_user_config_safe(config_path: Path | None = None) -> dict[str, Any]:
    """Load user config via `burndvd.config.load_user_config` if available, else fallback."""
    loader = getattr(burndvd_config, "load_user_config", None)
    if callable(loader):
        try:
            data = loader() if config_path is None else loader(config_path)
        except TypeError:
            if config_path is not None:
                return _read_user_config_fallback(config_path)
            data = None
        except Exception:
            data = None
        if isinstance(data, Mapping):
            return dict(data)
        if data is not None:
            return {
                k: getattr(data, k)
                for k in dir(data)
                if not k.startswith("_") and not callable(getattr(data, k))
            }
    return _read_user_config_fallback(config_path or USER_CONFIG_DEFAULT_PATH)


def resolve_no_extras(
    cli_no_extras: bool | None,
    *,
    config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Resolve effective `no_extras` flag: CLI > BURNDVD_NO_EXTRAS > user config > False."""
    if cli_no_extras is not None:
        return bool(cli_no_extras)
    env_map = os.environ if env is None else env
    raw_env = env_map.get("BURNDVD_NO_EXTRAS", "")
    parsed_env = _parse_bool(raw_env)
    if parsed_env is not None:
        return parsed_env
    cfg = load_user_config_safe(config_path)
    for key in ("no_extras", "no-extras"):
        if key in cfg:
            parsed_cfg = _parse_bool(cfg[key])
            if parsed_cfg is not None:
                return parsed_cfg
    return False


def resolve_default_target_root(
    show: str,
    media_format: str = "BD",
    *,
    nfs_root: str | Path = NFS_MEDIA_PREFIX,
    smb_root: str | Path = SMB_MEDIA_PREFIX,
) -> str:
    """Compute default TV target root, preferring writable NFS over SMB."""
    nfs_path = Path(nfs_root)
    smb_path = Path(smb_root)
    subdir = "TV Shows 4K" if str(media_format).strip().upper() == "4K" else "TV Shows"
    if nfs_path.is_dir() and os.access(nfs_path, os.W_OK):
        nas_root = nfs_path
    else:
        nas_root = smb_path
    return str(nas_root / subdir / show)


def drive_identity_key(drive_row: Mapping[str, Any]) -> str:
    """Return stable physical identity for a drive: hardware serial, else device node."""
    serial = str(drive_row.get("serial") or "").strip()
    if serial:
        return serial
    node = str(drive_row.get("device_node") or "").strip()
    if node:
        return node
    raw_dev = str(drive_row.get("raw_device") or "").strip()
    if raw_dev:
        return raw_dev
    return f"disc:{drive_row.get('index', 0)}"


def slugify_volume(value: str) -> str:
    """Return a filesystem-safe lowercase slug for log/queue filenames."""
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or "disc"


def read_drive_released_marker(state_path: str | Path | None) -> dict[str, Any] | None:
    """Read `<state>.drive_released` JSON metadata if the drive has been released.

    Returns `None` when the state file does not have a drive-released marker.
    Returns `{}` when the marker exists but cannot be parsed as a JSON object
    (so drive release is recognized without falsely asserting eject succeeded).
    """
    if not is_state_drive_released(state_path):
        return None
    marker = drive_released_marker_path(state_path)
    if marker is None:
        return None
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def is_eject_confirmed(marker: Mapping[str, Any] | None) -> bool:
    """Return True only when a drive-released marker explicitly confirms `ejected: true`."""
    return bool(marker is not None and marker.get("ejected") is True)


def load_drive_topology(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load cached optical drive topology (`drutil_to_serial`, `serial_to_slot`, `drutil_to_slot`)."""
    topo_path = path or DRIVE_TOPOLOGY_DEFAULT_PATH
    out: dict[str, dict[str, Any]] = {
        "drutil_to_serial": {},
        "serial_to_slot": {},
        "drutil_to_slot": {},
    }
    if not topo_path.is_file():
        return out
    try:
        data = json.loads(topo_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    d2s = data.get("drutil_to_serial")
    if isinstance(d2s, dict):
        for k, v in d2s.items():
            if str(k).strip() and str(v).strip():
                out["drutil_to_serial"][str(k).strip()] = str(v).strip()
    s2slot = data.get("serial_to_slot")
    if isinstance(s2slot, dict):
        for k, v in s2slot.items():
            if str(k).strip() and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                out["serial_to_slot"][str(k).strip().upper()] = int(v)
    d2slot = data.get("drutil_to_slot")
    if isinstance(d2slot, dict):
        for k, v in d2slot.items():
            if str(k).strip() and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                out["drutil_to_slot"][str(k).strip()] = int(v)
    return out


def _merge_topology_dicts(
    base: Mapping[str, Any] | None,
    overlay: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {
        "drutil_to_serial": {},
        "serial_to_slot": {},
        "drutil_to_slot": {},
    }
    for src in (base, overlay):
        if not isinstance(src, Mapping):
            continue
        for k, v in (src.get("drutil_to_serial") or {}).items():
            if str(k).strip() and str(v).strip():
                merged["drutil_to_serial"][str(k).strip()] = str(v).strip()
        for k, v in (src.get("serial_to_slot") or {}).items():
            if str(k).strip() and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                merged["serial_to_slot"][str(k).strip().upper()] = int(v)
        for k, v in (src.get("drutil_to_slot") or {}).items():
            if str(k).strip() and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                merged["drutil_to_slot"][str(k).strip()] = int(v)
    return merged


def save_drive_topology(
    rows: Iterable[Mapping[str, Any]],
    *,
    path: Path | None = None,
    drutil_by_dev: Mapping[str, int] | None = None,
) -> dict[str, dict[str, Any]]:
    """Persist observed drive topology (preserving `drutil_to_serial` compatibility with `bin/burndvd`)."""
    topo_path = path or DRIVE_TOPOLOGY_DEFAULT_PATH
    topo = load_drive_topology(topo_path)
    changed = False
    for row in rows:
        serial = str(row.get("serial") or "").strip()
        raw_idx = row.get("index")
        idx = int(raw_idx) if isinstance(raw_idx, int) and not isinstance(raw_idx, bool) and raw_idx >= 0 else None
        raw_drutil = row.get("drutil_drive")
        drutil_idx: int | None = None
        if isinstance(raw_drutil, int) and not isinstance(raw_drutil, bool) and raw_drutil >= 1:
            drutil_idx = raw_drutil
        elif drutil_by_dev:
            norm_dev = _normalize_device_node(str(row.get("device_node") or ""))
            if norm_dev and norm_dev in drutil_by_dev:
                drutil_idx = int(drutil_by_dev[norm_dev])
        if serial and idx is not None:
            ser_u = serial.upper()
            if topo["serial_to_slot"].get(ser_u) != idx:
                topo["serial_to_slot"][ser_u] = idx
                changed = True
        if drutil_idx is not None and serial:
            if topo["drutil_to_serial"].get(str(drutil_idx)) != serial:
                topo["drutil_to_serial"][str(drutil_idx)] = serial
                changed = True
        if drutil_idx is not None and idx is not None:
            if topo["drutil_to_slot"].get(str(drutil_idx)) != idx:
                topo["drutil_to_slot"][str(drutil_idx)] = idx
                changed = True
    if changed:
        try:
            topo_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = topo_path.with_name(f".{topo_path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(topo, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, topo_path)
        except OSError:
            pass
    return topo


def _volume_for_device(device_node: str, *, timeout_s: float = 5.0) -> str:
    """Return optical volume label via read-only `diskutil info /dev/diskN`, or ''."""
    norm = _normalize_device_node(device_node)
    if not norm:
        return ""
    try:
        proc = subprocess.run(
            [DISKUTIL_BIN, "info", norm],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    for line in (proc.stdout or "").splitlines():
        if "Volume Name" in line and ":" in line:
            val = line.split(":", 1)[1].strip()
            if val and "not applicable" not in val.lower():
                return val
    return ""


def _probe_free_makemkv_slot(
    makemkvcon: str,
    slot_idx: int,
    *,
    timeout_s: float = 10.0,
) -> tuple[str, str, str]:
    """Probe a single confirmed-free MakeMKV `disc:N` slot without touching busy drives.

    Returns `(status, serial, volume)` where status is `'loaded'`, `'empty'`, or `'unknown'`.
    Terminates as soon as `CINFO:2` or an empty-tray `MSG:5010` arrives.
    """
    if not makemkvcon:
        return ("unknown", "", "")
    try:
        proc = subprocess.Popen(
            [
                makemkvcon,
                "-r",
                "--noscan",
                "--cache=128",
                "--minlength=36000",
                "info",
                f"disc:{int(slot_idx)}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError:
        return ("unknown", "", "")

    line_q: queue.Queue[tuple[str, str]] = queue.Queue()

    def _reader() -> None:
        try:
            if proc.stdout is not None:
                for raw in proc.stdout:
                    line_q.put(("line", raw))
        finally:
            line_q.put(("eof", ""))

    threading.Thread(target=_reader, daemon=True).start()
    deadline = time.monotonic() + max(0.5, float(timeout_s))
    slot_serial = ""
    volume = ""
    saw_mkv_output = False
    status: str | None = None
    loaded_grace_deadline: float | None = None

    try:
        while True:
            now = time.monotonic()
            limit = (
                deadline
                if loaded_grace_deadline is None
                else min(deadline, loaded_grace_deadline)
            )
            rem = limit - now
            if rem <= 0:
                if status is None and saw_mkv_output:
                    status = "loaded"
                break
            try:
                kind, payload = line_q.get(timeout=min(0.25, rem))
            except queue.Empty:
                continue
            if kind == "eof":
                break
            line = payload.strip()
            if not line:
                continue
            if line.startswith(("DRV:", "MSG:", "CINFO:", "TINFO:")):
                saw_mkv_output = True
            if line.startswith("DRV:"):
                parsed_rows = parse_makemkv_drives(line)
                for row in parsed_rows:
                    if row["index"] == int(slot_idx) and row.get("serial"):
                        slot_serial = str(row["serial"]).strip()
            elif line.startswith("CINFO:"):
                m = re.match(r'^CINFO:2,\d+,"([^"]*)"', line)
                if m and m.group(1):
                    volume = m.group(1).strip()
                status = "loaded"
                break
            elif line.startswith(("MSG:3007", "MSG:3307", "MSG:3006", "MSG:3028")):
                status = "loaded"
                if loaded_grace_deadline is None:
                    loaded_grace_deadline = time.monotonic() + 0.25
            elif line.startswith("MSG:5010") or re.search(
                r"TRAY OPEN|MEDIUM NOT PRESENT", line, re.IGNORECASE
            ):
                if status != "loaded":
                    status = "empty"
                break
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass

    return (status or "unknown", slot_serial, volume)


def _list_drives_nonblocking_fallback(
    makemkvcon: str,
    *,
    busy_slots: set[int],
    busy_devices: set[str],
    busy_serials_cf: set[str],
    busy_drutil_drives: set[int],
    topology: Mapping[str, Any],
    probe_slot_timeout_s: float = 10.0,
) -> list[dict[str, Any]]:
    """Inspect idle optical drives via `drutil` + `diskutil` + cached topology without touching busy slots."""
    drutil_indices = _drutil_list_drives()
    if not drutil_indices:
        return []

    drutil_to_serial: dict[str, str] = dict(topology.get("drutil_to_serial") or {})
    serial_to_slot: dict[str, int] = {
        str(k).upper(): int(v)
        for k, v in (topology.get("serial_to_slot") or {}).items()
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0
    }
    slot_to_serial: dict[int, str] = {}
    _slot_serial_counts: dict[int, int] = {}
    for ser_u, sl in serial_to_slot.items():
        _slot_serial_counts[sl] = _slot_serial_counts.get(sl, 0) + 1
    for ser_u, sl in serial_to_slot.items():
        if _slot_serial_counts.get(sl) == 1:
            slot_to_serial[sl] = ser_u
    drutil_to_slot: dict[str, int] = {
        str(k): int(v)
        for k, v in (topology.get("drutil_to_slot") or {}).items()
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0
    }

    idle_empty: list[dict[str, Any]] = []
    avail_loaded: list[dict[str, Any]] = []

    for ix in drutil_indices:
        serial = str(drutil_to_serial.get(str(ix), "")).strip()
        if ix in busy_drutil_drives or (serial and serial.casefold() in busy_serials_cf):
            continue
        status_out = _drutil_status_output(ix)
        if status_out is None:
            continue
        node = _normalize_device_node(_parse_disk_node_from_status(status_out))
        has_media = _status_has_media(status_out)
        if node and node in busy_devices:
            continue
        if not has_media or not node:
            mapped_slot = (
                serial_to_slot.get(serial.upper())
                if serial
                else None
            )
            if mapped_slot is None:
                mapped_slot = drutil_to_slot.get(str(ix))
            if mapped_slot is None or mapped_slot in busy_slots:
                mapped_slot = max(0, ix - 1)
            idle_empty.append(
                {
                    "index": mapped_slot,
                    "state": 0,
                    "flags": 0,
                    "disc_flags": 0,
                    "drive_desc": "",
                    "serial": serial,
                    "volume": "",
                    "raw_device": "",
                    "device_node": None,
                    "has_media": False,
                    "drutil_drive": ix,
                }
            )
            continue

        disk_num = int(node.rsplit("disk", 1)[1])
        vol = _volume_for_device(node)
        avail_loaded.append(
            {
                "disk_num": disk_num,
                "drutil_drive": ix,
                "device_node": node,
                "serial": serial,
                "volume": vol,
            }
        )

    avail_loaded.sort(key=lambda item: (item["disk_num"], item["drutil_drive"]))

    known_slots = list(serial_to_slot.values()) + list(drutil_to_slot.values())
    total_slots = max(
        len(drutil_indices),
        (max(busy_slots) + 1) if busy_slots else 0,
        (max(known_slots) + 1) if known_slots else 0,
    )
    free_slots = [k for k in range(total_slots) if k not in busy_slots]

    if not avail_loaded:
        return idle_empty
    if not free_slots or len(avail_loaded) > len(free_slots):
        # Refuse unsafe fallback when we cannot map loaded drives without risking a busy slot.
        return idle_empty

    # Fast path 1: Every loaded drive has a unique, confirmed-free cached topology slot and a volume label.
    topo_paired: list[tuple[dict[str, Any], int, str, str]] = []
    used_topo_slots: set[int] = set()
    all_topo_mapped = True
    for item in avail_loaded:
        ser = item["serial"]
        slot = serial_to_slot.get(ser.upper()) if ser else None
        if slot is None:
            slot = drutil_to_slot.get(str(item["drutil_drive"]))
        if (
            slot is not None
            and slot in free_slots
            and slot not in used_topo_slots
            and item["volume"]
        ):
            used_topo_slots.add(slot)
            topo_paired.append((item, slot, ser, item["volume"]))
        else:
            all_topo_mapped = False
            break

    paired: list[tuple[dict[str, Any], int, str, str]] = []
    if all_topo_mapped and topo_paired:
        paired = topo_paired
    elif len(avail_loaded) == 1 and len(free_slots) == 1:
        # Fast path 2: Exactly one free slot and one loaded non-busy drive.
        item = avail_loaded[0]
        slot = free_slots[0]
        ser = item["serial"]
        vol = item["volume"]
        if not vol and makemkvcon:
            st, probed_ser, probed_vol = _probe_free_makemkv_slot(
                makemkvcon, slot, timeout_s=probe_slot_timeout_s
            )
            if st != "empty":
                ser = ser or probed_ser
                vol = vol or probed_vol
        paired = [(item, slot, ser, vol)]
    else:
        # Targeted probe of FREE slots only — never probes any slot in `busy_slots`.
        loaded_slots: list[tuple[int, str, str]] = []
        unknown_slots: list[tuple[int, str, str]] = []
        empty_slots: list[int] = []
        for k in free_slots:
            st, ser, svol = _probe_free_makemkv_slot(
                makemkvcon, k, timeout_s=probe_slot_timeout_s
            )
            if st == "loaded":
                loaded_slots.append((k, ser, svol))
                if len(avail_loaded) == 1:
                    break
            elif st == "empty":
                empty_slots.append(k)
            else:
                unknown_slots.append((k, ser, svol))

        if loaded_slots:
            rem_items = list(avail_loaded)
            rem_slots = list(loaded_slots)
            # 1. Exact volume match
            for slot_entry in list(rem_slots):
                k, ser, svol = slot_entry
                if not svol:
                    continue
                hits = [
                    it
                    for it in rem_items
                    if it["volume"] and it["volume"].casefold() == svol.casefold()
                ]
                if len(hits) == 1:
                    it = hits[0]
                    paired.append((it, k, it["serial"] or ser, it["volume"] or svol))
                    rem_items.remove(it)
                    rem_slots.remove(slot_entry)
            # 2. Cached drutil -> serial match
            for slot_entry in list(rem_slots):
                k, ser, svol = slot_entry
                if not ser:
                    continue
                hits = [
                    it
                    for it in rem_items
                    if drutil_to_serial.get(str(it["drutil_drive"]), "").casefold()
                    == ser.casefold()
                ]
                if len(hits) == 1:
                    it = hits[0]
                    paired.append((it, k, it["serial"] or ser, it["volume"] or svol))
                    rem_items.remove(it)
                    rem_slots.remove(slot_entry)
            # 3. Cached drutil -> slot match
            for slot_entry in list(rem_slots):
                k, ser, svol = slot_entry
                hits = [
                    it
                    for it in rem_items
                    if drutil_to_slot.get(str(it["drutil_drive"])) == k
                ]
                if len(hits) == 1:
                    it = hits[0]
                    paired.append((it, k, it["serial"] or ser, it["volume"] or svol))
                    rem_items.remove(it)
                    rem_slots.remove(slot_entry)
            # 4. Remaining deterministic 1-to-1 pairing across confirmed-loaded slots
            for it, (k, ser, svol) in zip(rem_items, rem_slots):
                paired.append((it, k, it["serial"] or ser, it["volume"] or svol))
        elif len(avail_loaded) == len(free_slots) and not empty_slots:
            # Probing was unavailable (e.g. MakeMKV binary didn't answer), but counts match exactly.
            for it, k in zip(avail_loaded, free_slots):
                paired.append((it, k, it["serial"], it["volume"]))

    rows: list[dict[str, Any]] = list(idle_empty)
    for item, slot_idx, ser, vol in paired:
        if slot_idx in busy_slots:
            continue
        resolved_serial = ser or slot_to_serial.get(int(slot_idx), "")
        node = item["device_node"]
        raw_dev = node.replace("/dev/disk", "/dev/rdisk") if node else ""
        rows.append(
            {
                "index": int(slot_idx),
                "state": 2,
                "flags": 0,
                "disc_flags": 0,
                "drive_desc": "",
                "serial": resolved_serial,
                "volume": vol,
                "raw_device": raw_dev,
                "device_node": node,
                "has_media": True,
                "drutil_drive": int(item["drutil_drive"]),
            }
        )
    rows.sort(key=lambda r: (int(r.get("index", 0)), drive_identity_key(r)))
    return rows


def list_makemkv_drives(
    makemkvcon: str = DEFAULT_WATCH_MAKEMKVCON,
    *,
    timeout_s: float = 20.0,
    busy_slots: Iterable[int] = (),
    busy_devices: Iterable[str] = (),
    busy_serials: Iterable[str] = (),
    busy_drutil_drives: Iterable[int] = (),
    busy_claims: Mapping[int, Mapping[str, Any]] | None = None,
    topology_path: Path | None = None,
    known_topology: Mapping[str, Any] | None = None,
    allow_global_when_busy: bool = False,
    probe_slot_timeout_s: float = 10.0,
) -> list[dict[str, Any]]:
    """Enumerate optical drives without blocking on busy sibling MakeMKV readers.

    When no sibling optical drive is actively ripping, runs `makemkvcon -r info list`
    and updates the cached topology. When a sibling drive is actively ripping (or when
    global `info list` fails/times out), falls back safely to non-blocking `drutil` +
    `diskutil` + cached topology and targeted free-slot probes without ever targeting
    a busy slot.
    """
    claims = active_busy_disc_claims() if busy_claims is None else busy_claims
    busy_slot_set: set[int] = {int(k) for k in (busy_slots or ())}
    busy_dev_set: set[str] = set()
    for d in busy_devices or ():
        norm = _normalize_device_node(str(d))
        if norm:
            busy_dev_set.add(norm)
    busy_serial_set: set[str] = {
        str(s).strip().casefold() for s in (busy_serials or ()) if str(s).strip()
    }
    busy_drutil_set: set[int] = {
        int(ix) for ix in (busy_drutil_drives or ()) if int(ix) >= 1
    }
    for slot_k, meta in (claims or {}).items():
        busy_slot_set.add(int(slot_k))
        if isinstance(meta, Mapping):
            c_dev = _normalize_device_node(str(meta.get("dev") or ""))
            if c_dev:
                busy_dev_set.add(c_dev)
            c_ser = str(meta.get("serial") or "").strip().casefold()
            if c_ser:
                busy_serial_set.add(c_ser)

    topology = _merge_topology_dicts(load_drive_topology(topology_path), known_topology)
    has_busy_sibling = bool(
        busy_slot_set or busy_dev_set or busy_serial_set or busy_drutil_set
    )

    if not has_busy_sibling or allow_global_when_busy:
        try:
            proc = subprocess.run(
                [makemkvcon, "-r", "info", "list"],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            if proc.returncode == 0 or (proc.stdout or "").strip():
                rows = parse_makemkv_drives(proc.stdout or "")
                if rows:
                    drutil_by_dev: dict[str, int] = {}
                    for ix in _drutil_list_drives():
                        st_out = _drutil_status_output(ix)
                        node = _normalize_device_node(_parse_disk_node_from_status(st_out))
                        if node:
                            drutil_by_dev[node] = ix
                    for row in rows:
                        norm_node = _normalize_device_node(str(row.get("device_node") or ""))
                        if norm_node and norm_node in drutil_by_dev:
                            row["drutil_drive"] = drutil_by_dev[norm_node]
                    save_drive_topology(
                        rows, path=topology_path, drutil_by_dev=drutil_by_dev
                    )
                    return rows
        except (subprocess.TimeoutExpired, OSError):
            pass

    return _list_drives_nonblocking_fallback(
        makemkvcon,
        busy_slots=busy_slot_set,
        busy_devices=busy_dev_set,
        busy_serials_cf=busy_serial_set,
        busy_drutil_drives=busy_drutil_set,
        topology=topology,
        probe_slot_timeout_s=probe_slot_timeout_s,
    )


def resolve_ripqueue_base_cmd(
    ripqueue_py: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Resolve the command prefix to invoke `ripqueue`."""
    env_map = os.environ if env is None else env
    candidate = ripqueue_py or env_map.get("BURNDVD_RIPQUEUE_PY", "").strip()
    if not candidate:
        default_script = Path.home() / "scripts" / "ripqueue.py"
        if default_script.is_file():
            candidate = str(default_script)
    if candidate:
        if candidate.endswith(".py"):
            return [sys.executable, candidate]
        return [candidate]
    return [sys.executable, "-m", "burndvd.cli.ripqueue"]


def build_ripqueue_command(
    *,
    queue_path: Path,
    state_path: Path,
    disc_index: int,
    makemkvcon: str = DEFAULT_WATCH_MAKEMKVCON,
    no_extras: bool = False,
    ripqueue_py: str | None = None,
    env: Mapping[str, str] | None = None,
    expected_volume: str | None = None,
    strict_disc_match: bool = False,
    allowed_volumes: Iterable[str] = (),
    allowed_fingerprints: Iterable[str] = (),
) -> list[str]:
    """Build the non-interactive `ripqueue` subprocess command for one disc."""
    cmd = resolve_ripqueue_base_cmd(ripqueue_py, env=env)
    cmd.extend(
        [
            "--queue",
            str(queue_path),
            "--state",
            str(state_path),
            "--makemkvcon",
            str(makemkvcon),
            "--device",
            f"disc:{int(disc_index)}",
            "--non-interactive",
            "--on-fail=retry",
            "--verify",
        ]
    )
    if no_extras:
        cmd.append("--no-extras")
    if expected_volume:
        cmd.extend(["--expected-volume", str(expected_volume)])
    if strict_disc_match:
        cmd.append("--strict-disc-match")
    for pattern in allowed_volumes or ():
        cleaned = str(pattern).strip()
        if cleaned:
            cmd.extend(["--allow-volume", cleaned])
    for fp in allowed_fingerprints or ():
        cleaned = str(fp).strip()
        if cleaned:
            cmd.extend(["--allow-fingerprint", cleaned])
    return cmd


def build_ripqueue_env(
    *,
    device_node: str,
    drive_serial: str,
    drutil_drive: int,
    no_extras: bool,
    slots_dir: Path | str | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct child subprocess environment pinned to one physical drive."""
    env = dict(os.environ if base_env is None else base_env)
    if device_node:
        env[DRIVE_DEVICE_ENV] = device_node
    else:
        env.pop(DRIVE_DEVICE_ENV, None)
    if drive_serial:
        env[DRIVE_SERIAL_ENV] = drive_serial
    else:
        env.pop(DRIVE_SERIAL_ENV, None)
    env[DRUTIL_DRIVE_ENV] = str(int(drutil_drive))
    env["BURNDVD_NO_EXTRAS"] = "1" if no_extras else "0"
    if slots_dir is not None:
        env[ACTIVE_TV_SLOTS_ENV] = str(slots_dir)
    return env


def notify_slack(
    text: str,
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 10.0,
) -> bool:
    """Best-effort Slack notification via webhook or Bot API. Never raises."""
    env_map = os.environ if env is None else env
    webhook = env_map.get("BURNDVD_SLACK_WEBHOOK", "").strip()
    token = env_map.get("BURNDVD_SLACK_TOKEN", "").strip()
    channel = env_map.get("BURNDVD_SLACK_CHANNEL", "").strip()

    try:
        if webhook:
            payload = json.dumps({"text": text}).encode("utf-8")
            req = urllib.request.Request(
                webhook,
                data=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_s):
                return True
        if token and channel:
            payload = json.dumps(
                {"channel": channel, "text": text, "unfurl_links": False}
            ).encode("utf-8")
            req = urllib.request.Request(
                "https://slack.com/api/chat.postMessage",
                data=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_s):
                return True
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return False


@dataclass
class WatchConfig:
    """Runtime configuration for multi-drive box-set watch mode."""

    show: str
    season: int
    media_format: str = "BD"
    target_root: str = ""
    no_extras: bool = False
    poll_interval: float = DEFAULT_POLL_INTERVAL_S
    scan_wait: float = DEFAULT_SCAN_WAIT_S
    episode_start: int | None = None
    max_discs: int = 0
    makemkvcon: str = DEFAULT_WATCH_MAKEMKVCON
    ripqueue_py: str | None = None
    log_dir: Path | None = None
    work_dir: Path | None = None
    slots_dir: Path | None = None
    media_smb_root: str = SMB_MEDIA_PREFIX
    media_nfs_root: str = NFS_MEDIA_PREFIX
    allowed_volumes: tuple[str, ...] = ()
    allowed_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.media_format = str(self.media_format or "BD").strip().upper()
        self.allowed_volumes = tuple(
            str(v).strip() for v in (self.allowed_volumes or ()) if str(v).strip()
        )
        self.allowed_fingerprints = tuple(
            str(fp).strip().lower()
            for fp in (self.allowed_fingerprints or ())
            if str(fp).strip()
        )
        if not self.target_root:
            self.target_root = resolve_default_target_root(
                self.show,
                self.media_format,
                nfs_root=self.media_nfs_root,
                smb_root=self.media_smb_root,
            )
        if self.log_dir is None:
            self.log_dir = Path(
                os.environ.get("BURNDVD_LOG_DIR", str(Path.home() / "logs"))
            )
        else:
            self.log_dir = Path(self.log_dir)
        if self.work_dir is None:
            self.work_dir = Path.home() / ".cache" / "burndvd" / "watch"
        else:
            self.work_dir = Path(self.work_dir)
        if self.slots_dir is not None:
            self.slots_dir = Path(self.slots_dir)

    @property
    def season_dir(self) -> Path:
        return Path(self.target_root) / f"Season {int(self.season):02d}"


@dataclass
class ActiveRip:
    """Tracks one live `ripqueue` subprocess pinned to a physical drive."""

    drive_key: str
    drive_serial: str
    device_node: str
    disc_index: int
    drutil_drive: int
    volume: str
    episode_start: int
    proc: Any
    log_path: Path
    queue_path: Path
    state_path: Path
    started_at: float
    pending_launch_path: Path | None = None
    drive_released: bool = False
    drive_released_at: float | None = None
    drive_ejected: bool = False
    tray_ready_confirmed: bool = False
    release_logged: bool = False


@dataclass
class CompletedRip:
    """Summary of one reaped `ripqueue` child process."""

    drive_key: str
    device_node: str
    volume: str
    episode_start: int
    returncode: int
    log_path: Path
    elapsed_s: float
    was_finalizing: bool = False


@dataclass
class WatchSummary:
    """Aggregate result returned when `BoxSetWatcher.run()` exits."""

    launched: int = 0
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    stopped_by_signal: bool = False
    active_at_exit: list[ActiveRip] = field(default_factory=list)
    finalizing_at_exit: list[ActiveRip] = field(default_factory=list)
    results: list[CompletedRip] = field(default_factory=list)


class BoxSetWatcher:
    """Coordinates zero-babysit multi-drive TV box-set ripping."""

    def __init__(
        self,
        config: WatchConfig,
        *,
        list_drives_fn: Callable[[], list[dict[str, Any]]] | None = None,
        spawn_fn: Callable[..., Any] | None = None,
        compute_next_ep_fn: Callable[..., dict[str, Any]] | None = None,
        discover_procs_fn: Callable[[], list[dict[str, Any]]] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], datetime] = datetime.now,
        notify_fn: Callable[[str], Any] | None = None,
        out: TextIO | None = None,
    ) -> None:
        self.config = config
        self._spawn_fn = spawn_fn or subprocess.Popen
        self._compute_next_ep_fn = compute_next_ep_fn or compute_next_tv_episode
        self._discover_procs_fn = (
            discover_procs_fn
            if discover_procs_fn is not None
            else burndvd_metadata._discover_active_ripqueue_procs
        )
        self._sleep_fn = sleep_fn
        self._clock_fn = clock_fn
        self._now_fn = now_fn
        self._notify_fn = notify_fn if notify_fn is not None else notify_slack
        self._out = out if out is not None else sys.stdout

        self.active_procs: dict[str, ActiveRip] = {}
        self.finalizing_procs: list[ActiveRip] = []
        self.handled_discs: dict[str, str] = {}
        self.rejected_discs: dict[str, str] = {}
        self.results: list[CompletedRip] = []
        self._drive_topology: dict[str, dict[str, Any]] = {
            "drutil_to_serial": {},
            "serial_to_slot": {},
            "drutil_to_slot": {},
        }
        self._launched_count: int = 0
        self._first_episode_override: int | None = config.episode_start
        self._stop_requested: bool = False
        self._launch_counter: int = 0
        self.watcher_id: str = uuid.uuid4().hex[:8]
        self._list_drives_fn = list_drives_fn or self._default_list_drives

    def _default_list_drives(self) -> list[dict[str, Any]]:
        """Enumerate drives while excluding optical slots currently occupied by `active_procs`."""
        busy_slots = {int(r.disc_index) for r in self.active_procs.values()}
        busy_devices = {
            r.device_node for r in self.active_procs.values() if r.device_node
        }
        busy_serials = {
            r.drive_serial for r in self.active_procs.values() if r.drive_serial
        }
        busy_drutil = {
            int(r.drutil_drive)
            for r in self.active_procs.values()
            if int(r.drutil_drive) >= 1
        }
        return list_makemkv_drives(
            self.config.makemkvcon,
            busy_slots=busy_slots,
            busy_devices=busy_devices,
            busy_serials=busy_serials,
            busy_drutil_drives=busy_drutil,
            known_topology=self._drive_topology,
        )

    def _record_topology(self, drives: Iterable[Mapping[str, Any]]) -> None:
        """Update in-memory drive topology from newly observed drive rows."""
        for row in drives or ():
            serial = str(row.get("serial") or "").strip()
            raw_idx = row.get("index")
            idx = (
                int(raw_idx)
                if isinstance(raw_idx, int) and not isinstance(raw_idx, bool) and raw_idx >= 0
                else None
            )
            raw_drutil = row.get("drutil_drive")
            drutil_idx = (
                int(raw_drutil)
                if isinstance(raw_drutil, int)
                and not isinstance(raw_drutil, bool)
                and raw_drutil >= 1
                else None
            )
            if serial and idx is not None:
                self._drive_topology["serial_to_slot"][serial.upper()] = idx
            if drutil_idx is not None and serial:
                self._drive_topology["drutil_to_serial"][str(drutil_idx)] = serial
            if drutil_idx is not None and idx is not None:
                self._drive_topology["drutil_to_slot"][str(drutil_idx)] = idx

    def _season_key(self) -> str:
        return _season_coord_key(
            self.config.show,
            self.config.season,
            self.config.target_root,
            self.config.media_smb_root,
            self.config.media_nfs_root,
        )

    def _pending_dir(self) -> Path:
        return Path(self.config.work_dir) / ".pending"

    def _register_live_rip(self, rip: ActiveRip) -> None:
        skey = self._season_key()
        with _WATCHER_REGISTRY_LOCK:
            bucket = [
                r
                for r in _LIVE_WATCHER_RIPS.get(skey, [])
                if r.proc.poll() is None
            ]
            bucket.append(rip)
            _LIVE_WATCHER_RIPS[skey] = bucket

    def _unregister_live_rip(self, rip: ActiveRip) -> None:
        skey = self._season_key()
        with _WATCHER_REGISTRY_LOCK:
            bucket = [
                r
                for r in _LIVE_WATCHER_RIPS.get(skey, [])
                if r is not rip and r.proc.poll() is None
            ]
            if bucket:
                _LIVE_WATCHER_RIPS[skey] = bucket
            else:
                _LIVE_WATCHER_RIPS.pop(skey, None)
        if rip.pending_launch_path is not None:
            try:
                rip.pending_launch_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_pending_launch_record(
        self,
        *,
        pid: int,
        drive_key: str,
        queue_path: Path,
        state_path: Path,
        log_path: Path,
        attempt_token: str,
    ) -> Path | None:
        pdir = self._pending_dir()
        try:
            pdir.mkdir(parents=True, exist_ok=True)
            dest = pdir / f"launch-{os.getpid()}-{int(pid)}-{attempt_token}.json"
            tmp = pdir / f".launch-{os.getpid()}-{int(pid)}-{attempt_token}.tmp"
            payload = {
                "pid": int(pid),
                "owner_pid": os.getpid(),
                "watcher_id": self.watcher_id,
                "season_key": self._season_key(),
                "drive_key": drive_key,
                "state_file": str(state_path),
                "queue_file": str(queue_path),
                "log_file": str(log_path),
                "created_at": time.time(),
            }
            tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, dest)
            return dest
        except OSError:
            return None

    def _emit(self, message: str) -> None:
        ts = self._now_fn().strftime("%H:%M:%S")
        print(f"[{ts}] {message}", file=self._out, flush=True)

    def _safe_notify(self, message: str) -> None:
        try:
            self._notify_fn(message)
        except Exception:
            pass

    def request_stop(self) -> None:
        """Signal the watcher loop to stop accepting new discs cleanly."""
        self._stop_requested = True

    def release_drives(self) -> list[ActiveRip]:
        """Move optical-released rips out of `active_procs` into `finalizing_procs`.

        Preserves child process tracking, episode reservations (`_LIVE_WATCHER_RIPS`
        and pending launch records), and `handled_discs` anti-rerip guard while freeing
        the optical drive slot immediately for the next disc.
        """
        newly_released: list[ActiveRip] = []
        for drive_key, rip in list(self.active_procs.items()):
            if rip.proc.poll() is not None:
                continue
            marker = read_drive_released_marker(rip.state_path)
            if marker is None:
                continue
            self.active_procs.pop(drive_key)
            rip.drive_released = True
            rip.drive_released_at = self._clock_fn()
            rip.drive_ejected = is_eject_confirmed(marker)
            self.finalizing_procs.append(rip)
            newly_released.append(rip)
        return newly_released

    def _confirm_tray_ready(self, drives: list[dict[str, Any]]) -> None:
        """Emit confirmed tray-ready notifications vs non-ejected release status.

        Only fires a tray-ready notification when `<state>.drive_released` confirms
        `ejected: true` AND the latest drive snapshot does not still show the exact
        same disc sitting loaded in that drive.
        """
        drives_by_key = {drive_identity_key(d): d for d in (drives or ())}
        for rip in self.finalizing_procs:
            if rip.tray_ready_confirmed:
                continue
            row = drives_by_key.get(rip.drive_key)
            same_disc_still_loaded = False
            if row is not None:
                row_node = str(row.get("device_node") or "").strip()
                row_vol = str(row.get("volume") or "").strip()
                if (
                    row.get("state") == 2
                    and row_node
                    and (not row_vol or row_vol == rip.volume)
                ):
                    same_disc_still_loaded = True

            if (
                rip.drive_ejected
                and not same_disc_still_loaded
                and rip.drive_key not in self.active_procs
            ):
                rip.tray_ready_confirmed = True
                rip.release_logged = True
                self._emit(
                    f"Drive {rip.drive_key} TRAY READY "
                    f"(released {rip.volume}; verifying/copying in background)"
                )
                self._safe_notify(
                    f":cd: *Drive tray ready* — `{self.config.show}` "
                    f"S{self.config.season:02d} `{rip.volume}` released drive "
                    f"`{rip.drive_key}` (ejected; insert next disc while verify/NAS "
                    f"finishes in background)."
                )
            elif not rip.release_logged:
                rip.release_logged = True
                if not rip.drive_ejected:
                    self._emit(
                        f"Drive {rip.drive_key} RELEASED {rip.volume} "
                        f"(eject not confirmed; remove disc manually — "
                        f"verifying/copying in background)"
                    )
                else:
                    self._emit(
                        f"Drive {rip.drive_key} RELEASED {rip.volume} "
                        f"(disc still detected in tray; tray not yet ready — "
                        f"verifying/copying in background)"
                    )

    def reap_finished(self) -> list[CompletedRip]:
        """Reap any finished optical-active or background-finalizing child processes."""
        reaped: list[CompletedRip] = []
        finished_active: list[tuple[str, int]] = []
        for drive_key, rip in list(self.active_procs.items()):
            rc = rip.proc.poll()
            if rc is not None:
                finished_active.append((drive_key, int(rc)))

        for drive_key, rc in finished_active:
            rip = self.active_procs.pop(drive_key)
            self._unregister_live_rip(rip)
            elapsed = max(0.0, self._clock_fn() - rip.started_at)
            # Keep handled_discs pinned to this volume so an un-ejected disc is not re-ripped.
            self.handled_discs[drive_key] = rip.volume
            completed = CompletedRip(
                drive_key=drive_key,
                device_node=rip.device_node,
                volume=rip.volume,
                episode_start=rip.episode_start,
                returncode=rc,
                log_path=rip.log_path,
                elapsed_s=elapsed,
                was_finalizing=False,
            )
            self.results.append(completed)
            reaped.append(completed)
            if rc == 0:
                self._emit(
                    f"Drive {drive_key} FINISHED {rip.volume} "
                    f"(exit 0, {fmt_dur(elapsed)})"
                )
                self._safe_notify(
                    f":white_check_mark: *Watch rip finished* — `{self.config.show}` "
                    f"S{self.config.season:02d} `{rip.volume}` (E{rip.episode_start}+, "
                    f"drive `{drive_key}`, {fmt_dur(elapsed)})."
                )
            else:
                self._emit(
                    f"Drive {drive_key} FAILED {rip.volume} "
                    f"(exit {rc}, {fmt_dur(elapsed)}, log: {rip.log_path}) — "
                    f"will not re-rip until disc is removed"
                )
                self._safe_notify(
                    f":x: *Watch rip FAILED* — `{self.config.show}` "
                    f"S{self.config.season:02d} `{rip.volume}` (exit {rc}, "
                    f"drive `{drive_key}`, log: `{rip.log_path.name}`)."
                )

        still_finalizing: list[ActiveRip] = []
        finished_finalizing: list[tuple[ActiveRip, int]] = []
        for rip in self.finalizing_procs:
            rc = rip.proc.poll()
            if rc is not None:
                finished_finalizing.append((rip, int(rc)))
            else:
                still_finalizing.append(rip)
        self.finalizing_procs = still_finalizing

        for rip, rc in finished_finalizing:
            drive_key = rip.drive_key
            self._unregister_live_rip(rip)
            elapsed = max(0.0, self._clock_fn() - rip.started_at)
            # Do NOT overwrite handled_discs[drive_key]: the optical drive may already
            # have accepted a newer disc since this rip released it.
            completed = CompletedRip(
                drive_key=drive_key,
                device_node=rip.device_node,
                volume=rip.volume,
                episode_start=rip.episode_start,
                returncode=rc,
                log_path=rip.log_path,
                elapsed_s=elapsed,
                was_finalizing=True,
            )
            self.results.append(completed)
            reaped.append(completed)
            if rc == 0:
                self._emit(
                    f"Drive {drive_key} PUBLISHED {rip.volume} "
                    f"(finalization complete, exit 0, {fmt_dur(elapsed)})"
                )
                self._safe_notify(
                    f":white_check_mark: *Watch publication complete* — `{self.config.show}` "
                    f"S{self.config.season:02d} `{rip.volume}` (E{rip.episode_start}+, "
                    f"drive `{drive_key}`, verified & published to NAS, {fmt_dur(elapsed)})."
                )
            else:
                self._emit(
                    f"Drive {drive_key} FINALIZE FAILED {rip.volume} "
                    f"(exit {rc}, {fmt_dur(elapsed)}, log: {rip.log_path}) — "
                    f"staged files preserved; check log before retrying"
                )
                self._safe_notify(
                    f":x: *Watch finalization FAILED* — `{self.config.show}` "
                    f"S{self.config.season:02d} `{rip.volume}` (exit {rc}, "
                    f"drive `{drive_key}`, log: `{rip.log_path.name}`)."
                )
        return reaped

    def _update_handled_guard(self, drives: list[dict[str, Any]]) -> None:
        """Clear `handled_discs` and `rejected_discs` once a drive reports empty/open tray or a new volume."""
        for d in drives:
            drive_key = drive_identity_key(d)
            if drive_key in self.active_procs:
                continue
            state = d.get("state")
            node = str(d.get("device_node") or "").strip()
            if state != 2 or not node:
                self.handled_discs.pop(drive_key, None)
                self.rejected_discs.pop(drive_key, None)
                continue
            raw_vol = str(d.get("volume") or "").strip()
            if (
                raw_vol
                and drive_key in self.handled_discs
                and self.handled_discs[drive_key] != raw_vol
            ):
                self.handled_discs.pop(drive_key, None)
            if (
                drive_key in self.rejected_discs
                and self.rejected_discs[drive_key] != raw_vol
            ):
                self.rejected_discs.pop(drive_key, None)

    def _eligible_drives(self, drives: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return idle drives holding a new, unhandled disc sorted deterministically."""
        candidates: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for d in sorted(
            drives, key=lambda row: (int(row.get("index", 0)), drive_identity_key(row))
        ):
            if d.get("state") != 2:
                continue
            node = str(d.get("device_node") or "").strip()
            if not node:
                continue
            drive_key = drive_identity_key(d)
            if drive_key in seen_keys or drive_key in self.active_procs:
                continue
            raw_vol = str(d.get("volume") or "").strip()
            if raw_vol and self.handled_discs.get(drive_key) == raw_vol:
                continue
            if drive_key in self.rejected_discs and self.rejected_discs[drive_key] == raw_vol:
                continue
            seen_keys.add(drive_key)
            candidates.append(d)
        return candidates

    def _known_active_processes(self) -> list[dict[str, Any]]:
        """Build active-process list combining optical-active, finalizing, sibling, and external procs."""
        procs: list[dict[str, Any]] = []
        seen_pids: set[int] = set()
        for rip in (*self.active_procs.values(), *self.finalizing_procs):
            pid = getattr(rip.proc, "pid", None)
            if isinstance(pid, int) and pid > 0 and rip.proc.poll() is None:
                seen_pids.add(pid)
                procs.append(
                    {
                        "pid": pid,
                        "state_file": str(rip.state_path),
                        "queue_file": str(rip.queue_path),
                        "log_file": str(rip.log_path),
                    }
                )
        skey = self._season_key()
        with _WATCHER_REGISTRY_LOCK:
            sibling_rips = [
                r
                for r in _LIVE_WATCHER_RIPS.get(skey, [])
                if r.proc.poll() is None
            ]
            if sibling_rips:
                _LIVE_WATCHER_RIPS[skey] = sibling_rips
            else:
                _LIVE_WATCHER_RIPS.pop(skey, None)
        for rip in sibling_rips:
            pid = getattr(rip.proc, "pid", None)
            if isinstance(pid, int) and pid > 0 and pid not in seen_pids:
                seen_pids.add(pid)
                procs.append(
                    {
                        "pid": pid,
                        "state_file": str(rip.state_path),
                        "queue_file": str(rip.queue_path),
                        "log_file": str(rip.log_path),
                    }
                )
        pdir = self._pending_dir()
        if pdir.is_dir():
            for path in sorted(pdir.glob("launch-*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict) or data.get("season_key") != skey:
                    continue
                try:
                    pid = int(data.get("pid", 0))
                    owner_pid = int(data.get("owner_pid", 0))
                except (TypeError, ValueError):
                    continue
                if pid <= 0 or pid in seen_pids:
                    continue
                if owner_pid != os.getpid() and not burndvd_metadata._pid_alive(pid):
                    if not burndvd_metadata._pid_alive(owner_pid):
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    continue
                if owner_pid == os.getpid():
                    # Same-process watcher launches are already tracked accurately
                    # via `_LIVE_WATCHER_RIPS` (including fake PIDs in unit tests).
                    continue
                seen_pids.add(pid)
                procs.append(
                    {
                        "pid": pid,
                        "state_file": str(data.get("state_file") or ""),
                        "queue_file": str(data.get("queue_file") or ""),
                        "log_file": str(data.get("log_file") or ""),
                    }
                )
        try:
            external = self._discover_procs_fn() or []
        except Exception:
            external = []
        for entry in external:
            try:
                pid = int(entry.get("pid", 0))
            except (TypeError, ValueError):
                continue
            if pid > 0 and pid not in seen_pids:
                seen_pids.add(pid)
                procs.append(dict(entry))
        return procs

    def _create_unique_attempt_paths(
        self, *, slug: str, drive_slug: str, ts: str
    ) -> tuple[Path, Path, Path, Path, int, str]:
        """Allocate collision-proof attempt directory and exclusive log file descriptor."""
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        pid = os.getpid()
        for _ in range(32):
            attempt_token = uuid.uuid4().hex[:8]
            unique_tag = (
                f"{slug}-{drive_slug}-{ts}-{pid}-"
                f"{self.watcher_id}-{self._launch_counter}-{attempt_token}"
            )
            attempt_dir = self.config.work_dir / f"attempt-{unique_tag}"
            try:
                attempt_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            log_path = self.config.log_dir / f"burndvd-watch-{unique_tag}.log"
            try:
                log_fd = os.open(
                    str(log_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                )
            except FileExistsError:
                try:
                    attempt_dir.rmdir()
                except OSError:
                    pass
                continue
            queue_path = attempt_dir / "queue.csv"
            state_path = attempt_dir / "state.json"
            return attempt_dir, queue_path, state_path, log_path, log_fd, attempt_token
        raise RuntimeError("failed to allocate unique watch attempt directory/log")

    def _launch_single_drive(self, drive_row: Mapping[str, Any]) -> ActiveRip | None:
        """Serialized, intent-bound launch gate for a single drive (zero slot or artifact race)."""
        # Reap any child that may have exited just before we compute the next episode.
        self.reap_finished()

        drive_key = drive_identity_key(drive_row)
        drive_serial = str(drive_row.get("serial") or "").strip()
        device_node = str(drive_row.get("device_node") or "").strip()
        disc_index = int(drive_row.get("index", 0))
        raw_drutil = drive_row.get("drutil_drive")
        drutil_drive = (
            int(raw_drutil)
            if isinstance(raw_drutil, int)
            and not isinstance(raw_drutil, bool)
            and raw_drutil >= 1
            else disc_index + 1
        )
        raw_vol = str(drive_row.get("volume") or "").strip()

        matched_intent, intent_reason = volume_matches_show_intent(
            raw_vol,
            self.config.show,
            self.config.season,
            allowed_volumes=self.config.allowed_volumes,
        )
        if not matched_intent:
            self.rejected_discs[drive_key] = raw_vol
            vol_disp = raw_vol or "(unlabeled)"
            self._emit(
                f"Drive {drive_key} ({device_node}) REJECTED {vol_disp} — "
                f"{intent_reason}; refusing unattended launch"
            )
            return None

        volume = raw_vol
        skey = self._season_key()
        with _watch_season_lock(self.config.work_dir, skey):
            # Guard against two independent watchers launching the same physical drive
            # while an optical rip still owns it (released/finalizing rips do not occupy the drive).
            with _WATCHER_REGISTRY_LOCK:
                already_active_drive = any(
                    r.drive_key == drive_key
                    and r.proc.poll() is None
                    and not r.drive_released
                    and not is_state_drive_released(r.state_path)
                    for r in _LIVE_WATCHER_RIPS.get(skey, [])
                )
            if already_active_drive:
                return None

            contract = episode_contract(self.config.show, self.config.season, volume)
            expected_episodes = ""
            expected_disc_episodes = ""
            expected_title_ids = ""
            expected_physical_disc = ""

            if self._first_episode_override is not None or contract is not None:
                if self._first_episode_override is not None:
                    ep_start = int(self._first_episode_override)
                    plan_count: int | None = None
                else:
                    assert contract is not None
                    ep_start = int(contract["episode_start"])
                    plan_count = int(contract["expected_disc_episodes"])
                    expected_episodes = str(contract["expected_episodes"])
                    expected_disc_episodes = str(contract["expected_disc_episodes"])
                    expected_title_ids = ",".join(
                        str(x) for x in contract["expected_title_ids"]
                    )
                    expected_physical_disc = str(contract["disc"])

                known_procs = self._known_active_processes()
                ep_info = self._compute_next_ep_fn(
                    title=self.config.show,
                    season=int(self.config.season),
                    target_root=self.config.target_root,
                    season_dir=self.config.season_dir,
                    slots_dir=self.config.slots_dir,
                    media_smb_root=self.config.media_smb_root,
                    media_nfs_root=self.config.media_nfs_root,
                    active_processes=known_procs,
                    wait_scan_s=0.0,
                    poll_interval_s=min(
                        2.0, max(0.05, float(self.config.poll_interval))
                    ),
                )
                existing_eps = ep_info.get("existing_episodes")
                if existing_eps is None:
                    existing_eps = scan_season_existing_episodes(
                        self.config.season_dir, int(self.config.season)
                    )
                conflict, blocking_pending = find_episode_plan_conflict(
                    ep_start,
                    episode_count=plan_count,
                    existing_episodes=existing_eps,
                    reservations=ep_info.get("reservations") or (),
                    pending_plans=ep_info.get("pending_plans") or (),
                    check_existing=True,
                )
                if conflict is None and blocking_pending and float(self.config.scan_wait) > 0:
                    ep_info = self._compute_next_ep_fn(
                        title=self.config.show,
                        season=int(self.config.season),
                        target_root=self.config.target_root,
                        season_dir=self.config.season_dir,
                        slots_dir=self.config.slots_dir,
                        media_smb_root=self.config.media_smb_root,
                        media_nfs_root=self.config.media_nfs_root,
                        active_processes=self._known_active_processes(),
                        wait_scan_s=float(self.config.scan_wait),
                        poll_interval_s=min(
                            2.0, max(0.05, float(self.config.poll_interval))
                        ),
                    )
                    existing_eps = ep_info.get("existing_episodes")
                    if existing_eps is None:
                        existing_eps = scan_season_existing_episodes(
                            self.config.season_dir, int(self.config.season)
                        )
                    conflict, blocking_pending = find_episode_plan_conflict(
                        ep_start,
                        episode_count=plan_count,
                        existing_episodes=existing_eps,
                        reservations=ep_info.get("reservations") or (),
                        pending_plans=ep_info.get("pending_plans") or (),
                        check_existing=True,
                    )
                if conflict is not None:
                    self.rejected_discs[drive_key] = volume
                    self._emit(
                        f"Drive {drive_key} ({device_node}) REJECTED {volume} — "
                        f"{conflict}; refusing overlapping launch"
                    )
                    return None
                if blocking_pending:
                    pids_s = ", ".join(str(p) for p in blocking_pending)
                    self._emit(
                        f"Drive {drive_key} ({device_node}) WAITING — concurrent scan "
                        f"pid(s) {pids_s} could overlap E{ep_start}+ and did not publish slot "
                        f"within {int(self.config.scan_wait)}s; deferring {volume}"
                    )
                    return None
                pending_scan_pids: list[int] = []
            else:
                known_procs = self._known_active_processes()
                ep_info = self._compute_next_ep_fn(
                    title=self.config.show,
                    season=int(self.config.season),
                    target_root=self.config.target_root,
                    season_dir=self.config.season_dir,
                    slots_dir=self.config.slots_dir,
                    media_smb_root=self.config.media_smb_root,
                    media_nfs_root=self.config.media_nfs_root,
                    active_processes=known_procs,
                    wait_scan_s=float(self.config.scan_wait),
                    poll_interval_s=min(2.0, max(0.05, float(self.config.poll_interval))),
                )
                pending_scan_pids = list(ep_info.get("pending_scan_pids") or [])
                if pending_scan_pids:
                    pids_s = ", ".join(str(p) for p in pending_scan_pids)
                    self._emit(
                        f"Drive {drive_key} ({device_node}) WAITING — concurrent scan "
                        f"pid(s) {pids_s} did not publish slot within {int(self.config.scan_wait)}s; "
                        f"deferring {volume}"
                    )
                    return None
                ep_start = int(ep_info.get("next_episode") or 1)

            self._launch_counter += 1
            ts = self._now_fn().strftime("%Y%m%dT%H%M%S")
            slug = slugify_volume(volume)
            drive_slug = slugify_volume(drive_key)
            (
                _attempt_dir,
                queue_path,
                state_path,
                log_path,
                log_fd,
                attempt_token,
            ) = self._create_unique_attempt_paths(
                slug=slug, drive_slug=drive_slug, ts=ts
            )

            write_queue(
                queue_path,
                title=self.config.show,
                item_type="tv-season",
                season=str(self.config.season),
                episode_start=str(ep_start),
                expected_episodes=expected_episodes,
                expected_disc_episodes=expected_disc_episodes,
                expected_title_ids=expected_title_ids,
                expected_physical_disc=expected_physical_disc,
                target_root=self.config.target_root,
                media_format=self.config.media_format,
                notes=f"watch:{drive_key}:{volume}",
            )

            cmd = build_ripqueue_command(
                queue_path=queue_path,
                state_path=state_path,
                disc_index=disc_index,
                makemkvcon=self.config.makemkvcon,
                no_extras=self.config.no_extras,
                ripqueue_py=self.config.ripqueue_py,
                expected_volume=volume,
                strict_disc_match=True,
                allowed_volumes=self.config.allowed_volumes,
                allowed_fingerprints=self.config.allowed_fingerprints,
            )
            child_env = build_ripqueue_env(
                device_node=device_node,
                drive_serial=drive_serial,
                drutil_drive=drutil_drive,
                no_extras=self.config.no_extras,
                slots_dir=self.config.slots_dir,
            )

            log_fh = os.fdopen(log_fd, "w", encoding="utf-8")
            spawn_kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": log_fh,
                "stderr": subprocess.STDOUT,
                "env": child_env,
            }
            if _spawn_accepts_start_new_session(self._spawn_fn):
                spawn_kwargs["start_new_session"] = True
            try:
                proc = self._spawn_fn(cmd, **spawn_kwargs)
            except Exception:
                log_fh.close()
                raise
            # Child inherited the fd; close parent copy so watcher shutdown never leaks FDs.
            log_fh.close()

            # Consume one-shot --episode-start override only after spawn succeeded.
            if self._first_episode_override is not None:
                self._first_episode_override = None

            pid_val = getattr(proc, "pid", None)
            pending_launch_path = (
                self._write_pending_launch_record(
                    pid=int(pid_val),
                    drive_key=drive_key,
                    queue_path=queue_path,
                    state_path=state_path,
                    log_path=log_path,
                    attempt_token=attempt_token,
                )
                if isinstance(pid_val, int) and pid_val > 0
                else None
            )

            rip = ActiveRip(
                drive_key=drive_key,
                drive_serial=drive_serial,
                device_node=device_node,
                disc_index=disc_index,
                drutil_drive=drutil_drive,
                volume=volume,
                episode_start=ep_start,
                proc=proc,
                log_path=log_path,
                queue_path=queue_path,
                state_path=state_path,
                started_at=self._clock_fn(),
                pending_launch_path=pending_launch_path,
            )
            self.active_procs[drive_key] = rip
            self.handled_discs[drive_key] = volume
            self._launched_count += 1
            self._register_live_rip(rip)

        pid_s = getattr(proc, "pid", "?")
        self._emit(
            f"Drive {drive_key} ({device_node}) STARTED {volume} -> "
            f"E{ep_start}+ (pid {pid_s}, log: {log_path})"
        )
        return rip

    def poll_once(self) -> list[ActiveRip]:
        """Run one poll-reap-release-launch tick; return newly launched rips."""
        self.reap_finished()
        self.release_drives()
        drives = self._list_drives_fn()
        if drives:
            self._record_topology(drives)
        self.release_drives()
        self._confirm_tray_ready(drives)
        if drives:
            self._update_handled_guard(drives)

        launched_this_tick: list[ActiveRip] = []
        if self._stop_requested:
            return launched_this_tick

        for drive_row in self._eligible_drives(drives):
            if self._stop_requested:
                break
            if (
                self.config.max_discs > 0
                and self._launched_count >= self.config.max_discs
            ):
                break
            rip = self._launch_single_drive(drive_row)
            if rip is not None:
                launched_this_tick.append(rip)
        return launched_this_tick

    def _reached_max_and_idle(self) -> bool:
        return (
            self.config.max_discs > 0
            and self._launched_count >= self.config.max_discs
            and not self.active_procs
            and not self.finalizing_procs
        )

    def run(self) -> WatchSummary:
        """Main watch loop until `--max-discs` completes or Ctrl-C / SIGTERM."""
        extras_label = "no-extras" if self.config.no_extras else "with-extras"
        max_label = (
            f", max-discs={self.config.max_discs}"
            if self.config.max_discs > 0
            else ""
        )
        ep_label = (
            f", initial-ep=E{self.config.episode_start}"
            if self.config.episode_start is not None
            else ""
        )
        self._emit(
            f"Watch mode started: {self.config.show} S{self.config.season:02d} "
            f"({self.config.media_format}, {extras_label}{ep_label}{max_label}) -> "
            f"{self.config.target_root}"
        )

        prev_sigterm = None
        installed_sigterm = False
        if threading.current_thread() is threading.main_thread():
            def _handle_sigterm(_signum, _frame):
                self._stop_requested = True

            prev_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _handle_sigterm)
            installed_sigterm = True

        stopped_by_signal = False
        try:
            while not self._stop_requested:
                self.poll_once()
                if self._reached_max_and_idle():
                    break
                if self._stop_requested:
                    break
                self._sleep_fn(float(self.config.poll_interval))
        except KeyboardInterrupt:
            stopped_by_signal = True
            self._stop_requested = True
        finally:
            if installed_sigterm:
                signal.signal(signal.SIGTERM, prev_sigterm)

        if self._stop_requested and not self._reached_max_and_idle():
            stopped_by_signal = True
            # Final non-blocking reap + release check before reporting active PIDs.
            self.reap_finished()
            self.release_drives()
            all_live = list(self.active_procs.values()) + list(self.finalizing_procs)
            if all_live:
                self._emit(
                    f"Stopping watch mode (no new discs will start). "
                    f"{len(all_live)} active rip(s) left running safely "
                    f"({len(self.active_procs)} optical, {len(self.finalizing_procs)} finalizing):"
                )
                for rip in self.active_procs.values():
                    pid_s = getattr(rip.proc, "pid", "?")
                    self._emit(
                        f"  pid {pid_s}  [optical]     Drive {rip.drive_key} ({rip.device_node})  "
                        f"{rip.volume} -> E{rip.episode_start}+  log: {rip.log_path}"
                    )
                for rip in self.finalizing_procs:
                    pid_s = getattr(rip.proc, "pid", "?")
                    self._emit(
                        f"  pid {pid_s}  [finalizing]  Drive {rip.drive_key} ({rip.device_node})  "
                        f"{rip.volume} -> E{rip.episode_start}+  log: {rip.log_path}"
                    )
            else:
                self._emit("Watch mode stopped cleanly (no active rips).")
        else:
            self._emit(
                f"Watch mode complete: {len(self.results)} disc(s) finished "
                f"({sum(1 for r in self.results if r.returncode == 0)} ok, "
                f"{sum(1 for r in self.results if r.returncode != 0)} failed)."
            )

        succeeded = sum(1 for r in self.results if r.returncode == 0)
        failed = sum(1 for r in self.results if r.returncode != 0)
        return WatchSummary(
            launched=self._launched_count,
            completed=len(self.results),
            succeeded=succeeded,
            failed=failed,
            stopped_by_signal=stopped_by_signal,
            active_at_exit=list(self.active_procs.values()) + list(self.finalizing_procs),
            finalizing_at_exit=list(self.finalizing_procs),
            results=list(self.results),
        )
