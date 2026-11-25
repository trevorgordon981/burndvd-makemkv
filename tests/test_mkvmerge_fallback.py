"""Comprehensive tests for the automatic mkvmerge unencrypted-BD fallback engine."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import burndvd
from burndvd import fallback, models, ripper


class FakePopen:
    def __init__(self, lines: list[str], returncode: int = 0):
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def _make_bdmv(root: Path, playlists: list[str], *, encrypted: bool = False, aacs_name: str = "AACS") -> Path:
    pl_dir = root / "BDMV" / "PLAYLIST"
    pl_dir.mkdir(parents=True, exist_ok=True)
    for name in playlists:
        (pl_dir / name).write_bytes(b"MPLS0200")
    if encrypted:
        aacs = root / aacs_name
        aacs.mkdir(exist_ok=True)
        (aacs / "Unit_Key_RO.inf").write_bytes(b"encrypted")
    return root


class TestMkvmergeFallbackEligibility(unittest.TestCase):
    def test_encrypted_aacs_disc_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = _make_bdmv(Path(tmp) / "ENC_BD", ["00001.mpls"], encrypted=True)
            ok, reason = fallback.can_use_mkvmerge_fallback(disc)
            self.assertFalse(ok)
            self.assertIn("AACS", reason)

    def test_lowercase_aacs_dir_also_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = _make_bdmv(Path(tmp) / "ENC_LOWER", ["00001.mpls"], encrypted=True, aacs_name="aacs")
            ok, reason = fallback.can_use_mkvmerge_fallback(disc)
            self.assertFalse(ok)
            self.assertIn("AACS", reason)

    def test_missing_bdmv_playlist_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = Path(tmp) / "DVD_DISC"
            (disc / "VIDEO_TS").mkdir(parents=True)
            ok, reason = fallback.can_use_mkvmerge_fallback(disc)
            self.assertFalse(ok)
            self.assertIn("BDMV/PLAYLIST", reason)

    def test_empty_playlist_dir_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = _make_bdmv(Path(tmp) / "EMPTY_BD", [], encrypted=False)
            ok, reason = fallback.can_use_mkvmerge_fallback(disc)
            self.assertFalse(ok)
            self.assertIn("no .mpls", reason)

    def test_unencrypted_bdmv_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = _make_bdmv(Path(tmp) / "UNENC_BD", ["00001.mpls", "00002.mpls"], encrypted=False)
            ok, reason = fallback.can_use_mkvmerge_fallback(disc)
            self.assertTrue(ok, reason)
            self.assertIn("2 playlist(s)", reason)


class TestDiscoverBdmvPlaylists(unittest.TestCase):
    def test_discovers_episodes_and_excludes_playall_dupes_and_shorts(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = _make_bdmv(
                Path(tmp) / "FMA_D5",
                ["00000.mpls", "00001.mpls", "00002.mpls", "00003.mpls", "00004.mpls", "00005.mpls"],
            )
            # 00000 = Play-All spanning 00001..00003 (72 min)
            # 00001..00003 = 3 real episodes (~24 min each)
            # 00004 = duplicate playlist pointing to same m2ts as 00001
            # 00005 = 5-min creditless OP extra (below min_dur_s)
            meta = {
                "00000.mpls": (4320.0, 15_000_000_000, ["00001.m2ts", "00002.m2ts", "00003.m2ts"]),
                "00001.mpls": (1440.0, 5_000_000_000, ["00001.m2ts"]),
                "00002.mpls": (1445.0, 5_000_000_000, ["00002.m2ts"]),
                "00003.mpls": (1435.0, 5_000_000_000, ["00003.m2ts"]),
                "00004.mpls": (1440.0, 5_000_000_000, ["00001.m2ts"]),
                "00005.mpls": (300.0, 800_000_000, ["00005.m2ts"]),
            }

            def fake_run(cmd, **kw):
                mpls_name = Path(cmd[-1]).name
                dur_s, size_b, files = meta[mpls_name]
                payload = {
                    "container": {
                        "properties": {
                            "playlist_duration": int(dur_s * 1e9),
                            "playlist_size": size_b,
                            "playlist_file": files,
                        }
                    },
                    "tracks": [
                        {"id": 0, "type": "video", "codec": "AVC"},
                        {"id": 1, "type": "audio", "codec": "TrueHD"},
                    ],
                }
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

            with mock.patch.object(fallback.subprocess, "run", side_effect=fake_run):
                found = fallback.discover_bdmv_playlists(disc, min_dur_s=900)

            self.assertEqual([p["playlist_id"] for p in found], ["00001", "00002", "00003"])

    def test_duration_playall_filtered_even_without_stream_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = _make_bdmv(
                Path(tmp) / "SHOW_BD",
                ["00001.mpls", "00002.mpls", "00003.mpls", "00010.mpls"],
            )
            durs = {
                "00001.mpls": 1440.0,
                "00002.mpls": 1440.0,
                "00003.mpls": 1440.0,
                "00010.mpls": 4320.0,
            }

            def fake_run(cmd, **kw):
                dur_s = durs[Path(cmd[-1]).name]
                payload = {
                    "container": {"properties": {"playlist_duration": int(dur_s * 1e9)}},
                    "tracks": [{"id": 0, "type": "video"}, {"id": 1, "type": "audio"}],
                }
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

            with mock.patch.object(fallback.subprocess, "run", side_effect=fake_run):
                found = fallback.discover_bdmv_playlists(disc, min_dur_s=900)

            self.assertEqual([p["playlist_id"] for p in found], ["00001", "00002", "00003"])

    def test_encrypted_disc_returns_empty_playlists(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = _make_bdmv(Path(tmp) / "ENC", ["00001.mpls"], encrypted=True)
            self.assertEqual(fallback.discover_bdmv_playlists(disc), [])

    def test_movie_mode_picks_single_longest_playlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = _make_bdmv(Path(tmp) / "MOV_BD", ["00001.mpls", "00002.mpls"])
            durs = {"00001.mpls": 1800.0, "00002.mpls": 6900.0}

            def fake_run(cmd, **kw):
                dur_s = durs[Path(cmd[-1]).name]
                payload = {
                    "container": {"properties": {"playlist_duration": int(dur_s * 1e9)}},
                    "tracks": [{"id": 0, "type": "video"}, {"id": 1, "type": "audio"}],
                }
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

            with mock.patch.object(fallback.subprocess, "run", side_effect=fake_run):
                found = fallback.discover_bdmv_playlists(disc, min_dur_s=900, item_type="movie")
            self.assertEqual([p["playlist_id"] for p in found], ["00002"])


class TestRemuxWithMkvmerge(unittest.TestCase):
    def test_tolerates_rc_1_interleaved_truehd_warning_and_verifies_ffprobe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mpls = root / "00001.mpls"
            mpls.write_bytes(b"MPLS")
            staging = root / "staging"
            playlists = [{"mpls_path": mpls, "playlist_id": "00001", "duration_s": 1440.0}]

            def fake_mkvmerge(cmd, **kw):
                out_path = Path(cmd[cmd.index("-o") + 1])
                out_path.write_bytes(b"valid-remuxed-mkv")
                return SimpleNamespace(
                    returncode=1,
                    stdout="Warning: TrueHD/AC-3 interleaved stream frame warning",
                    stderr="",
                )

            with mock.patch.object(fallback.subprocess, "run", side_effect=fake_mkvmerge), \
                 mock.patch.object(
                     fallback,
                     "ffprobe_streams",
                     return_value=(1440.5, [{"codec_type": "video"}, {"codec_type": "audio"}]),
                 ):
                res = fallback.remux_playlists_with_mkvmerge(
                    playlists, staging, "Fullmetal Alchemist"
                )

            self.assertTrue(res.ok, res.error)
            self.assertEqual(len(res.files), 1)
            self.assertEqual(res.files[0].name, "Fullmetal Alchemist_t00.mkv")
            self.assertEqual(len(res.warnings), 1)
            self.assertIn("exited 1", res.warnings[0])

    def test_fails_closed_on_rc_2_and_cleans_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mpls = root / "00001.mpls"
            mpls.write_bytes(b"MPLS")
            staging = root / "staging"
            playlists = [{"mpls_path": mpls, "playlist_id": "00001", "duration_s": 1440.0}]

            def fake_mkvmerge(cmd, **kw):
                out_path = Path(cmd[cmd.index("-o") + 1])
                out_path.write_bytes(b"corrupt-partial")
                return SimpleNamespace(returncode=2, stdout="", stderr="Error: read error on m2ts")

            with mock.patch.object(fallback.subprocess, "run", side_effect=fake_mkvmerge):
                res = fallback.remux_playlists_with_mkvmerge(playlists, staging, "Show")

            self.assertFalse(res.ok)
            self.assertIn("fatal exit 2", res.error)
            self.assertEqual(list(staging.glob("*.mkv")), [])

    def test_fails_closed_on_ffprobe_invalid_streams_or_zero_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mpls = root / "00001.mpls"
            mpls.write_bytes(b"MPLS")
            staging = root / "staging"
            playlists = [{"mpls_path": mpls, "playlist_id": "00001", "duration_s": 1440.0}]

            def fake_mkvmerge(cmd, **kw):
                out_path = Path(cmd[cmd.index("-o") + 1])
                out_path.write_bytes(b"no-audio-mkv")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            # Case 1: missing audio stream
            with mock.patch.object(fallback.subprocess, "run", side_effect=fake_mkvmerge), \
                 mock.patch.object(
                     fallback,
                     "ffprobe_streams",
                     return_value=(1440.0, [{"codec_type": "video"}]),
                 ):
                res = fallback.remux_playlists_with_mkvmerge(playlists, staging, "Show")
            self.assertFalse(res.ok)
            self.assertIn("no valid audio stream", res.error)
            self.assertEqual(list(staging.glob("*.mkv")), [])

            # Case 2: duration == 0
            with mock.patch.object(fallback.subprocess, "run", side_effect=fake_mkvmerge), \
                 mock.patch.object(
                     fallback,
                     "ffprobe_streams",
                     return_value=(0.0, [{"codec_type": "video"}, {"codec_type": "audio"}]),
                 ):
                res2 = fallback.remux_playlists_with_mkvmerge(playlists, staging, "Show")
            self.assertFalse(res2.ok)
            self.assertIn("invalid duration", res2.error)


class TestRipperMkvmergeFallbackIntegration(unittest.TestCase):
    def test_rip_auto_falls_back_to_mkvmerge_and_publishes_noclobber(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "Media"
            show = media / "TV Shows" / "Fullmetal Alchemist"
            season = show / "Season 01"
            season.mkdir(parents=True)

            item = models.QueueItem(
                title="Fullmetal Alchemist",
                type="tv-season",
                discs=1,
                target_root=str(show),
                format="BD",
                season=1,
                episode_start=1,
            )
            info = {
                "cinfo": {2: "FMA_DISC5"},
                "titles": {
                    0: {9: "0:24:00", 11: "1000", 16: "00001.mpls"},
                    1: {9: "0:24:00", 11: "1000", 16: "00002.mpls"},
                },
            }
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                overwrite=False,
                rerip_review=False,
                auto_rerip_review=False,
                min_free_gb=0,
                non_interactive=True,
                sync_move=True,
                no_split_playall=True,
                allow_mkvmerge_fallback=True,
                current_disc_fingerprint="f" * 64,
                review_root=None,
                state=str(root / "state.json"),
            )

            def fake_makemkv_fail(cmd, **kw):
                return FakePopen(
                    ['MSG:5003,0,0,"Failed to save title 0: sync error"'],
                    returncode=1,
                )

            def fake_fallback(a, it, staging_dir):
                fb_dir = staging_dir / "mkvmerge-fallback"
                fb_dir.mkdir(parents=True, exist_ok=True)
                f0 = fb_dir / "Fullmetal Alchemist_t00.mkv"
                f1 = fb_dir / "Fullmetal Alchemist_t01.mkv"
                f0.write_bytes(b"episode-1-mkvmerge")
                f1.write_bytes(b"episode-2-mkvmerge")
                return models.FallbackResult(
                    ok=True,
                    files=[f0, f1],
                    warnings=["rc=1 warning"],
                    playlists_remuxed=2,
                    file_playlists={str(f0): "00001.mpls", str(f1): "00002.mpls"},
                )

            with mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "staging"), \
                 mock.patch.object(burndvd.nas, "LOCAL_LOCK_BASE", root / "locks"), \
                 mock.patch.object(ripper, "_free_gb", return_value=500.0), \
                 mock.patch.object(ripper.subprocess, "Popen", side_effect=fake_makemkv_fail), \
                 mock.patch.object(ripper, "mkvmerge_fallback_rip", side_effect=fake_fallback), \
                 mock.patch.object(ripper, "ffprobe_dur_s", return_value=1440.0), \
                 mock.patch.object(ripper, "_alt_playlist_duplicate", return_value=False):
                ok, res = ripper.rip(args, item, info, [0, 1], season, {}, 5)

            self.assertTrue(ok, res)
            self.assertTrue(res.get("used_fallback"))
            self.assertTrue((season / "Fullmetal Alchemist - S01E01.mkv").is_file())
            self.assertTrue((season / "Fullmetal Alchemist - S01E02.mkv").is_file())

    def test_rip_respects_no_mkvmerge_fallback_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            show = root / "Media" / "TV Shows" / "Show"
            season = show / "Season 01"
            season.mkdir(parents=True)
            item = models.QueueItem(
                title="Show", type="tv-season", discs=1, target_root=str(show), format="BD", season=1
            )
            info = {"cinfo": {2: "SHOW"}, "titles": {0: {9: "0:24:00", 11: "1000"}}}
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                overwrite=False,
                rerip_review=False,
                auto_rerip_review=False,
                min_free_gb=0,
                non_interactive=True,
                sync_move=True,
                no_split_playall=True,
                allow_mkvmerge_fallback=False,
                current_disc_fingerprint="e" * 64,
                review_root=None,
                state=str(root / "state.json"),
            )

            with mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "staging"), \
                 mock.patch.object(burndvd.nas, "LOCAL_LOCK_BASE", root / "locks"), \
                 mock.patch.object(ripper, "_free_gb", return_value=500.0), \
                 mock.patch.object(
                     ripper.subprocess,
                     "Popen",
                     return_value=FakePopen(['MSG:5003,0,0,"Failed"'], returncode=1),
                 ), \
                 mock.patch.object(ripper, "mkvmerge_fallback_rip") as fb_mock:
                ok, msg = ripper.rip(args, item, info, [0], season, {}, 1)

            self.assertFalse(ok)
            fb_mock.assert_not_called()
            self.assertIn("makemkvcon exit 1", msg)


if __name__ == "__main__":
    unittest.main()
