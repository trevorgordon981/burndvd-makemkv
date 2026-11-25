"""Automatic mkvmerge unencrypted-BD fallback engine.

When MakeMKV fails on an unencrypted Blu-ray (e.g. interleaved TrueHD/AC-3 sync
errors or stalls), this module fails closed against any AACS-encrypted disc,
discovers clean BDMV playlists (`BDMV/PLAYLIST/*.mpls`) while filtering out
Play-All compilations and duplicates, remuxes directly via `mkvmerge`
(tolerating rc == 1 non-fatal mux warnings and failing closed on rc >= 2), and
verifies every output `.mkv` with `ffprobe`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from burndvd.config import C, MKVMERGE_BIN, sanitize
from burndvd.drive import (
    _drive_idx_from_args,
    _drutil_disk_node,
    mount_optical_readonly,
    optical_mount_point,
    unmount_blocking_volume,
)
from burndvd.models import FallbackResult, QueueItem
from burndvd.probe import ffprobe_streams
from burndvd.selection import episode_bonus_split, play_all_title_ids


def _format_hms(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"


def can_use_mkvmerge_fallback(mount_path: Path) -> tuple[bool, str]:
    """Fail-closed eligibility check for unencrypted BDMV fallback.

    Returns `(True, reason)` ONLY when:
      1. `mount_path` is an existing directory,
      2. `BDMV/PLAYLIST` exists and holds at least one `.mpls` file, AND
      3. No `AACS` directory/entry exists at the disc root (case-insensitive).
    """
    root = Path(mount_path)
    if not root.is_dir():
        return False, f"mount path does not exist or is not a directory: {root}"

    try:
        top_entries = list(root.iterdir())
    except OSError as exc:
        return False, f"cannot inspect disc root {root}: {exc}"

    for entry in top_entries:
        if entry.name.casefold() == "aacs":
            return False, f"AACS encryption directory present ({entry}); refusing mkvmerge fallback"

    playlist_dir = root / "BDMV" / "PLAYLIST"
    if not playlist_dir.is_dir():
        return False, f"BDMV/PLAYLIST directory missing under {root}"

    mpls_files = sorted(
        p for p in playlist_dir.iterdir()
        if p.is_file() and p.suffix.casefold() == ".mpls"
    )
    if not mpls_files:
        return False, f"no .mpls playlists found in {playlist_dir}"

    return True, f"unencrypted BDMV detected ({len(mpls_files)} playlist(s))"


def _probe_mpls_mkvmerge(mpls: Path, mkvmerge_bin: str) -> dict | None:
    """Inspect one `.mpls` file via `mkvmerge -J`."""
    try:
        r = subprocess.run(
            [mkvmerge_bin, "-J", str(mpls)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode not in (0, 1) or not (r.stdout or "").strip():
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None

    container = data.get("container") or {}
    props = container.get("properties") or {}
    tracks = data.get("tracks") or []

    duration_s = 0.0
    playlist_dur_ns = props.get("playlist_duration")
    if isinstance(playlist_dur_ns, (int, float)) and playlist_dur_ns > 0:
        duration_s = float(playlist_dur_ns) / 1e9
    else:
        for tr in tracks:
            tprops = tr.get("properties") or {}
            tag_dur = tprops.get("tag_duration")
            if isinstance(tag_dur, str) and ":" in tag_dur:
                # e.g. "00:24:12.123000000"
                parts = tag_dur.split(":")
                if len(parts) == 3:
                    try:
                        duration_s = max(
                            duration_s,
                            int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2]),
                        )
                    except ValueError:
                        pass

    size_bytes = int(props.get("playlist_size") or 0)
    has_video = any(tr.get("type") == "video" for tr in tracks)
    has_audio = any(tr.get("type") == "audio" for tr in tracks)
    raw_files = props.get("playlist_file") or []
    stream_files = [Path(str(f)).name for f in raw_files if f]

    return {
        "mpls_path": mpls,
        "playlist_id": mpls.stem,
        "duration_s": duration_s,
        "size_bytes": size_bytes,
        "has_video": has_video,
        "has_audio": has_audio,
        "stream_files": stream_files,
    }


def _probe_mpls_ffprobe(mpls: Path) -> dict | None:
    """Fallback `.mpls` probe using `ffprobe` when `mkvmerge -J` lacks duration."""
    dur_s, streams = ffprobe_streams(str(mpls))
    if dur_s <= 0:
        return None
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return {
        "mpls_path": mpls,
        "playlist_id": mpls.stem,
        "duration_s": dur_s,
        "size_bytes": 0,
        "has_video": has_video,
        "has_audio": has_audio,
        "stream_files": [],
    }


def discover_bdmv_playlists(
    mount_path: Path,
    min_dur_s: int = 900,
    *,
    item_type: str = "tv-season",
    mkvmerge_bin: str | None = None,
) -> list[dict]:
    """Discover clean BDMV playlists under `mount_path`, excluding play-alls & duplicates.

    - Inspects `BDMV/PLAYLIST/*.mpls` using `mkvmerge -J` (with `ffprobe` fallback).
    - Filters to playlists with `duration_s >= min_dur_s` and a video stream.
    - Deduplicates playlists sharing identical underlying `.m2ts` `stream_files`.
    - For TV seasons, removes Play-All compilations via both structural `.m2ts`
      superset matching and `play_all_title_ids()` duration matching, then keeps
      the primary episode cluster via `episode_bonus_split()`.
    - For movies, returns the single longest valid feature playlist.
    """
    ok, _reason = can_use_mkvmerge_fallback(mount_path)
    if not ok:
        return []

    mkvm = mkvmerge_bin or MKVMERGE_BIN
    playlist_dir = Path(mount_path) / "BDMV" / "PLAYLIST"
    mpls_files = sorted(
        p for p in playlist_dir.iterdir()
        if p.is_file() and p.suffix.casefold() == ".mpls"
    )

    candidates: list[dict] = []
    seen_streams: set[tuple[str, ...]] = set()
    for mpls in mpls_files:
        entry = _probe_mpls_mkvmerge(mpls, mkvm)
        if entry is None or entry["duration_s"] <= 0:
            ff_entry = _probe_mpls_ffprobe(mpls)
            if ff_entry is not None:
                if entry is None:
                    entry = ff_entry
                else:
                    entry["duration_s"] = ff_entry["duration_s"]
                    entry["has_video"] = entry["has_video"] or ff_entry["has_video"]
                    entry["has_audio"] = entry["has_audio"] or ff_entry["has_audio"]
        if entry is None or entry["duration_s"] < min_dur_s or not entry["has_video"]:
            continue

        # Deduplicate playlists pointing to the exact same sequence of M2TS clips.
        if entry["stream_files"]:
            sig = tuple(entry["stream_files"])
            if sig in seen_streams:
                continue
            seen_streams.add(sig)
        candidates.append(entry)

    if not candidates:
        return []

    if item_type == "movie":
        longest = max(candidates, key=lambda c: (c["duration_s"], c["size_bytes"]))
        return [longest]

    # 1. Structural Play-All filter: any playlist whose M2TS set contains 2+
    #    other distinct playlists' M2TS sets is unambiguously a Play-All.
    structural_playall: set[int] = set()
    for i, ci in enumerate(candidates):
        si = set(ci["stream_files"])
        if len(si) < 2:
            continue
        contained = 0
        for j, cj in enumerate(candidates):
            if i == j:
                continue
            sj = set(cj["stream_files"])
            if sj and sj < si:
                contained += 1
        if contained >= 2:
            structural_playall.add(i)

    # 2. Duration Play-All filter (reuses canonical play_all_title_ids heuristic).
    synth_info = {
        "titles": {
            idx: {9: _format_hms(c["duration_s"]), 11: str(c["size_bytes"])}
            for idx, c in enumerate(candidates)
            if idx not in structural_playall
        }
    }
    duration_playall = play_all_title_ids(synth_info)
    excluded = structural_playall | duration_playall

    remaining = [
        (idx, c) for idx, c in enumerate(candidates) if idx not in excluded
    ]
    if not remaining:
        return []

    # 3. Duration clustering: demote short outliers relative to median episode runtime.
    keep_ids, _bonus_ids, _thr = episode_bonus_split(
        [(idx, int(round(c["duration_s"]))) for idx, c in remaining]
    )
    keep_set = set(keep_ids)
    return [c for idx, c in remaining if idx in keep_set]


def verify_remuxed_mkv(path: Path) -> tuple[bool, str]:
    """Verify a remuxed `.mkv` is non-empty, has positive duration, and has video+audio."""
    if not path.is_file():
        return False, f"output file missing: {path}"
    try:
        size = path.stat().st_size
    except OSError as exc:
        return False, f"cannot stat output {path}: {exc}"
    if size <= 0:
        return False, f"output file is 0 bytes: {path}"

    dur_s, streams = ffprobe_streams(str(path))
    if dur_s <= 0:
        return False, f"ffprobe reported invalid duration ({dur_s}s) for {path.name}"
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if not has_video:
        return False, f"no valid video stream in {path.name}"
    if not has_audio:
        return False, f"no valid audio stream in {path.name}"
    return True, f"{dur_s:.1f}s, {size} bytes"


def remux_playlists_with_mkvmerge(
    playlists: list[dict],
    staging_dir: Path,
    title_prefix: str,
    *,
    mkvmerge_bin: str | None = None,
) -> FallbackResult:
    """Remux discovered BDMV playlists directly into `staging_dir` via `mkvmerge`.

    - Tolerates `rc == 0` and `rc == 1` (non-fatal interleaved TrueHD/AC-3 warnings).
    - Fails closed on `rc >= 2`, process errors, or `ffprobe` verification failures.
    """
    if not playlists:
        return FallbackResult(ok=False, error="no playlists provided for mkvmerge fallback")

    mkvm = mkvmerge_bin or MKVMERGE_BIN
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    safe_prefix = sanitize(title_prefix) or "bdmv"

    out_files: list[Path] = []
    file_playlists: dict[str, str] = {}
    warnings: list[str] = []

    for idx, pl in enumerate(playlists):
        mpls_path = Path(pl["mpls_path"])
        out_path = staging / f"{safe_prefix}_t{idx:02d}.mkv"
        cmd = [mkvm, "-o", str(out_path), str(mpls_path)]
        print(
            f"{C.D}  mkvmerge fallback [{idx + 1}/{len(playlists)}]: "
            f"{mpls_path.name} -> {out_path.name}{C.R}",
            flush=True,
        )
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as exc:
            return FallbackResult(
                ok=False,
                files=out_files,
                warnings=warnings,
                error=f"mkvmerge failed to launch for {mpls_path.name}: {exc}",
                playlists_remuxed=len(out_files),
            )

        if r.returncode == 1:
            warn_text = (r.stdout or r.stderr or "").strip()
            msg = (
                f"{mpls_path.name}: mkvmerge exited 1 (non-fatal warnings): "
                f"{warn_text[:240]}"
            )
            warnings.append(msg)
            print(f"{C.YLW}  {msg}{C.R}", flush=True)
        elif r.returncode >= 2 or r.returncode < 0:
            err_text = (r.stderr or r.stdout or "").strip()[:300]
            try:
                if out_path.exists():
                    out_path.unlink()
            except OSError:
                pass
            return FallbackResult(
                ok=False,
                files=out_files,
                warnings=warnings,
                error=(
                    f"mkvmerge fatal exit {r.returncode} on {mpls_path.name}: "
                    f"{err_text}"
                ),
                playlists_remuxed=len(out_files),
            )

        v_ok, v_detail = verify_remuxed_mkv(out_path)
        if not v_ok:
            try:
                if out_path.exists():
                    out_path.unlink()
            except OSError:
                pass
            return FallbackResult(
                ok=False,
                files=out_files,
                warnings=warnings,
                error=f"ffprobe verification failed for {out_path.name}: {v_detail}",
                playlists_remuxed=len(out_files),
            )
        out_files.append(out_path)
        file_playlists[str(out_path)] = mpls_path.name

    return FallbackResult(
        ok=True,
        files=out_files,
        warnings=warnings,
        playlists_remuxed=len(out_files),
        file_playlists=file_playlists,
    )


def mkvmerge_fallback_rip(
    args,
    item: QueueItem,
    staging_dir: Path,
    *,
    mount_path: Path | None = None,
    min_dur_s: int = 900,
    mkvmerge_bin: str | None = None,
) -> FallbackResult:
    """High-level unencrypted-BD fallback entrypoint used by `ripper.py`.

    If `mount_path` is not provided, resolves the drive's `/dev/diskN` node,
    mounts it read-only if needed, checks `can_use_mkvmerge_fallback`, discovers
    clean playlists, remuxes via `mkvmerge`, verifies every output `.mkv`, and
    unmounts cleanly if this function mounted the disc.
    """
    mounted_here = False
    drive_idx = _drive_idx_from_args(args) if args is not None else None
    resolved_mount = Path(mount_path) if mount_path is not None else None

    if resolved_mount is None:
        disk_node = _drutil_disk_node(drive_idx)
        if not disk_node:
            return FallbackResult(
                ok=False, error="cannot locate optical disk node for mkvmerge fallback"
            )
        existing = optical_mount_point(disk_node)
        if existing is not None:
            resolved_mount = existing
        else:
            resolved_mount = mount_optical_readonly(disk_node)
            if resolved_mount is None:
                return FallbackResult(
                    ok=False,
                    error=f"could not mount {disk_node} read-only for mkvmerge fallback",
                )
            mounted_here = True

    try:
        eligible, reason = can_use_mkvmerge_fallback(resolved_mount)
        if not eligible:
            return FallbackResult(
                ok=False, error=reason, mount_path=resolved_mount
            )

        playlists = discover_bdmv_playlists(
            resolved_mount,
            min_dur_s=min_dur_s,
            item_type=item.type,
            mkvmerge_bin=mkvmerge_bin,
        )
        if not playlists:
            return FallbackResult(
                ok=False,
                error=f"no eligible BDMV playlists >= {min_dur_s}s under {resolved_mount}",
                mount_path=resolved_mount,
            )

        # Clean any partial MakeMKV output before remuxing into a dedicated subdir
        # or clean staging directory so downstream naming sees only verified files.
        fallback_staging = Path(staging_dir) / "mkvmerge-fallback"
        res = remux_playlists_with_mkvmerge(
            playlists,
            fallback_staging,
            item.title,
            mkvmerge_bin=mkvmerge_bin,
        )
        res.mount_path = resolved_mount
        return res
    finally:
        if mounted_here:
            unmount_blocking_volume(None, drive_idx=drive_idx)
