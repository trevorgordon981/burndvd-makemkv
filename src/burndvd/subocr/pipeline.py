"""End-to-end PGS/VobSub-to-SRT OCR pipeline and kernel-managed flock serialization."""

from __future__ import annotations

import fcntl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from burndvd.config import C, SUBOCR_LOCK_PATH
from burndvd.models import TrackInfo
from burndvd.subocr.pgs import parse_sup
from burndvd.subocr.srt import write_srt
from burndvd.subocr.tracks import (
    LANG_639_1,
    VISION_LANG,
    _to_iso639_2,
    load_config,
    pick_audio_default,
    pick_sub_track,
    probe_tracks,
    set_track_defaults,
    show_for_path,
    show_settings,
)
from burndvd.subocr.vision import ocr_image
from burndvd.subocr.vobsub import parse_vobsub

LOCK_PATH = SUBOCR_LOCK_PATH
_DEDUP_GAP_MS = 150


def acquire_lock(lock_path: Path = LOCK_PATH):
    """Acquire an exclusive kernel-managed flock (auto-released on exit or SIGKILL)."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


def find_mkvs(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for s in paths:
        p = Path(s)
        if p.is_dir():
            out += sorted(p.rglob("*.mkv"))
        elif p.is_file() and p.suffix.lower() == ".mkv":
            out.append(p)
        else:
            print(
                f"{C.YLW}subocr: skipping {s} (not a .mkv or dir){C.R}",
                file=sys.stderr,
            )
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in out:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)
    return uniq


def _ocr_frames_to_srt(
    frames: list[tuple[int, int, object]],
    srt_out: Path,
    sub_lang: str,
    *,
    forced_only: bool = False,
    source_label: str = "SUP",
) -> bool:
    """Shared Vision OCR, consecutive-duplicate merging, and atomic `.srt` writer."""
    if not frames:
        if forced_only:
            print(
                f"{C.D}  no forced subtitle frames in {source_label}; skipping {srt_out.name}{C.R}"
            )
            return True
        print(f"{C.YLW}  no subtitle frames decoded from {source_label}{C.R}")
        return False

    print(f"  decoded {len(frames)} frame(s); running Vision OCR...")
    cues: list[tuple[int, int, str]] = []
    lang_tag = VISION_LANG.get(_to_iso639_2(sub_lang), "en-US")
    t0 = time.time()
    for i, (start, end, img) in enumerate(frames):
        text = ocr_image(img, lang=lang_tag)
        if text:
            if cues and cues[-1][2] == text and start <= cues[-1][1] + _DEDUP_GAP_MS:
                prev_start, prev_end, prev_text = cues[-1]
                cues[-1] = (prev_start, max(prev_end, end), prev_text)
            else:
                cues.append((start, end, text))
        if (i + 1) % 25 == 0 or i + 1 == len(frames):
            rate = (i + 1) / max(0.001, time.time() - t0)
            print(
                f"  ocr {i + 1}/{len(frames)}  ({rate:.1f}/s)",
                end="\r",
                flush=True,
            )
    print()
    if not cues:
        print(
            f"{C.YLW}  0 cues recognized across {len(frames)} frame(s); "
            f"not writing {srt_out.name}{C.R}"
        )
        return False
    write_srt(cues, srt_out)
    print(f"{C.GRN}  wrote {srt_out.name}  ({len(cues)} cues){C.R}")
    return True


def ocr_pgs_track(
    mkv: Path,
    track: TrackInfo,
    srt_out: Path,
    sub_lang: str,
    forced_only: bool = False,
) -> bool:
    """Extract one PGS track via `mkvextract`, decode bitmaps, OCR, and write SRT."""
    with tempfile.TemporaryDirectory(prefix="subocr-") as td:
        sup = Path(td) / f"track{track.index}.sup"
        r = subprocess.run(
            ["mkvextract", "tracks", str(mkv), f"{track.index}:{sup}"],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0 or not sup.exists():
            err_msg = (r.stderr or r.stdout).strip()[:200]
            print(
                f"{C.RED}  mkvextract failed for track {track.index}: {err_msg}{C.R}",
                file=sys.stderr,
            )
            return False
        mode_tag = " (forced-only)" if forced_only else ""
        print(f"  extracted {sup.stat().st_size / 1e6:.1f}MB SUP; parsing{mode_tag}...")
        try:
            frames = parse_sup(sup, forced_only=forced_only)
        except Exception as e:
            print(f"{C.RED}  parse_sup error: {e}{C.R}", file=sys.stderr)
            return False
        return _ocr_frames_to_srt(
            frames,
            srt_out,
            sub_lang,
            forced_only=forced_only,
            source_label="SUP",
        )


def ocr_vobsub_track(
    mkv: Path,
    track: TrackInfo,
    srt_out: Path,
    sub_lang: str,
    forced_only: bool = False,
) -> bool:
    """Extract one DVD VobSub track via `mkvextract`, decode SPU bitmaps, OCR, and write SRT."""
    with tempfile.TemporaryDirectory(prefix="subocr-") as td:
        idx = Path(td) / f"track{track.index}.idx"
        r = subprocess.run(
            ["mkvextract", "tracks", str(mkv), f"{track.index}:{idx}"],
            capture_output=True,
            text=True,
        )
        sub = idx.with_suffix(".sub")
        if r.returncode != 0 or not idx.exists() or not sub.exists():
            err_msg = (r.stderr or r.stdout).strip()[:200]
            print(
                f"{C.RED}  mkvextract failed for VobSub track {track.index}: {err_msg}{C.R}",
                file=sys.stderr,
            )
            return False
        mode_tag = " (forced-only)" if forced_only else ""
        print(f"  extracted {sub.stat().st_size / 1e6:.1f}MB VobSub; parsing{mode_tag}...")
        try:
            frames = parse_vobsub(idx, sub, forced_only=forced_only, lang=sub_lang)
        except Exception as e:
            print(f"{C.RED}  parse_vobsub error: {e}{C.R}", file=sys.stderr)
            return False
        return _ocr_frames_to_srt(
            frames,
            srt_out,
            sub_lang,
            forced_only=forced_only,
            source_label="VobSub",
        )


def ocr_sub_track(
    mkv: Path,
    track: TrackInfo,
    srt_out: Path,
    sub_lang: str,
    forced_only: bool = False,
) -> bool:
    """Dispatch OCR for either a Blu-ray PGS or DVD VobSub subtitle track."""
    if track.codec == "dvd_subtitle":
        return ocr_vobsub_track(mkv, track, srt_out, sub_lang, forced_only=forced_only)
    return ocr_pgs_track(mkv, track, srt_out, sub_lang, forced_only=forced_only)


def process_mkv(mkv: Path, settings: dict, args) -> bool:
    """Process one `.mkv`: OCR configured PGS/VobSub tracks and update audio default flag."""
    show = getattr(args, "show", None) or show_for_path(mkv) or "<unknown>"
    audio_lang = _to_iso639_2(settings["audio"])
    sub_mode = settings["sub_track"]
    sub_langs = [_to_iso639_2(x) for x in settings["sub_langs"]]

    langs_str = "+".join(sub_langs) if sub_mode != "none" else "none"
    print(
        f"{C.B}{mkv.name}{C.R}  show={show}  audio={audio_lang}  subs={sub_mode}:{langs_str}"
    )

    audio_streams, sub_streams = probe_tracks(mkv)

    audio_pick = pick_audio_default(audio_streams, audio_lang)
    if audio_pick is None and audio_lang != "eng":
        # By convention, never silently fall back to English if Japanese audio was
        # requested but absent; preserve existing audio defaults untouched.
        print(
            f"{C.YLW}  no {audio_lang} audio track; leaving audio defaults alone{C.R}"
        )

    any_error = False
    if sub_mode != "none":
        for lang in sub_langs:
            srt_out = mkv.with_suffix(f".{LANG_639_1.get(lang, lang[:2])}.srt")
            if srt_out.exists() and srt_out.stat().st_size > 0 and not args.overwrite:
                print(f"{C.D}  {srt_out.name} exists; skipping{C.R}")
                continue
            if srt_out.exists() and srt_out.stat().st_size == 0 and not args.overwrite:
                print(f"{C.YLW}  {srt_out.name} is 0 bytes; re-OCRing{C.R}")
            sub_pick = pick_sub_track(sub_streams, lang, sub_mode, mkv)
            if sub_pick is None:
                print(f"{C.D}  no PGS/VobSub {lang} subs; skipping{C.R}")
                continue
            if args.dry_run:
                fmt = "VobSub" if sub_pick.codec == "dvd_subtitle" else "PGS"
                mode_note = ", forced-only filter" if sub_pick.forced_only else ""
                print(
                    f"  would OCR track {sub_pick.index} ({lang}, {fmt}{mode_note}) -> {srt_out.name}"
                )
                continue
            if sub_pick.codec == "dvd_subtitle":
                ok = ocr_vobsub_track(
                    mkv, sub_pick, srt_out, lang, forced_only=sub_pick.forced_only
                )
            else:
                ok = ocr_pgs_track(
                    mkv, sub_pick, srt_out, lang, forced_only=sub_pick.forced_only
                )
            if not ok:
                any_error = True

    if not args.no_propedit:
        if not set_track_defaults(mkv, audio_pick, None, dry_run=args.dry_run):
            any_error = True
    return not any_error


def ocr_mkv(
    mkv: str | Path,
    *,
    show: str | None = None,
    audio: str | None = None,
    sub_mode: str | None = None,
    sub_langs: list[str] | None = None,
    overwrite: bool = False,
    no_propedit: bool = False,
    dry_run: bool = False,
) -> bool:
    """High-level library helper to OCR PGS or VobSub subtitles from a single `.mkv` file."""
    path = Path(mkv)
    cfg = load_config()
    resolved_show = show or show_for_path(path) or "_default"
    try:
        audio_streams, _ = probe_tracks(path)
        audio_langs = {a.lang for a in audio_streams}
    except Exception:
        audio_langs = set()
    settings = show_settings(cfg, resolved_show, audio_langs)
    if audio:
        settings["audio"] = _to_iso639_2(audio)
    if sub_mode:
        settings["sub_track"] = sub_mode
    if sub_langs is not None:
        settings["sub_langs"] = [_to_iso639_2(x) for x in sub_langs]
    args = SimpleNamespace(
        show=show,
        overwrite=overwrite,
        no_propedit=no_propedit,
        dry_run=dry_run,
    )
    return process_mkv(path, settings, args)
