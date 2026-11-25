"""Regression tests for stoprip fallback/fail-closed behavior, queue pending-state persistence, and finalizer gating (v2.0.2)."""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from burndvd import models, ripper
from burndvd.cli import ripqueue, stoprip


class TestStopripFallbackAndFailClosed(unittest.TestCase):
    def test_classify_and_discover_mkvmerge_fallback_child(self):
        procs = [
            (40101, 1, "/usr/bin/python3 /tmp/ripqueue.py --state /tmp/s.json --queue /tmp/q.csv --device disc:0"),
            (40102, 40101, "/opt/homebrew/bin/mkvmerge -o /tmp/staging/1700001-40101/mkvmerge-fallback/active.mkv /Volumes/DISC/BDMV/PLAYLIST/00001.mpls"),
            (40200, 1, "/opt/homebrew/bin/mkvmerge -o /Users/me/personal.mkv /Users/me/input.mkv"),
        ]
        sessions = stoprip.discover_rip_sessions(procs)
        self.assertEqual(len(sessions), 1)
        sess = sessions[0]
        self.assertEqual(sess["roles"].get("mkvmerge"), [40102])
        self.assertEqual(sess["pids"], [40101, 40102])
        # Unrelated mkvmerge process must not be captured.
        self.assertNotIn(40200, sess["pids"])

    def test_stop_session_protects_open_mkvmerge_fallback_file_including_cross_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_base = root / "staging"
            stage = stage_base / "1700001-40101"
            nested = stage / "mkvmerge-fallback"
            nested.mkdir(parents=True)
            active = nested / "active.mkv"
            complete = nested / "completed.mkv"
            complete.write_bytes(b"completed fixture")
            nas = root / "nas"
            nas.mkdir()
            vol = root / "vol"
            q = root / "queue.csv"
            q.write_text(f"title,type,season,target_root\nShow,tv-season,01,{vol}/TV Shows/Show\n")
            st = root / "state.json"
            st.write_text(json.dumps({"current_index": 0}))

            qpid, wp = 40101, 40102
            procs = [
                (qpid, 1, f"/usr/bin/python3 /tmp/ripqueue.py --state {st} --queue {q} --device disc:0"),
                (wp, qpid, f"/opt/homebrew/bin/mkvmerge -o {active} /Volumes/DISC/BDMV/PLAYLIST/00001.mpls"),
            ]
            (session,) = stoprip.discover_rip_sessions(procs)
            self.assertEqual(session["roles"].get("mkvmerge"), [wp])

            signals: list[tuple[tuple[int, ...], str]] = []
            queries: list[tuple[int, ...]] = []

            with active.open("wb") as fh:
                fh.write(b"in-progress fixture")
                fh.flush()
                actual_lsof = subprocess.run(
                    ["/usr/sbin/lsof", "-a", "-p", str(os.getpid()), "-d", str(fh.fileno()), "-Ffan"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                writer_lsof = actual_lsof.replace(f"p{os.getpid()}\n", f"p{wp}\n")

                def fake_lsof(argv, **_kwargs):
                    queried = tuple(int(pid) for pid in argv[-1].split(","))
                    queries.append(queried)
                    return writer_lsof if wp in queried else f"p{qpid}\n"

                orig_rename = os.rename

                def exdev_rename(src, dst, *a, **kw):
                    if Path(src) == active and Path(dst).is_relative_to(nas):
                        raise OSError(errno.EXDEV, "cross-device")
                    return orig_rename(src, dst, *a, **kw)

                with mock.patch.object(stoprip.subprocess, "check_output", side_effect=fake_lsof), \
                     mock.patch.object(os, "rename", side_effect=exdev_rename):
                    rescued = stoprip.stop_session(
                        session,
                        staging_base=stage_base,
                        nas_root=nas,
                        vol_root=vol,
                        kill_fn=lambda pids, sig: signals.append((tuple(pids), signal.Signals(sig).name)),
                        sleep_fn=lambda _: None,
                    )

            self.assertEqual(signals, [((qpid, wp), "SIGSTOP"), ((qpid, wp), "SIGKILL")])
            self.assertEqual(queries, [(qpid, wp)])
            self.assertTrue(active.is_file())
            self.assertEqual([p.name for p in rescued], ["completed.mkv"])
            self.assertTrue(all(".repair-quarantine" in str(p) for p in rescued))

    def test_stop_session_fails_closed_on_lsof_errors(self):
        for exc in (
            subprocess.TimeoutExpired(["lsof"], 5),
            FileNotFoundError("lsof missing"),
            OSError("permission denied"),
            subprocess.CalledProcessError(2, ["lsof"], output=""),
            subprocess.CalledProcessError(1, ["lsof"], output=""),
        ):
            with self.subTest(exc=type(exc).__name__):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    stage_base = root / "staging"
                    stage = stage_base / "1700001-40101"
                    nested = stage / "mkvmerge-fallback"
                    nested.mkdir(parents=True)
                    active = nested / "active.mkv"
                    complete = nested / "completed.mkv"
                    active.write_bytes(b"writing")
                    complete.write_bytes(b"done")
                    nas = root / "nas"
                    nas.mkdir()
                    vol = root / "vol"
                    q = root / "queue.csv"
                    q.write_text(f"title,type,season,target_root\nShow,tv-season,01,{vol}/TV Shows/Show\n")
                    st = root / "state.json"
                    st.write_text(json.dumps({"current_index": 0}))
                    session = {
                        "key": str(st),
                        "state": str(st),
                        "queue": str(q),
                        "device": "disc:0",
                        "roles": {"ripqueue": [40101], "makemkv": [40102]},
                        "pids": [40101, 40102],
                        "argv_by_pid": {40102: ["makemkvcon", "mkv", "disc:0", "1", str(stage)]},
                    }
                    with mock.patch.object(stoprip.subprocess, "check_output", side_effect=exc):
                        rescued = stoprip.stop_session(
                            session,
                            staging_base=stage_base,
                            nas_root=nas,
                            vol_root=vol,
                            kill_fn=lambda *a: None,
                            sleep_fn=lambda _: None,
                        )
                    self.assertEqual(rescued, [])
                    self.assertTrue(active.is_file())
                    self.assertTrue(complete.is_file())

    def test_stop_session_refuses_rescue_if_writer_still_alive_after_sigkill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage_base = root / "staging"
            stage = stage_base / "1700001-40101"
            stage.mkdir(parents=True)
            f1 = stage / "title_t00.mkv"
            f1.write_bytes(b"data")
            nas = root / "nas"
            nas.mkdir()
            vol = root / "vol"
            q = root / "queue.csv"
            q.write_text(f"title,type,season,target_root\nShow,tv-season,01,{vol}/TV Shows/Show\n")
            st = root / "state.json"
            st.write_text(json.dumps({"current_index": 0}))
            session = {
                "key": str(st),
                "state": str(st),
                "queue": str(q),
                "device": "disc:0",
                "roles": {"ripqueue": [40101], "mkvmerge": [40102]},
                "pids": [40101, 40102],
                "argv_by_pid": {40102: ["mkvmerge", "-o", str(f1), "/Volumes/D/BDMV/PLAYLIST/00001.mpls"]},
            }
            with mock.patch.object(stoprip, "_open_write_paths", return_value=set()):
                rescued = stoprip.stop_session(
                    session,
                    staging_base=stage_base,
                    nas_root=nas,
                    vol_root=vol,
                    kill_fn=lambda *a: None,
                    sleep_fn=lambda _: None,
                    alive_fn=lambda wpids, sess: list(wpids),
                )
            self.assertEqual(rescued, [])
            self.assertTrue(f1.is_file())


class TestQueuePendingPersistenceAndResume(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_probe_abort_with_pending_finalize_persists_committed_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "queue.csv"
            csv_path.write_text(
                f"title,type,discs,target_root,format\n"
                f"First,movie,1,{root / 'nas1'},BD\n"
                f"Second,movie,1,{root / 'nas2'},BD\n"
            )
            state_path = root / "state.json"
            staging = root / "staging"
            staging.mkdir()
            src = staging / "first.mkv"
            src.write_bytes(b"captured-first-disc")
            dst = root / "nas1" / "First.mkv"
            info = {"cinfo": {2: "FIRST"}, "titles": {0: {9: "1:40:00", 11: "1000"}}}
            physical_error = {"error": "PHYSICAL_DISC_TROUBLE", "codes": [5010], "msgs": []}
            argv = [
                "ripqueue.py",
                "--queue", str(csv_path),
                "--state", str(state_path),
                "--makemkvcon", sys.executable,
                "--device", "iso:/fake.iso",
                "--non-interactive",
                "--on-fail", "abort",
                "--no-eject",
                "--no-sound",
                "--no-subocr",
            ]
            callbacks = []

            def blocked_finalizer(*args, **kwargs):
                callbacks.append(kwargs)

            buf = io.StringIO()
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(ripqueue, "check_key_expiry"), \
                 mock.patch.object(ripqueue, "wait_for_disc", side_effect=[("disc", info), ("disc", physical_error)]), \
                 mock.patch.object(ripqueue, "rip", return_value=(True, {"elapsed_s": 0.1, "files": [str(dst)], "move_jobs": [(src, dst, "movie")], "staging": str(staging)})), \
                 mock.patch.object(ripqueue, "start_background_finalize", side_effect=blocked_finalizer), \
                 contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                with self.assertRaises(SystemExit) as ctx:
                    ripqueue.main()

            self.assertEqual(ctx.exception.code, 2)
            self.assertEqual(len(callbacks), 1)
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved["current_index"], 0)
            self.assertEqual(saved["completed"], [])
            self.assertEqual(len(saved["pending_finalize"]), 1)
            self.assertEqual(saved["pending_finalize"][0]["item_index"], 0)

            resumed = models.load_or_init(
                SimpleNamespace(state=str(state_path)),
                [models.QueueItem(**item) for item in saved["queue"]],
            )
            self.assertEqual(resumed["current_index"], 0)
            self.assertEqual(resumed["disc_index_in_item"], 0)
            self.assertEqual(len(resumed["pending_finalize"]), 1)

    def test_load_or_init_rewinds_legacy_advanced_cursor_with_unresolved_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "state.json"
            items = [
                models.QueueItem(title="First", type="movie", discs=1, target_root=str(root / "nas1"), format="BD"),
                models.QueueItem(title="Second", type="movie", discs=1, target_root=str(root / "nas2"), format="BD"),
            ]
            legacy_state = models.default_state(items)
            legacy_state["current_index"] = 2
            legacy_state["disc_index_in_item"] = 0
            legacy_state["pending_finalize"] = [
                {
                    "id": "0:0:1:legacy",
                    "item_index": 0,
                    "disc_index_in_item": 0,
                    "queue_disc_n": 1,
                    "item": "First",
                    "disc_n": 1,
                    "type": "movie",
                    "files": [str(root / "nas1" / "First.mkv")],
                    "status": "pending_finalize",
                }
            ]
            state_path.write_text(json.dumps(legacy_state))

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                resumed = models.load_or_init(SimpleNamespace(state=str(state_path)), items)

            self.assertEqual(resumed["current_index"], 0)
            self.assertEqual(resumed["disc_index_in_item"], 0)
            self.assertEqual(len(resumed["pending_finalize"]), 1)


class TestQueueFinalizerGatingForDoneBeepEject(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_background_finalize_failure_suppresses_done_success_beep_and_eject(self):
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
                "--no-subocr",
            ]
            beeps: list[bool] = []
            ejects: list[int] = []
            out_buf = io.StringIO()
            err_buf = io.StringIO()

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(ripqueue, "check_key_expiry"), \
                 mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info)), \
                 mock.patch.object(ripqueue, "rip", return_value=(True, {"elapsed_s": 5.0, "files": [str(dst)], "move_jobs": [(src, dst, "moving")], "staging": str(staging)})), \
                 mock.patch.object(ripper, "_await_dir_writable", return_value=None), \
                 mock.patch.object(ripqueue, "beep", side_effect=lambda a, ok=True: beeps.append(ok)), \
                 mock.patch.object(ripper, "beep", side_effect=lambda a, ok=True: beeps.append(ok)), \
                 mock.patch.object(ripqueue, "eject", side_effect=lambda a: ejects.append(1)), \
                 contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                with self.assertRaises(SystemExit) as ctx:
                    ripqueue.main()

            self.assertEqual(ctx.exception.code, 2)
            self.assertNotIn(True, beeps)
            self.assertIn(False, beeps)
            self.assertEqual(ejects, [])
            self.assertNotIn("Queue complete.", out_buf.getvalue())
            self.assertNotIn("Done in", out_buf.getvalue())
            self.assertIn("finalize/verify error(s)", err_buf.getvalue())

    def test_background_finalize_success_emits_done_success_beep_and_eject_after_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nas = root / "nas"
            nas.mkdir()
            qpath = root / "queue.csv"
            qpath.write_text(f"title,type,discs,target_root,format\nTest Movie,movie,1,{nas},BD\n")
            state_path = root / "state.json"
            staging = root / "staging"
            staging.mkdir()
            src = staging / "movie.mkv"
            src.write_bytes(b"movie-data")
            dst = nas / "Test Movie.mkv"
            info = {"cinfo": {2: "TEST_MOVIE"}, "titles": {0: {9: "1:40:00", 11: "1000"}}}
            argv = [
                "ripqueue.py",
                "--queue", str(qpath),
                "--state", str(state_path),
                "--makemkvcon", sys.executable,
                "--device", "iso:/fake.iso",
                "--non-interactive",
                "--no-subocr",
            ]
            events: list[str] = []
            orig_join = ripqueue.join_finalizers

            def tracked_join():
                orig_join()
                events.append("joined")

            out_buf = io.StringIO()
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(ripqueue, "check_key_expiry"), \
                 mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info)), \
                 mock.patch.object(ripqueue, "rip", return_value=(True, {"elapsed_s": 5.0, "files": [str(dst)], "move_jobs": [(src, dst, "moving")], "staging": str(staging)})), \
                 mock.patch.object(ripper, "_await_dir_writable", return_value=nas), \
                 mock.patch.object(ripper, "log_final_durations"), \
                 mock.patch.object(ripqueue, "join_finalizers", side_effect=tracked_join), \
                 mock.patch.object(ripqueue, "beep", side_effect=lambda a, ok=True: events.append(f"beep:{ok}")), \
                 mock.patch.object(ripqueue, "eject", side_effect=lambda a: events.append("eject")), \
                 contextlib.redirect_stdout(out_buf):
                ripqueue.main()

            self.assertEqual(events, ["joined", "beep:True", "eject"])
            self.assertIn("Done in", out_buf.getvalue())
            self.assertIn("Queue complete.", out_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
