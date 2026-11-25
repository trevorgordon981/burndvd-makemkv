"""Title selection, play-all detection/corroboration/splitting, and empirical frame verification."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from burndvd import metadata as _metadata
from burndvd.config import (
    ALT_DEDUP_DUR_TOL_S,
    ALT_DEDUP_OFFSETS,
    C,
    EP_BONUS_MEDIAN_RATIO,
    EP_BONUS_MIN_COUNT,
    EXTRAS_MIN_DUR_S,
    FFMPEG_BIN,
    LOCAL_STAGING_BASE,
    MOVIE_ALT_CUT_MIN_RATIO,
    PARALLEL_RIP_RESERVE_GB,
    PLAY_ALL_BYTE_TOL,
    PLAY_ALL_MIN_PARTS,
    PLAY_ALL_MIN_RATIO,
    PLAY_ALL_SUM_TOL,
    PLAY_ALL_VERIFY_OFFSETS,
    PLAYALL_EP_MAX_S,
    PLAYALL_EP_MIN_S,
    PLAYALL_MAX_STRIDE,
    PLAYALL_MIN_SHAPE_RATIO,
    PLAYALL_MIN_TOTAL_S,
    PLAYALL_SPREAD_TOL_S,
    STAGING_GROWTH_FACTOR,
    STAGING_HEADROOM_GB,
    TV_MIN_DUR_S,
    fmt_dur,
    sanitize,
)
from burndvd.drive import append_log
from burndvd.models import QueueItem
from burndvd.probe import (
    _title_size_bytes,
    disc_label,
    ffprobe_dur_s,
    parse_duration,
    title_num_from_filename,
)


def movie_part_from_notes(notes: str) -> int | None:
    """Extract an explicit PT/PART number from a physical-disc label recorded in notes."""
    match = re.search(
        r"(?:^|[^A-Z0-9])(?:PT|PART)[._ -]*(\d{1,2})(?:$|[^0-9])",
        notes,
        re.IGNORECASE,
    )
    if not match:
        return None
    part_n = int(match.group(1))
    return part_n if part_n > 0 else None


def movie_filename(item: QueueItem, disc_n: int) -> str:
    """Compute canonical Plex/Jellyfin movie filename (preserving PT/PART and multi-disc)."""
    base = sanitize(item.title)
    physical_part = movie_part_from_notes(item.notes)
    if physical_part is not None:
        quality = " - [Bluray-2160p]" if item.format.upper() == "4K" else ""
        return f"{base}{quality} - pt{physical_part}.mkv"
    if item.discs > 1:
        return f"{base} - disc{disc_n}.mkv"
    return f"{base}.mkv"


def disc_n_from_label(info: dict) -> int | None:
    m = re.search(
        r"(?<![A-Za-z0-9])(?:disc|disk|d)[\s_.:-]*0*(\d{1,2})(?=$|[^0-9])",
        disc_label(info),
        re.IGNORECASE,
    )
    return int(m.group(1)) if m else None


def season_n_from_label(info: dict) -> int | None:
    match = re.search(
        r"(?<![A-Za-z0-9])(?:season|s)[\s_.:-]*0*(\d{1,2})(?=$|[^0-9])",
        disc_label(info),
        re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def bound_physical_disc(item: QueueItem, info: dict, queue_disc_n: int) -> int:
    """Bind contracted TV rows to an independently observed title/season/disc tuple."""
    detected = disc_n_from_label(info)
    if item.type != "tv-season":
        return detected if detected is not None else queue_disc_n
    if item.expected_disc_episodes == 0:
        return detected if detected is not None else queue_disc_n
    label = disc_label(info)
    normalized_label = re.sub(r"[^a-z0-9]+", "", label.casefold())
    normalized_title = re.sub(r"[^a-z0-9]+", "", item.title.casefold())
    if not normalized_title or normalized_title not in normalized_label:
        raise ValueError(
            f"physical TV title mismatch: expected {item.title!r}, label reports {label!r}"
        )
    detected_season = season_n_from_label(info)
    if detected_season is None:
        raise ValueError(
            "cannot verify physical TV season: MakeMKV label has no season number"
        )
    if detected_season != item.season:
        raise ValueError(
            "physical TV season mismatch: "
            f"contract expects season {item.season}, label reports {detected_season}"
        )
    if detected is None:
        raise ValueError(
            "cannot verify physical TV disc: MakeMKV label has no disc number"
        )
    if detected != item.expected_physical_disc:
        raise ValueError(
            "physical TV disc mismatch: "
            f"contract expects disc {item.expected_physical_disc}, "
            f"label reports {detected}"
        )
    return item.expected_physical_disc


def automatic_review_contract_error(item: QueueItem, info: dict) -> str | None:
    """Require an exact built-in metadata registry match for automatic review routing."""
    if item.type != "tv-season":
        return "automatic review is only valid for TV seasons"
    try:
        contract = _metadata.episode_contract(
            item.title, item.season, disc_label(info)
        )
    except (AttributeError, TypeError, ValueError) as exc:
        return f"cannot verify automatic-review metadata contract: {exc}"
    if contract is None:
        return (
            "automatic review requires a registry-recognized physical-disc "
            "metadata contract"
        )
    queued = {
        "disc": item.expected_physical_disc,
        "episode_start": item.episode_start,
        "expected_disc_episodes": item.expected_disc_episodes,
        "expected_episodes": item.expected_episodes,
        "expected_title_ids": list(item.expected_title_ids),
    }
    expected = {
        "disc": int(contract["disc"]),
        "episode_start": int(contract["episode_start"]),
        "expected_disc_episodes": int(contract["expected_disc_episodes"]),
        "expected_episodes": int(contract["expected_episodes"]),
        "expected_title_ids": [int(value) for value in contract["expected_title_ids"]],
    }
    if queued != expected:
        return (
            "automatic-review queue contract does not match the verified "
            f"metadata registry: queued={queued}, expected={expected}"
        )
    return None


def loose_match(label: str, expected: str) -> bool:
    """Fuzzy substring match between disc volume label and expected item title."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())
    a, b = norm(label), norm(expected)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < 6 or len(b) < 6:
        return False
    return a in b or b in a


def partition_episode_contract(
    paths: list[Path], item: QueueItem
) -> tuple[list[Path], list[Path], list[int]]:
    """Apply exact physical title-ID contract while preserving disc order."""
    if item.expected_disc_episodes == 0:
        return paths, [], []
    if not item.expected_title_ids:
        selected = paths[: item.expected_disc_episodes]
        return selected, paths[item.expected_disc_episodes :], []
    by_title = {title_num_from_filename(path.name): path for path in paths}
    missing = [
        title_id for title_id in item.expected_title_ids if title_id not in by_title
    ]
    if missing:
        return [], paths, missing
    selected = [by_title[title_id] for title_id in item.expected_title_ids]
    return selected, [path for path in paths if path not in selected], []


def _median(xs: list[float] | list[int]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def episode_bonus_split(
    cand: list[tuple[int, int]]
) -> tuple[list[int], list[int], float | None]:
    """Split episode candidates into `(episode_ids, bonus_ids, threshold_s)`.

    Demotes featurettes far shorter than the disc's median episode runtime to Extras.
    """
    if len(cand) < EP_BONUS_MIN_COUNT:
        return sorted(t for t, _ in cand), [], None
    thr = EP_BONUS_MEDIAN_RATIO * _median([d for _, d in cand])
    eps = sorted(t for t, d in cand if d >= thr)
    bonus = sorted(t for t, d in cand if d < thr)
    return eps, bonus, thr


def play_all_title_ids(info: dict) -> set[int]:
    """Identify titles judged to be 'play all' compilations of episode subsets.

    Handles both single and dual equal-length play-alls by filtering eligible
    part lengths per-part (`d >= PLAY_ALL_MIN_RATIO * od`) rather than comparing
    against `max(others)`.
    """
    durs = [
        (tid, parse_duration(t.get(9, "0:00:00")))
        for tid, t in info["titles"].items()
    ]
    durs = [(tid, d) for tid, d in durs if d >= TV_MIN_DUR_S]
    out: set[int] = set()
    if len(durs) < PLAY_ALL_MIN_PARTS + 1:
        return out

    def proper_subset_sum_matches(
        target: int, parts: list[int], min_parts: int
    ) -> bool:
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
        eligible = [od for od in others if d >= PLAY_ALL_MIN_RATIO * od]
        if len(eligible) < PLAY_ALL_MIN_PARTS:
            continue
        s_all = sum(eligible)
        if s_all > 0 and abs(d - s_all) / s_all <= PLAY_ALL_SUM_TOL:
            out.add(tid)
            continue
        if proper_subset_sum_matches(d, eligible, min_parts=3):
            out.add(tid)
    return out


def select_titles(item: QueueItem, info: dict) -> list[int]:
    """Choose the primary title IDs to rip for a movie, double-feature, or TV disc."""
    durs = [
        (tid, parse_duration(t.get(9, "0:00:00")))
        for tid, t in info["titles"].items()
    ]
    if not durs:
        return []
    if item.type == "double-feature":
        present = set(info["titles"].keys())
        return [f["title_id"] for f in item.features if f["title_id"] in present]
    if item.type == "movie":
        longest_tid, longest = max(durs, key=lambda x: x[1])
        if longest <= 0:
            return []

        def _title_bytes(tid: int) -> int:
            try:
                return int(info["titles"][tid].get(11, "0") or "0")
            except (TypeError, ValueError):
                return 0

        peers = [
            (tid, dur)
            for tid, dur in durs
            if dur >= longest * MOVIE_ALT_CUT_MIN_RATIO
        ]
        rated = [
            (tid, _title_bytes(tid) / dur)
            for tid, dur in peers
            if dur > 0 and _title_bytes(tid) > 0
        ]
        if len(rated) > 1:
            best_tid, best_rate = max(rated, key=lambda x: x[1])
            if best_tid != longest_tid:
                print(
                    f"  movie pick: t{best_tid:02d} "
                    f"({best_rate * 8 / 1e6:.1f} Mbps) over longer t{longest_tid:02d} "
                    f"-- higher bitrate wins on a same-feature alternate"
                )
            return [best_tid]
        return [longest_tid]

    play_all = play_all_title_ids(info)
    cand = [
        (tid, dur)
        for tid, dur in durs
        if dur >= TV_MIN_DUR_S and tid not in play_all
    ]
    eps, _bonus, _thr = episode_bonus_split(cand)
    return eps


def log_title_audit(args, item: QueueItem, info: dict, title_ids: list[int]) -> None:
    """Log the disc's full title table and keep/skip rationale to console and state log."""
    chosen = set(title_ids)
    play_all = (
        play_all_title_ids(info)
        if item.type not in ("movie", "double-feature")
        else set()
    )
    bonus_gate: set[int] = set()
    if item.type not in ("movie", "double-feature"):
        _cand = [
            (tid, parse_duration(info["titles"][tid].get(9, "0:00:00")))
            for tid in info["titles"]
            if parse_duration(info["titles"][tid].get(9, "0:00:00")) >= TV_MIN_DUR_S
            and tid not in play_all
        ]
        _eps, _bonus_ids, _thr = episode_bonus_split(_cand)
        bonus_gate = set(_bonus_ids)
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
        elif tid in bonus_gate:
            reason = "skip (short vs disc median -> Extras)"
        elif tid in play_all:
            reason = "skip (play-all?)"
        elif dur_s < floor:
            tier = "extra" if dur_s >= EXTRAS_MIN_DUR_S else "noise"
            reason = f"skip <{floor}s TV floor ({tier})"
        else:
            reason = "skip"
        rows.append((tid, dur_s, name, size, kept, reason))
        print(
            f"  {C.D}t{tid:02d}  {fmt_dur(dur_s):>7}  {size:>9}  "
            f"{'KEEP' if kept else 'skip'}  {name}{C.R}"
        )
    disc_n = disc_n_from_label(info) or "?"
    for tid, dur_s, name, size, kept, reason in rows:
        append_log(
            args,
            f"TITLE {item.title} S{item.season:02d}D{disc_n} "
            f"t{tid:02d} {fmt_dur(dur_s)} ({dur_s}s) {size} {reason} | {name}",
        )


def _play_all_structurally_corroborated(
    pa_dur: int,
    pa_bytes: int,
    non_play_all_eps: list[tuple[int, int, int]],
) -> bool:
    """True only when a duration-flagged play-all is structurally corroborated for
    pre-rip skipping (>= 3 episodes + matching duration subset + byte-total agreement)."""
    if len(non_play_all_eps) < 3 or pa_dur <= 0:
        return False
    eligible = [
        (tid, dur, sz)
        for tid, dur, sz in non_play_all_eps
        if pa_dur >= PLAY_ALL_MIN_RATIO * dur
    ]
    if len(eligible) < 3:
        return False

    def _bytes_ok(subset: list[tuple[int, int, int]]) -> bool:
        if pa_bytes <= 0:
            return True
        if any(sz <= 0 for _, _, sz in subset):
            return True
        sum_b = sum(sz for _, _, sz in subset)
        return sum_b > 0 and abs(pa_bytes - sum_b) / sum_b <= PLAY_ALL_BYTE_TOL

    total_dur = sum(dur for _, dur, _ in eligible)
    if total_dur > 0 and abs(pa_dur - total_dur) / total_dur <= PLAY_ALL_SUM_TOL:
        if _bytes_ok(eligible):
            return True

    n = len(eligible)
    if n > 20:
        return False
    tol = max(int(pa_dur * PLAY_ALL_SUM_TOL), 30)
    for mask in range(1, 1 << n):
        if bin(mask).count("1") < 3:
            continue
        s = 0
        for i in range(n):
            if mask & (1 << i):
                s += eligible[i][1]
                if s > pa_dur + tol:
                    break
        if abs(s - pa_dur) <= tol:
            subset = [eligible[i] for i in range(n) if mask & (1 << i)]
            if _bytes_ok(subset):
                return True
    return False


def planned_rip_selectors(
    item: QueueItem, info: dict, title_ids: list[int]
) -> tuple[list[str], set[int]]:
    """Return `(makemkvcon_selectors, skipped_play_all_ids)`.

    Never pre-skips any title in `item.expected_title_ids`, and only pre-skips
    structurally corroborated (>= 3 parts, matching byte totals) play-all titles.
    """
    if item.type == "movie":
        return [str(tid) for tid in title_ids], set()
    titles = info.get("titles", {}) if isinstance(info, dict) else {}
    if item.type == "tv-season" and titles:
        play_all = play_all_title_ids(info)
        if play_all:
            protected_tids = set(item.expected_title_ids or ())
            non_play_all_eps = [
                (
                    tid,
                    parse_duration(title.get(9, "0:00:00")),
                    _title_size_bytes(title),
                )
                for tid, title in titles.items()
                if tid not in play_all
                and parse_duration(title.get(9, "0:00:00")) >= TV_MIN_DUR_S
            ]
            skippable: set[int] = set()
            for pa_tid in play_all:
                if pa_tid in protected_tids:
                    continue
                pa_title = titles.get(pa_tid, titles.get(str(pa_tid)))
                pa_dur = parse_duration((pa_title or {}).get(9, "0:00:00"))
                pa_bytes = _title_size_bytes(pa_title)
                if _play_all_structurally_corroborated(
                    pa_dur, pa_bytes, non_play_all_eps
                ):
                    skippable.add(pa_tid)
            if skippable:
                keep_ids = sorted(
                    tid
                    for tid, title in titles.items()
                    if tid not in skippable
                    and parse_duration(title.get(9, "0:00:00")) >= EXTRAS_MIN_DUR_S
                )
                if keep_ids:
                    return [str(tid) for tid in keep_ids], skippable
    return ["all"], set()


def staging_space_requirement(
    item: QueueItem,
    info: dict,
    title_ids: list[int],
    *,
    split_playall: bool = True,
) -> tuple[float, float | None, bool]:
    """Return `(required_gb, selected_gb, used_legacy_fallback)`."""
    legacy_gb = 110.0 if (item.format or "").upper() == "4K" else 60.0
    titles = info.get("titles", {}) if isinstance(info, dict) else {}
    if item.type == "movie":
        written_ids = list(title_ids)
    else:
        _, skipped_play_all = planned_rip_selectors(item, info, title_ids)
        written_ids = [
            tid
            for tid, title in titles.items()
            if tid not in skipped_play_all
            and parse_duration(title.get(9, "0:00:00")) >= EXTRAS_MIN_DUR_S
        ]
    if not written_ids:
        return legacy_gb, None, True

    selected_bytes = 0
    for tid in written_ids:
        title = titles.get(tid, titles.get(str(tid)))
        size_bytes = _title_size_bytes(title)
        if size_bytes <= 0:
            return legacy_gb, None, True
        selected_bytes += size_bytes

    will_split_playall = False
    if item.type == "tv-season" and split_playall:
        longs = [
            tid
            for tid in written_ids
            if parse_duration(
                (titles.get(tid, titles.get(str(tid))) or {}).get(9, "0:00:00")
            )
            >= PLAYALL_MIN_TOTAL_S
        ]
        others = [
            tid
            for tid in written_ids
            if tid not in longs
            and parse_duration(
                (titles.get(tid, titles.get(str(tid))) or {}).get(9, "0:00:00")
            )
            >= TV_MIN_DUR_S
        ]
        will_split_playall = len(longs) == 1 and len(others) < PLAY_ALL_MIN_PARTS

    selected_gb = selected_bytes / 1e9
    growth = (
        (STAGING_GROWTH_FACTOR + 1.0)
        if will_split_playall
        else STAGING_GROWTH_FACTOR
    )
    required_gb = selected_gb * growth + STAGING_HEADROOM_GB
    return required_gb, selected_gb, False


def staging_space_error(
    free_gb: float,
    required_gb: float,
    item_format: str,
    selected_gb: float | None,
    used_legacy_fallback: bool,
    active_other_rips: int = 0,
) -> str | None:
    """Return a human-readable error if free staging space is insufficient."""
    total_required = required_gb + active_other_rips * PARALLEL_RIP_RESERVE_GB
    if free_gb >= total_required:
        return None
    if active_other_rips:
        return (
            f"low disk space on local staging vs in-flight rips: "
            f"{free_gb:.1f}GB free, need {total_required:.1f}GB "
            f"({required_gb:.1f}GB for this {item_format} + "
            f"{PARALLEL_RIP_RESERVE_GB:.0f}GB per active in-flight rip "
            f"× {active_other_rips})"
        )
    if used_legacy_fallback:
        detail = f"legacy-safe fallback for {item_format}; title byte metadata unavailable"
    else:
        detail = (
            f"{selected_gb:.1f}GB selected titles + 5% growth allowance "
            f"+ {STAGING_HEADROOM_GB:.0f}GB headroom"
        )
    return (
        f"low disk space on local staging: {free_gb:.1f}GB free at "
        f"{LOCAL_STAGING_BASE}, need {required_gb:.1f}GB ({detail})"
    )


# --- Empirical frame verification & content dedup ---


def _frame_md5(path: str, offset_s: float) -> str | None:
    """Return MD5 hex digest of a single decoded video frame at `offset_s`, or None."""
    try:
        r = subprocess.run(
            [
                FFMPEG_BIN,
                "-v",
                "error",
                "-ss",
                f"{offset_s:g}",
                "-i",
                path,
                "-frames:v",
                "1",
                "-f",
                "md5",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"MD5=([0-9a-fA-F]+)", r.stdout)
    return m.group(1).lower() if m else None


def _alt_playlist_duplicate(path_a: str, path_b: str, dur_s: float) -> bool:
    usable = [o for o in ALT_DEDUP_OFFSETS if o < dur_s - 1]
    if len(usable) < 3 and dur_s > 4:
        usable = [round(dur_s * f, 2) for f in (0.2, 0.4, 0.6, 0.8)]
    if len(usable) < 3:
        return False
    matches = probed = 0
    for o in usable:
        a = _frame_md5(path_a, o)
        b = _frame_md5(path_b, o)
        if a is None or b is None:
            continue
        probed += 1
        if a == b:
            matches += 1
    return probed >= 3 and matches >= (probed // 2 + 1)


def _content_relation(path_a: str, path_b: str, dur_s: float) -> str:
    """Return `'duplicate'`, `'distinct'`, or `'unknown'` from decoded frame hashes."""
    usable = [offset for offset in ALT_DEDUP_OFFSETS if offset < dur_s - 1]
    if len(usable) < 3 and dur_s > 4:
        usable = [round(dur_s * fraction, 2) for fraction in (0.2, 0.4, 0.6, 0.8)]
    if len(usable) < 3:
        return "unknown"
    hashes = [
        (_frame_md5(path_a, offset), _frame_md5(path_b, offset))
        for offset in usable
    ]
    probed = [
        (left, right)
        for left, right in hashes
        if left is not None and right is not None
    ]
    if len(probed) < 3:
        return "unknown"
    matches = sum(left == right for left, right in probed)
    return "duplicate" if matches >= (len(probed) // 2 + 1) else "distinct"


def existing_content_collision(
    candidate: Path, candidate_duration: float, existing: dict[int, Path]
) -> tuple[Path | None, str | None]:
    """Detect decoded-content repeats against existing season files (fail-closed)."""
    for path in existing.values():
        duration = ffprobe_dur_s(str(path))
        if duration <= 0:
            return None, f"cannot read duration for existing episode {path}"
        if abs(duration - candidate_duration) > ALT_DEDUP_DUR_TOL_S:
            continue
        relation = _content_relation(str(candidate), str(path), candidate_duration)
        if relation == "duplicate":
            return path, None
        if relation == "unknown":
            return None, f"cannot prove content distinct from same-duration {path}"
    return None, None


def verify_play_all(
    flagged_path: str, first_episode_path: str, shortest_ep_dur_s: float
) -> tuple[bool, str]:
    """Empirically confirm or refute a heuristic play-all flag via frame MD5s."""
    usable = [o for o in PLAY_ALL_VERIFY_OFFSETS if o < shortest_ep_dur_s - 1]
    if not usable and shortest_ep_dur_s > 2:
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


# --- Play-all-only chapter splitter ---


def _ff(tool: str) -> str:
    return shutil.which(tool) or f"/opt/homebrew/bin/{tool}"


def chapter_marks_s(path: str) -> list[float]:
    try:
        r = subprocess.run(
            [_ff("ffprobe"), "-v", "error", "-show_chapters", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return [
            float(c["start_time"])
            for c in json.loads(r.stdout or "{}").get("chapters", [])
        ]
    except Exception:
        return []


def _intra_episode_shape_ratio(marks: list[float], stride: int) -> float:
    ratios = []
    for e in range(0, len(marks) - stride, stride):
        gaps = [marks[e + i + 1] - marks[e + i] for i in range(stride)]
        gaps = [g for g in gaps if g > 0]
        if len(gaps) >= 2:
            ratios.append(max(gaps) / min(gaps))
    if not ratios:
        return 0.0
    ratios.sort()
    return ratios[len(ratios) // 2]


def _playall_plan(marks: list[float], total: float):
    best = None
    for stride in range(2, PLAYALL_MAX_STRIDE + 1):
        if (len(marks) - 1) % stride and len(marks) % stride:
            continue
        if _intra_episode_shape_ratio(marks, stride) < PLAYALL_MIN_SHAPE_RATIO:
            continue
        edges = [marks[i] for i in range(0, len(marks), stride)]
        if total - edges[-1] >= PLAYALL_EP_MIN_S:
            edges.append(total)
        else:
            edges[-1] = total
        lens = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]
        if len(lens) < 2:
            continue
        if min(lens) < PLAYALL_EP_MIN_S or max(lens) > PLAYALL_EP_MAX_S:
            continue
        spread = max(lens) - min(lens)
        if spread > PLAYALL_SPREAD_TOL_S:
            continue
        if best is None or spread < best[0]:
            best = (spread, stride, edges, lens)
    return best


def split_chaptered_playall(args, rips: list[Path], staging: Path) -> list[Path]:
    """Expand a lone chaptered play-all title into individual episode MKVs in staging."""
    if getattr(args, "no_split_playall", False) or not rips:
        return rips
    longs = [p for p in rips if ffprobe_dur_s(str(p)) >= PLAYALL_MIN_TOTAL_S]
    if len(longs) != 1:
        return rips
    src = longs[0]
    others = [
        q for q in rips if q is not src and ffprobe_dur_s(str(q)) >= TV_MIN_DUR_S
    ]
    if len(others) >= PLAY_ALL_MIN_PARTS:
        print(
            f"{C.YLW}  Play-all present, but the disc already carries {len(others)} "
            f"episode-length titles — leaving the blob whole (it is excluded from "
            f"numbering by the play-all detector, not split).{C.R}",
            flush=True,
        )
        return rips
    total = ffprobe_dur_s(str(src))
    marks = chapter_marks_s(str(src))
    if len(marks) < 3:
        print(
            f"{C.YLW}  Single {fmt_dur(total)} title with no chapter marks — "
            f"leaving as one file.{C.R}",
            flush=True,
        )
        return rips
    plan = _playall_plan(marks, total)
    if plan is None:
        print(
            f"{C.YLW}  Single {fmt_dur(total)} title, {len(marks)} chapters, but no "
            f"stride cuts it into even episodes — leaving as one file.{C.R}",
            flush=True,
        )
        return rips
    spread, stride, edges, lens = plan
    n = len(lens)
    print(
        f"{C.B}  Play-all detected: {fmt_dur(total)}, {len(marks)} chapters → "
        f"{n} episodes at every {stride}th mark "
        f"(lengths {min(lens)/60:.1f}–{max(lens)/60:.1f}m).{C.R}",
        flush=True,
    )

    out: list[Path] = []
    for i in range(n):
        dst = staging / f"{src.stem}_split_t{900 + i:03d}.mkv"
        cmd = [
            _ff("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{edges[i]:.3f}",
            "-i",
            str(src),
            "-t",
            f"{lens[i]:.3f}",
            "-map",
            "0",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-f",
            "matroska",
            str(dst),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        got = ffprobe_dur_s(str(dst)) if dst.exists() else 0.0
        if r.returncode != 0 or abs(got - lens[i]) > 2.0:
            for p in out + [dst]:
                try:
                    p.unlink()
                except OSError:
                    pass
            print(
                f"{C.RED}  Split failed on part {i+1} "
                f"({r.stderr.strip()[:200] or f'{got:.1f}s != {lens[i]:.1f}s'}) — "
                f"keeping the single file.{C.R}",
                flush=True,
            )
            append_log(args, f"PLAYALL_SPLIT_FAIL {src.name} part {i+1}")
            return rips
        out.append(dst)
        print(
            f"{C.D}    part {i+1}/{n}  {got/60:5.2f}m  "
            f"{dst.stat().st_size/2**30:.2f} GB{C.R}",
            flush=True,
        )

    append_log(args, f"PLAYALL_SPLIT {src.name} → {n} episodes at stride {stride}")
    return [q for p in rips for q in (out if p == src else [p])]
