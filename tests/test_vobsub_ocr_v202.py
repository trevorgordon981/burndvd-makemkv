"""Unit tests for native DVD VobSub (S_VOBSUB / dvd_subtitle) OCR support."""

from __future__ import annotations

import io
import json
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _compat import subocr
from burndvd.subocr import vobsub

try:
    from PIL import Image  # noqa: F401
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def _encode_rle_nibbles(runs: list[tuple[int, int]]) -> bytes:
    """Encode `(length, color_2bit)` runs into DVD SPU 1/2/3/4-nibble RLE bytes, byte-aligned."""
    nibbles: list[int] = []
    for length, color in runs:
        c = color & 0x03
        if length == 0:
            # 4-nibble end-of-line fill
            code = c
            nibbles.extend([(code >> 12) & 0xF, (code >> 8) & 0xF, (code >> 4) & 0xF, code & 0xF])
        elif 1 <= length <= 3:
            code = (length << 2) | c
            nibbles.append(code & 0xF)
        elif 4 <= length <= 15:
            code = (length << 2) | c
            nibbles.extend([(code >> 4) & 0xF, code & 0xF])
        elif 16 <= length <= 63:
            code = (length << 2) | c
            nibbles.extend([(code >> 8) & 0xF, (code >> 4) & 0xF, code & 0xF])
        elif 64 <= length <= 255:
            code = (length << 2) | c
            nibbles.extend([(code >> 12) & 0xF, (code >> 8) & 0xF, (code >> 4) & 0xF, code & 0xF])
        else:
            raise ValueError(f"unsupported test run length: {length}")
    if len(nibbles) % 2 == 1:
        nibbles.append(0)
    out = bytearray()
    for i in range(0, len(nibbles), 2):
        out.append((nibbles[i] << 4) | nibbles[i + 1])
    return bytes(out)


def _encode_field_rows(row_runs: list[list[tuple[int, int]]]) -> bytes:
    return b"".join(_encode_rle_nibbles(r) for r in row_runs)


def _build_spu(
    *,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    top_rle: bytes,
    bot_rle: bytes,
    color_map: tuple[int, int, int, int] = (0, 1, 2, 3),  # (b, p, e1, e2)
    alpha_nibbles: tuple[int, int, int, int] = (0, 15, 15, 15),  # (b, p, e1, e2)
    forced: bool = False,
    start_date: int = 0,
    stop_date: int | None = 180,
    include_chg_colcon: bool = False,
) -> bytes:
    """Build a valid binary DVD SPU packet with header, RLE fields, and SP_DCSQT."""
    top_off = 4
    bot_off = top_off + len(top_rle)
    ctrl_off = bot_off + len(bot_rle)

    b_c, p_c, e1_c, e2_c = color_map
    b_a, p_a, e1_a, e2_a = alpha_nibbles
    color_bytes = bytes([(e2_c << 4) | e1_c, (p_c << 4) | b_c])
    alpha_bytes = bytes([(e2_a << 4) | e1_a, (p_a << 4) | b_a])
    darea = bytes([
        (x1 >> 4) & 0xFF,
        ((x1 & 0x0F) << 4) | ((x2 >> 8) & 0x0F),
        x2 & 0xFF,
        (y1 >> 4) & 0xFF,
        ((y1 & 0x0F) << 4) | ((y2 >> 8) & 0x0F),
        y2 & 0xFF,
    ])
    dspxa = struct.pack(">HH", top_off, bot_off)

    cmds0 = bytearray()
    cmds0.append(vobsub.CMD_FSTA_DSP if forced else vobsub.CMD_STA_DSP)
    cmds0.append(vobsub.CMD_SET_COLOR)
    cmds0.extend(color_bytes)
    cmds0.append(vobsub.CMD_SET_CONTR)
    cmds0.extend(alpha_bytes)
    cmds0.append(vobsub.CMD_SET_DAREA)
    cmds0.extend(darea)
    cmds0.append(vobsub.CMD_SET_DSPXA)
    cmds0.extend(dspxa)
    if include_chg_colcon:
        # 0x07 + 2-byte length (including the 2 length bytes) + 4 dummy payload bytes
        cmds0.append(vobsub.CMD_CHG_COLCON)
        cmds0.extend(struct.pack(">H", 6))
        cmds0.extend(b"\x00\x01\x02\x03")
    cmds0.append(vobsub.CMD_END)

    if stop_date is not None:
        dcsq1_off = ctrl_off + 4 + len(cmds0)
        dcsq0 = struct.pack(">HH", start_date, dcsq1_off) + bytes(cmds0)
        dcsq1 = struct.pack(">HH", stop_date, dcsq1_off) + bytes([vobsub.CMD_STP_DSP, vobsub.CMD_END])
        ctrl_table = dcsq0 + dcsq1
    else:
        dcsq0 = struct.pack(">HH", start_date, ctrl_off) + bytes(cmds0)
        ctrl_table = dcsq0

    total_size = ctrl_off + len(ctrl_table)
    return struct.pack(">HH", total_size, ctrl_off) + top_rle + bot_rle + ctrl_table


def _wrap_in_mpeg2_ps(spu: bytes, *, substream_id: int = 0x20, max_chunk: int = 2000) -> bytes:
    """Wrap an SPU packet across one or more 2048-byte MPEG-2 PS DVD sectors."""
    sectors = bytearray()
    off = 0
    first = True
    while off < len(spu):
        pack = b"\x00\x00\x01\xba" + bytes([0x44, 0, 0, 0, 0, 1, 0, 0, 0, 0xF8])
        pts_bytes = b"\x21\x00\x01\x00\x01" if first else b""
        pes_flags = bytes([0x81, 0x80 if first else 0x00, len(pts_bytes)])
        chunk = spu[off : off + max_chunk]
        off += len(chunk)
        first = False
        payload = pes_flags + pts_bytes + bytes([substream_id]) + chunk
        pes = b"\x00\x00\x01\xbd" + struct.pack(">H", len(payload)) + payload
        sector = bytearray(pack + pes)
        # Pad sector to 2048 bytes with a padding PES stream when needed
        if len(sector) < 2048:
            rem = 2048 - len(sector)
            if rem >= 6:
                sector.extend(b"\x00\x00\x01\xbe" + struct.pack(">H", rem - 6) + b"\xff" * (rem - 6))
            else:
                sector.extend(b"\x00" * rem)
        sectors.extend(sector)
    return bytes(sectors)


class TestVobSubOcrV202(unittest.TestCase):
    def test_01_rle_1_2_3_4_nibble_and_eol_fill_interlaced(self):
        # Width=72, Height=2 (top field row 0, bottom field row 1)
        # Row 0: 1-nibble (run=2, c=1) + 2-nibble (run=5, c=2) + 3-nibble (run=20, c=3) + EOL fill (run=0, c=1 -> 45 px)
        top_rle = _encode_field_rows([[(2, 1), (5, 2), (20, 3), (0, 1)]])
        # Row 1: 4-nibble (run=68, c=2) + 2-nibble (run=4, c=3)
        bot_rle = _encode_field_rows([[(68, 2), (4, 3)]])
        spu = _build_spu(x1=10, y1=20, x2=81, y2=21, top_rle=top_rle, bot_rle=bot_rle)

        ctrl = vobsub.parse_spu_control(spu)
        self.assertIsNotNone(ctrl)
        self.assertEqual((ctrl.width, ctrl.height), (72, 2))
        decoded = vobsub.decode_spu_rle(spu, ctrl.top_off, ctrl.bot_off, 72, 2)
        row0 = list(decoded[:72])
        row1 = list(decoded[72:])
        self.assertEqual(row0, [1] * 2 + [2] * 5 + [3] * 20 + [1] * 45)
        self.assertEqual(row1, [2] * 68 + [3] * 4)

    @unittest.skipUnless(HAS_PIL, "Pillow required for VobSub rendering tests")
    def test_02_palette_alpha_and_chg_colcon_skip(self):
        # 4x2 subtitle: row 0 = [0, 1, 2, 3], row 1 = [3, 2, 1, 0]
        top_rle = _encode_field_rows([[(1, 0), (1, 1), (1, 2), (1, 3)]])
        bot_rle = _encode_field_rows([[(1, 3), (1, 2), (1, 1), (1, 0)]])
        spu = _build_spu(
            x1=0,
            y1=0,
            x2=3,
            y2=1,
            top_rle=top_rle,
            bot_rle=bot_rle,
            color_map=(0, 3, 4, 8),  # b=0, p=3 (fdfdfd), e1=4 (64cff7), e2=8 (a62c07)
            alpha_nibbles=(0, 15, 10, 5),  # b=0, p=255, e1=170, e2=85
            include_chg_colcon=True,
        )
        ctrl = vobsub.parse_spu_control(spu)
        self.assertIsNotNone(ctrl)
        self.assertEqual(ctrl.color_map, (0, 3, 4, 8))
        self.assertEqual(ctrl.alpha_map, (0, 255, 170, 85))

        palette = [(0, 0, 0)] * 16
        palette[3] = (0xFD, 0xFD, 0xFD)
        palette[4] = (0x64, 0xCF, 0xF7)
        palette[8] = (0xA6, 0x2C, 0x07)

        img = vobsub.render_spu_image(spu, ctrl, palette, pad=0)
        self.assertIsNotNone(img)
        self.assertEqual(img.size, (4, 2))
        self.assertEqual(img.getpixel((0, 0)), (0, 0, 0, 0))
        self.assertEqual(img.getpixel((1, 0)), (0xFD, 0xFD, 0xFD, 255))
        self.assertEqual(img.getpixel((2, 0)), (0x64, 0xCF, 0xF7, 170))
        self.assertEqual(img.getpixel((3, 0)), (0xA6, 0x2C, 0x07, 85))
        self.assertEqual(img.getpixel((0, 1)), (0xA6, 0x2C, 0x07, 85))
        self.assertEqual(img.getpixel((3, 1)), (0, 0, 0, 0))

    @unittest.skipUnless(HAS_PIL, "Pillow required for VobSub parsing tests")
    def test_03_forced_vs_normal_stp_dsp_timing_and_multi_sector_pes(self):
        with tempfile.TemporaryDirectory() as td:
            idx_path = Path(td) / "ep.idx"
            sub_path = Path(td) / "ep.sub"

            # Cue 1: normal (STA_DSP), explicit STP_DSP date=180 (~2048 ms), split across 2 PES sectors
            top1 = _encode_field_rows([[(4, 1)], [(4, 1)]])
            bot1 = _encode_field_rows([[(4, 1)], [(4, 1)]])
            spu1 = _build_spu(
                x1=0, y1=0, x2=3, y2=3, top_rle=top1, bot_rle=bot1,
                forced=False, start_date=0, stop_date=180,
            )
            # Force multi-sector split by using max_chunk=20
            pkt1 = _wrap_in_mpeg2_ps(spu1, substream_id=0x20, max_chunk=20)
            self.assertGreater(len(pkt1), 2048)

            # Cue 2: forced (FSTA_DSP), NO STP_DSP -> should cap at Cue 3's start_ms (12500)
            spu2 = _build_spu(
                x1=0, y1=0, x2=3, y2=3, top_rle=top1, bot_rle=bot1,
                forced=True, start_date=0, stop_date=None,
            )
            pkt2 = _wrap_in_mpeg2_ps(spu2, substream_id=0x20)

            # Cue 3: normal (STA_DSP), NO STP_DSP, last cue -> should cap at start + 5000ms
            spu3 = _build_spu(
                x1=0, y1=0, x2=3, y2=3, top_rle=top1, bot_rle=bot1,
                forced=False, start_date=0, stop_date=None,
            )
            pkt3 = _wrap_in_mpeg2_ps(spu3, substream_id=0x20)

            pos1 = 0
            pos2 = len(pkt1)
            pos3 = pos2 + len(pkt2)
            sub_path.write_bytes(pkt1 + pkt2 + pkt3)

            idx_text = "\n".join([
                "# VobSub index file, v7",
                "size: 720x480",
                "langidx: 0",
                "palette: 000000, ffffff, 808080, cccccc, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000",
                "id: en, index: 0",
                f"timestamp: 00:00:01:000, filepos: {pos1:09x}",
                f"timestamp: 00:00:10:000, filepos: {pos2:09x}",
                f"timestamp: 00:00:12:500, filepos: {pos3:09x}",
            ])
            idx_path.write_text(idx_text, encoding="utf-8")

            all_cues = vobsub.parse_vobsub_cues(idx_path)
            self.assertEqual(len(all_cues), 3)
            self.assertEqual((all_cues[0].start_ms, all_cues[0].end_ms, all_cues[0].forced), (1000, 3048, False))
            self.assertEqual((all_cues[1].start_ms, all_cues[1].end_ms, all_cues[1].forced), (10000, 12500, True))
            self.assertEqual((all_cues[2].start_ms, all_cues[2].end_ms, all_cues[2].forced), (12500, 17500, False))

            forced_frames = vobsub.parse_vobsub(idx_path, forced_only=True)
            self.assertEqual(len(forced_frames), 1)
            self.assertEqual((forced_frames[0][0], forced_frames[0][1]), (10000, 12500))

    @unittest.skipUnless(HAS_PIL, "Pillow required for multi-track idx test")
    def test_04_idx_multiple_tracks_respects_language_and_index(self):
        with tempfile.TemporaryDirectory() as td:
            idx_path = Path(td) / "multi.idx"
            sub_path = Path(td) / "multi.sub"

            top = _encode_field_rows([[(4, 1)]])
            bot = _encode_field_rows([[(4, 1)]])
            spu_en = _build_spu(x1=0, y1=0, x2=3, y2=1, top_rle=top, bot_rle=bot, stop_date=90)
            spu_es = _build_spu(x1=0, y1=0, x2=3, y2=1, top_rle=top, bot_rle=bot, stop_date=180)
            pkt_en = _wrap_in_mpeg2_ps(spu_en, substream_id=0x20)
            pkt_es = _wrap_in_mpeg2_ps(spu_es, substream_id=0x21)
            sub_path.write_bytes(pkt_en + pkt_es)

            idx_path.write_text("\n".join([
                "size: 720x480",
                "langidx: 0",
                "palette: 000000, ffffff, 808080, cccccc, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000, 000000",
                "id: en, index: 0",
                "timestamp: 00:00:02:000, filepos: 000000000",
                "id: es, index: 1",
                f"timestamp: 00:00:05:000, filepos: {len(pkt_en):09x}",
            ]), encoding="utf-8")

            es_cues = vobsub.parse_vobsub(idx_path, lang="spa")
            self.assertEqual(len(es_cues), 1)
            self.assertEqual((es_cues[0][0], es_cues[0][1]), (5000, 7048))

            en_cues = vobsub.parse_vobsub(idx_path, track_index=0)
            self.assertEqual(len(en_cues), 1)
            self.assertEqual((en_cues[0][0], en_cues[0][1]), (2000, 3024))

    def test_05_track_selection_prefers_pgs_and_falls_back_to_vobsub(self):
        pgs_full = subocr.TrackInfo(index=2, codec="hdmv_pgs_subtitle", lang="eng", title="", size_bytes=500_000)
        vob_full = subocr.TrackInfo(index=3, codec="dvd_subtitle", lang="eng", title="", size_bytes=400_000)
        vob_forced = subocr.TrackInfo(index=4, codec="dvd_subtitle", lang="eng", title="", forced=True, size_bytes=12_000)
        pgs_forced = subocr.TrackInfo(index=5, codec="hdmv_pgs_subtitle", lang="eng", title="", forced=True, size_bytes=15_000)

        # Full mode with both PGS and VobSub -> prefers PGS
        p_both = subocr.pick_sub_track([vob_full, pgs_full], "eng", "full", Path("/tmp/a.mkv"))
        self.assertEqual(p_both.index, 2)
        self.assertEqual(p_both.codec, "hdmv_pgs_subtitle")

        # Forced mode with both forced PGS and forced VobSub -> prefers PGS
        p_forced_both = subocr.pick_sub_track([vob_forced, pgs_forced], "eng", "forced", Path("/tmp/a.mkv"))
        self.assertEqual(p_forced_both.index, 5)
        self.assertFalse(p_forced_both.forced_only)

        # DVD MKV with VobSub only (full mode) -> selects VobSub
        p_dvd_full = subocr.pick_sub_track([vob_full], "eng", "full", Path("/tmp/dvd.mkv"))
        self.assertEqual(p_dvd_full.index, 3)
        self.assertEqual(p_dvd_full.codec, "dvd_subtitle")
        self.assertFalse(p_dvd_full.forced_only)

        # DVD MKV with single non-forced VobSub in forced mode -> selects VobSub with forced_only=True
        p_dvd_forced = subocr.pick_sub_track([vob_full], "eng", "forced", Path("/tmp/dvd.mkv"))
        self.assertEqual(p_dvd_forced.index, 3)
        self.assertTrue(p_dvd_forced.forced_only)

        # Empty PGS + non-empty VobSub -> falls back to VobSub
        pgs_empty = subocr.TrackInfo(index=6, codec="hdmv_pgs_subtitle", lang="eng", title="", size_bytes=0)
        p_fallback = subocr.pick_sub_track([pgs_empty, vob_full], "eng", "full", Path("/tmp/hybrid.mkv"))
        self.assertEqual(p_fallback.index, 3)

    def test_06_probe_tracks_detects_s_vobsub_as_dvd_subtitle(self):
        mkvmerge_json = {
            "tracks": [
                {"id": 0, "type": "video", "codec": "MPEG-1/2", "properties": {"codec_id": "V_MPEG2"}},
                {"id": 1, "type": "audio", "codec": "AC-3", "properties": {"codec_id": "A_AC3", "language": "eng"}},
                {
                    "id": 2,
                    "type": "subtitles",
                    "codec": "VobSub",
                    "properties": {
                        "codec_id": "S_VOBSUB",
                        "language": "eng",
                        "forced_track": False,
                        "tag_number_of_bytes": "1504852",
                    },
                },
            ]
        }
        with mock.patch("subprocess.check_output", return_value=json.dumps(mkvmerge_json)):
            audio, subs = subocr.probe_tracks(Path("/tmp/sp.mkv"))
        self.assertEqual(len(audio), 1)
        self.assertEqual(len(subs), 1)
        self.assertEqual((subs[0].index, subs[0].codec, subs[0].lang, subs[0].size_bytes), (2, "dvd_subtitle", "eng", 1504852))

    def test_07_pipeline_process_mkv_dispatches_vobsub_and_dedups_cues(self):
        with tempfile.TemporaryDirectory() as td:
            mkv = Path(td) / "dvd_ep.mkv"
            mkv.write_bytes(b"fake")
            srt = Path(td) / "dvd_ep.en.srt"
            sub = subocr.TrackInfo(index=2, codec="dvd_subtitle", lang="eng", title="", size_bytes=10000)

            def fake_extract(cmd, capture_output=True, text=True):
                idx_arg = Path(cmd[-1].split(":", 1)[1])
                idx_arg.write_text("# idx\n", encoding="utf-8")
                idx_arg.with_suffix(".sub").write_bytes(b"\x00" * 64)
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            fake_frames = [
                (1000, 2000, object()),
                (2050, 3200, object()),  # duplicate within 150ms gap -> merged
                (5000, 6500, object()),
            ]
            args = SimpleNamespace(overwrite=False, no_propedit=True, dry_run=False, show=None)
            settings = {"audio": "eng", "sub_track": "full", "sub_langs": ["eng"]}
            with mock.patch.object(subocr, "probe_tracks", return_value=([], [sub])), \
                 mock.patch("subprocess.run", side_effect=fake_extract), \
                 mock.patch.object(subocr.pipeline, "parse_vobsub", return_value=fake_frames), \
                 mock.patch.object(subocr.pipeline, "ocr_image", side_effect=["Hello there", "Hello there", "Second line"]):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    ok = subocr.process_mkv(mkv, settings, args)
            self.assertTrue(ok)
            self.assertTrue(srt.exists())
            srt_txt = srt.read_text(encoding="utf-8")
            self.assertIn("00:00:01,000 --> 00:00:03,200\nHello there", srt_txt)
            self.assertIn("00:00:05,000 --> 00:00:06,500\nSecond line", srt_txt)
            self.assertEqual(srt_txt.count("Hello there"), 1)


if __name__ == "__main__":
    unittest.main()
