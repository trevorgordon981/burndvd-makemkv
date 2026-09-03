#!/usr/bin/env python3
"""Pure, hardware-free metadata and queue helpers for ``burndvd``.

Keeping this logic outside the shell wrapper makes title inference and CSV/JSON
escaping testable without querying, unmounting, or otherwise touching a drive.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


QUEUE_FIELDS = (
    "title",
    "type",
    "season",
    "episode_start",
    "expected_episodes",
    "expected_disc_episodes",
    "expected_title_ids",
    "expected_physical_disc",
    "discs",
    "target_root",
    "format",
    "notes",
    "features",
)

# Verified against the physical-title audit logs retained during the
# 2026-09-02 Game of Thrones repair.  The per-disc counts are important: a
# season total alone cannot distinguish a 25-minute bonus on Disc 1 from an
# episode and will shift every later filename.
TV_EPISODE_CONTRACTS = {
    "gameofthrones": {
        1: (10, ((1, 2), (1, 2, 3), (1, 2, 3), (1, 2))),
        2: (10, ((1, 2, 3), (1, 2, 3), (1, 2, 3), (1,))),
        3: (10, ((1, 2, 3), (1, 2, 3), (1, 2, 3), (1,))),
        4: (10, ((1, 3), (1, 2, 3), (1, 2, 3), (1, 2))),
        5: (10, ((1, 2), (1, 2, 3), (1, 2, 3), (1, 2))),
        6: (10, ((1, 2, 5), (1, 2), (1, 2, 3), (1, 2))),
    },
}

_YEAR_SUFFIX_RE = re.compile(r"\((?:18|19|20)\d{2}\)$")
_GOT_DISC_RE = re.compile(
    r"(?i)^game[._\s-]*of[._\s-]*thrones[._\s-]*"
    r"s0*(\d+)[._\s-]*(?:disc|d)[._\s-]*0*(\d+)(?:$|[._\s-])"
)


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def canonical_fast_title(value: str) -> str | None:
    """Return a canonical Fast-franchise movie title for a known alias/label.

    Optical-disc labels are often abbreviated and carry mastering suffixes
    (for example ``2F2F`` and ``FFTokyoDriftUHDPK75``).  Match distinctive
    tokens rather than requiring an exact label, while leaving unrelated titles
    untouched.
    """

    key = _compact(value)
    if "tokyodrift" in key:
        return "The Fast and the Furious: Tokyo Drift (2006)"
    if key == "2f2f" or "2fast2furious" in key:
        return "2 Fast 2 Furious (2003)"
    if key in {"thefastandthefurious", "fastandfurious2001"}:
        return "The Fast and the Furious (2001)"
    if key in {"fastandfurious", "fastandfurious2009"}:
        return "Fast & Furious (2009)"
    if "fastfive" in key:
        return "Fast Five (2011)"
    if key in {"fastfurious6", "fastandfurious6", "furious6"}:
        return "Fast & Furious 6 (2013)"
    if "furious7" in key:
        return "Furious 7 (2015)"
    if "fateofthefurious" in key:
        return "The Fate of the Furious (2017)"
    if key in {"f9", "f9thefastsaga"}:
        return "F9: The Fast Saga (2021)"
    if "fastx" in key:
        return "Fast X (2023)"
    return None


def normalize_movie_title(value: str) -> str:
    title = re.sub(r"\s+", " ", value).strip()
    if not title:
        return ""
    # Explicit user-provided canonical titles win, including punctuation.
    if movie_title_has_year(title):
        return title
    return canonical_fast_title(title) or title


def movie_title_has_year(value: str) -> bool:
    return bool(_YEAR_SUFFIX_RE.search(value.strip()))


def infer_defaults(volume: str) -> tuple[str, str, str]:
    """Infer (type, season, title) from an optical volume label."""

    got_disc = _GOT_DISC_RE.match(volume.strip())
    if got_disc:
        return "tv-season", got_disc.group(1), "Game of Thrones"

    spaced = volume.replace("_", " ").strip()
    tv_pat = r"(?i)(S\d+\s*D\d+|Season[\s-]+\d+|Disc[\s-]+\d+|Vol(?:ume)?[\s-]+\d+)"
    marker = re.search(tv_pat, spaced)
    if marker:
        season_match = re.search(
            r"(?i)(?<![A-Za-z])S0*(\d+)|Season[\s-]+0*(\d+)", spaced
        )
        season = (
            (season_match.group(1) or season_match.group(2))
            if season_match
            else ""
        )
        title = re.sub(tv_pat + r".*$", "", spaced).strip().rstrip("-").strip().title()
        return "tv-season", season, title

    mapped = canonical_fast_title(spaced)
    if mapped:
        title = mapped
    elif movie_title_has_year(spaced):
        title = spaced
    else:
        title = spaced.title()
    return "movie", "", title


def disc_number_from_label(value: str) -> int | None:
    """Extract a physical disc number without treating a season as a disc."""

    match = re.search(
        r"(?i)(?:^|[^a-z0-9])(?:disc|disk|d)[._\s-]*0*(\d{1,2})(?:$|[^\d])",
        value,
    )
    return int(match.group(1)) if match else None


def episode_contract(title: str, season: int, volume: str) -> dict[str, int] | None:
    """Return the immutable physical-disc episode contract for a TV disc."""

    season_contract = TV_EPISODE_CONTRACTS.get(_compact(title), {}).get(int(season))
    disc = disc_number_from_label(volume)
    if season_contract is None or disc is None:
        return None
    total, per_disc = season_contract
    if disc < 1 or disc > len(per_disc):
        return None
    title_ids = [int(value) for value in per_disc[disc - 1]]
    count = len(title_ids)
    start = 1 + sum(len(value) for value in per_disc[: disc - 1])
    return {
        "disc": disc,
        "episode_start": start,
        "expected_disc_episodes": count,
        "expected_episodes": int(total),
        "expected_title_ids": title_ids,
    }


def parse_features(raw: str) -> list[dict[str, object]]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"features is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, list):
        raise ValueError("features must be a JSON array")

    result: list[dict[str, object]] = []
    seen: set[int] = set()
    for index, feature in enumerate(data, start=1):
        if not isinstance(feature, dict):
            raise ValueError(f"feature {index} must be an object")
        try:
            title_id = int(feature["title_id"])
            raw_name = feature["name"]
            if not isinstance(raw_name, str):
                raise TypeError("name is not a string")
            name = raw_name.strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"feature {index} requires integer title_id and non-empty name"
            ) from exc
        if title_id < 0 or not name:
            raise ValueError(
                f"feature {index} requires non-negative title_id and non-empty name"
            )
        if title_id in seen:
            raise ValueError(f"duplicate feature title_id {title_id}")
        seen.add(title_id)
        result.append({"title_id": title_id, "name": name})
    return result


def write_queue(
    output: Path,
    *,
    title: str,
    item_type: str,
    season: str,
    episode_start: str,
    target_root: str,
    media_format: str,
    notes: str,
    features_json: str = "",
    expected_episodes: str = "",
    expected_disc_episodes: str = "",
    expected_title_ids: str = "",
    expected_physical_disc: str = "",
) -> None:
    """Write one queue row with standards-compliant CSV and embedded JSON."""

    features = parse_features(features_json)
    normalized_features = (
        json.dumps(features, ensure_ascii=False, separators=(",", ":"))
        if features
        else ""
    )
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(QUEUE_FIELDS)
        writer.writerow(
            [
                title,
                item_type,
                season,
                episode_start,
                expected_episodes,
                expected_disc_episodes,
                expected_title_ids,
                expected_physical_disc,
                "1",
                target_root,
                media_format,
                notes,
                normalized_features,
            ]
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    defaults = subparsers.add_parser("defaults")
    defaults.add_argument("volume")

    normalize = subparsers.add_parser("normalize-movie")
    normalize.add_argument("title")

    has_year = subparsers.add_parser("has-year")
    has_year.add_argument("title")

    contract = subparsers.add_parser("episode-contract")
    contract.add_argument("--title", required=True)
    contract.add_argument("--season", required=True, type=int)
    contract.add_argument("--volume", required=True)

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
        print("\n".join(infer_defaults(args.volume)))
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
