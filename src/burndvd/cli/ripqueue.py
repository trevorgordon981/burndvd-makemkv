"""Thin CLI entrypoint for `burndvd-queue` / `ripqueue.py`."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys
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
    fmt_dur,
)
from burndvd.drive import (
    _drive_identity,
    _drive_idx_from_args,
    append_log,
    beep,
    check_key_expiry,
    eject,
)
from burndvd.models import (
    QueueItem,
    load_or_init,
    load_queue,
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
)
from burndvd.probe import (
    disc_content_fingerprint,
    disc_label,
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
    log_title_audit,
    loose_match,
    select_titles,
)


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
        sys.exit(scan_titles_json(args))

    if not args.queue:
        print(
            f"{C.RED}--queue is required (unless using --scan).{C.R}",
            file=sys.stderr,
        )
        sys.exit(1)

    warn_state_location(args.state)
    check_key_expiry(args)

    items = load_queue(Path(args.queue))
    policy_error = run_policy_error(
        items, overwrite=args.overwrite, rerip_review=args.rerip_review
    )
    if policy_error:
        print(f"{C.RED}{policy_error}{C.R}", file=sys.stderr)
        sys.exit(64)
    state = load_or_init(args, items)
    save_state(state, args.state)
    clear_finalize_errors()
    _FINALIZE_THREADS.clear()

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
            save_state(state, args.state)
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
            save_state(state, args.state)
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
                    probe_retry_counts[probe_retry_key] = probe_retries_used + 1
                    save_state(state, args.state)
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
                save_state(state, args.state)
                beep(args, ok=False)
                sys.exit(2)
            if args.non_interactive and args.on_fail == "abort":
                print(
                    f"{C.RED}--on-fail=abort; exiting non-zero and leaving "
                    f"the disc in place.{C.R}"
                )
                save_state(state, args.state)
                beep(args, ok=False)
                sys.exit(2)

            ans = auto_answer(args, "[p]ark item / [s]kip item / [r]etry: ", "s")
            if ans.startswith("p"):
                cur = state["queue"].pop(state["current_index"])
                state["queue"].append(cur)
                state["disc_index_in_item"] = 0
                save_state(state, args.state)
                append_log(
                    args,
                    f"BAD_DISC  {item.title} disc{disc_n}  parked to end (codes={info.get('codes')})",
                )
            elif ans.startswith("s"):
                state["current_index"] += 1
                state["disc_index_in_item"] = 0
                save_state(state, args.state)
                append_log(
                    args,
                    f"BAD_DISC  {item.title} disc{disc_n}  skipped (codes={info.get('codes')})",
                )
            beep(args, ok=False)
            eject(args)
            continue

        probe_retry_key = f"{item.title}||disc{disc_n}"
        probe_retry_counts = state.get("physical_probe_failure_retry_counts", {})
        if probe_retry_counts.pop(probe_retry_key, None) is not None:
            save_state(state, args.state)

        label = disc_label(info)
        print(f"  Disc label: {C.MAG}{label}{C.R}")

        fingerprint = disc_content_fingerprint(info)
        disc_args = argparse.Namespace(**vars(args))
        disc_args.current_disc_fingerprint = fingerprint
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

        if not loose_match(label, item.title):
            print(f"{C.YLW}Disc label doesn't match expected '{item.title}'.{C.R}")
            ans = auto_answer(args, "Proceed anyway? [y/N/skip] ", "y")
            if ans == "skip":
                state["current_index"] += 1
                state["disc_index_in_item"] = 0
                save_state(state, args.state)
                continue
            if ans != "y":
                print("Eject and try again.")
                continue

        title_ids = select_titles(item, info)
        log_title_audit(args, item, info, title_ids)
        if not title_ids:
            print(
                f"{C.RED}No suitable titles on disc (TV floor: {TV_MIN_DUR_S}s).{C.R}"
            )
            beep(args, ok=False)
            ans = auto_answer(args, "[r]etry / [s]kip? ", "s")
            if ans.startswith("s"):
                state["current_index"] += 1
                state["disc_index_in_item"] = 0
                save_state(state, args.state)
            continue

        print(f"  Selected {len(title_ids)} title(s): {title_ids}")

        if item.type == "double-feature":
            ok, result = rip_double_feature(disc_args, item, info, state, disc_n)
        else:
            ok, result = rip(
                disc_args, item, info, title_ids, target, state, disc_n
            )
        if not ok:
            print(f"{C.RED}Rip failed: {result}{C.R}")
            append_log(args, f"FAIL  {item.title} disc{disc_n}  {result}")
            beep(args, ok=False)

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
                    save_state(state, args.state)
                    sys.exit(2)
                if ans == "retry":
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
                    save_state(state, args.state)
                    time.sleep(NONINTERACTIVE_RETRY_DELAY_S)
                else:
                    print(f"[r]etry / [s]kip? s  {C.D}(--on-fail={args.on_fail}){C.R}")

            if ans.startswith("s"):
                state["current_index"] += 1
                state["disc_index_in_item"] = 0
                recent.pop(key, None)
                retry_counts.pop(key, None)
            save_state(state, args.state)
            continue

        result["disc_fingerprint"] = fingerprint

        state["completed"].append(
            {
                "item": item.title,
                "disc_n": disc_n,
                "type": item.type,
                "files": result["files"],
                "elapsed_s": result["elapsed_s"],
                "ts": time.time(),
            }
        )
        state.get("recent_stall_bytes", {}).pop(f"{item.title}||disc{disc_n}", None)
        state.get("failure_retry_counts", {}).pop(f"{item.title}||disc{disc_n}", None)
        state["rip_durations_s"].append(result["elapsed_s"])
        state["rip_durations_s"] = state["rip_durations_s"][-30:]
        if queue_disc_n >= item.discs:
            state["current_index"] += 1
            state["disc_index_in_item"] = 0
        else:
            state["disc_index_in_item"] += 1
        save_state(state, args.state)

        append_log(
            args,
            f"OK    {item.title} disc{disc_n}  {fmt_dur(result['elapsed_s'])}  "
            f"{len(result['files'])} file(s)",
        )
        print(f"{C.GRN}Done in {fmt_dur(result['elapsed_s'])}:{C.R}")
        for fp in result["files"]:
            print(f"  {fp}")
        beep(args, ok=True)
        eject(args)
        if result.get("move_jobs") is not None:
            start_background_finalize(disc_args, result, item, disc_n)
        else:
            log_final_durations(disc_args, item, disc_n, result["files"])
            verified = True
            if disc_args.verify:
                print(f"{C.D}Verifying with ffmpeg...{C.R}")
                ok2, err = verify(result["files"])
                if not ok2:
                    verified = False
                    _record_finalize_error(
                        f"VERIFY_FAIL {item.title} disc{disc_n}: {err}"
                    )
                    append_log(
                        args, f"VERIFY_FAIL {item.title} disc{disc_n} :: {err}"
                    )
                    print(f"{C.RED}Verify failed: {err}{C.R}")
                    beep(args, ok=False)
            if (
                disc_args.verify
                and verified
                and result.get("disc_fingerprint")
                and result["files"]
                and not disc_args.rerip_review
            ):
                append_disc_receipt(
                    Path(disc_args.disc_receipts),
                    item,
                    disc_n,
                    result["disc_fingerprint"],
                    result["files"],
                )
            if verified:
                run_subocr_postrip(disc_args, result["files"])

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
        sys.exit(2)
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
