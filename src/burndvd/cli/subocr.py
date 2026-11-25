"""Thin CLI entrypoint for `subocr`."""

from __future__ import annotations

import argparse
import fcntl
import signal
import sys

from burndvd.config import C, ensure_tool_path
from burndvd.subocr.pipeline import acquire_lock, find_mkvs, process_mkv
from burndvd.subocr.tracks import (
    _to_iso639_2,
    load_config,
    probe_tracks,
    show_for_path,
    show_settings,
)

ensure_tool_path()


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    ap = argparse.ArgumentParser(
        description="OCR HDMV PGS subtitles from .mkv files into external .srt via Apple Vision.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("paths", nargs="+", help=".mkv file or dir to sweep")
    ap.add_argument(
        "--show",
        help="Override show name lookup (otherwise inferred from /TV Shows/<X>/)",
    )
    ap.add_argument("--audio", help="Override audio lang (jpn/eng/...)")
    ap.add_argument(
        "--sub-mode",
        choices=["full", "forced", "none"],
        help="Override sub-track mode",
    )
    ap.add_argument(
        "--langs",
        help="Comma-separated subtitle langs to OCR (e.g. eng,spa,zho). Overrides config.",
    )
    ap.add_argument("--sub-lang", help="(deprecated) single lang — use --langs")
    ap.add_argument(
        "--overwrite", action="store_true", help="Re-OCR even if .srt exists"
    )
    ap.add_argument(
        "--no-propedit",
        action="store_true",
        help="Skip mkvpropedit (don't touch the .mkv)",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Print would-do, don't modify files"
    )
    ap.add_argument(
        "--post-rip",
        action="store_true",
        help="Invoked from burndvd; don't fail on per-file errors",
    )
    args = ap.parse_args()

    lock_fh = None
    if not args.dry_run:
        lock_fh = acquire_lock()
    try:
        cfg = load_config()
        mkvs = find_mkvs(args.paths)
        if not mkvs:
            print("subocr: no .mkv files found", file=sys.stderr)
            sys.exit(1)

        failed = 0
        for mkv in mkvs:
            show = args.show or show_for_path(mkv) or "_default"
            try:
                audio_streams, _ = probe_tracks(mkv)
                audio_langs = {a.lang for a in audio_streams}
            except Exception:
                audio_langs = set()
            settings = show_settings(cfg, show, audio_langs)
            if args.audio:
                settings["audio"] = _to_iso639_2(args.audio)
            if args.sub_mode:
                settings["sub_track"] = args.sub_mode
            if args.langs:
                settings["sub_langs"] = [
                    _to_iso639_2(x.strip())
                    for x in args.langs.split(",")
                    if x.strip()
                ]
            elif args.sub_lang:
                settings["sub_langs"] = [_to_iso639_2(args.sub_lang)]
            try:
                ok = process_mkv(mkv, settings, args)
                if not ok:
                    failed += 1
            except Exception as e:
                failed += 1
                print(f"{C.RED}subocr: {mkv.name}: {e}{C.R}", file=sys.stderr)
                if not args.post_rip:
                    raise
        if failed:
            print(
                f"{C.YLW}subocr: {failed}/{len(mkvs)} file(s) had errors{C.R}",
                file=sys.stderr,
            )
            sys.exit(0 if args.post_rip else 2)
    finally:
        if lock_fh is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            finally:
                lock_fh.close()


if __name__ == "__main__":
    main()
