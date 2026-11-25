"""Matroska track introspection (`mkvmerge -J` + `ffprobe` fallback), ISO 639-2 normalization, and `mkvpropedit` defaults."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from burndvd.config import C, SUBOCR_CONFIG_PATH, ensure_tool_path
from burndvd.models import TrackInfo

ensure_tool_path()

CONFIG_PATH = SUBOCR_CONFIG_PATH

DEFAULT_CONFIG = {
    "_default": {
        "audio": "eng",
        "sub_track": "full",
        "sub_langs": ["eng", "spa", "zho"],
    },
    "Dragon Ball Z": {
        "audio": "eng",
        "sub_track": "forced",
        "sub_langs": ["eng"],
    },
}

# ISO 639-2/B (bibliographic, commonly emitted by MakeMKV/mkvmerge/ffprobe) -> ISO 639-2/T
_ISO639_2_B_TO_T = {
    "alb": "sqi",
    "arm": "hye",
    "baq": "eus",
    "bur": "mya",
    "chi": "zho",
    "cze": "ces",
    "dut": "nld",
    "fre": "fra",
    "geo": "kat",
    "ger": "deu",
    "gre": "ell",
    "ice": "isl",
    "mac": "mkd",
    "may": "msa",
    "per": "fas",
    "rum": "ron",
    "slo": "slk",
    "tib": "bod",
    "wel": "cym",
}

# ISO 639-2 -> 639-1 for SRT filename suffix (Jellyfin/Plex auto-picks `.en.srt` etc.)
LANG_639_1 = {
    "eng": "en",
    "jpn": "ja",
    "fra": "fr",
    "fre": "fr",
    "spa": "es",
    "deu": "de",
    "ger": "de",
    "ita": "it",
    "kor": "ko",
    "zho": "zh",
    "chi": "zh",
    "rus": "ru",
    "por": "pt",
    "nld": "nl",
    "dut": "nl",
    "ces": "cs",
    "cze": "cs",
    "ron": "ro",
    "rum": "ro",
    "ell": "el",
    "gre": "el",
    "mkd": "mk",
    "mac": "mk",
    "sqi": "sq",
    "alb": "sq",
    "hye": "hy",
    "arm": "hy",
    "eus": "eu",
    "baq": "eu",
    "mya": "my",
    "bur": "my",
    "kat": "ka",
    "geo": "ka",
    "isl": "is",
    "ice": "is",
    "msa": "ms",
    "may": "ms",
    "fas": "fa",
    "per": "fa",
    "slk": "sk",
    "slo": "sk",
    "bod": "bo",
    "tib": "bo",
    "cym": "cy",
    "wel": "cy",
}

# Apple Vision recognition language codes (BCP-47) keyed by canonical ISO 639-2/T
VISION_LANG = {
    "eng": "en-US",
    "jpn": "ja-JP",
    "fra": "fr-FR",
    "spa": "es-ES",
    "deu": "de-DE",
    "ita": "it-IT",
    "kor": "ko-KR",
    "zho": "zh-Hans",
    "rus": "ru-RU",
    "por": "pt-BR",
    "nld": "nl-NL",
    "ces": "cs-CZ",
    "ron": "ro-RO",
    "ell": "el-GR",
    "msa": "ms-MY",
    "slk": "sk-SK",
}

_LANG_639_2_BY_1 = {
    "en": "eng",
    "ja": "jpn",
    "fr": "fra",
    "es": "spa",
    "de": "deu",
    "it": "ita",
    "ko": "kor",
    "zh": "zho",
    "ru": "rus",
    "pt": "por",
    "nl": "nld",
    "cs": "ces",
    "ro": "ron",
    "el": "ell",
    "mk": "mkd",
    "sq": "sqi",
    "hy": "hye",
    "eu": "eus",
    "my": "mya",
    "ka": "kat",
    "is": "isl",
    "ms": "msa",
    "fa": "fas",
    "sk": "slk",
    "bo": "bod",
    "cy": "cym",
}

SHOW_RE = re.compile(r"/TV Shows(?: 4K)?/([^/]+)/")


def _to_iso639_2(code: str) -> str:
    """Normalize a 2- or 3-letter language code (incl. ISO 639-2/B) to ISO 639-2/T."""
    c = (code or "").lower().strip()
    if not c:
        return "und"
    base = c.split("-", 1)[0]
    c2 = _LANG_639_2_BY_1.get(base, base)
    return _ISO639_2_B_TO_T.get(c2, c2)


def show_for_path(p: Path) -> str | None:
    """Infer TV show name from a `/TV Shows/<Show>/...` or `/TV Shows 4K/<Show>/...` path."""
    m = SHOW_RE.search(str(p))
    return m.group(1) if m else None


def load_config(config_path: Path | None = None) -> dict:
    path = config_path or CONFIG_PATH
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        return dict(DEFAULT_CONFIG)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"{C.RED}subocr: bad JSON in {path}: {e}{C.R}", file=sys.stderr)
        sys.exit(2)


def show_settings(
    cfg: dict, show: str, audio_langs: set[str] | None = None
) -> dict:
    """Resolve per-show audio/subtitle settings with anime auto-detection."""
    default = cfg.get(
        "_default", {"audio": "eng", "sub_track": "full", "sub_langs": ["eng"]}
    )
    s = dict(default)
    norm_audio_langs = (
        {_to_iso639_2(a) for a in audio_langs} if audio_langs else set()
    )
    if show in cfg:
        s.update(cfg[show])
    elif "jpn" in norm_audio_langs:
        s["audio"] = "jpn"
    if "sub_langs" not in s:
        s["sub_langs"] = [s.get("sub_lang", "eng")]
    s["audio"] = _to_iso639_2(s.get("audio", "eng"))
    s["sub_langs"] = [_to_iso639_2(x) for x in s["sub_langs"]]
    return s


def _normalize_mkvmerge_codec(ttype: str, codec: str, codec_id: str) -> str:
    cid = (codec_id or "").upper()
    cl = (codec or "").lower()
    if ttype == "subtitles":
        if cid == "S_HDMV/PGS" or cl == "hdmv pgs":
            return "hdmv_pgs_subtitle"
        if cid == "S_VOBSUB" or cl == "vobsub":
            return "dvd_subtitle"
        return (codec_id or codec).lower()
    if cid == "A_TRUEHD" or "truehd" in cl:
        return "truehd"
    if cid == "A_FLAC" or "flac" in cl:
        return "flac"
    if cid.startswith("A_PCM") or "pcm" in cl:
        return "pcm"
    if "dts-hd master" in cl:
        return "dts-hd-ma"
    if cid == "A_DTS" or "dts" in cl:
        return "dts"
    if cid == "A_EAC3" or "e-ac-3" in cl:
        return "eac3"
    if cid == "A_AC3" or "ac-3" in cl:
        return "ac3"
    if cid.startswith("A_AAC") or "aac" in cl:
        return "aac"
    return (codec_id or codec).lower()


def _parse_int_tag(val) -> int | None:
    if val is None:
        return None
    try:
        n = int(val)
        return n if n >= 0 else None
    except (ValueError, TypeError):
        return None


def _probe_tracks_mkvmerge(mkv: Path) -> tuple[list[TrackInfo], list[TrackInfo]]:
    out = subprocess.check_output(["mkvmerge", "-J", str(mkv)], text=True)
    j = json.loads(out)
    audio, subs = [], []
    for pos, tr in enumerate(j.get("tracks", []), start=1):
        ttype = tr.get("type")
        if ttype not in ("audio", "subtitles"):
            continue
        props = tr.get("properties") or {}
        raw_lang = props.get("language")
        if not raw_lang or raw_lang.lower() == "und":
            raw_lang = props.get("language_ietf") or raw_lang or "und"
        n_frames = _parse_int_tag(props.get("tag_number_of_frames")) or 0
        size_bytes = _parse_int_tag(props.get("tag_number_of_bytes"))
        t = TrackInfo(
            index=int(tr["id"]),
            codec=_normalize_mkvmerge_codec(
                ttype, tr.get("codec", ""), props.get("codec_id", "")
            ),
            lang=_to_iso639_2(raw_lang),
            title=props.get("track_name", "") or "",
            forced=bool(props.get("forced_track", False)),
            mkv_pos=pos,
            n_frames_estimate=n_frames,
            size_bytes=size_bytes,
        )
        (audio if ttype == "audio" else subs).append(t)
    return audio, subs


def _probe_tracks_ffprobe(mkv: Path) -> tuple[list[TrackInfo], list[TrackInfo]]:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name:stream_disposition=forced:stream_tags=language,title",
            "-of",
            "json",
            str(mkv),
        ],
        text=True,
    )
    j = json.loads(out)
    audio, subs = [], []
    for s in j.get("streams", []):
        ct = s.get("codec_type")
        if ct not in ("audio", "subtitle"):
            continue
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}
        t = TrackInfo(
            index=s["index"],
            codec=s.get("codec_name", ""),
            lang=_to_iso639_2(tags.get("language") or "und"),
            title=tags.get("title", "") or "",
            forced=bool(disp.get("forced", 0)),
        )
        (audio if ct == "audio" else subs).append(t)
    return audio, subs


def probe_tracks(mkv: Path) -> tuple[list[TrackInfo], list[TrackInfo]]:
    """Return `(audio_tracks, sub_tracks)` with canonical ISO 639-2/T language codes."""
    try:
        return _probe_tracks_mkvmerge(mkv)
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError):
        return _probe_tracks_ffprobe(mkv)


def pick_audio_default(audio: list[TrackInfo], pref_lang: str) -> TrackInfo | None:
    pref = _to_iso639_2(pref_lang)
    cands = [a for a in audio if _to_iso639_2(a.lang) == pref]
    if not cands and pref == "jpn":
        return None
    if not cands:
        cands = list(audio)
    if not cands:
        return None
    rank = {
        "truehd": 0,
        "flac": 0,
        "pcm": 0,
        "dts-hd-ma": 0,
        "dts": 1,
        "eac3": 2,
        "ac3": 2,
        "aac": 3,
    }
    cands.sort(key=lambda a: rank.get(a.codec, 9))
    return cands[0]


def pick_sub_track(
    subs: list[TrackInfo], lang: str, mode: str, mkv: Path
) -> TrackInfo | None:
    """Pick the PGS subtitle track to OCR for `lang` and `mode` (`full` or `forced`)."""
    target_lang = _to_iso639_2(lang)
    cands = [
        s
        for s in subs
        if s.codec == "hdmv_pgs_subtitle" and _to_iso639_2(s.lang) == target_lang
    ]
    if not cands:
        return None

    need_measure = [t for t in cands if t.size_bytes is None]
    measured_ok: set[int] = set()
    if need_measure:
        with tempfile.TemporaryDirectory(prefix="subocr-sizehint-") as td:
            for t in need_measure:
                sup = Path(td) / f"t{t.index}.sup"
                r = subprocess.run(
                    ["mkvextract", "tracks", str(mkv), f"{t.index}:{sup}"],
                    capture_output=True,
                    text=True,
                )
                if r.returncode != 0 or not sup.exists():
                    continue
                t.size_bytes = sup.stat().st_size
                measured_ok.add(t.index)

    sized = [
        t
        for t in cands
        if t.size_bytes is not None
        and (t not in need_measure or t.index in measured_ok)
    ]
    nonempty = [t for t in sized if (t.size_bytes or 0) > 0]
    if not nonempty:
        return None

    for t in nonempty:
        t.forced_only = False

    if mode == "forced":
        explicit_forced = [t for t in nonempty if t.forced]
        if explicit_forced:
            explicit_forced.sort(key=lambda t: t.size_bytes or 0)
            chosen = explicit_forced[0]
            chosen.forced_only = False
            return chosen
        if len(nonempty) == 1:
            chosen = nonempty[0]
            chosen.forced_only = True
            return chosen
        nonempty.sort(key=lambda t: t.size_bytes or 0)
        chosen = nonempty[0]
        chosen.forced_only = False
        return chosen

    nonempty.sort(key=lambda t: t.size_bytes or 0)
    chosen = nonempty[-1]
    chosen.forced_only = False
    return chosen


def _mkvmerge_track_positions(mkv: Path) -> dict[int, int]:
    try:
        out = subprocess.check_output(["mkvmerge", "-J", str(mkv)], text=True)
        j = json.loads(out)
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError):
        return {}
    pos_for: dict[int, int] = {}
    for i, t in enumerate(j.get("tracks", []), start=1):
        if "id" in t:
            pos_for[t["id"]] = i
    return pos_for


def set_track_defaults(
    mkv: Path,
    audio_track: TrackInfo | None,
    sub_track: TrackInfo | None,
    dry_run: bool = False,
) -> bool:
    """Set chosen audio/sub track `flag-default=1` via `mkvpropedit`.

    When `audio_track` is None, existing audio default flags are preserved untouched.
    """
    audio_streams, sub_streams = probe_tracks(mkv)
    pos_for = {
        t.index: t.mkv_pos
        for t in (*audio_streams, *sub_streams)
        if t.mkv_pos is not None
    }
    if not pos_for and (audio_streams or sub_streams):
        pos_for = _mkvmerge_track_positions(mkv)
    cmd = ["mkvpropedit", str(mkv)]
    if audio_track is not None:
        for a in audio_streams:
            n = pos_for.get(a.index)
            if n is None:
                continue
            flag = "1" if a.index == audio_track.index else "0"
            cmd += ["--edit", f"track:{n}", "--set", f"flag-default={flag}"]
    for s in sub_streams:
        n = pos_for.get(s.index)
        if n is None:
            continue
        flag = "1" if (sub_track and s.index == sub_track.index) else "0"
        cmd += ["--edit", f"track:{n}", "--set", f"flag-default={flag}"]
    if len(cmd) == 2:
        return True
    if dry_run:
        print(f"  would run: {' '.join(cmd)}")
        return True
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        err_msg = (r.stderr or r.stdout).strip()[:200]
        print(f"{C.YLW}  mkvpropedit failed: {err_msg}{C.R}", file=sys.stderr)
        return False
    return True
