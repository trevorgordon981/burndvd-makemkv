"""Rip orchestration, mkvmerge fallback integration, background finalizer pool, and queue runner."""

from __future__ import annotations

import argparse
import concurrent.futures
import errno
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from burndvd.config import (
    ALT_DEDUP_DUR_TOL_S,
    C,
    EXTRAS_MIN_DUR_S,
    FFMPEG_BIN,
    FFPROBE_BIN,
    LOCAL_STAGING_BASE,
    MAX_NONINTERACTIVE_RETRIES,
    MAX_RESPINS,
    NFS_MEDIA_PREFIX,
    PROBE_COOLDOWN_S,
    RESPIN_AFTER_S,
    SMB_MEDIA_PREFIX,
    STAGING_HEADROOM_GB,
    TV_MIN_DUR_S,
    fmt_dur,
    render_bar,
    sanitize,
)
from burndvd.drive import (
    append_log,
    beep,
    disc_present,
    resolve_makemkv_device,
    respin_drive,
)
from burndvd.fallback import mkvmerge_fallback_rip
from burndvd.metadata import (
    clear_tv_slot_reservation,
    scan_season_highest_episode,
    write_tv_slot_reservation,
)
from burndvd.models import QueueItem, save_state
from burndvd.nas import (
    _await_dir_writable,
    _claim_tv_destinations,
    _dir_writable,
    _emit_claimed_tv_moves,
    _free_gb,
    _rebind_claim_identity,
    _remove_zero_placeholder,
    _rollback_incomplete_dst,
    _salvage_and_fail,
    _to_nfs_path,
    apply_tv_output_mode,
    compute_target_dir,
    existing_episode_slots,
    move_with_progress,
    move_with_progress_noclobber,
    protected_quarantine_child,
    record_episode_count,
    require_free_extra_destination,
    reserve_tv_review_attempt_dir,
    resolve_nas_path,
    season_dir_lock,
    starting_ep,
    tv_partial_capture_root,
    tv_review_capture_dir,
    tv_show_extras_dir,
)
from burndvd import probe as _probe
from burndvd.probe import (
    _reader,
    ffprobe_dur_s,
    log_final_durations,
    parse_duration,
    parse_line,
    parse_save_summary,
    probe_disc,
    stall_timeout_for,
    title_num_from_filename,
)
from burndvd.receipts import append_disc_receipt
from burndvd.selection import (
    _alt_playlist_duplicate,
    episode_bonus_split,
    existing_content_collision,
    movie_filename,
    partition_episode_contract,
    planned_rip_selectors,
    play_all_title_ids,
    split_chaptered_playall,
    staging_space_error,
    staging_space_requirement,
    verify_play_all,
)

# --- Process-global coordination state ---
_SKIP_REQUESTED = False
_FINALIZE_THREADS: list[threading.Thread] = []
_FINALIZE_LOCK = threading.Lock()
_FINALIZE_ERRORS: list[str] = []
_FINALIZE_ERRORS_LOCK = threading.Lock()

_STALL_BYTE_RE = re.compile(r"stalled at PRGV total=(\d+)/")


def _record_finalize_error(msg: str) -> None:
    with _FINALIZE_ERRORS_LOCK:
        _FINALIZE_ERRORS.append(msg)


def get_finalize_errors() -> list[str]:
    with _FINALIZE_ERRORS_LOCK:
        return list(_FINALIZE_ERRORS)


def clear_finalize_errors() -> None:
    with _FINALIZE_ERRORS_LOCK:
        _FINALIZE_ERRORS.clear()


def stall_byte(result: str) -> int | None:
    m = _STALL_BYTE_RE.search(result or "")
    return int(m.group(1)) if m else None


def noninteractive_failure_action(
    on_fail: str, retries_used: int, repeated_same_byte_stall: bool = False
) -> str:
    """Return `'abort'`, `'retry'`, or `'skip'` for a detached rip failure."""
    if on_fail == "abort":
        return "abort"
    if on_fail == "retry":
        if repeated_same_byte_stall:
            return "abort"
        if retries_used < MAX_NONINTERACTIVE_RETRIES:
            return "retry"
        return "abort"
    return "skip"


def auto_answer(args, prompt_text: str, auto: str) -> str:
    if getattr(args, "non_interactive", False):
        print(f"{prompt_text}{auto}  {C.D}(--non-interactive){C.R}")
        return auto
    return input(prompt_text).strip().lower()


def _staging_write_age(staging: Path) -> float:
    try:
        newest = max(
            (p.stat().st_mtime for p in staging.rglob("*.mkv")),
            default=0.0,
        )
    except OSError:
        return 1e9
    return time.time() - newest if newest else 1e9


def _draw_progress(
    start: float, total_v: int, mx: int, cur_task: str, stall_age_s: float
) -> None:
    pct = 100.0 * total_v / mx if mx else 0
    el = time.time() - start
    eta = (el * (100 - pct) / pct) if pct > 1 else 0
    bar = render_bar(pct / 100)
    stall_tag = f" {C.YLW}stall {int(stall_age_s)}s{C.R}" if stall_age_s > 30 else ""
    clock = time.strftime("%H:%M:%S")
    sys.stdout.write(
        f"\r{C.GRN}{bar}{C.R} {pct:5.1f}%  "
        f"el {fmt_dur(el)}  ETA {fmt_dur(eta)}  "
        f"{C.D}{clock}{C.R}  "
        f"{cur_task[:38]:<38}{stall_tag}"
    )
    sys.stdout.flush()


def _parse_ffmpeg_out_time_s(progress_stdout: str) -> float | None:
    """Parse the final decoded timestamp (seconds) from ffmpeg `-progress pipe:1` output."""
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


def _probe_expected_duration_s(path: str) -> tuple[float | None, str | None]:
    """Probe container/video-stream metadata with ffprobe; return (duration_s, error)."""
    try:
        probe = subprocess.run(
            [
                FFPROBE_BIN,
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
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"ffprobe failed: {exc}"
    if probe.returncode != 0:
        return None, f"ffprobe exit {probe.returncode}; {(probe.stderr or "").strip()[:200]}"
    if (probe.stderr or "").strip():
        return None, f"ffprobe error: {(probe.stderr or "").strip()[:200]}"
    try:
        data = json.loads(probe.stdout or "{}")
    except (ValueError, TypeError):
        return None, "ffprobe returned invalid JSON"
    streams = data.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        return None, "no video stream found"
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
        return None, "missing duration metadata"
    try:
        dur = float(raw_dur)
    except (ValueError, TypeError):
        return None, f"invalid duration metadata: {raw_dur!r}"
    if dur <= 0:
        return None, f"non-positive duration: {dur}"
    return dur, None


def _verify_single(p: str) -> tuple[bool, str]:
    """Decode-verify a single MKV through strict ffmpeg null muxer and duration checks."""
    r = subprocess.run(
        [
            FFMPEG_BIN,
            "-nostdin",
            "-v",
            "error",
            "-err_detect",
            "explode",
            "-xerror",
            "-progress",
            "pipe:1",
            "-i",
            p,
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    stderr_txt = (getattr(r, "stderr", "") or "").strip()
    if r.returncode != 0:
        return False, f"{p}: exit {r.returncode}; {stderr_txt[:240]}"
    if stderr_txt:
        return False, f"{p}: ffmpeg decode error: {stderr_txt[:240]}"
    if os.path.isfile(p):
        try:
            if os.path.getsize(p) <= 0:
                return False, f"{p}: empty file (0 bytes)"
        except OSError as exc:
            return False, f"{p}: stat failed: {exc}"
        expected_dur, probe_err = _probe_expected_duration_s(p)
        if probe_err is not None:
            return False, f"{p}: {probe_err}"
        if expected_dur is not None and expected_dur > 0:
            decoded_s = _parse_ffmpeg_out_time_s(getattr(r, "stdout", "") or "")
            if decoded_s is not None:
                tol = max(0.25, min(10.0, expected_dur * 0.05))
                if decoded_s <= 0 or (expected_dur - decoded_s) > tol:
                    return (
                        False,
                        f"{p}: truncated stream (decoded {decoded_s:.2f}s of "
                        f"{expected_dur:.2f}s container duration)",
                    )
    return True, ""


def verify(paths: list[str]) -> tuple[bool, str]:
    """Decode-verify every output MKV through strict ffmpeg null muxer and duration checks."""
    path_list = list(paths)
    if not path_list:
        return True, ""
    if len(path_list) == 1:
        return _verify_single(path_list[0])
    max_workers = min(len(path_list), max(2, min(6, (os.cpu_count() or 4) // 2)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(_verify_single, path_list))
    for ok, err in results:
        if not ok:
            return False, err
    return True, ""


def run_subocr_postrip(args, files: list[str]) -> None:
    """Detach a background `subocr --post-rip` run (subocr itself owns subocr.flock)."""
    if getattr(args, "no_subocr", False) or not files:
        return
    subocr_bin = Path.home() / ".local" / "bin" / "subocr"
    if not subocr_bin.exists():
        return
    log_dir = Path.home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"subocr-postrip-{int(time.time())}.log"
    runner = (
        "import os, subprocess, sys; "
        "os.environ['PATH'] = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:' + os.environ.get('PATH', ''); "
        "rc = subprocess.call(sys.argv[1:]); "
        "print(f'[subocr-postrip] exit={rc}', flush=True); "
        "raise SystemExit(rc)"
    )
    cmd = [
        sys.executable,
        "-c",
        runner,
        str(subocr_bin),
        "--post-rip",
        *files,
    ]
    with open(log_path, "a", encoding="utf-8") as fh:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(
        f"{C.D}  subocr started in background ({len(files)} file(s); "
        f"log: {log_path.name}){C.R}"
    )


def _unpack_move_job(job):
    if len(job) == 3:
        src, dst, label = job
        return src, dst, label, False, None, None, None, None
    if len(job) == 6:
        src, dst, label, protected, placeholder_identity, lock_dir = job
        return (
            src,
            dst,
            label,
            bool(protected),
            placeholder_identity,
            None,
            None,
            Path(lock_dir) if lock_dir is not None else None,
        )
    (
        src,
        dst,
        label,
        protected,
        placeholder_identity,
        claim_path,
        partial_root,
        lock_dir,
    ) = job
    if placeholder_identity is not None:
        placeholder_identity = tuple(placeholder_identity)
    return (
        src,
        dst,
        label,
        bool(protected),
        placeholder_identity,
        Path(claim_path) if claim_path is not None else None,
        Path(partial_root) if partial_root is not None else None,
        Path(lock_dir) if lock_dir is not None else None,
    )


_COMMITTED_MANIFEST_NAME = ".burndvd-committed.json"


def _norm_job_key(src: Path | str, orig_dst: Path | str) -> tuple[str, str]:
    return (os.path.normpath(os.fspath(src)), os.path.normpath(os.fspath(orig_dst)))


def _load_committed_jobs(result: dict, staging) -> list[dict]:
    """Load committed per-file publish records from `result` and staging manifest."""
    merged: dict[tuple[str, str], dict] = {}
    sources: list[list] = []
    if isinstance(result.get("committed_jobs"), list):
        sources.append(result["committed_jobs"])
    if staging:
        manifest = Path(staging) / _COMMITTED_MANIFEST_NAME
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("committed_jobs"), list):
                    sources.append(data["committed_jobs"])
            except Exception:
                pass
    for entries in sources:
        for rec in entries:
            if not isinstance(rec, dict):
                continue
            src = rec.get("src")
            orig_dst = rec.get("orig_dst", rec.get("dst"))
            if not src or not orig_dst:
                continue
            merged[_norm_job_key(src, orig_dst)] = dict(rec)
    return list(merged.values())


def _save_committed_manifest(staging, committed_jobs: list[dict]) -> None:
    """Atomically persist per-file commit records inside the staging directory."""
    if not staging:
        return
    stage_dir = Path(staging)
    if not stage_dir.is_dir():
        return
    manifest = stage_dir / _COMMITTED_MANIFEST_NAME
    tmp = stage_dir / f"{_COMMITTED_MANIFEST_NAME}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        tmp.write_text(
            json.dumps({"committed_jobs": committed_jobs}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, manifest)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _find_committed_record(
    committed_jobs: list[dict], src: Path, orig_dst: Path
) -> dict | None:
    key = _norm_job_key(src, orig_dst)
    for rec in committed_jobs:
        if not isinstance(rec, dict):
            continue
        if _norm_job_key(rec.get("src", ""), rec.get("orig_dst", rec.get("dst", ""))) == key:
            return rec
    return None


def _verify_committed_destination(rec: dict, src: Path) -> Path:
    """Verify a previously committed destination still exists with recorded size/identity."""
    dst_raw = rec.get("dst") or rec.get("orig_dst")
    if not dst_raw:
        raise OSError("committed job record is missing destination path")
    actual_dst = Path(dst_raw)
    try:
        dst_stat = os.lstat(actual_dst)
    except OSError as exc:
        raise OSError(
            f"previously committed destination missing or unreadable: {actual_dst}: {exc}"
        ) from exc
    if not stat.S_ISREG(dst_stat.st_mode):
        raise OSError(
            f"previously committed destination is not a regular file: {actual_dst}"
        )
    expected_size = rec.get("size")
    if expected_size is not None and dst_stat.st_size != int(expected_size):
        raise OSError(
            f"previously committed destination size mismatch at {actual_dst}: "
            f"expected {expected_size}, found {dst_stat.st_size}"
        )
    rec_dev = rec.get("dst_dev")
    rec_ino = rec.get("dst_ino")
    if (
        rec_dev is not None
        and rec_ino is not None
        and dst_stat.st_dev == int(rec_dev)
        and dst_stat.st_ino != int(rec_ino)
    ):
        raise OSError(
            f"previously committed destination inode mismatch at {actual_dst}"
        )
    if src.exists():
        raise OSError(
            f"staged source still present for committed destination {actual_dst}: {src}"
        )
    return actual_dst


def _cleanup_owned_claims(unpacked_jobs: list[tuple]) -> int:
    cleaned = 0
    for (
        _src,
        _dst,
        _,
        protected,
        placeholder_identity,
        claim_path,
        _,
        _,
    ) in unpacked_jobs:
        if (
            protected
            and placeholder_identity is not None
            and claim_path is not None
        ):
            _remove_zero_placeholder(claim_path, placeholder_identity)
            cleaned += 1
    return cleaned


def _run_finalize(
    args,
    result: dict,
    item: QueueItem,
    disc_n: int,
    *,
    on_complete=None,
    on_failure=None,
    on_file_committed=None,
    on_verified=None,
) -> bool:
    """Verify staged files before publish, execute resumable NAS moves, clean staging, receipt, commit, and subocr."""
    staging = result.get("staging")
    jobs = result.get("move_jobs") or []
    files = list(result.get("files", []))
    preverify_futures = result.pop("_preverify_futures", None)
    preverify_pool = result.pop("_preverify_pool", None)
    allow_movie_overwrite = bool(
        getattr(args, "overwrite", False) and getattr(item, "type", None) == "movie"
    )
    media_committed = False
    failure_reported = False

    def _report_failure(err_msg: str) -> None:
        nonlocal failure_reported
        if failure_reported or media_committed:
            return
        failure_reported = True
        if on_failure is not None:
            try:
                on_failure(err_msg)
            except Exception:
                pass

    try:
        verified = True
        with _FINALIZE_LOCK:
            unpacked_jobs = [_unpack_move_job(job) for job in jobs]
            committed_jobs = _load_committed_jobs(result, staging)
            result["committed_jobs"] = list(committed_jobs)

            # Classify jobs into already-committed (from an earlier partial batch) vs pending.
            pending_indices: list[int] = []
            try:
                for i, unpacked in enumerate(unpacked_jobs):
                    src, orig_dst_raw = unpacked[0], unpacked[1]
                    src_path = Path(src)
                    orig_dst = Path(orig_dst_raw)
                    rec = _find_committed_record(committed_jobs, src_path, orig_dst)
                    if rec is not None:
                        actual_dst = _verify_committed_destination(rec, src_path)
                        if str(actual_dst) != str(orig_dst):
                            files = [
                                str(actual_dst) if fp == str(orig_dst) else fp
                                for fp in files
                            ]
                    else:
                        pending_indices.append(i)
            except BaseException as e:
                cleaned_claims = _cleanup_owned_claims(unpacked_jobs)
                err_msg = f"MOVE_FAIL {item.title} disc{disc_n}: {e}"
                _record_finalize_error(err_msg)
                append_log(
                    args,
                    f"MOVE_FAIL {item.title} disc{disc_n} :: {e} "
                    f"(RIP PRESERVED at {staging}; "
                    f"{cleaned_claims} owned claim(s) cleaned)",
                )
                print(f"{C.RED}Background NAS move failed: {e}{C.R}", flush=True)
                beep(args, ok=False)
                _report_failure(err_msg)
                if not isinstance(e, Exception):
                    raise
                return False

            # Join any pipelined pre-verify futures started during multi-title ripping.
            preverified: dict[str, tuple[int, int, bool, str]] = dict(
                result.get("preverified") or {}
            )
            if preverify_futures:
                for p_str, (snap_sz, snap_mtime, fut) in preverify_futures.items():
                    try:
                        p_ok, p_err = fut.result()
                    except Exception as exc:
                        p_ok, p_err = False, f"{p_str}: pre-verify error: {exc}"
                    preverified[p_str] = (
                        snap_sz,
                        snap_mtime,
                        bool(p_ok),
                        str(p_err or ""),
                    )
                result["preverified"] = preverified

            # Pre-publish verification: decode-verify staged files BEFORE moving any bytes
            # into the live library so corrupt/truncated rips never become visible at final
            # filenames and staged sources stay 100% intact on failure.
            if getattr(args, "verify", False) and pending_indices:
                staged_sources = [str(Path(unpacked_jobs[i][0])) for i in pending_indices]
                ok_pre, err_pre = True, ""
                remaining_to_verify: list[str] = []
                for src_s in staged_sources:
                    cached = preverified.get(src_s)
                    if cached is not None:
                        c_size, c_mtime, c_ok, c_err = cached
                        try:
                            st = os.stat(src_s)
                            stat_matches = (
                                st.st_size == c_size and st.st_mtime_ns == c_mtime
                            )
                        except OSError:
                            stat_matches = False
                        if stat_matches:
                            if not c_ok:
                                ok_pre, err_pre = False, c_err
                                break
                            continue
                    remaining_to_verify.append(src_s)
                if ok_pre and remaining_to_verify:
                    ok_pre, err_pre = verify(remaining_to_verify)
                if not ok_pre:
                    verified = False
                    _cleanup_owned_claims([unpacked_jobs[i] for i in pending_indices])
                    err_msg = f"VERIFY_FAIL {item.title} disc{disc_n}: {err_pre}"
                    _record_finalize_error(err_msg)
                    append_log(
                        args, f"VERIFY_FAIL {item.title} disc{disc_n} :: {err_pre}"
                    )
                    print(
                        f"{C.RED}Verify failed (pre-publish): {err_pre}{C.R}",
                        flush=True,
                    )
                    beep(args, ok=False)
                    _report_failure(err_msg)
                    return False
                if on_verified is not None:
                    try:
                        on_verified()
                    except Exception:
                        pass

            done_pending_pos = 0
            try:
                for pos, i in enumerate(pending_indices):
                    (
                        src,
                        dst,
                        label,
                        protected,
                        placeholder_identity,
                        claim_path,
                        partial_root,
                        lock_dir,
                    ) = unpacked_jobs[i]
                    src_path = Path(src)
                    orig_dst = Path(dst)
                    dst = orig_dst
                    writable_dir = _await_dir_writable(dst.parent, staging)
                    if not writable_dir:
                        raise OSError(f"target {dst.parent} not writable after retries")
                    if writable_dir != dst.parent:
                        dst = writable_dir / dst.name
                        if claim_path is not None:
                            nfs_claim = _to_nfs_path(claim_path)
                            if nfs_claim is not None:
                                claim_path = nfs_claim
                                placeholder_identity = _rebind_claim_identity(
                                    claim_path, placeholder_identity
                                )
                        if partial_root is not None:
                            partial_root = _to_nfs_path(partial_root) or partial_root
                        if lock_dir is not None:
                            lock_dir = _to_nfs_path(lock_dir) or lock_dir
                        unpacked_jobs[i] = (
                            src,
                            dst,
                            label,
                            protected,
                            placeholder_identity,
                            claim_path,
                            partial_root,
                            lock_dir,
                        )
                        files = [
                            str(dst) if fp == str(orig_dst) else fp for fp in files
                        ]
                    expected_size: int | None = None
                    try:
                        expected_size = os.lstat(src_path).st_size
                    except OSError:
                        expected_size = None
                    if protected:
                        move_with_progress_noclobber(
                            src_path,
                            dst,
                            label=label,
                            placeholder_identity=placeholder_identity,
                            claim_path=claim_path,
                            partial_root=partial_root,
                            lock_dir=lock_dir,
                        )
                    elif allow_movie_overwrite:
                        move_with_progress(
                            src_path, dst, label=label, allow_overwrite=True
                        )
                    else:
                        move_with_progress(src_path, dst, label=label)

                    dst_stat = os.lstat(dst)
                    if not stat.S_ISREG(dst_stat.st_mode):
                        raise OSError(f"published destination is not a regular file: {dst}")
                    if expected_size is not None and dst_stat.st_size != expected_size:
                        raise OSError(
                            f"published destination size mismatch at {dst}: "
                            f"expected {expected_size}, got {dst_stat.st_size}"
                        )
                    commit_rec = {
                        "src": str(src_path),
                        "orig_dst": str(orig_dst),
                        "dst": str(dst),
                        "size": int(dst_stat.st_size),
                        "dst_dev": int(dst_stat.st_dev),
                        "dst_ino": int(dst_stat.st_ino),
                        "label": str(label),
                        "committed_at": time.time(),
                    }
                    committed_jobs.append(commit_rec)
                    result["committed_jobs"] = list(committed_jobs)
                    _save_committed_manifest(staging, committed_jobs)
                    if on_file_committed is not None:
                        try:
                            on_file_committed(dict(commit_rec))
                        except Exception:
                            pass
                    done_pending_pos = pos + 1
            except BaseException as e:
                # Only clean owned zero-byte TV claim placeholders (verified by
                # exact inode identity) for uncommitted jobs. Never touch
                # pre-existing destinations; owned `.partial` artifacts are rolled
                # back inside move_with_progress / move_with_progress_noclobber.
                remaining_unpacked = [
                    unpacked_jobs[idx] for idx in pending_indices[done_pending_pos:]
                ]
                cleaned_claims = _cleanup_owned_claims(remaining_unpacked)
                err_msg = f"MOVE_FAIL {item.title} disc{disc_n}: {e}"
                _record_finalize_error(err_msg)
                append_log(
                    args,
                    f"MOVE_FAIL {item.title} disc{disc_n} :: {e} "
                    f"(RIP PRESERVED at {staging}; "
                    f"{cleaned_claims} owned claim(s) cleaned)",
                )
                print(f"{C.RED}Background NAS move failed: {e}{C.R}", flush=True)
                print(
                    f"{C.YLW}  Staged rip preserved at {staging}; fix the mount "
                    f"and move it manually.{C.R}",
                    flush=True,
                )
                beep(args, ok=False)
                _report_failure(err_msg)
                if not isinstance(e, Exception):
                    raise
                return False

        result["files"] = files
        if files and getattr(args, "state", None):
            log_final_durations(args, item, disc_n, files)
        # If there were no move_jobs (files already at destination), still verify them.
        if getattr(args, "verify", False) and files and not jobs:
            ok2, err = verify(files)
            if not ok2:
                verified = False
                err_msg = f"VERIFY_FAIL {item.title} disc{disc_n}: {err}"
                _record_finalize_error(err_msg)
                append_log(args, f"VERIFY_FAIL {item.title} disc{disc_n} :: {err}")
                print(f"{C.RED}Verify failed (background): {err}{C.R}", flush=True)
                beep(args, ok=False)
                _report_failure(err_msg)
        if verified:
            if not jobs and getattr(args, "verify", False) and on_verified is not None:
                try:
                    on_verified()
                except Exception:
                    pass
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
            fingerprint = result.get("disc_fingerprint")
            if (
                getattr(args, "verify", False)
                and fingerprint
                and files
                and not getattr(args, "rerip_review", False)
                and not result.get("routed_to_review", False)
            ):
                append_disc_receipt(
                    Path(args.disc_receipts), item, disc_n, fingerprint, files
                )
            if on_complete is not None:
                on_complete(result)
            media_committed = True
            try:
                run_subocr_postrip(args, files)
            except Exception as ocr_exc:
                warn_msg = (
                    f"SUBOCR_WARN {item.title} disc{disc_n} :: "
                    f"{type(ocr_exc).__name__}: {ocr_exc}"
                )
                try:
                    append_log(args, warn_msg)
                except Exception:
                    pass
                print(
                    f"{C.YLW}Warning: subtitle OCR launch failed "
                    f"({type(ocr_exc).__name__}): {ocr_exc} "
                    f"(verified media already committed).{C.R}",
                    flush=True,
                )
        return verified
    except Exception as exc:
        if media_committed:
            warn_msg = (
                f"SUBOCR_WARN {item.title} disc{disc_n} :: "
                f"post-commit error ({type(exc).__name__}: {exc})"
            )
            try:
                append_log(args, warn_msg)
            except Exception:
                pass
            print(
                f"{C.YLW}Warning: post-commit step failed ({type(exc).__name__}): "
                f"{exc} (verified media already committed).{C.R}",
                flush=True,
            )
            return True
        err_msg = (
            f"FINALIZE_FAIL {item.title} disc{disc_n}: "
            f"{type(exc).__name__}: {exc}"
        )
        _record_finalize_error(err_msg)
        try:
            append_log(
                args,
                f"FINALIZE_FAIL {item.title} disc{disc_n} :: "
                f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass
        print(
            f"{C.RED}Background finalize failed ({type(exc).__name__}): {exc}{C.R}",
            flush=True,
        )
        try:
            beep(args, ok=False)
        except Exception:
            pass
        _report_failure(err_msg)
        return False
    finally:
        if preverify_pool is not None:
            preverify_pool.shutdown(wait=True)


def start_background_finalize(
    args,
    result: dict,
    item: QueueItem,
    disc_n: int,
    *,
    on_complete=None,
    on_failure=None,
    on_file_committed=None,
    on_verified=None,
) -> None:
    t = threading.Thread(
        target=_run_finalize,
        args=(args, result, item, disc_n),
        kwargs={
            "on_complete": on_complete,
            "on_failure": on_failure,
            "on_file_committed": on_file_committed,
            "on_verified": on_verified,
        },
        daemon=False,
    )
    t.start()
    _FINALIZE_THREADS.append(t)
    print(
        f"{C.D}  NAS transfer running in background; drive is free for the "
        f"next disc.{C.R}",
        flush=True,
    )

def join_finalizers() -> None:
    pending = [t for t in _FINALIZE_THREADS if t.is_alive()]
    if pending:
        print(
            f"{C.D}Waiting for {len(pending)} background NAS transfer(s) "
            f"to finish before exit...{C.R}",
            flush=True,
        )
    for t in _FINALIZE_THREADS:
        t.join()


def resolve_fallback_title_mapping(
    rips: list[Path], info: dict, file_playlists: dict[str, str]
) -> dict[Path, int]:
    """Map fallback `.mkv` files to proven MakeMKV title IDs via TINFO[16] `.mpls` names.

    Only returns unambiguous 1-to-1 mappings where an output's source `.mpls`
    playlist matches exactly one MakeMKV title in `info['titles']`.
    """
    if not file_playlists:
        return {}
    mpls_to_tids: dict[str, list[int]] = {}
    for tid, attrs in (info.get("titles") or {}).items():
        if not isinstance(attrs, dict):
            continue
        raw_src = str(attrs.get(16, "") or "").strip()
        if raw_src.lower().endswith(".mpls"):
            key = Path(raw_src).name.casefold()
            mpls_to_tids.setdefault(key, []).append(int(tid))
    proven: dict[Path, int] = {}
    used_tids: set[int] = set()
    for src in rips:
        raw_pl = file_playlists.get(str(src)) or file_playlists.get(src.name)
        if not raw_pl:
            continue
        key = Path(raw_pl).name.casefold()
        tids = mpls_to_tids.get(key) or []
        if len(tids) == 1 and tids[0] not in used_tids:
            proven[src] = tids[0]
            used_tids.add(tids[0])
    return proven


def rip(
    args,
    item: QueueItem,
    info: dict,
    title_ids: list[int],
    target_dir: Path,
    state: dict,
    disc_n: int,
):
    """Rip one disc to local staging, optionally falling back to mkvmerge for
    unencrypted BDs on MakeMKV failure, then publish safely to NAS."""
    preverify_holder: dict = {"pool": None, "transferred": False}
    try:
        ok, payload = _rip_impl(
            args, item, info, title_ids, target_dir, state, disc_n, preverify_holder
        )
        if not ok:
            clear_tv_slot_reservation(os.getpid())
        return ok, payload
    finally:
        pool = preverify_holder.get("pool")
        if pool is not None and not preverify_holder.get("transferred"):
            pool.shutdown(wait=False, cancel_futures=True)


def _rip_impl(
    args,
    item: QueueItem,
    info: dict,
    title_ids: list[int],
    target_dir: Path,
    state: dict,
    disc_n: int,
    preverify_holder: dict,
):
    if item.type == "tv-season" and getattr(args, "overwrite", False):
        return False, "--overwrite is forbidden for TV in every mode"

    target_dir = resolve_nas_path(
        target_dir, prefer_nfs=(item.type == "tv-season")
    )

    if item.type == "movie":
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            final_target = target_dir / movie_filename(item, disc_n)
            movie_exists = final_target.exists()
        except OSError:
            nfs_alt = _to_nfs_path(target_dir)
            if nfs_alt is not None and _dir_writable(nfs_alt):
                print(
                    f"{C.GRN}  SMB movie target unavailable at preflight; "
                    f"routing through NFS ({nfs_alt}).{C.R}"
                )
                target_dir = nfs_alt
                final_target = target_dir / movie_filename(item, disc_n)
                movie_exists = final_target.exists()
            else:
                raise
        output_dir = target_dir
        if movie_exists and not args.overwrite:
            return False, f"target file exists: {final_target} (use --overwrite)"
    else:
        if getattr(args, "rerip_review", False):
            contract_error = apply_tv_output_mode(
                args, item, target_dir, "backend preflight"
            )
        else:
            with season_dir_lock(target_dir, what="precheck"):
                contract_error = apply_tv_output_mode(
                    args, item, target_dir, "backend preflight"
                )
        if contract_error:
            return False, contract_error

        if getattr(args, "rerip_review", False):
            try:
                output_dir = tv_review_capture_dir(
                    item,
                    target_dir,
                    getattr(args, "current_disc_fingerprint", ""),
                    getattr(args, "review_root", None),
                )
            except ValueError as exc:
                return False, str(exc)
        else:
            output_dir = target_dir
        output_dir.mkdir(parents=True, exist_ok=True)

    free_gb = _free_gb(output_dir)
    if free_gb < args.min_free_gb:
        return (
            False,
            f"low disk space at target: {free_gb:.1f}GB free, need {args.min_free_gb}GB",
        )

    LOCAL_STAGING_BASE.mkdir(parents=True, exist_ok=True)
    local_free_gb = shutil.disk_usage(LOCAL_STAGING_BASE).free / 1e9
    skip_extras = bool(getattr(args, "no_extras", False))
    local_need, selected_gb, legacy_space_fallback = staging_space_requirement(
        item,
        info,
        title_ids,
        split_playall=not getattr(args, "no_split_playall", False),
        skip_extras=skip_extras,
    )
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
            try:
                os.kill(owner_pid, 0)
                active_other_rips += 1
            except (ProcessLookupError, PermissionError):
                pass
    except OSError:
        pass
    space_error = staging_space_error(
        local_free_gb,
        local_need,
        item.format,
        selected_gb,
        legacy_space_fallback,
        active_other_rips,
    )
    if space_error:
        return False, space_error
    if legacy_space_fallback:
        budget_detail = "legacy-safe fallback (title byte metadata unavailable)"
    else:
        budget_detail = (
            f"{selected_gb:.1f}GB selected + 5% + "
            f"{STAGING_HEADROOM_GB:.0f}GB headroom"
        )
    print(
        f"{C.D}  staging budget: need {local_need:.1f}GB "
        f"({budget_detail}); {local_free_gb:.1f}GB free{C.R}"
    )

    staging = LOCAL_STAGING_BASE / f"{int(time.time())}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    (staging / ".ignore").write_text("burndvd staging dir\n", encoding="utf-8")

    if item.type == "movie" and len(title_ids) != 1:
        return False, f"movie rip expects 1 title, got {title_ids}"
    selectors, skipped_play_all = planned_rip_selectors(
        item, info, title_ids, skip_extras=skip_extras
    )
    if (
        item.type == "tv-season"
        and item.season
        and title_ids
        and not getattr(args, "rerip_review", False)
    ):
        _res_start = (
            int(item.episode_start)
            if item.episode_start and int(item.episode_start) > 0
            else (scan_season_highest_episode(target_dir, int(item.season)) + 1)
        )
        write_tv_slot_reservation(
            pid=os.getpid(),
            title=item.title,
            season=int(item.season),
            target_root=str(item.target_root or target_dir.parent),
            episode_start=_res_start,
            episode_count=len(title_ids),
        )
    if skipped_play_all:
        print(
            f"{C.D}  skipping play-all duplicate title(s) {sorted(skipped_play_all)}; "
            f"ripping {len(selectors)} title(s) directly{C.R}"
        )
    elif skip_extras and item.type == "tv-season" and selectors != ["all"]:
        print(
            f"{C.D}  --no-extras: ripping {len(selectors)} episode title(s) directly{C.R}"
        )

    print(f"{C.D}  staging: {staging}{C.R}")
    start = time.time()
    stall_sec = stall_timeout_for(item.format)
    fail_reason = None
    multi_sel = len(selectors) > 1

    do_preverify = bool(getattr(args, "verify", False)) and multi_sel
    preverify_pool: concurrent.futures.ThreadPoolExecutor | None = (
        concurrent.futures.ThreadPoolExecutor(max_workers=2) if do_preverify else None
    )
    preverify_holder["pool"] = preverify_pool
    preverify_futures: dict[str, tuple[int, int, concurrent.futures.Future]] = {}
    seen_staged_mkvs: set[Path] = set()

    def _preverify_one(path_str: str) -> tuple[bool, str]:
        return verify([path_str])

    def _attach_preverify(target_payload: dict) -> None:
        if not preverify_futures:
            return
        completed: dict[str, tuple[int, int, bool, str]] = {}
        pending_futs: dict[str, tuple[int, int, concurrent.futures.Future]] = {}
        for p_str, (snap_sz, snap_mtime, fut) in preverify_futures.items():
            if fut.done():
                try:
                    p_ok, p_err = fut.result()
                except Exception as exc:
                    p_ok, p_err = False, f"{p_str}: pre-verify error: {exc}"
                completed[p_str] = (
                    snap_sz,
                    snap_mtime,
                    bool(p_ok),
                    str(p_err or ""),
                )
            else:
                pending_futs[p_str] = (snap_sz, snap_mtime, fut)
        target_payload["preverified"] = completed
        if pending_futs and preverify_pool is not None:
            target_payload["_preverify_futures"] = pending_futs
            target_payload["_preverify_pool"] = preverify_pool
            preverify_holder["transferred"] = True

    for sel_idx, selector in enumerate(selectors, 1):
        sel_start = time.time()
        resolved_device = resolve_makemkv_device(args)
        cmd = [
            args.makemkvcon,
            "-r",
            "--progress=-same",
            "--noscan",
            "--cache=1024",
            f"--minlength={EXTRAS_MIN_DUR_S}",
            "mkv",
            resolved_device,
            selector,
            str(staging),
        ]

        prefix = f"[{sel_idx}/{len(selectors)}] " if multi_sel else ""
        print(f"{C.D}{prefix}$ {' '.join(cmd)}{C.R}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

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

        try:
            while True:
                try:
                    kind, payload = q.get(timeout=2.0)
                except queue.Empty:
                    stall_age = time.time() - last_progress_time
                    if stall_age > stall_sec:
                        try:
                            recent_write = max(
                                (p.stat().st_mtime for p in staging.rglob("*.mkv")),
                                default=0.0,
                            )
                        except OSError:
                            recent_write = 0.0
                        if time.time() - recent_write < 60:
                            last_progress_time = time.time()
                        else:
                            sys.stdout.write("\n")
                            print(
                                f"{C.RED}Stalled: no progress for {int(stall_age)}s "
                                f"(threshold {stall_sec}s for {item.format}, "
                                f"no staging write in {int(time.time() - recent_write)}s); "
                                f"killing makemkvcon.{C.R}"
                            )
                            stalled = True
                            proc.terminate()
                            try:
                                proc.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.wait()
                            break
                    disp_task = f"{prefix}{cur_task}" if multi_sel else cur_task
                    _draw_progress(
                        sel_start,
                        total_v,
                        mx,
                        disp_task,
                        min(stall_age, _staging_write_age(staging)),
                    )
                    continue
                if kind == "eof":
                    break
                line = payload.rstrip()
                tag, f = parse_line(line)
                if tag == "PRGV" and f and len(f) >= 3:
                    try:
                        cur_v, total_v, mx = int(f[0]), int(f[1]), int(f[2])
                        if total_v > last_progress_total:
                            last_progress_total = total_v
                            last_progress_time = time.time()
                    except ValueError:
                        pass
                elif tag == "PRGT" and f and len(f) >= 3:
                    cur_task = f[2]
                elif tag == "MSG" and f and len(f) >= 4:
                    msgs.append(f[3])
                    if len(msgs) > 50:
                        msgs = msgs[-50:]
                now = time.time()
                if now - last_print > 0.5 and mx > 0:
                    disp_task = f"{prefix}{cur_task}" if multi_sel else cur_task
                    _draw_progress(
                        sel_start,
                        total_v,
                        mx,
                        disp_task,
                        min(now - last_progress_time, _staging_write_age(staging)),
                    )
                    last_print = now
        except KeyboardInterrupt:
            interrupted = True
            sys.stdout.write("\n")
            print(f"{C.YLW}Interrupted; killing makemkvcon...{C.R}")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            if not interrupted and not stalled:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

        sys.stdout.write("\n")

        if stalled:
            fail_reason = (
                f"{prefix}stalled at PRGV total={last_progress_total}/{mx}; "
                f"last MSGs: {' | '.join(msgs[-3:])}"
            )
        elif proc.returncode != 0:
            tail = " | ".join(msgs[-5:]) or "(no MSG output)"
            fail_reason = f"{prefix}makemkvcon exit {proc.returncode}; last MSGs: {tail}"
        else:
            summary = parse_save_summary(msgs)
            if summary is None:
                fail_reason = (
                    f"{prefix}no save-summary MSG found; "
                    f"last MSGs: {' | '.join(msgs[-5:])}"
                )
            else:
                saved, failed = summary
                if saved == 0:
                    fail_reason = (
                        f"{prefix}makemkvcon save summary: 0 saved, {failed} "
                        f"failed; last MSGs: {' | '.join(msgs[-5:])}"
                    )
                elif failed > 0:
                    fail_reason = (
                        f"{prefix}makemkvcon save summary: {saved} saved, "
                        f"{failed} failed (partial)"
                    )
        if fail_reason is not None:
            break
        if preverify_pool is not None:
            for new_mkv in sorted(staging.glob("*.mkv")):
                if new_mkv in seen_staged_mkvs:
                    continue
                seen_staged_mkvs.add(new_mkv)
                try:
                    st = new_mkv.stat()
                except OSError:
                    continue
                fut = preverify_pool.submit(_preverify_one, str(new_mkv))
                preverify_futures[str(new_mkv)] = (st.st_size, st.st_mtime_ns, fut)

    elapsed = time.time() - start
    used_fallback = False
    fallback_file_playlists: dict[str, str] = {}
    rips: list[Path] = []

    if fail_reason is not None:
        # Automatic mkvmerge unencrypted-BD fallback (enabled by default for BD/4K).
        if (
            (item.format or "").upper() in ("BD", "4K")
            and getattr(args, "allow_mkvmerge_fallback", True)
        ):
            print(
                f"{C.YLW}MakeMKV failed ({fail_reason}); checking for unencrypted-BD "
                f"mkvmerge fallback...{C.R}",
                flush=True,
            )
            fb = mkvmerge_fallback_rip(args, item, staging)
            if fb.ok and fb.files:
                used_fallback = True
                fallback_file_playlists = dict(getattr(fb, "file_playlists", None) or {})
                rips = sorted(fb.files)
                elapsed = time.time() - start
                append_log(
                    args,
                    f"MKVMERGE_FALLBACK_OK {item.title} disc{disc_n} "
                    f"files={len(rips)} warnings={len(fb.warnings)}",
                )
                print(
                    f"{C.GRN}  mkvmerge unencrypted-BD fallback succeeded: "
                    f"{len(rips)} verified .mkv file(s){C.R}",
                    flush=True,
                )
            else:
                print(
                    f"{C.D}  mkvmerge fallback declined/failed: {fb.error}{C.R}",
                    flush=True,
                )
                append_log(
                    args,
                    f"MKVMERGE_FALLBACK_SKIP {item.title} disc{disc_n} :: {fb.error}",
                )
        if not used_fallback:
            return _salvage_and_fail(
                args, item, info, disc_n, staging, target_dir, fail_reason
            )
    else:
        rips = sorted(staging.glob("*.mkv"))
        if not rips:
            shutil.rmtree(staging, ignore_errors=True)
            return False, "no .mkv files in staging despite save-summary success"

    if item.type == "tv-season" and not getattr(args, "rerip_review", False):
        with season_dir_lock(target_dir, what="post-rip output decision"):
            contract_error = apply_tv_output_mode(
                args, item, target_dir, "post-rip output decision"
            )
        if contract_error:
            return False, f"{contract_error}; RIP PRESERVED at {staging}"
        if getattr(args, "rerip_review", False):
            try:
                output_dir = tv_review_capture_dir(
                    item,
                    target_dir,
                    getattr(args, "current_disc_fingerprint", ""),
                    getattr(args, "review_root", None),
                )
            except ValueError as exc:
                return False, f"{exc}; RIP PRESERVED at {staging}"
            output_dir.mkdir(parents=True, exist_ok=True)

    move_jobs: list = []

    def _emit_move(
        src: Path,
        dst: Path,
        label: str,
        *,
        placeholder_identity: tuple[int, int] | None = None,
        claim_path: Path | None = None,
        publish_lock_held: bool = False,
    ):
        protected = item.type == "tv-season"
        protected_lock_dir = dst.parent if protected else None
        if getattr(args, "sync_move", False):
            if protected:
                move_with_progress_noclobber(
                    src,
                    dst,
                    label=label,
                    placeholder_identity=placeholder_identity,
                    claim_path=claim_path,
                    partial_root=protected_partial_root,
                    lock_dir=protected_lock_dir,
                    publish_lock_held=publish_lock_held,
                )
            elif bool(getattr(args, "overwrite", False) and item.type == "movie"):
                move_with_progress(src, dst, label=label, allow_overwrite=True)
            else:
                move_with_progress(src, dst, label=label)
        else:
            move_jobs.append(
                (
                    src,
                    dst,
                    label,
                    protected,
                    placeholder_identity,
                    claim_path,
                    protected_partial_root,
                    protected_lock_dir,
                )
            )

    _dur_cache: dict[str, int] = {}

    def _src_dur_s(src: Path) -> int:
        if not used_fallback:
            tnum = title_num_from_filename(src.name)
            if tnum is not None:
                d = parse_duration(info.get("titles", {}).get(tnum, {}).get(9, "0:00:00"))
                if d:
                    return d
        key = str(src)
        if key not in _dur_cache:
            _dur_cache[key] = int(ffprobe_dur_s(key))
        return _dur_cache[key]

    def _move_extras(
        extra_srcs: list[Path],
        extras_dir: Path,
        name_for,
        *,
        allow_disambiguate: bool = True,
    ):
        if not extra_srcs:
            return
        extras_dir.mkdir(parents=True, exist_ok=True)
        allow_overwrite = bool(args.overwrite and item.type == "movie")
        fp_tag = re.sub(
            r"[^0-9a-f]",
            "",
            (getattr(args, "current_disc_fingerprint", "") or "").casefold(),
        )[:8]
        planned_extras: list[tuple[Path, Path, int | None]] = []
        reserved_dests: set[Path] = set()
        for src in extra_srcs:
            tnum = title_num_from_filename(src.name)
            dest = extras_dir / name_for(src, tnum)
            if (
                (dest.exists() or dest in reserved_dests)
                and not allow_overwrite
                and allow_disambiguate
            ):
                stem, suffix = dest.stem, dest.suffix
                candidates = []
                if fp_tag:
                    candidates.append(extras_dir / f"{stem} - {fp_tag}{suffix}")
                for seq in range(2, 1000):
                    candidates.append(extras_dir / f"{stem} - dup{seq}{suffix}")
                chosen = next(
                    (
                        c
                        for c in candidates
                        if not c.exists() and c not in reserved_dests
                    ),
                    None,
                )
                if chosen is not None:
                    print(
                        f"{C.YLW}  extras collision: {dest.name} exists — "
                        f"disambiguating as {chosen.name}{C.R}"
                    )
                    dest = chosen
            try:
                require_free_extra_destination(src, dest, allow_overwrite)
            except FileExistsError:
                print(
                    f"{C.YLW}  extras collision: {dest.name} exists; "
                    f"staging PRESERVED at {staging}.{C.R}"
                )
                raise
            reserved_dests.add(dest)
            planned_extras.append((src, dest, tnum))
        for src, dest, tnum in planned_extras:
            _emit_move(
                src,
                dest,
                f"moving extra t{tnum:02d}" if tnum is not None else "moving extra",
            )
            finals.append(str(dest))
        print(f"{C.GRN}  {len(extra_srcs)} extra(s) -> {extras_dir}{C.R}")

    def _dest_writable() -> bool:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            probe = output_dir / f".writeprobe-{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            return False

    if not _dest_writable():
        _nfs_dir = Path(
            str(output_dir).replace(SMB_MEDIA_PREFIX + "/", NFS_MEDIA_PREFIX + "/")
        )

        def _nfs_writable() -> bool:
            if NFS_MEDIA_PREFIX + "/" not in str(_nfs_dir):
                return False
            try:
                _nfs_dir.mkdir(parents=True, exist_ok=True)
                _pr = _nfs_dir / f".writeprobe-{os.getpid()}"
                _pr.write_text("ok", encoding="utf-8")
                _pr.unlink()
                return True
            except OSError:
                return False

        _recovered = False
        for attempt in range(1, 13):
            if _nfs_writable():
                print(
                    f"{C.GRN}  SMB target unwritable; routing this transfer "
                    f"through the NFS mount of the same NAS dir ({_nfs_dir}).{C.R}"
                )
                output_dir = _nfs_dir
                if item.type == "tv-season" and getattr(args, "rerip_review", False):
                    nfs_target = _to_nfs_path(target_dir)
                    if nfs_target is not None:
                        target_dir = nfs_target
                else:
                    target_dir = output_dir
                _recovered = True
                break
            print(
                f"{C.YLW}  Target {output_dir} not writable "
                f"(attempt {attempt}/12) — SMB reconnecting and NFS also "
                f"down; retrying in 50s. Staged rip is safe at {staging}.{C.R}"
            )
            time.sleep(50)
            if _dest_writable():
                print(f"{C.GRN}  Target writable again; continuing move.{C.R}")
                _recovered = True
                break
        if not _recovered:
            return (
                False,
                f"target not writable after retries (SMB+NFS both down) — "
                f"RIP PRESERVED at {staging}; fix the mount and re-run "
                f"(staging was NOT deleted)",
            )

    if item.type == "tv-season":
        nfs_output = _to_nfs_path(output_dir)
        nfs_target = _to_nfs_path(target_dir)
        if nfs_output is not None or nfs_target is not None:
            cand_output = nfs_output or output_dir
            cand_target = nfs_target or target_dir
            if not _dir_writable(cand_output) or not _dir_writable(cand_target):
                return (
                    False,
                    f"atomic TV publication backend (NFS) is unavailable; RIP "
                    f"PRESERVED at {staging}",
                )
            output_dir = cand_output
            target_dir = cand_target
            print(f"{C.D}  TV publication routed through atomic NFS view: {output_dir}{C.R}")

    protected_partial_root = None
    normal_tv_extras_dir = None
    if item.type == "tv-season":
        try:
            normal_tv_extras_dir = tv_show_extras_dir(target_dir, item)
            protected_partial_root = tv_partial_capture_root(
                item, target_dir, getattr(args, "review_root", None)
            )
        except ValueError as exc:
            return False, f"{exc}; RIP PRESERVED at {staging}"

    finals: list[str] = []
    if item.type == "movie":
        main = max(rips, key=lambda p: p.stat().st_size)
        target_path = target_dir / movie_filename(item, disc_n)
        if target_path.exists() and not args.overwrite:
            print(
                f"{C.YLW}  Collision: {target_path} exists — staging PRESERVED "
                f"at {staging}.{C.R}"
            )
            return (
                False,
                f"would overwrite {target_path} — RIP PRESERVED at "
                f"{staging} (not deleted; move/rename manually)",
            )
        movie_base = movie_filename(item, disc_n)[:-4]
        try:
            _move_extras(
                [p for p in rips if p != main],
                target_dir / "Extras",
                lambda src, tnum: (
                    f"{movie_base} - extra - "
                    f"{('t%02d' % tnum) if tnum is not None else src.stem}.mkv"
                ),
            )
        except FileExistsError as exc:
            return (
                False,
                f"extras collision: {exc} — RIP PRESERVED at "
                f"{staging} (not deleted; resolve manually)",
            )
        _emit_move(main, target_path, "moving main title")
        finals.insert(0, str(target_path))
    else:
        route_to_review = bool(getattr(args, "rerip_review", False))
        proven_fallback_map: dict[Path, int] = {}

        rips = split_chaptered_playall(args, rips, staging)
        play_all = set() if used_fallback else play_all_title_ids(info)
        episode_rips = [
            p
            for p in rips
            if _src_dur_s(p) >= TV_MIN_DUR_S
            and title_num_from_filename(p.name) not in play_all
        ]
        extras_rips = [p for p in rips if _src_dur_s(p) < TV_MIN_DUR_S]

        if used_fallback and not route_to_review and episode_rips:
            proven_fallback_map = resolve_fallback_title_mapping(
                episode_rips, info, fallback_file_playlists
            )
            unverified_reason: str | None = None
            if item.expected_title_ids:
                tid_to_src = {tid: src for src, tid in proven_fallback_map.items()}
                missing_fb = [
                    tid for tid in item.expected_title_ids if tid not in tid_to_src
                ]
                if missing_fb:
                    unverified_reason = (
                        f"mkvmerge fallback has unverified episode identities for "
                        f"contracted title ids {item.expected_title_ids} "
                        f"(unmapped: {missing_fb})"
                    )
            else:
                unmapped = [p.name for p in episode_rips if p not in proven_fallback_map]
                if unmapped:
                    unverified_reason = (
                        f"mkvmerge fallback playlist-to-title mapping is unverified "
                        f"for {unmapped}"
                    )
            if unverified_reason:
                try:
                    output_dir = tv_review_capture_dir(
                        item,
                        target_dir,
                        getattr(args, "current_disc_fingerprint", ""),
                        getattr(args, "review_root", None),
                    )
                    output_dir.mkdir(parents=True, exist_ok=True)
                except (OSError, ValueError) as exc:
                    return (
                        False,
                        f"{unverified_reason}; cannot isolate to review ({exc}); "
                        f"RIP PRESERVED at {staging}",
                    )
                route_to_review = True
                print(
                    f"{C.YLW}  {unverified_reason} — routing fallback outputs to "
                    f"isolated review quarantine (not Season {item.season:02d}).{C.R}"
                )
                append_log(
                    args,
                    f"FALLBACK_UNVERIFIED_REVIEW {item.title} S{item.season:02d} "
                    f"disc{disc_n} :: {unverified_reason}",
                )

        _review_dir = None
        if route_to_review:
            try:
                _review_dir = reserve_tv_review_attempt_dir(output_dir, staging)
            except (OSError, ValueError) as exc:
                return (
                    False,
                    f"cannot reserve isolated review attempt: {exc}; RIP "
                    f"PRESERVED at {staging}",
                )
            print(f"{C.D}  review attempt: {_review_dir}{C.R}")

        _gcand = [(title_num_from_filename(pp.name), _src_dur_s(pp)) for pp in episode_rips]
        _gkeep, _gbonus, _gthr = episode_bonus_split(_gcand)
        if _gbonus:
            _gbonus_set = set(_gbonus)
            _gdemoted = [
                pp
                for pp in episode_rips
                if title_num_from_filename(pp.name) in _gbonus_set
            ]
            episode_rips = [
                pp
                for pp in episode_rips
                if title_num_from_filename(pp.name) not in _gbonus_set
            ]
            extras_rips = extras_rips + _gdemoted
            for _dp in _gdemoted:
                print(
                    f"{C.YLW}  demote t{title_num_from_filename(_dp.name):02d} "
                    f"({fmt_dur(_src_dur_s(_dp))}) -> Extras: short vs disc median "
                    f"{fmt_dur(int(_gthr))} (bonus, not an episode).{C.R}"
                )

        playall_srcs = [
            p
            for p in rips
            if _src_dur_s(p) >= TV_MIN_DUR_S
            and title_num_from_filename(p.name) in play_all
        ]
        if playall_srcs:
            ref = str(episode_rips[0]) if episode_rips else None
            shortest_ep = min((_src_dur_s(p) for p in episode_rips), default=0.0)
            salvaged_playall = []
            disc_lbl = f"S{item.season:02d}D{disc_n}"
            for src in playall_srcs:
                tnum = title_num_from_filename(src.name)
                if ref is None:
                    confirmed, detail = (
                        False,
                        "no kept episode to compare against (fail-safe -> salvage)",
                    )
                else:
                    confirmed, detail = verify_play_all(str(src), ref, shortest_ep)
                if confirmed:
                    print(
                        f"  {C.D}t{tnum:02d}: play-all CONFIRMED by frame "
                        f"compare — {detail}; dropping{C.R}"
                    )
                    append_log(
                        args,
                        f"PLAYALL {item.title} {disc_lbl} t{tnum:02d} "
                        f"skip (play-all CONFIRMED by frame compare) | {detail}",
                    )
                else:
                    print(
                        f"  {C.YLW}t{tnum:02d}: play-all flag REFUTED by frame "
                        f"compare — {detail}; SALVAGING to extras/{C.R}"
                    )
                    salvaged_playall.append(src)
            if salvaged_playall:
                try:
                    _move_extras(
                        salvaged_playall,
                        _review_dir if _review_dir is not None else normal_tv_extras_dir,
                        lambda src, tnum: (
                            f"{sanitize(item.title)} - {disc_lbl} bonus "
                            f"t{tnum:02d} (unverified play-all flag).mkv"
                        ),
                        allow_disambiguate=(_review_dir is None),
                    )
                except FileExistsError as exc:
                    return (
                        False,
                        f"extras collision: {exc} — RIP PRESERVED at "
                        f"{staging} (not deleted; resolve manually)",
                    )
                for src in salvaged_playall:
                    tnum = title_num_from_filename(src.name)
                    append_log(
                        args,
                        f"PLAYALL {item.title} {disc_lbl} t{tnum:02d} SALVAGE "
                        f"(play-all flag refuted by frame compare) → "
                        f"extras/{sanitize(item.title)} - {disc_lbl} bonus "
                        f"t{tnum:02d} (unverified play-all flag).mkv",
                    )

        try:
            if getattr(args, "rerip_review", False):
                assert _review_dir is not None
                _move_extras(
                    extras_rips,
                    _review_dir,
                    lambda src, tnum: (
                        f"{sanitize(item.title)} - S{item.season:02d}D{disc_n} - "
                        f"review-extra-{('t%02d' % tnum) if tnum is not None else src.stem}.mkv"
                    ),
                    allow_disambiguate=False,
                )
            else:
                _move_extras(
                    extras_rips,
                    normal_tv_extras_dir,
                    lambda src, tnum: (
                        f"{sanitize(item.title)} - S{item.season:02d}D{disc_n} - extra - "
                        f"{('t%02d' % tnum) if tnum is not None else src.stem}.mkv"
                    ),
                )
        except FileExistsError as exc:
            return (
                False,
                f"extras collision: {exc} — RIP PRESERVED at "
                f"{staging} (not deleted; resolve manually)",
            )

        if len(episode_rips) > 1:
            _by_tid = sorted(
                episode_rips, key=lambda pp: title_num_from_filename(pp.name) or 0
            )
            _kept, _dups = [], []
            for _p in _by_tid:
                _pd = _src_dur_s(_p)
                _orig = next(
                    (
                        k
                        for k in _kept
                        if abs(_src_dur_s(k) - _pd) <= ALT_DEDUP_DUR_TOL_S
                        and _alt_playlist_duplicate(str(k), str(_p), _pd)
                    ),
                    None,
                )
                if _orig is not None:
                    _dups.append((_p, _orig))
                else:
                    _kept.append(_p)
            for _p, _orig in _dups:
                print(
                    f"{C.YLW}  dedup: t{title_num_from_filename(_p.name):02d} is an "
                    f"alternate playlist of t{title_num_from_filename(_orig.name):02d} "
                    f"(frames match) - dropping from episodes.{C.R}"
                )
                append_log(
                    args,
                    f"ALTDUP {item.title} S{item.season:02d}D{disc_n} "
                    f"t{title_num_from_filename(_p.name):02d} == "
                    f"t{title_num_from_filename(_orig.name):02d} (dropped)",
                )
            episode_rips = _kept

        if route_to_review:
            try:
                _move_extras(
                    episode_rips,
                    _review_dir,
                    lambda src, tnum: (
                        f"{sanitize(item.title)} - S{item.season:02d}D{disc_n} - "
                        f"review-title-{('t%02d' % tnum) if tnum is not None else src.stem}.mkv"
                    ),
                    allow_disambiguate=False,
                )
            except FileExistsError as exc:
                return (
                    False,
                    f"extras collision: {exc} — RIP PRESERVED at "
                    f"{staging} (not deleted; resolve manually)",
                )
            episode_rips = []
        else:
            expected = item.expected_disc_episodes
            if expected > 0:
                if item.expected_title_ids and used_fallback:
                    tid_to_src = {tid: src for src, tid in proven_fallback_map.items()}
                    missing_ids = [
                        tid for tid in item.expected_title_ids if tid not in tid_to_src
                    ]
                    if missing_ids:
                        return (
                            False,
                            f"fallback disc is missing proven contracted title ids {missing_ids}; "
                            f"RIP PRESERVED at {staging}",
                        )
                    selected = [tid_to_src[tid] for tid in item.expected_title_ids]
                    selected_set = set(selected)
                    overflow = [p for p in episode_rips if p not in selected_set]
                    if overflow:
                        try:
                            _move_extras(
                                overflow,
                                normal_tv_extras_dir,
                                lambda src, tnum: (
                                    f"{sanitize(item.title)} - S{item.season:02d}D{disc_n} - "
                                    f"contract-extra-{('t%02d' % tnum) if tnum is not None else src.stem}.mkv"
                                ),
                            )
                        except FileExistsError as exc:
                            return (
                                False,
                                f"extras collision: {exc} — RIP PRESERVED at "
                                f"{staging} (not deleted; resolve manually)",
                            )
                    episode_rips = selected
                elif item.expected_title_ids and not used_fallback:
                    selected, overflow, missing_ids = partition_episode_contract(
                        episode_rips, item
                    )
                    if missing_ids:
                        return (
                            False,
                            f"physical disc is missing contracted title ids {missing_ids}; "
                            f"RIP PRESERVED at {staging}",
                        )
                    if overflow:
                        try:
                            _move_extras(
                                overflow,
                                normal_tv_extras_dir,
                                lambda src, tnum: (
                                    f"{sanitize(item.title)} - S{item.season:02d}D{disc_n} - "
                                    f"contract-extra-{('t%02d' % tnum) if tnum is not None else src.stem}.mkv"
                                ),
                            )
                        except FileExistsError as exc:
                            return (
                                False,
                                f"extras collision: {exc} — RIP PRESERVED at "
                                f"{staging} (not deleted; resolve manually)",
                            )
                    episode_rips = selected
                elif len(episode_rips) < expected:
                    return (
                        False,
                        f"physical disc yielded {len(episode_rips)} episode candidate(s), "
                        f"but its contract requires {expected}; RIP PRESERVED at {staging}",
                    )
                elif len(episode_rips) > expected:
                    overflow = episode_rips[expected:]
                    try:
                        _move_extras(
                            overflow,
                            normal_tv_extras_dir,
                            lambda src, tnum: (
                                f"{sanitize(item.title)} - S{item.season:02d}D{disc_n} - "
                                f"contract-overflow-{('t%02d' % tnum) if tnum is not None else src.stem}.mkv"
                            ),
                        )
                    except FileExistsError as exc:
                        return (
                            False,
                            f"extras collision: {exc} — RIP PRESERVED at "
                            f"{staging} (not deleted; resolve manually)",
                        )
                    episode_rips = episode_rips[:expected]

            existing = existing_episode_slots(item, target_dir)
            for candidate in episode_rips:
                duplicate, identity_error = existing_content_collision(
                    candidate, float(_src_dur_s(candidate)), existing
                )
                if identity_error:
                    return (
                        False,
                        f"content identity check failed closed: {identity_error}; "
                        f"RIP PRESERVED at {staging}",
                    )
                if duplicate is not None:
                    return (
                        False,
                        f"decoded content duplicates existing episode {duplicate}; "
                        f"refusing publication; RIP PRESERVED at {staging}",
                    )

        rips = episode_rips
        if not rips:
            print(f"{C.YLW}  No titles >= {TV_MIN_DUR_S}s — extras-only disc.{C.R}")
            early_payload: dict = {"elapsed_s": elapsed, "files": finals}
            if used_fallback:
                early_payload["used_fallback"] = True
            if route_to_review:
                early_payload["routed_to_review"] = True
            if getattr(args, "sync_move", False):
                shutil.rmtree(staging, ignore_errors=True)
                return True, early_payload
            early_payload["move_jobs"] = move_jobs
            early_payload["staging"] = str(staging)
            _attach_preverify(early_payload)
            return True, early_payload

        with season_dir_lock(target_dir, what="rename"):
            start_ep = starting_ep(item, disc_n, state, target_dir=target_dir)
            proposed = []
            for i, src in enumerate(rips):
                ep = start_ep + i
                tnum = title_num_from_filename(src.name)
                dur_str = (
                    info.get("titles", {}).get(tnum, {}).get(9, "?:??:??")
                    if (tnum is not None and not used_fallback)
                    else fmt_dur(_src_dur_s(src))
                )
                size_gb = src.stat().st_size / 1e9
                proposed.append((src, ep, tnum, dur_str, size_gb))

            print(f"\n{C.B}Proposed episode mapping (disc {disc_n}/{item.discs}):{C.R}")
            for src, ep, tnum, dur_str, size_gb in proposed:
                print(
                    f"  S{item.season:02d}E{ep:02d}  ←  {src.name}  ({dur_str}, {size_gb:.2f}GB)"
                )
            ans = auto_answer(args, f"{C.B}In broadcast order? [Y/n]:{C.R} ", "y")
            accept = ans in ("", "y", "yes")

            if accept:
                target_dir = resolve_nas_path(target_dir, prefer_nfs=True)
                planned = [
                    (
                        src,
                        ep,
                        target_dir
                        / f"{sanitize(item.title)} - S{item.season:02d}E{ep:02d}.mkv",
                    )
                    for src, ep, _, _, _ in proposed
                ]
                for src, ep, final in planned:
                    try:
                        os.lstat(final)
                    except FileNotFoundError:
                        pass
                    else:
                        print(
                            f"{C.YLW}  Collision: {final} exists — staging "
                            f"PRESERVED at {staging}.{C.R}"
                        )
                        return (
                            False,
                            f"would overwrite {final} — RIP PRESERVED "
                            f"at {staging} (not deleted; resolve manually)",
                        )
                try:
                    claims = _claim_tv_destinations([final for _, _, final in planned])
                except OSError as e:
                    print(
                        f"{C.YLW}  Slot race lost — staging PRESERVED at "
                        f"{staging}.{C.R}"
                    )
                    reason = (
                        "slot already claimed by parallel rip"
                        if e.errno == errno.EEXIST
                        else "could not reserve every TV slot"
                    )
                    return (
                        False,
                        f"{reason}: {e} — RIP PRESERVED at {staging} (resolve manually)",
                    )
                emit_error = _emit_claimed_tv_moves(
                    planned, claims, _emit_move, finals
                )
                if emit_error is not None:
                    return (
                        False,
                        f"protected TV publication failed: {emit_error}; RIP "
                        f"PRESERVED at {staging}",
                    )
                record_episode_count(item, disc_n, len(proposed), state)
            else:
                try:
                    manual_dir = tv_review_capture_dir(
                        item,
                        target_dir,
                        getattr(args, "current_disc_fingerprint", ""),
                        getattr(args, "review_root", None),
                    )
                    manual_dir = protected_quarantine_child(
                        manual_dir, "_manual-order"
                    )
                    manual_dir.mkdir(parents=True, exist_ok=True)
                except (OSError, ValueError) as exc:
                    return (
                        False,
                        f"cannot isolate manual-order TV output: {exc}; RIP "
                        f"PRESERVED at {staging}",
                    )
                for src, _, tnum, _, _ in proposed:
                    tag = f"t{tnum:02d}" if tnum is not None else src.stem
                    final = manual_dir / (
                        f"{sanitize(item.title)} - S{item.season:02d} - {tag}.mkv"
                    )
                    if final.exists():
                        print(
                            f"{C.YLW}  Collision: {final} exists — staging "
                            f"PRESERVED at {staging}.{C.R}"
                        )
                        return (
                            False,
                            f"would overwrite {final} — RIP PRESERVED "
                            f"at {staging} (not deleted; resolve manually)",
                        )
                    _emit_move(
                        src, final, f"moving {tag}", publish_lock_held=True
                    )
                    finals.append(str(final))
                print(
                    f"{C.YLW}Files isolated outside the library in traceable "
                    f"form (no SxxExx): {manual_dir}.{C.R}"
                )
                print(
                    f"{C.YLW}  Review and promote manually when order is known.{C.R}"
                )
                try:
                    count = auto_answer(
                        args,
                        "How many actual episodes were on this disc? [Enter to skip]: ",
                        "",
                    )
                except (KeyboardInterrupt, EOFError):
                    count = ""
                if count.isdigit() and int(count) > 0:
                    record_episode_count(item, disc_n, int(count), state)
                    next_start = starting_ep(
                        item, disc_n + 1, state, target_dir=target_dir
                    )
                    print(
                        f"  Recorded {count} episodes; next disc → "
                        f"S{item.season:02d}E{next_start:02d}."
                    )
                else:
                    next_start = starting_ep(
                        item, disc_n + 1, state, target_dir=target_dir
                    )
                    print(
                        f"{C.YLW}  Counter not advanced; next disc → "
                        f"S{item.season:02d}E{next_start:02d}.{C.R}"
                    )

    payload: dict = {"elapsed_s": elapsed, "files": finals}
    if used_fallback:
        payload["used_fallback"] = True
    if item.type == "tv-season" and route_to_review:
        payload["routed_to_review"] = True
    if getattr(args, "sync_move", False):
        shutil.rmtree(staging, ignore_errors=True)
        return True, payload
    payload["move_jobs"] = move_jobs
    payload["staging"] = str(staging)
    _attach_preverify(payload)
    return True, payload


# Alias matching public library API naming
rip_disc = rip


def rip_double_feature(
    args, item: QueueItem, info: dict, state: dict, disc_n: int
):
    """Rip each pinned movie on a double-feature disc synchronously so the drive is
    not released prematurely between features."""
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
        sub = QueueItem(
            title=name,
            type="movie",
            discs=1,
            target_root=str(base / sanitize(name)),
            format=item.format,
        )
        sub_target = compute_target_dir(sub)
        dur = info["titles"].get(tid, {}).get(9, "?:??:??")
        print(
            f"\n{C.B}Double feature {n}/{len(item.features)} — "
            f"ripping t{tid:02d} ({dur}) → {name}{C.R}"
        )
        sub_args = argparse.Namespace(**vars(args))
        sub_args.sync_move = True
        ok, result = rip(sub_args, sub, info, [tid], sub_target, state, disc_n)
        if not ok:
            print(f"{C.RED}  feature '{name}' failed: {result}{C.R}")
            append_log(args, f"FAIL  {item.title} :: {name} t{tid:02d}  {result}")
            errors.append(f"{name}: {result}")
            continue
        all_files.extend(result["files"])
        total_elapsed += result["elapsed_s"]
        append_log(
            args,
            f"OK    {item.title} :: {name} t{tid:02d}  "
            f"{fmt_dur(result['elapsed_s'])}  {len(result['files'])} file(s)",
        )
    if not all_files:
        return False, "all features failed: " + " | ".join(errors)
    return True, {"elapsed_s": total_elapsed, "files": all_files}


# --- Queue interactive menu, header, and disc polling ---


def show_remaining(state: dict) -> None:
    print(f"\n{C.B}Remaining queue:{C.R}")
    for i in range(state["current_index"], len(state["queue"])):
        it = state["queue"][i]
        mark = "->" if i == state["current_index"] else "  "
        extra = f" S{it['season']:02d}" if it["type"] == "tv-season" else ""
        print(
            f"  {mark} {i + 1:>3}. [{it['type']:<10}{extra}] {it['title']} "
            f"({it['discs']} disc{'s' if it['discs'] != 1 else ''}, {it['format']})"
        )
    print()


def build_urgent_interactive() -> QueueItem:
    title = input("  title: ").strip()
    typ = input("  type [movie/tv-season] (movie): ").strip() or "movie"
    discs = int(input("  disc count (1): ").strip() or "1")
    target_root = input("  target root path: ").strip()
    fmt = input("  format [4K/BD/DVD] (BD): ").strip() or "BD"
    season, ep_start = 0, 1
    expected_episodes, expected_disc_episodes = 0, 0
    expected_physical_disc = 0
    if typ == "tv-season":
        season = int(input("  season number: ").strip())
        ep_start = int(input("  episode start (1): ").strip() or "1")
        expected_episodes = int(input("  expected season episode count: ").strip())
        expected_disc_episodes = int(
            input("  expected episodes on this disc: ").strip()
        )
        expected_physical_disc = int(input("  physical disc number: ").strip())
    return QueueItem(
        title=title,
        type=typ,
        discs=discs,
        target_root=target_root,
        format=fmt,
        season=season,
        episode_start=ep_start,
        expected_episodes=expected_episodes,
        expected_disc_episodes=expected_disc_episodes,
        notes="urgent",
        expected_physical_disc=expected_physical_disc,
    )


def menu(state: dict, args) -> str:
    print(
        f"\n{C.B}Menu:{C.R} [c]ontinue [s]kip [u]rgent [p]ark-current "
        f"[r]emaining [q]uit"
    )
    while True:
        a = input("> ").strip().lower()
        if a in ("c", "s", "u", "p", "r", "q"):
            break
    if a == "r":
        show_remaining(state)
        return menu(state, args)
    if a == "q":
        save_state(state, args.state)
        print("State saved. Bye.")
        sys.exit(0)
    if a == "s":
        cur = state["queue"][state["current_index"]]
        print(f"{C.YLW}Skipping: {cur['title']}{C.R}")
        state["current_index"] += 1
        state["disc_index_in_item"] = 0
    if a == "p":
        cur = state["queue"].pop(state["current_index"])
        state["queue"].append(cur)
        state["disc_index_in_item"] = 0
        print(f"{C.YLW}Parked '{cur['title']}' to end{C.R}")
    if a == "u":
        try:
            urgent = build_urgent_interactive()
        except (KeyboardInterrupt, EOFError):
            print("Cancelled.")
            return "c"
        state["queue"].insert(state["current_index"], asdict(urgent))
        state["disc_index_in_item"] = 0
        print(f"{C.YLW}Inserted '{urgent.title}' at front{C.R}")
    save_state(state, args.state)
    return "c"


def remaining_discs(state: dict) -> int:
    n = 0
    for i, it in enumerate(state["queue"]):
        if i < state["current_index"]:
            continue
        if i == state["current_index"]:
            n += max(0, it["discs"] - state["disc_index_in_item"])
        else:
            n += it["discs"]
    return n


def avg(xs) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def header(state: dict, item: QueueItem, disc_n: int) -> None:
    pos = state["current_index"] + 1
    total = len(state["queue"])
    a = avg(state["rip_durations_s"])
    rd = remaining_discs(state)
    eta = a * rd if a else 0
    proj = (
        (datetime.now() + timedelta(seconds=eta)).strftime("%a %b %d %H:%M")
        if eta
        else "--"
    )
    print(f"\n{C.B}{'=' * 72}{C.R}")
    print(f"{C.B}Queue {pos}/{total}{C.R}  {C.CYA}{item.title}{C.R}  ({item.format})")
    if item.type == "tv-season":
        print(f"  TV  S{item.season:02d}  Disc {disc_n}/{item.discs}")
    else:
        print(f"  Movie  Disc {disc_n}/{item.discs}")
    print(f"  Target: {item.target_root}")
    if item.notes:
        print(f"  Notes:  {item.notes}")
    print(
        f"  Done: {len(state['completed'])} rips   Avg/disc: {fmt_dur(a)}   "
        f"Remaining discs: {rd}"
    )
    print(f"  Projected complete: {proj}  (~{fmt_dur(eta)})")
    print(f"{C.B}{'=' * 72}{C.R}")


def wait_for_disc(args, state: dict):
    print(f"\n{C.CYA}Insert disc, or press Enter for menu...{C.R}", flush=True)
    last_poll_print = 0.0
    started = time.time()
    last_dropoff_hint = 0.0
    poll_stdin = not getattr(args, "non_interactive", False)
    fail_since = None
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
            print(
                f"  {C.D}polling drive ({datetime.now().strftime('%H:%M:%S')})...{C.R}",
                flush=True,
            )
            last_poll_print = now
        if disc_present(args):
            if fail_since is None:
                fail_since = now
            if now - fail_since >= RESPIN_AFTER_S and respins < MAX_RESPINS:
                respin_drive(args)
                respins += 1
                fail_since = None
                continue
            if respins >= MAX_RESPINS and now - fail_since >= RESPIN_AFTER_S:
                print(
                    f"\n{C.RED}Disc failed to probe after "
                    f"{MAX_RESPINS} respins; giving up on this disc.{C.R}",
                    flush=True,
                )
                append_log(
                    args,
                    f"FAIL  probe wedged after {MAX_RESPINS} respins; "
                    f"surfacing as ('error','probe_max_respins')",
                )
                return (
                    "error",
                    {
                        "reason": "probe_max_respins",
                        "respins": respins,
                        "elapsed_s": int(now - started),
                    },
                )
            time.sleep(PROBE_COOLDOWN_S)
        else:
            fail_since = None
        if now - started > 120 and now - last_dropoff_hint > 60:
            if _probe._LAST_PROBE_ACTIVITY < now - 30:
                print(
                    f"  {C.YLW}No disc activity for >2min. If the drive dropped off bus:{C.R}"
                )
                print(
                    f"  {C.YLW}    system_profiler SPUSBDataType | grep -B2 -A4 'BU40N\\|UHD'{C.R}"
                )
                print(
                    f"  {C.YLW}    Reseat USB cable, try a powered hub, or run under{C.R}"
                )
                print(
                    f"  {C.YLW}    `caffeinate -dimsu burndvd ...` to defeat power management.{C.R}"
                )
                last_dropoff_hint = now


def _cli_state_path(argv: list[str] | None = None) -> str | None:
    args_list = sys.argv if argv is None else argv
    return next(
        (
            args_list[i + 1]
            for i, a in enumerate(args_list)
            if a == "--state" and i + 1 < len(args_list)
        ),
        None,
    )


def _write_exit_sentinel(
    state_path: str | None, exit_code: int, reason: str
) -> None:
    if not state_path:
        return
    try:
        with open(state_path + ".exit", "w", encoding="utf-8") as fh:
            json.dump(
                {"exit_code": exit_code, "reason": reason, "ts": int(time.time())},
                fh,
            )
    except OSError:
        pass
