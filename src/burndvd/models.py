"""Core data models, queue CSV parsing/validation, and run-state persistence."""

from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from burndvd.config import C, STATE_VERSION, sanitize

REQUIRED_QUEUE_COLUMNS = {"title", "type", "discs", "target_root", "format"}
VALID_QUEUE_TYPES = {"movie", "tv-season", "double-feature"}
VALID_QUEUE_FORMATS = {"4K", "BD", "DVD"}


@dataclass
class QueueItem:
    """Single work item representing a movie, TV season disc(s), or double feature."""

    title: str
    type: str  # "movie" | "tv-season" | "double-feature"
    discs: int
    target_root: str
    format: str  # "4K" | "BD" | "DVD"
    season: int = 0
    episode_start: int = 1
    expected_episodes: int = 0
    expected_disc_episodes: int = 0
    expected_title_ids: list[int] = field(default_factory=list)
    expected_physical_disc: int = 0
    notes: str = ""
    features: list[dict] = field(default_factory=list)


@dataclass
class TitleInfo:
    """Normalized metadata for a single optical disc title."""

    title_id: int
    duration_s: int
    size_bytes: int = 0
    size_human: str = "?"
    name: str = ""
    chapters: int = 0
    is_feature: bool = False
    is_play_all: bool = False


@dataclass
class TrackInfo:
    """Audio or subtitle stream metadata inside a Matroska container."""

    index: int  # mkvmerge track id (matches `mkvextract tracks <id>:...`)
    codec: str
    lang: str
    title: str
    forced: bool = False  # Matroska forced_track / ffprobe disposition.forced
    mkv_pos: int | None = None  # 1-based positional index for mkvpropedit
    n_frames_estimate: int = 0
    size_bytes: int | None = None
    forced_only: bool = False  # True when forced mode falls back to 1 non-forced track


@dataclass
class RipResult:
    """Structured outcome of ripping a single optical disc."""

    ok: bool
    elapsed_s: float = 0.0
    files: list[str] = field(default_factory=list)
    move_jobs: list | None = None
    staging: str | None = None
    disc_fingerprint: str = ""
    used_fallback: bool = False
    error: str = ""

    def to_legacy_tuple(self) -> tuple[bool, dict | str]:
        """Convert to legacy `(ok, payload_dict_or_error_str)` shape."""
        if not self.ok:
            return False, self.error
        payload: dict = {"elapsed_s": self.elapsed_s, "files": list(self.files)}
        if self.move_jobs is not None:
            payload["move_jobs"] = list(self.move_jobs)
            payload["staging"] = self.staging
        if self.disc_fingerprint:
            payload["disc_fingerprint"] = self.disc_fingerprint
        if self.used_fallback:
            payload["used_fallback"] = True
        return True, payload


@dataclass
class FallbackResult:
    """Outcome of an automatic unencrypted-BD mkvmerge fallback attempt."""

    ok: bool
    files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    mount_path: Path | None = None
    playlists_remuxed: int = 0
    file_playlists: dict[str, str] = field(default_factory=dict)


def csv_int(row: dict, key: str, default: int) -> int:
    raw = (row.get(key) or "").strip()
    return int(raw) if raw else default


def _parse_features(raw: str) -> list[dict]:
    """Parse CSV `features` JSON array into `[{"title_id": int, "name": str}, ...]`."""
    raw = (raw or "").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("features must be a JSON array")
    out: list[dict] = []
    seen: set[int] = set()
    for index, d in enumerate(data, start=1):
        if not isinstance(d, dict):
            raise ValueError(f"feature {index} must be an object")
        try:
            title_id = int(d["title_id"])
            raw_name = d["name"]
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
        out.append({"title_id": title_id, "name": name})
    return out


def _parse_title_ids(raw: str) -> list[int]:
    raw = (raw or "").strip()
    if not raw:
        return []
    values = json.loads(raw)
    if not isinstance(values, list):
        raise ValueError("expected_title_ids must be a JSON array")
    result = [int(value) for value in values]
    if any(value < 0 for value in result) or len(result) != len(set(result)):
        raise ValueError("expected_title_ids must be unique non-negative integers")
    return result


def validate_queue_item(item: QueueItem) -> None:
    """Reject queue rows that would otherwise fail late or target an unsafe path."""
    if not item.title or "\x00" in item.title:
        raise ValueError("title must be non-empty and contain no NUL byte")
    if sanitize(item.title) in {"", ".", ".."}:
        raise ValueError("title must produce a safe non-empty path component")
    if item.type not in VALID_QUEUE_TYPES:
        raise ValueError(
            f"type must be one of {sorted(VALID_QUEUE_TYPES)}, got {item.type!r}"
        )
    if item.format not in VALID_QUEUE_FORMATS:
        raise ValueError(
            f"format must be one of {sorted(VALID_QUEUE_FORMATS)}, got {item.format!r}"
        )
    if item.discs < 1:
        raise ValueError("discs must be at least 1")
    if not item.target_root or not Path(item.target_root).is_absolute():
        raise ValueError("target_root must be an absolute path")
    if item.type == "tv-season" and item.season < 1:
        raise ValueError("tv-season rows require season >= 1")
    if item.episode_start < 1:
        raise ValueError("episode_start must be at least 1")
    if item.type == "tv-season":
        contract_fields = (
            item.expected_episodes,
            item.expected_disc_episodes,
            item.expected_physical_disc,
        )
        has_contract = (
            any(value != 0 for value in contract_fields)
            or bool(item.expected_title_ids)
        )
        if has_contract and item.expected_episodes < 1:
            raise ValueError("contracted tv-season rows require expected_episodes >= 1")
        if has_contract and item.expected_disc_episodes < 1:
            raise ValueError(
                "contracted tv-season rows require expected_disc_episodes >= 1"
            )
        if has_contract and item.expected_physical_disc < 1:
            raise ValueError(
                "contracted tv-season rows require expected_physical_disc >= 1"
            )
        if has_contract:
            final_ep = item.episode_start + item.expected_disc_episodes - 1
            if final_ep > item.expected_episodes:
                raise ValueError(
                    "disc episode range exceeds expected_episodes "
                    f"({item.episode_start}-{final_ep} > {item.expected_episodes})"
                )
            if (
                item.expected_title_ids
                and len(item.expected_title_ids) != item.expected_disc_episodes
            ):
                raise ValueError(
                    "expected_title_ids length must equal expected_disc_episodes"
                )
    if item.type == "double-feature" and len(item.features) < 2:
        raise ValueError("double-feature rows require at least two features")
    if item.type != "double-feature" and item.features:
        raise ValueError("features are only valid for double-feature rows")


def run_policy_error(
    items: list[QueueItem], *, overwrite: bool, rerip_review: bool
) -> str | None:
    """Enforce global safety invariants across a queue."""
    if overwrite and any(item.type == "tv-season" for item in items):
        return "--overwrite is forbidden for TV in every mode"
    return None


def load_queue(path: Path) -> list[QueueItem]:
    """Load and validate a ripping queue CSV file."""
    items: list[QueueItem] = []
    try:
        fh = open(path, newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"cannot open queue {path}: {exc}") from exc
    with fh:
        reader = csv.DictReader(fh)
        raw_fields = reader.fieldnames or []
        duplicate_fields = sorted(
            {name for name in raw_fields if raw_fields.count(name) > 1}
        )
        if duplicate_fields:
            raise ValueError(
                f"queue {path} has duplicate column(s): {', '.join(duplicate_fields)}"
            )
        fields = set(raw_fields)
        missing = sorted(REQUIRED_QUEUE_COLUMNS - fields)
        if missing:
            raise ValueError(
                f"queue {path} is missing required column(s): {', '.join(missing)}"
            )
        for row in reader:
            row_number = reader.line_num
            if None in row:
                raise ValueError(
                    f"queue {path} row {row_number} has extra unquoted CSV field(s)"
                )
            if not any((value or "").strip() for value in row.values()):
                continue
            try:
                item = QueueItem(
                    title=(row["title"] or "").strip(),
                    type=(row["type"] or "").strip(),
                    discs=csv_int(row, "discs", 1),
                    target_root=(row["target_root"] or "").strip(),
                    format=(row["format"] or "").strip().upper(),
                    season=csv_int(row, "season", 0),
                    episode_start=csv_int(row, "episode_start", 1),
                    expected_episodes=csv_int(row, "expected_episodes", 0),
                    expected_disc_episodes=csv_int(
                        row, "expected_disc_episodes", 0
                    ),
                    expected_title_ids=_parse_title_ids(
                        row.get("expected_title_ids", "")
                    ),
                    expected_physical_disc=csv_int(
                        row, "expected_physical_disc", 0
                    ),
                    notes=(row.get("notes") or "").strip(),
                    features=_parse_features(row.get("features", "")),
                )
                validate_queue_item(item)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"queue {path} row {row_number}: {exc}") from exc
            items.append(item)
    if not items:
        raise ValueError(f"queue {path} contains no items")
    return items


# --- State persistence ---


def default_state(items: list[QueueItem]) -> dict:
    return {
        "version": STATE_VERSION,
        "queue": [asdict(i) for i in items],
        "current_index": 0,
        "disc_index_in_item": 0,
        "disc_episode_counts": {},
        "completed": [],
        "pending_finalize": [],
        "failed_finalize": [],
        "rip_durations_s": [],
        "started_at": time.time(),
    }


def save_state(state: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)


def warn_state_location(state_path: str) -> None:
    abs_path = os.path.abspath(state_path)
    if abs_path.startswith("/Volumes/"):
        print(f"{C.YLW}Warning: state file lives on a network volume ({abs_path}).{C.R}")
        print(f"{C.YLW}  os.replace is not atomic across all macOS SMB versions.{C.R}")
        print(f"{C.YLW}  Recommended: --state ~/ripqueue-state.json{C.R}")


def warn_queue_divergence(state: dict, items: list[QueueItem]) -> None:
    sq = state.get("queue", [])
    live = [asdict(i) for i in items]
    if len(sq) != len(live):
        print(
            f"{C.YLW}Warning: queue.csv has {len(live)} rows, state has {len(sq)}. "
            f"Using state's snapshot. Delete the state file to start fresh.{C.R}"
        )
        return
    diffs = []
    for i, (s, lv) in enumerate(zip(sq, live)):
        delta = [k for k in lv if s.get(k) != lv.get(k)]
        if delta:
            diffs.append((i + 1, s.get("title"), delta))
    if diffs:
        print(
            f"{C.YLW}Warning: queue.csv differs from state snapshot in "
            f"{len(diffs)} row(s):{C.R}"
        )
        for n, title, delta in diffs[:5]:
            print(f"{C.YLW}  row {n} ({title}): {','.join(delta)} (state wins){C.R}")
        print(
            f"{C.YLW}  Edit ripqueue-state.json or delete it to apply queue.csv changes.{C.R}"
        )


def _reconcile_pending_finalize(state: dict) -> list[dict]:
    """Drop already-completed pending entries and rewind cursor to earliest unresolved pending item."""
    raw_pending = state.get("pending_finalize")
    if not isinstance(raw_pending, list):
        state["pending_finalize"] = []
        return []
    queue = state.get("queue", [])
    completed_keys: set[tuple[str, int]] = set()
    for rec in state.get("completed", []):
        if isinstance(rec, dict) and isinstance(rec.get("item"), str):
            try:
                completed_keys.add((rec["item"], int(rec.get("disc_n", 0))))
            except (TypeError, ValueError):
                pass

    unresolved: list[dict] = []
    earliest: tuple[int, int] | None = None
    for entry in raw_pending:
        if not isinstance(entry, dict):
            continue
        title = entry.get("item")
        try:
            disc_n = int(entry.get("disc_n", 0))
        except (TypeError, ValueError):
            disc_n = 0
        if isinstance(title, str) and (title, disc_n) in completed_keys:
            continue
        unresolved.append(entry)

        item_idx = entry.get("item_index")
        if not isinstance(item_idx, int) or not (0 <= item_idx < len(queue)):
            item_idx = None
            if isinstance(title, str):
                for q_idx, q_row in enumerate(queue):
                    if isinstance(q_row, dict) and q_row.get("title") == title:
                        item_idx = q_idx
                        break
        if item_idx is None:
            continue

        disc_idx = entry.get("disc_index_in_item")
        if not isinstance(disc_idx, int) or disc_idx < 0:
            q_disc_n = entry.get("queue_disc_n", entry.get("disc_n", 1))
            try:
                disc_idx = max(0, int(q_disc_n) - 1)
            except (TypeError, ValueError):
                disc_idx = 0
        pos = (item_idx, disc_idx)
        if earliest is None or pos < earliest:
            earliest = pos

    state["pending_finalize"] = unresolved
    cur_pos = (
        int(state.get("current_index", 0)),
        int(state.get("disc_index_in_item", 0) or 0),
    )
    if earliest is not None and cur_pos > earliest:
        state["current_index"] = earliest[0]
        state["disc_index_in_item"] = earliest[1]
        print(
            f"{C.YLW}Warning: {len(unresolved)} unresolved pending_finalize entry(ies); "
            f"rewinding resume cursor to item {earliest[0] + 1}, "
            f"disc {earliest[1] + 1}.{C.R}"
        )
    return unresolved


def load_or_init(args, items: list[QueueItem]) -> dict:
    p = Path(args.state)
    if p.exists():
        raw = p.read_text(encoding="utf-8")
        if not raw.strip():
            print(
                f"{C.CYA}New run{C.R}: state file {p} is empty; "
                f"loaded {len(items)} queue items."
            )
            return default_state(items)
        try:
            state = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"state file {p} is not valid JSON at line {exc.lineno}, "
                f"column {exc.colno}; refusing to overwrite resume data"
            ) from exc
        if not isinstance(state, dict):
            raise ValueError(f"state file {p} must contain a JSON object")
        for key, expected_type in (
            ("queue", list),
            ("current_index", int),
            ("completed", list),
        ):
            if key not in state or not isinstance(state[key], expected_type):
                raise ValueError(f"state file {p} has invalid or missing {key!r}")
        state.setdefault("disc_index_in_item", 0)
        state.setdefault("disc_episode_counts", {})
        state.setdefault("pending_finalize", [])
        state.setdefault("failed_finalize", [])
        state.pop("next_episode", None)
        unresolved = _reconcile_pending_finalize(state)
        prior_done = (
            state.get("current_index", 0) >= len(state.get("queue", []))
            and not unresolved
        )
        same_queue = state.get("queue", []) == [asdict(i) for i in items]
        if prior_done and not same_queue:
            print(
                f"{C.YLW}Prior state was fully complete and queue.csv changed; "
                f"starting fresh.{C.R}"
            )
            return default_state(items)
        print(
            f"{C.CYA}Resumed{C.R}: position {state['current_index'] + 1}/{len(state['queue'])}, "
            f"{len(state['completed'])} rips already done."
        )
        warn_queue_divergence(state, items)
        return state
    print(f"{C.CYA}New run{C.R}: loaded {len(items)} queue items.")
    return default_state(items)
