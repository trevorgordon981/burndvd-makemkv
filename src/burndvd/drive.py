"""Optical drive enumeration, slim-tray safety guard, unmount/eject/respin, and key expiry."""

from __future__ import annotations

import queue
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from burndvd.config import (
    C,
    DISKUTIL_BIN,
    DRUTIL_BIN,
    MOUNT_BIN,
    PROBE_COOLDOWN_S,
)

_SLIM_TRAY_DRIVE_RE = re.compile(
    r"\b(?:BU\d+N|BP\d+NB\d+|WP\d+NB\d+|SLIM)\b", re.IGNORECASE
)


def append_log(args, line: str) -> None:
    """Append a timestamped audit line next to `--state`."""
    state_path = getattr(args, "state", None)
    if not state_path:
        return
    log = Path(state_path).with_suffix(".log")
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now().isoformat(timespec='seconds')}  {line}\n")


def _drive_idx_from_args(args) -> int | None:
    """Extract the 0-based `disc:N` index from `args.device`, or None for non-disc inputs."""
    m = re.match(r"disc:(\d+)$", getattr(args, "device", "") or "")
    return int(m.group(1)) if m else None


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


def _drutil_disk_node(drive_idx: int | None = None) -> str | None:
    """Return `/dev/diskN` for 0-based `drive_idx` (or first drive if None)."""
    cmd = [DRUTIL_BIN, "status"]
    if drive_idx is not None:
        cmd += ["-drive", str(drive_idx + 1)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(r"Name:\s+(/dev/disk\d+)", r.stdout or "")
    return m.group(1) if m else None


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
    rdisk_path: str | None, drive_idx: int | None = None
) -> bool:
    """Unmount an auto-mounted optical volume so MakeMKV can open raw blocks.

    When `drive_idx` is given, strictly targets that drive so concurrent rips on
    other optical drives are never disturbed.
    """
    try:
        subprocess.run(["pkill", "-x", "DVD Player"], capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass

    candidates: list[str] = []
    m = re.match(r"^/dev/r(disk\d+)$", rdisk_path or "")
    if m:
        candidates.append(f"/dev/{m.group(1)}")
    if drive_idx is not None:
        drutil_node = _drutil_disk_node(drive_idx)
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


def disc_present(args=None) -> bool:
    """Return True if optical media is loaded in the targeted drive."""
    cmd = [DRUTIL_BIN, "status"]
    drive_idx = _drive_idx_from_args(args) if args is not None else None
    if drive_idx is not None:
        cmd += ["-drive", str(drive_idx + 1)]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return False
    m = re.search(r"Type:\s*(.+)", out)
    return bool(m) and "no media" not in m.group(1).strip().lower()


def _is_slim_tray_drive(drive_id: str | None) -> bool:
    """Return True for slim/laptop optical drives whose spring tray cannot auto-close."""
    return bool(drive_id and _SLIM_TRAY_DRIVE_RE.search(drive_id))


def respin_drive(args=None) -> bool:
    """Reset a hung LibreDrive handshake via tray cycle (motorized) or cooldown (slim).

    Slim non-motorized drives (e.g. LG BU40N) cannot auto-close their spring
    tray via `drutil tray close`; on those drives we unmount and cool down only
    without ejecting. Returns True if a tray cycle ran, False if cooldown-only.
    """
    drive_idx = _drive_idx_from_args(args) if args is not None else None
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


def eject(args) -> None:
    """Eject the disc in the targeted drive with bounded timeouts and diskutil fallback."""
    if getattr(args, "no_eject", False):
        return
    drive_idx = _drive_idx_from_args(args)
    disk_node = _drutil_disk_node(drive_idx)
    candidates: list[list[str]] = []
    if drive_idx is not None:
        candidates.append([DRUTIL_BIN, "-drive", str(drive_idx + 1), "eject"])
        candidates.append([DRUTIL_BIN, "-drive", str(drive_idx + 1), "tray", "eject"])
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
            return
    print(f"{C.YLW}Eject failed; remove disc manually before next.{C.R}")
    append_log(args, "WARN  eject failed (all eject variants returned non-zero)")


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
