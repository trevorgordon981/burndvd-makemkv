"""MakeMKV disc probing, robot-output parsing, disc fingerprinting, and ffprobe helpers."""

from __future__ import annotations

import contextlib
import hashlib
import json
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from burndvd.config import (
    C,
    EXTRAS_MIN_DUR_S,
    FFPROBE_BIN,
    MOVIE_FEATURE_MIN_S,
    PROBE_HARD_S,
    PROBE_STALL_S,
    fmt_dur,
)
from burndvd.drive import (
    _drive_idx_from_args,
    _optical_mounts,
    append_log,
    disc_present,
    unmount_blocking_volume,
)
from burndvd.models import QueueItem, TitleInfo

# Module-level last-line timestamp updated while MakeMKV streams probe lines.
_LAST_PROBE_ACTIVITY = 0.0


def _reader(stream, q: queue.Queue) -> None:
    try:
        for line in stream:
            q.put(("line", line))
    finally:
        q.put(("eof", None))


def parse_line(line: str) -> tuple[str | None, list[str] | None]:
    """Parse one `makemkvcon -r` robot-mode line into `(tag, fields)`."""
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


def parse_duration(s: str) -> int:
    """Parse `H:MM:SS` MakeMKV duration string into integer seconds."""
    m = re.match(r"^(\d+):(\d{2}):(\d{2})$", s.strip())
    if not m:
        return 0
    h, mn, sc = map(int, m.groups())
    return h * 3600 + mn * 60 + sc


def title_num_from_filename(name: str) -> int | None:
    """Extract MakeMKV title id from a `*_tNN.mkv` filename."""
    m = re.search(r"_t(\d+)\.mkv$", name)
    return int(m.group(1)) if m else None


def parse_save_summary(msgs: list[str]) -> tuple[int, int] | None:
    """Return `(saved, failed)` from MakeMKV's save-complete MSG, or None if absent."""
    for m in reversed(msgs):
        match = re.search(
            r"(\d+)\s+titles?\s+saved(?:,\s*(\d+)\s+failed)?", m, re.IGNORECASE
        )
        if match:
            saved = int(match.group(1))
            failed = int(match.group(2)) if match.group(2) else 0
            return saved, failed
    return None


def stall_timeout_for(format_: str) -> int:
    """Seconds without PRGV total advance before declaring a stall."""
    return {"4K": 900, "BD": 720, "DVD": 480}.get((format_ or "").upper(), 720)


def msgs_indicate_aacs_lock(msgs: list[str]) -> bool:
    """Detect AACS v82+ key failures distinct from generic 'no titles'."""
    pat = re.compile(
        r"(newer\s+version.*aacs|aacs.*newer\s+version|aacs.*not\s+available|failed\s+to\s+open\s+disc.*aacs)",
        re.IGNORECASE,
    )
    return any(pat.search(m) for m in msgs)


def disc_label(info: dict) -> str:
    """Return the best human-readable disc label from MakeMKV `CINFO`."""
    c = info.get("cinfo", {})
    return c.get(2) or c.get(32) or c.get(30) or c.get(1) or "(unknown)"


def disc_content_fingerprint(info: dict) -> str:
    """Hash stable disc/title-table identity (SHA-256), excluding drive noise."""
    rows = []
    for title_id, values in sorted(info.get("titles", {}).items()):
        rows.append(
            [
                int(title_id),
                *[str(values.get(key, "")) for key in (2, 9, 10, 11, 27)],
            ]
        )
    payload = {
        "label": disc_label(info).casefold().strip(),
        "titles": rows,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _title_size_bytes(title: dict | None) -> int:
    try:
        return int((title or {}).get(11, "0") or "0")
    except (AttributeError, TypeError, ValueError):
        return 0


def titles_from_info(info: dict) -> list[TitleInfo]:
    """Convert raw MakeMKV probe dictionary into structured `TitleInfo` objects."""
    result: list[TitleInfo] = []
    for tid in sorted(info.get("titles", {}).keys()):
        t = info["titles"][tid]
        dur_s = parse_duration(t.get(9, "0:00:00"))
        try:
            chapters = int(t.get(8, "0") or "0")
        except (TypeError, ValueError):
            chapters = 0
        result.append(
            TitleInfo(
                title_id=int(tid),
                duration_s=dur_s,
                size_bytes=_title_size_bytes(t),
                size_human=str(t.get(10, "?")),
                name=str(t.get(2) or t.get(27) or ""),
                chapters=chapters,
                is_feature=dur_s >= MOVIE_FEATURE_MIN_S,
            )
        )
    return result


def probe_disc(args) -> dict | None:
    """Run `makemkvcon -r --minlength=... info` with stall watchdog and unmount recovery."""
    target_drive_idx = _drive_idx_from_args(args)
    want_drv_idx = target_drive_idx if target_drive_idx is not None else 0

    def _run_once():
        if _optical_mounts():
            unmount_blocking_volume(None, drive_idx=target_drive_idx)
        try:
            proc = subprocess.Popen(
                [
                    args.makemkvcon,
                    "-r",
                    "--cache=128",
                    f"--minlength={EXTRAS_MIN_DUR_S}",
                    "info",
                    args.device,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
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
            if now - last_status_t >= 10:
                tag = " (slow-mode VOB scan)" if slow_mode else ""
                print(
                    f"  {C.D}probing... {int(now - start)}s elapsed, "
                    f"{titles_added} title(s) found{tag}{C.R}",
                    flush=True,
                )
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
                if "MSG:3028" in line:
                    titles_added += 1
                    if stall_s < 180:
                        stall_s = 180
                if not slow_mode and "MSG:3042" in line:
                    slow_mode = True
                    stall_s = 300
                    hard_s = max(hard_s, 1800)
                    print(
                        f"  {C.YLW}disc has corrupt IFO — falling back to "
                        f"VOB scan, this is slow but should finish{C.R}",
                        flush=True,
                    )
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        if killed and not any(
            line.startswith(("CINFO:", "TINFO:")) for line in lines
        ):
            return None, None, None, None
        if proc.returncode not in (0, None) and not lines:
            return None, None, None, None

        cinfo, titles, msgs, rdisk = {}, {}, [], None
        msg_codes: set[int] = set()
        for line in lines:
            tag, f = parse_line(line)
            if tag == "MSG" and f and len(f) >= 4:
                msgs.append(f[3])
                try:
                    msg_codes.add(int(f[0]))
                except ValueError:
                    pass
            elif tag == "DRV" and f and len(f) >= 7 and f[6].startswith("/dev/rdisk"):
                try:
                    if int(f[0]) == want_drv_idx:
                        rdisk = f[6]
                except ValueError:
                    pass
            elif tag == "CINFO" and f and len(f) >= 3:
                try:
                    cinfo[int(f[0])] = f[2]
                except ValueError:
                    pass
            elif tag == "TINFO" and f and len(f) >= 4:
                try:
                    tid, code = int(f[0]), int(f[1])
                    titles.setdefault(tid, {})[code] = f[3]
                except ValueError:
                    pass
        return (cinfo, titles, msgs), msg_codes, rdisk, proc

    result, codes, rdisk, _ = _run_once()
    if (result is None or not result[1]) and _optical_mounts():
        if unmount_blocking_volume(rdisk, drive_idx=target_drive_idx):
            result, codes, rdisk, _ = _run_once()
    if result is None:
        return None
    cinfo, titles, msgs = result
    if titles:
        return {"cinfo": cinfo, "titles": titles, "msgs": msgs}
    if msgs_indicate_aacs_lock(msgs):
        return {"error": "AACS_LOCKED", "msgs": msgs}
    diag_codes = (codes or set()) & {3042, 2003, 5010}
    if diag_codes:
        if target_drive_idx is not None and not disc_present(args):
            return None
        return {
            "error": "PHYSICAL_DISC_TROUBLE",
            "codes": sorted(diag_codes),
            "msgs": msgs,
        }
    return None


def scan_titles_json(args) -> int:
    """Probe the disc and emit its title table as JSON to stdout."""
    with contextlib.redirect_stdout(sys.stderr):
        info = probe_disc(args)
    titles = []
    if isinstance(info, dict) and "titles" in info:
        for tid in sorted(info["titles"].keys()):
            t = info["titles"][tid]
            dur_s = parse_duration(t.get(9, "0:00:00"))
            titles.append(
                {
                    "id": tid,
                    "dur_s": dur_s,
                    "dur": fmt_dur(dur_s),
                    "size": t.get(10, "?"),
                    "name": t.get(2) or t.get(27) or "",
                    "feature": dur_s >= MOVIE_FEATURE_MIN_S,
                }
            )
    print(json.dumps(titles))
    return 0


def ffprobe_dur_s(path: str) -> float:
    """Return actual container duration in seconds via ffprobe, or 0.0 on failure."""
    try:
        r = subprocess.run(
            [
                FFPROBE_BIN,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else 0.0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def ffprobe_streams(path: str) -> tuple[float, list[dict]]:
    """Return `(duration_s, streams)` via ffprobe JSON output, or `(0.0, [])`."""
    try:
        r = subprocess.run(
            [
                FFPROBE_BIN,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=index,codec_type,codec_name",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return 0.0, []
        data = json.loads(r.stdout)
        dur_raw = (data.get("format") or {}).get("duration")
        dur_s = float(dur_raw) if dur_raw not in (None, "", "N/A") else 0.0
        streams = list(data.get("streams") or [])
        return dur_s, streams
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return 0.0, []


def log_final_durations(args, item: QueueItem, disc_n, files: list[str]) -> None:
    """Post-rip audit: record real ffprobe duration and size of each landed file."""
    if not getattr(args, "state", None):
        return
    for path in files:
        p = Path(path)
        dur_s = ffprobe_dur_s(path)
        try:
            size_gb = p.stat().st_size / 1e9
        except OSError:
            size_gb = 0.0
        append_log(
            args,
            f"FINAL {item.title} S{item.season:02d}D{disc_n} "
            f"{fmt_dur(dur_s)} ({int(dur_s)}s) {size_gb:.2f}GB | {p.name}",
        )
