#!/usr/bin/env python3
"""stoprip — safely freeze, rescue completed staged MKVs, and SIGKILL a targeted rip chain."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

_REPO_SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", ".."))
if os.path.isdir(os.path.join(_REPO_SRC, "burndvd")) and _REPO_SRC not in sys.path:
    sys.path.insert(0, _REPO_SRC)

from burndvd.config import LOCAL_STAGING_BASE, NFS_MEDIA_PREFIX, SMB_MEDIA_PREFIX

DEFAULT_NAS_ROOT = Path(NFS_MEDIA_PREFIX)
DEFAULT_VOL_ROOT = Path(SMB_MEDIA_PREFIX)
STAGING_BASE = LOCAL_STAGING_BASE


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


def proc_argv(pid: int) -> list[str]:
    try:
        out = subprocess.check_output(
            ["ps", "-ww", "-o", "args=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return _split_cmd(out.strip())
    except (OSError, subprocess.CalledProcessError):
        return []


def list_processes() -> list[tuple[int, int, str]]:
    try:
        out = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    procs: list[tuple[int, int, str]] = []
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
    seen: set[int] = set()
    cur = pid
    while cur and cur not in seen:
        if cur == ancestor_pid:
            return True
        seen.add(cur)
        cur = ppid_map.get(cur, 0)
    return False


def _classify_process(argv: list[str]) -> str | None:
    if not argv:
        return None
    exe = os.path.basename(argv[0]).lower()
    if exe == "stoprip" or any(os.path.basename(a) == "stoprip" for a in argv[:2]):
        return None
    if exe == "makemkvcon":
        return "makemkv"
    if exe in ("ripqueue.py", "burndvd-queue"):
        if "--validate-queue" in argv or "--scan" in argv:
            return None
        return "ripqueue"
    if exe.startswith("python"):
        if any(os.path.basename(a) in ("ripqueue.py", "burndvd-queue") for a in argv[1:]):
            if "--validate-queue" in argv or "--scan" in argv:
                return None
            return "ripqueue"
        return None
    if exe == "caffeinate":
        if any(os.path.basename(a) in ("ripqueue.py", "burndvd-queue") for a in argv[1:]):
            return "caffeinate"
        return None
    if exe == "script":
        if any(
            os.path.basename(a) in ("ripqueue.py", "burndvd", "burndvd-wrapper.sh", "burndvd-queue")
            or "burndvd-" in a
            for a in argv[1:]
        ):
            return "script"
        return None
    if exe in ("burndvd", "burndvd-wrapper.sh"):
        return "burndvd"
    if exe in ("bash", "sh", "zsh"):
        if len(argv) >= 2 and os.path.basename(argv[1]) in (
            "burndvd",
            "burndvd-wrapper.sh",
        ):
            return "burndvd"
    return None


def discover_rip_sessions(
    procs: list[tuple[int, int, str]] | None = None
) -> list[dict]:
    """Discover active rip-chain sessions grouped by `--state` and process ancestry."""
    if procs is None:
        procs = list_processes()

    my_pid = os.getpid()
    ppid_map = {pid: ppid for pid, ppid, _ in procs}
    parsed: list[tuple[int, int, list[str], str | None]] = []
    argv_by_pid: dict[int, list[str]] = {}

    for pid, ppid, cmd in procs:
        if pid == my_pid:
            continue
        argv = _split_cmd(cmd)
        argv_by_pid[pid] = argv
        role = _classify_process(argv)
        parsed.append((pid, ppid, argv, role))

    burndvd_pids = {pid for pid, _, _, role in parsed if role == "burndvd"}
    matched: list[tuple[int, int, list[str], str]] = []
    for pid, ppid, argv, role in parsed:
        if role is not None:
            matched.append((pid, ppid, argv, role))
        elif (
            ppid in burndvd_pids
            and argv
            and os.path.basename(argv[0]).lower().startswith("python")
        ):
            if any("select.poll" in a for a in argv[1:]):
                matched.append((pid, ppid, argv, "feeder"))

    if not matched:
        return []

    sessions: list[dict] = []
    by_state: dict[str, dict] = {}
    pid_to_session: dict[int, dict] = {}

    def _new_session(
        key: str,
        state: str | None = None,
        queue: str | None = None,
        device: str | None = None,
    ) -> dict:
        sess = {
            "key": key,
            "state": state,
            "queue": queue,
            "device": device,
            "roles": {},
            "pids": [],
            "argv_by_pid": {},
        }
        sessions.append(sess)
        if state:
            by_state[state] = sess
        return sess

    def _attach(sess: dict, pid: int, role: str, argv: list[str]) -> None:
        sess["roles"].setdefault(role, [])
        if pid not in sess["roles"][role]:
            sess["roles"][role].append(pid)
        if pid not in sess["pids"]:
            sess["pids"].append(pid)
        sess["argv_by_pid"][pid] = argv
        pid_to_session[pid] = sess
        if not sess["state"]:
            st = _extract_flag(argv, "--state")
            if st:
                sess["state"] = st
                by_state[st] = sess
        if not sess["queue"]:
            q = _extract_flag(argv, "--queue")
            if q:
                sess["queue"] = q
        if not sess["device"]:
            d = _extract_flag(argv, "--device")
            if d:
                sess["device"] = d

    for pid, ppid, argv, role in matched:
        if role != "ripqueue":
            continue
        state = _extract_flag(argv, "--state")
        queue = _extract_flag(argv, "--queue")
        device = _extract_flag(argv, "--device")
        sess = by_state.get(state) if state else None
        if sess is None:
            sess = _new_session(
                state or f"ripqueue:{pid}", state=state, queue=queue, device=device
            )
        _attach(sess, pid, role, argv)

    remaining: list[tuple[int, int, list[str], str]] = []
    for pid, ppid, argv, role in matched:
        if pid in pid_to_session:
            continue
        state = _extract_flag(argv, "--state")
        if state and state in by_state:
            _attach(by_state[state], pid, role, argv)
            continue
        attached = False
        for sess in sessions:
            for spid in list(sess["pids"]):
                if _is_ancestor(spid, pid, ppid_map) or _is_ancestor(
                    pid, spid, ppid_map
                ):
                    _attach(sess, pid, role, argv)
                    attached = True
                    break
            if attached:
                break
        if attached:
            continue
        if role == "makemkv":
            for tok in reversed(argv[1:]):
                if "/staging/" in tok:
                    for sess in sessions:
                        for rq_pid in sess["roles"].get("ripqueue", []):
                            if tok.endswith(f"-{rq_pid}"):
                                _attach(sess, pid, role, argv)
                                attached = True
                                break
                        if attached:
                            break
                    break
        if not attached:
            remaining.append((pid, ppid, argv, role))

    for pid, ppid, argv, role in remaining:
        state = _extract_flag(argv, "--state")
        sess = by_state.get(state) if state else None
        if sess is None:
            for existing in sessions:
                if any(
                    _is_ancestor(spid, pid, ppid_map)
                    or _is_ancestor(pid, spid, ppid_map)
                    for spid in existing["pids"]
                ):
                    sess = existing
                    break
        if sess is None:
            sess = _new_session(
                state or f"{role}:{pid}",
                state=state,
                queue=_extract_flag(argv, "--queue"),
                device=_extract_flag(argv, "--device"),
            )
        _attach(sess, pid, role, argv)

    for sess in sessions:
        sess["pids"].sort()
    return sessions


def find_rip_pids(
    procs: list[tuple[int, int, str]] | None = None
) -> dict[str, list[int]]:
    roles: dict[str, list[int]] = {}
    for sess in discover_rip_sessions(procs):
        for role, pids in sess["roles"].items():
            roles.setdefault(role, [])
            for p in pids:
                if p not in roles[role]:
                    roles[role].append(p)
    return roles


def signal_all(pids: list[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def locate_staging(
    session: dict, staging_base: Path = STAGING_BASE
) -> Path | None:
    argv_by_pid = session.get("argv_by_pid", {})
    for mk_pid in session.get("roles", {}).get("makemkv", []):
        argv = argv_by_pid.get(mk_pid) or proc_argv(mk_pid)
        for tok in reversed(argv[1:]):
            if "/staging/" in tok:
                cand = Path(tok)
                if cand.is_dir():
                    return cand
        if argv:
            cand = Path(argv[-1])
            if cand.is_dir() and (
                cand.parent == staging_base or "/staging/" in str(cand)
            ):
                return cand

    if staging_base.is_dir():
        for rq_pid in session.get("roles", {}).get("ripqueue", []):
            suffix = f"-{rq_pid}"
            matches = [
                d
                for d in staging_base.iterdir()
                if d.is_dir() and d.name.endswith(suffix)
            ]
            if matches:
                matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return matches[0]
    return None


def locate_queue_state(session: dict) -> tuple[Path | None, Path | None]:
    queue = Path(session["queue"]) if session.get("queue") else None
    state = Path(session["state"]) if session.get("state") else None
    if queue and state:
        return queue, state
    for rq_pid in session.get("roles", {}).get("ripqueue", []):
        argv = session.get("argv_by_pid", {}).get(rq_pid) or proc_argv(rq_pid)
        q = _extract_flag(argv, "--queue")
        s = _extract_flag(argv, "--state")
        if q and not queue:
            queue = Path(q)
        if s and not state:
            state = Path(s)
    return queue, state


def current_item(queue: Path | None, state: Path | None) -> dict | None:
    if not queue or not state:
        return None
    try:
        idx = json.loads(state.read_text()).get("current_index", 0)
        with queue.open() as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, json.JSONDecodeError):
        return None
    if 0 <= idx < len(rows):
        return rows[idx]
    return None


def resolve_nas_path(
    raw_path: str | Path,
    nas_root: Path = DEFAULT_NAS_ROOT,
    vol_root: Path = DEFAULT_VOL_ROOT,
) -> Path:
    p = Path(raw_path)
    try:
        rel = p.relative_to(vol_root)
    except ValueError:
        return p
    if nas_root.is_dir() or not (vol_root.is_dir() and os.access(vol_root, os.W_OK)):
        return nas_root / rel
    return p


def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()


def _open_write_paths(pids: list[int]) -> set[Path]:
    opened: set[Path] = set()
    if not pids:
        return opened
    lsof_bin = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        out = subprocess.check_output(
            [lsof_bin, "-Ffan", "-p", ",".join(str(p) for p in pids)],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return opened
    cur_write = False
    for line in out.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            cur_write = False
        elif tag == "f":
            # Linux lsof may append w/u to numeric fd; avoid false match on "cwd".
            cur_write = bool(re.match(r"^\d+[wu]$", val))
        elif tag == "a":
            # macOS and Linux lsof emit explicit access mode in `a` (`r`, `w`, `u`).
            cur_write = ("w" in val) or ("u" in val)
        elif tag == "n" and cur_write and val.startswith("/"):
            try:
                opened.add(Path(val).resolve())
            except OSError:
                opened.add(Path(val))
    return opened

def _move_with_backup(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        bak = dest.with_suffix(dest.suffix + f".pre-stoprip-{int(time.time())}")
        dest.rename(bak)
        print(f"stoprip: existing target moved aside → {bak.name}")
    size_gb = src.stat().st_size / 1e9
    print(f"stoprip: rescue {src.name} ({size_gb:.1f} GB)")
    print(f"      →  {dest}")
    shutil.move(str(src), str(dest))
    return dest


def _rescue_quarantine_dir(
    item: dict,
    staging: Path,
    nas_root: Path = DEFAULT_NAS_ROOT,
    vol_root: Path = DEFAULT_VOL_ROOT,
) -> Path:
    """Derive an out-of-library quarantine directory for rescued unverified files."""
    base_dir = resolve_nas_path(
        item["target_root"], nas_root=nas_root, vol_root=vol_root
    )
    quarantine_root: Path | None = None
    for root in (nas_root, vol_root):
        try:
            base_dir.relative_to(root)
            quarantine_root = root / ".repair-quarantine" / "stoprip-rescue"
            break
        except ValueError:
            continue
    if quarantine_root is None:
        quarantine_root = base_dir.parent / ".repair-quarantine" / "stoprip-rescue"

    title_part = sanitize(str(item.get("title") or "unknown")) or "unknown"
    dest = quarantine_root / title_part
    if item.get("type") == "tv-season":
        season_raw = str(item.get("season") or "").strip()
        try:
            season_num = int(season_raw)
        except ValueError:
            season_num = 0
        if season_num > 0:
            dest = dest / f"Season {season_num:02d}"
    return dest / staging.name


def rescue_staging(
    staging: Path | None,
    queue: Path | None,
    state: Path | None,
    session: dict | None = None,
    nas_root: Path = DEFAULT_NAS_ROOT,
    vol_root: Path = DEFAULT_VOL_ROOT,
) -> list[Path]:
    if not staging or not staging.is_dir():
        print("stoprip: cannot find staging dir; skipping rescue")
        return []

    mkvs = sorted(
        (
            p
            for p in staging.rglob("*")
            if p.is_file() and p.suffix.lower() == ".mkv" and p.stat().st_size > 0
        ),
        key=lambda p: p.relative_to(staging).as_posix(),
    )
    if not mkvs:
        print(f"stoprip: no non-empty .mkv in {staging}; skipping rescue")
        return []

    writer_pids: set[int] = set()
    if session:
        writer_pids.update(session.get("pids") or [])
        roles = session.get("roles") or {}
        for role_pids in roles.values():
            if isinstance(role_pids, list):
                writer_pids.update(role_pids)
    in_progress = _open_write_paths(sorted(writer_pids))

    completed = [p for p in mkvs if p.resolve() not in in_progress]
    if not completed:
        print(
            f"stoprip: no completed .mkv in {staging} (all open for writing); preserving staging"
        )
        return []

    item = current_item(queue, state)
    if not item:
        print(
            f"stoprip: queue/state unavailable; preserving {len(completed)} unverified "
            f".mkv file(s) in staging {staging} (never published to live library)"
        )
        return []

    quarantine_dir = _rescue_quarantine_dir(
        item, staging, nas_root=nas_root, vol_root=vol_root
    )
    rescued: list[Path] = []
    for mkv in completed:
        rel = mkv.relative_to(staging)
        rescued.append(_move_with_backup(mkv, quarantine_dir / rel))
    return rescued


def _staging_has_remaining_files(staging: Path) -> bool:
    return any(p.is_file() for p in staging.rglob("*"))


def stop_session(
    session: dict,
    no_save: bool = False,
    staging_base: Path = STAGING_BASE,
    nas_root: Path = DEFAULT_NAS_ROOT,
    vol_root: Path = DEFAULT_VOL_ROOT,
    kill_fn=signal_all,
    sleep_fn=time.sleep,
) -> list[Path]:
    pids = list(session["pids"])
    print(
        f"stoprip: targeting {len(pids)} rip-chain procs "
        f"({session.get('device') or session['key']}): {session['roles']}"
    )

    kill_fn(pids, signal.SIGSTOP)
    staging: Path | None = None
    rescued: list[Path] = []
    rescue_completed = False
    try:
        sleep_fn(0.5)
        staging = locate_staging(session, staging_base=staging_base)
        queue, state = locate_queue_state(session)
        if not no_save:
            rescued = rescue_staging(
                staging,
                queue,
                state,
                session=session,
                nas_root=nas_root,
                vol_root=vol_root,
            )
        rescue_completed = True
    finally:
        kill_fn(pids, signal.SIGKILL)

    sleep_fn(1.0)

    if rescue_completed and staging and staging.is_dir():
        if no_save:
            shutil.rmtree(staging, ignore_errors=True)
            print(f"stoprip: removed staging {staging} (--no-save)")
        elif not _staging_has_remaining_files(staging):
            shutil.rmtree(staging, ignore_errors=True)
            print(f"stoprip: removed empty staging {staging}")
        else:
            print(
                f"stoprip: preserving staging {staging} (unresolved/in-progress files remain)"
            )

    for r in rescued:
        print(f"stoprip: rescued (quarantine) {r}")
    return rescued

def _select_sessions(sessions: list[dict], args) -> list[dict] | None:
    if args.all:
        return sessions
    filtered = sessions
    if args.device:
        want = args.device if args.device.startswith("disc:") else f"disc:{args.device}"
        filtered = [s for s in filtered if s.get("device") == want]
    if args.state:
        filtered = [s for s in filtered if s.get("state") == args.state]
    if args.pid is not None:
        filtered = [s for s in filtered if args.pid in s["pids"]]

    if not filtered:
        print("stoprip: no matching rip session found")
        return None
    if len(filtered) > 1:
        print(
            f"stoprip: {len(filtered)} active rip sessions; specify --device, --state, --pid, or --all:"
        )
        for s in filtered:
            print(
                f"  device={s.get('device') or '?'}  pids={s['pids']}  state={s.get('state') or '?'}"
            )
        return None
    return filtered


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--no-save",
        action="store_true",
        help="kill and discard staging without rescuing completed titles",
    )
    ap.add_argument(
        "--skip-current",
        action="store_true",
        help="advance past the current queue item via SIGUSR1",
    )
    ap.add_argument(
        "--device",
        default=None,
        help="target a specific drive (e.g. disc:0 or 0) when multiple rips run",
    )
    ap.add_argument("--state", default=None, help="target a specific --state path")
    ap.add_argument(
        "--pid", type=int, default=None, help="target the session containing this PID"
    )
    ap.add_argument("--all", action="store_true", help="target all active sessions")
    args = ap.parse_args()

    sessions = discover_rip_sessions()
    if not sessions:
        print("stoprip: no rip in flight")
        return 1

    targets = _select_sessions(sessions, args)
    if not targets:
        return 1

    if args.skip_current:
        rc = 0
        for sess in targets:
            rq_pids = sess["roles"].get("ripqueue", [])
            if not rq_pids:
                print(f"stoprip: no ripqueue.py process to signal in {sess['key']}")
                rc = 1
                continue
            signal_all(sess["roles"].get("makemkv", []), signal.SIGKILL)
            for rq_pid in rq_pids:
                try:
                    os.kill(rq_pid, signal.SIGUSR1)
                    print(f"stoprip: signaled ripqueue pid={rq_pid} to skip current item")
                except ProcessLookupError:
                    print(f"stoprip: ripqueue pid={rq_pid} disappeared")
                    rc = 1
        return rc

    for sess in targets:
        stop_session(sess, no_save=args.no_save)
    return 0


def cli_main() -> None:
    sys.exit(main())


if __name__ == "__main__":
    cli_main()
