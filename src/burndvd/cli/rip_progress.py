#!/usr/bin/env python3
"""rip-progress — report throughput & ETA of an in-flight staging -> NAS transfer."""

from __future__ import annotations

import csv
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

_REPO_SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", ".."))
if os.path.isdir(os.path.join(_REPO_SRC, "burndvd")) and _REPO_SRC not in sys.path:
    sys.path.insert(0, _REPO_SRC)

from burndvd.config import LOCAL_STAGING_BASE, NFS_MEDIA_PREFIX, SMB_MEDIA_PREFIX

STAGING = LOCAL_STAGING_BASE
MEDIA_BASES = (Path(NFS_MEDIA_PREFIX), Path(SMB_MEDIA_PREFIX))
MEDIA_SUBDIRS = ("Movies 4K", "Movies", "TV Shows 4K", "TV Shows")
SKIP_DIRS = {"#recycle", "@eaDir", "Extras"}
MAX_WALK_DEPTH = 4


def _active_media_base() -> Path:
    for base in MEDIA_BASES:
        if (base / "TV Shows").is_dir() or base.is_dir():
            return base
    return MEDIA_BASES[-1]


def _default_target_roots() -> list[Path]:
    base = _active_media_base()
    return [
        base / ".repair-quarantine" / ".burndvd-partials",
        *(base / sub for sub in MEDIA_SUBDIRS),
    ]


TARGET_ROOTS = _default_target_roots()
STATE_FILE = Path.home() / ".cache" / "burndvd" / "rip-progress-state.json"
RECENT_TARGET_MAX_AGE = 120
STALE_STAGING_MIN_AGE = 10


def normalize_nas_path(
    p: Path,
    nas_base: Path = Path(NFS_MEDIA_PREFIX),
    vol_base: Path = Path(SMB_MEDIA_PREFIX),
) -> Path:
    try:
        rel = p.relative_to(vol_base)
    except ValueError:
        return p
    if nas_base.is_dir():
        return nas_base / rel
    return p


def _extract_flag(argv: list[str], flag: str) -> str | None:
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def active_ripqueue_target_dirs(
    ps_lines: list[str] | None = None
) -> list[Path]:
    if ps_lines is None:
        try:
            out = subprocess.check_output(
                ["ps", "-axo", "command="],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            ps_lines = out.splitlines()
        except (OSError, subprocess.SubprocessError):
            return []

    dirs: list[Path] = []
    seen: set[Path] = set()

    def _add(d: Path) -> None:
        nd = normalize_nas_path(d)
        if nd not in seen and nd.is_dir():
            seen.add(nd)
            dirs.append(nd)

    found_rq = False
    for line in ps_lines:
        try:
            argv = shlex.split(line)
        except ValueError:
            argv = line.split()
        if not argv:
            continue
        exe = os.path.basename(argv[0]).lower()
        is_rq = (
            exe.startswith("python")
            and any(
                os.path.basename(a) in ("ripqueue.py", "burndvd-queue")
                for a in argv[1:]
            )
        ) or exe in ("ripqueue.py", "burndvd-queue")
        if not is_rq or "--validate-queue" in argv or "--scan" in argv:
            continue
        found_rq = True
        state_p = _extract_flag(argv, "--state")
        queue_p = _extract_flag(argv, "--queue")
        items = []
        if state_p and os.path.isfile(state_p):
            try:
                data = json.loads(Path(state_p).read_text())
                items.extend(data.get("queue", []))
            except (OSError, json.JSONDecodeError):
                pass
        if not items and queue_p and os.path.isfile(queue_p):
            try:
                with open(queue_p) as fh:
                    items.extend(csv.DictReader(fh))
            except OSError:
                pass
        for item in items:
            tr = item.get("target_root")
            if not tr:
                continue
            root = normalize_nas_path(Path(tr))
            if item.get("type") == "tv-season":
                try:
                    s = int(str(item.get("season") or "").strip())
                except ValueError:
                    s = 0
                if s > 0:
                    _add(root / f"Season {s:02d}")
            _add(root)

    if found_rq:
        base = _active_media_base()
        _add(base / ".repair-quarantine" / ".burndvd-partials")
        _add(base / ".repair-quarantine" / "burndvd-review")
    return dirs


def _is_candidate_target(name: str) -> bool:
    low = name.lower()
    if low.endswith(".mkv"):
        return True
    if name.startswith(".") and low.endswith(".partial"):
        return True
    return False


def _iter_target_files(root: Path, max_depth: int = MAX_WALK_DEPTH):
    if not root.is_dir():
        return
    base_depth = str(root).rstrip("/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath.rstrip("/").count(os.sep) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if _is_candidate_target(fn):
                yield Path(dirpath) / fn


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PB"


def human_duration(s: float) -> str:
    if s < 0 or s != s:
        return "?"
    if s < 60:
        return f"{s:.0f}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m:.0f}m{s:02.0f}s"
    h, m = divmod(m, 60)
    return f"{h:.0f}h{m:02.0f}m"


def find_pair(search_roots: list[Path] | None = None):
    candidates = []
    now = time.time()
    if not STAGING.exists():
        return None
    for sub in STAGING.iterdir():
        if not sub.is_dir():
            continue
        for mkv in sub.glob("*.mkv"):
            try:
                st = mkv.stat()
            except OSError:
                continue
            age = now - st.st_mtime
            if age < STALE_STAGING_MIN_AGE:
                continue
            candidates.append(mkv)
    if not candidates:
        return None

    if search_roots is None:
        scoped = active_ripqueue_target_dirs()
        roots = scoped if scoped else TARGET_ROOTS
    else:
        roots = search_roots

    for src in sorted(candidates, key=lambda p: -p.stat().st_size):
        try:
            src_size = src.stat().st_size
        except OSError:
            continue
        if src_size == 0:
            continue
        best_dst = None
        best_dst_mtime = 0.0
        for root in roots:
            if not root.exists():
                continue
            for dst in _iter_target_files(root):
                try:
                    st = dst.stat()
                except OSError:
                    continue
                if st.st_size > src_size:
                    continue
                if (now - st.st_mtime) > RECENT_TARGET_MAX_AGE:
                    continue
                if st.st_mtime > best_dst_mtime:
                    best_dst = dst
                    best_dst_mtime = st.st_mtime
        if best_dst is not None:
            return src, best_dst
    return None


def load_prev_sample(key: str):
    if not STATE_FILE.exists():
        return None
    try:
        d = json.loads(STATE_FILE.read_text())
        return d.get(key)
    except (json.JSONDecodeError, OSError):
        return None


def save_sample(key: str, sample: dict) -> None:
    try:
        d = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    except (json.JSONDecodeError, OSError):
        d = {}
    d[key] = sample
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(d))


def main() -> int:
    pair = find_pair()
    if pair is None:
        print("no in-flight rip upload detected", file=sys.stderr)
        return 1
    src, dst = pair
    src_size = src.stat().st_size
    dst_size = dst.stat().st_size
    pct = 100.0 * dst_size / src_size if src_size > 0 else 0.0

    key = str(dst)
    now = time.time()
    prev = load_prev_sample(key)
    rate_str = "?"
    eta_str = "?"
    if prev:
        dt = now - prev["t"]
        dsize = dst_size - prev["size"]
        if dt > 0.5 and dsize > 0:
            mbps = dsize / dt / (1024 * 1024)
            remaining = src_size - dst_size
            eta = remaining / (dsize / dt) if dsize > 0 else float("inf")
            rate_str = f"{mbps:.1f} MB/s"
            eta_str = human_duration(eta)
    save_sample(key, {"t": now, "size": dst_size})

    print(f"src: {src.name}")
    print(f"dst: {dst}")
    print(
        f"     {human_bytes(dst_size)} / {human_bytes(src_size)}  "
        f"({pct:.1f}%)  rate={rate_str}  eta={eta_str}"
    )
    return 0


def cli_main() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli_main()
