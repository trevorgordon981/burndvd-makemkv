#!/usr/bin/env python3
"""checkrip — inspect active rips or verify a finished rip directory.

Read-only. Never moves or deletes files; reports play-all blobs, numbering gaps,
and content-verified duplicates.
"""

from __future__ import annotations

import argparse
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


def probe_duration(path: str) -> float | None:
    """Return duration in seconds (float) or None if ffprobe fails."""
    try:
        out = subprocess.run(
            [
                FFPROBE,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return float(out.stdout.strip())
    except (ValueError, OSError, subprocess.SubprocessError):
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
        entries.append(
            {
                "path": f,
                "name": os.path.basename(f),
                "season": int(m.group(1)) if m else None,
                "episode": int(m.group(2)) if m else None,
                "size": os.path.getsize(f),
                "duration": probe_duration(f),
            }
        )
    return entries


def analyze(entries: list[dict], expected: int | None = None) -> dict:
    durs = [e["duration"] for e in entries if e["duration"]]
    median = statistics.median(durs) if durs else None

    blobs, dupes, gaps = [], [], []
    if median:
        for e in entries:
            d = e["duration"]
            if d and d > median * BLOB_FACTOR:
                est = round(d / median)
                blobs.append({**e, "est_episodes": est})

    blob_paths = {b["path"] for b in blobs}
    singles = [e for e in entries if e["path"] not in blob_paths]

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


def active_rips(procs=None) -> list[dict]:
    """Detect all active rips via running ripqueue.py / makemkvcon processes."""
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
            for tok in reversed(argv[1:]):
                if "/staging/" in tok:
                    staging = tok
                    break
            makemkvs.append(
                {"pid": pid, "ppid": ppid, "argv": argv, "staging": staging}
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
        elif (
            exe.startswith("python")
            and any(
                os.path.basename(a) in ("ripqueue.py", "burndvd-queue")
                for a in argv[1:]
            )
        ) or exe in ("ripqueue.py", "burndvd-queue"):
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

        staging = None
        for mk in makemkvs:
            if _is_ancestor(rq_pid, mk["pid"], ppid_map):
                staging = mk["staging"]
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
                    used_mk.add(mk["pid"])
                    break
        if staging is None:
            staging = _staging_for_pid(rq_pid)

        rips.append(
            {
                "pid": rq_pid,
                "state": rq_state,
                "queue": rq["queue"],
                "device": rq["device"],
                "log_path": log_path,
                "staging": staging,
            }
        )

    if not rips:
        for mk in makemkvs:
            if mk["staging"]:
                rips.append(
                    {
                        "pid": mk["pid"],
                        "state": None,
                        "queue": None,
                        "device": None,
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


def measure_rip_speeds(stagings: list[str | None], secs: float = 6.0) -> dict[str, float | None]:
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
    args = ap.parse_args()

    if args.directory is None:
        rips = active_rips()
        if rips:
            speeds = measure_rip_speeds([r["staging"] for r in rips])
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
                tail = active_rip_tail(log_path) if log_path else []
                speed = speeds.get(r["staging"]) if r["staging"] else None
                rendered.append(
                    {
                        "pid": r["pid"],
                        "device": r["device"],
                        "staging": r["staging"],
                        "log_path": log_path,
                        "speed": speed,
                        "tail": tail,
                    }
                )

            if args.json:
                if len(rendered) == 1:
                    one = rendered[0]
                    print(
                        json.dumps(
                            {
                                "active": True,
                                "pid": one["pid"],
                                "device": one["device"],
                                "staging": one["staging"],
                                "log_path": one["log_path"],
                                "speed_mbps": round(one["speed"], 1)
                                if one["speed"] is not None
                                else None,
                                "log_tail": [
                                    ANSI_RE.sub("", ln) for ln in one["tail"]
                                ],
                            },
                            indent=2,
                        )
                    )
                else:
                    print(
                        json.dumps(
                            {
                                "active": True,
                                "rips": [
                                    {
                                        "pid": item["pid"],
                                        "device": item["device"],
                                        "staging": item["staging"],
                                        "log_path": item["log_path"],
                                        "speed_mbps": round(item["speed"], 1)
                                        if item["speed"] is not None
                                        else None,
                                        "log_tail": [
                                            ANSI_RE.sub("", ln)
                                            for ln in item["tail"]
                                        ],
                                    }
                                    for item in rendered
                                ],
                            },
                            indent=2,
                        )
                    )
            else:
                for idx, item in enumerate(rendered, 1):
                    if len(rendered) > 1:
                        dev = item["device"] or "unknown-device"
                        print(
                            f"=== Active rip [{idx}/{len(rendered)}]: {dev} (pid {item['pid']}) ==="
                        )
                    if item["tail"]:
                        print("\n".join(item["tail"]))
                    else:
                        dev = item["device"] or "active"
                        print(
                            f"  rip active ({dev}, pid {item['pid']}) — no log output yet"
                        )
                    if item["speed"] is not None:
                        s = item["speed"]
                        print(
                            f"\n  rip speed: {s:.1f} MB/s  ({s * 60 / 1000:.2f} GB/min)"
                        )
                    elif item["staging"] is None:
                        print(
                            "\n  (between titles / probing / transferring — no active staging write)"
                        )
                    if idx < len(rendered):
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
            result["blobs"]
            or result["gaps"]
            or result["dupes"]
            or result["missing_vs_expected"]
        )
        return 0 if ok else 1

    print(
        f"\n{os.path.basename(args.directory.rstrip('/'))}  "
        f"({len(entries)} files, median {result['median_min']} min)\n"
    )
    for e in entries:
        tag = ""
        if any(b["path"] == e["path"] for b in result["blobs"]):
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
