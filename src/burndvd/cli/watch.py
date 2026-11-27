"""CLI entrypoint for zero-babysit multi-drive TV box-set watch mode (`burndvd --watch`)."""

from __future__ import annotations

import argparse
import os
import sys

from burndvd.watch import (
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_SCAN_WAIT_S,
    DEFAULT_WATCH_MAKEMKVCON,
    BoxSetWatcher,
    WatchConfig,
    resolve_default_target_root,
    resolve_no_extras,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="burndvd --watch",
        description=(
            "Zero-babysit multi-drive TV box-set watcher: polls optical drives, "
            "serializes episode-slot reservation across drives, and rips discs automatically."
        ),
    )
    parser.add_argument(
        "--show",
        required=True,
        help='TV show title (e.g. "Dragon Ball Super")',
    )
    parser.add_argument(
        "--season",
        required=True,
        type=int,
        help="Season number (e.g. 1)",
    )
    parser.add_argument(
        "--format",
        dest="media_format",
        default="BD",
        type=str.upper,
        choices=("4K", "BD", "DVD"),
        help="Disc format (default: BD)",
    )
    parser.add_argument(
        "--target-root",
        default=None,
        help=(
            "Target show directory (default: <nas_root>/TV Shows/<show> or "
            "TV Shows 4K/<show>, auto-preferring /private/nas/media if writable "
            "else /Volumes/Media)"
        ),
    )
    parser.add_argument(
        "--no-extras",
        dest="no_extras",
        action="store_true",
        default=None,
        help="Skip ripping TV bonus/extra titles (overrides config/env and --with-extras)",
    )
    parser.add_argument(
        "--with-extras",
        dest="no_extras",
        action="store_false",
        help="Include TV bonus/extra titles (overrides config/env and --no-extras)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_S,
        help=f"Seconds between drive polls (default: {DEFAULT_POLL_INTERVAL_S})",
    )
    parser.add_argument(
        "--scan-wait",
        type=float,
        default=DEFAULT_SCAN_WAIT_S,
        help=(
            f"Max seconds to wait for a concurrent drive to finish initial disc scan "
            f"before assigning the next drive episode_start (default: {DEFAULT_SCAN_WAIT_S})"
        ),
    )
    parser.add_argument(
        "--episode-start",
        type=int,
        default=None,
        help="Optional episode number override for the very first disc only; subsequent discs auto-advance",
    )
    parser.add_argument(
        "--max-discs",
        type=int,
        default=0,
        help="Maximum number of discs to rip before exiting cleanly (0 = unlimited until Ctrl-C)",
    )
    parser.add_argument(
        "--makemkvcon",
        default=os.environ.get("BURNDVD_MAKEMKVCON", DEFAULT_WATCH_MAKEMKVCON),
        help=f"Path to makemkvcon binary (default: {DEFAULT_WATCH_MAKEMKVCON})",
    )
    parser.add_argument(
        "--ripqueue-py",
        default=None,
        help="Path to ripqueue.py / burndvd-queue (default: ~/scripts/ripqueue.py or python -m burndvd.cli.ripqueue)",
    )
    return parser


def config_from_args(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser | None = None,
) -> WatchConfig:
    """Validate parsed CLI arguments and build a `WatchConfig`."""
    def _err(msg: str) -> None:
        if parser is not None:
            parser.error(msg)
        raise ValueError(msg)

    show = (args.show or "").strip()
    if not show:
        _err("--show must be non-empty")
    if args.season < 1:
        _err("--season must be >= 1")
    if args.poll_interval <= 0:
        _err("--poll-interval must be > 0")
    if args.scan_wait < 0:
        _err("--scan-wait must be >= 0")
    if args.episode_start is not None and args.episode_start < 1:
        _err("--episode-start must be >= 1")
    if args.max_discs < 0:
        _err("--max-discs must be >= 0")

    target_root = args.target_root or resolve_default_target_root(
        show, args.media_format
    )
    no_extras = resolve_no_extras(args.no_extras)

    return WatchConfig(
        show=show,
        season=int(args.season),
        media_format=args.media_format,
        target_root=target_root,
        no_extras=no_extras,
        poll_interval=float(args.poll_interval),
        scan_wait=float(args.scan_wait),
        episode_start=args.episode_start,
        max_discs=int(args.max_discs),
        makemkvcon=args.makemkvcon,
        ripqueue_py=args.ripqueue_py,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = config_from_args(args, parser=parser)
    watcher = BoxSetWatcher(config)
    summary = watcher.run()
    return 0 if summary.failed == 0 else 1


def cli_main() -> None:
    sys.exit(main())


if __name__ == "__main__":
    raise SystemExit(main())
