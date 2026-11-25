"""Configuration, binary resolution, environment overrides, and formatting helpers."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

# Ensure Homebrew / local tools are discoverable under non-interactive shells.
HOMEBREW_PATHS = ("/opt/homebrew/bin", "/usr/local/bin")


def ensure_tool_path() -> None:
    """Prepend standard Homebrew bin directories to PATH if missing."""
    cur = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    prefix = [p for p in HOMEBREW_PATHS if p not in cur]
    if prefix:
        os.environ["PATH"] = os.pathsep.join(prefix + cur)


def resolve_binary(name: str, *fallbacks: str, env_var: str | None = None) -> str:
    """Resolve an external binary via env override, PATH, or absolute fallbacks."""
    if env_var:
        override = os.environ.get(env_var, "").strip()
        if override:
            return override
    found = shutil.which(name)
    if found:
        return found
    for candidate in fallbacks:
        if os.path.isfile(candidate):
            return candidate
    return fallbacks[0] if fallbacks else name


# --- External binaries ---
DEFAULT_MAKEMKVCON = resolve_binary(
    "makemkvcon",
    "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon",
    env_var="BURNDVD_MAKEMKVCON",
)
DISKUTIL_BIN = os.environ.get("BURNDVD_DISKUTIL", "/usr/sbin/diskutil")
MOUNT_BIN = os.environ.get("BURNDVD_MOUNT", "/sbin/mount")
DRUTIL_BIN = os.environ.get("BURNDVD_DRUTIL", "drutil")
FFPROBE_BIN = resolve_binary(
    "ffprobe",
    "/opt/homebrew/bin/ffprobe",
    "/usr/local/bin/ffprobe",
    env_var="BURNDVD_FFPROBE",
)
FFMPEG_BIN = resolve_binary(
    "ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    env_var="BURNDVD_FFMPEG",
)
MKVMERGE_BIN = resolve_binary(
    "mkvmerge",
    "/opt/homebrew/bin/mkvmerge",
    "/usr/local/bin/mkvmerge",
    env_var="BURNDVD_MKVMERGE",
)
MKVEXTRACT_BIN = resolve_binary(
    "mkvextract",
    "/opt/homebrew/bin/mkvextract",
    "/usr/local/bin/mkvextract",
    env_var="BURNDVD_MKVEXTRACT",
)
MKVPROPEDIT_BIN = resolve_binary(
    "mkvpropedit",
    "/opt/homebrew/bin/mkvpropedit",
    "/usr/local/bin/mkvpropedit",
    env_var="BURNDVD_MKVPROPEDIT",
)

# --- Media roots & local runtime paths ---
SMB_MEDIA_PREFIX = os.environ.get("BURNDVD_MEDIA_ROOT", "/Volumes/Media")
NFS_MEDIA_PREFIX = os.environ.get("BURNDVD_NFS_MEDIA_ROOT", "/private/nas/media")
MEDIA_SUBDIRS = ("TV Shows", "TV Shows 4K", "Movies", "Movies 4K")

LOCAL_STAGING_BASE = Path(
    os.environ.get(
        "BURNDVD_STAGING_DIR",
        str(Path.home() / ".cache" / "burndvd" / "staging"),
    )
)
LOCAL_LOCK_BASE = Path(
    os.environ.get(
        "BURNDVD_LOCK_DIR",
        str(Path.home() / ".cache" / "burndvd" / "locks"),
    )
)
DISC_RECEIPT_DEFAULT = Path(
    os.environ.get(
        "BURNDVD_RECEIPTS_PATH",
        str(Path.home() / ".local" / "state" / "burndvd" / "disc-receipts.jsonl"),
    )
)
SUBOCR_CONFIG_PATH = Path(
    os.environ.get(
        "BURNDVD_SUBOCR_CONFIG",
        str(Path.home() / ".config" / "subocr" / "shows.json"),
    )
)
SUBOCR_LOCK_PATH = Path(
    os.environ.get(
        "BURNDVD_SUBOCR_LOCK",
        str(Path.home() / ".cache" / "burndvd" / "subocr.flock"),
    )
)

# --- Ripping, probing, and selection thresholds ---
STAGING_GROWTH_FACTOR = 1.05
STAGING_HEADROOM_GB = 8.0
PARALLEL_RIP_RESERVE_GB = 110.0

MAX_NONINTERACTIVE_RETRIES = 1
NONINTERACTIVE_RETRY_DELAY_S = 5
PHYSICAL_PROBE_RETRY_DELAY_S = 30

PROBE_STALL_S = 60
PROBE_HARD_S = 300
PROBE_COOLDOWN_S = 20
RESPIN_AFTER_S = 150
MAX_RESPINS = 3

MOVIE_ALT_CUT_MIN_RATIO = 0.85
MOVIE_FEATURE_MIN_S = 2700
TV_MIN_DUR_S = 600
EXTRAS_MIN_DUR_S = 120
EP_BONUS_MEDIAN_RATIO = 0.4
EP_BONUS_MIN_COUNT = 3

PLAY_ALL_SUM_TOL = 0.08
PLAY_ALL_MIN_RATIO = 1.8
PLAY_ALL_MIN_PARTS = 2
PLAY_ALL_BYTE_TOL = 0.25
PLAY_ALL_VERIFY_OFFSETS = (120.0, 300.0, 600.0)

ALT_DEDUP_DUR_TOL_S = 3
ALT_DEDUP_OFFSETS = (180.0, 600.0, 1200.0, 1800.0, 2400.0)

PLAYALL_MIN_TOTAL_S = 70 * 60
PLAYALL_EP_MIN_S = 15 * 60
PLAYALL_EP_MAX_S = 45 * 60
PLAYALL_SPREAD_TOL_S = 300.0
PLAYALL_MAX_STRIDE = 12
PLAYALL_MIN_SHAPE_RATIO = 2.0

STATE_VERSION = 3


class C:
    """ANSI color escape codes for console output."""

    R = "\033[0m"
    B = "\033[1m"
    D = "\033[2m"
    RED = "\033[31m"
    GRN = "\033[32m"
    YLW = "\033[33m"
    BLU = "\033[34m"
    MAG = "\033[35m"
    CYA = "\033[36m"


def fmt_dur(s: float) -> str:
    """Format seconds into compact human duration (e.g. '1h23m' or '24m10s')."""
    if s <= 0:
        return "--:--"
    total = int(s)
    h = total // 3600
    m = (total % 3600) // 60
    ss = total % 60
    return f"{h}h{m:02d}m" if h else f"{m}m{ss:02d}s"


def render_bar(frac: float, width: int = 30) -> str:
    """Render an ASCII progress bar for a fraction in [0, 1]."""
    n = max(0, min(width, int(frac * width)))
    return "[" + "#" * n + "-" * (width - n) + "]"


def sanitize(s: str) -> str:
    """Remove filesystem-unsafe characters and normalize whitespace."""
    cleaned = re.sub(r"[\\/:\*\?\"<>\|]", "", s)
    return re.sub(r"\s+", " ", cleaned).strip()
