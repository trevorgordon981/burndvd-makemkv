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
import argparse, contextlib, csv, fcntl, json, os, queue, re, shutil, signal, subprocess, sys, threading, time
from dataclasses import dataclass, asdict, field
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
    # season_dir_lock's claim mechanism creates 0-byte placeholder files at
    # the chosen destination paths so parallel rips see those slots as
    # taken. shutil.move would raise (cross-fs) or silently overwrite
    # (same-fs) depending on the dst layout; explicitly unlink any 0-byte
    # placeholder so semantics are uniform.
    try:
        if dst.is_file() and dst.stat().st_size == 0:
            dst.unlink()
    except OSError:
        pass
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
    type: str            # "movie" | "tv-season" | "double-feature"
    discs: int
    target_root: str
    format: str          # "4K" | "BD" | "DVD"
    season: int = 0
    episode_start: int = 1
    notes: str = ""
    # Double-feature only: list of {"title_id": int, "name": str}. One physical
    # disc holding two (or more) distinct movies, each pinned to a specific
    # MakeMKV title index and named/foldered independently. Empty for everything
    # else. target_root for a double-feature item is the Movies *base* dir (no
    # per-title subfolder) — each feature gets its own "<base>/<Name>/" folder.
    features: list = field(default_factory=list)

def _parse_features(raw: str) -> list:
    """Parse the CSV `features` column (a JSON array string) into a list of
    {"title_id": int, "name": str} dicts. Tolerates empty/missing values."""
    raw = (raw or "").strip()
    if not raw:
        return []
    data = json.loads(raw)
    out = []
    for d in data:
        out.append({"title_id": int(d["title_id"]), "name": str(d["name"]).strip()})
    return out

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
                features=_parse_features(row.get("features", "")),
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

def _drive_idx_from_args(args) -> int | None:
    """Extract the makemkvcon disc:N index from args.device (0-based). Returns
    None for non-disc inputs (iso:, file:, dev:) which legacy-pick the first
    drive. Used to target drutil/diskutil at the right drive on multi-drive
    setups — without this, eject/respin/disc-present commands hit drutil's
    "first drive" default, which on a 2+ drive setup can routinely operate
    on the wrong drive's disc (e.g. ejecting a mid-rip disc on the other
    drive). Threaded into eject/respin/disc_present below."""
    m = re.match(r"disc:(\d+)$", getattr(args, "device", "") or "")
    return int(m.group(1)) if m else None

def _drive_identity(drive_idx: int) -> str | None:
    """Vendor+product+rev string for the drive at the given makemkvcon disc:N
    index (0-based). Used to detect drive hotswap / USB reset / reboot
    mid-queue: drives can change /dev/diskN under us, and disc:N can silently
    map to a different physical drive between items, sending the next rip to
    a different drive's content. Caching this at queue start and re-checking
    per-item catches the divergence early (audit #12).

    Returns None if no drive at that index. The exact format we capture is
    drutil's `Vendor Product Rev` triple, which is stable across reboots
    of the SAME drive but differs across drive substitutions."""
    try:
        r = subprocess.run(["drutil", "status", "-drive", str(drive_idx + 1)],
                           capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return None
    # drutil output looks like:
    #   Vendor   Product           Rev
    #   HL-DT-ST BD-RE BU40N       1.03
    # Skip the header, grab the data row.
    for line in (r.stdout or "").splitlines():
        s = line.strip()
        if not s or s.lower().startswith(("vendor", "type:", "name:", "sessions:",
                                          "tracks:", "overwritable:", "space ",
                                          "writability:", "book ")):
            continue
        return s
    return None

def _drutil_disk_node(drive_idx: int | None = None) -> str | None:
    """Return /dev/diskN for the given drive index (0-based — matches
    makemkvcon disc:N; drutil is 1-indexed internally so we add 1).
    drive_idx=None falls back to drutil's first-drive default for callers
    that don't have args (e.g. cron sweeps).
    drutil knows the device even when makemkvcon has stopped reporting it."""
    cmd = ["drutil", "status"]
    if drive_idx is not None:
        cmd += ["-drive", str(drive_idx + 1)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return None
    m = re.search(r"Name:\s+(/dev/disk\d+)", r.stdout or "")
    return m.group(1) if m else None

def _optical_disk_node() -> str | None:
    """Legacy single-drive helper for callers without an `args` context.
    Prefer _drutil_disk_node(_drive_idx_from_args(args)) at new call sites."""
    return _drutil_disk_node(None)

def _optical_mounts() -> list[str]:
    """Mounted /dev/diskN nodes with optical filesystem types. Catches stale
    mounts that linger after the disc has dropped off (drutil + makemkvcon
    will both report "No Media" while `mount` still shows the volume)."""
    # timeout=10 (audit #14): a stuck SMB / autofs mount can make `mount`
    # block indefinitely. Better to return empty and let upstream handle
    # the "no mount info" case than freeze the entire rip queue.
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True,
                             timeout=10).stdout
    except (subprocess.TimeoutExpired, OSError):
        return []
    return re.findall(r"^(/dev/disk\d+)\b.*\((?:udf|cd9660)\b", out, re.MULTILINE)

def unmount_blocking_volume(rdisk_path: str | None) -> bool:
    """macOS auto-mounts BD/DVD volumes, which holds the device exclusively and
    makes makemkvcon emit MSG 5010 "Failed to open disc". Unmount the volume
    (keeping the disc in the drive). Returns True if something was actually
    unmounted. Prefers the device makemkvcon reported; falls back to drutil."""
    # macOS's DVD Player.app auto-launches on insert and holds the volume,
    # dissenting `diskutil unmount` ("dissented by PID NNN /Applications/DVD
    # Player.app/..."). Quit it first so the unmount can actually proceed.
    subprocess.run(["pkill", "-x", "DVD Player"],
                   capture_output=True, timeout=5)
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
    # timeout=10 — see _optical_mounts (audit #14).
    try:
        mounts = subprocess.run(["mount"], capture_output=True, text=True,
                                timeout=10).stdout
    except (subprocess.TimeoutExpired, OSError):
        mounts = ""
    unmounted = False
    for disk in candidates:
        if not re.search(rf"^{re.escape(disk)}\b", mounts, re.MULTILINE):
            continue
        for cmd in (["diskutil", "unmount", disk], ["diskutil", "unmount", "force", disk]):
            # diskutil unmount can dissent for tens of seconds while the
            # OS asks every dissenter; capping at 30s avoids a stuck
            # dissenter (e.g. a frozen Spotlight indexer) stalling the rip.
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=30)
            except subprocess.TimeoutExpired:
                continue
            if r.returncode == 0:
                print(f"  {C.D}auto-unmounted {disk} so makemkvcon can open the disc{C.R}")
                unmounted = True
                break
        else:
            print(f"  {C.YLW}diskutil unmount {disk} failed: {r.stderr.strip() or r.stdout.strip()}{C.R}")
    return unmounted

def disc_present(args=None) -> bool:
    """True if optical media is actually loaded *in this rip's drive*. Gates
    the re-spin recovery so we never cycle the tray on an empty drive while
    waiting for the user to insert a disc. `drutil status` prints 'Type: No
    Media' when empty and a real disc type (BD-ROM/DVD-ROM/...) when loaded.
    With args, scopes to the drive at args.device's index so a second drive's
    empty state doesn't trick a busy drive into a respin."""
    cmd = ["drutil", "status"]
    drive_idx = _drive_idx_from_args(args) if args is not None else None
    if drive_idx is not None:
        cmd += ["-drive", str(drive_idx + 1)]
    try:
        out = subprocess.run(cmd, capture_output=True,
                             text=True, timeout=10).stdout
    except (subprocess.TimeoutExpired, OSError):
        return False
    m = re.search(r"Type:\s*(.+)", out)
    return bool(m) and "no media" not in m.group(1).strip().lower()

def respin_drive(args=None) -> bool:
    """Last-resort reset for a hung LibreDrive handshake: eject the tray and
    immediately reclose it so the drive re-spins the disc and redoes the
    handshake. The disc never leaves the drive. Unmount any auto-mounted volume
    first so the tray isn't held. Returns True if the cycle ran. The post-close
    sleep gives the disc time to spin up before the next probe.
    Drive-targeted via args so a multi-drive setup doesn't pop the OTHER
    drive's disc mid-rip — the prior bare `drutil tray eject` would act on
    drutil's first drive regardless of which one this ripqueue owns."""
    unmount_blocking_volume(None)
    print(f"  {C.YLW}drive handshake stuck — re-spinning the disc "
          f"(tray eject + reclose, disc stays in){C.R}", flush=True)
    drive_idx = _drive_idx_from_args(args) if args is not None else None
    drive_args = ["-drive", str(drive_idx + 1)] if drive_idx is not None else []
    # timeouts (audit #14): drutil can hang on a drive whose firmware has
    # locked up; capping each call prevents the respin from extending the
    # 2-min hang-detect window into an indefinite freeze.
    for sub_cmd in (["tray", "eject"], ["tray", "close"]):
        try:
            subprocess.run(["drutil"] + drive_args + sub_cmd,
                           capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            pass
        if sub_cmd[-1] == "eject":
            time.sleep(3)
    time.sleep(20)
    return True

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
PROBE_STALL_S = 60      # kill makemkvcon if no output for this long (it's hung)
PROBE_HARD_S = 300      # absolute cap on a single probe attempt

# Recovery for a flaky LibreDrive handshake. On the BU40N (seen on the WALL-E
# UHD rip 2026-05-29) makemkvcon intermittently opens the drive (MSG:2010) then
# hangs forever before the LibreDrive transition, emitting zero further output —
# the stall watchdog kills it and every retry lands on the same hung handshake.
# A good attempt enumerates titles in ~60-90s, so the cure is to keep retrying
# with a rest between attempts (lets the drive settle), and only if still stuck
# after a few minutes, cycle the tray (eject + immediate reclose, disc stays in)
# to force the drive to re-spin and redo the handshake.
PROBE_COOLDOWN_S = 20   # rest between failed probes so the drive can settle
RESPIN_AFTER_S = 150    # stuck-but-disc-present this long -> tray re-spin
MAX_RESPINS = 3         # cap tray cycles so we never loop the tray forever

# Module-level last-line timestamp from probe_disc. wait_for_disc reads it
# to decide whether to print the misleading "drive dropped off bus" warning:
# if makemkvcon is actively producing output, the drive is fine — the probe
# is just slow.
_LAST_PROBE_ACTIVITY = 0.0

# Set true by the SIGUSR1 handler (sent by `stoprip --skip-current`). The
# main loop checks this between queue items and advances current_index when
# set, letting batch sessions move past a stuck disc without killing the
# whole queue.
_SKIP_REQUESTED = False

# Background finalize (NAS move + verify + subocr). The optical drive is only
# needed for the rip-to-staging step; the multi-minute SMB copy that follows
# holds neither the drive nor the disc. So once staging succeeds we eject and
# run the copy on a worker thread, freeing the drive for the next disc (a fresh
# burndvd process, or the next item in a CSV queue) while this copy finishes.
# Threads are NON-daemon and joined by join_finalizers() before the process
# exits, so a normal exit never truncates an in-flight transfer. A lock
# serializes copies: a copy is always faster than a rip, so serial is enough,
# and it keeps NAS writes and progress output from interleaving.
_FINALIZE_THREADS: list = []
_FINALIZE_LOCK = threading.Lock()


def probe_disc(args) -> dict | None:
    def _run_once():
        # makemkvcon occasionally hangs ("stuck" in ps, 0% CPU) on certain DVDs
        # — most often after the LibreDrive "Opening Blu-ray/DVD disc" step
        # and before TINFO/CINFO lines start streaming. The old subprocess.run
        # would wait the full 300s timeout before giving up; user would see the
        # "No disc detected for >2min" warning while the drive was actually
        # locked by a dead process. Stream stdout with a stall watchdog and
        # kill early on a 60s output gap so the outer loop can retry.
        # Pre-emptive unmount: macOS auto-mounts BD/DVD volumes seconds after
        # the drive is opened, holding the device exclusively. The old code
        # only reacted *after* a probe failed (line 529), which meant every
        # first attempt against a freshly-inserted disc raced the mounter and
        # frequently lost. Clearing the mount up front lets makemkvcon get
        # clean LibreDrive direct access from the start. No-op if nothing's
        # mounted, so it's cheap on the common path.
        if _optical_mounts():
            unmount_blocking_volume(None)
        try:
            proc = subprocess.Popen(
                [args.makemkvcon, "-r", "--cache=128", "info", args.device],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
        except OSError:
            return None, None, None, None

        q: queue.Queue = queue.Queue()
        threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()

        lines: list[str] = []
        last_line_t = time.time()
        start = time.time()
        last_status_t = 0.0
        titles_added = 0
        killed = False
        # Patience modes: a "normal" disc enumerates titles in seconds and any
        # 60s gap is a true hang. A disc with a corrupt IFO file (MSG:3042)
        # triggers makemkvcon's VOB-scan fallback, which has legitimate silent
        # stretches of several minutes while it walks corrupted sectors. Once
        # we see 3042, extend both the per-stall and hard timeouts so we don't
        # kill makemkvcon mid-scan and lose titles it would have parsed.
        stall_s = PROBE_STALL_S
        hard_s = PROBE_HARD_S
        slow_mode = False
        while True:
            now = time.time()
            if now - start > hard_s:
                proc.kill()
                killed = True
                break
            if now - last_line_t > stall_s:
                proc.kill()
                killed = True
                break
            # Live progress every 10s — quiet probes look hung otherwise.
            # Shows elapsed time + titles enumerated so far + slow-mode flag.
            if now - last_status_t >= 10:
                tag = " (slow-mode VOB scan)" if slow_mode else ""
                print(f"  {C.D}probing... {int(now - start)}s elapsed, "
                      f"{titles_added} title(s) found{tag}{C.R}",
                      flush=True)
                last_status_t = now
            try:
                kind, payload = q.get(timeout=2)
            except queue.Empty:
                continue
            if kind == "eof":
                break
            if kind == "line":
                line = payload.rstrip()
                lines.append(line)
                last_line_t = now
                global _LAST_PROBE_ACTIVITY
                _LAST_PROBE_ACTIVITY = now
                # Track MSG:3028 (title added) to surface forward progress.
                if "MSG:3028" in line:
                    titles_added += 1
                    # Once title enumeration has started, post-enumeration
                    # work (CINFO/TINFO emission, CSS finalization, audio-
                    # stream probing) can run silently for well over the 60s
                    # PROBE_STALL_S cap — particularly on DVDs where macOS
                    # races to auto-mount the UDF volume. The old behavior
                    # killed makemkvcon mid-finish, dropped the already-found
                    # titles (no TINFO yet → return None at line 487), and
                    # retried from scratch in a tight loop (Chappelle's Show
                    # S01D1, 2026-05-30: 8 titles enumerated every probe,
                    # never made it to TINFO). Bumping the stall budget once
                    # we have proof the disc is readable lets the same probe
                    # finish on the original attempt.
                    if stall_s < 180:
                        stall_s = 180
                if not slow_mode and "MSG:3042" in line:
                    # Corrupt IFO — switch to patient mode for the VOB scan.
                    slow_mode = True
                    stall_s = 300        # 5 min of silence allowed mid-scan
                    hard_s = max(hard_s, 1800)  # 30 min total cap
                    print(f"  {C.YLW}disc has corrupt IFO — falling back to "
                          f"VOB scan, this is slow but should finish{C.R}",
                          flush=True)
        # Reap the process so it doesn't linger as a zombie holding the drive.
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: pass

        # If we killed mid-probe with zero useful output, signal "no disc"
        # back to the caller so wait_for_disc loops and retries.
        if killed and not any(line.startswith(("CINFO:", "TINFO:")) for line in lines):
            return None, None, None, None
        if proc.returncode not in (0, None) and not lines:
            return None, None, None, None

        cinfo, titles, msgs, rdisk = {}, {}, [], None
        msg_codes = set()
        for line in lines:
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
        return (cinfo, titles, msgs), msg_codes, rdisk, proc

    result, codes, rdisk, _ = _run_once()
    # macOS auto-mounts the disc's UDF/ISO volume a few seconds after the drive
    # is released, and the mounted volume holds the device exclusively. With the
    # volume mounted, makemkvcon can't open disc:0: sometimes it returns 0 titles
    # (occasionally with MSG 5010, sometimes — seen on a Batman BD-ROM
    # 2026-05-28 — silently), but just as often it produces no output at all and
    # the stall watchdog kills it, so _run_once returns None. The original code
    # ran the unmount-retry only on a non-None 0-title result and bailed on None
    # *before* the recovery, which made the recovery unreachable in the common
    # held-device case (killed probe → None) — the WALL-E UHD rip on 2026-05-29
    # polled forever at "0 titles" with the volume mounted the whole time and the
    # unmount never fired. So trigger recovery on EITHER a None result or a
    # 0-title result whenever an optical volume is currently mounted.
    # unmount_blocking_volume falls back to drutil + a `mount` scan when rdisk is
    # None (it is None on the killed path), so rdisk is a best-effort hint, not a
    # requirement. _optical_mounts() gates this so we don't churn on AACS-locked
    # or no-media discs (nothing mounted → nothing to unmount).
    if (result is None or not result[1]) and _optical_mounts():
        if unmount_blocking_volume(rdisk):
            result, codes, rdisk, _ = _run_once()
    if result is None:
        return None
    cinfo, titles, msgs = result
    if titles:
        return {"cinfo": cinfo, "titles": titles, "msgs": msgs}
    if msgs_indicate_aacs_lock(msgs):
        return {"error": "AACS_LOCKED", "msgs": msgs}
    # No titles + no AACS lock — surface the specific failure mode if the
    # MSG codes show physical-disc trouble. This stops users from sitting
    # through a "polling drive..." retry loop without knowing the disc is
    # the problem, not the script.
    # 3042 = IFO file corrupt (DVD authoring damage or scratched disc)
    # 2003 = SCSI error (tray-open / medium-not-present mid-read)
    # 5010 = Failed to open disc (umbrella final-fail)
    diag_codes = codes & {3042, 2003, 5010}
    if diag_codes:
        return {
            "error": "PHYSICAL_DISC_TROUBLE",
            "codes": sorted(diag_codes),
            "msgs": msgs,
        }
    return None

def scan_titles_json(args) -> int:
    """Probe the disc and print its title table as JSON, then exit. Used by the
    burndvd wrapper's interactive pre-flight to notice a disc with two or more
    feature-length titles (a likely double feature) and ask the user. Prints a
    JSON array of {id, dur_s, dur, size, name, feature} to stdout; an empty
    array on any probe failure (no disc, AACS lock, physical trouble) so the
    caller can just fall through to normal single-movie handling. probe_disc
    prints live progress to stdout; redirect that to stderr so stdout carries
    only the JSON the wrapper parses."""
    import contextlib
    with contextlib.redirect_stdout(sys.stderr):
        info = probe_disc(args)
    titles = []
    if isinstance(info, dict) and "titles" in info:
        for tid in sorted(info["titles"].keys()):
            t = info["titles"][tid]
            dur_s = parse_duration(t.get(9, "0:00:00"))
            titles.append({
                "id": tid,
                "dur_s": dur_s,
                "dur": fmt_dur(dur_s),
                "size": t.get(10, "?"),
                "name": t.get(2) or t.get(27) or "",
                "feature": dur_s >= MOVIE_FEATURE_MIN_S,
            })
    print(json.dumps(titles))
    return 0

def disc_label(info: dict) -> str:
    c = info.get("cinfo", {})
    return c.get(2) or c.get(32) or c.get(30) or c.get(1) or "(unknown)"

def disc_n_from_label(info: dict) -> int | None:
    # The old `\bdisc\s*[-_:]?\s*(\d+)\b` regex matched `\b` word boundaries,
    # which can fire inside titles that merely *start* with "disc" — e.g.
    # an audit-flagged "Disclosure_1994" / "Disco_1994" / "Discworld_8"
    # pattern could in principle yield a phantom disc_n that then overrides
    # the queue's real disc number. Tighten via negative lookbehind so
    # "disc" must be preceded by a non-letter (start-of-string, space,
    # underscore, dash, etc.). All real volume labels we've seen
    # (SOUTHPARK7_DISC2, CS_S2_D1, "South Park Season 5 - Disc 2") still
    # match cleanly. (audit #15, 2026-05-31)
    m = re.search(r"(?<![A-Za-z])disc[\s_:-]*(\d+)\b",
                  disc_label(info), re.IGNORECASE)
    return int(m.group(1)) if m else None

# Floor above which a movie title is "feature-length" — used only by --scan
# to flag candidate features so the burndvd wrapper can notice a disc with two
# (or more) feature-length titles and ask whether it's a double feature. Set
# generous (45 min) to catch short B-movies / grindhouse features; the wrapper
# prompt defaults to "no", so a single movie + a long making-of just costs the
# user one extra Enter.
MOVIE_FEATURE_MIN_S = 2700
TV_MIN_DUR_S = 600
# Floor below which MakeMKV titles are noise (menu reels, transitions, FBI
# warnings, language stubs). Raised from 60 → 120 on 2026-05-26 after Power
# of the Dog UHD rip kept two ~130-260MB bumpers; 60s let those through.
# Between this and TV_MIN_DUR_S → routed to Extras/ as bonus content.
EXTRAS_MIN_DUR_S = 120

# A "play all" title concatenates every episode on the disc into one long
# title. Its hallmark is a near-exact identity a real episode can never meet:
# its runtime equals the SUM of all the other episode-length titles. Left in,
# MakeMKV's title order numbers it as a phantom episode and shoves every later
# episode's number up by one. (South Park S2D1, 2026-05-29: 9 eps + 1 play-all
# sitting at title index t03 → E04 became the blob and E05-E10 were each off by
# one.) checkrip catches this after the fact; this skips it at rip time.
PLAY_ALL_SUM_TOL = 0.08    # |dur - sum(others)| / sum(others) must be within this
PLAY_ALL_MIN_RATIO = 1.8   # play-all must be >= this x the longest other title
# Was 3 (rationale: avoid false positives on 2-title discs where a long title
# happens to be 1.8x the short one). Lowered to 2 (audit #8, 2026-05-31):
# 2-episode bonus discs and finale 2-parters have a play-all blob that the
# old threshold missed, producing a phantom E03 numbered as if it were a
# real episode. The MIN_RATIO duration check is the actual hard signal —
# a real episode never matches >=1.8x the longest other — so dropping the
# count floor is safe.
PLAY_ALL_MIN_PARTS = 2     # and be a compilation of at least this many other titles

def play_all_title_ids(info: dict) -> set:
    """Title ids judged to be a 'play all' compilation and worth skipping.
    Identifies a title whose duration equals the sum of SOME SUBSET (>= 2) of
    the other episode-length titles. Generalizes the original "sum of all
    others" check to also catch BDs that include a play-all spanning only
    one cut (broadcast vs uncut), e.g. South Park S19 D2 t02 = 1h51m =
    5 x 22min broadcast episodes alongside 5 uncut alternates.

    A genuinely long episode (e.g. double-length finale) cannot match a
    subset sum that big with the discrete part-lengths present, so it is
    never mistaken for a play-all."""
    durs = [(tid, parse_duration(t.get(9, "0:00:00"))) for tid, t in info["titles"].items()]
    durs = [(tid, d) for tid, d in durs if d >= TV_MIN_DUR_S]
    out = set()
    if len(durs) < PLAY_ALL_MIN_PARTS + 1:
        return out

    def proper_subset_sum_matches(target: int, parts: list[int], min_parts: int) -> bool:
        """True if any subset of `parts` (size >= min_parts) sums to within
        PLAY_ALL_SUM_TOL of target. Brute-force over 2^n subsets; capped
        at n<=20 for safety."""
        n = len(parts)
        if n < min_parts or target <= 0 or n > 20:
            return False
        tol = max(int(target * PLAY_ALL_SUM_TOL), 30)
        for mask in range(1, 1 << n):
            if bin(mask).count("1") < min_parts:
                continue
            s = 0
            for i in range(n):
                if mask & (1 << i):
                    s += parts[i]
                    if s > target + tol:
                        break
            if abs(s - target) <= tol:
                return True
        return False

    for tid, d in durs:
        others = [od for otid, od in durs if otid != tid]
        if len(others) < PLAY_ALL_MIN_PARTS:
            continue
        if d < PLAY_ALL_MIN_RATIO * max(others):
            continue  # not long enough to be a compilation
        # Original behavior — duration ≈ sum of ALL others (keeps the 2-part
        # bonus-disc / 2-parter case the previous tuning explicitly preserved).
        s_all = sum(others)
        if s_all > 0 and abs(d - s_all) / s_all <= PLAY_ALL_SUM_TOL:
            out.add(tid)
            continue
        # Generalization — duration ≈ sum of a SUBSET of others (S19D2 case
        # where play-all spans broadcast cuts but not the alt-cut alternates).
        # Tightened to >=3 parts here to avoid flagging double-length finales
        # whose duration happens to ≈ 2 × normal-episode subset.
        if proper_subset_sum_matches(d, others, min_parts=3):
            out.add(tid)
    return out

def select_titles(item: QueueItem, info: dict) -> list[int]:
    durs = [(tid, parse_duration(t.get(9, "0:00:00"))) for tid, t in info["titles"].items()]
    if not durs: return []
    if item.type == "double-feature":
        # Titles were pinned interactively by the burndvd wrapper at scan time.
        # Keep only those still present on the disc (defensive against a probe
        # that enumerates titles differently than the scan did).
        present = set(info["titles"].keys())
        return [f["title_id"] for f in item.features if f["title_id"] in present]
    if item.type == "movie":
        tid, dur = max(durs, key=lambda x: x[1])
        return [tid] if dur > 0 else []
    play_all = play_all_title_ids(info)
    return [tid for tid, dur in durs if dur >= TV_MIN_DUR_S and tid not in play_all]

def log_title_audit(args, item: QueueItem, info: dict, title_ids: list[int]):
    # Persistent audit trail of the probe's full title table and the keep/skip
    # decision for each title. Added 2026-05-28 after a Rick and Morty S6 rip
    # dropped title index 8 (sub-floor extra) with no record of its duration —
    # the selection happened in-memory and nothing logged why a title was
    # excluded. Writes both to console (captured in the per-disc stdout log)
    # and to ripqueue-state.log so the reason survives across runs.
    chosen = set(title_ids)
    play_all = play_all_title_ids(info) if item.type not in ("movie", "double-feature") else set()
    floor = TV_MIN_DUR_S if item.type != "movie" else 0
    rows = []
    for tid in sorted(info["titles"].keys()):
        t = info["titles"][tid]
        dur_s = parse_duration(t.get(9, "0:00:00"))
        name = t.get(2) or t.get(27) or "(unnamed)"
        size = t.get(10, "?")
        kept = tid in chosen
        if kept:
            reason = "KEEP"
        elif item.type == "movie":
            reason = "skip (not longest title)"
        elif tid in play_all:
            # Tentative at probe time — the duration heuristic FLAGS it, but the
            # empirical frame-compare verdict (CONFIRMED vs SALVAGE) is decided
            # post-rip in rip() and logged there. (2026-07-10 DBZ S7D4.)
            reason = "skip (play-all?)"
        elif dur_s < floor:
            tier = "extra" if dur_s >= EXTRAS_MIN_DUR_S else "noise"
            reason = f"skip <{floor}s TV floor ({tier})"
        else:
            reason = "skip"
        rows.append((tid, dur_s, name, size, kept, reason))
        print(f"  {C.D}t{tid:02d}  {fmt_dur(dur_s):>7}  {size:>9}  "
              f"{'KEEP' if kept else 'skip'}  {name}{C.R}")
    # Compact one-line-per-title record in the durable state log.
    disc_n = disc_n_from_label(info) or "?"
    for tid, dur_s, name, size, kept, reason in rows:
        append_log(args, f"TITLE {item.title} S{item.season:02d}D{disc_n} "
                         f"t{tid:02d} {fmt_dur(dur_s)} ({dur_s}s) {size} "
                         f"{reason} | {name}")

def ffprobe_dur_s(path: str) -> float:
    # Actual container duration of a landed file. Returns 0.0 if ffprobe is
    # missing or the read fails — logging is best-effort and must never abort
    # a completed rip.
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else 0.0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0

def log_final_durations(args, item: QueueItem, disc_n, files: list[str]):
    # Post-rip audit: ffprobe each file that actually landed in the target and
    # record its real duration + on-disk size. Complements log_title_audit's
    # pre-rip probe figures — catches truncated rips or selector/duration drift
    # between what MakeMKV reported and what was written. Best-effort; failures
    # are logged as a 0:00 line rather than raised.
    for path in files:
        p = Path(path)
        dur_s = ffprobe_dur_s(path)
        try:
            size_gb = p.stat().st_size / 1e9
        except OSError:
            size_gb = 0.0
        append_log(args, f"FINAL {item.title} S{item.season:02d}D{disc_n} "
                         f"{fmt_dur(dur_s)} ({int(dur_s)}s) {size_gb:.2f}GB | {p.name}")

# -------- empirical play-all verification (frame compare) --------
# Homebrew ffmpeg. Hard-coded (not bare "ffmpeg") because ripqueue runs under a
# `script + caffeinate` wrapper whose PATH may not include /opt/homebrew/bin.
FFMPEG_BIN = "/opt/homebrew/bin/ffmpeg"

# Seconds into the title to sample frames when confirming/refuting a play-all
# flag. Clamped below the shortest kept episode so the reference always has a
# frame there; a fractional fallback covers very short titles / test fixtures.
PLAY_ALL_VERIFY_OFFSETS = (120.0, 300.0, 600.0)

def _frame_md5(path: str, offset_s: float) -> str | None:
    """md5 of the single decoded video frame at offset_s (seconds), or None on
    any ffmpeg error / missing file / unparseable output. Callers treat None as
    a verification failure and FAIL SAFE (salvage, never delete). Cheap: one
    frame, decode stops after `-frames:v 1`."""
    try:
        r = subprocess.run(
            [FFMPEG_BIN, "-v", "error", "-ss", f"{offset_s:g}", "-i", path,
             "-frames:v", "1", "-f", "md5", "-"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"MD5=([0-9a-fA-F]+)", r.stdout)
    return m.group(1).lower() if m else None

def verify_play_all(flagged_path: str, first_episode_path: str,
                    shortest_ep_dur_s: float) -> tuple[bool, str]:
    """Empirically CONFIRM or REFUTE a heuristic 'play-all' flag by comparing
    decoded frames of the flagged title against the first kept episode.

    A genuine play-all compilation begins with the first episode's content, so
    single frames decoded at the SAME offset produce identical md5s (same disc =
    same encode = bit-identical decode). A unique bonus featurette that merely
    happens to satisfy the play-all DURATION heuristic (the 2026-07-10 DBZ S7D4
    t02 case) decodes to DIFFERENT frames.

    Returns (confirmed, detail):
      confirmed=True  -> every probed frame matched -> real play-all, safe to drop.
      confirmed=False -> a frame differed, OR ffmpeg/file failed -> FAIL SAFE:
                         the caller salvages the file rather than deleting it.

    Offsets are clamped below shortest_ep_dur_s so the reference episode always
    has a frame there; falls back to fractional offsets for very short titles."""
    usable = [o for o in PLAY_ALL_VERIFY_OFFSETS if o < shortest_ep_dur_s - 1]
    if not usable and shortest_ep_dur_s > 2:
        # Episode shorter than the smallest fixed offset (very short titles /
        # test fixtures): sample fractionally so all three stay in range.
        usable = [round(shortest_ep_dur_s * f, 2) for f in (0.25, 0.5, 0.75)]
    if not usable:
        return False, "no usable in-range offset (fail-safe -> salvage)"
    for o in usable:
        a = _frame_md5(flagged_path, o)
        b = _frame_md5(first_episode_path, o)
        if a is None or b is None:
            return False, f"ffmpeg/file failure at {o:g}s (fail-safe -> salvage)"
        if a != b:
            return False, f"frame mismatch at {o:g}s (play-all flag refuted)"
    off_str = ",".join(f"{o:g}s" for o in usable)
    return True, f"all {len(usable)} frames match ({off_str}) — play-all CONFIRMED"

# -------- target paths --------
def compute_target_dir(item: QueueItem) -> Path:
    root = Path(item.target_root)
    if item.type == "movie":
        return root
    return root / f"Season {item.season:02d}"

@contextlib.contextmanager
def season_dir_lock(target_dir: Path, what: str = "rename"):
    """Cross-process exclusive flock on <season_dir>/.burndvd.lock. Serializes
    the rename DECISION across parallel ripqueue processes targeting the same
    season. Without it, two rips (e.g. S6D1 on drive A, S6D2 on drive B, both
    finishing makemkvcon around the same time) can both fs-scan an empty
    season dir, both compute start_ep=1, and silently overwrite each other's
    SxxEyy files when their async NAS transfers race.

    Held only during the FAST decision phase (starting_ep + collision scan +
    placeholder creation), NOT during makemkvcon read or actual NAS transfers.
    Subsequent rips see the 0-byte placeholder files in their dir scan and
    pick the next free episode number; the real moves overwrite the
    placeholders later (move_with_progress unlinks 0-byte dst pre-move).

    Different seasons (different target_dir) use different lock files and
    don't contend, so cross-season parallelism is unaffected. The lock file
    is a hidden flag file inside the season dir; Plex/Jellyfin ignore
    dotfiles."""
    target_dir.mkdir(parents=True, exist_ok=True)
    lock_path = target_dir / ".burndvd.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        # Blocking acquire — same-season parallel rips genuinely need to
        # serialize here. In practice the held duration is sub-second
        # (fs scan + placeholder touches), so callers don't notice.
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try: fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception: pass
        try: os.close(fd)
        except Exception: pass

# -------- episode counter (overwrite-safe) --------
def ep_key(item: QueueItem) -> str:
    return f"{item.title}||S{item.season:02d}"

def starting_ep(item: QueueItem, disc_n: int, state: dict) -> int:
    counts = state.get("disc_episode_counts", {}).get(ep_key(item), {})
    prior = sum(int(v) for k, v in counts.items() if int(k) < disc_n)
    state_based = item.episode_start + prior
    # Filesystem-aware fallback: if state.json has no record for this season
    # (e.g. fresh state, multi-session resume, or burndvd reinvocation after
    # a stoprip), scan the target season dir for existing SxxEyy.mkv files
    # and start at highest+1. Prevents re-numbering over already-landed
    # episodes from a previous run that lost state.
    if not counts:
        season_dir = compute_target_dir(item)
        try:
            highest = 0
            pat = re.compile(rf"S0*{item.season}E(\d+)")
            if season_dir.exists():
                for entry in season_dir.iterdir():
                    if entry.suffix.lower() != ".mkv":
                        continue
                    # Ignore 0-byte files: these are slot-claim placeholders
                    # (season_dir_lock) or orphans left by an aborted/rolled-
                    # back move. Counting them would compute the wrong next
                    # episode number on a re-rip (DBZ S6, 2026-07-07).
                    try:
                        if entry.stat().st_size == 0:
                            continue
                    except OSError:
                        continue
                    m = pat.search(entry.name)
                    if m:
                        highest = max(highest, int(m.group(1)))
            if highest >= state_based:
                return highest + 1
        except OSError:
            pass
    return state_based

def record_episode_count(item: QueueItem, disc_n: int, count: int, state: dict):
    state.setdefault("disc_episode_counts", {}).setdefault(ep_key(item), {})[str(disc_n)] = int(count)

# -------- ripping --------
def _staging_write_age(staging: Path) -> float:
    """Seconds since the newest staged .mkv was last written, or a large number
    if nothing's been written yet. Lets the progress display tell a real stall
    (makemkvcon wedged) apart from a merely frozen PRGV counter while bytes are
    still flowing. Single-title BD rips sit at PRGV 0% during the initial read
    even though the .mkv grows steadily, which used to show a bogus stall tag."""
    try:
        newest = max((p.stat().st_mtime for p in staging.rglob("*.mkv")),
                     default=0.0)
    except OSError:
        return 1e9
    return time.time() - newest if newest else 1e9

def _draw_progress(start: float, total_v: int, mx: int, cur_task: str, stall_age_s: float):
    pct = 100.0 * total_v / mx if mx else 0
    el = time.time() - start
    eta = (el * (100 - pct) / pct) if pct > 1 else 0
    bar = render_bar(pct/100)
    stall_tag = f" {C.YLW}stall {int(stall_age_s)}s{C.R}" if stall_age_s > 30 else ""
    clock = time.strftime("%H:%M:%S")
    sys.stdout.write(f"\r{C.GRN}{bar}{C.R} {pct:5.1f}%  "
                     f"el {fmt_dur(el)}  ETA {fmt_dur(eta)}  "
                     f"{C.D}{clock}{C.R}  "
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

def _title_complete(staged: Path, info: dict) -> bool:
    """True if a staged .mkv is a fully-ripped title (not the truncated one that
    makemkvcon was mid-write on when it was killed). Prefers ffprobe duration vs
    the probe's expected duration; falls back to on-disk size vs the probe's
    expected byte count (TINFO code 11) when ffprobe can't read the file. If the
    probe gave neither reference, keep the file only if ffprobe still reports a
    duration (better than silently discarding a good rip)."""
    tnum = title_num_from_filename(staged.name)
    t = info.get("titles", {}).get(tnum, {}) if tnum is not None else {}
    exp_dur = parse_duration(t.get(9, "0:00:00"))
    try:
        exp_bytes = int(t.get(11, "0") or "0")
    except ValueError:
        exp_bytes = 0
    try:
        act_bytes = staged.stat().st_size
    except OSError:
        act_bytes = 0
    act_dur = ffprobe_dur_s(str(staged))
    dur_ok = exp_dur > 0 and act_dur >= exp_dur - 3
    size_ok = exp_bytes > 0 and act_bytes >= exp_bytes * 0.97
    if exp_dur == 0 and exp_bytes == 0:
        return act_dur > 0
    return dur_ok or size_ok

def _salvage_and_fail(args, item: QueueItem, info: dict, disc_n: int,
                      staging: Path, target_dir: Path, reason: str):
    """A rip failed hard (stall-kill / non-zero exit / partial save-summary) but
    makemkvcon may have written one or more COMPLETE titles before dying — the
    common damaged-disc case where early titles rip clean and one bad title
    hangs. Quarantine the complete titles under <target>/_partial/ with TRACEABLE
    t-names (never positional SxxEyy — a missing middle title would renumber
    every later episode, the recurring misorder trap) so a 1h+ rip isn't thrown
    away, then return failure so the queue still treats the disc as incomplete
    and the operator re-rips the missing title(s). Returns (False, reason)."""
    try:
        staged = sorted(staging.glob("*.mkv"))
    except OSError:
        staged = []
    complete, dropped = [], []
    for p in staged:
        (complete if _title_complete(p, info) else dropped).append(p)
    if not complete:
        shutil.rmtree(staging, ignore_errors=True)
        return False, f"{reason} (no complete titles to salvage)"

    partial_dir = target_dir / "_partial"
    saved_names = []
    try:
        partial_dir.mkdir(parents=True, exist_ok=True)
        for p in complete:
            tnum = title_num_from_filename(p.name)
            tag = f"t{tnum:02d}" if tnum is not None else p.stem
            if item.type == "movie":
                base = movie_filename(item, disc_n)[:-4]
                name = f"{base} - {tag}.mkv"
            else:
                name = (f"{sanitize(item.title)} - "
                        f"S{item.season:02d}D{disc_n} - {tag}.mkv")
            dest = partial_dir / name
            if dest.exists() and dest.stat().st_size > 0 and not args.overwrite:
                print(f"{C.YLW}  salvage: {dest.name} already present — skipping{C.R}")
                try: p.unlink()
                except OSError: pass
                continue
            move_with_progress(p, dest, label=f"salvaging {tag}")
            saved_names.append(dest.name)
    except OSError as e:
        # Quarantine itself failed (mount drop): keep staging so nothing is lost.
        return False, (f"{reason}; salvage of {len(complete)} complete title(s) "
                       f"FAILED ({e}) — staging PRESERVED at {staging}")

    shutil.rmtree(staging, ignore_errors=True)
    if dropped:
        print(f"{C.YLW}  salvage: dropped {len(dropped)} truncated/partial "
              f"title(s): {', '.join(p.name for p in dropped)}{C.R}")
    print(f"{C.GRN}  SALVAGED {len(saved_names)} complete title(s) -> "
          f"{partial_dir}{C.R}")
    print(f"{C.YLW}  Traceable t-names (NOT SxxEyy) — promote manually after "
          f"re-ripping the missing title(s).{C.R}")
    return False, (f"{reason}; SALVAGED {len(saved_names)} complete title(s) to "
                   f"{partial_dir} (traceable t-names; {len(dropped)} truncated "
                   f"dropped; promote manually)")

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
        # Briefly hold the season lock so this scan + start_ep computation
        # is consistent w.r.t. parallel rips of the same season (otherwise
        # another rip could be mid-claim and we'd miss its slots).
        with season_dir_lock(target_dir, what="precheck"):
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
    # Account for parallel in-flight rips on other drives. The single-rip
    # `free >= local_need` check passes for both rips of a 220GB parallel
    # UHD pair when ~200GB is free (audit follow-up 2026-05-31), then they
    # collide mid-rip when the SSD fills. Staging dirs are named
    # `<epoch>-<pid>`; only those whose owner PID is still alive represent
    # rips that will grow into the free space we're about to commit to.
    active_other_rips = 0
    try:
        for sub in LOCAL_STAGING_BASE.iterdir():
            if not sub.is_dir():
                continue
            m = re.match(r"\d+-(\d+)$", sub.name)
            if not m:
                continue
            owner_pid = int(m.group(1))
            if owner_pid == os.getpid():
                continue
            # PID alive? signal 0 doesn't actually deliver, just checks.
            try:
                os.kill(owner_pid, 0)
                active_other_rips += 1
            except (ProcessLookupError, PermissionError):
                pass  # owner exited; staging is abandoned, no future growth
    except OSError:
        pass
    # Reserve a full local_need-equivalent (max 110GB UHD ceiling, we don't
    # know each rip's format from outside) per active other rip, on top of
    # this rip's local_need.
    required_with_parallel = local_need + (active_other_rips * 110)
    if local_free_gb < local_need:
        return False, (f"low disk space on local staging: {local_free_gb:.1f}GB free at "
                       f"{LOCAL_STAGING_BASE}, need {local_need}GB for {item.format}")
    if local_free_gb < required_with_parallel:
        return False, (f"low disk space on local staging vs in-flight rips: "
                       f"{local_free_gb:.1f}GB free, need {required_with_parallel}GB "
                       f"({local_need}GB for this {item.format} + ~110GB per active "
                       f"in-flight rip × {active_other_rips})")

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

    # --cache=1024: larger read buffer than makemkvcon's default (~128MB).
    # Helps recover from marginal sectors (the S2D1 stall-at-22% failure
    # mode, 2026-05-30) and gives a bigger window for LibreDrive's CSS
    # handshake to land on flaky Comedy Central DVD pressings.
    cmd = [args.makemkvcon, "-r", "--progress=-same", "--noscan",
           "--cache=1024",
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
                _draw_progress(start, total_v, mx, cur_task,
                               min(stall_age, _staging_write_age(staging)))
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
                _draw_progress(start, total_v, mx, cur_task,
                               min(now - last_progress_time,
                                   _staging_write_age(staging)))
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

    # Failure triage. Every hard-failure path used to rmtree(staging) on the
    # spot, discarding any titles makemkvcon had already finished before it died
    # (South Park S22 D1, 2026-06-02: ~5 episodes lost after a 1h rip). Now each
    # one funnels through _salvage_and_fail, which quarantines the complete
    # titles before wiping staging and still returns failure.
    fail_reason = None
    if stalled:
        fail_reason = (f"stalled at PRGV total={last_progress_total}/{mx}; "
                       f"last MSGs: {' | '.join(msgs[-3:])}")
    elif proc.returncode != 0:
        tail = " | ".join(msgs[-5:]) or "(no MSG output)"
        fail_reason = f"makemkvcon exit {proc.returncode}; last MSGs: {tail}"
    else:
        # makemkvcon exits 0 even when titles fail. Verify save summary.
        summary = parse_save_summary(msgs)
        if summary is None:
            fail_reason = (f"no save-summary MSG found; "
                           f"last MSGs: {' | '.join(msgs[-5:])}")
        else:
            saved, failed = summary
            if saved == 0:
                fail_reason = (f"makemkvcon save summary: 0 saved, {failed} "
                               f"failed; last MSGs: {' | '.join(msgs[-5:])}")
            elif failed > 0:
                fail_reason = (f"makemkvcon save summary: {saved} saved, "
                               f"{failed} failed (partial)")

    if fail_reason is not None:
        return _salvage_and_fail(args, item, info, disc_n, staging,
                                 target_dir, fail_reason)

    rips = sorted(staging.glob("*.mkv"))
    if not rips:
        shutil.rmtree(staging, ignore_errors=True)
        return False, "no .mkv files in staging despite save-summary success"

    # Deferred NAS moves: collect (src, dst, label) jobs instead of copying
    # inline, so the caller can eject + free the drive the moment staging is
    # done and run the SMB copy in the background. --sync-move copies inline
    # (legacy: drive held until the copy finishes).
    move_jobs: list = []
    def _emit_move(src: Path, dst: Path, label: str):
        if getattr(args, "sync_move", False):
            move_with_progress(src, dst, label=label)
        else:
            move_jobs.append((src, dst, label))

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
            _emit_move(src, dest, f"moving extra t{tnum:02d}" if tnum is not None else "moving extra")
            finals.append(str(dest))
        print(f"{C.GRN}  {len(extra_srcs)} extra(s) -> {extras_dir}{C.R}")

    # A completed rip must never be lost to a transiently unwritable target.
    # The NAS mount can drop mid-rip (e.g. a SMB/auth blip during a long rip),
    # which previously crashed the move with an unhandled PermissionError and
    # stranded the staged files. Verify writability, retry (~10 min; mounts
    # recover), and on persistent failure PRESERVE staging and bail cleanly.
    def _dest_writable() -> bool:
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            probe = target_dir / f".writeprobe-{os.getpid()}"
            probe.write_text("ok"); probe.unlink()
            return True
        except OSError:
            return False

    if not _dest_writable():
        for attempt in range(1, 13):  # ~10 min of retries
            print(f"{C.YLW}  Target {target_dir} not writable "
                  f"(attempt {attempt}/12) — mount may be reconnecting; "
                  f"retrying in 50s. Staged rip is safe at {staging}.{C.R}")
            time.sleep(50)
            if _dest_writable():
                print(f"{C.GRN}  Target writable again; continuing move.{C.R}")
                break
        else:
            return False, (f"target not writable after retries — RIP PRESERVED at "
                           f"{staging}; fix the mount and re-run to finish "
                           f"(staging was NOT deleted)")

    finals: list[str] = []
    if item.type == "movie":
        # Largest staged file is the main feature; everything else >=60s is an extra.
        main = max(rips, key=lambda p: p.stat().st_size)
        target_path = target_dir / movie_filename(item, disc_n)
        if target_path.exists() and not args.overwrite:
            # Never rmtree a COMPLETED rip over a naming dispute (salvage
            # doctrine, post-2026-06-02). Preserve staging; caller resolves.
            print(f"{C.YLW}  Collision: {target_path} exists — staging PRESERVED "
                  f"at {staging}.{C.R}")
            return False, (f"would overwrite {target_path} — RIP PRESERVED at "
                           f"{staging} (not deleted; move/rename manually)")
        _emit_move(main, target_path, "moving main title")
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
        # A play-all title clears the episode floor (it's the longest title on
        # the disc), so duration alone would number it as a phantom episode and
        # shove every later episode up one (S2D1/S2D2/S3D1, 2026-05-29). Drop the
        # play-all title ids here so select_titles' decision actually takes effect
        # on the TV path; the blob is left in staging and cleaned up below.
        play_all = play_all_title_ids(info)
        episode_rips = [p for p in rips
                        if _src_dur_s(p) >= TV_MIN_DUR_S
                        and title_num_from_filename(p.name) not in play_all]
        extras_rips  = [p for p in rips if _src_dur_s(p) <  TV_MIN_DUR_S]

        # Empirical play-all verification (2026-07-10 DBZ S7D4 incident).
        # play_all_title_ids() is a cheap DURATION test: a title whose runtime
        # ≈ the sum of a subset of the other episode-length titles is assumed to
        # be a "play all" compilation and dropped — its staged file is left out
        # of episode_rips AND extras_rips (it's >= TV_MIN_DUR_S), so it falls
        # through to the staging rmtree below and is silently DELETED. But a
        # duration collision is not proof. Dragon Ball Z Season 7 Disc 4 held
        # t00 20m51s + t01 20m35s (= 41m26s) and a t02 bonus featurette of
        # 38m24s — within 7.3% of that sum and > 1.8× each episode, so the
        # heuristic FLAGGED t02 and would have deleted it. t02 was actually a
        # UNIQUE 38-minute featurette (its frames differ from the kept episodes);
        # Trevor caught it live and we salvaged via hardlink. So before honoring
        # the flag we now VERIFY it empirically against the staged files: a real
        # play-all begins with the first episode's content, so single frames
        # decoded at the same offsets have identical md5s (same disc = same
        # encode). CONFIRMED -> drop as before. REFUTED, or any ffmpeg/file
        # error (FAIL SAFE) -> SALVAGE the file to Season NN/extras/ as a bonus,
        # never consuming an episode number. Cheap: only flagged titles (rare),
        # 3 frame extracts each, ~seconds. Same doctrine as the alt-playlist
        # dedup removed 2026-05-27: a lost episode/featurette costs far more than
        # a stray Jellyfin entry, so on any doubt we KEEP.
        playall_srcs = [p for p in rips
                        if _src_dur_s(p) >= TV_MIN_DUR_S
                        and title_num_from_filename(p.name) in play_all]
        if playall_srcs:
            # Reference = first kept episode's staged file (rips is sorted, so
            # episode_rips is in title order → [0] is the lowest-numbered kept
            # episode). If EVERY title got flagged there is no reference to
            # compare against, so we can't confirm — fail safe and salvage all.
            ref = str(episode_rips[0]) if episode_rips else None
            shortest_ep = min((_src_dur_s(p) for p in episode_rips), default=0.0)
            salvaged_playall = []
            disc_lbl = f"S{item.season:02d}D{disc_n}"
            for src in playall_srcs:
                tnum = title_num_from_filename(src.name)
                if ref is None:
                    confirmed, detail = False, ("no kept episode to compare "
                                                "against (fail-safe -> salvage)")
                else:
                    confirmed, detail = verify_play_all(str(src), ref, shortest_ep)
                if confirmed:
                    print(f"  {C.D}t{tnum:02d}: play-all CONFIRMED by frame "
                          f"compare — {detail}; dropping{C.R}")
                    append_log(args, f"PLAYALL {item.title} {disc_lbl} "
                                     f"t{tnum:02d} skip (play-all CONFIRMED by "
                                     f"frame compare) | {detail}")
                    # Leave in staging → rmtree'd below (unchanged behavior).
                else:
                    print(f"  {C.YLW}t{tnum:02d}: play-all flag REFUTED by frame "
                          f"compare — {detail}; SALVAGING to extras/{C.R}")
                    salvaged_playall.append(src)
            if salvaged_playall:
                # Reuse _move_extras: same NAS-drop-safe _emit_move path and
                # finals[] tracking as ordinary extras. Distinct name marks it
                # as an un-verified-away play-all bonus for later human review.
                _move_extras(
                    salvaged_playall, target_dir / "extras",
                    lambda src, tnum: f"{sanitize(item.title)} - {disc_lbl} bonus "
                                      f"t{tnum:02d} (unverified play-all flag).mkv",
                )
                for src in salvaged_playall:
                    tnum = title_num_from_filename(src.name)
                    append_log(args, f"PLAYALL {item.title} {disc_lbl} "
                                     f"t{tnum:02d} SALVAGE (play-all flag refuted "
                                     f"by frame compare) → extras/{sanitize(item.title)} "
                                     f"- {disc_lbl} bonus t{tnum:02d} "
                                     f"(unverified play-all flag).mkv")

        # Alt-playlist dedup removed 2026-05-27: Spectacular Spider-Man S01
        # (animated, consistent ~23min runtimes, similar bitrates) had real
        # distinct episodes demoted as duplicates under the prior 5s+1%
        # tolerance. False-positive cost (lost episode) >> false-negative
        # cost (one stray Jellyfin entry); rely on the per-title MakeMKV
        # selector for TV box sets.

        _move_extras(
            extras_rips,
            target_dir / "Extras",
            lambda src, tnum: f"{sanitize(item.title)} - S{item.season:02d}D{disc_n} - extra - "
                              f"{('t%02d' % tnum) if tnum is not None else src.stem}.mkv",
        )
        rips = episode_rips
        if not rips:
            print(f"{C.YLW}  No titles >= {TV_MIN_DUR_S}s — extras-only disc.{C.R}")
            if getattr(args, "sync_move", False):
                shutil.rmtree(staging, ignore_errors=True)
                return True, {"elapsed_s": elapsed, "files": finals}
            return True, {"elapsed_s": elapsed, "files": finals,
                          "move_jobs": move_jobs, "staging": str(staging)}
        # Season-dir lock spans the start_ep computation + the file-naming
        # decisions + the placeholder claim. Once this block exits, parallel
        # rips of the same season can safely scan the dir and see our
        # claimed slots as filled, picking higher episode numbers. We do NOT
        # hold this lock during the prior makemkvcon read (long), only this
        # fast decide-and-claim phase. See season_dir_lock docstring.
        with season_dir_lock(target_dir, what="rename"):
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
                # Two-pass under the lock: (1) collision check + atomic slot
                # claim for every proposed episode, (2) enqueue the moves.
                # Splitting it lets us roll back the placeholders cleanly if
                # any slot is already taken — without this, a mid-loop
                # FileExistsError leaves orphan 0-byte placeholders that
                # would pollute the season dir and confuse the next rip's
                # filesystem scan.
                planned = [
                    (src, ep,
                     target_dir / f"{sanitize(item.title)} - S{item.season:02d}E{ep:02d}.mkv")
                    for src, ep, _, _, _ in proposed
                ]
                for src, ep, final in planned:
                    if final.exists() and final.stat().st_size > 0 and not args.overwrite:
                        # Preserve the completed rip; a naming collision must
                        # never destroy staged episodes (salvage doctrine).
                        print(f"{C.YLW}  Collision: {final} exists — staging "
                              f"PRESERVED at {staging}.{C.R}")
                        return False, (f"would overwrite {final} — RIP PRESERVED "
                                       f"at {staging} (not deleted; resolve manually)")
                claimed: list[Path] = []
                try:
                    for src, ep, final in planned:
                        # Atomic slot claim: 0-byte placeholder under the
                        # lock so a same-season parallel rip's dir scan sees
                        # this slot as taken and skips to a higher episode
                        # number. move_with_progress unlinks 0-byte dst
                        # before shutil.move, so the real NAS transfer
                        # replaces this transparently later.
                        try:
                            os.close(os.open(str(final),
                                             os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                             0o644))
                            claimed.append(final)
                        except FileExistsError:
                            if not args.overwrite:
                                raise
                            # overwrite mode: caller accepts overwriting.
                except FileExistsError as e:
                    # Drop our own just-created 0-byte placeholders (safe: they
                    # are empty), but PRESERVE the completed rip in staging —
                    # a lost slot race must not destroy ripped episodes.
                    for c in claimed:
                        try: c.unlink()
                        except OSError: pass
                    print(f"{C.YLW}  Slot race lost — staging PRESERVED at "
                          f"{staging}.{C.R}")
                    return False, (f"slot already claimed by parallel rip: {e} — "
                                   f"RIP PRESERVED at {staging} (resolve manually)")
                for src, ep, final in planned:
                    _emit_move(src, final, f"moving episode {ep:02d}")
                    finals.append(str(final))
                record_episode_count(item, disc_n, len(proposed), state)
            else:
                for src, _, tnum, _, _ in proposed:
                    tag = f"t{tnum:02d}" if tnum is not None else src.stem
                    final = target_dir / f"{sanitize(item.title)} - S{item.season:02d} - {tag}.mkv"
                    if final.exists() and not args.overwrite:
                        print(f"{C.YLW}  Collision: {final} exists — staging "
                              f"PRESERVED at {staging}.{C.R}")
                        return False, (f"would overwrite {final} — RIP PRESERVED "
                                       f"at {staging} (not deleted; resolve manually)")
                    _emit_move(src, final, f"moving {tag}")
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

    if getattr(args, "sync_move", False):
        shutil.rmtree(staging, ignore_errors=True)
        return True, {"elapsed_s": elapsed, "files": finals}
    return True, {"elapsed_s": elapsed, "files": finals,
                  "move_jobs": move_jobs, "staging": str(staging)}

def rip_double_feature(args, item: QueueItem, info: dict, state: dict, disc_n: int):
    """Rip a disc holding two (or more) distinct movies. Each feature was pinned
    to a MakeMKV title index by the burndvd wrapper at scan time. We rip each
    one through the ordinary single-title movie path (a synthetic per-feature
    QueueItem), so all the stall/progress/move/retry handling is shared — only
    the foldering differs: each feature lands in its own "<base>/<Name>/<Name>.mkv".
    Returns the same (ok, {"elapsed_s", "files"}) shape as rip(); succeeds if at
    least one feature ripped, so a single bad title doesn't strand the other."""
    base = Path(item.target_root)
    present = set(info["titles"].keys())
    all_files: list[str] = []
    total_elapsed = 0.0
    errors: list[str] = []
    for n, feat in enumerate(item.features, 1):
        tid, name = feat["title_id"], feat["name"]
        if tid not in present:
            msg = f"title t{tid:02d} ({name}) not present on disc"
            print(f"{C.RED}  double-feature: {msg}{C.R}")
            errors.append(msg)
            continue
        sub = QueueItem(title=name, type="movie", discs=1,
                        target_root=str(base / sanitize(name)), format=item.format)
        sub_target = compute_target_dir(sub)
        dur = info["titles"].get(tid, {}).get(9, "?:??:??")
        print(f"\n{C.B}Double feature {n}/{len(item.features)} — "
              f"ripping t{tid:02d} ({dur}) → {name}{C.R}")
        ok, result = rip(args, sub, info, [tid], sub_target, state, disc_n)
        if not ok:
            print(f"{C.RED}  feature '{name}' failed: {result}{C.R}")
            append_log(args, f"FAIL  {item.title} :: {name} t{tid:02d}  {result}")
            errors.append(f"{name}: {result}")
            continue
        all_files.extend(result["files"])
        total_elapsed += result["elapsed_s"]
        append_log(args, f"OK    {item.title} :: {name} t{tid:02d}  "
                         f"{fmt_dur(result['elapsed_s'])}  {len(result['files'])} file(s)")
    if not all_files:
        return False, "all features failed: " + " | ".join(errors)
    return True, {"elapsed_s": total_elapsed, "files": all_files}

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
    # Drive-target via _drive_idx_from_args so multi-drive setups don't blindly
    # eject the first drive (which might be the *other* in-flight rip). The
    # old code hardcoded "disk4" for the diskutil fallback — only correct
    # when the rip's drive happened to enumerate as disk4 that boot.
    drive_idx = _drive_idx_from_args(args)
    disk_node = _drutil_disk_node(drive_idx)
    candidates = []
    if drive_idx is not None:
        candidates.append(["drutil", "-drive", str(drive_idx + 1), "eject"])
        candidates.append(["drutil", "-drive", str(drive_idx + 1), "tray", "eject"])
    else:
        candidates.append(["drutil", "eject", "external"])
        candidates.append(["drutil", "tray", "eject"])
    if disk_node:
        candidates.append(["diskutil", "eject", "force", disk_node])
    for cmd in candidates:
        # timeout=15 (audit #14): a wedged drive/firmware can hang drutil
        # or diskutil indefinitely. Falling through to the next candidate
        # is better than freezing the queue between discs.
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            continue
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
    exits.

    Serialized across parallel rips via a mkdir-based lock at
    ~/.cache/burndvd/subocr.lock — two rips finishing close together
    would otherwise both spawn subocr concurrently, pegging CPU and
    starving the next disc's makemkvcon read on each drive. The
    serialized version uses a bash subshell that mkdir-spinlocks on the
    lockdir (5s poll) then runs subocr; when subocr exits, the trap
    removes the lockdir. macOS doesn't ship flock(1), so mkdir is the
    portable atomic primitive.
    """
    if getattr(args, "no_subocr", False) or not files:
        return
    subocr_bin = Path.home() / ".local" / "bin" / "subocr"
    if not subocr_bin.exists():
        return
    log_dir = Path.home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"subocr-postrip-{int(time.time())}.log"
    import shlex
    lock_dir = Path.home() / ".cache" / "burndvd" / "subocr.lock"
    (lock_dir.parent).mkdir(parents=True, exist_ok=True)
    files_shell = " ".join(shlex.quote(f) for f in files)
    bash_cmd = (
        # Ensure subocr's child tools (ffprobe, mkvextract) are found regardless
        # of how ripqueue itself was launched. Without this, retries launched
        # from non-login shells silently error on every file.
        f"export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH; "
        f"LOCKDIR={shlex.quote(str(lock_dir))}; "
        # Spin until we get the lockdir. Every minute (12×5s), check whether
        # the holder is still alive; if no subocr.py is running, the lock is
        # orphaned (e.g. previous run was SIGKILL'd before its trap could fire)
        # and we reclaim it.
        f"attempt=0; "
        f"while ! mkdir \"$LOCKDIR\" 2>/dev/null; do "
        f"  attempt=$((attempt+1)); "
        f"  if [ $((attempt % 12)) -eq 0 ] && ! pgrep -f 'subocr\\.py.*--post-rip' >/dev/null 2>&1; then "
        f"    rmdir \"$LOCKDIR\" 2>/dev/null; "
        f"  fi; "
        f"  sleep 5; "
        f"done; "
        # Always remove the lockdir, even on signals/crashes. NOTE: no `exec`
        # below — exec replaces this shell with subocr, which means the trap
        # never fires when subocr exits (the bash process is gone). Calling
        # subocr as a child preserves the trap.
        f"trap 'rmdir \"$LOCKDIR\" 2>/dev/null' EXIT INT TERM; "
        f"{shlex.quote(str(subocr_bin))} --post-rip {files_shell}"
    )
    cmd = ["/bin/bash", "-c", bash_cmd]
    with open(log_path, "a") as f:
        # stdin=DEVNULL is required: ripqueue is launched via `script + caffeinate`,
        # whose stdin can be in a state Python's startup can't initialize cleanly,
        # producing "can't initialize sys standard streams" on the venv interpreter.
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                         stdout=f, stderr=subprocess.STDOUT,
                         start_new_session=True)
    print(f"{C.D}  subocr started in background ({len(files)} file(s); log: {log_path.name}){C.R}")

# DEFERRED (2026-07-09 audit, lower severity — not fixed this pass):
#   - busy-race: two rips racing the same drive/slot outside the season lock.
#   - USB-drop-during-wait: BU40N vanishing off USB while wait_for_disc polls.
#   - short-episode routing: sub-TV_MIN_DUR_S real episodes routed to Extras.
#   - durable-log-in-tmp: append_log target under /tmp not persisted across reboot.
def _dir_writable(dirpath: Path) -> bool:
    try:
        dirpath.mkdir(parents=True, exist_ok=True)
        probe = dirpath / f".writeprobe-{os.getpid()}-{threading.get_ident()}"
        probe.write_text("ok"); probe.unlink()
        return True
    except OSError:
        return False

def _await_dir_writable(dirpath: Path, staging) -> bool:
    """Block until `dirpath` is writable, retrying ~10 min (12×50s). Mirrors the
    sync-move path's writability retry (rip()) so a mid-transfer NAS mount drop
    on the BACKGROUND path recovers instead of failing the whole batch. Returns
    True if writable (possibly after retries), False on give-up."""
    if _dir_writable(dirpath):
        return True
    for attempt in range(1, 13):
        print(f"{C.YLW}  Target {dirpath} not writable (attempt {attempt}/12) — "
              f"mount may be reconnecting; retrying in 50s. "
              f"Staged rip safe at {staging}.{C.R}", flush=True)
        time.sleep(50)
        if _dir_writable(dirpath):
            print(f"{C.GRN}  Target writable again; continuing move.{C.R}", flush=True)
            return True
    return False

def _rollback_incomplete_dst(src: Path, dst: Path):
    """Remove a 0-byte slot-claim placeholder or a partially-copied dst for a
    move that did NOT complete. Leaves fully-copied files alone. Prevents a
    re-rip from counting an orphan as a real episode (starting_ep / burndvd
    scan), which mis-numbered DBZ S6 on 2026-07-07."""
    try:
        if not dst.exists():
            return
        try: dsz = dst.stat().st_size
        except OSError: dsz = -1
        try: ssz = src.stat().st_size if src.exists() else -1
        except OSError: ssz = -1
        # 0-byte placeholder, or partial copy (smaller than its source, which
        # still exists because shutil.move only unlinks src after a full copy).
        if dsz == 0 or (ssz >= 0 and 0 <= dsz < ssz):
            dst.unlink()
    except OSError:
        pass

def _run_finalize(args, result, item, disc_n):
    """Execute the deferred NAS moves, clean staging, verify, and kick subocr.
    Runs on a worker thread after the disc is already ejected. On ANY move
    failure, PRESERVE staging and log loudly — a completed rip must never be
    lost to a transient mount drop. Serialized via _FINALIZE_LOCK."""
    staging = result.get("staging")
    jobs = result.get("move_jobs") or []
    with _FINALIZE_LOCK:
        done = 0
        try:
            for i, (src, dst, label) in enumerate(jobs):
                dst = Path(dst)
                if not _await_dir_writable(dst.parent, staging):
                    raise OSError(f"target {dst.parent} not writable after retries")
                move_with_progress(Path(src), dst, label=label)
                done = i + 1
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
        except BaseException as e:
            # Roll back the failed job's partial dst + every unattempted job's
            # 0-byte slot-claim placeholder so a re-rip numbers correctly and no
            # half-file pollutes the season dir. jobs[:done] already completed —
            # those are real files, leave them. Staging is PRESERVED regardless.
            for src, dst, label in jobs[done:]:
                _rollback_incomplete_dst(Path(src), Path(dst))
            append_log(args, f"MOVE_FAIL {item.title} disc{disc_n} :: {e} "
                             f"(RIP PRESERVED at {staging}; "
                             f"{len(jobs) - done} incomplete slot(s) rolled back)")
            print(f"{C.RED}Background NAS move failed: {e}{C.R}", flush=True)
            print(f"{C.YLW}  Staged rip preserved at {staging}; fix the mount "
                  f"and move it manually.{C.R}", flush=True)
            beep(args, ok=False)
            return
    files = result.get("files", [])
    if args.verify and files:
        ok2, err = verify(files)
        if not ok2:
            append_log(args, f"VERIFY_FAIL {item.title} disc{disc_n} :: {err}")
            print(f"{C.RED}Verify failed (background): {err}{C.R}", flush=True)
            beep(args, ok=False)
    run_subocr_postrip(args, files)

def start_background_finalize(args, result, item, disc_n):
    """Hand the NAS move + verify + subocr to a worker thread so the caller can
    move straight to the next disc. Tracked in _FINALIZE_THREADS for join."""
    t = threading.Thread(target=_run_finalize,
                         args=(args, result, item, disc_n), daemon=False)
    t.start()
    _FINALIZE_THREADS.append(t)
    print(f"{C.D}  NAS transfer running in background; drive is free for the "
          f"next disc.{C.R}", flush=True)

def join_finalizers():
    """Block until every background transfer has finished. Called before the
    process exits so a normal exit never kills an in-flight copy."""
    pending = [t for t in _FINALIZE_THREADS if t.is_alive()]
    if pending:
        print(f"{C.D}Waiting for {len(pending)} background NAS transfer(s) "
              f"to finish before exit...{C.R}", flush=True)
    for t in _FINALIZE_THREADS:
        t.join()

# Push notification on FAIL is now handled out-of-process by ~/.hermes/bin/
# disc-rip-watcher, which tails the log file. Single-source dedup, no
# code-deploy required to cover in-flight ripqueue processes.

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
    if <14 days remaining. Skips silently for permanent keys or parse failures.

    Defensive: makemkvcon can hang on certain optical-media states (the same
    failure mode probe_disc handles via a stall watchdog). We don't want a
    startup banner check to block burndvd. Use Popen + a short stall window
    so a hung makemkvcon doesn't burn a full 20s wait at every invocation.
    """
    try:
        proc = subprocess.Popen(
            [args.makemkvcon, "-r", "--cache=128", "info", args.device],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError:
        return

    qq: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, qq), daemon=True).start()
    lines: list[str] = []
    last_t = time.time()
    start = time.time()
    while True:
        now = time.time()
        if now - start > 20 or now - last_t > 5:
            proc.kill()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired: pass
            break
        try:
            kind, payload = qq.get(timeout=1)
        except queue.Empty:
            continue
        if kind == "eof":
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired: proc.kill()
            break
        if kind == "line":
            lines.append(payload.rstrip())
            last_t = now

    msgs = []
    for line in lines:
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
    fail_since = None   # start of the current stuck-with-disc-present streak
    respins = 0
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
        # Flaky-handshake recovery. Only engage when a disc is actually loaded —
        # an empty drive failing to probe is just "waiting for insert", not a
        # hung drive. After RESPIN_AFTER_S of continuous failure with media
        # present, cycle the tray to force a re-spin; otherwise rest between
        # probes so the drive can settle and the next attempt can catch a good
        # handshake window.
        if disc_present(args):
            if fail_since is None:
                fail_since = now
            if now - fail_since >= RESPIN_AFTER_S and respins < MAX_RESPINS:
                respin_drive(args)
                respins += 1
                fail_since = None
                continue
            # MAX_RESPINS exhausted with disc still present and still failing
            # to probe — bail out instead of polling forever. The old code
            # silently fell through the if and time.sleep'd in a loop that
            # only `select.select` or a manual kill could escape, which in
            # --non-interactive mode (cron/launchd/burndvd-detached) meant
            # the wrapper would hang indefinitely with no rip, no eject, no
            # progress, and nothing for the user to do.
            if respins >= MAX_RESPINS and now - fail_since >= RESPIN_AFTER_S:
                print(f"\n{C.RED}Disc failed to probe after "
                      f"{MAX_RESPINS} respins; giving up on this disc.{C.R}",
                      flush=True)
                append_log(args, f"FAIL  probe wedged after {MAX_RESPINS} respins; "
                                 f"surfacing as ('error','probe_max_respins')")
                return ("error", {"reason": "probe_max_respins",
                                  "respins": respins,
                                  "elapsed_s": int(now - started)})
            time.sleep(PROBE_COOLDOWN_S)
        else:
            fail_since = None
        if now - started > 120 and now - last_dropoff_hint > 60:
            # Only warn about drive-dropped-off if makemkvcon truly isn't
            # producing output — otherwise the probe is just slow (corrupt
            # IFO, lots of titles, etc.) and the warning misleads.
            if _LAST_PROBE_ACTIVITY < now - 30:
                print(f"  {C.YLW}No disc activity for >2min. If the drive dropped off bus:{C.R}")
                print(f"  {C.YLW}    system_profiler SPUSBDataType | grep -B2 -A4 'BU40N\\|UHD'{C.R}")
                print(f"  {C.YLW}    Reseat USB cable, try a powered hub, or run under{C.R}")
                print(f"  {C.YLW}    `caffeinate -dimsu burndvd ...` to defeat power management.{C.R}")
                last_dropoff_hint = now

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue")
    ap.add_argument("--scan", action="store_true",
                    help="probe the disc, print its title table as JSON, and "
                         "exit (used by burndvd to detect double-feature discs)")
    ap.add_argument("--state", default="ripqueue-state.json")
    ap.add_argument("--makemkvcon", default=DEFAULT_MAKEMKVCON)
    ap.add_argument("--device", default="disc:0")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--min-free-gb", type=int, default=200)
    ap.add_argument("--no-eject", action="store_true")
    ap.add_argument("--sync-move", action="store_true",
                    help="copy to the NAS synchronously before ejecting "
                         "(legacy; disables the background transfer that frees "
                         "the drive for the next disc during the copy)")
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

    # SIGUSR1 → "skip current queue item" via stoprip --skip-current.
    # The handler just sets a flag; the main loop checks between items so
    # the in-progress rip can finish/abort cleanly via its own teardown path.
    def _skip_current_signal(signum, frame):
        global _SKIP_REQUESTED
        _SKIP_REQUESTED = True
    signal.signal(signal.SIGUSR1, _skip_current_signal)

    # Capture initial drive identity. With CSV-batch mode about to feed 500
    # discs through this, a drive hotswap / USB reset / reboot mid-queue
    # would silently shift disc:N to a different physical drive, sending
    # rips to the wrong drive's content. Re-checked at the top of each
    # queue iteration; abort on mismatch. (audit #12)
    _initial_drive_idx = _drive_idx_from_args(args)
    _initial_drive_id = (_drive_identity(_initial_drive_idx)
                         if _initial_drive_idx is not None else None)
    if _initial_drive_idx is not None and not _initial_drive_id:
        # No drive at the requested index at all. Bail fast rather than
        # silently retrying probes against a non-existent drive.
        print(f"{C.RED}--device {args.device} doesn't map to any optical "
              f"drive at startup. Aborting.{C.R}", file=sys.stderr)
        sys.exit(1)

    if not sys.stdin.isatty() and not args.non_interactive and not args.scan:
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

    # --scan is a standalone probe-and-dump; it doesn't need a queue or state.
    if args.scan:
        sys.exit(scan_titles_json(args))

    if not args.queue:
        print(f"{C.RED}--queue is required (unless using --scan).{C.R}", file=sys.stderr)
        sys.exit(1)

    warn_state_location(args.state)
    check_key_expiry(args)

    items = load_queue(Path(args.queue))
    state = load_or_init(args, items)
    save_state(state, args.state)

    while state["current_index"] < len(state["queue"]):
        # Re-verify drive identity at the start of each queue item. If a
        # USB reset / hotswap / reboot has shifted disc:N to a different
        # physical drive (or no drive at all), abort rather than send the
        # rip to the wrong drive's content. The check is fast (sub-50ms
        # drutil status call) so per-item is fine. (audit #12)
        if _initial_drive_idx is not None:
            _current_drive_id = _drive_identity(_initial_drive_idx)
            if _current_drive_id != _initial_drive_id:
                print(f"\n{C.RED}Drive at {args.device} changed mid-queue:{C.R}",
                      file=sys.stderr)
                print(f"  was: {_initial_drive_id}", file=sys.stderr)
                print(f"  now: {_current_drive_id or '(no drive)'}",
                      file=sys.stderr)
                print(f"  {C.RED}Aborting — refusing to rip to a drive that "
                      f"may not own the current disc.{C.R}", file=sys.stderr)
                append_log(args, f"ABORT drive identity changed: "
                                 f"{_initial_drive_id} -> {_current_drive_id}")
                sys.exit(3)

        # SIGUSR1 from `stoprip --skip-current` sets this flag. Advance past
        # the current queue item without trying to rip it, log the skip,
        # then continue with whatever's next.
        global _SKIP_REQUESTED
        if _SKIP_REQUESTED:
            _SKIP_REQUESTED = False
            try:
                cur_item = QueueItem(**state["queue"][state["current_index"]])
                disc_n = state["disc_index_in_item"] + 1
                append_log(args, f"SKIP_SIGUSR1  {cur_item.title} disc{disc_n}")
                print(f"{C.YLW}stoprip --skip-current: advancing past "
                      f"{cur_item.title} disc {disc_n}{C.R}", flush=True)
            except Exception:
                pass
            state["current_index"] += 1
            state["disc_index_in_item"] = 0
            save_state(state, args.state)
            continue

        item = QueueItem(**state["queue"][state["current_index"]])
        disc_n = state["disc_index_in_item"] + 1
        header(state, item, disc_n)

        kind, payload = wait_for_disc(args, state)
        if kind == "cmd":
            continue
        if kind == "error":
            # wait_for_disc gave up after MAX_RESPINS — disc is wedged in a
            # way physical respin couldn't fix. Treat as a rip failure and
            # honor --on-fail so a 500-disc batch doesn't stall forever on
            # one bad disc. Without this short-circuit, the old code would
            # have polled the hung drive indefinitely in --non-interactive
            # mode (audit #4, 2026-05-31).
            print(f"{C.RED}Probe gave up: {payload.get('reason','?')}; "
                  f"advancing per --on-fail={args.on_fail}.{C.R}")
            append_log(args, f"FAIL  {item.title} disc{disc_n} probe gave up "
                             f"({payload.get('reason')}); --on-fail={args.on_fail}")
            if args.on_fail == "abort":
                sys.exit(2)
            # skip or retry both advance the queue (retry would re-loop on the
            # same wedged disc, defeating the point of giving up).
            state["current_index"] += 1
            state["disc_index_in_item"] = 0
            save_state(state, args.state)
            eject(args)
            continue
        info = payload

        # burndvd builds a single-disc ephemeral queue per session, so disc_n
        # is always 1 — collides with later discs of the same TV set ripped in
        # separate sessions (extras "S01D1" clobbering each other). Prefer the
        # disc number embedded in the MakeMKV disc label when present.
        if isinstance(info, dict) and "titles" in info:
            detected = disc_n_from_label(info)
            if detected is not None and detected != disc_n:
                print(f"  {C.YLW}Disc label reports Disc {detected}; "
                      f"overriding queue disc_n {disc_n} → {detected}.{C.R}")
                disc_n = detected

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

        # Physical disc trouble: corrupt IFO, SCSI errors, drive reporting
        # tray-open mid-read, etc. No amount of retry fixes a scratched or
        # mis-pressed disc. Tell the user what makemkvcon actually said
        # instead of looping with "polling drive..." indefinitely.
        if isinstance(info, dict) and info.get("error") == "PHYSICAL_DISC_TROUBLE":
            print(f"{C.RED}Disc looks physically damaged or unreadable.{C.R}")
            CODE_BLURB = {
                3042: "IFO file corrupt — DVD authoring damage; VOB scan slow and often fails",
                2003: "SCSI error mid-read — drive lost the medium (tray-open / read fault)",
                5010: "MakeMKV gave up opening the disc",
            }
            for code in info.get("codes", []):
                blurb = CODE_BLURB.get(code, "")
                print(f"  {C.YLW}MSG:{code}{C.R}  {blurb}")
            for m in info.get("msgs", [])[-4:]:
                print(f"  {C.D}{m}{C.R}")
            print(f"  {C.YLW}Try: wipe disc with a soft cloth (concentric scratches), reinsert.{C.R}")
            print(f"  {C.YLW}If repeated: skip this disc, continue with the next.{C.R}")
            ans = auto_answer(args, "[p]ark item / [s]kip item / [r]etry: ", "s")
            if ans.startswith("p"):
                cur = state["queue"].pop(state["current_index"])
                state["queue"].append(cur)
                state["disc_index_in_item"] = 0
                save_state(state, args.state)
                append_log(args, f"BAD_DISC  {item.title} disc{disc_n}  parked to end (codes={info.get('codes')})")
            elif ans.startswith("s"):
                state["current_index"] += 1
                state["disc_index_in_item"] = 0
                save_state(state, args.state)
                append_log(args, f"BAD_DISC  {item.title} disc{disc_n}  skipped (codes={info.get('codes')})")
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
        log_title_audit(args, item, info, title_ids)
        if not title_ids:
            print(f"{C.RED}No suitable titles on disc (TV floor: {TV_MIN_DUR_S}s).{C.R}")
            beep(args, ok=False)
            ans = auto_answer(args, "[r]etry / [s]kip? ", "s")
            if ans.startswith("s"):
                state["current_index"] += 1; state["disc_index_in_item"] = 0
                save_state(state, args.state)
            continue

        print(f"  Selected {len(title_ids)} title(s): {title_ids}")

        if item.type == "double-feature":
            ok, result = rip_double_feature(args, item, info, state, disc_n)
        else:
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

        log_final_durations(args, item, disc_n, result["files"])

        # Record the rip and advance the queue now. The NAS move + verify +
        # subocr run in the background (start_background_finalize) so the disc
        # ejects and the drive is free for the next disc immediately, instead
        # of being held for the multi-minute SMB copy. The optimistic advance
        # means a hard kill mid-copy won't re-rip — acceptable because the
        # staged file is preserved on any move failure (recoverable) and a
        # normal exit joins all transfers (join_finalizers) before quitting.
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
        if result.get("move_jobs") is not None:
            # Async path: copy to NAS + verify + subocr on a worker thread.
            start_background_finalize(args, result, item, disc_n)
        else:
            # --sync-move / double-feature: files are already on the NAS.
            if args.verify:
                print(f"{C.D}Verifying with ffmpeg...{C.R}")
                ok2, err = verify(result["files"])
                if not ok2:
                    append_log(args, f"VERIFY_FAIL {item.title} disc{disc_n} :: {err}")
                    print(f"{C.RED}Verify failed: {err}{C.R}"); beep(args, ok=False)
            run_subocr_postrip(args, result["files"])

    join_finalizers()
    print(f"\n{C.B}{C.GRN}Queue complete.{C.R}")

if __name__ == "__main__":
    # Wrap main() so we always write an exit sentinel next to --state. The
    # burndvd wrapper waits on a pgrep loop (not `wait $PID`) for the
    # detached ripqueue and so doesn't see the actual exit code. Without
    # this sentinel the wrapper silently treats a failed rip the same as
    # a successful one, ejects the disc, and moves on — across a 500-disc
    # batch this means failed discs get silently skipped with no surfacing
    # to the user (audit #3, 2026-05-31).
    _exit_code = 0
    _exit_reason = "queue_complete"
    try:
        main()
    except KeyboardInterrupt:
        # join_finalizers() prints "Waiting for N background NAS transfer(s)
        # to finish..." which gives the user explicit feedback that the
        # process WILL wait for in-flight transfers before exiting (non-
        # daemon threads block sys.exit until done). Without this print
        # they saw only "Interrupted." and often force-killed, losing the
        # staged rip mid-move. (audit #10, 2026-05-31)
        print("\nInterrupted. State on disk is current.")
        try:
            join_finalizers()
        except KeyboardInterrupt:
            # Second Ctrl-C while waiting: user really wants out. Daemon
            # threads will be killed at exit — staged files for the
            # interrupted transfer may be lost.
            print(f"{C.YLW}Second Ctrl-C; abandoning in-flight transfers. "
                  f"Any partial files in staging are recoverable manually.{C.R}",
                  flush=True)
        _exit_code = 130
        _exit_reason = "keyboard_interrupt"
    except SystemExit as e:
        _exit_code = e.code if isinstance(e.code, int) else 1
        _exit_reason = f"sys_exit_{_exit_code}"
    except Exception as e:
        print(f"\n{C.RED}Unexpected error: {e}{C.R}", file=sys.stderr)
        _exit_code = 1
        _exit_reason = f"exception:{type(e).__name__}:{str(e)[:120]}"
    finally:
        try:
            _state_path = next(
                (sys.argv[i + 1] for i, a in enumerate(sys.argv)
                 if a == "--state" and i + 1 < len(sys.argv)),
                None,
            )
            if _state_path:
                with open(_state_path + ".exit", "w") as _f:
                    json.dump({"exit_code": _exit_code,
                               "reason": _exit_reason,
                               "ts": int(time.time())}, _f)
        except OSError:
            pass
    sys.exit(_exit_code)
