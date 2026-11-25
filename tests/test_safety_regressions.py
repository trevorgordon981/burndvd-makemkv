"""Safety regression tests for movie publication, stoprip rescue, fallback identity, finalizer state, and checkrip (v2.0.1)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import burndvd
from burndvd import models, ripper
from burndvd.cli import checkrip, ripqueue, stoprip


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


class TestFinding1MovieAtomicAndSafeRollback(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_late_movie_destination_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            args = SimpleNamespace(state=None, verify=False, no_subocr=True, no_sound=True)
            item = SimpleNamespace(title="Audit Film", type="movie")
            dst = d / "movie.mkv"
            src = d / "staging.mkv"
            src.write_bytes(b"RIP")
            dst.write_bytes(b"OTHER MOVIE LANDED AFTER CHECK")
            result = {"files": [str(dst)], "move_jobs": [(src, dst, "movie")], "staging": str(d / "stg")}
            with mock.patch.object(ripper, "_await_dir_writable", return_value=d):
                ok = ripper._run_finalize(args, result, item, 1)
            self.assertFalse(ok)
            self.assertEqual(dst.read_text(), "OTHER MOVIE LANDED AFTER CHECK")
            self.assertTrue(src.exists())
            self.assertTrue(ripper.get_finalize_errors())

    def test_unstarted_jobs_and_preexisting_destinations_survive_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            args = SimpleNamespace(state=None, verify=False, no_subocr=True, no_sound=True)
            item = SimpleNamespace(title="Audit Film", type="movie")
            s1 = d / "first.staged"
            s1.write_bytes(b"first")
            s2 = d / "second.staged"
            s2.write_bytes(b"a much larger staged movie")
            d2 = d / "second.landed"
            d2.write_bytes(b"other")
            jobs = [(s1, d / "first.landed", "one"), (s2, d2, "two")]
            with mock.patch.object(ripper, "_await_dir_writable", return_value=None),                  mock.patch.object(ripper, "append_log"),                  mock.patch.object(ripper, "beep"):
                ripper._run_finalize(args, {"files": [], "move_jobs": jobs}, item, 1)
            self.assertTrue(d2.exists())
            self.assertEqual(d2.read_text(), "other")


class TestFinding2StopripLsofAndFallbackPreservation(unittest.TestCase):
    def test_real_lsof_detects_open_write_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "open.mkv"
            with p.open("wb") as fh:
                fh.write(b"writing")
                fh.flush()
                detected = stoprip._open_write_paths([os.getpid()])
                self.assertIn(p.resolve(), detected)

    def test_nested_fallback_staging_preserved_when_queue_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            staging = d / "staging"
            nested = staging / "mkvmerge-fallback"
            nested.mkdir(parents=True)
            completed = nested / "completed.mkv"
            completed.write_text("already verified playlist")
            with mock.patch.object(stoprip, "locate_staging", return_value=staging),                  mock.patch.object(stoprip, "locate_queue_state", return_value=(None, None)):
                rescued = stoprip.stop_session(
                    {"pids": [], "roles": {}, "key": "test"},
                    kill_fn=lambda *a: None,
                    sleep_fn=lambda *a: None,
                )
            self.assertEqual(rescued, [])
            self.assertTrue(staging.exists())
            self.assertTrue(completed.is_file())

    def test_nested_fallback_rescued_to_quarantine_not_live_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nas = root / "nas"
            vol = root / "vol"
            nas.mkdir()
            staging = root / "staging" / "1700001-200"
            nested = staging / "mkvmerge-fallback"
            nested.mkdir(parents=True)
            (nested / "completed.mkv").write_bytes(b"verified-episode")
            q = root / "q.csv"
            q.write_text(f"title,type,season,target_root\nShow,tv-season,01,{vol}/TV Shows/Show\n")
            st = root / "s.json"
            st.write_text(json.dumps({"current_index": 0}))
            session = {"key": str(st), "state": str(st), "queue": str(q), "pids": [], "roles": {}}
            with mock.patch.object(stoprip, "locate_staging", return_value=staging),                  mock.patch.object(stoprip, "locate_queue_state", return_value=(q, st)):
                rescued = stoprip.stop_session(
                    session,
                    nas_root=nas,
                    vol_root=vol,
                    kill_fn=lambda *a: None,
                    sleep_fn=lambda *a: None,
                )
            self.assertEqual(len(rescued), 1)
            self.assertIn(".repair-quarantine", str(rescued[0]))
            self.assertTrue(rescued[0].is_file())
            self.assertFalse((nas / "TV Shows" / "Show" / "Season 01").exists())


class TestFinding3FallbackUnverifiedEpisodeIdentity(unittest.TestCase):
    def test_contracted_unmapped_fallback_routes_to_review_not_season(self):
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
                expected_episodes=2,
                expected_disc_episodes=2,
                expected_title_ids=[9, 7],
                expected_physical_disc=5,
            )
            info = {
                "cinfo": {2: "FMA_DISC5"},
                "titles": {0: {9: "0:24:00", 11: "1000"}, 1: {9: "0:24:00", 11: "1000"}},
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

            def fake_fallback(a, it, staging_dir):
                fb_dir = staging_dir / "mkvmerge-fallback"
                fb_dir.mkdir(parents=True, exist_ok=True)
                f0 = fb_dir / "Fullmetal Alchemist_t00.mkv"
                f1 = fb_dir / "Fullmetal Alchemist_t01.mkv"
                f0.write_bytes(b"ep-a")
                f1.write_bytes(b"ep-b")
                return models.FallbackResult(ok=True, files=[f0, f1], playlists_remuxed=2)

            with mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "staging"),                  mock.patch.object(burndvd.nas, "LOCAL_LOCK_BASE", root / "locks"),                  mock.patch.object(ripper, "_free_gb", return_value=500.0),                  mock.patch.object(ripper.subprocess, "Popen", return_value=FakePopen(['MSG:5003,0,0,"fail"'], returncode=1)),                  mock.patch.object(ripper, "mkvmerge_fallback_rip", side_effect=fake_fallback),                  mock.patch.object(ripper, "ffprobe_dur_s", return_value=1440.0),                  mock.patch.object(ripper, "_alt_playlist_duplicate", return_value=False):
                ok, res = ripper.rip(args, item, info, [9, 7], season, {}, 5)

            self.assertTrue(ok, res)
            self.assertTrue(res.get("used_fallback"))
            self.assertTrue(res.get("routed_to_review"))
            self.assertFalse((season / "Fullmetal Alchemist - S01E01.mkv").exists())
            self.assertFalse((season / "Fullmetal Alchemist - S01E02.mkv").exists())
            self.assertEqual(len(res["files"]), 2)
            for fp in res["files"]:
                self.assertIn("burndvd-review", fp)

    def test_contracted_proven_mpls_mapping_publishes_in_contract_order(self):
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
                expected_episodes=2,
                expected_disc_episodes=2,
                expected_title_ids=[9, 7],
                expected_physical_disc=5,
            )
            info = {
                "cinfo": {2: "FMA_DISC5"},
                "titles": {
                    9: {9: "0:24:00", 11: "1000", 16: "00009.mpls"},
                    7: {9: "0:24:00", 11: "1000", 16: "00007.mpls"},
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

            def fake_fallback(a, it, staging_dir):
                fb_dir = staging_dir / "mkvmerge-fallback"
                fb_dir.mkdir(parents=True, exist_ok=True)
                f0 = fb_dir / "Fullmetal Alchemist_t00.mkv"
                f1 = fb_dir / "Fullmetal Alchemist_t01.mkv"
                f0.write_bytes(b"title-7-payload")
                f1.write_bytes(b"title-9-payload")
                return models.FallbackResult(
                    ok=True,
                    files=[f0, f1],
                    playlists_remuxed=2,
                    file_playlists={str(f0): "00007.mpls", str(f1): "00009.mpls"},
                )

            with mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "staging"),                  mock.patch.object(burndvd.nas, "LOCAL_LOCK_BASE", root / "locks"),                  mock.patch.object(ripper, "_free_gb", return_value=500.0),                  mock.patch.object(ripper.subprocess, "Popen", return_value=FakePopen(['MSG:5003,0,0,"fail"'], returncode=1)),                  mock.patch.object(ripper, "mkvmerge_fallback_rip", side_effect=fake_fallback),                  mock.patch.object(ripper, "ffprobe_dur_s", return_value=1440.0),                  mock.patch.object(ripper, "_alt_playlist_duplicate", return_value=False):
                ok, res = ripper.rip(args, item, info, [9, 7], season, {}, 5)

            self.assertTrue(ok, res)
            self.assertEqual((season / "Fullmetal Alchemist - S01E01.mkv").read_bytes(), b"title-9-payload")
            self.assertEqual((season / "Fullmetal Alchemist - S01E02.mkv").read_bytes(), b"title-7-payload")


class TestFinding4StateCommitAndBackgroundExceptionCapture(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_missing_verifier_is_captured_not_uncaught_thread_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            dst = d / "movie.mkv"
            dst.write_bytes(b"data")
            args = SimpleNamespace(state=None, verify=True, no_subocr=True, no_sound=True)
            item = SimpleNamespace(title="Audit Film", type="movie")
            exceptions = []
            with mock.patch.object(ripper, "FFMPEG_BIN", str(d / "missing-verifier")),                  mock.patch.object(threading, "excepthook", lambda a: exceptions.append(a.exc_type.__name__)):
                ripper.start_background_finalize(args, {"files": [str(dst)], "move_jobs": []}, item, 1)
                ripper.join_finalizers()
            self.assertEqual(exceptions, [])
            errs = ripper.get_finalize_errors()
            self.assertTrue(errs)
            self.assertIn("FileNotFoundError", errs[0])

    def test_restart_after_failed_background_transfer_does_not_skip_disc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            qpath = root / "queue.csv"
            qpath.write_text(f"title,type,discs,target_root,format\nTest Movie,movie,1,{root / 'nas'},BD\n")
            state_path = root / "state.json"
            staging = root / "staging"
            staging.mkdir()
            src = staging / "movie.mkv"
            src.write_bytes(b"movie-data")
            dst = root / "nas" / "Test Movie.mkv"
            info = {"cinfo": {2: "TEST_MOVIE"}, "titles": {0: {9: "1:40:00", 11: "1000"}}}
            argv = [
                "ripqueue.py",
                "--queue", str(qpath),
                "--state", str(state_path),
                "--makemkvcon", sys.executable,
                "--device", "iso:/fake.iso",
                "--non-interactive",
                "--no-eject",
                "--no-sound",
                "--no-subocr",
            ]
            # Run 1: background NAS transfer fails -> must exit 2 and NOT commit completed or advance cursor.
            with mock.patch.object(sys, "argv", argv),                  mock.patch.object(ripqueue, "check_key_expiry"),                  mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info)),                  mock.patch.object(ripqueue, "rip", return_value=(True, {"elapsed_s": 5.0, "files": [str(dst)], "move_jobs": [(src, dst, "moving")], "staging": str(staging)})),                  mock.patch.object(ripper, "_await_dir_writable", return_value=None):
                with self.assertRaises(SystemExit) as ctx:
                    ripqueue.main()
            self.assertEqual(ctx.exception.code, 2)

            persisted = json.loads(state_path.read_text())
            self.assertEqual(persisted["current_index"], 0)
            self.assertEqual(persisted["disc_index_in_item"], 0)
            self.assertEqual(persisted["completed"], [])
            self.assertEqual(persisted["pending_finalize"], [])
            self.assertEqual(len(persisted["failed_finalize"]), 1)
            self.assertTrue(src.exists())

            # Run 2 (restart): destination now writable -> resumes item 0 (does NOT skip) and commits!
            (root / "nas").mkdir(parents=True, exist_ok=True)
            with mock.patch.object(sys, "argv", argv),                  mock.patch.object(ripqueue, "check_key_expiry"),                  mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info)),                  mock.patch.object(ripqueue, "rip", return_value=(True, {"elapsed_s": 5.0, "files": [str(dst)], "move_jobs": [(src, dst, "moving")], "staging": str(staging)})),                  mock.patch.object(ripper, "_await_dir_writable", return_value=root / "nas"),                  mock.patch.object(ripper, "log_final_durations"):
                ripqueue.main()

            final_state = json.loads(state_path.read_text())
            self.assertEqual(final_state["current_index"], 1)
            self.assertEqual(len(final_state["completed"]), 1)
            self.assertTrue(dst.is_file())


class TestFinding5CheckripRejectsEmptyAndUnreadable(unittest.TestCase):
    def test_empty_zero_byte_mkv_fails_nonzero_json_and_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "S01E01.mkv").write_bytes(b"")
            buf = io.StringIO()
            with mock.patch.object(sys, "argv", ["checkrip", str(d), "--expected", "1", "--json"]),                  contextlib.redirect_stdout(buf):
                rc_json = checkrip.main()
            self.assertEqual(rc_json, 1)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["real_count"], 0)
            self.assertEqual(payload["missing_vs_expected"], 1)
            self.assertEqual(len(payload["unreadable"]), 1)
            self.assertEqual(payload["unreadable"][0]["name"], "S01E01.mkv")

            buf2 = io.StringIO()
            with mock.patch.object(sys, "argv", ["checkrip", str(d), "--expected", "1"]),                  contextlib.redirect_stdout(buf2):
                rc_human = checkrip.main()
            self.assertEqual(rc_human, 1)
            self.assertIn("UNREADABLE", buf2.getvalue())


if __name__ == "__main__":
    unittest.main()
