#!/usr/bin/env python3
"""ripqueue.py — guided MakeMKV ripping queue for a media library buildout.

Usage:
  caffeinate -dimsu burndvd --queue queue.csv [--state ~/ripqueue-state.json]
                            [--device disc:0] [--makemkvcon /path/to/makemkvcon]
                            [--overwrite] [--verify] [--min-free-gb 200]
                            [--no-eject] [--no-sound]

The `caffeinate -dimsu` wrapper is strongly recommended: macOS sleeping
mid-rip kills makemkvcon, and BU40N USB drives can drop off the bus under
power management.

Companion to ~/scripts/rip-disc.sh (manual interactive ripper). This driver
reads a pre-loaded queue.csv and auto-names per Plex/Jellyfin convention.
"""
from __future__ import annotations
import argparse, csv, json, os, queue, re, shutil, subprocess, sys, threading, time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_MAKEMKVCON = "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon"

# Local staging keeps in-flight files off NAS (forum recommendation: avoid
# direct-to-SMB rips; fragmentation + Jellyfin scan races during rip).
LOCAL_STAGING_BASE = Path.home() / ".cache" / "burndvd" / "staging"

# -------- presentation --------
class C:
    R="\033[0m"; B="\033[1m"; D="\033[2m"
    RED="\033[31m"; GRN="\033[32m"; YLW="\033[33m"
    BLU="\033[34m"; MAG="\033[35m"; CYA="\033[36m"

def fmt_dur(s: float) -> str:
    if s <= 0: return "--:--"
    s = int(s); h = s//3600; m = (s%3600)//60; ss = s%60
    return f"{h}h{m:02d}m" if h else f"{m}m{ss:02d}s"

def render_bar(frac: float, width: int = 30) -> str:
    n = max(0, min(width, int(frac * width)))
    return "[" + "#"*n + "-"*(width-n) + "]"

def sanitize(s: str) -> str:
    s = re.sub(r"[\\/:\*\?\"<>\|]", "", s)
    return re.sub(r"\s+", " ", s).strip()

def csv_int(row: dict, key: str, default: int) -> int:
    raw = (row.get(key) or "").strip()
    return int(raw) if raw else default

def move_with_progress(src: Path, dst: Path, label: str = "moving",
                       interval: float = 5.0) -> None:
    """shutil.move with periodic progress lines.

    Cross-filesystem moves copy then delete; on a gigabit NAS that means
    multi-minute silence with no feedback. We run the move in a worker
    thread and poll dst size from the main thread.
    """
    total = src.stat().st_size
    done_evt = threading.Event()
    err_box: list[BaseException] = []

    def worker():
        try:
            shutil.move(str(src), str(dst))
        except BaseException as e:
            err_box.append(e)
        finally:
            done_evt.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    start = time.time()
    last_size = 0
    last_t = start
    print(f"{C.D}{label}: {src.name} -> {dst}{C.R}", flush=True)

    while not done_evt.wait(timeout=interval):
        try:
            cur = dst.stat().st_size if dst.exists() else 0
        except FileNotFoundError:
            cur = 0
        now = time.time()
        frac = cur / total if total > 0 else 0.0
        dt = now - last_t
        delta = cur - last_size
        mbps = delta / dt / (1024 * 1024) if dt > 0 else 0.0
        eta = (total - cur) / (delta / dt) if delta > 0 and dt > 0 else 0
        bar = render_bar(frac)
        elapsed = fmt_dur(now - start)
        print(f"  {bar} {frac*100:5.1f}%  {cur/1e9:5.2f}/{total/1e9:5.2f} GB"
              f"  {mbps:5.1f} MB/s  eta {fmt_dur(eta)}  elapsed {elapsed}",
              flush=True)
        last_size = cur
        last_t = now

    t.join()
    if err_box:
        raise err_box[0]
    elapsed = time.time() - start
    avg_mbps = total / elapsed / (1024 * 1024) if elapsed > 0 else 0.0
    print(f"  {C.GRN}done{C.R}  {total/1e9:.2f} GB in {fmt_dur(elapsed)}"
          f"  ({avg_mbps:.1f} MB/s avg)", flush=True)

# -------- queue model --------
@dataclass
class QueueItem:
    title: str
    type: str            # "movie" | "tv-season"
    discs: int
    target_root: str
    format: str          # "4K" | "BD" | "DVD"
    season: int = 0
    episode_start: int = 1
    notes: str = ""

def load_queue(path: Path) -> list[QueueItem]:
    items: list[QueueItem] = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            items.append(QueueItem(
                title=row["title"].strip(),
                type=row["type"].strip(),
                discs=csv_int(row, "discs", 1),
                target_root=row["target_root"].strip(),
                format=row["format"].strip(),
                season=csv_int(row, "season", 0),
                episode_start=csv_int(row, "episode_start", 1),
                notes=(row.get("notes") or "").strip(),
            ))
    return items

# -------- state --------
STATE_VERSION = 2
def default_state(items: list[QueueItem]) -> dict:
    return {
        "version": STATE_VERSION,
        "queue": [asdict(i) for i in items],
        "current_index": 0,
        "disc_index_in_item": 0,
        "disc_episode_counts": {},
        "completed": [],
        "rip_durations_s": [],
        "started_at": time.time(),
    }

def save_state(state: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)

def warn_state_location(state_path: str):
    abs_path = os.path.abspath(state_path)
    if abs_path.startswith("/Volumes/"):
        print(f"{C.YLW}Warning: state file lives on a network volume ({abs_path}).{C.R}")
        print(f"{C.YLW}  os.replace is not atomic across all macOS SMB versions.{C.R}")
        print(f"{C.YLW}  Recommended: --state ~/ripqueue-state.json{C.R}")

def warn_queue_divergence(state: dict, items: list[QueueItem]):
    sq = state.get("queue", [])
    live = [asdict(i) for i in items]
    if len(sq) != len(live):
        print(f"{C.YLW}Warning: queue.csv has {len(live)} rows, state has {len(sq)}. "
              f"Using state's snapshot. Delete the state file to start fresh.{C.R}")
        return
    diffs = []
    for i, (s, l) in enumerate(zip(sq, live)):
        delta = [k for k in l if s.get(k) != l.get(k)]
        if delta:
            diffs.append((i+1, s.get("title"), delta))
    if diffs:
        print(f"{C.YLW}Warning: queue.csv differs from state snapshot in {len(diffs)} row(s):{C.R}")
        for n, title, delta in diffs[:5]:
            print(f"{C.YLW}  row {n} ({title}): {','.join(delta)} (state wins){C.R}")
        print(f"{C.YLW}  Edit ripqueue-state.json or delete it to apply queue.csv changes.{C.R}")

def load_or_init(args, items) -> dict:
    p = Path(args.state)
    if p.exists():
        state = json.loads(p.read_text())
        state.setdefault("disc_episode_counts", {})
        state.pop("next_episode", None)
        # Auto-discard a fully-complete snapshot when the new CSV is a different
        # queue. Otherwise burndvd's single-disc mode would inherit the previous
        # disc's "done" state and skip the new rip entirely.
        prior_done = state.get("current_index", 0) >= len(state.get("queue", []))
        same_queue = state.get("queue", []) == [asdict(i) for i in items]
        if prior_done and not same_queue:
            print(f"{C.YLW}Prior state was fully complete and queue.csv changed; "
                  f"starting fresh.{C.R}")
            return default_state(items)
        print(f"{C.CYA}Resumed{C.R}: position {state['current_index']+1}/{len(state['queue'])}, "
              f"{len(state['completed'])} rips already done.")
        warn_queue_divergence(state, items)
        return state
    print(f"{C.CYA}New run{C.R}: loaded {len(items)} queue items.")
    return default_state(items)

# -------- makemkvcon parsing --------
def parse_line(line: str):
    if ":" not in line: return None, None
    tag, rest = line.split(":", 1)
    out, cur, in_q, i = [], "", False, 0
    while i < len(rest):
        c = rest[i]
        if c == '"':
            if in_q and i+1 < len(rest) and rest[i+1] == '"':
                cur += '"'; i += 2; continue
            in_q = not in_q
        elif c == "," and not in_q:
            out.append(cur); cur = ""
        else:
            cur += c
        i += 1
    out.append(cur)
    return tag, out

def parse_duration(s: str) -> int:
    m = re.match(r"^(\d+):(\d{2}):(\d{2})$", s.strip())
    if not m: return 0
    h, mn, sc = map(int, m.groups()); return h*3600 + mn*60 + sc

def title_num_from_filename(name: str):
    m = re.search(r"_t(\d+)\.mkv$", name)
    return int(m.group(1)) if m else None

# -------- forum-derived guards --------
def parse_save_summary(msgs: list[str]):
    """Returns (saved, failed) from MakeMKV's save-complete MSG, or None if absent.

    makemkvcon exits 0 even when titles fail to save (forum.makemkv.com t=3800).
    The truth is in MSG:5036/5037: 'Copy complete. N titles saved[, M failed].'
    """
    for m in reversed(msgs):
        match = re.search(r'(\d+)\s+titles?\s+saved(?:,\s*(\d+)\s+failed)?', m, re.IGNORECASE)
        if match:
            saved = int(match.group(1))
            failed = int(match.group(2)) if match.group(2) else 0
            return saved, failed
    return None

def stall_timeout_for(format_: str) -> int:
    """Seconds without PRGV total advance before declaring stall."""
    return {"4K": 900, "BD": 720, "DVD": 480}.get((format_ or "").upper(), 720)

def msgs_indicate_aacs_lock(msgs: list[str]) -> bool:
    """Detect AACS v82+ failures distinct from generic 'no titles'."""
    pat = re.compile(r'(newer\s+version.*aacs|aacs.*newer\s+version|aacs.*not\s+available|failed\s+to\s+open\s+disc.*aacs)',
                     re.IGNORECASE)
    return any(pat.search(m) for m in msgs)

def _optical_disk_node() -> str | None:
    """Return the /dev/diskN node of the optical drive per drutil, or None.
    drutil knows the device even when makemkvcon has stopped reporting it."""
    r = subprocess.run(["drutil", "status"], capture_output=True, text=True)
    m = re.search(r"Name:\s+(/dev/disk\d+)", r.stdout or "")
    return m.group(1) if m else None

def _optical_mounts() -> list[str]:
    """Mounted /dev/diskN nodes with optical filesystem types. Catches stale
    mounts that linger after the disc has dropped off (drutil + makemkvcon
    will both report "No Media" while `mount` still shows the volume)."""
    out = subprocess.run(["mount"], capture_output=True, text=True).stdout
    return re.findall(r"^(/dev/disk\d+)\b.*\((?:udf|cd9660)\b", out, re.MULTILINE)

def unmount_blocking_volume(rdisk_path: str | None) -> bool:
    """macOS auto-mounts BD/DVD volumes, which holds the device exclusively and
    makes makemkvcon emit MSG 5010 "Failed to open disc". Unmount the volume
    (keeping the disc in the drive). Returns True if something was actually
    unmounted. Prefers the device makemkvcon reported; falls back to drutil."""
    candidates: list[str] = []
    m = re.match(r"^/dev/r(disk\d+)$", rdisk_path or "")
    if m:
        candidates.append(f"/dev/{m.group(1)}")
    drutil_node = _optical_disk_node()
    if drutil_node and drutil_node not in candidates:
        candidates.append(drutil_node)
    for stale in _optical_mounts():
        if stale not in candidates:
            candidates.append(stale)
    mounts = subprocess.run(["mount"], capture_output=True, text=True).stdout
    unmounted = False
    for disk in candidates:
        if not re.search(rf"^{re.escape(disk)}\b", mounts, re.MULTILINE):
            continue
        for cmd in (["diskutil", "unmount", disk], ["diskutil", "unmount", "force", disk]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                print(f"  {C.D}auto-unmounted {disk} so makemkvcon can open the disc{C.R}")
                unmounted = True
                break
        else:
            print(f"  {C.YLW}diskutil unmount {disk} failed: {r.stderr.strip() or r.stdout.strip()}{C.R}")
    return unmounted

def scan_for_key_expiry(msgs: list[str]):
    """Return days remaining for evaluation key, or None if permanent/unknown."""
    for m in msgs:
        if re.search(r'evaluat', m, re.IGNORECASE):
            match = re.search(r'(\d+)\s+day', m)
            if match: return int(match.group(1))
    return None

def movie_filename(item: QueueItem, disc_n: int) -> str:
    base = sanitize(item.title)
    if item.discs > 1:
        return f"{base} - disc{disc_n}.mkv"
    return f"{base}.mkv"

# -------- probe --------
def probe_disc(args) -> dict | None:
    def _run_once():
        try:
            # TV-show DVDs with 20-40 short titles per disc take longer than
            # the original 120s allowance. Bumped to 300s and cache from 1MB
            # to 128MB so the scan doesn't thrash the drive on title enum.
            p = subprocess.run(
                [args.makemkvcon, "-r", "--cache=128", "info", args.device],
                capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return None, None, None, None
        if p.returncode != 0 and not p.stdout:
            return None, None, None, None
        cinfo, titles, msgs, rdisk = {}, {}, [], None
        msg_codes = set()
        for line in p.stdout.splitlines():
            tag, f = parse_line(line)
            if tag == "MSG" and len(f) >= 4:
                msgs.append(f[3])
                try: msg_codes.add(int(f[0]))
                except ValueError: pass
            elif tag == "DRV" and len(f) >= 7 and f[6].startswith("/dev/rdisk"):
                rdisk = f[6]
            elif tag == "CINFO" and len(f) >= 3:
                try: cinfo[int(f[0])] = f[2]
                except ValueError: pass
            elif tag == "TINFO" and len(f) >= 4:
                try:
                    tid, code = int(f[0]), int(f[1])
                    titles.setdefault(tid, {})[code] = f[3]
                except ValueError: pass
        return (cinfo, titles, msgs), msg_codes, rdisk, p

    result, codes, rdisk, _ = _run_once()
    if result is None:
        return None
    # MSG 5010 "Failed to open disc" with no titles parsed almost always means
    # macOS auto-mounted the volume and is holding the device exclusively.
    if not result[1] and codes and 5010 in codes and rdisk:
        if unmount_blocking_volume(rdisk):
            result, codes, rdisk, _ = _run_once()
            if result is None:
                return None
    cinfo, titles, msgs = result
    if titles:
        return {"cinfo": cinfo, "titles": titles, "msgs": msgs}
    if msgs_indicate_aacs_lock(msgs):
        return {"error": "AACS_LOCKED", "msgs": msgs}
    return None

def disc_label(info: dict) -> str:
    c = info.get("cinfo", {})
    return c.get(2) or c.get(32) or c.get(30) or c.get(1) or "(unknown)"

TV_MIN_DUR_S = 1200
# Floor below which MakeMKV titles are noise (menu reels, transitions, FBI
# warnings, language stubs). 60s lets through ~130-260MB studio bumpers on
# modern UHD discs; 120s catches all the legitimate junk without dropping
# real short featurettes.
# Between this and TV_MIN_DUR_S → routed to Extras/ as bonus content.
EXTRAS_MIN_DUR_S = 120

def select_titles(item: QueueItem, info: dict) -> list[int]:
    durs = [(tid, parse_duration(t.get(9, "0:00:00"))) for tid, t in info["titles"].items()]
    if not durs: return []
    if item.type == "movie":
        tid, dur = max(durs, key=lambda x: x[1])
        return [tid] if dur > 0 else []
    return [tid for tid, dur in durs if dur >= TV_MIN_DUR_S]

# -------- target paths --------
def compute_target_dir(item: QueueItem) -> Path:
    root = Path(item.target_root)
    if item.type == "movie":
        return root
    return root / f"Season {item.season:02d}"

# -------- episode counter (overwrite-safe) --------
def ep_key(item: QueueItem) -> str:
    return f"{item.title}||S{item.season:02d}"

def starting_ep(item: QueueItem, disc_n: int, state: dict) -> int:
    counts = state.get("disc_episode_counts", {}).get(ep_key(item), {})
    prior = sum(int(v) for k, v in counts.items() if int(k) < disc_n)
    return item.episode_start + prior

def record_episode_count(item: QueueItem, disc_n: int, count: int, state: dict):
    state.setdefault("disc_episode_counts", {}).setdefault(ep_key(item), {})[str(disc_n)] = int(count)

# -------- ripping --------
def _draw_progress(start: float, total_v: int, mx: int, cur_task: str, stall_age_s: float):
    pct = 100.0 * total_v / mx if mx else 0
    el = time.time() - start
    eta = (el * (100 - pct) / pct) if pct > 1 else 0
    bar = render_bar(pct/100)
    stall_tag = f" {C.YLW}stall {int(stall_age_s)}s{C.R}" if stall_age_s > 30 else ""
    sys.stdout.write(f"\r{C.GRN}{bar}{C.R} {pct:5.1f}%  "
                     f"el {fmt_dur(el)}  ETA {fmt_dur(eta)}  "
                     f"{cur_task[:38]:<38}{stall_tag}")
    sys.stdout.flush()

def _reader(stream, q):
    try:
        for line in stream:
            q.put(("line", line))
    finally:
        q.put(("eof", None))

def _free_gb(path: Path) -> float:
    # macOS Python's shutil.disk_usage (statvfs) returns wrong values for
    # SMB mounts; df uses BSD statfs which is correct. Use df for both.
    try:
        out = subprocess.check_output(["df", "-Pk", str(path)], text=True)
        avail_kb = int(out.strip().splitlines()[-1].split()[3])
        return avail_kb * 1024 / 1e9
    except Exception:
        return shutil.disk_usage(path).free / 1e9

def rip(args, item: QueueItem, info: dict, title_ids: list[int],
        target_dir: Path, state: dict, disc_n: int):
    target_dir.mkdir(parents=True, exist_ok=True)

    # Pre-check final filename collisions
    if item.type == "movie":
        final_target = target_dir / movie_filename(item, disc_n)
        if final_target.exists() and not args.overwrite:
            return False, f"target file exists: {final_target} (use --overwrite)"
    else:
        # Multi-disc seasons share one Season dir, so prior discs' episodes
        # are expected here. Only block on files at or above this disc's start.
        start_ep = starting_ep(item, disc_n, state)
        title_prefix = f"{sanitize(item.title)} - S{item.season:02d}E"
        colliding = []
        for p in target_dir.iterdir():
            if p.suffix.lower() != ".mkv" or not p.name.startswith(title_prefix):
                continue
            m = re.match(r"(\d{2})\.mkv$", p.name[len(title_prefix):])
            if m and int(m.group(1)) >= start_ep:
                colliding.append(p.name)
        if colliding and not args.overwrite:
            shown = ", ".join(sorted(colliding)[:3])
            more = f" (+{len(colliding) - 3} more)" if len(colliding) > 3 else ""
            return False, (f"target {target_dir} has existing .mkv at or above "
                           f"S{item.season:02d}E{start_ep:02d}: {shown}{more} "
                           f"(use --overwrite)")

    free_gb = _free_gb(target_dir)
    if free_gb < args.min_free_gb:
        return False, f"low disk space at target: {free_gb:.1f}GB free, need {args.min_free_gb}GB"

    # Local staging — keeps in-flight files off NAS
    LOCAL_STAGING_BASE.mkdir(parents=True, exist_ok=True)
    local_free_gb = shutil.disk_usage(LOCAL_STAGING_BASE).free / 1e9
    local_need = 110 if (item.format or "").upper() == "4K" else 60
    if local_free_gb < local_need:
        return False, (f"low disk space on local staging: {local_free_gb:.1f}GB free at "
                       f"{LOCAL_STAGING_BASE}, need {local_need}GB for {item.format}")

    staging = LOCAL_STAGING_BASE / f"{int(time.time())}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    # Belt-and-suspenders for any tool that accidentally scans staging
    (staging / ".ignore").write_text("burndvd staging dir\n")

    # Movies: title was pre-selected (longest) via select_titles; rip just
    # that one. Saves ~10-15min and ~12GB staging churn per UHD movie (no
    # alternate playlists / branching duplicates / bumpers written to disk).
    # TV: rip everything above the noise floor, partition main vs extras
    # by duration post-rip; loses the per-title pre-scan benefit but lets
    # one MakeMKV invocation handle a whole season disc.
    if item.type == "movie":
        if len(title_ids) != 1:
            return False, f"movie rip expects 1 title, got {title_ids}"
        selector = str(title_ids[0])
    else:
        selector = "all"

    cmd = [args.makemkvcon, "-r", "--progress=-same", "--noscan",
           f"--minlength={EXTRAS_MIN_DUR_S}",
           "mkv", args.device, selector, str(staging)]

    print(f"{C.D}$ {' '.join(cmd)}{C.R}")
    print(f"{C.D}  staging: {staging}{C.R}")
    start = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)

    # Threaded reader so the main loop can run a stall watchdog without blocking.
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()

    cur_v, total_v, mx = 0, 0, 65536
    cur_task = ""
    last_print = 0.0
    last_progress_time = time.time()
    last_progress_total = 0
    msgs: list[str] = []
    interrupted = False
    stalled = False
    stall_sec = stall_timeout_for(item.format)

    try:
        while True:
            try:
                kind, payload = q.get(timeout=2.0)
            except queue.Empty:
                stall_age = time.time() - last_progress_time
                if stall_age > stall_sec:
                    # MakeMKV's PRGV counter sometimes freezes mid-rip while
                    # makemkvcon keeps streaming bytes to disk. Confirm a real
                    # stall by checking staging .mkv mtimes before killing.
                    try:
                        recent_write = max(
                            (p.stat().st_mtime for p in staging.rglob("*.mkv")),
                            default=0.0)
                    except OSError:
                        recent_write = 0.0
                    if time.time() - recent_write < 60:
                        last_progress_time = time.time()
                    else:
                        sys.stdout.write("\n")
                        print(f"{C.RED}Stalled: no progress for {int(stall_age)}s "
                              f"(threshold {stall_sec}s for {item.format}, "
                              f"no staging write in {int(time.time()-recent_write)}s); "
                              f"killing makemkvcon.{C.R}")
                        stalled = True
                        proc.terminate()
                        try: proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill(); proc.wait()
                        break
                _draw_progress(start, total_v, mx, cur_task, stall_age)
                continue
            if kind == "eof":
                break
            line = payload.rstrip()
            tag, f = parse_line(line)
            if tag == "PRGV" and len(f) >= 3:
                try:
                    cur_v, total_v, mx = int(f[0]), int(f[1]), int(f[2])
                    if total_v > last_progress_total:
                        last_progress_total = total_v
                        last_progress_time = time.time()
                except ValueError: pass
            elif tag == "PRGT" and len(f) >= 3:
                cur_task = f[2]
            elif tag == "MSG" and len(f) >= 4:
                msgs.append(f[3])
                if len(msgs) > 50: msgs = msgs[-50:]
            now = time.time()
            if now - last_print > 0.5 and mx > 0:
                _draw_progress(start, total_v, mx, cur_task, now - last_progress_time)
                last_print = now
    except KeyboardInterrupt:
        interrupted = True
        sys.stdout.write("\n")
        print(f"{C.YLW}Interrupted; killing makemkvcon...{C.R}")
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if not interrupted and not stalled:
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()

    elapsed = time.time() - start
    sys.stdout.write("\n")

    if stalled:
        shutil.rmtree(staging, ignore_errors=True)
        return False, (f"stalled at PRGV total={last_progress_total}/{mx}; "
                       f"last MSGs: {' | '.join(msgs[-3:])}")

    if proc.returncode != 0:
        tail = " | ".join(msgs[-5:]) or "(no MSG output)"
        shutil.rmtree(staging, ignore_errors=True)
        return False, f"makemkvcon exit {proc.returncode}; last MSGs: {tail}"

    # makemkvcon exits 0 even when titles fail. Verify save summary.
    summary = parse_save_summary(msgs)
    if summary is None:
        shutil.rmtree(staging, ignore_errors=True)
        return False, f"no save-summary MSG found; last MSGs: {' | '.join(msgs[-5:])}"
    saved, failed = summary
    if failed > 0 or saved == 0:
        shutil.rmtree(staging, ignore_errors=True)
        return False, (f"makemkvcon save summary: {saved} saved, {failed} failed; "
                       f"last MSGs: {' | '.join(msgs[-5:])}")

    rips = sorted(staging.glob("*.mkv"))
    if not rips:
        shutil.rmtree(staging, ignore_errors=True)
        return False, "no .mkv files in staging despite save-summary success"

    def _src_dur_s(src: Path) -> int:
        tnum = title_num_from_filename(src.name)
        if tnum is None: return 0
        return parse_duration(info["titles"].get(tnum, {}).get(9, "0:00:00"))

    def _move_extras(extra_srcs: list, extras_dir: Path, name_for):
        """Route short titles to Extras/. name_for(src, tnum) → final filename."""
        if not extra_srcs: return
        extras_dir.mkdir(parents=True, exist_ok=True)
        for src in extra_srcs:
            tnum = title_num_from_filename(src.name)
            dest = extras_dir / name_for(src, tnum)
            if dest.exists() and not args.overwrite:
                print(f"{C.YLW}  extras: skip {dest.name} (exists){C.R}")
                try: src.unlink()
                except OSError: pass
                continue
            move_with_progress(src, dest, label=f"moving extra t{tnum:02d}" if tnum is not None else "moving extra")
            finals.append(str(dest))
        print(f"{C.GRN}  Moved {len(extra_srcs)} extra(s) to {extras_dir}{C.R}")

    finals: list[str] = []
    if item.type == "movie":
        # Largest staged file is the main feature; everything else >=60s is an extra.
        main = max(rips, key=lambda p: p.stat().st_size)
        target_path = target_dir / movie_filename(item, disc_n)
        if target_path.exists() and not args.overwrite:
            shutil.rmtree(staging, ignore_errors=True)
            return False, f"would overwrite {target_path}"
        move_with_progress(main, target_path, label="moving main title")
        finals.append(str(target_path))
        movie_base = movie_filename(item, disc_n)[:-4]  # strip ".mkv"
        _move_extras(
            [p for p in rips if p != main],
            target_dir / "Extras",
            lambda src, tnum: f"{movie_base} - extra - "
                              f"{('t%02d' % tnum) if tnum is not None else src.stem}.mkv",
        )
    else:
        # Partition: episodes are >= TV_MIN_DUR_S; everything else is an extra.
        episode_rips = [p for p in rips if _src_dur_s(p) >= TV_MIN_DUR_S]
        extras_rips  = [p for p in rips if _src_dur_s(p) <  TV_MIN_DUR_S]
        _move_extras(
            extras_rips,
            target_dir / "Extras",
            lambda src, tnum: f"{sanitize(item.title)} - S{item.season:02d}D{disc_n} - extra - "
                              f"{('t%02d' % tnum) if tnum is not None else src.stem}.mkv",
        )
        rips = episode_rips
        if not rips:
            shutil.rmtree(staging, ignore_errors=True)
            print(f"{C.YLW}  No titles >= {TV_MIN_DUR_S}s — extras-only disc.{C.R}")
            return True, {"elapsed_s": elapsed, "files": finals}
        start_ep = starting_ep(item, disc_n, state)
        proposed = []
        for i, src in enumerate(rips):
            ep = start_ep + i
            tnum = title_num_from_filename(src.name)
            dur_str = info["titles"].get(tnum, {}).get(9, "?:??:??") if tnum is not None else "?:??:??"
            size_gb = src.stat().st_size / 1e9
            proposed.append((src, ep, tnum, dur_str, size_gb))

        print(f"\n{C.B}Proposed episode mapping (disc {disc_n}/{item.discs}):{C.R}")
        for src, ep, tnum, dur_str, size_gb in proposed:
            print(f"  S{item.season:02d}E{ep:02d}  ←  {src.name}  ({dur_str}, {size_gb:.2f}GB)")
        ans = auto_answer(args, f"{C.B}In broadcast order? [Y/n]:{C.R} ", "y")
        accept = ans in ("", "y", "yes")

        if accept:
            for src, ep, _, _, _ in proposed:
                final = target_dir / f"{sanitize(item.title)} - S{item.season:02d}E{ep:02d}.mkv"
                if final.exists() and not args.overwrite:
                    shutil.rmtree(staging, ignore_errors=True)
                    return False, f"would overwrite {final}"
                move_with_progress(src, final, label=f"moving episode {ep:02d}")
                finals.append(str(final))
            record_episode_count(item, disc_n, len(proposed), state)
        else:
            for src, _, tnum, _, _ in proposed:
                tag = f"t{tnum:02d}" if tnum is not None else src.stem
                final = target_dir / f"{sanitize(item.title)} - S{item.season:02d} - {tag}.mkv"
                if final.exists() and not args.overwrite:
                    shutil.rmtree(staging, ignore_errors=True)
                    return False, f"would overwrite {final}"
                move_with_progress(src, final, label=f"moving {tag}")
                finals.append(str(final))
            print(f"{C.YLW}Files renamed to traceable form (no SxxExx).{C.R}")
            print(f"{C.YLW}  Manual rename required before Plex/Jellyfin will index.{C.R}")
            try:
                count = auto_answer(args,
                    "How many actual episodes were on this disc? [Enter to skip]: ", "")
            except (KeyboardInterrupt, EOFError):
                count = ""
            if count.isdigit() and int(count) > 0:
                record_episode_count(item, disc_n, int(count), state)
                next_start = starting_ep(item, disc_n + 1, state)
                print(f"  Recorded {count} episodes; next disc → S{item.season:02d}E{next_start:02d}.")
            else:
                print(f"{C.YLW}  Counter not advanced; next disc → "
                      f"S{item.season:02d}E{starting_ep(item, disc_n+1, state):02d}.{C.R}")

    shutil.rmtree(staging, ignore_errors=True)
    return True, {"elapsed_s": elapsed, "files": finals}

def verify(paths: list[str]):
    for p in paths:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", p, "-f", "null", "-"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"{p}: exit {r.returncode}; {r.stderr.strip()[:240]}"
    return True, ""

# -------- side effects --------
def eject(args):
    if args.no_eject: return
    # drutil asks nicely; loginwindow dissents after Spotlight touches the disc
    # (seen 2026-05-23 on BU40N). diskutil eject force bypasses the dissenter.
    candidates = [
        ["drutil", "eject", "external"],
        ["drutil", "tray", "eject"],
        ["diskutil", "eject", "force", "disk4"],
    ]
    for cmd in candidates:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return
    print(f"{C.YLW}Eject failed; remove disc manually before next.{C.R}")
    append_log(args, "WARN  eject failed (all eject variants returned non-zero)")

def beep(args, ok: bool):
    if args.no_sound: return
    sound = "/System/Library/Sounds/Glass.aiff" if ok else "/System/Library/Sounds/Sosumi.aiff"
    subprocess.Popen(["afplay", sound], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def run_subocr_postrip(args, files: list[str]):
    """Detach a subocr run on the newly-ripped files. Won't block the queue
    (next disc can be inserted immediately) and won't kill OCR if ripqueue
    exits."""
    if getattr(args, "no_subocr", False) or not files:
        return
    subocr_bin = Path.home() / ".local" / "bin" / "subocr"
    if not subocr_bin.exists():
        return
    log_dir = Path.home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"subocr-postrip-{int(time.time())}.log"
    cmd = [str(subocr_bin), "--post-rip"] + list(files)
    with open(log_path, "a") as f:
        # stdin=DEVNULL is required: ripqueue is launched via `script + caffeinate`,
        # whose stdin can be in a state Python's startup can't initialize cleanly,
        # producing "can't initialize sys standard streams" on the venv interpreter.
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                         stdout=f, stderr=subprocess.STDOUT,
                         start_new_session=True)
    print(f"{C.D}  subocr started in background ({len(files)} file(s); log: {log_path.name}){C.R}")

# Push notifications on FAIL are intentionally not emitted from ripqueue
# itself; wire your own watcher against the log file if you want them.
# Keeps the queue process dependency-free and single-source-of-truth.

# -------- queue commands --------
def show_remaining(state: dict):
    print(f"\n{C.B}Remaining queue:{C.R}")
    for i in range(state["current_index"], len(state["queue"])):
        it = state["queue"][i]
        mark = "->" if i == state["current_index"] else "  "
        extra = f" S{it['season']:02d}" if it["type"] == "tv-season" else ""
        print(f"  {mark} {i+1:>3}. [{it['type']:<10}{extra}] {it['title']} "
              f"({it['discs']} disc{'s' if it['discs']!=1 else ''}, {it['format']})")
    print()

def menu(state: dict, args) -> str:
    print(f"\n{C.B}Menu:{C.R} [c]ontinue [s]kip [u]rgent [p]ark-current "
          f"[r]emaining [q]uit")
    while True:
        a = input("> ").strip().lower()
        if a in ("c","s","u","p","r","q"): break
    if a == "r":
        show_remaining(state); return menu(state, args)
    if a == "q":
        save_state(state, args.state); print("State saved. Bye."); sys.exit(0)
    if a == "s":
        cur = state["queue"][state["current_index"]]
        print(f"{C.YLW}Skipping: {cur['title']}{C.R}")
        state["current_index"] += 1; state["disc_index_in_item"] = 0
    if a == "p":
        cur = state["queue"].pop(state["current_index"])
        state["queue"].append(cur); state["disc_index_in_item"] = 0
        print(f"{C.YLW}Parked '{cur['title']}' to end{C.R}")
    if a == "u":
        try: urgent = build_urgent_interactive()
        except (KeyboardInterrupt, EOFError):
            print("Cancelled."); return "c"
        state["queue"].insert(state["current_index"], asdict(urgent))
        state["disc_index_in_item"] = 0
        print(f"{C.YLW}Inserted '{urgent.title}' at front{C.R}")
    save_state(state, args.state)
    return "c"

def build_urgent_interactive() -> QueueItem:
    title = input("  title: ").strip()
    typ = (input("  type [movie/tv-season] (movie): ").strip() or "movie")
    discs = int(input("  disc count (1): ").strip() or "1")
    target_root = input("  target root path: ").strip()
    fmt = (input("  format [4K/BD/DVD] (BD): ").strip() or "BD")
    season, ep_start = 0, 1
    if typ == "tv-season":
        season = int(input("  season number: ").strip())
        ep_start = int(input("  episode start (1): ").strip() or "1")
    return QueueItem(title, typ, discs, target_root, fmt, season, ep_start, "urgent")

# -------- header --------
def remaining_discs(state: dict) -> int:
    n = 0
    for i, it in enumerate(state["queue"]):
        if i < state["current_index"]: continue
        if i == state["current_index"]:
            n += max(0, it["discs"] - state["disc_index_in_item"])
        else:
            n += it["discs"]
    return n

def avg(xs): return sum(xs)/len(xs) if xs else 0.0

def header(state: dict, item: QueueItem, disc_n: int):
    pos = state["current_index"] + 1
    total = len(state["queue"])
    a = avg(state["rip_durations_s"])
    rd = remaining_discs(state)
    eta = a * rd if a else 0
    proj = (datetime.now() + timedelta(seconds=eta)).strftime("%a %b %d %H:%M") if eta else "--"
    print(f"\n{C.B}{'='*72}{C.R}")
    print(f"{C.B}Queue {pos}/{total}{C.R}  {C.CYA}{item.title}{C.R}  ({item.format})")
    if item.type == "tv-season":
        print(f"  TV  S{item.season:02d}  Disc {disc_n}/{item.discs}")
    else:
        print(f"  Movie  Disc {disc_n}/{item.discs}")
    print(f"  Target: {item.target_root}")
    if item.notes: print(f"  Notes:  {item.notes}")
    print(f"  Done: {len(state['completed'])} rips   Avg/disc: {fmt_dur(a)}   "
          f"Remaining discs: {rd}")
    print(f"  Projected complete: {proj}  (~{fmt_dur(eta)})")
    print(f"{C.B}{'='*72}{C.R}")

# -------- match check --------
def loose_match(label: str, expected: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())
    a, b = norm(label), norm(expected)
    if not a or not b: return False
    if a == b: return True
    if len(a) < 6 or len(b) < 6: return False
    return a in b or b in a

# -------- key expiry --------
def check_key_expiry(args):
    """Run a quick `info` and scan for evaluation-key expiry. Prints a banner
    if <14 days remaining. Skips silently for permanent keys or parse failures."""
    try:
        p = subprocess.run(
            [args.makemkvcon, "-r", "--cache=1", "info", args.device],
            capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return
    msgs = []
    for line in p.stdout.splitlines():
        tag, f = parse_line(line)
        if tag == "MSG" and len(f) >= 4:
            msgs.append(f[3])
    days = scan_for_key_expiry(msgs)
    if days is None:
        return
    if days <= 14:
        print(f"{C.YLW}MakeMKV beta key expires in {days} day(s).{C.R}")
        print(f"{C.YLW}  This is a multi-month project. Consider buying the permanent")
        print(f"{C.YLW}  license (~$60) or refresh the beta key from{C.R}")
        print(f"{C.YLW}  https://forum.makemkv.com/forum/viewtopic.php?t=1053{C.R}")
    else:
        print(f"{C.D}MakeMKV evaluation key: {days} days remaining.{C.R}")

# -------- main --------
def auto_answer(args, prompt_text: str, auto: str) -> str:
    """Wrapper around input() that auto-answers when --non-interactive.

    `auto` is the lowercase string that would-be typed by the user. We print
    it so the log shows what was decided.
    """
    if getattr(args, "non_interactive", False):
        print(f"{prompt_text}{auto}  {C.D}(--non-interactive){C.R}")
        return auto
    return input(prompt_text).strip().lower()

# Parse the byte offset out of a stall result string for same-byte detection.
_STALL_BYTE_RE = re.compile(r"stalled at PRGV total=(\d+)/")

def stall_byte(result: str) -> int | None:
    m = _STALL_BYTE_RE.search(result or "")
    return int(m.group(1)) if m else None

def append_log(args, line: str):
    log = Path(args.state).with_suffix(".log")
    with open(log, "a") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {line}\n")

def wait_for_disc(args, state: dict):
    print(f"\n{C.CYA}Insert disc, or press Enter for menu...{C.R}", flush=True)
    last_poll_print = 0.0
    started = time.time()
    last_dropoff_hint = 0.0
    # In --non-interactive mode (cron, launchd, background bash) stdin is
    # typically closed, which makes select.select fire immediately on it as
    # "readable" (read would return EOF). The previous code interpreted that
    # as a user keypress and opened the menu, which then crashed on EOFError
    # at the input() call. Skip the stdin check entirely when non-interactive.
    poll_stdin = not getattr(args, "non_interactive", False)
    while True:
        if poll_stdin:
            import select
            r, _, _ = select.select([sys.stdin], [], [], 2.0)
            if r:
                sys.stdin.readline()
                return ("cmd", menu(state, args))
        else:
            time.sleep(2.0)
        info = probe_disc(args)
        if info:
            return ("disc", info)
        now = time.time()
        if now - last_poll_print > 10:
            print(f"  {C.D}polling drive ({datetime.now().strftime('%H:%M:%S')})...{C.R}",
                  flush=True)
            last_poll_print = now
        if now - started > 120 and now - last_dropoff_hint > 60:
            print(f"  {C.YLW}No disc detected for >2min. If the drive dropped off bus:{C.R}")
            print(f"  {C.YLW}    system_profiler SPUSBDataType | grep -B2 -A4 'BU40N\\|UHD'{C.R}")
            print(f"  {C.YLW}    Reseat USB cable, try a powered hub, or run under{C.R}")
            print(f"  {C.YLW}    `caffeinate -dimsu burndvd ...` to defeat power management.{C.R}")
            last_dropoff_hint = now

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", required=True)
    ap.add_argument("--state", default="ripqueue-state.json")
    ap.add_argument("--makemkvcon", default=DEFAULT_MAKEMKVCON)
    ap.add_argument("--device", default="disc:0")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--min-free-gb", type=int, default=200)
    ap.add_argument("--no-eject", action="store_true")
    ap.add_argument("--no-sound", action="store_true")
    ap.add_argument("--no-subocr", action="store_true",
                    help="Skip post-rip OCR of PGS subtitles to external .srt")
    ap.add_argument("--non-interactive", action="store_true",
                    help="never prompt: ambiguity prompts auto-skip the item, "
                         "rip/verify failures use --on-fail.")
    ap.add_argument("--on-fail", choices=["skip", "retry", "abort"],
                    default="skip",
                    help="non-interactive disposition for FAIL. "
                         "retry attempts up to 2 times before giving up; "
                         "abort exits non-zero.")
    args = ap.parse_args()

    if not sys.stdin.isatty() and not args.non_interactive:
        print(f"{C.RED}burndvd requires an interactive TTY (stdin not a terminal).{C.R}",
              file=sys.stderr)
        print(f"{C.RED}  Don't run under nohup or with stdin piped/redirected,{C.R}",
              file=sys.stderr)
        print(f"{C.RED}  or pass --non-interactive.{C.R}",
              file=sys.stderr)
        sys.exit(1)

    if not Path(args.makemkvcon).exists() and not shutil.which(args.makemkvcon):
        print(f"{C.RED}makemkvcon not found at {args.makemkvcon}{C.R}", file=sys.stderr)
        sys.exit(1)

    warn_state_location(args.state)
    check_key_expiry(args)

    items = load_queue(Path(args.queue))
    state = load_or_init(args, items)
    save_state(state, args.state)

    while state["current_index"] < len(state["queue"]):
        item = QueueItem(**state["queue"][state["current_index"]])
        disc_n = state["disc_index_in_item"] + 1
        header(state, item, disc_n)

        kind, payload = wait_for_disc(args, state)
        if kind == "cmd":
            continue
        info = payload

        # AACS-locked: park or skip, don't churn retrying the same key.
        if isinstance(info, dict) and info.get("error") == "AACS_LOCKED":
            print(f"{C.RED}Disc appears AACS-locked (newer protection than current keys).{C.R}")
            for m in info.get("msgs", [])[-3:]:
                print(f"  {C.D}{m}{C.R}")
            print("  Recent UHD release? Wait for next MakeMKV update or refresh keys.")
            ans = auto_answer(args, "[p]ark item / [s]kip item / [r]etry: ", "s")
            if ans.startswith("p"):
                cur = state["queue"].pop(state["current_index"])
                state["queue"].append(cur)
                state["disc_index_in_item"] = 0
                save_state(state, args.state)
                append_log(args, f"AACS  {item.title} disc{disc_n}  parked to end")
            elif ans.startswith("s"):
                state["current_index"] += 1
                state["disc_index_in_item"] = 0
                save_state(state, args.state)
                append_log(args, f"AACS  {item.title} disc{disc_n}  skipped")
            beep(args, ok=False)
            eject(args)
            continue

        label = disc_label(info)
        print(f"  Disc label: {C.MAG}{label}{C.R}")

        if not loose_match(label, item.title):
            print(f"{C.YLW}Disc label doesn't match expected '{item.title}'.{C.R}")
            # Under --non-interactive: trust the title the user typed. Disc
            # internal labels are often generic ("DISC_2", "MOVIE", studio
            # boilerplate) and don't match the canonical show/movie name.
            ans = auto_answer(args, "Proceed anyway? [y/N/skip] ", "y")
            if ans == "skip":
                state["current_index"] += 1; state["disc_index_in_item"] = 0
                save_state(state, args.state); continue
            if ans != "y":
                print("Eject and try again."); continue

        title_ids = select_titles(item, info)
        if not title_ids:
            print(f"{C.RED}No suitable titles on disc (TV floor: {TV_MIN_DUR_S}s).{C.R}")
            beep(args, ok=False)
            ans = auto_answer(args, "[r]etry / [s]kip? ", "s")
            if ans.startswith("s"):
                state["current_index"] += 1; state["disc_index_in_item"] = 0
                save_state(state, args.state)
            continue

        print(f"  Selected {len(title_ids)} title(s): {title_ids}")
        target = compute_target_dir(item)

        ok, result = rip(args, item, info, title_ids, target, state, disc_n)
        if not ok:
            print(f"{C.RED}Rip failed: {result}{C.R}")
            append_log(args, f"FAIL  {item.title} disc{disc_n}  {result}")
            beep(args, ok=False)

            # Force-skip after the same byte stalls twice in a row: that's
            # a bad sector / bad disc, not something a retry will fix.
            byte = stall_byte(result)
            recent = state.setdefault("recent_stall_bytes", {})
            key = f"{item.title}||disc{disc_n}"
            prior = recent.get(key)
            same_byte_count = (prior["count"] + 1
                               if byte is not None and prior
                                   and prior.get("byte") == byte
                               else 1)
            if byte is not None:
                recent[key] = {"byte": byte, "count": same_byte_count}

            if same_byte_count >= 2:
                print(f"{C.YLW}Same-byte stall at total={byte} hit "
                      f"{same_byte_count}x; skipping disc as bad-sector.{C.R}")
                append_log(args, f"SKIP  {item.title} disc{disc_n}  "
                                 f"repeated stall at PRGV total={byte}")
                ans = "s"
            elif args.non_interactive:
                if args.on_fail == "abort":
                    print(f"{C.RED}--on-fail=abort; exiting non-zero.{C.R}")
                    save_state(state, args.state)
                    sys.exit(2)
                ans = "r" if args.on_fail == "retry" else "s"
                print(f"[r]etry / [s]kip? {ans}  "
                      f"{C.D}(--on-fail={args.on_fail}){C.R}")
            else:
                ans = input("[r]etry / [s]kip? ").strip().lower()

            if ans.startswith("s"):
                state["current_index"] += 1; state["disc_index_in_item"] = 0
                recent.pop(key, None)
            save_state(state, args.state); continue

        if args.verify:
            print(f"{C.D}Verifying with ffmpeg...{C.R}")
            ok2, err = verify(result["files"])
            if not ok2:
                print(f"{C.RED}Verify failed: {err}{C.R}")
                beep(args, ok=False)
                # NOTE: verify-failures don't currently write append_log entries,
                # so the watcher won't see them. If we want push on verify-fail,
                # add an append_log call here with a parseable tag.
                ans = auto_answer(args, "[r]etry / [s]kip / [a]ccept? ", "s")
                if ans.startswith("s"):
                    state["current_index"] += 1; state["disc_index_in_item"] = 0
                    save_state(state, args.state); continue
                if ans.startswith("r"):
                    save_state(state, args.state); continue

        state["completed"].append({
            "item": item.title, "disc_n": disc_n, "type": item.type,
            "files": result["files"], "elapsed_s": result["elapsed_s"],
            "ts": time.time(),
        })
        state.get("recent_stall_bytes", {}).pop(f"{item.title}||disc{disc_n}", None)
        state["rip_durations_s"].append(result["elapsed_s"])
        state["rip_durations_s"] = state["rip_durations_s"][-30:]
        if disc_n >= item.discs:
            state["current_index"] += 1; state["disc_index_in_item"] = 0
        else:
            state["disc_index_in_item"] += 1
        save_state(state, args.state)

        append_log(args, f"OK    {item.title} disc{disc_n}  "
                         f"{fmt_dur(result['elapsed_s'])}  "
                         f"{len(result['files'])} file(s)")
        print(f"{C.GRN}Done in {fmt_dur(result['elapsed_s'])}:{C.R}")
        for fp in result["files"]:
            print(f"  {fp}")
        beep(args, ok=True)
        eject(args)
        run_subocr_postrip(args, result["files"])

    print(f"\n{C.B}{C.GRN}Queue complete.{C.R}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. State on disk is current.")
        sys.exit(130)
