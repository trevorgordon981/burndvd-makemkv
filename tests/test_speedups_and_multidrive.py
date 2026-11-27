"""Unit tests for multi-drive MakeMKV targeting, parallel/pipelined verify,
early eject after pre-publish verify, and --no-extras (100% mocked, zero hardware I/O).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import burndvd
from burndvd import drive, probe, ripper, selection
from burndvd.cli import ripqueue
from burndvd.models import QueueItem

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "bin" / "burndvd"


def _write_exec(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _FakePopen:
    def __init__(self, lines: list[str], returncode: int = 0, on_start=None):
        self._lines = [ln if ln.endswith("\n") else ln + "\n" for ln in lines]
        self.stdout = io.StringIO("".join(self._lines))
        self.returncode = returncode
        if on_start is not None:
            on_start()

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class TestMultiDriveMakeMKVResolution(unittest.TestCase):
    def setUp(self):
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                k: v
                for k, v in os.environ.items()
                if k
                not in (
                    drive.DRUTIL_DRIVE_ENV,
                    drive.DRIVE_DEVICE_ENV,
                    drive.DRIVE_SERIAL_ENV,
                )
            },
            clear=True,
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_parse_makemkv_drives_extracts_indices_nodes_media_and_serials(self):
        sample = "\n".join(
            [
                'MSG:1005,0,1,"MakeMKV v1.17.7 darwin(arm64-release) started"',
                'DRV:0,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","",""',
                'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 SIK95G426222","SHOW_S1_D2","/dev/rdisk6"',
                'DRV:2,2,999,12,"DVD+R-DL OPTIARC DVD RW 1.03","OLD_DVD","/dev/rdisk9"',
                'DRV:3,256,0,0,"","",""',
            ]
        )
        parsed = drive.parse_makemkv_drives(sample)
        self.assertEqual(len(parsed), 4)
        self.assertEqual(parsed[0]["index"], 0)
        self.assertFalse(parsed[0]["has_media"])
        self.assertEqual(parsed[0]["serial"], "DRVSER00001")
        self.assertIsNone(parsed[0]["device_node"])

        self.assertEqual(parsed[1]["index"], 1)
        self.assertTrue(parsed[1]["has_media"])
        self.assertEqual(parsed[1]["serial"], "SIK95G426222")
        self.assertEqual(parsed[1]["device_node"], "/dev/disk6")
        self.assertEqual(parsed[1]["volume"], "SHOW_S1_D2")

        self.assertEqual(parsed[2]["index"], 2)
        self.assertEqual(parsed[2]["serial"], "")
        self.assertEqual(parsed[2]["device_node"], "/dev/disk9")

    def test_resolve_makemkv_device_tracks_shifted_index_by_node_and_serial(self):
        # No pin -> no subprocess call, returns args.device unchanged.
        args_unpinned = SimpleNamespace(device="disc:1", makemkvcon="/fake/makemkvcon")
        with mock.patch.object(drive.subprocess, "run") as run_mock:
            self.assertEqual(drive.resolve_makemkv_device(args_unpinned), "disc:1")
            run_mock.assert_not_called()

        # Non-disc selector -> no subprocess call even if env pin is set.
        args_iso = SimpleNamespace(device="iso:/tmp/x.iso", makemkvcon="/fake/makemkvcon")
        with mock.patch.dict(
            os.environ,
            {drive.DRIVE_DEVICE_ENV: "/dev/disk6", drive.DRIVE_SERIAL_ENV: "SIK95G426222"},
        ), mock.patch.object(drive.subprocess, "run") as run_mock:
            self.assertEqual(drive.resolve_makemkv_device(args_iso), "iso:/tmp/x.iso")
            run_mock.assert_not_called()

        # Drive A ejected; Drive B (/dev/disk6, SIK95G426222) shifted from disc:1 -> disc:0.
        shifted_out = (
            'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 SIK95G426222","SHOW_S1_D2","/dev/rdisk6"\n'
            'DRV:1,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","",""\n'
        )
        args_pinned = SimpleNamespace(device="disc:1", makemkvcon="/fake/makemkvcon")
        with mock.patch.dict(
            os.environ,
            {drive.DRIVE_DEVICE_ENV: "/dev/disk6", drive.DRIVE_SERIAL_ENV: "SIK95G426222"},
        ), mock.patch.object(
            drive.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=shifted_out, stderr=""),
        ):
            self.assertEqual(drive.resolve_makemkv_device(args_pinned), "disc:0")

        # Serial fallback (node changed to /dev/disk8, serial matches loaded drive at index 0).
        serial_out = (
            'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 SIK95G426222","SHOW_S1_D2","/dev/rdisk8"\n'
        )
        with mock.patch.dict(
            os.environ,
            {drive.DRIVE_DEVICE_ENV: "/dev/disk6", drive.DRIVE_SERIAL_ENV: "SIK95G426222"},
        ), mock.patch.object(
            drive.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=serial_out, stderr=""),
        ):
            self.assertEqual(drive.resolve_makemkv_device(args_pinned), "disc:0")

        # Empty drive matching serial should NOT override device.
        empty_serial_out = (
            'DRV:0,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 SIK95G426222","",""\n'
        )
        with mock.patch.dict(
            os.environ, {drive.DRIVE_SERIAL_ENV: "SIK95G426222"}
        ), mock.patch.object(
            drive.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=empty_serial_out, stderr=""),
        ):
            self.assertEqual(drive.resolve_makemkv_device(args_pinned), "disc:1")

        # Timeout/OSError falls back gracefully to args.device.
        with mock.patch.dict(
            os.environ, {drive.DRIVE_DEVICE_ENV: "/dev/disk6"}
        ), mock.patch.object(
            drive.subprocess, "run", side_effect=subprocess.TimeoutExpired("cmd", 15)
        ):
            self.assertEqual(drive.resolve_makemkv_device(args_pinned), "disc:1")

    def test_probe_disc_uses_dynamically_resolved_device(self):
        seen_cmds: list[list[str]] = []

        def fake_popen(cmd, **kwargs):
            seen_cmds.append(list(cmd))
            return _FakePopen(
                [
                    'CINFO:2,0,"MY_DISC"',
                    'TINFO:0,9,0,"0:42:00"',
                    'TINFO:0,11,0,"4000000000"',
                ]
            )

        args = SimpleNamespace(
            makemkvcon="/fake/makemkvcon",
            device="disc:1",
            drive_device="/dev/disk6",
        )
        with mock.patch.object(probe, "_optical_mounts", return_value=[]), \
             mock.patch.object(probe, "_drive_idx_from_args", return_value=1), \
             mock.patch.object(probe, "resolve_makemkv_device", return_value="disc:0"), \
             mock.patch.object(probe.subprocess, "Popen", side_effect=fake_popen):
            info = probe.probe_disc(args)

        self.assertIsNotNone(info)
        self.assertEqual(seen_cmds[0][-1], "disc:0")

    def test_rip_re_resolves_device_before_each_selector(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "TV Shows" / "Sample Show" / "Season 01"
            target.mkdir(parents=True)
            item = QueueItem(
                title="Sample Show",
                type="tv-season",
                discs=1,
                target_root=str(root / "TV Shows" / "Sample Show"),
                format="BD",
                season=1,
                episode_start=1,
            )
            info = {
                "cinfo": {2: "SAMPLE_SHOW_S1_D1"},
                "titles": {
                    0: {9: "0:22:00", 11: "1000000000"},
                    1: {9: "0:22:00", 11: "1000000000"},
                    2: {9: "0:03:00", 11: "200000000"},
                },
            }
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:1",
                drive_device="/dev/disk6",
                overwrite=False,
                rerip_review=False,
                review_root=None,
                min_free_gb=1,
                no_split_playall=True,
                no_extras=True,
                verify=False,
                sync_move=False,
                non_interactive=True,
                allow_mkvmerge_fallback=False,
                state=None,
            )
            seen_devices: list[str] = []
            resolved_seq = iter(["disc:1", "disc:0"])

            def fake_popen(cmd, **kwargs):
                seen_devices.append(cmd[cmd.index("mkv") + 1])
                sel = cmd[cmd.index("mkv") + 2]
                out_dir = Path(cmd[cmd.index("mkv") + 3])
                (out_dir / f"title_t{int(sel):02d}.mkv").write_bytes(b"fake-mkv-bytes")
                return _FakePopen(['MSG:5036,0,1,"1 titles saved"'])

            with mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "staging"), \
                 mock.patch.object(burndvd.nas, "LOCAL_LOCK_BASE", root / "locks"), \
                 mock.patch.object(ripper, "_free_gb", return_value=500.0), \
                 mock.patch.object(ripper, "resolve_makemkv_device", side_effect=lambda a: next(resolved_seq)), \
                 mock.patch.object(ripper.subprocess, "Popen", side_effect=fake_popen), \
                 mock.patch.object(ripper, "_alt_playlist_duplicate", return_value=False):
                ok, result = ripper.rip(args, item, info, [0, 1], target, {}, 1)

            self.assertTrue(ok, result)
            self.assertEqual(seen_devices, ["disc:1", "disc:0"])


class TestParallelAndPipelinedVerify(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_verify_runs_in_parallel_and_returns_deterministic_first_failure(self):
        # Single path stays in-thread.
        main_tid = threading.get_ident()
        seen_tids: list[int] = []

        def single_stub(p: str):
            seen_tids.append(threading.get_ident())
            return True, ""

        with mock.patch.object(ripper, "_verify_single", side_effect=single_stub):
            self.assertEqual(ripper.verify(["only.mkv"]), (True, ""))
        self.assertEqual(seen_tids, [main_tid])

        # Multiple paths run concurrently (verified via threading.Barrier).
        barrier = threading.Barrier(2, timeout=5)

        def parallel_stub(p: str):
            if p in ("a.mkv", "b.mkv"):
                barrier.wait()
            if p == "a.mkv":
                time.sleep(0.05)
                return False, "a.mkv: first error"
            if p == "b.mkv":
                return False, "b.mkv: second error"
            return True, ""

        with mock.patch.object(ripper, "_verify_single", side_effect=parallel_stub):
            ok, err = ripper.verify(["a.mkv", "b.mkv", "c.mkv"])
        self.assertFalse(ok)
        self.assertEqual(err, "a.mkv: first error")

    def test_pipelined_preverify_runs_during_rip_and_skips_reverify_in_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "TV Shows" / "Pipe Show" / "Season 01"
            target.mkdir(parents=True)
            item = QueueItem(
                title="Pipe Show",
                type="tv-season",
                discs=1,
                target_root=str(root / "TV Shows" / "Pipe Show"),
                format="BD",
                season=1,
                episode_start=1,
            )
            info = {
                "cinfo": {2: "PIPE_SHOW_S1_D1"},
                "titles": {
                    0: {9: "0:22:00", 11: "1000000000"},
                    1: {9: "0:22:00", 11: "1000000000"},
                },
            }
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="iso:/fake.iso",
                overwrite=False,
                rerip_review=False,
                review_root=None,
                disc_receipts=str(root / "receipts.jsonl"),
                min_free_gb=1,
                no_split_playall=True,
                no_extras=True,
                no_subocr=True,
                no_sound=True,
                verify=True,
                sync_move=False,
                non_interactive=True,
                allow_mkvmerge_fallback=False,
                state=None,
            )
            verify_calls: list[list[str]] = []

            def spy_verify(paths):
                verify_calls.append(list(paths))
                return True, ""

            def fake_popen(cmd, **kwargs):
                sel = cmd[cmd.index("mkv") + 2]
                out_dir = Path(cmd[cmd.index("mkv") + 3])
                (out_dir / f"title_t{int(sel):02d}.mkv").write_bytes(
                    f"data-{sel}".encode("utf-8")
                )
                return _FakePopen(['MSG:5036,0,1,"1 titles saved"'])

            with mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "staging"), \
                 mock.patch.object(burndvd.nas, "LOCAL_LOCK_BASE", root / "locks"), \
                 mock.patch.object(ripper, "_free_gb", return_value=500.0), \
                 mock.patch.object(ripper.subprocess, "Popen", side_effect=fake_popen), \
                 mock.patch.object(ripper, "_alt_playlist_duplicate", return_value=False), \
                 mock.patch.object(ripper, "verify", side_effect=spy_verify), \
                 mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok, result = ripper.rip(args, item, info, [0, 1], target, {}, 1)
                self.assertTrue(ok, result)
                self.assertIn("preverified", result)
                # Run finalize — already pre-verified files must NOT trigger another verify() call!
                fin_ok = ripper._run_finalize(args, result, item, 1)

            self.assertTrue(fin_ok)
            # Exactly 2 calls total (1 per selector from pipelined pre-verify, 0 redundant in finalize).
            self.assertEqual(len(verify_calls), 2)
            self.assertTrue((target / "Pipe Show - S01E01.mkv").is_file())
            self.assertTrue((target / "Pipe Show - S01E02.mkv").is_file())

    def test_finalize_rejects_failed_preverify_and_rechecks_modified_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            lib = root / "lib"
            stage.mkdir()
            lib.mkdir()
            src = stage / "ep1.mkv"
            src.write_bytes(b"corrupt-bytes")
            st = src.stat()
            dst = lib / "Movie.mkv"
            args = SimpleNamespace(
                state=None,
                verify=True,
                rerip_review=False,
                no_subocr=True,
                no_sound=True,
                overwrite=False,
                disc_receipts=str(root / "receipts.jsonl"),
            )
            item = SimpleNamespace(title="Movie", type="movie")

            # Case 1: Matching stat with ok=False -> fails pre-publish without calling verify() or publishing.
            result_fail = {
                "files": [str(dst)],
                "move_jobs": [(src, dst, "moving")],
                "staging": str(stage),
                "preverified": {str(src): (st.st_size, st.st_mtime_ns, False, "bad stream")},
            }
            on_verified_hits: list[int] = []
            with mock.patch.object(ripper, "verify") as verify_mock, \
                 mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"):
                ok = ripper._run_finalize(
                    args,
                    result_fail,
                    item,
                    1,
                    on_verified=lambda: on_verified_hits.append(1),
                )
            self.assertFalse(ok)
            verify_mock.assert_not_called()
            self.assertFalse(dst.exists())
            self.assertTrue(src.is_file())
            self.assertEqual(on_verified_hits, [])

            # Case 2: Stat changed since pre-verify -> must re-verify!
            src.write_bytes(b"rewritten-longer-bytes")
            result_stale = {
                "files": [str(dst)],
                "move_jobs": [(src, dst, "moving")],
                "staging": str(stage),
                "preverified": {str(src): (st.st_size, st.st_mtime_ns, True, "")},
            }
            with mock.patch.object(ripper, "verify", return_value=(True, "")) as verify_mock, \
                 mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok2 = ripper._run_finalize(args, result_stale, item, 1)
            self.assertTrue(ok2)
            verify_mock.assert_called_once_with([str(src)])
            self.assertTrue(dst.is_file())


class TestEarlyEjectAfterPrePublishVerify(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_single_disc_verify_ejects_before_nas_move_and_does_not_double_eject(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nas = root / "nas"
            nas.mkdir()
            qpath = root / "queue.csv"
            qpath.write_text(
                f"title,type,discs,target_root,format\nEarly Movie,movie,1,{nas},BD\n",
                encoding="utf-8",
            )
            state_path = root / "state.json"
            ledger = root / "receipts.jsonl"
            staging = root / "staging"
            staging.mkdir()
            src = staging / "movie.mkv"
            src.write_bytes(b"valid-movie")
            dst = nas / "Early Movie.mkv"
            info = {"cinfo": {2: "EARLY_MOVIE"}, "titles": {0: {9: "1:40:00", 11: "1000"}}}
            argv = [
                "ripqueue.py",
                "--queue", str(qpath),
                "--state", str(state_path),
                "--makemkvcon", sys.executable,
                "--device", "iso:/fake.iso",
                "--non-interactive",
                "--verify",
                "--no-subocr",
                "--disc-receipts", str(ledger),
            ]
            events: list[str] = []
            real_move = ripper.move_with_progress

            def tracked_verify(paths):
                events.append("verify")
                return True, ""

            def tracked_move(s, d, **kw):
                events.append("move")
                return real_move(s, d, **kw)

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(ripqueue, "check_key_expiry"), \
                 mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info)), \
                 mock.patch.object(
                     ripqueue,
                     "rip",
                     return_value=(
                         True,
                         {
                             "elapsed_s": 5.0,
                             "files": [str(dst)],
                             "move_jobs": [(src, dst, "moving")],
                             "staging": str(staging),
                         },
                     ),
                 ), \
                 mock.patch.object(ripper, "verify", side_effect=tracked_verify), \
                 mock.patch.object(ripper, "move_with_progress", side_effect=tracked_move), \
                 mock.patch.object(ripper, "_await_dir_writable", return_value=nas), \
                 mock.patch.object(ripper, "log_final_durations"), \
                 mock.patch.object(ripqueue, "beep", side_effect=lambda a, ok=True: events.append(f"beep:{ok}")), \
                 mock.patch.object(ripqueue, "eject", side_effect=lambda a: events.append("eject")), \
                 contextlib.redirect_stdout(io.StringIO()):
                ripqueue.main()

            self.assertEqual(events.count("eject"), 1, f"must eject exactly once: {events}")
            self.assertLess(events.index("verify"), events.index("eject"))
            self.assertLess(events.index("eject"), events.index("move"))
            self.assertTrue(dst.is_file())

    def test_verify_failure_never_triggers_early_eject(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nas = root / "nas"
            nas.mkdir()
            qpath = root / "queue.csv"
            qpath.write_text(
                f"title,type,discs,target_root,format\nBad Movie,movie,1,{nas},BD\n",
                encoding="utf-8",
            )
            state_path = root / "state.json"
            staging = root / "staging"
            staging.mkdir()
            src = staging / "movie.mkv"
            src.write_bytes(b"corrupt")
            dst = nas / "Bad Movie.mkv"
            info = {"cinfo": {2: "BAD_MOVIE"}, "titles": {0: {9: "1:40:00", 11: "1000"}}}
            argv = [
                "ripqueue.py",
                "--queue", str(qpath),
                "--state", str(state_path),
                "--makemkvcon", sys.executable,
                "--device", "iso:/fake.iso",
                "--non-interactive",
                "--verify",
                "--no-subocr",
            ]
            ejects: list[int] = []
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(ripqueue, "check_key_expiry"), \
                 mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info)), \
                 mock.patch.object(
                     ripqueue,
                     "rip",
                     return_value=(
                         True,
                         {
                             "elapsed_s": 5.0,
                             "files": [str(dst)],
                             "move_jobs": [(src, dst, "moving")],
                             "staging": str(staging),
                         },
                     ),
                 ), \
                 mock.patch.object(ripper, "verify", return_value=(False, "corrupt")), \
                 mock.patch.object(ripqueue, "beep"), \
                 mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripqueue, "eject", side_effect=lambda a: ejects.append(1)), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    ripqueue.main()

            self.assertEqual(ctx.exception.code, 2)
            self.assertEqual(ejects, [])
            self.assertFalse(dst.exists())
            self.assertTrue(src.is_file())


class TestNoExtrasSelectionAndCLI(unittest.TestCase):
    def test_planned_rip_selectors_skip_extras_omits_short_titles_but_keeps_contracted(self):
        info = {
            "cinfo": {2: "SHOW_S1_D1"},
            "titles": {
                0: {9: "1:06:00", 11: "6000000000"},  # play-all of 1+2+3
                1: {9: "0:22:00", 11: "2000000000"},
                2: {9: "0:22:00", 11: "2000000000"},
                3: {9: "0:22:00", 11: "2000000000"},
                4: {9: "0:05:00", 11: "400000000"},  # extra < TV_MIN_DUR_S
                5: {9: "0:04:00", 11: "300000000"},  # extra < TV_MIN_DUR_S
            },
        }
        item = QueueItem(
            title="Show",
            type="tv-season",
            discs=1,
            target_root="/Volumes/Media/TV Shows/Show",
            format="BD",
            season=1,
        )
        sels_default, skipped_default = selection.planned_rip_selectors(
            item, info, [1, 2, 3], skip_extras=False
        )
        self.assertEqual(sels_default, ["1", "2", "3", "4", "5"])
        self.assertEqual(skipped_default, {0})

        sels_no_extras, skipped_ne = selection.planned_rip_selectors(
            item, info, [1, 2, 3], skip_extras=True
        )
        self.assertEqual(sels_no_extras, ["1", "2", "3"])
        self.assertEqual(skipped_ne, {0})

        # Protected expected_title_ids are NEVER omitted even if shorter than TV_MIN_DUR_S.
        contracted = QueueItem(
            title="Show",
            type="tv-season",
            discs=1,
            target_root="/Volumes/Media/TV Shows/Show",
            format="BD",
            season=1,
            expected_episodes=4,
            expected_disc_episodes=4,
            expected_physical_disc=1,
            expected_title_ids=[1, 2, 3, 4],
        )
        sels_protected, _ = selection.planned_rip_selectors(
            contracted, info, [1, 2, 3, 4], skip_extras=True
        )
        self.assertEqual(sels_protected, ["1", "2", "3", "4"])

    def test_ripqueue_parser_no_extras_flag_and_env(self):
        with mock.patch.dict(os.environ, {"BURNDVD_CONFIG": "/nonexistent/burndvd-test.cfg"}, clear=False):
            os.environ.pop("BURNDVD_NO_EXTRAS", None)
            self.assertFalse(ripqueue.build_parser().parse_args([]).no_extras)
            self.assertTrue(ripqueue.build_parser().parse_args(["--no-extras"]).no_extras)
        with mock.patch.dict(os.environ, {"BURNDVD_NO_EXTRAS": "1"}):
            self.assertTrue(ripqueue.build_parser().parse_args([]).no_extras)


class TestWrapperMultiDriveAndFeatures(unittest.TestCase):
    def _build_fake_env(
        self,
        root: Path,
        *,
        makemkv_stdout: str,
        drutil_script: str,
        diskutil_script: str,
        pgrep_script: str = "#!/bin/bash\nexit 1\n",
        ps_script: str = "#!/bin/bash\nexit 0\n",
    ) -> tuple[dict[str, str], Path]:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        logs = root / "logs"
        logs.mkdir()
        media = root / "media"
        (media / "Movies").mkdir(parents=True)
        (media / "TV Shows").mkdir(parents=True)
        capture_file = root / "captured.json"

        _write_exec(
            fake_bin / "makemkvcon",
            f"#!/bin/bash\ncat <<'EOF'\n{makemkv_stdout}\nEOF\nexit 0\n",
        )
        _write_exec(fake_bin / "drutil", drutil_script)
        _write_exec(fake_bin / "diskutil", diskutil_script)
        _write_exec(fake_bin / "pgrep", pgrep_script)
        _write_exec(fake_bin / "ps", ps_script)
        _write_exec(fake_bin / "lsof", "#!/bin/bash\nexit 1\n")
        _write_exec(
            fake_bin / "caffeinate",
            """#!/bin/bash
while [ "$#" -gt 0 ]; do
    case "$1" in
        -t) shift 2 ;;
        -*) shift ;;
        *) break ;;
    esac
done
if [ "$#" -gt 0 ]; then
    exec "$@"
fi
exit 0
""",
        )
        fake_ripqueue = _write_exec(
            root / "fake_ripqueue.py",
            f"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

capture = Path({str(capture_file)!r})
args = sys.argv[1:]
if "--validate-queue" in args:
    sys.exit(0)
if "--scan" in args:
    print("[]")
    sys.exit(0)
state_path = None
if "--state" in args:
    state_path = Path(args[args.index("--state") + 1])
rec = {{
    "argv": args,
    "BURNDVD_DRUTIL_DRIVE": os.environ.get("BURNDVD_DRUTIL_DRIVE"),
    "BURNDVD_DRIVE_DEVICE": os.environ.get("BURNDVD_DRIVE_DEVICE"),
    "BURNDVD_DRIVE_SERIAL": os.environ.get("BURNDVD_DRIVE_SERIAL"),
}}
capture.write_text(json.dumps(rec))
if state_path is not None:
    Path(str(state_path) + ".exit").write_text(json.dumps({{"exit_code": 0, "reason": "ok"}}))
sys.exit(0)
""",
        )
        env = {
            "PATH": f"{fake_bin}:{Path(sys.executable).parent}:/usr/bin:/bin",
            "HOME": str(root),
            "TMPDIR": str(root),
            "BURNDVD_RIPQUEUE_PY": str(fake_ripqueue),
            "BURNDVD_MEDIA_ROOT": str(media),
            "BURNDVD_NFS_MEDIA_ROOT": str(media),
            "BURNDVD_LOG_DIR": str(logs),
            "PYTHONPATH": str(REPO_ROOT / "src"),
        }
        return env, capture_file

    def test_wrapper_makemkv_enumeration_serial_claim_and_no_extras(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Note: MakeMKV reports /dev/rdisk9 at DRV:1 while DRV:0 is empty.
            # Old SORTED_NUMS logic would have wrongly guessed disc:0!
            mkv_out = "\n".join(
                [
                    'DRV:0,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","",""',
                    'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 SIK95G426222","SAMPLE_MOVIE_2003","/dev/rdisk9"',
                ]
            )
            drutil_sh = """#!/bin/bash
if [ "$1" = "list" ]; then
    echo "  1  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported"
    echo "  2  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported"
    exit 0
fi
if [ "$1" = "status" ] && [ "$3" = "1" ]; then
    echo "  Type: No Media Inserted"
    exit 0
fi
if [ "$1" = "status" ] && [ "$3" = "2" ]; then
    echo "  Type: BD-ROM  Name: /dev/disk9"
    echo "  Space Used: 25.00GB"
    exit 0
fi
exit 1
"""
            diskutil_sh = """#!/bin/bash
if [ "$1" = "info" ] && [ "$2" = "/dev/disk9" ]; then
    echo "   Volume Name:               SAMPLE_MOVIE_2003"
    exit 0
fi
exit 1
"""
            env, capture = self._build_fake_env(
                root,
                makemkv_stdout=mkv_out,
                drutil_script=drutil_sh,
                diskutil_script=diskutil_sh,
            )
            res = subprocess.run(
                [str(WRAPPER), "--no-extras"],
                input="Sample Movie (2003)\nmovie\nBD\n",
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(res.returncode, 0, f"stdout={res.stdout}\nstderr={res.stderr}")
            captured = json.loads(capture.read_text())
            self.assertEqual(captured["BURNDVD_DRUTIL_DRIVE"], "2")
            self.assertEqual(captured["BURNDVD_DRIVE_DEVICE"], "/dev/disk9")
            self.assertEqual(captured["BURNDVD_DRIVE_SERIAL"], "SIK95G426222")
            self.assertIn("disc:1", captured["argv"])
            self.assertIn("--no-extras", captured["argv"])
            claim_dir = root / ".cache" / "burndvd" / "drive-claims"
            self.assertTrue((claim_dir / "serial-SIK95G426222.claim").is_dir())
            self.assertFalse((claim_dir / "disc-1.claim").exists())

    def test_wrapper_two_loaded_drives_menu_and_explicit_drive_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mkv_out = "\n".join(
                [
                    'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","DISC_ONE_2001","/dev/rdisk5"',
                    'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 SIK95G426222","DISC_TWO_2002","/dev/rdisk6"',
                ]
            )
            drutil_sh = """#!/bin/bash
if [ "$1" = "list" ]; then
    echo "  1  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported"
    echo "  2  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported"
    exit 0
fi
if [ "$1" = "status" ] && [ "$3" = "1" ]; then
    echo "  Type: BD-ROM  Name: /dev/disk5"
    echo "  Space Used: 22.00GB"
    exit 0
fi
if [ "$1" = "status" ] && [ "$3" = "2" ]; then
    echo "  Type: BD-ROM  Name: /dev/disk6"
    echo "  Space Used: 24.00GB"
    exit 0
fi
exit 1
"""
            diskutil_sh = """#!/bin/bash
if [ "$1" = "info" ] && [ "$2" = "/dev/disk5" ]; then
    echo "   Volume Name:               DISC_ONE_2001"
    exit 0
fi
if [ "$1" = "info" ] && [ "$2" = "/dev/disk6" ]; then
    echo "   Volume Name:               DISC_TWO_2002"
    exit 0
fi
exit 1
"""
            env, capture = self._build_fake_env(
                root,
                makemkv_stdout=mkv_out,
                drutil_script=drutil_sh,
                diskutil_script=diskutil_sh,
            )

            # Without --drive: prompts numbered menu; user picks [2].
            res_menu = subprocess.run(
                [str(WRAPPER)],
                input="2\nDisc Two (2002)\nmovie\nBD\n",
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(res_menu.returncode, 0, f"stdout={res_menu.stdout}\nstderr={res_menu.stderr}")
            self.assertIn("[1] /dev/disk5 (DRVSER00001) — DISC_ONE_2001", res_menu.stdout)
            self.assertIn("[2] /dev/disk6 (SIK95G426222) — DISC_TWO_2002", res_menu.stdout)
            cap1 = json.loads(capture.read_text())
            self.assertEqual(cap1["BURNDVD_DRIVE_DEVICE"], "/dev/disk6")
            self.assertEqual(cap1["BURNDVD_DRIVE_SERIAL"], "SIK95G426222")
            self.assertIn("disc:1", cap1["argv"])

            # Clear stale claim from finished run and test --drive by serial (no menu prompt!).
            capture.unlink()
            env_ne = {**env, "BURNDVD_NO_EXTRAS": "1"}
            res_flag = subprocess.run(
                [str(WRAPPER), "--drive", "DRVSER00001"],
                input="Disc One (2001)\nmovie\nBD\n",
                env=env_ne,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(res_flag.returncode, 0, f"stdout={res_flag.stdout}\nstderr={res_flag.stderr}")
            self.assertNotIn("Multiple drives have discs inserted", res_flag.stdout)
            cap2 = json.loads(capture.read_text())
            self.assertEqual(cap2["BURNDVD_DRIVE_DEVICE"], "/dev/disk5")
            self.assertEqual(cap2["BURNDVD_DRIVE_SERIAL"], "DRVSER00001")
            self.assertIn("disc:0", cap2["argv"])
            self.assertIn("--no-extras", cap2["argv"])

    def test_wrapper_yellow_concurrent_tv_warning_before_episode_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_state = root / "other-state.json"
            live_state.write_text(
                json.dumps(
                    {
                        "queue": [
                            {
                                "title": "Parallel Show",
                                "type": "tv-season",
                                "season": 1,
                                "target_root": str(root / "media" / "TV Shows" / "Parallel Show"),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            mkv_out = 'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","PARALLEL_SHOW_S1_D2","/dev/rdisk5"'
            drutil_sh = """#!/bin/bash
if [ "$1" = "list" ]; then
    echo "  1  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported"
    exit 0
fi
if [ "$1" = "status" ] && [ "$3" = "1" ]; then
    echo "  Type: BD-ROM  Name: /dev/disk5"
    echo "  Space Used: 22.00GB"
    exit 0
fi
exit 1
"""
            diskutil_sh = """#!/bin/bash
if [ "$1" = "info" ] && [ "$2" = "/dev/disk5" ]; then
    echo "   Volume Name:               PARALLEL_SHOW_S1_D2"
    exit 0
fi
exit 1
"""
            pgrep_sh = """#!/bin/bash
if printf '%s\\n' "$*" | grep -q 'ripqueue'; then
    if printf '%s\\n' "$*" | grep -q 'burndvd-state'; then
        exit 1
    fi
    echo "99999"
    exit 0
fi
exit 1
"""
            ps_sh = f"""#!/bin/bash
if [ "$1" = "-o" ] && [ "$3" = "-p" ] && [ "$4" = "99999" ]; then
    echo "python3 /u/ripqueue.py --state {live_state} --device disc:0"
    exit 0
fi
exit 0
"""
            env, _capture = self._build_fake_env(
                root,
                makemkv_stdout=mkv_out,
                drutil_script=drutil_sh,
                diskutil_script=diskutil_sh,
                pgrep_script=pgrep_sh,
                ps_script=ps_sh,
            )
            res = subprocess.run(
                [str(WRAPPER)],
                input="Parallel Show\ntv-season\nBD\n1\n5\n",
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(res.returncode, 0, f"stdout={res.stdout}\nstderr={res.stderr}")
            self.assertIn("\033[33mWARNING:", res.stdout)
            self.assertIn("Episode start", res.stdout)
            self.assertLess(
                res.stdout.index("\033[33mWARNING:"),
                res.stdout.index("Episode start"),
            )


    def test_wrapper_sweep_cleans_serial_dev_and_legacy_disc_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claim_dir = root / ".cache" / "burndvd" / "drive-claims"
            claim_dir.mkdir(parents=True)
            stale_serial = claim_dir / "serial-DRVSER00001.claim"
            stale_dev = claim_dir / "dev-disk9.claim"
            stale_legacy = claim_dir / "disc-0.claim"
            live_serial = claim_dir / "serial-LIVE123456.claim"
            for d in (stale_serial, stale_dev, stale_legacy, live_serial):
                d.mkdir()
                (d / "state").write_text(str(root / f"{d.name}.state"), encoding="utf-8")
            for d in (stale_serial, stale_dev, stale_legacy):
                (d / "pid").write_text("999999", encoding="utf-8")
            (live_serial / "pid").write_text(str(os.getpid()), encoding="utf-8")

            script = r"""
set -euo pipefail
export BURNDVD_LIBRARY_ONLY=1
source "$WRAPPER"
sweep_burndvd_orphans() {
    local claim_dir="$HOME/.cache/burndvd/drive-claims"
    if [ -d "$claim_dir" ]; then
        local claim claim_pid claim_state
        for claim in "$claim_dir"/serial-*.claim "$claim_dir"/dev-*.claim "$claim_dir"/disc-*.claim; do
            [ -d "$claim" ] || continue
            claim_pid=$(cat "$claim/pid" 2>/dev/null || true)
            if [ -n "$claim_pid" ] && [[ "$claim_pid" =~ ^[0-9]+$ ]] && kill -0 "$claim_pid" 2>/dev/null; then
                continue
            fi
            rm -rf "$claim" 2>/dev/null || true
        done
    fi
}
sweep_burndvd_orphans
"""
            # Verify the wrapper source itself sweeps all three patterns
            wrapper_src = WRAPPER.read_text(encoding="utf-8")
            self.assertIn(
                '"$claim_dir"/serial-*.claim "$claim_dir"/dev-*.claim "$claim_dir"/disc-*.claim',
                wrapper_src,
            )
            res = subprocess.run(
                ["/bin/bash", "-c", script],
                env={**os.environ, "HOME": str(root), "WRAPPER": str(WRAPPER)},
                text=True,
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertFalse(stale_serial.exists())
            self.assertFalse(stale_dev.exists())
            self.assertFalse(stale_legacy.exists())
            self.assertTrue(live_serial.is_dir())


if __name__ == "__main__":
    unittest.main()


class AutoNextEpisodeTests(unittest.TestCase):
    def test_compute_next_episode_uses_nas_plus_live_slot_and_prunes_dead_pid(self):
        from burndvd.metadata import (
            clear_tv_slot_reservation,
            compute_next_tv_episode,
            write_tv_slot_reservation,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            season_dir = root / "Dragon Ball Super" / "Season 01"
            season_dir.mkdir(parents=True)
            for ep in (90, 91):
                (season_dir / f"Dragon Ball Super - S01E{ep:02d}.mkv").write_bytes(b"data")
            slots_dir = root / "slots"

            # Spawn a real short-lived subprocess so its PID is alive during check
            holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
            try:
                write_tv_slot_reservation(
                    pid=holder.pid,
                    title="Dragon Ball Super",
                    season=1,
                    target_root="/private/nas/media/TV Shows/Dragon Ball Super",
                    episode_start=92,
                    episode_count=9,
                    slots_dir=slots_dir,
                )
                # Dead PID reservation should be ignored and pruned
                write_tv_slot_reservation(
                    pid=999999,
                    title="Dragon Ball Super",
                    season=1,
                    target_root="/private/nas/media/TV Shows/Dragon Ball Super",
                    episode_start=200,
                    episode_count=10,
                    slots_dir=slots_dir,
                )
                res = compute_next_tv_episode(
                    title="Dragon Ball Super",
                    season=1,
                    target_root="/Volumes/Media/TV Shows/Dragon Ball Super",
                    season_dir=season_dir,
                    slots_dir=slots_dir,
                    active_processes=[],
                )
                self.assertEqual(res["nas_highest"], 91)
                self.assertEqual(res["next_episode"], 101)
                self.assertEqual(len(res["reservations"]), 1)
                self.assertEqual(res["reservations"][0]["episode_start"], 92)
                self.assertEqual(res["reservations"][0]["episode_end"], 100)
                self.assertFalse((slots_dir / "pid-999999.json").exists())
                clear_tv_slot_reservation(holder.pid, slots_dir=slots_dir)
                self.assertFalse((slots_dir / f"pid-{holder.pid}.json").exists())
            finally:
                holder.terminate()
                holder.wait(timeout=5)

    def test_compute_next_episode_falls_back_to_active_log_selected_count(self):
        from burndvd.metadata import compute_next_tv_episode

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            season_dir = root / "Dragon Ball Super" / "Season 01"
            season_dir.mkdir(parents=True)
            (season_dir / "Dragon Ball Super - S01E91.mkv").write_bytes(b"data")
            queue_csv = root / "q.csv"
            queue_csv.write_text(
                "title,type,season,episode_start,expected_episodes,expected_disc_episodes,"
                "expected_title_ids,expected_physical_disc,discs,target_root,format,notes,features\n"
                "Dragon Ball Super,tv-season,1,92,,,,,1,/private/nas/media/TV Shows/Dragon Ball Super,BD,p8d1,\n",
                encoding="utf-8",
            )
            log_file = root / "burndvd-p8d1.log"
            log_file.write_text("  Selected 9 title(s): [0, 1, 2, 3, 4, 5, 6, 7, 8]\n", encoding="utf-8")

            res = compute_next_tv_episode(
                title="Dragon Ball Super",
                season=1,
                target_root="/private/nas/media/TV Shows/Dragon Ball Super",
                season_dir=season_dir,
                slots_dir=root / "empty-slots",
                active_processes=[
                    {
                        "pid": 54321,
                        "state_file": "",
                        "queue_file": str(queue_csv),
                        "log_file": str(log_file),
                    }
                ],
            )
            self.assertEqual(res["next_episode"], 101)
            self.assertEqual(res["reservations"][0]["source"], "log")
            self.assertEqual(res["reservations"][0]["episode_count"], 9)

    def test_wrapper_auto_populates_episode_start_from_live_slot_reservation(self):
        from burndvd.metadata import write_tv_slot_reservation

        helper = TestWrapperMultiDriveAndFeatures()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            slots_dir = root / "slots"
            drutil_sh = (
                "#!/bin/bash\n"
                "if [ \"$1\" = \"list\" ]; then\n"
                "    echo \"1  HL-DT-ST BD-RE BU40N 1.03 USB Unsupported\"\n"
                "    exit 0\n"
                "fi\n"
                "echo \" Vendor   Product           Rev\"\n"
                "echo \" HL-DT-ST BD-RE BU40N       1.03\"\n"
                "echo \"           Type: BD-ROM               Name: /dev/disk6\"\n"
                "echo \"     Space Used: 1000:00:00         blocks: 1000 / 23.4GB / 21.8GiB\"\n"
            )
            diskutil_sh = (
                "#!/bin/bash\n"
                "echo \"   Volume Name:               DragonBallSuper_P8_D2\"\n"
            )
            env, capture = helper._build_fake_env(
                root,
                makemkv_stdout='DRV:0,2,999,12,"BD-RE BU40N 1.03 DRVSER00002","DragonBallSuper_P8_D2","/dev/rdisk6"',
                drutil_script=drutil_sh,
                diskutil_script=diskutil_sh,
            )
            media_root = root / "media"
            season_dir = media_root / "TV Shows" / "Dragon Ball Super" / "Season 01"
            season_dir.mkdir(parents=True, exist_ok=True)
            (season_dir / "Dragon Ball Super - S01E91.mkv").write_bytes(b"data")
            env["BURNDVD_ACTIVE_TV_SLOTS_DIR"] = str(slots_dir)

            holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
            try:
                write_tv_slot_reservation(
                    pid=holder.pid,
                    title="Dragon Ball Super",
                    season=1,
                    target_root=str(media_root / "TV Shows" / "Dragon Ball Super"),
                    episode_start=92,
                    episode_count=9,
                    slots_dir=slots_dir,
                )
                res = subprocess.run(
                    [str(WRAPPER)],
                    input="Dragon Ball Super\ntv-season\nBD\n1\n\n",
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(res.returncode, 0, f"stdout={res.stdout}\nstderr={res.stderr}")
                self.assertIn("Auto episode start: 101", res.stdout)
                cap = json.loads(capture.read_text())
                q_idx = cap["argv"].index("--queue") + 1
                q_text = Path(cap["argv"][q_idx]).read_text(encoding="utf-8")
                self.assertIn(",101,", q_text)
            finally:
                holder.terminate()
                holder.wait(timeout=5)
