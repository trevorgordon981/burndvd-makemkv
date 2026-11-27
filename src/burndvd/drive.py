"""Optical drive enumeration, slim-tray safety guard, unmount/eject/respin, and key expiry."""

from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from burndvd.config import (
    C,
    DEFAULT_MAKEMKVCON,
    DISKUTIL_BIN,
    DRUTIL_BIN,
    MOUNT_BIN,
    PROBE_COOLDOWN_S,
)

DRUTIL_DRIVE_ENV = "BURNDVD_DRUTIL_DRIVE"
DRIVE_DEVICE_ENV = "BURNDVD_DRIVE_DEVICE"
DRIVE_SERIAL_ENV = "BURNDVD_DRIVE_SERIAL"

_SLIM_TRAY_DRIVE_RE = re.compile(
    r"\b(?:BU\d+N|BP\d+NB\d+|WP\d+NB\d+|SLIM)\b", re.IGNORECASE
)
_DEVICE_NODE_RE = re.compile(r"^(?:/dev/)?r?(disk\d+)$")
_DRUTIL_LIST_LINE_RE = re.compile(r"^\s*(\d+)\s+\S")


def append_log(args, line: str) -> None:
    """Append a timestamped audit line next to `--state`."""
    state_path = getattr(args, "state", None)
    if not state_path:
        return
    log = Path(state_path).with_suffix(".log")
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now().isoformat(timespec='seconds')}  {line}\n")


def _parse_positive_int(value: int | str | None) -> int | None:
    """Return a positive integer (>= 1) or None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"\d+", s):
            n = int(s)
            return n if n >= 1 else None
    return None


def _normalize_device_node(device_node: str | None) -> str | None:
    """Normalize `diskN`, `/dev/diskN`, or `/dev/rdiskN` to `/dev/diskN`."""
    if not isinstance(device_node, str):
        return None
    m = _DEVICE_NODE_RE.match(device_node.strip())
    return f"/dev/{m.group(1)}" if m else None


def _extract_device_spec(target) -> str:
    """Return the MakeMKV device string from a string or args-like namespace."""
    if target is None:
        return ""
    if isinstance(target, str):
        return target
    return getattr(target, "device", "") or ""


def _disc_selector_index(device: str) -> int | None:
    """Extract the 0-based `disc:N` MakeMKV selector index, or None."""
    m = re.match(r"disc:(\d+)$", device or "")
    return int(m.group(1)) if m else None


def _explicit_drive_target(
    target=None,
    *,
    drutil_drive: int | None = None,
    device_node: str | None = None,
) -> tuple[int | None, str | None]:
    """Return explicit `(1-based drutil drive, /dev/diskN)` from kwargs, args, or env."""
    device_spec = _extract_device_spec(target)
    disc_idx = _disc_selector_index(device_spec)
    is_non_disc = bool(device_spec) and disc_idx is None

    if drutil_drive is None and target is not None and not isinstance(target, str):
        drutil_drive = getattr(target, "drutil_drive", None)
    if drutil_drive is None and not is_non_disc:
        drutil_drive = os.environ.get(DRUTIL_DRIVE_ENV)

    if device_node is None and target is not None and not isinstance(target, str):
        device_node = getattr(target, "drive_device", None) or getattr(
            target, "device_node", None
        )
    if device_node is None and not is_non_disc:
        device_node = os.environ.get(DRIVE_DEVICE_ENV)

    return _parse_positive_int(drutil_drive), _normalize_device_node(device_node)


def _explicit_drive_serial(
    target=None,
    *,
    drive_serial: str | None = None,
) -> str | None:
    """Return explicit hardware serial from kwargs, args, or `BURNDVD_DRIVE_SERIAL`."""
    device_spec = _extract_device_spec(target)
    disc_idx = _disc_selector_index(device_spec)
    is_non_disc = bool(device_spec) and disc_idx is None

    if drive_serial is None and target is not None and not isinstance(target, str):
        drive_serial = getattr(target, "drive_serial", None)
    if drive_serial is None and not is_non_disc:
        drive_serial = os.environ.get(DRIVE_SERIAL_ENV)
    if isinstance(drive_serial, str):
        cleaned = drive_serial.strip()
        return cleaned or None
    return None


def parse_makemkv_drives(output: str) -> list[dict]:
    """Parse `DRV:` lines from `makemkvcon -r info list` output."""
    drives: list[dict] = []
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("DRV:"):
            continue
        tag, fields = _parse_msg_fields(line)
        if tag != "DRV" or not fields or len(fields) < 7:
            continue
        try:
            idx = int(fields[0])
            state = int(fields[1])
            flags = int(fields[2])
            disc_flags = int(fields[3])
        except ValueError:
            continue
        drive_desc = fields[4].strip()
        volume = fields[5].strip()
        raw_dev = fields[6].strip()
        device_node = _normalize_device_node(raw_dev)
        tokens = drive_desc.split()
        last_tok = tokens[-1] if tokens else ""
        serial = last_tok if (len(last_tok) >= 6 and last_tok.isalnum()) else ""
        drives.append(
            {
                "index": idx,
                "state": state,
                "flags": flags,
                "disc_flags": disc_flags,
                "drive_desc": drive_desc,
                "serial": serial,
                "volume": volume,
                "raw_device": raw_dev,
                "device_node": device_node,
                "has_media": state == 2,
            }
        )
    return drives


def resolve_makemkv_device(args) -> str:
    """Resolve a `disc:N` selector to the current MakeMKV index for a pinned physical drive.

    When `BURNDVD_DRIVE_DEVICE` / `args.drive_device` or `BURNDVD_DRIVE_SERIAL` /
    `args.drive_serial` is explicitly set and `args.device` is a `disc:N` selector,
    queries `[makemkvcon, "-r", "info", "list"]` and matches by normalized `/dev/diskN`
    (preferred) or by hardware serial with media loaded. Falls back gracefully to
    `args.device` if no explicit pin is set or if enumeration fails.
    """
    device = _extract_device_spec(args)
    if _disc_selector_index(device) is None:
        return device

    _, exp_node = _explicit_drive_target(args)
    exp_serial = _explicit_drive_serial(args)
    if exp_node is None and exp_serial is None:
        return device

    makemkvcon = getattr(args, "makemkvcon", None) or DEFAULT_MAKEMKVCON
    try:
        r = subprocess.run(
            [makemkvcon, "-r", "info", "list"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return device

    if r.returncode != 0 and not (r.stdout or "").strip():
        return device
    drives = parse_makemkv_drives(r.stdout or "")
    if not drives:
        return device

    if exp_node is not None:
        node_matches = [d for d in drives if d["device_node"] == exp_node]
        if node_matches:
            if exp_serial is not None:
                exact = [
                    d
                    for d in node_matches
                    if d["serial"]
                    and d["serial"].casefold() == exp_serial.casefold()
                ]
                if exact:
                    return f"disc:{exact[0]['index']}"
            loaded = [d for d in node_matches if d["has_media"]]
            chosen = loaded[0] if loaded else node_matches[0]
            return f"disc:{chosen['index']}"

    if exp_serial is not None:
        serial_matches = [
            d
            for d in drives
            if d["serial"]
            and d["serial"].casefold() == exp_serial.casefold()
            and d["has_media"]
        ]
        if serial_matches:
            return f"disc:{serial_matches[0]['index']}"

    return device


def _drutil_status_output(drutil_drive: int | None = None) -> str | None:
    """Run `drutil status [-drive N]` and return stdout, or None on error."""
    cmd = [DRUTIL_BIN, "status"]
    if drutil_drive is not None:
        cmd += ["-drive", str(drutil_drive)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return r.stdout or ""


def _parse_disk_node_from_status(status_text: str | None) -> str | None:
    """Extract `/dev/diskN` from `drutil status` output."""
    m = re.search(r"Name:\s+(/dev/disk\d+)", status_text or "")
    return m.group(1) if m else None


def _parse_media_type_from_status(status_text: str | None) -> str | None:
    """Extract the `Type:` value from `drutil status` output."""
    m = re.search(r"Type:\s*(.+)", status_text or "")
    return m.group(1).strip() if m else None


def _status_has_media(status_text: str | None) -> bool:
    """Return True if `drutil status` output indicates inserted optical media."""
    media_type = _parse_media_type_from_status(status_text)
    return bool(media_type) and "no media" not in media_type.lower()


def _drutil_list_drives() -> list[int]:
    """Return 1-based drive indices reported by `drutil list`."""
    try:
        r = subprocess.run(
            [DRUTIL_BIN, "list"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    drives: list[int] = []
    for line in (r.stdout or "").splitlines():
        m = _DRUTIL_LIST_LINE_RE.match(line)
        if m:
            idx = int(m.group(1))
            if idx >= 1 and idx not in drives:
                drives.append(idx)
    return drives


def _find_drive_by_device_node(
    device_node: str,
) -> tuple[int | None, str | None, str | None]:
    """Find the 1-based drutil drive currently holding `device_node`."""
    for drv in _drutil_list_drives():
        out = _drutil_status_output(drv)
        if out is None:
            continue
        node = _parse_disk_node_from_status(out)
        if node == device_node:
            return drv, node, out
    return None, None, None


def _resolve_disc_selector_from_drutil(
    disc_index: int,
) -> tuple[int | None, str | None, str | None]:
    """Map a 0-based MakeMKV `disc:N` selector onto loaded physical `drutil` drives.

    Matches the wrapper's disk-number ordering so an empty drive 1 does not
    cause `disc:0` to target drive 1 when drive 2 holds the inserted disc.
    """
    drives = _drutil_list_drives()
    if not drives:
        return None, None, None

    loaded: list[tuple[int, int, str | None, str]] = []
    for drv in drives:
        out = _drutil_status_output(drv)
        if out is None or not _status_has_media(out):
            continue
        node = _parse_disk_node_from_status(out)
        if node:
            disk_num = int(node.rsplit("disk", 1)[1])
        else:
            disk_num = 10**9 + drv
        loaded.append((disk_num, drv, node, out))

    loaded.sort(key=lambda entry: (entry[0], entry[1]))
    if 0 <= disc_index < len(loaded):
        _, drv, node, out = loaded[disc_index]
        return drv, node, out
    return None, None, None


def _resolve_drive_target(
    target=None,
    *,
    drutil_drive: int | None = None,
    device_node: str | None = None,
    probe: bool = True,
) -> tuple[int | None, str | None, str | None]:
    """Resolve `(1-based drutil drive, /dev/diskN, cached drutil status output)`.

    Resolution order:
      1. Explicit verified `drutil_drive` (kwarg, args attr, or `BURNDVD_DRUTIL_DRIVE`),
         cross-checked against `device_node` / `BURNDVD_DRIVE_DEVICE` when both are set.
      2. Explicit `device_node` / `BURNDVD_DRIVE_DEVICE` matched via `drutil list/status`.
      3. For `disc:N`, probe `drutil list/status` to map the Nth inserted disc to its
         actual physical drive index, falling back to `N + 1` when `drutil list` yields
         no drives (e.g., mocked or unavailable enumeration).
    """
    device_spec = _extract_device_spec(target)
    disc_idx = _disc_selector_index(device_spec)
    exp_drive, exp_node = _explicit_drive_target(
        target, drutil_drive=drutil_drive, device_node=device_node
    )

    if device_spec and disc_idx is None and exp_drive is None and exp_node is None:
        return None, None, None

    if exp_drive is not None:
        if exp_node is None or not probe:
            return exp_drive, exp_node, None
        status_out = _drutil_status_output(exp_drive)
        actual_node = _parse_disk_node_from_status(status_out)
        if actual_node == exp_node:
            return exp_drive, actual_node, status_out
        if actual_node is not None and actual_node != exp_node:
            # Explicit drive holds a different disk node than verified; search for exp_node.
            return _find_drive_by_device_node(exp_node)
        if status_out and "no media" in status_out.lower():
            matched = _find_drive_by_device_node(exp_node)
            if matched[0] is not None:
                return matched
            return exp_drive, None, status_out
        return exp_drive, exp_node, status_out

    if exp_node is not None:
        if probe:
            return _find_drive_by_device_node(exp_node)
        return None, exp_node, None

    if disc_idx is not None:
        if probe:
            resolved = _resolve_disc_selector_from_drutil(disc_idx)
            if resolved[0] is not None:
                return resolved
        return disc_idx + 1, None, None

    return None, None, None


def drutil_drive_index(
    device=None,
    *,
    drutil_drive: int | None = None,
    device_node: str | None = None,
    probe: bool = True,
) -> int | None:
    """Resolve the 1-based `drutil -drive N` index for a device or args namespace."""
    resolved, _, _ = _resolve_drive_target(
        device, drutil_drive=drutil_drive, device_node=device_node, probe=probe
    )
    return resolved


def _drive_idx_from_args(args, *, probe: bool = False) -> int | None:
    """Return the 0-based physical optical drive index for `args`, or None.

    Honors explicit `BURNDVD_DRUTIL_DRIVE` / `BURNDVD_DRIVE_DEVICE` (and matching
    attributes on `args`). When neither is set and `probe=False`, falls back directly
    to parsing `disc:N` without spawning `drutil`.
    """
    exp_drive, exp_node = _explicit_drive_target(args)
    should_probe = probe or (exp_node is not None)
    resolved = drutil_drive_index(args, probe=should_probe)
    return (resolved - 1) if resolved is not None else None


def _drive_identity(drive_idx: int) -> str | None:
    """Return `Vendor Product Rev` string for the optical drive at 0-based `drive_idx`.

    Retries transient empty USB `drutil status` reads before concluding the drive
    is absent.
    """
    for attempt in range(6):
        try:
            r = subprocess.run(
                [DRUTIL_BIN, "status", "-drive", str(drive_idx + 1)],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            r = None
        if r is not None:
            for line in (r.stdout or "").splitlines():
                s = line.strip()
                if not s or s.lower().startswith(
                    (
                        "vendor",
                        "type:",
                        "name:",
                        "sessions:",
                        "tracks:",
                        "overwritable:",
                        "space ",
                        "writability:",
                        "book ",
                    )
                ):
                    continue
                return s
        if attempt < 5:
            time.sleep(1.5)
    return None


def _drutil_disk_node(
    drive_idx: int | None = None, *, drutil_drive: int | None = None
) -> str | None:
    """Return `/dev/diskN` for 0-based `drive_idx` or 1-based `drutil_drive`."""
    resolved_drive = (
        _parse_positive_int(drutil_drive)
        if drutil_drive is not None
        else (drive_idx + 1 if drive_idx is not None else None)
    )
    out = _drutil_status_output(resolved_drive)
    return _parse_disk_node_from_status(out)


def _optical_disk_node() -> str | None:
    """Legacy single-drive helper returning `/dev/diskN` of the first optical drive."""
    return _drutil_disk_node(None)


def _optical_mounts() -> list[str]:
    """Return mounted `/dev/diskN` nodes carrying optical filesystems (`udf`/`cd9660`)."""
    try:
        out = subprocess.run(
            [MOUNT_BIN], capture_output=True, text=True, timeout=10
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return []
    return re.findall(r"^(/dev/disk\d+)\b.*\((?:udf|cd9660)\b", out, re.MULTILINE)


def optical_mount_point(disk_node: str | None) -> Path | None:
    """Return the current mount point for `disk_node` (e.g. `/dev/disk5`), or None."""
    if not disk_node:
        return None
    try:
        out = subprocess.run(
            [MOUNT_BIN], capture_output=True, text=True, timeout=10
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(
        rf"^{re.escape(disk_node)}\s+on\s+(.+?)\s+\(", out, re.MULTILINE
    )
    return Path(m.group(1)) if m else None


def mount_optical_readonly(disk_node: str) -> Path | None:
    """Mount `disk_node` read-only via `diskutil mount readOnly` and return its mount path."""
    existing = optical_mount_point(disk_node)
    if existing is not None:
        return existing
    try:
        r = subprocess.run(
            [DISKUTIL_BIN, "mount", "readOnly", disk_node],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    return optical_mount_point(disk_node)


def unmount_blocking_volume(
    rdisk_path: str | None,
    drive_idx: int | None = None,
    *,
    drutil_drive: int | None = None,
    device_node: str | None = None,
) -> bool:
    """Unmount an auto-mounted optical volume so MakeMKV can open raw blocks.

    When `drive_idx`, `drutil_drive`, or `device_node` is given, strictly targets
    that drive so concurrent rips on other optical drives are never disturbed.
    """
    try:
        subprocess.run(["pkill", "-x", "DVD Player"], capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass

    candidates: list[str] = []
    norm_rdisk = _normalize_device_node(rdisk_path)
    if norm_rdisk:
        candidates.append(norm_rdisk)

    norm_node = _normalize_device_node(device_node)
    if norm_node and norm_node not in candidates:
        candidates.append(norm_node)

    resolved_drive = _parse_positive_int(drutil_drive)
    if resolved_drive is not None:
        drutil_node = _drutil_disk_node(drutil_drive=resolved_drive)
        if drutil_node and drutil_node not in candidates:
            candidates.append(drutil_node)
    elif drive_idx is not None:
        drutil_node = _drutil_disk_node(drive_idx)
        if drutil_node and drutil_node not in candidates:
            candidates.append(drutil_node)
    elif not candidates:
        env_drive, env_node = _explicit_drive_target(None)
        if env_node and env_node not in candidates:
            candidates.append(env_node)
        if env_drive is not None:
            drutil_node = _drutil_disk_node(drutil_drive=env_drive)
            if drutil_node and drutil_node not in candidates:
                candidates.append(drutil_node)
        else:
            drutil_node = _optical_disk_node()
            if drutil_node and drutil_node not in candidates:
                candidates.append(drutil_node)
            for stale in _optical_mounts():
                if stale not in candidates:
                    candidates.append(stale)

    try:
        mounts = subprocess.run(
            [MOUNT_BIN], capture_output=True, text=True, timeout=10
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        mounts = ""

    unmounted = False
    for disk in candidates:
        if not re.search(rf"^{re.escape(disk)}\b", mounts, re.MULTILINE):
            continue
        last_err = "timed out"
        for cmd in (
            [DISKUTIL_BIN, "unmount", disk],
            [DISKUTIL_BIN, "unmount", "force", disk],
        ):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired:
                last_err = "timed out"
                continue
            except OSError as exc:
                last_err = str(exc)
                continue
            if r.returncode == 0:
                print(
                    f"  {C.D}auto-unmounted {disk} so makemkvcon can open the disc{C.R}"
                )
                unmounted = True
                break
            last_err = r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}"
        else:
            print(f"  {C.YLW}diskutil unmount {disk} failed: {last_err}{C.R}")
    return unmounted


def drive_status(
    device=None,
    *,
    drutil_drive: int | None = None,
    device_node: str | None = None,
) -> dict[str, object]:
    """Query `drutil status` for the verified physical drive and return parsed details."""
    exp_drive, exp_node = _explicit_drive_target(
        device, drutil_drive=drutil_drive, device_node=device_node
    )
    resolved_drive, resolved_node, cached_out = _resolve_drive_target(
        device, drutil_drive=drutil_drive, device_node=device_node, probe=True
    )
    if resolved_drive is None and (exp_drive is not None or exp_node is not None):
        return {
            "drutil_drive": None,
            "device_node": None,
            "media_present": False,
            "media_type": None,
            "raw": "",
        }
    out = cached_out if cached_out is not None else _drutil_status_output(resolved_drive)
    disk_node = _parse_disk_node_from_status(out) or resolved_node
    return {
        "drutil_drive": resolved_drive,
        "device_node": disk_node,
        "media_present": _status_has_media(out),
        "media_type": _parse_media_type_from_status(out),
        "raw": out or "",
    }


def disc_present(
    args=None,
    *,
    drutil_drive: int | None = None,
    device_node: str | None = None,
) -> bool:
    """Return True if optical media is loaded in the targeted drive."""
    status = drive_status(args, drutil_drive=drutil_drive, device_node=device_node)
    return bool(status["media_present"])


def _is_slim_tray_drive(drive_id: str | None) -> bool:
    """Return True for slim/laptop optical drives whose spring tray cannot auto-close."""
    return bool(drive_id and _SLIM_TRAY_DRIVE_RE.search(drive_id))


def respin_drive(
    args=None,
    *,
    drutil_drive: int | None = None,
    device_node: str | None = None,
) -> bool:
    """Reset a hung LibreDrive handshake via tray cycle (motorized) or cooldown (slim).

    Slim non-motorized drives (e.g. LG BU40N) cannot auto-close their spring
    tray via `drutil tray close`; on those drives we unmount and cool down only
    without ejecting. Returns True if a tray cycle ran, False if cooldown-only.
    """
    exp_drive, exp_node = _explicit_drive_target(
        args, drutil_drive=drutil_drive, device_node=device_node
    )
    if exp_drive is not None or exp_node is not None:
        resolved_drive = drutil_drive_index(
            args,
            drutil_drive=drutil_drive,
            device_node=device_node,
            probe=(exp_node is not None),
        )
        drive_idx = (resolved_drive - 1) if resolved_drive is not None else None
    else:
        drive_idx = _drive_idx_from_args(args) if args is not None else None

    if exp_node is not None:
        unmount_blocking_volume(None, drive_idx=drive_idx, device_node=exp_node)
    else:
        unmount_blocking_volume(None, drive_idx=drive_idx)
    drive_id = getattr(args, "_drive_identity", None) if args is not None else None
    if drive_id is None:
        drive_id = _drive_identity(drive_idx if drive_idx is not None else 0)
    if _is_slim_tray_drive(drive_id):
        print(
            f"  {C.YLW}drive handshake stuck on slim-tray drive ({drive_id}) — "
            f"skipping tray eject (non-motorized tray cannot auto-close); "
            f"unmounted + cooling down {PROBE_COOLDOWN_S}s{C.R}",
            flush=True,
        )
        time.sleep(PROBE_COOLDOWN_S)
        return False
    print(
        f"  {C.YLW}drive handshake stuck — re-spinning the disc "
        f"(tray eject + reclose, disc stays in){C.R}",
        flush=True,
    )
    drive_args = ["-drive", str(drive_idx + 1)] if drive_idx is not None else []
    for sub_cmd in (["tray", "eject"], ["tray", "close"]):
        try:
            subprocess.run(
                [DRUTIL_BIN] + drive_args + sub_cmd,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
        if sub_cmd[-1] == "eject":
            time.sleep(3)
    time.sleep(20)
    return True


def scan_for_key_expiry(msgs: list[str]) -> int | None:
    """Return days remaining for a MakeMKV evaluation key, or None if permanent/unknown."""
    for m in msgs:
        if re.search(r"evaluat", m, re.IGNORECASE):
            match = re.search(r"(\d+)\s+day", m)
            if match:
                return int(match.group(1))
    return None


def _stream_reader(stream, q: queue.Queue) -> None:
    try:
        for line in stream:
            q.put(("line", line))
    finally:
        q.put(("eof", None))


def _parse_msg_fields(line: str) -> tuple[str | None, list[str] | None]:
    if ":" not in line:
        return None, None
    tag, rest = line.split(":", 1)
    out, cur, in_q, i = [], "", False, 0
    while i < len(rest):
        c = rest[i]
        if c == '"':
            if in_q and i + 1 < len(rest) and rest[i + 1] == '"':
                cur += '"'
                i += 2
                continue
            in_q = not in_q
        elif c == "," and not in_q:
            out.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    out.append(cur)
    return tag, out


def check_key_expiry(args) -> int | None:
    """Query MakeMKV against nonexistent `disc:9999` to surface beta-key expiry
    without touching or resetting any live optical drive."""
    try:
        proc = subprocess.Popen(
            [args.makemkvcon, "-r", "--cache=128", "info", "disc:9999"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError:
        return None

    qq: queue.Queue = queue.Queue()
    threading.Thread(target=_stream_reader, args=(proc.stdout, qq), daemon=True).start()
    lines: list[str] = []
    last_t = time.time()
    start = time.time()
    while True:
        now = time.time()
        if now - start > 20 or now - last_t > 5:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            break
        try:
            kind, payload = qq.get(timeout=1)
        except queue.Empty:
            continue
        if kind == "eof":
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
        if kind == "line":
            lines.append(payload.rstrip())
            last_t = now

    msgs: list[str] = []
    for line in lines:
        tag, f = _parse_msg_fields(line)
        if tag == "MSG" and f and len(f) >= 4:
            msgs.append(f[3])
    days = scan_for_key_expiry(msgs)
    if days is None:
        return None
    if days <= 14:
        print(f"{C.YLW}MakeMKV beta key expires in {days} day(s).{C.R}")
        print(f"{C.YLW}  Consider buying a permanent license or refreshing the beta key:{C.R}")
        print(f"{C.YLW}  https://forum.makemkv.com/forum/viewtopic.php?t=1053{C.R}")
    else:
        print(f"{C.D}MakeMKV evaluation key: {days} days remaining.{C.R}")
    return days


def eject_disc(
    args=None,
    *,
    device: str | None = None,
    drutil_drive: int | None = None,
    device_node: str | None = None,
) -> bool:
    """Eject the disc in the verified target drive with bounded timeouts and diskutil fallback."""
    if args is not None and getattr(args, "no_eject", False):
        return False

    target = device if device is not None else args
    exp_drive, exp_node = _explicit_drive_target(
        target, drutil_drive=drutil_drive, device_node=device_node
    )
    resolved_drive, resolved_node, cached_out = _resolve_drive_target(
        target, drutil_drive=drutil_drive, device_node=device_node, probe=True
    )

    if resolved_drive is None and (exp_drive is not None or exp_node is not None):
        print(
            f"{C.YLW}Refusing eject: verified drive target did not match an active drive.{C.R}"
        )
        append_log(
            args,
            "WARN  eject skipped (verified physical drive target mismatch)",
        )
        return False

    disk_node = resolved_node
    if disk_node is None and cached_out is None:
        disk_node = _drutil_disk_node(drutil_drive=resolved_drive)

    candidates: list[list[str]] = []
    if resolved_drive is not None:
        candidates.append([DRUTIL_BIN, "-drive", str(resolved_drive), "eject"])
        candidates.append([DRUTIL_BIN, "-drive", str(resolved_drive), "tray", "eject"])
    else:
        candidates.append([DRUTIL_BIN, "eject", "external"])
        candidates.append([DRUTIL_BIN, "tray", "eject"])
    if disk_node:
        candidates.append([DISKUTIL_BIN, "eject", "force", disk_node])
    for cmd in candidates:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if r.returncode == 0:
            return True
    print(f"{C.YLW}Eject failed; remove disc manually before next.{C.R}")
    append_log(args, "WARN  eject failed (all eject variants returned non-zero)")
    return False


def eject(
    args,
    *,
    drutil_drive: int | None = None,
    device_node: str | None = None,
) -> None:
    """Eject the disc in the targeted drive with bounded timeouts and diskutil fallback."""
    eject_disc(args, drutil_drive=drutil_drive, device_node=device_node)


def beep(args, ok: bool) -> None:
    """Play a non-blocking macOS system sound unless `--no-sound` is set."""
    if getattr(args, "no_sound", False):
        return
    sound = (
        "/System/Library/Sounds/Glass.aiff"
        if ok
        else "/System/Library/Sounds/Sosumi.aiff"
    )
    try:
        subprocess.Popen(
            ["afplay", sound], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError:
        pass
