"""burndvd — decoupled macOS DVD/Blu-ray/UHD ripping, verification, fallback & OCR library."""

from __future__ import annotations

from burndvd.cli.checkrip import active_rips, analyze, inspect_rips, scan
from burndvd.drive import check_key_expiry, disc_present, eject, respin_drive
from burndvd.fallback import (
    can_use_mkvmerge_fallback,
    discover_bdmv_playlists,
    mkvmerge_fallback_rip,
    remux_playlists_with_mkvmerge,
)
from burndvd.models import (
    FallbackResult,
    QueueItem,
    RipResult,
    TitleInfo,
    TrackInfo,
    load_queue,
    validate_queue_item,
)
from burndvd.nas import move_with_progress_noclobber, resolve_nas_path, season_dir_lock
from burndvd.probe import (
    disc_content_fingerprint,
    disc_label,
    probe_disc,
    titles_from_info,
)
from burndvd.receipts import append_disc_receipt, disc_receipt_seen
from burndvd.ripper import rip_disc, rip_double_feature, verify
from burndvd.selection import (
    episode_bonus_split,
    planned_rip_selectors,
    play_all_title_ids,
    select_titles,
    split_chaptered_playall,
    staging_space_requirement,
    verify_play_all,
)
from burndvd.subocr import ocr_mkv, parse_sup, probe_tracks, write_srt

__version__ = "2.0.0"

__all__ = [
    "__version__",
    "FallbackResult",
    "QueueItem",
    "RipResult",
    "TitleInfo",
    "TrackInfo",
    "active_rips",
    "analyze",
    "append_disc_receipt",
    "can_use_mkvmerge_fallback",
    "check_key_expiry",
    "disc_content_fingerprint",
    "disc_label",
    "disc_present",
    "disc_receipt_seen",
    "discover_bdmv_playlists",
    "eject",
    "episode_bonus_split",
    "inspect_rips",
    "load_queue",
    "mkvmerge_fallback_rip",
    "move_with_progress_noclobber",
    "ocr_mkv",
    "parse_sup",
    "planned_rip_selectors",
    "play_all_title_ids",
    "probe_disc",
    "probe_tracks",
    "remux_playlists_with_mkvmerge",
    "resolve_nas_path",
    "respin_drive",
    "rip_disc",
    "rip_double_feature",
    "scan",
    "season_dir_lock",
    "select_titles",
    "split_chaptered_playall",
    "staging_space_requirement",
    "titles_from_info",
    "validate_queue_item",
    "verify",
    "verify_play_all",
    "write_srt",
]
