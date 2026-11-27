#!/usr/bin/env python3
"""checkrip — inspect active rips or verify a finished rip directory.

Read-only. Never moves or deletes files; reports play-all blobs, numbering gaps,
and content-verified duplicates.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

# Allow running directly under minimal PATH (e.g. /usr/bin/python3) without site-packages.
_REPO_SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", ".."))
if os.path.isdir(os.path.join(_REPO_SRC, "burndvd")) and _REPO_SRC not in sys.path:
    sys.path.insert(0, _REPO_SRC)

from burndvd.config import MEDIA_SUBDIRS, NFS_MEDIA_PREFIX, SMB_MEDIA_PREFIX, resolve_binary

EP_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
VIDEO_EXTS = (".mkv", ".mp4", ".m4v", ".ts")
BLOB_FACTOR = 1.6
DUP_TOL = 0.001

FFPROBE = resolve_binary(
    "ffprobe",
    "/opt/homebrew/bin/ffprobe",
    "/usr/local/bin/ffprobe",
    env_var="BURNDVD_FFPROBE",
)
FFMPEG = resolve_binary(
    "ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    env_var="BURNDVD_FFMPEG",
)

_LAST_PROBE_ERRORS: dict[str, str] = {}

_MEDIA_BASES = (NFS_MEDIA_PREFIX, "/private/tmp/nasmedia", SMB_MEDIA_PREFIX)
_MEDIA_SUBDIRS = MEDIA_SUBDIRS


def _media_base() -> str:
    for base in _MEDIA_BASES:
        if os.path.isdir(os.path.join(base, _MEDIA_SUBDIRS[0])):
            return base
    return _MEDIA_BASES[-1]


MEDIA_ROOTS = tuple(os.path.join(_media_base(), sub) for sub in _MEDIA_SUBDIRS)
SKIP_DIRS = {"Extras", "#recycle", "@eaDir"}
MAX_RIP_DEPTH = 3

ACTIVE_TAIL_LINES = 14
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _parse_ffmpeg_out_time_s(progress_stdout: str) -> float | None:
    """Parse final packet timestamp (seconds) from ffmpeg `-progress pipe:1` output."""
    val: float | None = None
    for line in (progress_stdout or "").splitlines():
        if line.startswith("out_time_us="):
            raw = line.split("=", 1)[1].strip()
            try:
                us = int(raw)
                if us >= 0:
                    val = us / 1_000_000.0
            except ValueError:
                pass
    return val


def verify_stream_integrity(
    path: str, expected_dur: float | None = None
) -> tuple[bool, str]:
    """Verify container packet integrity end-to-end without full video pixel decode."""
    try:
        out = subprocess.run(
            [
                FFMPEG,
                "-nostdin",
                "-v",
                "error",
                "-err_detect",
                "explode",
                "-xerror",
                "-progress",
                "pipe:1",
                "-i",
                path,
                "-c",
                "copy",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"stream integrity check failed ({exc})"
    stderr_txt = (getattr(out, "stderr", "") or "").strip()
    if out.returncode != 0:
        return (
            False,
            f"corrupt/truncated stream (ffmpeg exit {out.returncode}: {stderr_txt[:160]})",
        )
    if stderr_txt:
        return False, f"corrupt/truncated stream ({stderr_txt[:160]})"
    if expected_dur is not None and expected_dur > 0:
        read_s = _parse_ffmpeg_out_time_s(getattr(out, "stdout", "") or "")
        if read_s is not None:
            tol = max(0.35, min(10.0, expected_dur * 0.05))
            if read_s <= 0 or (expected_dur - read_s) > tol:
                return (
                    False,
                    f"truncated stream (read {read_s:.2f}s of {expected_dur:.2f}s container duration)",
                )
    return True, ""


def probe_duration(path: str) -> float | None:
    """Return positive duration in seconds (float) for a complete, readable video file, or None."""
    _LAST_PROBE_ERRORS.pop(path, None)
    try:
        if os.path.getsize(path) <= 0:
            return None
    except OSError:
        return None
    try:
        out = subprocess.run(
            [
                FFPROBE,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,duration",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        stderr_txt = (getattr(out, "stderr", "") or "").strip()
        if out.returncode != 0:
            if stderr_txt:
                _LAST_PROBE_ERRORS[path] = f"probe_failed ({stderr_txt[:160]})"
            return None
        if stderr_txt:
            _LAST_PROBE_ERRORS[path] = f"probe_failed ({stderr_txt[:160]})"
            return None
        data = json.loads(out.stdout or "{}")
        streams = data.get("streams") or []
        video_streams = [s for s in streams if s.get("codec_type") == "video"]
        if not video_streams:
            return None
        raw_dur = (data.get("format") or {}).get("duration")
        if raw_dur in (None, "", "N/A"):
            raw_dur = next(
                (
                    s.get("duration")
                    for s in video_streams
                    if s.get("duration") not in (None, "", "N/A")
                ),
                None,
            )
        if raw_dur is None:
            return None
        dur = float(raw_dur)
        if dur <= 0:
            return None
        ok, integrity_err = verify_stream_integrity(path, expected_dur=dur)
        if not ok:
            _LAST_PROBE_ERRORS[path] = integrity_err
            return None
        return dur
    except (ValueError, TypeError, OSError, subprocess.SubprocessError):
        return None

def edge_hash(path: str, span: int = 8 << 20) -> str | None:
    """Hash first + last `span` bytes as a cheap content fingerprint."""
    try:
        h = hashlib.sha1()
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            h.update(fh.read(min(span, size)))
            if size > span:
                fh.seek(max(0, size - span))
                h.update(fh.read(span))
        return h.hexdigest()
    except OSError:
        return None


def human_min(sec: float | None) -> str:
    if sec is None:
        return "  ?  "
    return f"{sec / 60:5.1f}m"


def human_gb(nbytes: int) -> str:
    return f"{nbytes / 1e9:5.1f} GB"


def scan(directory: str) -> list[dict]:
    files = []
    with os.scandir(directory) as it:
        for entry in it:
            if entry.is_file() and entry.name.lower().endswith(VIDEO_EXTS):
                files.append(entry.path)
    files.sort()
    entries = []
    for f in files:
        m = EP_RE.search(os.path.basename(f))
        dur = probe_duration(f)
        entry = {
            "path": f,
            "name": os.path.basename(f),
            "season": int(m.group(1)) if m else None,
            "episode": int(m.group(2)) if m else None,
            "size": os.path.getsize(f),
            "duration": dur,
        }
        if dur is None:
            err = _LAST_PROBE_ERRORS.pop(f, None)
            if err:
                entry["probe_error"] = err
        entries.append(entry)
    return entries


def analyze(entries: list[dict], expected: int | None = None) -> dict:
    unreadable: list[dict] = []
    for e in entries:
        reasons: list[str] = []
        if not e.get("size") or e["size"] <= 0:
            reasons.append("empty (0 bytes)")
        if e.get("duration") is None or e["duration"] <= 0:
            reasons.append(
                e.get("probe_error") or "probe_failed (no valid duration/video stream)"
            )
        if reasons:
            unreadable.append({**e, "reason": "; ".join(reasons)})

    unreadable_paths = {u["path"] for u in unreadable}
    healthy = [e for e in entries if e["path"] not in unreadable_paths]

    durs = [e["duration"] for e in healthy if e["duration"]]
    median = statistics.median(durs) if durs else None

    blobs, dupes, gaps = [], [], []
    if median:
        for e in healthy:
            d = e["duration"]
            if d and d > median * BLOB_FACTOR:
                est = round(d / median)
                blobs.append({**e, "est_episodes": est})

    blob_paths = {b["path"] for b in blobs}
    singles = [e for e in healthy if e["path"] not in blob_paths]

    by_num: dict[int, list[dict]] = {}
    for e in singles:
        if e["episode"] is not None:
            by_num.setdefault(e["episode"], []).append(e)
    for num, group in by_num.items():
        if len(group) > 1:
            dupes.append(
                (group[0]["name"], group[1]["name"], f"both numbered E{num:02d}")
            )
    for i in range(len(singles)):
        for j in range(i + 1, len(singles)):
            a, b = singles[i], singles[j]
            if not (a["size"] and b["size"]):
                continue
            if abs(a["size"] - b["size"]) > DUP_TOL * max(a["size"], b["size"]):
                continue
            ha, hb = edge_hash(a["path"]), edge_hash(b["path"])
            if ha is not None and ha == hb:
                dupes.append((a["name"], b["name"], "identical content"))

    nums = sorted(e["episode"] for e in singles if e["episode"] is not None)
    if nums:
        full = set(range(min(nums), max(nums) + 1))
        gaps = sorted(full - set(nums))

    real_count = len([e for e in singles if e["episode"] is not None])
    missing_vs_expected = (expected - real_count) if expected is not None else None

    return {
        "median_min": round(median / 60, 1) if median else None,
        "unreadable": unreadable,
        "blobs": blobs,
        "dupes": dupes,
        "gaps": gaps,
        "real_count": real_count,
        "expected": expected,
        "missing_vs_expected": missing_vs_expected,
    }

def inspect_rips(directory: str, expected: int | None = None) -> dict:
    """Library helper: scan and analyze a finished rip directory."""
    entries = scan(directory)
    result = analyze(entries, expected=expected)
    result["files"] = entries
    return result


def _split_cmd(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _extract_flag(argv: list[str], flag: str) -> str | None:
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _list_processes() -> list[tuple[int, int, str]]:
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    procs = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            procs.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return procs


def _is_ancestor(ancestor_pid: int, pid: int, ppid_map: dict[int, int]) -> bool:
    seen = set()
    cur = pid
    while cur and cur not in seen:
        if cur == ancestor_pid:
            return True
        seen.add(cur)
        cur = ppid_map.get(cur)
    return False


def _script_log_from_argv(argv: list[str]) -> str | None:
    if not argv or os.path.basename(argv[0]) != "script":
        return None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-t", "-F"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return None


def _wrapper_log_from_argv(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a == "--internal-pty-run" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _staging_for_pid(pid: int) -> str | None:
    base = os.path.expanduser("~/.cache/burndvd/staging")
    if not os.path.isdir(base):
        return None
    suffix = f"-{pid}"
    matches = []
    try:
        with os.scandir(base) as it:
            for entry in it:
                if entry.is_dir() and entry.name.endswith(suffix):
                    matches.append(entry.path)
    except OSError:
        return None
    if not matches:
        return None
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0]


def _is_ripqueue_argv(argv: list[str]) -> bool:
    if not argv:
        return False
    exe = os.path.basename(argv[0]).lower()
    if exe in ("ripqueue.py", "burndvd-queue"):
        return True
    if exe.startswith("python"):
        for idx, tok in enumerate(argv[1:], 1):
            if os.path.basename(tok) in ("ripqueue.py", "burndvd-queue"):
                return True
            if tok == "-m" and idx + 1 < len(argv) and argv[idx + 1] == "burndvd.cli.ripqueue":
                return True
    return False


def _is_watch_argv(argv: list[str]) -> bool:
    if not argv:
        return False
    exe = os.path.basename(argv[0]).lower()
    if exe.startswith("python"):
        for idx, tok in enumerate(argv[1:], 1):
            if tok == "-m" and idx + 1 < len(argv) and argv[idx + 1] == "burndvd.cli.watch":
                return True
    return False


def _read_proc_env_hints(pid: int) -> tuple[str | None, str | None]:
    """Read BURNDVD_DRIVE_DEVICE / BURNDVD_DRIVE_SERIAL from `ps eww -p <pid>`."""
    try:
        r = subprocess.run(
            ["ps", "eww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    out = r.stdout or ""
    m_dev = re.search(r"\bBURNDVD_DRIVE_DEVICE=([^\s]+)", out)
    m_ser = re.search(r"\bBURNDVD_DRIVE_SERIAL=([^\s]+)", out)
    return (
        m_dev.group(1).strip() if m_dev else None,
        m_ser.group(1).strip() if m_ser else None,
    )


def _find_open_burndvd_log(pid: int) -> str | None:
    """Locate an open ~/logs/burndvd-*.log FD for watch-spawned child rips."""
    try:
        r = subprocess.run(
            ["lsof", "-Fn", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in (r.stdout or "").splitlines():
        if line.startswith("n") and "/logs/burndvd-" in line and line.endswith(".log"):
            return line[1:]
    return None


def _claim_hints_for_rip(pid: int, state_path: str | None) -> tuple[str | None, str | None]:
    """Infer (drive_device, drive_serial) from ~/.cache/burndvd/drive-claims/*."""
    claim_dir = os.path.expanduser("~/.cache/burndvd/drive-claims")
    if not os.path.isdir(claim_dir):
        return None, None
    dev, ser = None, None
    try:
        for entry in os.scandir(claim_dir):
            if not entry.is_dir() or not entry.name.endswith(".claim"):
                continue
            c_state = ""
            c_pid = ""
            try:
                c_state = Path(entry.path, "state").read_text(encoding="utf-8").strip()
            except OSError:
                pass
            try:
                c_pid = Path(entry.path, "pid").read_text(encoding="utf-8").strip()
            except OSError:
                pass
            if (state_path and c_state == state_path) or (c_pid.isdigit() and int(c_pid) == pid):
                if entry.name.startswith("serial-"):
                    ser = entry.name[len("serial-") : -len(".claim")]
                elif entry.name.startswith("dev-"):
                    raw = entry.name[len("dev-") : -len(".claim")]
                    dev = raw if raw.startswith("/dev/") else f"/dev/{raw}"
    except OSError:
        pass
    return dev, ser


def _queue_and_slot_metadata(pid: int, queue_path: str | None) -> dict:
    """Read title/season/volume/episode_range from queue CSV and active-tv-slots JSON."""
    meta = {"title": None, "season": None, "volume": None, "episode_range": None}
    if queue_path and os.path.isfile(queue_path):
        try:
            with open(queue_path, encoding="utf-8", newline="") as fh:
                row = next(csv.DictReader(fh), None)
            if row:
                meta["title"] = (row.get("title") or "").strip() or None
                raw_s = (row.get("season") or "").strip()
                if raw_s.isdigit():
                    meta["season"] = int(raw_s)
                notes = (row.get("notes") or "").strip()
                m_vol = re.search(r"auto-built from\s+(\S+)", notes)
                if m_vol:
                    meta["volume"] = m_vol.group(1)
                raw_ep = (row.get("episode_start") or "").strip()
                if raw_ep.isdigit() and meta["season"] is not None:
                    meta["episode_range"] = f"S{meta['season']:02d}E{int(raw_ep):02d}+"
        except OSError:
            pass
    slot_dir = os.environ.get(
        "BURNDVD_ACTIVE_TV_SLOTS_DIR",
        os.path.expanduser("~/.cache/burndvd/active-tv-slots"),
    )
    slot_file = os.path.join(slot_dir, f"pid-{pid}.json")
    if os.path.isfile(slot_file):
        try:
            data = json.loads(Path(slot_file).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                s = int(data.get("season") or 0)
                ep_s = int(data.get("episode_start") or 0)
                ep_c = int(data.get("episode_count") or 0)
                if data.get("title"):
                    meta["title"] = str(data["title"])
                if s > 0:
                    meta["season"] = s
                if s > 0 and ep_s > 0 and ep_c > 0:
                    meta["episode_range"] = f"S{s:02d}E{ep_s:02d}-E{ep_s + ep_c - 1:02d}"
        except (OSError, ValueError, TypeError):
            pass
    return meta


def active_watchers(procs=None) -> list[dict]:
    """Detect active `burndvd --watch` daemon processes."""
    if procs is None:
        procs = _list_processes()
    out = []
    for pid, _ppid, cmd in procs:
        argv = _split_cmd(cmd)
        if not _is_watch_argv(argv):
            continue
        out.append(
            {
                "pid": pid,
                "show": _extract_flag(argv, "--show"),
                "season": _extract_flag(argv, "--season"),
            }
        )
    return out


def active_rips(procs=None) -> list[dict]:
    """Detect all active rips via running ripqueue / burndvd-watch / makemkvcon processes."""
    live_probe = procs is None
    if procs is None:
        procs = _list_processes()

    ppid_map = {pid: ppid for pid, ppid, _ in procs}
    parsed = [(pid, ppid, cmd, _split_cmd(cmd)) for pid, ppid, cmd in procs]

    ripqueues = []
    scripts = []
    wrappers = []
    makemkvs = []

    for pid, ppid, cmd, argv in parsed:
        if not argv:
            continue
        exe = os.path.basename(argv[0]).lower()
        if exe == "makemkvcon":
            staging = None
            mk_dev = None
            for tok in argv[1:]:
                if tok.startswith("disc:") or tok.startswith("dev:"):
                    mk_dev = tok
            for tok in reversed(argv[1:]):
                if "/staging/" in tok:
                    staging = tok
                    break
            makemkvs.append(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "argv": argv,
                    "staging": staging,
                    "device": mk_dev,
                }
            )
        elif exe == "script":
            log_path = _script_log_from_argv(argv)
            if log_path and any(
                "ripqueue.py" in a or "burndvd" in a for a in argv[1:]
            ):
                scripts.append(
                    {
                        "pid": pid,
                        "ppid": ppid,
                        "log_path": log_path,
                        "state": _extract_flag(argv, "--state"),
                    }
                )
        elif any(
            os.path.basename(a) in ("burndvd-wrapper.sh", "burndvd")
            for a in argv[:2]
        ):
            log_path = _wrapper_log_from_argv(argv)
            if log_path:
                wrappers.append(
                    {
                        "pid": pid,
                        "ppid": ppid,
                        "log_path": log_path,
                        "state": _extract_flag(argv, "--state"),
                    }
                )
        elif _is_ripqueue_argv(argv):
            if "--validate-queue" in argv or "--scan" in argv:
                continue
            ripqueues.append(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "state": _extract_flag(argv, "--state"),
                    "queue": _extract_flag(argv, "--queue"),
                    "device": _extract_flag(argv, "--device"),
                }
            )

    rips = []
    used_mk = set()
    for rq in ripqueues:
        rq_pid = rq["pid"]
        rq_state = rq["state"]

        log_path = None
        for sc in scripts:
            if _is_ancestor(sc["pid"], rq_pid, ppid_map):
                log_path = sc["log_path"]
                break
        if log_path is None and rq_state:
            for sc in scripts:
                if sc["state"] == rq_state:
                    log_path = sc["log_path"]
                    break
        if log_path is None:
            for wr in wrappers:
                if _is_ancestor(wr["pid"], rq_pid, ppid_map) or (
                    rq_state and wr["state"] == rq_state
                ):
                    log_path = wr["log_path"]
                    break
        if log_path is None and live_probe:
            log_path = _find_open_burndvd_log(rq_pid)

        staging = None
        mk_device = None
        for mk in makemkvs:
            if _is_ancestor(rq_pid, mk["pid"], ppid_map):
                staging = mk["staging"]
                mk_device = mk.get("device")
                used_mk.add(mk["pid"])
                break
        if staging is None:
            for mk in makemkvs:
                if (
                    mk["pid"] not in used_mk
                    and mk["staging"]
                    and mk["staging"].endswith(f"-{rq_pid}")
                ):
                    staging = mk["staging"]
                    mk_device = mk.get("device")
                    used_mk.add(mk["pid"])
                    break
        if staging is None:
            staging = _staging_for_pid(rq_pid)

        drive_dev, drive_ser = _claim_hints_for_rip(rq_pid, rq_state)
        if live_probe and (not drive_dev or not drive_ser):
            env_dev, env_ser = _read_proc_env_hints(rq_pid)
            drive_dev = drive_dev or env_dev
            drive_ser = drive_ser or env_ser

        meta = _queue_and_slot_metadata(rq_pid, rq["queue"])
        rips.append(
            {
                "pid": rq_pid,
                "state": rq_state,
                "queue": rq["queue"],
                "device": mk_device or rq["device"],
                "drive_device": drive_dev,
                "drive_serial": drive_ser,
                "title": meta["title"],
                "season": meta["season"],
                "volume": meta["volume"],
                "episode_range": meta["episode_range"],
                "log_path": log_path,
                "staging": staging,
            }
        )

    for mk in makemkvs:
        if mk["pid"] not in used_mk and mk["staging"]:
            rips.append(
                {
                    "pid": mk["pid"],
                    "state": None,
                    "queue": None,
                    "device": mk.get("device"),
                    "drive_device": None,
                    "drive_serial": None,
                    "title": None,
                    "season": None,
                    "volume": None,
                    "episode_range": None,
                    "log_path": None,
                    "staging": mk["staging"],
                }
            )
    return rips


def active_staging_dir() -> str | None:
    for rip in active_rips():
        if rip["staging"]:
            return rip["staging"]
    return None


def staged_bytes(staging: str) -> int:
    total = 0
    with os.scandir(staging) as it:
        for entry in it:
            if entry.is_file() and entry.name.lower().endswith(".mkv"):
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    return total


def measure_rip_speed(staging: str, secs: float = 6.0) -> float | None:
    if not staging or not os.path.isdir(staging):
        return None
    try:
        b0 = staged_bytes(staging)
        time.sleep(secs)
        b1 = staged_bytes(staging)
    except OSError:
        return None
    return max(0, b1 - b0) / secs / 1e6


def measure_rip_speeds(stagings: list[str | None], secs: float = 3.0) -> dict[str, float | None]:
    valid = [s for s in stagings if s and os.path.isdir(s)]
    if not valid:
        return {}
    b0: dict[str, int] = {}
    for s in valid:
        try:
            b0[s] = staged_bytes(s)
        except OSError:
            pass
    if not b0:
        return {}
    time.sleep(secs)
    speeds: dict[str, float | None] = {}
    for s, start_b in b0.items():
        try:
            b1 = staged_bytes(s)
            if start_b == 0 and b1 == 0:
                speeds[s] = None
            else:
                speeds[s] = max(0, b1 - start_b) / secs / 1e6
        except OSError:
            speeds[s] = None
    return speeds


def active_rip_tail(log_path: str | None = None, n: int = ACTIVE_TAIL_LINES) -> list[str] | None:
    if log_path is None:
        rips = active_rips()
        if not rips:
            return None
        log_path = rips[0].get("log_path")
        if not log_path:
            logs = sorted(
                glob.glob(os.path.expanduser("~/logs/burndvd-*.log")),
                key=os.path.getmtime,
                reverse=True,
            )
            if not logs:
                return []
            log_path = logs[0]
    try:
        with open(log_path, "r", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return []
    bar = re.compile(r"^\s*\[[#\-]+\]\s")
    lines: list[str] = []
    for seg in re.split(r"[\r\n]", raw):
        seg = seg.rstrip()
        if not seg:
            continue
        is_bar = bool(bar.match(ANSI_RE.sub("", seg)))
        if is_bar and lines and bar.match(ANSI_RE.sub("", lines[-1])):
            lines[-1] = seg
            continue
        if lines and lines[-1] == seg:
            continue
        lines.append(seg)
    return lines[-n:]


def latest_rip_dir() -> str | None:
    best, best_mtime = None, -1.0
    for root in MEDIA_ROOTS:
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("/").count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath.rstrip("/").count(os.sep) - base_depth >= MAX_RIP_DEPTH:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            if os.path.basename(dirpath) in SKIP_DIRS:
                continue
            if not any(f.lower().endswith(VIDEO_EXTS) for f in filenames):
                continue
            m = os.stat(dirpath).st_mtime
            if m > best_mtime:
                best, best_mtime = dirpath, m
    return best


_PROGRESS_RE = re.compile(
    r"(\[[#=\-]+\])\s+(\d+(?:\.\d+)?)%"
    r"(?:\s+el\s+(\S+))?"
    r"(?:\s+ETA\s+(\S+))?"
    r"(?:\s+\d{2}:\d{2}:\d{2})?"
    r"(?:\s+(\[\d+/\d+\]))?"
)


def _diskutil_external_volumes() -> dict[str, str]:
    """Return {/dev/diskN: VOLUME_NAME} for external physical disks (read-only)."""
    try:
        res = subprocess.run(
            ["diskutil", "list", "external"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode != 0:
            return {}
    except (OSError, subprocess.SubprocessError):
        return {}
    vols: dict[str, str] = {}
    cur_dev: str | None = None
    for raw in res.stdout.splitlines():
        m_dev = re.match(r"^(/dev/disk\d+)\b", raw.strip())
        if m_dev:
            cur_dev = m_dev.group(1)
            continue
        if cur_dev and raw.strip().startswith("0:"):
            m_row = re.match(
                r"^0:\s+(.+?)\s+\*?[\d.]+\s+[KMGT]?B\s+disk\d+\s*$",
                raw.strip(),
            )
            if m_row:
                name = m_row.group(1).strip()
                if name and not name.startswith("Apple_"):
                    vols[cur_dev] = name
    return vols


def connected_optical_drives() -> list[dict]:
    """Discover connected optical drives via read-only drutil status + diskutil.

    Never touches MakeMKV or disturbs active rips. Returns [] if unavailable.
    """
    try:
        res = subprocess.run(
            ["drutil", "status"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode != 0:
            return []
    except (OSError, subprocess.SubprocessError):
        return []

    vols = _diskutil_external_volumes()
    drives: list[dict] = []
    cur: dict | None = None
    for line in res.stdout.splitlines():
        if re.match(r"^\s*Vendor\s+Product\s+Rev\b", line):
            if cur is not None:
                drives.append(cur)
            cur = {
                "slot": len(drives) + 1,
                "model": None,
                "media_type": None,
                "device": None,
                "volume": None,
            }
            continue
        if cur is None:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if cur["model"] is None and not stripped.startswith(
            ("Type:", "Sessions:", "Overwritable:", "Space ", "Writability:")
        ):
            cur["model"] = re.sub(r"\s+", " ", stripped)
            continue
        m_type = re.search(r"Type:\s*(.+?)(?:\s{2,}Name:\s*(\S+))?\s*$", line)
        if m_type:
            mtype = m_type.group(1).strip()
            dev = (m_type.group(2) or "").strip() or None
            cur["media_type"] = None if mtype.lower().startswith("no media") else mtype
            cur["device"] = dev
            if dev and dev in vols:
                cur["volume"] = vols[dev]
    if cur is not None:
        drives.append(cur)
    return drives


def _parse_rip_status(tail: list[str], speed: float | None) -> dict:
    """Summarize current rip stage, progress bar, title counter, elapsed, and ETA."""
    clean = [ANSI_RE.sub("", ln).strip() for ln in tail if ANSI_RE.sub("", ln).strip()]
    prog = None
    prog_idx = -1
    for i, ln in enumerate(clean):
        m = _PROGRESS_RE.search(ln)
        if m:
            prog = {
                "bar": m.group(1),
                "percent": m.group(2),
                "elapsed": m.group(3),
                "eta": m.group(4),
                "titles": m.group(5),
            }
            prog_idx = i

    post_lines = clean[prog_idx + 1 :] if prog_idx >= 0 else clean
    stage = "RIPPING" if (prog is not None or (speed is not None and speed > 0.1)) else "STARTING"
    detail = None
    for ln in reversed(post_lines):
        low = ln.lower()
        if any(k in low for k in ("copying", "rsync", "transferring", "publishing", "installing")):
            stage = "COPYING"
            detail = ln
            break
        if any(k in low for k in ("verifying", "ffprobe", "verify ", "integrity")):
            stage = "VERIFYING"
            detail = ln
            break
        if any(k in low for k in ("probing", "scanning", "opening disc")):
            stage = "PROBING"
            detail = ln
            break
        if ln.startswith("Selected ") or ln.startswith("Disc label:"):
            if stage != "RIPPING":
                detail = ln
                break

    if stage == "STARTING" and detail is None and clean:
        for ln in reversed(clean):
            if not ln.startswith("[") and not ln.startswith("staging:"):
                detail = ln
                break

    return {
        "stage": stage,
        "progress": prog,
        "detail": detail,
    }


def _active_claim_devices() -> dict[str, int]:
    """Return {/dev/diskN: pid} for live burndvd drive claims."""
    claim_dir = os.path.expanduser("~/.cache/burndvd/drive-claims")
    out: dict[str, int] = {}
    if not os.path.isdir(claim_dir):
        return out
    try:
        for entry in os.scandir(claim_dir):
            if not entry.is_dir() or not entry.name.endswith(".claim"):
                continue
            try:
                pid_txt = Path(entry.path, "pid").read_text(encoding="utf-8").strip()
                pid_val = int(pid_txt)
                os.kill(pid_val, 0)
            except (OSError, ValueError):
                continue
            if entry.name.startswith("dev-"):
                dev = "/dev/" + entry.name[4:-6]
                out[dev] = pid_val
    except OSError:
        pass
    return out

CHECKRIP_JSON_SCHEMA_VERSION = 1


def _norm_device_node(dev: str | None) -> str:
    raw = (dev or "").strip()
    if raw.startswith("/dev/rdisk"):
        return "/dev/disk" + raw[len("/dev/rdisk") :]
    return raw


def _match_drives_to_rips(drives: list[dict], rendered: list[dict]) -> list[dict]:
    """Attribute active rips to physical drives without label-stealing.

    Pass 1: Global exact match on stable hardware identity (serial, then device node).
    Pass 2: Volume-label fallback ONLY for unmatched drives and unmatched rips
            with NO known hardware identity, and ONLY when the volume label is
            unambiguous (1-to-1) across both unmatched sets.
    Pass 3: Append any remaining unmatched rips as standalone rows.
    """
    matched_rip_ids: set[int] = set()
    matched_by_drive: dict[int, dict] = {}

    # Pass 1: Global hardware-identity pass (serial, then normalized device node).
    for idx, d in enumerate(drives):
        d_serial = (d.get("serial") or "").strip()
        d_dev = _norm_device_node(d.get("device"))
        for item in rendered:
            if item["pid"] in matched_rip_ids:
                continue
            r_serial = (item.get("drive_serial") or "").strip()
            r_dev = _norm_device_node(item.get("drive_device"))
            if d_serial and r_serial and d_serial == r_serial:
                matched_by_drive[idx] = item
                matched_rip_ids.add(item["pid"])
                break
            if d_dev and r_dev and d_dev == r_dev:
                matched_by_drive[idx] = item
                matched_rip_ids.add(item["pid"])
                break

    # Pass 2: Restricted, unambiguous volume-label fallback only when the rip
    # has NO known hardware identity (drive_device / drive_serial).
    unmatched_drive_idxs = [i for i in range(len(drives)) if i not in matched_by_drive]
    unmatched_identityless_rips = [
        item
        for item in rendered
        if item["pid"] not in matched_rip_ids
        and not _norm_device_node(item.get("drive_device"))
        and not (item.get("drive_serial") or "").strip()
        and (item.get("volume") or "").strip()
    ]
    drive_vol_counts: dict[str, int] = {}
    for idx in unmatched_drive_idxs:
        vol = (drives[idx].get("volume") or "").strip().lower()
        if vol:
            drive_vol_counts[vol] = drive_vol_counts.get(vol, 0) + 1
    rip_vol_counts: dict[str, int] = {}
    for item in unmatched_identityless_rips:
        vol = (item.get("volume") or "").strip().lower()
        if vol:
            rip_vol_counts[vol] = rip_vol_counts.get(vol, 0) + 1

    for idx in unmatched_drive_idxs:
        vol = (drives[idx].get("volume") or "").strip().lower()
        if not vol:
            continue
        if drive_vol_counts.get(vol, 0) != 1 or rip_vol_counts.get(vol, 0) != 1:
            continue
        for item in unmatched_identityless_rips:
            if item["pid"] in matched_rip_ids:
                continue
            if (item.get("volume") or "").strip().lower() == vol:
                matched_by_drive[idx] = item
                matched_rip_ids.add(item["pid"])
                break

    drive_rows: list[dict] = [
        {"drive": d, "rip": matched_by_drive.get(idx)}
        for idx, d in enumerate(drives)
    ]

    # Pass 3: Unmatched rips (e.g. discovery unavailable or conflicting hardware identity).
    next_slot = len(drive_rows) + 1
    for item in rendered:
        if item["pid"] not in matched_rip_ids:
            drive_rows.append(
                {
                    "drive": {
                        "slot": next_slot,
                        "model": None,
                        "media_type": "BD-ROM",
                        "device": item["drive_device"] or item["device"],
                        "volume": item["volume"],
                    },
                    "rip": item,
                }
            )
            next_slot += 1

    return drive_rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify a finished rip directory.")
    ap.add_argument(
        "directory",
        nargs="?",
        default=None,
        help="season or movie directory to check (default: active or most recent rip)",
    )
    ap.add_argument(
        "--expected", type=int, default=None, help="expected episode count"
    )
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument(
        "--json-version",
        choices=("1", "2"),
        default=str(CHECKRIP_JSON_SCHEMA_VERSION),
        help=(
            "JSON schema version for active-rip output: "
            "1 (default, preserves legacy flat single-rip keys alongside drives/rips) "
            "or 2 (strict envelope only)"
        ),
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show raw MakeMKV log tail under each active drive",
    )
    args = ap.parse_args()

    if args.directory is None:
        rips = active_rips()
        watchers = active_watchers()
        if rips or watchers:
            speeds = measure_rip_speeds([r["staging"] for r in rips]) if rips else {}
            tail_n = ACTIVE_TAIL_LINES if args.verbose else 12
            rendered = []
            for r in rips:
                log_path = r["log_path"]
                if not log_path and len(rips) == 1:
                    logs = sorted(
                        glob.glob(os.path.expanduser("~/logs/burndvd-*.log")),
                        key=os.path.getmtime,
                        reverse=True,
                    )
                    log_path = logs[0] if logs else None
                tail = active_rip_tail(log_path, n=tail_n) if log_path else []
                speed = speeds.get(r["staging"]) if r["staging"] else None
                status = _parse_rip_status(tail, speed)
                rendered.append(
                    {
                        "pid": r["pid"],
                        "device": r["device"],
                        "drive_device": r.get("drive_device"),
                        "drive_serial": r.get("drive_serial"),
                        "title": r.get("title"),
                        "season": r.get("season"),
                        "volume": r.get("volume"),
                        "episode_range": r.get("episode_range"),
                        "staging": r["staging"],
                        "log_path": log_path,
                        "speed": speed,
                        "stage": status["stage"],
                        "progress": status["progress"],
                        "detail": status["detail"],
                        "tail": tail,
                    }
                )

            rendered.sort(
                key=lambda x: (
                    x.get("drive_device") or "~",
                    x.get("device") or "~",
                    x.get("pid") or 0,
                )
            )

            drives = connected_optical_drives()
            claim_devs = _active_claim_devices()
            drive_rows = _match_drives_to_rips(drives, rendered)

            def _item_json(item: dict) -> dict:
                return {
                    "pid": item["pid"],
                    "device": item["device"],
                    "drive_device": item["drive_device"],
                    "drive_serial": item["drive_serial"],
                    "title": item["title"],
                    "season": item["season"],
                    "volume": item["volume"],
                    "episode_range": item["episode_range"],
                    "stage": item["stage"],
                    "progress": item["progress"],
                    "staging": item["staging"],
                    "log_path": item["log_path"],
                    "speed_mbps": round(item["speed"], 1)
                    if item["speed"] is not None
                    else None,
                    "log_tail": [ANSI_RE.sub("", ln) for ln in item["tail"]],
                }

            def _drive_tag(item: dict, drv: dict | None = None) -> str:
                dev = (
                    item.get("drive_device")
                    or (drv.get("device") if drv else None)
                    or item.get("device")
                    or "unknown"
                )
                ser = item.get("drive_serial")
                return f"{dev} ({ser})" if ser else dev

            def _content_label(item: dict, drv: dict | None = None) -> str:
                bits = []
                if item.get("title"):
                    bits.append(item["title"])
                if item.get("episode_range"):
                    bits.append(item["episode_range"])
                vol = item.get("volume") or (drv.get("volume") if drv else None)
                if vol:
                    bits.append(f"[{vol}]")
                return " ".join(bits) if bits else "(scanning / unknown disc)"

            if args.json:
                drive_summary = []
                for row in drive_rows:
                    d = row["drive"]
                    r_item = row["rip"]
                    if r_item is not None:
                        st = r_item["stage"]
                    elif d.get("device") and d["device"] in claim_devs:
                        st = "STARTING"
                    elif d.get("media_type"):
                        st = "READY"
                    else:
                        st = "IDLE"
                    drive_summary.append(
                        {
                            "slot": d["slot"],
                            "device": d.get("device"),
                            "media_type": d.get("media_type"),
                            "volume": d.get("volume") or (r_item.get("volume") if r_item else None),
                            "status": st,
                            "pid": r_item["pid"]
                            if r_item
                            else claim_devs.get(d.get("device") or ""),
                        }
                    )
                schema_ver = int(getattr(args, "json_version", CHECKRIP_JSON_SCHEMA_VERSION))
                rips_json = [_item_json(item) for item in rendered]
                if schema_ver == 2:
                    payload = {
                        "schema_version": 2,
                        "active": bool(rendered),
                        "rips": rips_json,
                        "drives": drive_summary,
                        "watchers": watchers,
                    }
                else:
                    payload = {
                        "schema_version": 1,
                        "active": bool(rendered),
                    }
                    # Preserve legacy flat single-rip fields consistently regardless of
                    # whether hardware discovery returned 0, 1, or N drive rows.
                    if len(rips_json) == 1 and not watchers:
                        payload.update(rips_json[0])
                    payload["rips"] = rips_json
                    payload["drives"] = drive_summary
                    if watchers:
                        payload["watchers"] = watchers
                print(json.dumps(payload, indent=2))
            else:
                if watchers:
                    for w in watchers:
                        season_val = w.get("season") or ""
                        s_txt = f" S{int(season_val):02d}" if season_val.isdigit() else ""
                        show_name = w.get("show") or "TV"
                        print(f"Watch daemon active (pid {w['pid']}): {show_name}{s_txt}")
                if not rendered and not drive_rows:
                    print("  (watch daemon idle — waiting for disc insertion)")
                    return 0

                total_drives = len(drive_rows)
                active_count = len(rendered)
                print(f"=== Optical Drives ({active_count}/{total_drives} ripping) ===")
                for idx, row in enumerate(drive_rows):
                    d = row["drive"]
                    item = row["rip"]
                    slot = d["slot"]
                    if item is not None:
                        dtag = _drive_tag(item, d)
                        content = _content_label(item, d)
                        stage_txt = item["stage"]
                        print(f"  Drive {slot}  {dtag}  {stage_txt:<9}  {content}")
                        prog = item["progress"]
                        stats = []
                        if prog:
                            if prog["titles"]:
                                stats.append(prog["titles"])
                            stats.append(f"{prog['percent']}%")
                            if prog["elapsed"]:
                                stats.append(f"el {prog['elapsed']}")
                            if prog["eta"]:
                                stats.append(f"ETA {prog['eta']}")
                        if item["speed"] is not None:
                            s = item["speed"]
                            stats.append(f"{s:.1f} MB/s ({s * 60 / 1000:.2f} GB/min)")
                        elif item["detail"]:
                            stats.append(item["detail"])
                        else:
                            stats.append("no active staging write yet")
                        stats.append(f"pid {item['pid']}")
                        joined_stats = "  |  ".join(stats)
                        print(f"           {joined_stats}")
                        if prog and prog["bar"]:
                            print(f"           {prog['bar']}")
                        if args.verbose and item["tail"]:
                            print("           --- log ---")
                            for ln in item["tail"]:
                                print(f"           {ln}")
                    else:
                        dev = d.get("device") or "(empty)"
                        vol = d.get("volume")
                        vol_txt = f"[{vol}] " if vol else ""
                        if d.get("device") and d["device"] in claim_devs:
                            cpid = claim_devs[d["device"]]
                            print(f"  Drive {slot}  {dev}  STARTING   {vol_txt}(probing / waiting, pid {cpid})")
                        elif d.get("media_type"):
                            print(f"  Drive {slot}  {dev}  READY      {vol_txt}(disc inserted, not ripping)")
                        else:
                            print(f"  Drive {slot}  (empty)     IDLE       No disc inserted")
                    if idx < len(drive_rows) - 1:
                        print()
            return 0
        args.directory = latest_rip_dir()
        if args.directory is None:
            print(
                "checkrip: no active rip and no rip directories found; "
                "pass a directory explicitly",
                file=sys.stderr,
            )
            return 2
        if not args.json:
            print(
                f"checkrip: no active rip; checking most recent: {args.directory}",
                file=sys.stderr,
            )

    if not os.path.isdir(args.directory):
        print(f"checkrip: not a directory: {args.directory}", file=sys.stderr)
        return 2

    entries = scan(args.directory)
    if not entries:
        print(f"checkrip: no video files in {args.directory}", file=sys.stderr)
        return 1

    result = analyze(entries, args.expected)

    if args.json:
        out = {**result}
        out["unreadable"] = [
            {
                "name": u["name"],
                "episode": u["episode"],
                "duration_s": u["duration"],
                "size": u["size"],
                "reason": u["reason"],
            }
            for u in result["unreadable"]
        ]
        out["files"] = [
            {
                "name": e["name"],
                "episode": e["episode"],
                "duration_s": e["duration"],
                "size": e["size"],
            }
            for e in entries
        ]
        print(json.dumps(out, indent=2))
        ok = not (
            result["unreadable"]
            or result["blobs"]
            or result["gaps"]
            or result["dupes"]
            or result["missing_vs_expected"]
        )
        return 0 if ok else 1

    unreadable_by_path = {u["path"]: u["reason"] for u in result["unreadable"]}
    print(
        f"\n{os.path.basename(args.directory.rstrip('/'))}  "
        f"({len(entries)} files, median {result['median_min']} min)\n"
    )
    for e in entries:
        tag = ""
        if e["path"] in unreadable_by_path:
            tag = f"  <-- UNREADABLE ({unreadable_by_path[e['path']]})"
        elif any(b["path"] == e["path"] for b in result["blobs"]):
            est = next(
                b["est_episodes"]
                for b in result["blobs"]
                if b["path"] == e["path"]
            )
            tag = f"  <-- PLAY-ALL BLOB (~{est} episodes)"
        print(
            f"  {human_min(e['duration'])}  {human_gb(e['size'])}  {e['name']}{tag}"
        )

    print()
    ok = True
    if result["unreadable"]:
        ok = False
        print(
            f"  UNREADABLE: {len(result['unreadable'])} empty or failed-probe file(s):"
        )
        for u in result["unreadable"]:
            print(f"    - {u['name']}: {u['reason']}")
    if result["blobs"]:
        ok = False
        print(
            f"  BLOBS: {len(result['blobs'])} oversized file(s) — split out real "
            f"episodes, then delete the blob."
        )
    if result["dupes"]:
        ok = False
        for a, b, why in result["dupes"]:
            print(f"  DUPLICATE: '{a}' and '{b}' ({why}).")
    if result["gaps"]:
        ok = False
        print(f"  GAPS: missing episode number(s): {result['gaps']}")
    if result["missing_vs_expected"]:
        ok = False
        n = result["missing_vs_expected"]
        if n > 0:
            print(
                f"  COVERAGE: {result['real_count']}/{result['expected']} "
                f"episodes present — {n} short."
            )
        else:
            print(
                f"  COVERAGE: {result['real_count']}/{result['expected']} "
                f"episodes — {-n} MORE than expected (extra titles?)."
            )
    elif result["expected"]:
        print(
            f"  COVERAGE: {result['real_count']}/{result['expected']} episodes — OK."
        )

    if ok:
        print("  OK — no blobs, gaps, or duplicates detected.")
    print()
    return 0 if ok else 1


def cli_main() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli_main()
