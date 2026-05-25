#!/usr/bin/env python3
"""
subocr — OCR HDMV PGS subtitles from .mkv files into external .srt using
macOS Apple Vision. Designed for the Jellyfin web player, which can't render
PGS natively and falls back to slow CPU burn-in transcoding.

Usage:
    subocr <file.mkv | dir>...           # backfill
    subocr --post-rip <file.mkv>...      # invoked by burndvd after each rip
    subocr --show "Mob Psycho 100" ...   # override per-show config lookup

Per-show config at ~/.config/subocr/shows.json:
    {
      "_default": {"audio": "jpn", "sub_track": "full"},
      "Dragon Ball Z": {"audio": "eng", "sub_track": "forced"},
      "Mob Psycho 100": {"audio": "jpn", "sub_track": "full"}
    }

sub_track values:
    "full"   → biggest PGS track in the picked language (dialogue)
    "forced" → smallest PGS track in the picked language (signs/songs)
    "none"   → don't OCR; just set audio default and exit
"""
from __future__ import annotations
import argparse, io, json, os, re, struct, subprocess, sys, tempfile, time
from dataclasses import dataclass, field
from pathlib import Path

# --- color codes (terse) -----------------------------------------------------
class C:
    B = "\033[1m"; D = "\033[2m"; R = "\033[0m"
    GRN = "\033[32m"; YLW = "\033[33m"; RED = "\033[31m"

# --- constants ---------------------------------------------------------------
CONFIG_PATH = Path.home() / ".config" / "subocr" / "shows.json"
# Defaults: assume Western content (eng audio + eng subs available on toggle).
# If a file has both jpn AND eng audio tracks, anime auto-detect kicks in below
# (jpn audio default; subs auto-shown via Jellyfin's foreign-audio preference).
# Per-show entries override: e.g., Dragon Ball Z is explicitly eng-dub.
# sub_langs: list of langs to OCR (3-letter ISO 639-2). Missing langs are skipped silently.
DEFAULT_CONFIG = {
    "_default": {"audio": "eng", "sub_track": "full",
                 "sub_langs": ["eng", "spa", "zho"]},
    "Dragon Ball Z": {"audio": "eng", "sub_track": "forced",
                      "sub_langs": ["eng"]},
}
# ISO 639-2 → 639-1 for SRT filename suffix (Jellyfin auto-picks `.en.srt` etc.)
LANG_639_1 = {"eng": "en", "jpn": "ja", "fra": "fr", "fre": "fr", "spa": "es",
              "deu": "de", "ger": "de", "ita": "it", "kor": "ko", "zho": "zh",
              "chi": "zh", "rus": "ru", "por": "pt"}
# Apple Vision recognition language codes (BCP-47)
VISION_LANG = {"eng": "en-US", "jpn": "ja-JP", "fra": "fr-FR", "spa": "es-ES",
               "deu": "de-DE", "ita": "it-IT", "kor": "ko-KR", "zho": "zh-Hans",
               "chi": "zh-Hans", "rus": "ru-RU", "por": "pt-BR"}
# Reverse of LANG_639_1 for user input normalization
_LANG_639_2_BY_1 = {"en": "eng", "ja": "jpn", "fr": "fra", "es": "spa",
                    "de": "deu", "it": "ita", "ko": "kor", "zh": "zho",
                    "ru": "rus", "pt": "por"}

def _to_iso639_2(code: str) -> str:
    """Normalize a 2- or 3-letter lang code to ISO 639-2 (what ffprobe reports)."""
    c = code.lower().strip()
    return _LANG_639_2_BY_1.get(c, c)

PGS_MAGIC = b"PG"
SEG_PDS = 0x14
SEG_ODS = 0x15
SEG_PCS = 0x16
SEG_WDS = 0x17
SEG_END = 0x80
PTS_HZ = 90_000  # PGS timestamps in 90 kHz ticks

# --- per-show config --------------------------------------------------------
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        return dict(DEFAULT_CONFIG)
    try:
        return json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        print(f"{C.RED}subocr: bad JSON in {CONFIG_PATH}: {e}{C.R}", file=sys.stderr)
        sys.exit(2)

def show_settings(cfg: dict, show: str, audio_langs: set[str] | None = None) -> dict:
    """Resolve per-show settings. Per-show config wins; otherwise the default,
    with anime auto-detect (jpn audio override) when no explicit entry exists."""
    default = cfg.get("_default", {"audio": "eng", "sub_track": "full",
                                   "sub_langs": ["eng"]})
    s = dict(default)
    if show in cfg:
        s.update(cfg[show])
    elif audio_langs and "jpn" in audio_langs:
        s["audio"] = "jpn"  # anime auto-detect
    # Back-compat: if config still uses singular "sub_lang", promote to list
    if "sub_langs" not in s:
        s["sub_langs"] = [s.get("sub_lang", "eng")]
    return s

# --- show detection from path ----------------------------------------------
SHOW_RE = re.compile(r"/TV Shows/([^/]+)/")

def show_for_path(p: Path) -> str | None:
    m = SHOW_RE.search(str(p))
    return m.group(1) if m else None

# --- mkv track introspection ------------------------------------------------
@dataclass
class TrackInfo:
    index: int       # ffmpeg stream index
    codec: str
    lang: str
    title: str
    n_frames_estimate: int = 0  # PGS only, from packet count
    size_bytes: int = 0         # PGS only, from track-data extraction

def probe_tracks(mkv: Path) -> tuple[list[TrackInfo], list[TrackInfo]]:
    """Return (audio_tracks, sub_tracks). Languages normalized to 3-letter ISO."""
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=index,codec_type,codec_name:stream_tags=language,title",
         "-of", "json", str(mkv)],
        text=True)
    j = json.loads(out)
    audio, subs = [], []
    for s in j.get("streams", []):
        ct = s.get("codec_type")
        if ct not in ("audio", "subtitle"): continue
        tags = s.get("tags", {}) or {}
        t = TrackInfo(
            index=s["index"],
            codec=s.get("codec_name", ""),
            lang=(tags.get("language") or "und").lower(),
            title=tags.get("title", ""),
        )
        (audio if ct == "audio" else subs).append(t)
    return audio, subs

def pick_audio_default(audio: list[TrackInfo], pref_lang: str) -> TrackInfo | None:
    cands = [a for a in audio if a.lang == pref_lang]
    if not cands and pref_lang == "jpn":
        return None  # caller will fall back; don't silently pick eng
    if not cands:
        cands = audio
    if not cands:
        return None  # file has no audio tracks at all (some extras/menus)
    # Prefer lossless (truehd > flac > dts-hd-ma) > lossy
    rank = {"truehd": 0, "flac": 0, "dts": 1, "ac3": 2, "eac3": 2, "aac": 3}
    cands.sort(key=lambda a: rank.get(a.codec, 9))
    return cands[0]

def pick_sub_track(subs: list[TrackInfo], lang: str, mode: str,
                   mkv: Path) -> TrackInfo | None:
    """Pick the PGS track to OCR. mode='full' = biggest, 'forced' = smallest."""
    cands = [s for s in subs if s.codec == "hdmv_pgs_subtitle" and s.lang == lang]
    if not cands: return None
    # Size = byte count of the track in the mkv. Approximated via mkvmerge -i + identification
    # is unreliable; the cheap way is to extract and measure. To avoid double extraction,
    # use a quick "size hint" pass with mkvextract --silent on each track to /dev/null.
    # In practice we have <=4 PGS tracks; just extract each to a temp .sup and measure.
    sized = []
    with tempfile.TemporaryDirectory(prefix="subocr-sizehint-") as td:
        for t in cands:
            sup = Path(td) / f"t{t.index}.sup"
            r = subprocess.run(
                ["mkvextract", "tracks", str(mkv), f"{t.index}:{sup}"],
                capture_output=True, text=True)
            if r.returncode != 0 or not sup.exists():
                continue
            t.size_bytes = sup.stat().st_size
            sized.append(t)
    if not sized: return None
    sized.sort(key=lambda t: t.size_bytes)
    return sized[0] if mode == "forced" else sized[-1]

# --- PGS parser -------------------------------------------------------------
@dataclass
class PgsObject:
    object_id: int
    version: int
    width: int = 0
    height: int = 0
    rle: bytes = b""  # accumulated across fragments

@dataclass
class PgsPalette:
    palette_id: int
    version: int
    # entry_id -> (R, G, B, A)
    entries: dict[int, tuple[int, int, int, int]] = field(default_factory=dict)

@dataclass
class PgsComposition:
    object_id: int
    window_id: int
    forced: bool
    x: int
    y: int
    crop: tuple[int, int, int, int] | None  # (x, y, w, h) or None

@dataclass
class PgsDisplay:
    pts_ms: int
    video_w: int
    video_h: int
    palette_id: int
    compositions: list[PgsComposition]
    # filled by renderer
    objects: dict[int, PgsObject] = field(default_factory=dict)
    palettes: dict[int, PgsPalette] = field(default_factory=dict)

def _ycrcb_to_rgb(y: int, cr: int, cb: int) -> tuple[int, int, int]:
    """BT.601 limited range YCrCb → RGB (8-bit each)."""
    yp = y - 16
    cbp = cb - 128
    crp = cr - 128
    r = 1.164 * yp + 1.596 * crp
    g = 1.164 * yp - 0.392 * cbp - 0.813 * crp
    b = 1.164 * yp + 2.017 * cbp
    return (max(0, min(255, int(r))),
            max(0, min(255, int(g))),
            max(0, min(255, int(b))))

def _decode_rle(data: bytes, width: int, height: int) -> bytes:
    """PGS RLE → flat palette-index buffer of length width*height."""
    out = bytearray(width * height)
    di = 0    # destination index
    si = 0    # source index
    x = 0     # current x on row
    y = 0     # current y
    L = len(data)
    while si < L and y < height:
        b = data[si]; si += 1
        if b != 0:
            out[di] = b; di += 1; x += 1
            continue
        # b == 0 → escape byte follows
        if si >= L: break
        b2 = data[si]; si += 1
        if b2 == 0:
            # end of line; pad remainder of row with 0 (transparent)
            pad = width - x
            if pad > 0:
                di += pad
            x = 0; y += 1
            continue
        top2 = b2 & 0xC0
        low6 = b2 & 0x3F
        if top2 == 0x00:
            run = low6
            color = 0
        elif top2 == 0x40:
            if si >= L: break
            run = (low6 << 8) | data[si]; si += 1
            color = 0
        elif top2 == 0x80:
            run = low6
            if si >= L: break
            color = data[si]; si += 1
        else:  # 0xC0
            if si + 1 >= L: break
            run = (low6 << 8) | data[si]; si += 1
            color = data[si]; si += 1
        # Truncate run that would overflow the row. PGS encoders shouldn't emit
        # these, but if they do, spilling into the next row's bytes desyncs the
        # whole bitmap. Drop the overflow; rely on EOL to wrap.
        remain = width - x
        if run > remain: run = remain
        if run > 0:
            if color == 0:
                di += run  # buffer is already zeroed
            else:
                out[di:di+run] = bytes([color]) * run
                di += run
            x += run
    return bytes(out)

def parse_sup(sup_path: Path):
    """Generator yielding (pts_ms, end_pts_ms_or_None, PIL.Image) for each
    composition (subtitle frame). end_pts_ms is filled in by the caller from
    the next clearing PCS."""
    from PIL import Image
    data = sup_path.read_bytes()
    pos = 0
    L = len(data)
    cur_objs: dict[int, PgsObject] = {}
    cur_pals: dict[int, PgsPalette] = {}
    displays: list[PgsDisplay] = []
    pending: PgsDisplay | None = None

    while pos + 13 <= L:
        if data[pos:pos+2] != PGS_MAGIC:
            # Resync — shouldn't happen on a valid file. Skip byte and continue.
            pos += 1
            continue
        pts = struct.unpack(">I", data[pos+2:pos+6])[0]
        # dts at pos+6..10 ignored
        stype = data[pos+10]
        ssize = struct.unpack(">H", data[pos+11:pos+13])[0]
        body = data[pos+13:pos+13+ssize]
        pos += 13 + ssize

        if stype == SEG_PCS:
            if len(body) < 11: continue
            video_w, video_h, _frame_rate, _comp_num, _comp_state, \
                _pal_update, palette_id, num_objs = struct.unpack(">HHBHBBBB", body[:11])
            cps: list[PgsComposition] = []
            off = 11
            for _ in range(num_objs):
                if off + 8 > len(body): break
                obj_id, win_id, flags, ox, oy = struct.unpack(">HBBHH", body[off:off+8])
                off += 8
                forced = bool(flags & 0x40)
                cropped = bool(flags & 0x80)
                crop = None
                if cropped:
                    if off + 8 > len(body): break
                    crop = struct.unpack(">HHHH", body[off:off+8])
                    off += 8
                cps.append(PgsComposition(obj_id, win_id, forced, ox, oy, crop))
            pending = PgsDisplay(
                pts_ms=pts // (PTS_HZ // 1000),
                video_w=video_w, video_h=video_h,
                palette_id=palette_id,
                compositions=cps,
            )

        elif stype == SEG_PDS:
            if len(body) < 2: continue
            pal_id, pal_ver = body[0], body[1]
            pal = PgsPalette(pal_id, pal_ver)
            off = 2
            while off + 5 <= len(body):
                eid, Y, Cr, Cb, A = body[off:off+5]
                off += 5
                r, g, b = _ycrcb_to_rgb(Y, Cr, Cb)
                pal.entries[eid] = (r, g, b, A)
            cur_pals[pal_id] = pal

        elif stype == SEG_ODS:
            if len(body) < 4: continue
            obj_id = struct.unpack(">H", body[:2])[0]
            ver = body[2]
            last_flag = body[3]
            # 0xC0 first+last (whole), 0x80 first, 0x40 last, 0x00 middle
            if last_flag & 0x80:
                # first fragment: object_data_length(3) + width(2) + height(2) + rle...
                if len(body) < 11: continue
                _odl = (body[4] << 16) | (body[5] << 8) | body[6]
                width = struct.unpack(">H", body[7:9])[0]
                height = struct.unpack(">H", body[9:11])[0]
                cur_objs[obj_id] = PgsObject(obj_id, ver, width, height, body[11:])
            else:
                # Continuation: REPLACE the obj with a new one whose .rle is the
                # concatenation. In-place `rle +=` would mutate the obj that
                # earlier END snapshots still reference, retroactively corrupting
                # them.
                prev = cur_objs.get(obj_id)
                if prev is not None:
                    cur_objs[obj_id] = PgsObject(
                        prev.object_id, prev.version, prev.width, prev.height,
                        prev.rle + body[4:],
                    )

        elif stype == SEG_END:
            if pending is None:
                continue
            # Snapshot current palette + objects for this display
            pending.palettes = {k: v for k, v in cur_pals.items()}
            pending.objects = {k: v for k, v in cur_objs.items()}
            displays.append(pending)
            pending = None

        # WDS, etc., ignored — window geometry isn't needed for OCR

    # Render each display to an RGBA PIL image and yield with start/end pts
    out = []
    for i, d in enumerate(displays):
        if not d.compositions:
            # Clearing PCS — sets end-pts of previous frame
            if out:
                out[-1] = (out[-1][0], d.pts_ms, out[-1][2])
            continue
        pal = d.palettes.get(d.palette_id)
        if pal is None: continue
        img = Image.new("RGBA", (d.video_w, d.video_h), (0, 0, 0, 0))
        any_rendered = False
        for cp in d.compositions:
            obj = d.objects.get(cp.object_id)
            if obj is None or not obj.rle or obj.width == 0:
                continue
            try:
                indexed = _decode_rle(obj.rle, obj.width, obj.height)
            except Exception:
                continue
            sub_img = Image.new("RGBA", (obj.width, obj.height), (0, 0, 0, 0))
            px = sub_img.load()
            for yy in range(obj.height):
                row_off = yy * obj.width
                for xx in range(obj.width):
                    idx = indexed[row_off + xx]
                    rgba = pal.entries.get(idx, (0, 0, 0, 0))
                    if rgba[3] > 0:
                        px[xx, yy] = rgba
            img.alpha_composite(sub_img, dest=(cp.x, cp.y))
            any_rendered = True
        if any_rendered:
            # A new rendered frame ends the previous one (PGS semantics: prior
            # composition is replaced, not overlaid). Without this, the +5000ms
            # backstop overlaps consecutive cues.
            if out and out[-1][1] is None:
                out[-1] = (out[-1][0], d.pts_ms, out[-1][2])
            # Crop to non-transparent bbox to speed up OCR
            bbox = img.getbbox()
            if bbox:
                img = img.crop(bbox)
            out.append((d.pts_ms, None, img))
        else:
            if out:
                out[-1] = (out[-1][0], d.pts_ms, out[-1][2])

    # Backstop: any frame missing an end-pts gets +5s default
    finalized = []
    for start, end, im in out:
        if end is None:
            end = start + 5000
        if end > start:
            finalized.append((start, end, im))
    return finalized

# --- Vision OCR -------------------------------------------------------------
_vision_cache = {}

def _vision_init():
    """Lazy-import pyobjc bindings so non-OCR code paths don't pay the cost."""
    if "Vision" in _vision_cache: return _vision_cache
    import Quartz, Vision
    from Cocoa import NSURL
    _vision_cache["Vision"] = Vision
    _vision_cache["Quartz"] = Quartz
    _vision_cache["NSURL"] = NSURL
    return _vision_cache

def ocr_image(img, lang: str = "en-US") -> str:
    """Run Apple Vision text recognition on a PIL.Image. Returns multi-line text."""
    v = _vision_init()
    Vision = v["Vision"]; Quartz = v["Quartz"]; NSURL = v["NSURL"]
    # Write to a temp PNG; PIL → CGImage via NSURL is the simplest reliable path
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = Path(f.name)
    try:
        # Composite over black for OCR — Vision handles transparent PNGs but
        # high-contrast composite is more reliable for thin anti-aliased subs.
        from PIL import Image
        bg = Image.new("RGB", img.size, (0, 0, 0))
        if img.mode == "RGBA":
            bg.paste(img, mask=img.split()[3])
        else:
            bg.paste(img)
        bg.save(tmp, "PNG")

        url = NSURL.fileURLWithPath_(str(tmp))
        src = Quartz.CGImageSourceCreateWithURL(url, None)
        if src is None: return ""
        cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        if cg is None: return ""

        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(1)  # accurate
        req.setUsesLanguageCorrection_(True)
        req.setRecognitionLanguages_([lang])

        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            return ""
        lines = []
        for r in (req.results() or []):
            cands = r.topCandidates_(1)
            if cands:
                lines.append(str(cands[0].string()))
        return "\n".join(lines).strip()
    finally:
        try: tmp.unlink()
        except OSError: pass

# --- SRT writer -------------------------------------------------------------
def _ms_to_srt_ts(ms: int) -> str:
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def write_srt(cues: list[tuple[int, int, str]], path: Path):
    with path.open("w", encoding="utf-8") as f:
        idx = 1
        for start, end, text in cues:
            text = text.strip()
            if not text: continue
            f.write(f"{idx}\n{_ms_to_srt_ts(start)} --> {_ms_to_srt_ts(end)}\n{text}\n\n")
            idx += 1

# --- mkvpropedit (audio + sub default flags) -------------------------------
def _mkvmerge_track_positions(mkv: Path) -> dict[int, int]:
    """Map ffmpeg stream index → 1-based positional index in mkvmerge's track
    listing. That positional is what `mkvpropedit ... --edit track:N` selects."""
    try:
        out = subprocess.check_output(["mkvmerge", "-J", str(mkv)], text=True)
        j = json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return {}
    pos_for: dict[int, int] = {}
    for i, t in enumerate(j.get("tracks", []), start=1):
        if "id" in t:
            pos_for[t["id"]] = i
    return pos_for

def set_track_defaults(mkv: Path, audio_track: TrackInfo | None,
                       sub_track: TrackInfo | None, dry_run: bool = False) -> bool:
    """Set the chosen audio/sub track as default=1; clear default on the rest."""
    audio_streams, sub_streams = probe_tracks(mkv)
    pos_for = _mkvmerge_track_positions(mkv)
    cmd = ["mkvpropedit", str(mkv)]
    for a in audio_streams:
        n = pos_for.get(a.index)
        if n is None: continue
        flag = "1" if (audio_track and a.index == audio_track.index) else "0"
        cmd += ["--edit", f"track:{n}", "--set", f"flag-default={flag}"]
    for s in sub_streams:
        n = pos_for.get(s.index)
        if n is None: continue
        flag = "1" if (sub_track and s.index == sub_track.index) else "0"
        cmd += ["--edit", f"track:{n}", "--set", f"flag-default={flag}"]
    if dry_run:
        print(f"  would run: {' '.join(cmd)}")
        return True
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"{C.YLW}  mkvpropedit failed: {r.stderr.strip()[:200]}{C.R}", file=sys.stderr)
        return False
    return True

# --- main per-file flow ------------------------------------------------------
def process_mkv(mkv: Path, settings: dict, args) -> bool:
    """Returns True on success (incl. 'nothing to do'), False on error."""
    show = show_for_path(mkv) or "<unknown>"
    audio_lang = settings["audio"]
    sub_mode = settings["sub_track"]   # full / forced / none
    sub_langs = settings["sub_langs"]  # list of 3-letter ISO 639-2 codes

    langs_str = "+".join(sub_langs) if sub_mode != "none" else "none"
    print(f"{C.B}{mkv.name}{C.R}  show={show}  audio={audio_lang}  subs={sub_mode}:{langs_str}")

    audio_streams, sub_streams = probe_tracks(mkv)

    audio_pick = pick_audio_default(audio_streams, audio_lang)
    if audio_pick is None and audio_lang != "eng":
        # Don't silently fall back to English if a non-eng track was requested
        # but missing; flag it and leave audio defaults alone.
        print(f"{C.YLW}  no {audio_lang} audio track; leaving audio defaults alone{C.R}")

    any_error = False
    if sub_mode != "none":
        for lang in sub_langs:
            srt_out = mkv.with_suffix(f".{LANG_639_1.get(lang, lang[:2])}.srt")
            if srt_out.exists() and not args.overwrite:
                print(f"{C.D}  {srt_out.name} exists; skipping{C.R}")
                continue
            sub_pick = pick_sub_track(sub_streams, lang, sub_mode, mkv)
            if sub_pick is None:
                print(f"{C.D}  no PGS {lang} subs; skipping{C.R}")
                continue
            ok = ocr_pgs_track(mkv, sub_pick, srt_out, lang)
            if not ok:
                any_error = True

    # Audio defaults inside the mkv (external SRTs handle sub selection)
    if not args.no_propedit:
        set_track_defaults(mkv, audio_pick, None, dry_run=args.dry_run)
    return not any_error

def ocr_pgs_track(mkv: Path, track: TrackInfo, srt_out: Path, sub_lang: str) -> bool:
    """Extract one PGS track, parse, OCR each frame, write SRT."""
    with tempfile.TemporaryDirectory(prefix="subocr-") as td:
        sup = Path(td) / f"track{track.index}.sup"
        r = subprocess.run(
            ["mkvextract", "tracks", str(mkv), f"{track.index}:{sup}"],
            capture_output=True, text=True)
        if r.returncode != 0 or not sup.exists():
            print(f"{C.RED}  mkvextract failed for track {track.index}: "
                  f"{r.stderr.strip()[:200]}{C.R}", file=sys.stderr)
            return False
        print(f"  extracted {sup.stat().st_size/1e6:.1f}MB SUP; parsing...")
        try:
            frames = parse_sup(sup)
        except Exception as e:
            print(f"{C.RED}  parse_sup error: {e}{C.R}", file=sys.stderr)
            return False
        if not frames:
            print(f"{C.YLW}  no subtitle frames decoded from SUP{C.R}")
            return False
        print(f"  decoded {len(frames)} frame(s); running Vision OCR...")
        cues: list[tuple[int, int, str]] = []
        lang_tag = VISION_LANG.get(sub_lang, "en-US")
        t0 = time.time()
        for i, (start, end, img) in enumerate(frames):
            text = ocr_image(img, lang=lang_tag)
            if text:
                cues.append((start, end, text))
            if (i + 1) % 25 == 0 or i + 1 == len(frames):
                rate = (i + 1) / max(0.001, time.time() - t0)
                print(f"  ocr {i+1}/{len(frames)}  ({rate:.1f}/s)", end="\r", flush=True)
        print()  # newline after the progress carriage-returns
        write_srt(cues, srt_out)
        print(f"{C.GRN}  wrote {srt_out.name}  ({len(cues)} cues){C.R}")
        return True

# --- CLI --------------------------------------------------------------------
def find_mkvs(paths: list[str]) -> list[Path]:
    out = []
    for s in paths:
        p = Path(s)
        if p.is_dir():
            out += sorted(p.rglob("*.mkv"))
        elif p.is_file() and p.suffix.lower() == ".mkv":
            out.append(p)
        else:
            print(f"{C.YLW}subocr: skipping {s} (not a .mkv or dir){C.R}", file=sys.stderr)
    # De-dup, preserve order
    seen = set(); uniq = []
    for p in out:
        if p in seen: continue
        seen.add(p); uniq.append(p)
    return uniq

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help=".mkv file or dir to sweep")
    ap.add_argument("--show", help="Override show name lookup (otherwise inferred from /TV Shows/<X>/)")
    ap.add_argument("--audio", help="Override audio lang (jpn/eng/...)")
    ap.add_argument("--sub-mode", choices=["full", "forced", "none"], help="Override sub-track mode")
    ap.add_argument("--langs", help="Comma-separated subtitle langs to OCR (e.g. eng,spa,zho). Overrides config.")
    ap.add_argument("--sub-lang", help="(deprecated) single lang — use --langs")
    ap.add_argument("--overwrite", action="store_true", help="Re-OCR even if .srt exists")
    ap.add_argument("--no-propedit", action="store_true", help="Skip mkvpropedit (don't touch the .mkv)")
    ap.add_argument("--dry-run", action="store_true", help="Print would-do, don't modify files")
    ap.add_argument("--post-rip", action="store_true", help="Invoked from burndvd; don't fail on per-file errors")
    args = ap.parse_args()

    cfg = load_config()
    mkvs = find_mkvs(args.paths)
    if not mkvs:
        print("subocr: no .mkv files found", file=sys.stderr); sys.exit(1)

    failed = 0
    for mkv in mkvs:
        show = args.show or show_for_path(mkv) or "_default"
        # Peek at audio langs so the resolver can auto-detect anime
        try:
            audio_streams, _ = probe_tracks(mkv)
            audio_langs = {a.lang for a in audio_streams}
        except Exception:
            audio_langs = set()
        settings = show_settings(cfg, show, audio_langs)
        if args.audio: settings["audio"] = args.audio
        if args.sub_mode: settings["sub_track"] = args.sub_mode
        # Lang normalization: accept either 2- or 3-letter; map to 3-letter for matching
        if args.langs:
            settings["sub_langs"] = [_to_iso639_2(x.strip()) for x in args.langs.split(",")]
        elif args.sub_lang:
            settings["sub_langs"] = [_to_iso639_2(args.sub_lang)]
        try:
            ok = process_mkv(mkv, settings, args)
            if not ok: failed += 1
        except Exception as e:
            failed += 1
            print(f"{C.RED}subocr: {mkv.name}: {e}{C.R}", file=sys.stderr)
            if not args.post_rip:
                raise
    if failed:
        print(f"{C.YLW}subocr: {failed}/{len(mkvs)} file(s) had errors{C.R}", file=sys.stderr)
        sys.exit(0 if args.post_rip else 2)

if __name__ == "__main__":
    main()
