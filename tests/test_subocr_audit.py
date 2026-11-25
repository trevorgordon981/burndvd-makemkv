"""Comprehensive unit tests covering all 14 verified P0/P1/P2 fixes in burndvd.subocr."""

from __future__ import annotations

import fcntl
import io
import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from burndvd import compat_subocr as subocr

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    from PIL import Image  # noqa: F401
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def _make_pgs_seg(pts_ms: int, stype: int, body: bytes) -> bytes:
    pts = pts_ms * (subocr.PTS_HZ // 1000)
    return b"PG" + struct.pack(">IIBH", pts, 0, stype, len(body)) + body


def _make_pds(pts_ms: int, pal_id: int, entries: list[tuple[int, int, int, int, int]]) -> bytes:
    body = bytes([pal_id, 0]) + b"".join(bytes(e) for e in entries)
    return _make_pgs_seg(pts_ms, subocr.SEG_PDS, body)


def _make_ods(pts_ms: int, obj_id: int, width: int, height: int, rle: bytes) -> bytes:
    odl = 4 + len(rle)
    body = struct.pack(">HBB", obj_id, 0, 0xC0)
    body += bytes([(odl >> 16) & 0xFF, (odl >> 8) & 0xFF, odl & 0xFF])
    body += struct.pack(">HH", width, height) + rle
    return _make_pgs_seg(pts_ms, subocr.SEG_ODS, body)


def _make_pcs(pts_ms: int, w: int, h: int, pal_id: int, comps) -> bytes:
    body = struct.pack(">HHBHBBBB", w, h, 0x10, 0, 0x80, 0, pal_id, len(comps))
    for obj_id, win_id, forced, ox, oy, crop in comps:
        flags = (0x40 if forced else 0) | (0x80 if crop is not None else 0)
        body += struct.pack(">HBBHH", obj_id, win_id, flags, ox, oy)
        if crop is not None:
            body += struct.pack(">HHHH", *crop)
    return _make_pgs_seg(pts_ms, subocr.SEG_PCS, body)


def _make_end(pts_ms: int) -> bytes:
    return _make_pgs_seg(pts_ms, subocr.SEG_END, b"")


def _encode_solid_rle(width: int, height: int, color: int) -> bytes:
    row = bytes([color] * width) + b"\x00\x00"
    return row * height


class TestSubocrAuditFixes(unittest.TestCase):
    def test_01a_write_srt_atomic_and_fsync(self):
        with tempfile.TemporaryDirectory() as td:
            srt = Path(td) / "ep.en.srt"
            with mock.patch("os.fsync", wraps=os.fsync) as m_fsync, \
                 mock.patch("os.replace", wraps=os.replace) as m_replace:
                subocr.write_srt([(1000, 2500, "Hello world")], srt)
            self.assertTrue(srt.exists())
            self.assertIn("Hello world", srt.read_text(encoding="utf-8"))
            self.assertEqual(m_fsync.call_count, 1)
            self.assertEqual(m_replace.call_count, 1)
            self.assertEqual(list(Path(td).glob("*.part.*")), [])

    def test_01b_write_srt_cleanup_on_failure_preserves_existing(self):
        with tempfile.TemporaryDirectory() as td:
            srt = Path(td) / "ep.en.srt"
            srt.write_text("ORIGINAL CONTENT\n", encoding="utf-8")
            with mock.patch("os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    subocr.write_srt([(1000, 2000, "New")], srt)
            self.assertEqual(srt.read_text(encoding="utf-8"), "ORIGINAL CONTENT\n")
            self.assertEqual(list(Path(td).glob("*.part.*")), [])

    def test_01c_ocr_pgs_track_no_zero_byte_srt_when_empty_cues(self):
        with tempfile.TemporaryDirectory() as td:
            mkv = Path(td) / "ep.mkv"
            mkv.write_bytes(b"fake")
            srt = Path(td) / "ep.en.srt"
            track = subocr.TrackInfo(index=2, codec="hdmv_pgs_subtitle", lang="eng", title="")

            def fake_extract(cmd, capture_output=True, text=True):
                sup_arg = cmd[-1].split(":", 1)[1]
                Path(sup_arg).write_bytes(b"PGdummy")
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            fake_frames = [(1000, 2000, object())]
            with mock.patch("subprocess.run", side_effect=fake_extract), \
                 mock.patch.object(subocr, "parse_sup", return_value=fake_frames), \
                 mock.patch.object(subocr, "ocr_image", return_value=""):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    ok = subocr.ocr_pgs_track(mkv, track, srt, "eng")
            self.assertFalse(ok)
            self.assertFalse(srt.exists())

    def test_01d_process_mkv_does_not_skip_existing_zero_byte_srt(self):
        with tempfile.TemporaryDirectory() as td:
            mkv = Path(td) / "ep.mkv"
            mkv.write_bytes(b"fake")
            srt = Path(td) / "ep.en.srt"
            srt.write_bytes(b"")
            sub = subocr.TrackInfo(index=2, codec="hdmv_pgs_subtitle", lang="eng", title="", size_bytes=5000)
            args = SimpleNamespace(overwrite=False, no_propedit=True, dry_run=False, show=None)
            settings = {"audio": "eng", "sub_track": "full", "sub_langs": ["eng"]}
            with mock.patch.object(subocr, "probe_tracks", return_value=([], [sub])), \
                 mock.patch.object(subocr, "ocr_pgs_track", return_value=True) as m_ocr:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    ok = subocr.process_mkv(mkv, settings, args)
            self.assertTrue(ok)
            m_ocr.assert_called_once()

    def test_02_set_track_defaults_none_audio_preserves_audio_flags(self):
        audio = [
            subocr.TrackInfo(index=1, codec="ac3", lang="eng", title="", mkv_pos=2),
            subocr.TrackInfo(index=2, codec="ac3", lang="spa", title="", mkv_pos=3),
        ]
        subs = [subocr.TrackInfo(index=3, codec="hdmv_pgs_subtitle", lang="eng", title="", mkv_pos=4)]
        with mock.patch.object(subocr, "probe_tracks", return_value=(audio, subs)), \
             mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as m_run:
            ok = subocr.set_track_defaults(Path("/tmp/fake.mkv"), None, None, dry_run=False)
        self.assertTrue(ok)
        cmd = m_run.call_args[0][0]
        self.assertNotIn("track:2", cmd)
        self.assertNotIn("track:3", cmd)
        self.assertIn("track:4", cmd)

    def test_03_wrapper_and_library_prepend_homebrew_path(self):
        env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT / "src")}
        out = subprocess.check_output(
            [sys.executable, "-c", "import os; import burndvd.subocr; print(os.environ['PATH'])"],
            env=env,
            text=True,
        ).strip()
        parts = out.split(os.pathsep)
        self.assertEqual(parts[0], "/opt/homebrew/bin")
        self.assertEqual(parts[1], "/usr/local/bin")
        wrapper_txt = (REPO_ROOT / "bin" / "subocr").read_text()
        self.assertIn('export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"', wrapper_txt)

    def test_04_flock_mutual_exclusion_and_sigkill_autorelease(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "nested" / "subocr.flock"
            fh = subocr.acquire_lock(lock_path)
            self.assertTrue(lock_path.exists())
            try:
                with lock_path.open("a+") as fh2:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fh2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                fh.close()
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    f"import sys, time; sys.path.insert(0, {str(REPO_ROOT / 'src')!r}); "
                    f"from burndvd.subocr import acquire_lock; from pathlib import Path; "
                    f"fh = acquire_lock(Path({str(lock_path)!r})); "
                    f"print('LOCKED', flush=True); time.sleep(30)",
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(proc.stdout.readline().strip(), "LOCKED")
            finally:
                proc.kill()
                proc.wait(timeout=5)
                proc.stdout.close()
            with lock_path.open("a+") as fh3:
                fcntl.flock(fh3.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh3.fileno(), fcntl.LOCK_UN)

    @unittest.skipUnless(HAS_PIL, "Pillow required for PGS image rendering tests")
    def test_05_consecutive_clearing_pcs_does_not_overwrite_end_pts(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Path(td) / "test.sup"
            raw = b"".join(
                [
                    _make_pcs(1000, 16, 16, 0, [(0, 0, False, 0, 0, None)]),
                    _make_pds(1000, 0, [(1, 235, 128, 128, 255)]),
                    _make_ods(1000, 0, 4, 4, _encode_solid_rle(4, 4, 1)),
                    _make_end(1000),
                    _make_pcs(3000, 16, 16, 0, []),
                    _make_end(3000),
                    _make_pcs(9000, 16, 16, 0, []),
                    _make_end(9000),
                    _make_pcs(12000, 16, 16, 0, [(99, 0, False, 0, 0, None)]),
                    _make_end(12000),
                ]
            )
            sup.write_bytes(raw)
            frames = subocr.parse_sup(sup)
            self.assertEqual(len(frames), 1)
            self.assertEqual((frames[0][0], frames[0][1]), (1000, 3000))

    def test_06_iso639_2b_to_2t_normalization(self):
        expected = {
            "chi": "zho", "fre": "fra", "ger": "deu", "dut": "nld",
            "cze": "ces", "rum": "ron", "gre": "ell", "mac": "mkd",
        }
        for b_code, t_code in expected.items():
            self.assertEqual(subocr._to_iso639_2(b_code), t_code)
            self.assertIn(t_code, subocr.LANG_639_1)
        sub_chi = subocr.TrackInfo(index=5, codec="hdmv_pgs_subtitle", lang="chi", title="", size_bytes=1000)
        picked = subocr.pick_sub_track([sub_chi], "zho", "full", Path("/tmp/fake.mkv"))
        self.assertIsNotNone(picked)
        self.assertEqual(picked.index, 5)

    def test_07_dry_run_never_writes_srt(self):
        with tempfile.TemporaryDirectory() as td:
            mkv = Path(td) / "ep.mkv"
            mkv.write_bytes(b"fake")
            srt = Path(td) / "ep.en.srt"
            audio = [subocr.TrackInfo(index=1, codec="ac3", lang="eng", title="", mkv_pos=2)]
            subs = [subocr.TrackInfo(index=2, codec="hdmv_pgs_subtitle", lang="eng", title="", mkv_pos=3, size_bytes=50000)]
            args = SimpleNamespace(overwrite=False, no_propedit=False, dry_run=True, show=None)
            settings = {"audio": "eng", "sub_track": "full", "sub_langs": ["eng"]}
            with mock.patch.object(subocr, "probe_tracks", return_value=(audio, subs)), \
                 mock.patch.object(subocr, "ocr_pgs_track") as m_ocr:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    ok = subocr.process_mkv(mkv, settings, args)
            self.assertTrue(ok)
            m_ocr.assert_not_called()
            self.assertFalse(srt.exists())

    def test_08a_forced_mode_ignores_empty_prefers_explicit_and_single_fallback(self):
        t_empty = subocr.TrackInfo(index=10, codec="hdmv_pgs_subtitle", lang="eng", title="", forced=True, size_bytes=0)
        t_small_nf = subocr.TrackInfo(index=11, codec="hdmv_pgs_subtitle", lang="eng", title="", forced=False, size_bytes=5000)
        t_explicit = subocr.TrackInfo(index=12, codec="hdmv_pgs_subtitle", lang="eng", title="", forced=True, size_bytes=20000)
        t_full = subocr.TrackInfo(index=13, codec="hdmv_pgs_subtitle", lang="eng", title="", forced=False, size_bytes=5000000)
        p1 = subocr.pick_sub_track([t_empty, t_small_nf, t_explicit, t_full], "eng", "forced", Path("/tmp/x.mkv"))
        self.assertEqual(p1.index, 12)
        self.assertFalse(p1.forced_only)

        t_zero = subocr.TrackInfo(index=20, codec="hdmv_pgs_subtitle", lang="eng", title="", forced=False, size_bytes=0)
        t_only = subocr.TrackInfo(index=21, codec="hdmv_pgs_subtitle", lang="eng", title="", forced=False, size_bytes=9000000)
        p2 = subocr.pick_sub_track([t_zero, t_only], "eng", "forced", Path("/tmp/x.mkv"))
        self.assertEqual(p2.index, 21)
        self.assertTrue(p2.forced_only)

    @unittest.skipUnless(HAS_PIL, "Pillow required for PGS image rendering tests")
    def test_08b_parse_sup_forced_only_filters_dialogue_cues(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Path(td) / "mixed.sup"
            raw = b"".join(
                [
                    _make_pcs(1000, 16, 16, 0, [(0, 0, False, 0, 0, None)]),
                    _make_pds(1000, 0, [(1, 235, 128, 128, 255)]),
                    _make_ods(1000, 0, 4, 4, _encode_solid_rle(4, 4, 1)),
                    _make_end(1000),
                    _make_pcs(2000, 16, 16, 0, []),
                    _make_end(2000),
                    _make_pcs(5000, 16, 16, 0, [(0, 0, True, 0, 0, None)]),
                    _make_end(5000),
                    _make_pcs(7500, 16, 16, 0, [(0, 0, False, 0, 0, None)]),
                    _make_end(7500),
                    _make_pcs(9000, 16, 16, 0, []),
                    _make_end(9000),
                ]
            )
            sup.write_bytes(raw)
            self.assertEqual(len(subocr.parse_sup(sup, forced_only=False)), 3)
            forced_frames = subocr.parse_sup(sup, forced_only=True)
            self.assertEqual(len(forced_frames), 1)
            self.assertEqual((forced_frames[0][0], forced_frames[0][1]), (5000, 7500))

    def test_09_decode_rle_literal_overflow_guarded(self):
        rle = bytes([1, 2, 3, 9, 9, 0, 0, 4, 5, 6, 8, 8, 8, 0, 0])
        decoded = subocr._decode_rle(rle, 3, 2)
        self.assertEqual(list(decoded), [1, 2, 3, 4, 5, 6])

    @unittest.skipUnless(HAS_PIL, "Pillow required for PGS image rendering tests")
    def test_10_pgs_composition_crop_applied_and_clamped(self):
        with tempfile.TemporaryDirectory() as td:
            sup = Path(td) / "crop.sup"
            rle = bytes([1, 1, 2, 2, 0, 0, 1, 1, 2, 2, 0, 0])
            pds = _make_pds(1000, 0, [(1, 82, 240, 90, 255), (2, 41, 110, 240, 255)])
            raw = b"".join(
                [
                    _make_pcs(1000, 16, 16, 0, [(0, 0, False, 4, 4, (2, 0, 10, 10))]),
                    pds,
                    _make_ods(1000, 0, 4, 2, rle),
                    _make_end(1000),
                    _make_pcs(2000, 16, 16, 0, []),
                    _make_end(2000),
                ]
            )
            sup.write_bytes(raw)
            frames = subocr.parse_sup(sup)
            self.assertEqual(len(frames), 1)
            img = frames[0][2]
            self.assertEqual(img.size, (2, 2))
            for y in range(2):
                for x in range(2):
                    r, g, b, a = img.getpixel((x, y))
                    self.assertGreater(b, r)
                    self.assertEqual(a, 255)

    def test_11_set_track_defaults_zero_edits_and_error_propagation(self):
        with mock.patch.object(subocr, "probe_tracks", return_value=([], [])), \
             mock.patch("subprocess.run") as m_run:
            ok = subocr.set_track_defaults(Path("/tmp/empty.mkv"), None, None, dry_run=False)
        self.assertTrue(ok)
        m_run.assert_not_called()

        sub = subocr.TrackInfo(index=1, codec="hdmv_pgs_subtitle", lang="eng", title="", mkv_pos=2)
        fail_res = SimpleNamespace(returncode=2, stderr="", stdout="Error: file is read-only")
        with mock.patch.object(subocr, "probe_tracks", return_value=([], [sub])), \
             mock.patch("subprocess.run", return_value=fail_res):
            err_buf = io.StringIO()
            with redirect_stderr(err_buf):
                ok = subocr.set_track_defaults(Path("/tmp/ro.mkv"), None, None, dry_run=False)
        self.assertFalse(ok)
        self.assertIn("Error: file is read-only", err_buf.getvalue())

    def test_12_show_re_matches_tv_shows_and_4k(self):
        self.assertEqual(
            subocr.show_for_path(Path("/private/nas/media/TV Shows/One Punch Man/Season 01/ep.mkv")),
            "One Punch Man",
        )
        self.assertEqual(
            subocr.show_for_path(Path("/private/nas/media/TV Shows 4K/Andor/Season 01/Andor - S01E01.mkv")),
            "Andor",
        )
        self.assertIsNone(subocr.show_for_path(Path("/private/nas/media/Movies/Dune/Dune.mkv")))

    def test_13_sigterm_exits_143_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as td:
            marker_dir = Path(td) / "temp_cleanup_check"
            script = "\n".join(
                [
                    "import sys, time",
                    f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})",
                    "from burndvd import compat_subocr as subocr",
                    "from pathlib import Path",
                    "from unittest import mock",
                    f"d = Path({str(marker_dir)!r})",
                    "d.mkdir()",
                    "def slow_process(*a, **kw):",
                    "    try:",
                    "        print('READY', flush=True)",
                    "        time.sleep(30)",
                    "    finally:",
                    "        d.rmdir()",
                    f"mkv = Path({td!r}) / 'a.mkv'",
                    "mkv.write_bytes(b'x')",
                    "sys.argv = ['subocr', '--dry-run', str(mkv)]",
                    "with mock.patch.object(subocr, 'probe_tracks', return_value=([], [])), \\",
                    "     mock.patch.object(subocr, 'process_mkv', side_effect=slow_process):",
                    "    subocr.main()",
                ]
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(proc.stdout.readline().strip(), "READY")
                proc.send_signal(signal.SIGTERM)
                rc = proc.wait(timeout=5)
                self.assertEqual(rc, 143)
                self.assertFalse(marker_dir.exists())
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                proc.stdout.close()
                proc.stderr.close()

    def test_14_probe_tracks_uses_mkvmerge_ids_and_ffprobe_fallback(self):
        mkvmerge_json = {
            "attachments": [{"id": 1, "file_name": "font.ttf"}],
            "tracks": [
                {"id": 0, "type": "video", "codec": "AVC", "properties": {"codec_id": "V_MPEG4/ISO/AVC"}},
                {"id": 1, "type": "audio", "codec": "DTS-HD Master Audio", "properties": {"codec_id": "A_DTS", "language": "chi"}},
                {"id": 3, "type": "subtitles", "codec": "HDMV PGS", "properties": {"codec_id": "S_HDMV/PGS", "language": "chi", "forced_track": True, "tag_number_of_bytes": "12345"}},
            ],
        }
        with mock.patch("subprocess.check_output", return_value=json.dumps(mkvmerge_json)):
            audio, subs = subocr.probe_tracks(Path("/tmp/attached.mkv"))
        self.assertEqual((audio[0].index, audio[0].codec, audio[0].lang), (1, "dts-hd-ma", "zho"))
        self.assertEqual((subs[0].index, subs[0].lang, subs[0].forced, subs[0].size_bytes), (3, "zho", True, 12345))


if __name__ == "__main__":
    unittest.main()
