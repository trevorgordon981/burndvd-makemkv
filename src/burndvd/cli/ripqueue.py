"""Thin CLI entrypoint for `burndvd-queue` / `ripqueue.py`."""

from __future__ import annotations

import argparse
import contextlib
import copy
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

from burndvd.config import (
    C,
    DEFAULT_MAKEMKVCON,
    DISC_RECEIPT_DEFAULT,
    MAX_NONINTERACTIVE_RETRIES,
    NONINTERACTIVE_RETRY_DELAY_S,
    PHYSICAL_PROBE_RETRY_DELAY_S,
    TV_MIN_DUR_S,
    config_bool,
    fmt_dur,
    load_user_config,
)
from burndvd.drive import (
    DriveSlotConflictError,
    _drive_identity,
    _drive_idx_from_args,
    append_log,
    beep,
    check_key_expiry,
    disc_slot_lock,
    drive_released_marker_path,
    eject,
    invalidate_resolved_device_cache,
    mark_drive_released,
    resolve_makemkv_device,
    slot_lock_scope,
)
from burndvd.metadata import (
    _compact,
    compute_next_tv_episode,
    find_episode_plan_conflict,
    volume_matches_show_intent,
)
from burndvd.models import (
    QueueItem,
    load_or_init,
    load_queue,
    record_committed_job,
    run_policy_error,
    save_state,
    validate_queue_item,
    warn_state_location,
)
from burndvd.nas import (
    apply_tv_output_mode,
    compute_target_dir,
    resolve_nas_path,
    season_dir_lock,
    starting_ep,
)
from burndvd.probe import (
    disc_content_fingerprint,
    disc_label,
    disc_volume_candidates,
    log_final_durations,
    scan_titles_json,
)
from burndvd.receipts import append_disc_receipt, disc_receipt_seen
from burndvd import ripper as _ripper
from burndvd.ripper import (
    _FINALIZE_THREADS,
    _cli_state_path,
    _record_finalize_error,
    _write_exit_sentinel,
    auto_answer,
    clear_finalize_errors,
    get_finalize_errors,
    header,
    join_finalizers,
    noninteractive_failure_action,
    rip,
    rip_double_feature,
    run_subocr_postrip,
    stall_byte,
    start_background_finalize,
    verify,
    wait_for_disc,
)
from burndvd.selection import (
    automatic_review_contract_error,
    bound_physical_disc,
    existing_season_median_dur_s,
    log_title_audit,
    loose_match,
    select_titles,
)


def _default_no_extras() -> bool:
    env_val = os.environ.get("BURNDVD_NO_EXTRAS")
    if env_val is not None and env_val.strip() != "":
        return env_val.strip().lower() in ("1", "true", "yes", "on")
    return config_bool(load_user_config(), "no_extras", default=False)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Guided MakeMKV ripping queue with automatic unencrypted-BD fallback."
    )
    ap.add_argument("--queue")
    ap.add_argument(
        "--validate-queue",
        metavar="PATH",
        help="parse and validate a queue, then exit without querying a drive or MakeMKV",
    )
    ap.add_argument(
        "--scan",
        action="store_true",
        help="probe the disc, print its title table as JSON, and exit",
    )
    ap.add_argument("--state", default="ripqueue-state.json")
    ap.add_argument("--makemkvcon", default=DEFAULT_MAKEMKVCON)
    ap.add_argument("--device", default="disc:0")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument(
        "--rerip-review",
        action="store_true",
        help="re-read a known TV disc into an isolated out-of-library quarantine",
    )
    ap.add_argument("--auto-rerip-review", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument(
        "--review-root",
        help="absolute out-of-library root for TV review captures",
    )
    ap.add_argument(
        "--disc-receipts",
        default=str(DISC_RECEIPT_DEFAULT),
        help="durable JSONL ledger used to reject a previously published physical disc",
    )
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--min-free-gb", type=int, default=200)
    ap.add_argument("--no-eject", action="store_true")
    ap.add_argument(
        "--sync-move",
        action="store_true",
        help="copy to the NAS synchronously before ejecting",
    )
    ap.add_argument("--no-sound", action="store_true")
    ap.add_argument(
        "--no-subocr",
        action="store_true",
        help="skip post-rip OCR of PGS subtitles to external .srt",
    )
    ap.add_argument(
        "--no-split-playall",
        action="store_true",
        help="keep a play-all-only disc as one file instead of cutting on chapters",
    )
    ap.add_argument(
        "--no-extras",
        dest="no_extras",
        action="store_true",
        default=_default_no_extras(),
        help="skip ripping bonus/extra titles shorter than TV_MIN_DUR_S on TV discs",
    )
    ap.add_argument(
        "--with-extras",
        dest="no_extras",
        action="store_false",
        default=argparse.SUPPRESS,
        help="include bonus/extra titles on TV discs (overrides --no-extras, env, and user config)",
    )
    ap.add_argument(
        "--allow-mkvmerge-fallback",
        dest="allow_mkvmerge_fallback",
        action="store_true",
        default=True,
        help="allow automatic mkvmerge remux fallback for unencrypted BDs on MakeMKV failure (default)",
    )
    ap.add_argument(
        "--no-mkvmerge-fallback",
        dest="allow_mkvmerge_fallback",
        action="store_false",
        help="disable automatic mkvmerge fallback for unencrypted BDs",
    )
    ap.add_argument(
        "--non-interactive",
        action="store_true",
        help="never prompt: rip/verify failures follow --on-fail",
    )
    ap.add_argument(
        "--on-fail",
        choices=["skip", "retry", "abort"],
        default="skip",
        help="non-interactive disposition for FAIL",
    )
    ap.add_argument(
        "--expected-volume",
        default=None,
        help="require probed physical disc volume label to match before staging",
    )
    ap.add_argument(
        "--allow-volume",
        dest="allow_volumes",
        action="append",
        default=[],
        help="repeatable volume label/glob allowlist for strict physical-disc binding",
    )
    ap.add_argument(
        "--allow-fingerprint",
        dest="allow_fingerprints",
        action="append",
        default=[],
        help="repeatable physical-disc SHA-256 fingerprint allowlist for pre-staging verification",
    )
    ap.add_argument(
        "--strict-disc-match",
        action="store_true",
        help="fail closed (exit 4) before staging if probed disc does not match configured show/volume intent",
    )
    return ap


def main() -> None:
    args = build_parser().parse_args()

    if args.validate_queue:
        items = load_queue(Path(args.validate_queue))
        print(f"Queue valid: {len(items)} item(s).")
        return

    def _skip_current_signal(signum, frame):
        _ripper._SKIP_REQUESTED = True

    signal.signal(signal.SIGUSR1, _skip_current_signal)

    def _sigterm_handler(signum, frame):
        raise KeyboardInterrupt("SIGTERM")

    signal.signal(signal.SIGTERM, _sigterm_handler)

    _initial_drive_idx = _drive_idx_from_args(args)
    _initial_drive_id = (
        _drive_identity(_initial_drive_idx)
        if _initial_drive_idx is not None
        else None
    )
    args._drive_identity = _initial_drive_id
    if _initial_drive_idx is not None and not _initial_drive_id:
        print(
            f"{C.RED}--device {args.device} doesn't map to any optical "
            f"drive at startup. Aborting.{C.R}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not sys.stdin.isatty() and not args.non_interactive and not args.scan:
        print(
            f"{C.RED}burndvd requires an interactive TTY (stdin not a terminal).{C.R}",
            file=sys.stderr,
        )
        print(
            f"{C.RED}  Don't run under nohup or with stdin piped/redirected,{C.R}",
            file=sys.stderr,
        )
        print(f"{C.RED}  or pass --non-interactive.{C.R}", file=sys.stderr)
        sys.exit(1)

    if not Path(args.makemkvcon).exists() and not shutil.which(args.makemkvcon):
        print(
            f"{C.RED}makemkvcon not found at {args.makemkvcon}{C.R}",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.scan:
        try:
            with slot_lock_scope():
                resolved_device = resolve_makemkv_device(args)
                args.device = resolved_device
                with disc_slot_lock(args.device, target=args):
                    sys.exit(scan_titles_json(args))
        except DriveSlotConflictError as exc:
            print(f"{C.RED}{exc}; refusing to touch busy optical drive.{C.R}", file=sys.stderr)
            append_log(args, f"DRIVE_SLOT_BUSY {args.device} :: {exc}")
            sys.exit(4)

    if not args.queue:
        print(
            f"{C.RED}--queue is required (unless using --scan).{C.R}",
            file=sys.stderr,
        )
        sys.exit(1)

    warn_state_location(args.state)
    try:
        with slot_lock_scope():
            resolved_device = resolve_makemkv_device(args)
            args.device = resolved_device
            with contextlib.ExitStack() as lock_stack:
                lock_stack.enter_context(disc_slot_lock(args.device, target=args))
                _run_queue_session(
                    args,
                    _initial_drive_idx,
                    _initial_drive_id,
                    release_drive_lock=lock_stack.close,
                )
    except DriveSlotConflictError as exc:
        print(f"{C.RED}{exc}; refusing to touch busy optical drive.{C.R}", file=sys.stderr)
        append_log(args, f"DRIVE_SLOT_BUSY {args.device} :: {exc}")
        sys.exit(4)


def _run_queue_session(
    args,
    _initial_drive_idx,
    _initial_drive_id,
    *,
    release_drive_lock=None,
) -> None:
    check_key_expiry(args)

    items = load_queue(Path(args.queue))
    policy_error = run_policy_error(
        items, overwrite=args.overwrite, rerip_review=args.rerip_review
    )
    if policy_error:
        print(f"{C.RED}{policy_error}{C.R}", file=sys.stderr)
        sys.exit(64)
    state = load_or_init(args, items)
    state.setdefault("pending_finalize", [])
    state.setdefault("failed_finalize", [])
    state_lock = threading.Lock()
    committed_cursor = {
        "current_index": state["current_index"],
        "disc_index_in_item": state["disc_index_in_item"],
        "disc_episode_counts": copy.deepcopy(state.get("disc_episode_counts", {})),
    }
    committed_slots: set[tuple[int, int]] = set()

    def _advance_committed_locked() -> None:
        while committed_cursor["current_index"] < len(state["queue"]):
            ci = committed_cursor["current_index"]
            cd = committed_cursor["disc_index_in_item"]
            if (ci, cd) not in committed_slots:
                break
            q_item_discs = int(state["queue"][ci].get("discs", 1) or 1)
            if cd + 1 >= q_item_discs:
                committed_cursor["current_index"] = ci + 1
                committed_cursor["disc_index_in_item"] = 0
            else:
                committed_cursor["disc_index_in_item"] = cd + 1

    def _earliest_pending_locked() -> tuple[int, int] | None:
        earliest: tuple[int, int] | None = None
        for entry in state.get("pending_finalize", []):
            if not isinstance(entry, dict):
                continue
            idx = entry.get("item_index")
            if not isinstance(idx, int) or idx < 0:
                continue
            disc_idx = entry.get("disc_index_in_item", 0)
            if not isinstance(disc_idx, int) or disc_idx < 0:
                disc_idx = 0
            pos = (idx, disc_idx)
            if earliest is None or pos < earliest:
                earliest = pos
        return earliest

    def _save_snapshot_locked() -> None:
        _advance_committed_locked()
        cur_i = committed_cursor["current_index"]
        cur_d = committed_cursor["disc_index_in_item"]
        earliest = _earliest_pending_locked()
        if earliest is not None and (cur_i, cur_d) > earliest:
            cur_i, cur_d = earliest
        snapshot = copy.deepcopy(state)
        snapshot["current_index"] = cur_i
        snapshot["disc_index_in_item"] = cur_d
        snapshot["disc_episode_counts"] = copy.deepcopy(
            committed_cursor["disc_episode_counts"]
        )
        save_state(snapshot, args.state)

    def _persist_committed_state() -> None:
        with state_lock:
            _save_snapshot_locked()

    def _sync_committed_on_skip() -> None:
        with state_lock:
            if not state.get("pending_finalize") and not state.get("failed_finalize"):
                committed_cursor["current_index"] = state["current_index"]
                committed_cursor["disc_index_in_item"] = state["disc_index_in_item"]
            _save_snapshot_locked()

    _persist_committed_state()
    _stale_released = drive_released_marker_path(args.state)
    if _stale_released is not None:
        try:
            _stale_released.unlink(missing_ok=True)
        except OSError:
            pass
    clear_finalize_errors()
    _FINALIZE_THREADS.clear()
    pending_final_summary: list[tuple[str, int, float, list[str]]] = []
    needs_final_eject = False
    early_ejected = threading.Event()
    drive_released = False

    def _release_optical_drive(*, should_eject: bool) -> None:
        nonlocal drive_released, needs_final_eject
        if drive_released:
            return
        drive_released = True
        did_eject = False
        if should_eject and not args.no_eject and not early_ejected.is_set():
            early_ejected.set()
            needs_final_eject = False
            did_eject = bool(eject(args))
        else:
            needs_final_eject = False
        mark_drive_released(
            args.state,
            device=str(args.device),
            ejected=did_eject,
        )
        if release_drive_lock is not None:
            release_drive_lock()
        action = "ejected & unlocked" if did_eject else "unlocked"
        print(
            f"{C.D}  Optical drive {action} — ready for the next disc while "
            f"verify/NAS finishes in background.{C.R}",
            flush=True,
        )

    while state["current_index"] < len(state["queue"]):
        if _initial_drive_idx is not None:
            _current_drive_id = _drive_identity(_initial_drive_idx)
            if _current_drive_id != _initial_drive_id:
                print(
                    f"\n{C.RED}Drive at {args.device} changed mid-queue:{C.R}",
                    file=sys.stderr,
                )
                print(f"  was: {_initial_drive_id}", file=sys.stderr)
                print(
                    f"  now: {_current_drive_id or '(no drive)'}", file=sys.stderr
                )
                print(
                    f"  {C.RED}Aborting — refusing to rip to a drive that "
                    f"may not own the current disc.{C.R}",
                    file=sys.stderr,
                )
                append_log(
                    args,
                    f"ABORT drive identity changed: "
                    f"{_initial_drive_id} -> {_current_drive_id}",
                )
                sys.exit(3)

        if _ripper._SKIP_REQUESTED:
            _ripper._SKIP_REQUESTED = False
            try:
                cur_item = QueueItem(**state["queue"][state["current_index"]])
                disc_n = state["disc_index_in_item"] + 1
                append_log(args, f"SKIP_SIGUSR1  {cur_item.title} disc{disc_n}")
                print(
                    f"{C.YLW}stoprip --skip-current: advancing past "
                    f"{cur_item.title} disc {disc_n}{C.R}",
                    flush=True,
                )
            except Exception:
                pass
            state["current_index"] += 1
            state["disc_index_in_item"] = 0
            _sync_committed_on_skip()
            continue

        item = QueueItem(**state["queue"][state["current_index"]])
        validate_queue_item(item)
        queue_disc_n = state["disc_index_in_item"] + 1
        disc_n = queue_disc_n
        header(state, item, disc_n)

        kind, payload = wait_for_disc(args, state)
        if kind == "cmd":
            continue
        if kind == "error":
            print(
                f"{C.RED}Probe gave up: {payload.get('reason', '?')}; "
                f"advancing per --on-fail={args.on_fail}.{C.R}"
            )
            append_log(
                args,
                f"FAIL  {item.title} disc{disc_n} probe gave up "
                f"({payload.get('reason')}); --on-fail={args.on_fail}",
            )
            if args.on_fail == "abort":
                sys.exit(2)
            state["current_index"] += 1
            state["disc_index_in_item"] = 0
            _sync_committed_on_skip()
            eject(args)
            continue
        info = payload

        if isinstance(info, dict) and "titles" in info:
            try:
                disc_n = bound_physical_disc(item, info, disc_n)
            except ValueError as exc:
                print(f"{C.RED}{exc}; refusing optical read.{C.R}", file=sys.stderr)
                append_log(args, f"DISC_CONTRACT_FAIL {item.title} :: {exc}")
                sys.exit(4)

        if isinstance(info, dict) and info.get("error") == "AACS_LOCKED":
            print(
                f"{C.RED}Disc appears AACS-locked (newer protection than current keys).{C.R}"
            )
            for m in info.get("msgs", [])[-3:]:
                print(f"  {C.D}{m}{C.R}")
            print("  Recent UHD release? Wait for next MakeMKV update or refresh keys.")
            ans = auto_answer(args, "[p]ark item / [s]kip item / [r]etry: ", "s")
            if ans.startswith("p"):
                with state_lock:
                    cur = state["queue"].pop(state["current_index"])
                    state["queue"].append(cur)
                    state["disc_index_in_item"] = 0
                _sync_committed_on_skip()
                append_log(args, f"AACS  {item.title} disc{disc_n}  parked to end")
            elif ans.startswith("s"):
                with state_lock:
                    state["current_index"] += 1
                    state["disc_index_in_item"] = 0
                _sync_committed_on_skip()
                append_log(args, f"AACS  {item.title} disc{disc_n}  skipped")
            beep(args, ok=False)
            eject(args)
            continue

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
            print(
                f"  {C.YLW}Try: wipe disc with a soft cloth (concentric scratches), reinsert.{C.R}"
            )
            print(
                f"  {C.YLW}If repeated: skip this disc, continue with the next.{C.R}"
            )

            probe_retry_key = f"{item.title}||disc{disc_n}"
            probe_retry_counts = state.setdefault(
                "physical_probe_failure_retry_counts", {}
            )
            probe_retries_used = int(probe_retry_counts.get(probe_retry_key, 0))
            if args.non_interactive and args.on_fail == "retry":
                if probe_retries_used < MAX_NONINTERACTIVE_RETRIES:
                    with state_lock:
                        probe_retry_counts[probe_retry_key] = probe_retries_used + 1
                    _persist_committed_state()
                    print(
                        f"  {C.YLW}Transient probe retry "
                        f"{probe_retries_used + 1}/{MAX_NONINTERACTIVE_RETRIES}; "
                        f"settling drive {PHYSICAL_PROBE_RETRY_DELAY_S}s. "
                        f"Disc stays in place.{C.R}",
                        flush=True,
                    )
                    append_log(
                        args,
                        f"PROBE_RETRY  {item.title} disc{disc_n}  "
                        f"automatic {probe_retries_used + 1}/"
                        f"{MAX_NONINTERACTIVE_RETRIES}",
                    )
                    time.sleep(PHYSICAL_PROBE_RETRY_DELAY_S)
                    continue
                print(
                    f"{C.RED}Automatic probe retry exhausted; exiting "
                    f"non-zero and leaving the disc in place.{C.R}"
                )
                append_log(
                    args,
                    f"PROBE_RETRY_EXHAUSTED  {item.title} disc{disc_n}",
                )
                _persist_committed_state()
                beep(args, ok=False)
                sys.exit(2)
            if args.non_interactive and args.on_fail == "abort":
                print(
                    f"{C.RED}--on-fail=abort; exiting non-zero and leaving "
                    f"the disc in place.{C.R}"
                )
                _persist_committed_state()
                beep(args, ok=False)
                sys.exit(2)

            ans = auto_answer(args, "[p]ark item / [s]kip item / [r]etry: ", "s")
            if ans.startswith("p"):
                with state_lock:
                    cur = state["queue"].pop(state["current_index"])
                    state["queue"].append(cur)
                    state["disc_index_in_item"] = 0
                _sync_committed_on_skip()
                append_log(
                    args,
                    f"BAD_DISC  {item.title} disc{disc_n}  parked to end (codes={info.get('codes')})",
                )
            elif ans.startswith("s"):
                with state_lock:
                    state["current_index"] += 1
                    state["disc_index_in_item"] = 0
                _sync_committed_on_skip()
                append_log(
                    args,
                    f"BAD_DISC  {item.title} disc{disc_n}  skipped (codes={info.get('codes')})",
                )
            beep(args, ok=False)
            eject(args)
            continue

        probe_retry_key = f"{item.title}||disc{disc_n}"
        with state_lock:
            probe_retry_counts = state.get("physical_probe_failure_retry_counts", {})
            cleared_probe_retry = (
                probe_retry_counts.pop(probe_retry_key, None) is not None
            )
        if cleared_probe_retry:
            _persist_committed_state()

        label = disc_label(info)
        print(f"  Disc label: {C.MAG}{label}{C.R}")

        fingerprint = disc_content_fingerprint(info)
        disc_args = argparse.Namespace(**vars(args))
        disc_args.current_disc_fingerprint = fingerprint

        # Pre-staging physical disc intent revalidation (strict watch / allowlist / auto-built guard).
        is_watch_row = (item.notes or "").startswith("watch:")
        expected_vol = (getattr(args, "expected_volume", None) or "").strip()
        if not expected_vol and is_watch_row:
            note_parts = (item.notes or "").split(":", 2)
            if len(note_parts) == 3:
                expected_vol = note_parts[2].strip()
        if not expected_vol and (item.notes or "").startswith("auto-built from "):
            expected_vol = (item.notes or "")[len("auto-built from ") :].strip()
        allow_volumes = tuple(
            str(v).strip()
            for v in (getattr(args, "allow_volumes", None) or ())
            if str(v).strip()
        )
        allow_fingerprints = {
            str(fp).strip().lower()
            for fp in (getattr(args, "allow_fingerprints", None) or ())
            if str(fp).strip()
        }
        strict_show_intent = bool(
            getattr(args, "strict_disc_match", False)
            or is_watch_row
            or allow_volumes
        )
        strict_disc_match = bool(
            strict_show_intent
            or expected_vol
            or allow_fingerprints
        )
        if strict_disc_match:
            if allow_fingerprints and (fingerprint or "").lower() not in allow_fingerprints:
                err_msg = (
                    f"probed physical disc fingerprint {(fingerprint or '')[:16]!r} "
                    f"is not in --allow-fingerprint allowlist"
                )
                print(
                    f"{C.RED}{err_msg}; refusing optical read before staging.{C.R}",
                    file=sys.stderr,
                )
                append_log(args, f"DISC_INTENT_MISMATCH {item.title} :: {err_msg}")
                sys.exit(4)
            if expected_vol:
                exp_compact = _compact(expected_vol)
                vol_candidates = disc_volume_candidates(info)
                vol_matched = any(
                    _compact(cand) == exp_compact for cand in vol_candidates
                )
                if not vol_matched:
                    err_msg = (
                        f"probed disc volume {label!r} does not match expected "
                        f"volume {expected_vol!r} (disc changed or misrouted)"
                    )
                    print(
                        f"{C.RED}{err_msg}; refusing optical read before staging.{C.R}",
                        file=sys.stderr,
                    )
                    append_log(args, f"DISC_INTENT_MISMATCH {item.title} :: {err_msg}")
                    sys.exit(4)
            if item.type == "tv-season" and strict_show_intent and not allow_fingerprints:
                ok_intent, intent_reason = volume_matches_show_intent(
                    label,
                    item.title,
                    item.season,
                    allowed_volumes=allow_volumes,
                )
                if not ok_intent:
                    err_msg = f"probed disc rejected for {item.title} S{item.season:02d}: {intent_reason}"
                    print(
                        f"{C.RED}{err_msg}; refusing optical read before staging.{C.R}",
                        file=sys.stderr,
                    )
                    append_log(args, f"DISC_INTENT_MISMATCH {item.title} :: {err_msg}")
                    sys.exit(4)

        target = resolve_nas_path(
            compute_target_dir(item), prefer_nfs=(item.type == "tv-season")
        )
        if (
            item.type == "tv-season"
            and getattr(disc_args, "auto_rerip_review", False)
            and not disc_args.rerip_review
        ):
            binding_error = automatic_review_contract_error(item, info)
            if binding_error:
                print(
                    f"{C.RED}{binding_error}; refusing automatic review.{C.R}",
                    file=sys.stderr,
                )
                append_log(
                    disc_args,
                    f"AUTO_REVIEW_CONTRACT_FAIL {item.title} :: {binding_error}",
                )
                sys.exit(4)
            with season_dir_lock(target, what="fingerprint review decision"):
                contract_error = apply_tv_output_mode(
                    disc_args, item, target, "fingerprint review decision"
                )
            if contract_error:
                print(f"{C.RED}{contract_error}.{C.R}", file=sys.stderr)
                append_log(
                    disc_args, f"TV_CONTRACT_FAIL {item.title} :: {contract_error}"
                )
                sys.exit(4)
        if (
            item.type == "tv-season"
            and not disc_args.rerip_review
            and disc_receipt_seen(Path(args.disc_receipts), fingerprint)
        ):
            print(
                f"{C.RED}Physical disc fingerprint {fingerprint[:16]} was already "
                f"published. Refusing re-rip; use --rerip-review to preserve a "
                f"traceable comparison copy.{C.R}",
                file=sys.stderr,
            )
            append_log(args, f"DUP_DISC {item.title} disc{disc_n} {fingerprint}")
            sys.exit(4)

        if not strict_disc_match and not loose_match(label, item.title):
            print(f"{C.YLW}Disc label doesn't match expected '{item.title}'.{C.R}")
            ans = auto_answer(args, "Proceed anyway? [y/N/skip] ", "y")
            if ans == "skip":
                with state_lock:
                    state["current_index"] += 1
                    state["disc_index_in_item"] = 0
                _sync_committed_on_skip()
                continue
            if ans != "y":
                print("Eject and try again.")
                continue

        season_median_s = (
            existing_season_median_dur_s(target)
            if item.type == "tv-season"
            else None
        )
        disc_args._season_median_s = season_median_s
        disc_args._season_median_computed = True
        if season_median_s is not None:
            print(
                f"  {C.D}Season duration anchor: median {fmt_dur(season_median_s)}{C.R}"
            )
        title_ids = select_titles(item, info, season_median_s=season_median_s)
        log_title_audit(
            args, item, info, title_ids, season_median_s=season_median_s
        )
        if not title_ids:
            print(
                f"{C.RED}No suitable titles on disc (TV floor: {TV_MIN_DUR_S}s).{C.R}"
            )
            beep(args, ok=False)
            ans = auto_answer(args, "[r]etry / [s]kip? ", "s")
            if ans.startswith("s"):
                with state_lock:
                    state["current_index"] += 1
                    state["disc_index_in_item"] = 0
                _sync_committed_on_skip()
            continue

        print(f"  Selected {len(title_ids)} title(s): {title_ids}")

        if (
            item.type == "tv-season"
            and item.season > 0
            and not disc_args.rerip_review
        ):
            planned_start = starting_ep(item, disc_n, state, target_dir=target)
            ep_info = compute_next_tv_episode(
                title=item.title,
                season=int(item.season),
                target_root=str(item.target_root or target.parent),
                season_dir=target,
                wait_scan_s=0.0,
                exclude_queue_file=getattr(args, "queue", None),
                exclude_state_file=getattr(args, "state", None),
            )
            overlap_err, _ = find_episode_plan_conflict(
                planned_start,
                episode_count=len(title_ids),
                reservations=ep_info.get("reservations") or (),
                pending_plans=ep_info.get("pending_plans") or (),
                check_existing=False,
            )
            if overlap_err:
                print(
                    f"{C.RED}{overlap_err}; refusing overlapping launch before staging.{C.R}",
                    file=sys.stderr,
                )
                append_log(args, f"EPISODE_OVERLAP {item.title} :: {overlap_err}")
                sys.exit(4)

        cur_item_idx = state["current_index"]
        cur_disc_idx = state["disc_index_in_item"]
        pre_rip_ep_counts = copy.deepcopy(state.get("disc_episode_counts", {}))
        will_have_more_items = (
            (queue_disc_n < item.discs)
            or (cur_item_idx + 1 < len(state["queue"]))
        )
        optical_done_for_disc = False
        ejected_for_disc = False

        def _on_optical_done() -> None:
            nonlocal optical_done_for_disc, ejected_for_disc
            if optical_done_for_disc:
                return
            optical_done_for_disc = True
            if not will_have_more_items:
                _release_optical_drive(should_eject=True)
                ejected_for_disc = True
            elif not get_finalize_errors() and not args.no_eject:
                eject(args)
                ejected_for_disc = True

        if item.type == "double-feature":
            ok, result = rip_double_feature(disc_args, item, info, state, disc_n)
        elif disc_args.sync_move:
            ok, result = rip(
                disc_args, item, info, title_ids, target, state, disc_n
            )
        else:
            ok, result = rip(
                disc_args,
                item,
                info,
                title_ids,
                target,
                state,
                disc_n,
                on_optical_done=_on_optical_done,
            )
        if not ok:
            invalidate_resolved_device_cache()
            print(f"{C.RED}Rip failed: {result}{C.R}")
            append_log(args, f"FAIL  {item.title} disc{disc_n}  {result}")
            beep(args, ok=False)

            if optical_done_for_disc:
                append_log(
                    args,
                    f"POST_OPTICAL_FAIL  {item.title} disc{disc_n}  "
                    f"refusing optical reread after drive release ({result})",
                )
                print(
                    f"{C.RED}Downstream post-optical step failed after optical "
                    f"extraction completed; refusing to re-read disc.{C.R}",
                    file=sys.stderr,
                )
                if args.non_interactive and args.on_fail == "skip" and will_have_more_items:
                    with state_lock:
                        state["current_index"] += 1
                        state["disc_index_in_item"] = 0
                    _sync_committed_on_skip()
                    continue
                _persist_committed_state()
                join_finalizers()
                sys.exit(2)

            byte = stall_byte(result)
            recent = state.setdefault("recent_stall_bytes", {})
            key = f"{item.title}||disc{disc_n}"
            retry_counts = state.setdefault("failure_retry_counts", {})
            retries_used = int(retry_counts.get(key, 0))
            prior = recent.get(key)
            same_byte_count = (
                prior["count"] + 1
                if byte is not None and prior and prior.get("byte") == byte
                else 1
            )
            if byte is not None:
                recent[key] = {"byte": byte, "count": same_byte_count}

            if args.non_interactive:
                ans = noninteractive_failure_action(
                    args.on_fail,
                    retries_used,
                    repeated_same_byte_stall=(same_byte_count >= 2),
                )
            else:
                ans = input("[r]etry / [s]kip? ").strip().lower()

            if same_byte_count >= 2:
                if not (args.non_interactive and args.on_fail == "retry"):
                    ans = "skip"
                disposition = "stopping" if ans == "abort" else "skipping"
                print(
                    f"{C.YLW}Same-byte stall at total={byte} hit "
                    f"{same_byte_count}x; {disposition} as bad-sector.{C.R}"
                )
                append_log(
                    args,
                    f"BAD_SECTOR  {item.title} disc{disc_n}  "
                    f"repeated stall at PRGV total={byte}",
                )

            if args.non_interactive:
                if ans == "abort":
                    if args.on_fail == "retry":
                        print(
                            f"{C.RED}Automatic retry exhausted; exiting "
                            f"non-zero and leaving the disc in place.{C.R}"
                        )
                        append_log(
                            args,
                            f"RETRY_EXHAUSTED {item.title} disc{disc_n} "
                            f"after {retries_used} retry",
                        )
                    else:
                        print(f"{C.RED}--on-fail=abort; exiting non-zero.{C.R}")
                    _persist_committed_state()
                    sys.exit(2)
                if ans == "retry":
                    with state_lock:
                        retry_counts[key] = retries_used + 1
                    print(
                        f"[r]etry / [s]kip? r  {C.D}(automatic retry "
                        f"{retries_used + 1}/{MAX_NONINTERACTIVE_RETRIES}; "
                        f"settling drive {NONINTERACTIVE_RETRY_DELAY_S}s){C.R}"
                    )
                    append_log(
                        args,
                        f"RETRY {item.title} disc{disc_n}  automatic "
                        f"{retries_used + 1}/{MAX_NONINTERACTIVE_RETRIES}",
                    )
                    _persist_committed_state()
                    time.sleep(NONINTERACTIVE_RETRY_DELAY_S)
                else:
                    print(f"[r]etry / [s]kip? s  {C.D}(--on-fail={args.on_fail}){C.R}")

            if ans.startswith("s"):
                with state_lock:
                    state["current_index"] += 1
                    state["disc_index_in_item"] = 0
                    recent.pop(key, None)
                    retry_counts.pop(key, None)
                _sync_committed_on_skip()
            else:
                _persist_committed_state()
            continue

        result["disc_fingerprint"] = fingerprint
        post_rip_ep_counts = copy.deepcopy(state.get("disc_episode_counts", {}))
        ep_delta_counts = {
            k: copy.deepcopy(v)
            for k, v in post_rip_ep_counts.items()
            if pre_rip_ep_counts.get(k) != v
        }

        pending_id = f"{cur_item_idx}:{cur_disc_idx}:{disc_n}:{time.time_ns()}"
        raw_move_jobs = result.get("move_jobs") or []
        serialized_move_jobs = [
            {
                "src": str(job[0]),
                "dst": str(job[1]),
                "label": str(job[2]) if len(job) > 2 else "moving",
            }
            for job in raw_move_jobs
            if isinstance(job, (tuple, list)) and len(job) >= 2
        ]
        pending_entry = {
            "id": pending_id,
            "item_index": cur_item_idx,
            "disc_index_in_item": cur_disc_idx,
            "queue_disc_n": queue_disc_n,
            "item": item.title,
            "disc_n": disc_n,
            "type": item.type,
            "files": list(result["files"]),
            "move_jobs": serialized_move_jobs,
            "committed_jobs": list(result.get("committed_jobs") or []),
            "staging": result.get("staging"),
            "elapsed_s": result["elapsed_s"],
            "status": "pending_finalize",
            "ts": time.time(),
        }
        with state_lock:
            state["pending_finalize"] = [
                e
                for e in state.get("pending_finalize", [])
                if not (
                    isinstance(e, dict)
                    and e.get("item_index") == cur_item_idx
                    and e.get("disc_index_in_item", 0) == cur_disc_idx
                )
            ]
            state["pending_finalize"].append(pending_entry)
        # Persist pending_finalize BEFORE transfer/verification; do NOT mark
        # completed or advance persisted cursor yet.
        _persist_committed_state()

        # Advance only the in-memory loop cursor so the drive can proceed to the
        # next disc while background finalization runs (persisted state waits for commit).
        if queue_disc_n >= item.discs:
            state["current_index"] += 1
            state["disc_index_in_item"] = 0
        else:
            state["disc_index_in_item"] += 1

        def _make_commit_callbacks(
            *,
            _pending_id=pending_id,
            _pending_entry=pending_entry,
            _slot=(cur_item_idx, cur_disc_idx),
            _item_title=item.title,
            _item_type=item.type,
            _disc_n=disc_n,
            _elapsed_s=result["elapsed_s"],
            _ep_delta=ep_delta_counts,
        ):
            def _on_complete(final_result: dict) -> None:
                with state_lock:
                    state["pending_finalize"] = [
                        e
                        for e in state.get("pending_finalize", [])
                        if e.get("id") != _pending_id
                        and not (
                            isinstance(e, dict)
                            and e.get("item_index") == _slot[0]
                            and e.get("disc_index_in_item", 0) == _slot[1]
                        )
                    ]
                    state["completed"].append(
                        {
                            "item": _item_title,
                            "disc_n": _disc_n,
                            "type": _item_type,
                            "files": list(final_result.get("files", [])),
                            "elapsed_s": _elapsed_s,
                            "ts": time.time(),
                        }
                    )
                    state.get("recent_stall_bytes", {}).pop(
                        f"{_item_title}||disc{_disc_n}", None
                    )
                    state.get("failure_retry_counts", {}).pop(
                        f"{_item_title}||disc{_disc_n}", None
                    )
                    state["rip_durations_s"].append(_elapsed_s)
                    state["rip_durations_s"] = state["rip_durations_s"][-30:]
                    for ek, ev in _ep_delta.items():
                        committed_cursor["disc_episode_counts"][ek] = copy.deepcopy(ev)
                    committed_slots.add(_slot)
                _persist_committed_state()

            def _on_file_committed(commit_rec: dict) -> None:
                with state_lock:
                    record_committed_job(_pending_entry, commit_rec)
                    for entry in state.get("pending_finalize", []):
                        if isinstance(entry, dict) and entry.get("id") == _pending_id:
                            record_committed_job(entry, commit_rec)
                _persist_committed_state()

            def _on_failure(err_msg: str) -> None:
                with state_lock:
                    state["pending_finalize"] = [
                        e for e in state.get("pending_finalize", [])
                        if e.get("id") != _pending_id
                    ]
                    failed_rec = {
                        **copy.deepcopy(_pending_entry),
                        "status": "failed",
                        "error": err_msg,
                        "failed_ts": time.time(),
                    }
                    state.setdefault("failed_finalize", []).append(failed_rec)
                _persist_committed_state()

            return _on_complete, _on_failure, _on_file_committed

        on_complete_cb, on_failure_cb, on_file_committed_cb = _make_commit_callbacks()

        has_more_items = state["current_index"] < len(state["queue"])
        if result.get("move_jobs") is not None:
            print(
                f"{C.D}Staged in {fmt_dur(result['elapsed_s'])}; "
                f"finalizing to NAS...{C.R}"
            )
            start_background_finalize(
                disc_args,
                result,
                item,
                disc_n,
                on_complete=on_complete_cb,
                on_failure=on_failure_cb,
                on_file_committed=on_file_committed_cb,
                on_verified=None,
            )
            pending_final_summary.append(
                (item.title, disc_n, result["elapsed_s"], list(result["files"]))
            )
            if has_more_items:
                invalidate_resolved_device_cache()
                if not get_finalize_errors() and not ejected_for_disc:
                    eject(args)
            else:
                _release_optical_drive(should_eject=True)
        else:
            try:
                log_final_durations(disc_args, item, disc_n, result["files"])
                verified = True
                if disc_args.verify:
                    print(f"{C.D}Verifying with ffmpeg...{C.R}")
                    ok2, err = verify(result["files"])
                    if not ok2:
                        verified = False
                        err_msg = f"VERIFY_FAIL {item.title} disc{disc_n}: {err}"
                        _record_finalize_error(err_msg)
                        append_log(
                            args, f"VERIFY_FAIL {item.title} disc{disc_n} :: {err}"
                        )
                        print(f"{C.RED}Verify failed: {err}{C.R}")
                        beep(args, ok=False)
                        on_failure_cb(err_msg)
                if verified:
                    if (
                        disc_args.verify
                        and result.get("disc_fingerprint")
                        and result["files"]
                        and not disc_args.rerip_review
                        and not result.get("routed_to_review", False)
                    ):
                        append_disc_receipt(
                            Path(disc_args.disc_receipts),
                            item,
                            disc_n,
                            result["disc_fingerprint"],
                            result["files"],
                        )
                    on_complete_cb(result)
                    if has_more_items:
                        invalidate_resolved_device_cache()
                        append_log(
                            args,
                            f"OK    {item.title} disc{disc_n}  "
                            f"{fmt_dur(result['elapsed_s'])}  "
                            f"{len(result['files'])} file(s)",
                        )
                        print(f"{C.GRN}Done in {fmt_dur(result['elapsed_s'])}:{C.R}")
                        for fp in result["files"]:
                            print(f"  {fp}")
                        if not get_finalize_errors():
                            beep(args, ok=True)
                            eject(args)
                    else:
                        pending_final_summary.append(
                            (
                                item.title,
                                disc_n,
                                result["elapsed_s"],
                                list(result["files"]),
                            )
                        )
                        needs_final_eject = True
                    try:
                        run_subocr_postrip(disc_args, result["files"])
                    except Exception as ocr_exc:
                        append_log(
                            args,
                            f"SUBOCR_WARN {item.title} disc{disc_n} :: "
                            f"{type(ocr_exc).__name__}: {ocr_exc}",
                        )
                        print(
                            f"{C.YLW}Warning: post-rip subtitle OCR failed to launch "
                            f"({type(ocr_exc).__name__}: {ocr_exc}); "
                            f"verified media publication remains committed.{C.R}"
                        )
            except Exception as exc:
                err_msg = (
                    f"FINALIZE_FAIL {item.title} disc{disc_n}: "
                    f"{type(exc).__name__}: {exc}"
                )
                _record_finalize_error(err_msg)
                append_log(
                    args,
                    f"FINALIZE_FAIL {item.title} disc{disc_n} :: "
                    f"{type(exc).__name__}: {exc}",
                )
                print(
                    f"{C.RED}Finalize failed ({type(exc).__name__}): {exc}{C.R}"
                )
                beep(args, ok=False)
                on_failure_cb(err_msg)
    if any(t.is_alive() for t in _FINALIZE_THREADS) and not drive_released:
        _release_optical_drive(should_eject=needs_final_eject)
    join_finalizers()
    finalize_errors = get_finalize_errors()
    if finalize_errors:
        print(
            f"\n{C.RED}Queue finished with {len(finalize_errors)} "
            f"finalize/verify error(s):{C.R}",
            file=sys.stderr,
        )
        for err in finalize_errors:
            print(f"  {C.RED}- {err}{C.R}", file=sys.stderr)
        beep(args, ok=False)
        sys.exit(2)
    for title_s, disc_s, elapsed_s, files_s in pending_final_summary:
        append_log(
            args,
            f"OK    {title_s} disc{disc_s}  {fmt_dur(elapsed_s)}  "
            f"{len(files_s)} file(s)",
        )
        print(f"{C.GRN}Done in {fmt_dur(elapsed_s)}:{C.R}")
        for fp in files_s:
            print(f"  {fp}")
    if pending_final_summary:
        beep(args, ok=True)
    if needs_final_eject and not early_ejected.is_set() and not drive_released:
        eject(args)
    print(f"\n{C.B}{C.GRN}Queue complete.{C.R}")


def cli_main() -> None:
    """Process entrypoint writing `<state>.exit` sentinel and handling 2nd Ctrl-C."""
    exit_code = 0
    exit_reason = "queue_complete"
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. State on disk is current.")
        try:
            join_finalizers()
        except KeyboardInterrupt:
            print(
                f"{C.YLW}Second Ctrl-C; abandoning in-flight transfers. "
                f"Any partial files in staging are recoverable manually.{C.R}",
                flush=True,
            )
            _write_exit_sentinel(
                _cli_state_path(), 130, "keyboard_interrupt_forced"
            )
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except OSError:
                pass
            os._exit(130)
        exit_code = 130
        exit_reason = "keyboard_interrupt"
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
        exit_reason = f"sys_exit_{exit_code}"
    except Exception as e:
        print(f"\n{C.RED}Unexpected error: {e}{C.R}", file=sys.stderr)
        exit_code = 1
        exit_reason = f"exception:{type(e).__name__}:{str(e)[:120]}"
    finally:
        _write_exit_sentinel(_cli_state_path(), exit_code, exit_reason)
    sys.exit(exit_code)


if __name__ == "__main__":
    cli_main()
