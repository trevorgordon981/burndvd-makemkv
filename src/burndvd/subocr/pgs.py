"""HDMV PGS (`.sup`) binary parser, RLE decoder, BT.601 YCrCb->RGB palette, and crop compositor."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

PGS_MAGIC = b"PG"
SEG_PDS = 0x14
SEG_ODS = 0x15
SEG_PCS = 0x16
SEG_WDS = 0x17
SEG_END = 0x80
PTS_HZ = 90_000  # PGS timestamps in 90 kHz ticks


@dataclass
class PgsObject:
    object_id: int
    version: int
    width: int = 0
    height: int = 0
    rle: bytes = b""


@dataclass
class PgsPalette:
    palette_id: int
    version: int
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
    objects: dict[int, PgsObject] = field(default_factory=dict)
    palettes: dict[int, PgsPalette] = field(default_factory=dict)


def _ycrcb_to_rgb(y: int, cr: int, cb: int) -> tuple[int, int, int]:
    """Convert BT.601 limited-range YCrCb to 8-bit RGB."""
    yp = y - 16
    cbp = cb - 128
    crp = cr - 128
    r = 1.164 * yp + 1.596 * crp
    g = 1.164 * yp - 0.392 * cbp - 0.813 * crp
    b = 1.164 * yp + 2.017 * cbp
    return (
        max(0, min(255, int(r))),
        max(0, min(255, int(g))),
        max(0, min(255, int(b))),
    )


def _decode_rle(data: bytes, width: int, height: int) -> bytes:
    """Decode PGS RLE stream into a flat palette-index buffer of length `width * height`."""
    out = bytearray(width * height)
    di = 0
    si = 0
    x = 0
    y = 0
    L = len(data)
    while si < L and y < height:
        b = data[si]
        si += 1
        if b != 0:
            if x < width:
                out[di] = b
                di += 1
                x += 1
            continue
        if si >= L:
            break
        b2 = data[si]
        si += 1
        if b2 == 0:
            pad = width - x
            if pad > 0:
                di += pad
            x = 0
            y += 1
            continue
        top2 = b2 & 0xC0
        low6 = b2 & 0x3F
        if top2 == 0x00:
            run = low6
            color = 0
        elif top2 == 0x40:
            if si >= L:
                break
            run = (low6 << 8) | data[si]
            si += 1
            color = 0
        elif top2 == 0x80:
            run = low6
            if si >= L:
                break
            color = data[si]
            si += 1
        else:
            if si + 1 >= L:
                break
            run = (low6 << 8) | data[si]
            si += 1
            color = data[si]
            si += 1
        remain = width - x
        if run > remain:
            run = remain
        if run > 0:
            if color == 0:
                di += run
            else:
                out[di : di + run] = bytes([color]) * run
                di += run
            x += run
    return bytes(out)


def parse_sup(sup_path: Path, forced_only: bool = False) -> list[tuple[int, int, object]]:
    """Parse a PGS `.sup` file into `[(pts_ms, end_pts_ms, PIL.Image), ...]`.

    When `forced_only=True`, only compositions with `cp.forced == True` are rendered.
    """
    from PIL import Image

    data = Path(sup_path).read_bytes()
    pos = 0
    L = len(data)
    cur_objs: dict[int, PgsObject] = {}
    cur_pals: dict[int, PgsPalette] = {}
    displays: list[PgsDisplay] = []
    pending: PgsDisplay | None = None

    while pos + 13 <= L:
        if data[pos : pos + 2] != PGS_MAGIC:
            pos += 1
            continue
        pts = struct.unpack(">I", data[pos + 2 : pos + 6])[0]
        stype = data[pos + 10]
        ssize = struct.unpack(">H", data[pos + 11 : pos + 13])[0]
        body = data[pos + 13 : pos + 13 + ssize]
        pos += 13 + ssize

        if stype == SEG_PCS:
            if len(body) < 11:
                continue
            (
                video_w,
                video_h,
                _frame_rate,
                _comp_num,
                _comp_state,
                _pal_update,
                palette_id,
                num_objs,
            ) = struct.unpack(">HHBHBBBB", body[:11])
            cps: list[PgsComposition] = []
            off = 11
            for _ in range(num_objs):
                if off + 8 > len(body):
                    break
                obj_id, win_id, flags, ox, oy = struct.unpack(
                    ">HBBHH", body[off : off + 8]
                )
                off += 8
                forced = bool(flags & 0x40)
                cropped = bool(flags & 0x80)
                crop = None
                if cropped:
                    if off + 8 > len(body):
                        break
                    crop = struct.unpack(">HHHH", body[off : off + 8])
                    off += 8
                cps.append(PgsComposition(obj_id, win_id, forced, ox, oy, crop))
            pending = PgsDisplay(
                pts_ms=pts // (PTS_HZ // 1000),
                video_w=video_w,
                video_h=video_h,
                palette_id=palette_id,
                compositions=cps,
            )

        elif stype == SEG_PDS:
            if len(body) < 2:
                continue
            pal_id, pal_ver = body[0], body[1]
            pal = PgsPalette(pal_id, pal_ver)
            off = 2
            while off + 5 <= len(body):
                eid, Y, Cr, Cb, A = body[off : off + 5]
                off += 5
                r, g, b = _ycrcb_to_rgb(Y, Cr, Cb)
                pal.entries[eid] = (r, g, b, A)
            cur_pals[pal_id] = pal

        elif stype == SEG_ODS:
            if len(body) < 4:
                continue
            obj_id = struct.unpack(">H", body[:2])[0]
            ver = body[2]
            last_flag = body[3]
            if last_flag & 0x80:
                if len(body) < 11:
                    continue
                width = struct.unpack(">H", body[7:9])[0]
                height = struct.unpack(">H", body[9:11])[0]
                cur_objs[obj_id] = PgsObject(obj_id, ver, width, height, body[11:])
            else:
                prev = cur_objs.get(obj_id)
                if prev is not None:
                    cur_objs[obj_id] = PgsObject(
                        prev.object_id,
                        prev.version,
                        prev.width,
                        prev.height,
                        prev.rle + body[4:],
                    )

        elif stype == SEG_END:
            if pending is None:
                continue
            pending.palettes = dict(cur_pals)
            pending.objects = dict(cur_objs)
            displays.append(pending)
            pending = None

    out: list[tuple[int, int | None, object]] = []
    for d in displays:
        if not d.compositions:
            if out and out[-1][1] is None:
                out[-1] = (out[-1][0], d.pts_ms, out[-1][2])
            continue
        cps = (
            [cp for cp in d.compositions if cp.forced]
            if forced_only
            else d.compositions
        )
        if not cps:
            if out and out[-1][1] is None:
                out[-1] = (out[-1][0], d.pts_ms, out[-1][2])
            continue
        pal = d.palettes.get(d.palette_id)
        if pal is None:
            if out and out[-1][1] is None:
                out[-1] = (out[-1][0], d.pts_ms, out[-1][2])
            continue
        img = Image.new("RGBA", (d.video_w, d.video_h), (0, 0, 0, 0))
        any_rendered = False
        for cp in cps:
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
            if cp.crop:
                cx, cy, cw, ch = cp.crop
                x0 = max(0, min(cx, sub_img.width))
                y0 = max(0, min(cy, sub_img.height))
                x1 = max(x0, min(cx + cw, sub_img.width))
                y1 = max(y0, min(cy + ch, sub_img.height))
                if x1 <= x0 or y1 <= y0:
                    continue
                sub_img = sub_img.crop((x0, y0, x1, y1))
            img.alpha_composite(sub_img, dest=(cp.x, cp.y))
            any_rendered = True
        if any_rendered:
            if out and out[-1][1] is None:
                out[-1] = (out[-1][0], d.pts_ms, out[-1][2])
            bbox = img.getbbox()
            if bbox:
                img = img.crop(bbox)
            out.append((d.pts_ms, None, img))
        else:
            if out and out[-1][1] is None:
                out[-1] = (out[-1][0], d.pts_ms, out[-1][2])

    finalized: list[tuple[int, int, object]] = []
    for start, end, im in out:
        if end is None:
            end = start + 5000
        if end > start:
            finalized.append((start, end, im))
    return finalized
