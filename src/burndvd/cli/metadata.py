"""Thin CLI entrypoint for `burndvd-metadata` (used by `bin/burndvd`)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from burndvd.metadata import (
    compute_next_tv_episode,
    episode_contract,
    infer_defaults,
    movie_title_has_year,
    normalize_movie_title,
    write_queue,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    defaults = subparsers.add_parser("defaults")
    defaults.add_argument("volume")
    defaults.add_argument("--tv-root", action="append", default=[])

    normalize = subparsers.add_parser("normalize-movie")
    normalize.add_argument("title")

    has_year = subparsers.add_parser("has-year")
    has_year.add_argument("title")

    contract = subparsers.add_parser("episode-contract")
    contract.add_argument("--title", required=True)
    contract.add_argument("--season", required=True, type=int)
    contract.add_argument("--volume", required=True)

    next_ep = subparsers.add_parser("next-episode")
    next_ep.add_argument("--title", required=True)
    next_ep.add_argument("--season", required=True, type=int)
    next_ep.add_argument("--target-root", default="")
    next_ep.add_argument("--season-dir", default="")
    next_ep.add_argument("--media-smb-root", default="/Volumes/Media")
    next_ep.add_argument("--media-nfs-root", default="/private/nas/media")
    next_ep.add_argument("--wait-scan-s", type=float, default=0.0)

    queue = subparsers.add_parser("write-queue")
    queue.add_argument("--output", required=True, type=Path)
    queue.add_argument("--title", required=True)
    queue.add_argument("--type", dest="item_type", required=True)
    queue.add_argument("--season", default="")
    queue.add_argument("--episode-start", default="")
    queue.add_argument("--expected-episodes", default="")
    queue.add_argument("--expected-disc-episodes", default="")
    queue.add_argument("--expected-title-ids", default="")
    queue.add_argument("--expected-physical-disc", default="")
    queue.add_argument("--target-root", required=True)
    queue.add_argument("--format", dest="media_format", required=True)
    queue.add_argument("--notes", default="")
    queue.add_argument("--features", dest="features_json", default="")

    args = parser.parse_args()
    if args.command == "defaults":
        print(
            "\n".join(
                infer_defaults(args.volume, tv_roots=args.tv_root or None)
            )
        )
        return 0
    if args.command == "normalize-movie":
        print(normalize_movie_title(args.title))
        return 0
    if args.command == "has-year":
        return 0 if movie_title_has_year(args.title) else 1
    if args.command == "episode-contract":
        value = episode_contract(args.title, args.season, args.volume)
        if value is None:
            return 2
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    if args.command == "next-episode":
        value = compute_next_tv_episode(
            title=args.title,
            season=args.season,
            target_root=args.target_root,
            season_dir=args.season_dir or None,
            media_smb_root=args.media_smb_root,
            media_nfs_root=args.media_nfs_root,
            wait_scan_s=args.wait_scan_s,
        )
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    if args.command == "write-queue":
        write_queue(
            args.output,
            title=args.title,
            item_type=args.item_type,
            season=args.season,
            episode_start=args.episode_start,
            expected_episodes=args.expected_episodes,
            expected_disc_episodes=args.expected_disc_episodes,
            expected_title_ids=args.expected_title_ids,
            expected_physical_disc=args.expected_physical_disc,
            target_root=args.target_root,
            media_format=args.media_format,
            notes=args.notes,
            features_json=args.features_json,
        )
        return 0
    raise AssertionError(f"unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
