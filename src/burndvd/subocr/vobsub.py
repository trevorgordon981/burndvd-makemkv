"""DVD VobSub (`.idx` + `.sub`) binary parser, MPEG-2 PS/PES demuxer, SPU DCSQT parser, and interlaced 2-bit RLE decoder."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from burndvd.subocr.tracks import _to_iso639_2

# MPEG-2 Program Stream / PES start codes
_PACK_START_CODE = 0xBA
_SYSTEM_HEADER_CODE = 0xBB
_PRIVATE_STREAM_1 = 0xBD
_PADDING_STREAM = 0xBE
_PRIVATE_STREAM_2 = 0xBF
_SPU_SUBSTREAM_MIN = 0x20
_SPU_SUBSTREAM_MAX = 0x3F

# DVD SPU Display Control Sequence commands
CMD_FSTA_DSP = 0x00  # Forced start display
CMD_STA_DSP = 0x01   # Start display
CMD_STP_DSP = 0x02   # Stop display
CMD_SET_COLOR = 0x03  # Palette indices [e2, e1, p, b]
CMD_SET_CONTR = 0x04  # Alpha/contrast [e2, e1, p, b] (4-bit 0..15)
CMD_SET_DAREA = 0x05  # Display area (x1, x2, y1, y2)
CMD_SET_DSPXA = 0x06  # Top-field & bottom-field RLE offsets
CMD_CHG_COLCON = 0x07  # Change color/contrast within area (2-byte length incl. header)
CMD_END = 0xFF        # End of DCSQ

DEFAULT_VOBSUB_DURATION_MS = 5000
_MAX_SPU_SCAN_BYTES = 512 * 1024

_DEFAULT_PALETTE: list[tuple[int, int, int]] = [
    (0, 0, 0),
    (255, 255, 255),
    (128, 128, 128),
    (235, 235, 235),
    (255, 255, 0),
    (255, 255, 255),
    (0, 0, 0),
    (0, 0, 0),
    (128, 128, 128),
    (200, 200, 200),
    (0, 0, 0),
    (0, 0, 0),
    (0, 0, 0),
    (0, 0, 0),
    (128, 128, 128),
    (220, 220, 220),
]

_SIZE_RE = re.compile(r"^size\s*:\s*(\d+)\s*x\s*(\d+)", re.IGNORECASE)
_PALETTE_RE = re.compile(r"^palette\s*:\s*(.+)$", re.IGNORECASE)
_LANGIDX_RE = re.compile(r"^langidx\s*:\s*(\d+)", re.IGNORECASE)
_TIMEOFFSET_RE = re.compile(r"^time\s+offset\s*:\s*([+-]?\d+)", re.IGNORECASE)
_ID_RE = re.compile(r"^id\s*:\s*([^,\s]+)\s*,\s*index\s*:\s*(\d+)", re.IGNORECASE)
_DELAY_RE = re.compile(r"^delay\s*:\s*([+-]?\d+:\d+:\d+:\d+)", re.IGNORECASE)
_TIMESTAMP_RE = re.compile(
    r"^timestamp\s*:\s*([+-]?\d+:\d+:\d+:\d+)\s*,\s*filepos\s*:\s*([0-9a-fA-F]+)",
    re.IGNORECASE,
)


@dataclass
class VobSubIndexEntry:
    timestamp_ms: int
    filepos: int
    track_index: int = 0
    lang: str = "und"


@dataclass
class VobSubIndex:
    size: tuple[int, int] = (720, 480)
    palette: list[tuple[int, int, int]] = field(default_factory=lambda: list(_DEFAULT_PALETTE))
    langidx: int = 0
    time_offset_ms: int = 0
    tracks: dict[int, tuple[str, list[VobSubIndexEntry]]] = field(default_factory=dict)


@dataclass
class SpuControl:
    forced: bool = False
    start_delay_ms: int = 0
    stop_delay_ms: int | None = None
    color_map: tuple[int, int, int, int] = (0, 1, 2, 3)  # indexed by 2-bit pixel (b, p, e1, e2)
    alpha_map: tuple[int, int, int, int] = (0, 255, 255, 255)  # 8-bit alpha (b, p, e1, e2)
    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0
    top_off: int = 0
    bot_off: int = 0
    has_darea: bool = False
    has_dspxa: bool = False

    @property
    def width(self) -> int:
        return self.x2 - self.x1 + 1 if self.has_darea and self.x2 >= self.x1 else 0

    @property
    def height(self) -> int:
        return self.y2 - self.y1 + 1 if self.has_darea and self.y2 >= self.y1 else 0


@dataclass
class VobSubCue:
    start_ms: int
    end_ms: int
    image: object
    forced: bool = False


def _parse_idx_timestamp(raw: str) -> int:
    """Parse `[+-]HH:MM:SS:mmm` into signed milliseconds."""
    s = raw.strip()
    sign = -1 if s.startswith("-") else 1
    if s[0] in "+-":
        s = s[1:]
    parts = s.split(":")
    if len(parts) != 4:
        raise ValueError(f"invalid VobSub timestamp: {raw!r}")
    hh, mm, ss, mmm = (int(p) for p in parts)
    return sign * (((hh * 3600 + mm * 60 + ss) * 1000) + mmm)


def parse_idx(idx_path: Path) -> VobSubIndex:
    """Parse a VobSub `.idx` file into header metadata and per-track timestamp/filepos entries."""
    text = Path(idx_path).read_text(encoding="utf-8", errors="replace")
    idx = VobSubIndex()
    cur_track = 0
    cur_lang = "und"
    cur_delay_ms = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        m = _SIZE_RE.match(line)
        if m:
            idx.size = (int(m.group(1)), int(m.group(2)))
            continue

        m = _PALETTE_RE.match(line)
        if m:
            colors: list[tuple[int, int, int]] = []
            for tok in m.group(1).split(","):
                h = tok.strip().lstrip("#")
                if len(h) >= 6:
                    try:
                        colors.append((int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))
                    except ValueError:
                        colors.append((0, 0, 0))
            while len(colors) < 16:
                colors.append(_DEFAULT_PALETTE[len(colors)])
            idx.palette = colors[:16]
            continue

        m = _LANGIDX_RE.match(line)
        if m:
            idx.langidx = int(m.group(1))
            continue

        m = _TIMEOFFSET_RE.match(line)
        if m:
            idx.time_offset_ms = int(m.group(1))
            continue

        m = _ID_RE.match(line)
        if m:
            cur_lang = m.group(1).strip()
            cur_track = int(m.group(2))
            cur_delay_ms = 0
            if cur_track not in idx.tracks:
                idx.tracks[cur_track] = (cur_lang, [])
            continue

        m = _DELAY_RE.match(line)
        if m:
            try:
                cur_delay_ms += _parse_idx_timestamp(m.group(1))
            except ValueError:
                pass
            continue

        m = _TIMESTAMP_RE.match(line)
        if m:
            try:
                ts_ms = _parse_idx_timestamp(m.group(1)) + idx.time_offset_ms + cur_delay_ms
                filepos = int(m.group(2), 16)
            except ValueError:
                continue
            if cur_track not in idx.tracks:
                idx.tracks[cur_track] = (cur_lang, [])
            idx.tracks[cur_track][1].append(
                VobSubIndexEntry(
                    timestamp_ms=max(0, ts_ms),
                    filepos=filepos,
                    track_index=cur_track,
                    lang=cur_lang,
                )
            )

    return idx


def _select_idx_track(
    idx: VobSubIndex,
    *,
    track_index: int | None = None,
    lang: str | None = None,
) -> tuple[int, list[VobSubIndexEntry]]:
    """Select which `.idx` track entries to decode based on track_index, lang, or langidx."""
    if not idx.tracks:
        return (0, [])
    if track_index is not None:
        if track_index in idx.tracks:
            return (track_index, idx.tracks[track_index][1])
        return (track_index, [])
    if lang is not None:
        target = _to_iso639_2(lang)
        for tid, (tlang, entries) in idx.tracks.items():
            if entries and _to_iso639_2(tlang) == target:
                return (tid, entries)
    if idx.langidx in idx.tracks and idx.tracks[idx.langidx][1]:
        return (idx.langidx, idx.tracks[idx.langidx][1])
    for tid, (_tlang, entries) in idx.tracks.items():
        if entries:
            return (tid, entries)
    first_tid = next(iter(idx.tracks))
    return (first_tid, idx.tracks[first_tid][1])


def extract_spu_packet(
    sub_data: bytes,
    filepos: int,
    *,
    end_pos: int | None = None,
    substream_id: int | None = None,
) -> bytes | None:
    """Demux MPEG-2 PS/PES packets starting at `filepos` into one complete DVD SPU packet."""
    n = len(sub_data)
    if filepos < 0 or filepos >= n:
        return None
    limit = n if end_pos is None else min(n, max(filepos, end_pos))
    scan_limit = min(limit, filepos + _MAX_SPU_SCAN_BYTES)

    pos = filepos
    spu_buf = bytearray()
    expected_size: int | None = None
    active_sid: int | None = substream_id

    while pos + 4 <= scan_limit:
        if sub_data[pos] != 0x00 or sub_data[pos + 1] != 0x00 or sub_data[pos + 2] != 0x01:
            nxt = sub_data.find(b"\x00\x00\x01", pos + 1, scan_limit)
            if nxt == -1:
                break
            pos = nxt
            continue

        code = sub_data[pos + 3]

        if code == _PACK_START_CODE:
            if pos + 5 > scan_limit:
                break
            marker = sub_data[pos + 4]
            if (marker & 0xC0) == 0x40:
                # MPEG-2 PS pack header: 14 bytes + stuffing length (lower 3 bits of byte 13)
                if pos + 14 > scan_limit:
                    break
                stuffing = sub_data[pos + 13] & 0x07
                pos += 14 + stuffing
            elif (marker & 0xF0) == 0x20:
                # MPEG-1 PS pack header: 12 bytes
                pos += 12
            else:
                pos += 4
            continue

        if code == _PRIVATE_STREAM_1:
            if pos + 6 > scan_limit:
                break
            pes_len = struct.unpack(">H", sub_data[pos + 4 : pos + 6])[0]
            pkt_end = min(n, pos + 6 + pes_len) if pes_len > 0 else scan_limit
            if pos + 9 > pkt_end:
                pos = max(pos + 6, pkt_end)
                continue

            flags0 = sub_data[pos + 6]
            if (flags0 & 0xC0) == 0x80:
                # MPEG-2 PES header
                hdr_len = sub_data[pos + 8]
                payload_start = pos + 9 + hdr_len
            else:
                # MPEG-1 PES header fallback
                p = pos + 6
                while p < pkt_end and sub_data[p] == 0xFF:
                    p += 1
                if p < pkt_end and (sub_data[p] & 0xC0) == 0x40:
                    p += 2
                if p < pkt_end:
                    b = sub_data[p]
                    if (b & 0xF0) == 0x20:
                        p += 5
                    elif (b & 0xF0) == 0x30:
                        p += 10
                    else:
                        p += 1
                payload_start = p

            if payload_start < pkt_end:
                sid = sub_data[payload_start]
                if _SPU_SUBSTREAM_MIN <= sid <= _SPU_SUBSTREAM_MAX:
                    if active_sid is None:
                        active_sid = sid
                    if sid == active_sid:
                        chunk = sub_data[payload_start + 1 : pkt_end]
                        if expected_size is not None:
                            need = expected_size - len(spu_buf)
                            if need > 0:
                                spu_buf.extend(chunk[:need])
                        else:
                            spu_buf.extend(chunk)
                        if expected_size is None and len(spu_buf) >= 2:
                            expected_size = struct.unpack(">H", spu_buf[:2])[0]
                            if expected_size < 4:
                                return None
                        if expected_size is not None and len(spu_buf) >= expected_size:
                            return bytes(spu_buf[:expected_size])

            pos = max(pos + 6, pkt_end)
            continue

        if code in (_SYSTEM_HEADER_CODE, _PADDING_STREAM, _PRIVATE_STREAM_2) or 0xBC <= code <= 0xEF:
            if pos + 6 > scan_limit:
                break
            pes_len = struct.unpack(">H", sub_data[pos + 4 : pos + 6])[0]
            pos = max(pos + 6, pos + 6 + pes_len)
            continue

        pos += 4

    if expected_size is not None and len(spu_buf) >= 4:
        ctrl_off = struct.unpack(">H", spu_buf[2:4])[0]
        if ctrl_off + 4 <= len(spu_buf):
            return bytes(spu_buf[: min(len(spu_buf), expected_size)])
    return None


def _spu_delay_ms(date: int) -> int:
    """Convert DVD SPU DCSQ `date` units (1024 / 90000 s) to milliseconds."""
    return int(round(date * 1024 / 90.0))


def parse_spu_control(spu: bytes) -> SpuControl | None:
    """Parse a DVD SPU Display Control Sequence Table (`SP_DCSQT`)."""
    n = len(spu)
    if n < 4:
        return None
    _spu_size, ctrl_off = struct.unpack(">HH", spu[:4])
    if ctrl_off < 4 or ctrl_off + 4 > n:
        return None

    ctrl = SpuControl()
    has_start = False
    best_alpha_sum = -1
    visited: set[int] = set()
    off = ctrl_off

    while off not in visited and 4 <= off and off + 4 <= n:
        visited.add(off)
        date, next_off = struct.unpack(">HH", spu[off : off + 4])
        delay_ms = _spu_delay_ms(date)
        pos = off + 4

        dcsq_alpha: tuple[int, int, int, int] | None = None
        dcsq_color: tuple[int, int, int, int] | None = None
        dcsq_is_stop = False

        while pos < n:
            cmd = spu[pos]
            pos += 1

            if cmd == CMD_FSTA_DSP:
                ctrl.forced = True
                if not has_start:
                    ctrl.start_delay_ms = delay_ms
                    has_start = True
            elif cmd == CMD_STA_DSP:
                if not has_start:
                    ctrl.start_delay_ms = delay_ms
                    has_start = True
            elif cmd == CMD_STP_DSP:
                ctrl.stop_delay_ms = delay_ms
                dcsq_is_stop = True
            elif cmd == CMD_SET_COLOR:
                if pos + 2 > n:
                    break
                b0, b1 = spu[pos], spu[pos + 1]
                pos += 2
                e2 = (b0 >> 4) & 0x0F
                e1 = b0 & 0x0F
                p = (b1 >> 4) & 0x0F
                b = b1 & 0x0F
                dcsq_color = (b, p, e1, e2)
            elif cmd == CMD_SET_CONTR:
                if pos + 2 > n:
                    break
                b0, b1 = spu[pos], spu[pos + 1]
                pos += 2
                e2 = ((b0 >> 4) & 0x0F) * 17
                e1 = (b0 & 0x0F) * 17
                p = ((b1 >> 4) & 0x0F) * 17
                b = (b1 & 0x0F) * 17
                dcsq_alpha = (b, p, e1, e2)
            elif cmd == CMD_SET_DAREA:
                if pos + 6 > n:
                    break
                d0, d1, d2, d3, d4, d5 = spu[pos : pos + 6]
                pos += 6
                ctrl.x1 = (d0 << 4) | ((d1 >> 4) & 0x0F)
                ctrl.x2 = ((d1 & 0x0F) << 8) | d2
                ctrl.y1 = (d3 << 4) | ((d4 >> 4) & 0x0F)
                ctrl.y2 = ((d4 & 0x0F) << 8) | d5
                ctrl.has_darea = True
            elif cmd == CMD_SET_DSPXA:
                if pos + 4 > n:
                    break
                ctrl.top_off, ctrl.bot_off = struct.unpack(">HH", spu[pos : pos + 4])
                pos += 4
                ctrl.has_dspxa = True
            elif cmd == CMD_CHG_COLCON:
                if pos + 2 > n:
                    break
                seg_len = struct.unpack(">H", spu[pos : pos + 2])[0]
                if seg_len < 2 or pos + seg_len > n:
                    break
                pos += seg_len
            elif cmd == CMD_END:
                break
            else:
                break

        if dcsq_color is not None and not dcsq_is_stop:
            ctrl.color_map = dcsq_color
        if dcsq_alpha is not None and not dcsq_is_stop:
            alpha_sum = sum(dcsq_alpha)
            if alpha_sum > best_alpha_sum:
                best_alpha_sum = alpha_sum
                ctrl.alpha_map = dcsq_alpha

        if next_off == off or next_off < ctrl_off or next_off + 4 > n:
            break
        off = next_off

    if not ctrl.has_darea or not ctrl.has_dspxa or ctrl.width <= 0 or ctrl.height <= 0:
        return None
    return ctrl


class _NibbleReader:
    """MSB-first 4-bit nibble reader with scanline byte alignment."""

    __slots__ = ("_data", "_pos", "_end", "_high")

    def __init__(self, data: bytes, start: int, end: int) -> None:
        self._data = data
        self._pos = start
        self._end = min(len(data), end)
        self._high = True

    def read_nibble(self) -> int | None:
        if self._pos >= self._end:
            return None
        b = self._data[self._pos]
        if self._high:
            self._high = False
            return (b >> 4) & 0x0F
        self._high = True
        self._pos += 1
        return b & 0x0F

    def byte_align(self) -> None:
        if not self._high:
            self._high = True
            self._pos += 1


def _decode_field(
    data: bytes,
    start_off: int,
    end_off: int,
    out: bytearray,
    width: int,
    height: int,
    start_y: int,
) -> None:
    """Decode one interlaced field (even rows for start_y=0, odd rows for start_y=1)."""
    if start_off < 0 or start_off >= len(data):
        return
    reader = _NibbleReader(data, start_off, end_off)
    y = start_y
    while y < height:
        x = 0
        row_base = y * width
        while x < width:
            n1 = reader.read_nibble()
            if n1 is None:
                return
            if n1 >= 0x4:
                # 1-nibble code: run 1..3
                code = n1
                run = code >> 2
                color = code & 0x03
            else:
                n2 = reader.read_nibble()
                if n2 is None:
                    return
                code = (n1 << 4) | n2
                if code >= 0x10:
                    # 2-nibble code: run 4..15
                    run = code >> 2
                    color = code & 0x03
                else:
                    n3 = reader.read_nibble()
                    if n3 is None:
                        return
                    code = (code << 4) | n3
                    if code >= 0x40:
                        # 3-nibble code: run 16..63
                        run = code >> 2
                        color = code & 0x03
                    else:
                        n4 = reader.read_nibble()
                        if n4 is None:
                            return
                        code = (code << 4) | n4
                        # 4-nibble code: run 64..255, or run==0 -> fill to end of line
                        run = code >> 2
                        color = code & 0x03
                        if run == 0:
                            run = width - x

            remain = width - x
            if run > remain:
                run = remain
            if run > 0:
                if color != 0:
                    out[row_base + x : row_base + x + run] = bytes([color]) * run
                x += run
        reader.byte_align()
        y += 2


def decode_spu_rle(
    spu: bytes,
    top_off: int,
    bot_off: int,
    width: int,
    height: int,
    *,
    ctrl_off: int | None = None,
) -> bytes:
    """Decode interlaced 2-bit DVD SPU RLE fields into a flat `width * height` pixel buffer."""
    out = bytearray(width * height)
    if width <= 0 or height <= 0:
        return bytes(out)

    data_end = len(spu) if ctrl_off is None else min(len(spu), ctrl_off)
    top_end = bot_off if top_off < bot_off <= data_end else data_end
    bot_end = data_end if bot_off < data_end else len(spu)

    _decode_field(spu, top_off, top_end, out, width, height, start_y=0)
    _decode_field(spu, bot_off, bot_end, out, width, height, start_y=1)
    return bytes(out)


def render_spu_image(
    spu: bytes,
    ctrl: SpuControl,
    palette: list[tuple[int, int, int]],
    *,
    pad: int = 8,
) -> object | None:
    """Render a decoded DVD SPU into a clean high-contrast cropped RGBA `PIL.Image` for OCR."""
    from PIL import Image

    width = ctrl.width
    height = ctrl.height
    if width <= 0 or height <= 0:
        return None

    _spu_size, ctrl_off = struct.unpack(">HH", spu[:4])
    pixels = decode_spu_rle(
        spu,
        ctrl.top_off,
        ctrl.bot_off,
        width,
        height,
        ctrl_off=ctrl_off,
    )

    rgba_lut: list[tuple[int, int, int, int]] = []
    max_visible_luma = 0
    for val in range(4):
        pidx = ctrl.color_map[val] & 0x0F
        r, g, b = palette[pidx] if pidx < len(palette) else (255, 255, 255)
        a = ctrl.alpha_map[val]
        rgba_lut.append((r, g, b, a))
        if a > 0:
            luma = (299 * r + 587 * g + 114 * b) // 1000
            if luma > max_visible_luma:
                max_visible_luma = luma

    # Fallback if a disc has an all-black/missing palette so glyphs stay readable on black canvas
    if max_visible_luma < 64:
        fallback_lut: list[tuple[int, int, int, int]] = []
        for val in range(4):
            a = ctrl.alpha_map[val]
            if a == 0:
                fallback_lut.append((0, 0, 0, 0))
            elif val == 1:
                fallback_lut.append((255, 255, 255, a))
            elif val == 2:
                fallback_lut.append((180, 180, 180, a))
            else:
                fallback_lut.append((40, 40, 40, a))
        rgba_lut = fallback_lut

    raw_rgba = bytearray(width * height * 4)
    has_visible = False
    for i, val in enumerate(pixels):
        r, g, b, a = rgba_lut[val]
        if a > 0:
            off = i * 4
            raw_rgba[off] = r
            raw_rgba[off + 1] = g
            raw_rgba[off + 2] = b
            raw_rgba[off + 3] = a
            has_visible = True

    if not has_visible:
        return None

    img = Image.frombytes("RGBA", (width, height), bytes(raw_rgba))
    bbox = img.getbbox()
    if bbox is None:
        return None
    img = img.crop(bbox)

    if pad > 0:
        padded = Image.new("RGBA", (img.width + pad * 2, img.height + pad * 2), (0, 0, 0, 0))
        padded.paste(img, (pad, pad))
        return padded
    return img


def parse_vobsub_cues(
    idx_path: Path,
    sub_path: Path | None = None,
    *,
    forced_only: bool = False,
    track_index: int | None = None,
    lang: str | None = None,
    default_duration_ms: int = DEFAULT_VOBSUB_DURATION_MS,
    pad: int = 8,
) -> list[VobSubCue]:
    """Parse VobSub `.idx` + `.sub` files into structured `VobSubCue` objects."""
    idx_file = Path(idx_path)
    sub_file = Path(sub_path) if sub_path is not None else idx_file.with_suffix(".sub")
    if not idx_file.exists() or not sub_file.exists():
        return []

    idx = parse_idx(idx_file)
    sel_tid, entries = _select_idx_track(idx, track_index=track_index, lang=lang)
    if not entries:
        return []

    sub_data = sub_file.read_bytes()
    multi_track = len(idx.tracks) > 1
    expected_sid = (_SPU_SUBSTREAM_MIN + sel_tid) if multi_track else None

    # Sort all file positions across tracks so single-track packet scans have tight bounds
    all_positions = sorted(
        {e.filepos for _lang, elist in idx.tracks.values() for e in elist}
    )
    next_pos_map: dict[int, int] = {}
    for i, fp in enumerate(all_positions):
        next_pos_map[fp] = all_positions[i + 1] if i + 1 < len(all_positions) else len(sub_data)

    # First pass: demux & parse control headers to get accurate start/end timestamps
    decoded: list[tuple[int, int | None, bool, bytes, SpuControl]] = []
    for entry in entries:
        end_bound = None if multi_track else next_pos_map.get(entry.filepos)
        spu = extract_spu_packet(
            sub_data,
            entry.filepos,
            end_pos=end_bound,
            substream_id=expected_sid,
        )
        if spu is None and expected_sid is not None:
            spu = extract_spu_packet(sub_data, entry.filepos, end_pos=None, substream_id=None)
        if spu is None:
            continue
        ctrl = parse_spu_control(spu)
        if ctrl is None:
            continue
        start_ms = max(0, entry.timestamp_ms + ctrl.start_delay_ms)
        end_ms: int | None = None
        if ctrl.stop_delay_ms is not None and ctrl.stop_delay_ms > ctrl.start_delay_ms:
            end_ms = entry.timestamp_ms + ctrl.stop_delay_ms
        decoded.append((start_ms, end_ms, ctrl.forced, spu, ctrl))

    cues: list[VobSubCue] = []
    for i, (start_ms, end_ms, is_forced, spu, ctrl) in enumerate(decoded):
        if end_ms is None:
            next_start = decoded[i + 1][0] if i + 1 < len(decoded) else None
            cap = start_ms + default_duration_ms
            if next_start is not None and next_start > start_ms:
                end_ms = min(next_start, cap)
            else:
                end_ms = cap
        if end_ms <= start_ms:
            continue
        if forced_only and not is_forced:
            continue
        img = render_spu_image(spu, ctrl, idx.palette, pad=pad)
        if img is None:
            continue
        cues.append(VobSubCue(start_ms=start_ms, end_ms=end_ms, image=img, forced=is_forced))

    return cues


def parse_vobsub(
    idx_path: Path,
    sub_path: Path | None = None,
    *,
    forced_only: bool = False,
    track_index: int | None = None,
    lang: str | None = None,
    default_duration_ms: int = DEFAULT_VOBSUB_DURATION_MS,
) -> list[tuple[int, int, object]]:
    """Parse VobSub `.idx` + `.sub` files into `[(start_ms, end_ms, PIL.Image), ...]`.

    Matches `pgs.parse_sup()`'s return contract for shared pipeline OCR.
    """
    cues = parse_vobsub_cues(
        idx_path,
        sub_path=sub_path,
        forced_only=forced_only,
        track_index=track_index,
        lang=lang,
        default_duration_ms=default_duration_ms,
    )
    return [(c.start_ms, c.end_ms, c.image) for c in cues]
