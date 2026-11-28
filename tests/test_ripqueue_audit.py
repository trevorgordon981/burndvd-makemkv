"""Comprehensive audit tests covering all 17 P0/P1/P2 fixes across the ripqueue library."""

from __future__ import annotations

import argparse
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _compat import ripqueue


def _make_tv_item(root: Path, **overrides) -> ripqueue.QueueItem:
    vals = dict(
        title="Test Show",
        type="tv-season",
        discs=1,
        target_root=str(root),
        format="BD",
        season=1,
        episode_start=1,
        expected_episodes=0,
        expected_disc_episodes=0,
        expected_title_ids=[],
        expected_physical_disc=0,
    )
    vals.update(overrides)
    return ripqueue.QueueItem(**vals)


def _make_movie_item(root: Path, **overrides) -> ripqueue.QueueItem:
    vals = dict(
        title="Test Movie",
        type="movie",
        discs=1,
        target_root=str(root),
        format="BD",
    )
    vals.update(overrides)
    return ripqueue.QueueItem(**vals)


class FakePopen:
    def __init__(self, lines: list[str], returncode: int = 0, on_start=None):
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self.returncode = returncode
        self.killed = False
        self.terminated = False
        if on_start:
            on_start()

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class AuditFixesTests(unittest.TestCase):
    def setUp(self):
        ripqueue.clear_finalize_errors()
        ripqueue._FINALIZE_THREADS.clear()

    def test_01_respin_drive_skips_eject_on_bu40n_slim_drive(self):
        args = SimpleNamespace(device="disc:0")
        with mock.patch.object(ripqueue, "unmount_blocking_volume") as unmount, \
             mock.patch.object(ripqueue, "_drive_identity", return_value="HL-DT-ST BD-RE BU40N 1.03"), \
             mock.patch.object(ripqueue.subprocess, "run") as srun, \
             mock.patch.object(ripqueue.time, "sleep") as sleep_fn:
            cycled = ripqueue.respin_drive(args)
        self.assertFalse(cycled)
        unmount.assert_called_once_with(None, drive_idx=0)
        srun.assert_not_called()
        sleep_fn.assert_called_once_with(ripqueue.PROBE_COOLDOWN_S)

    def test_01_respin_drive_cycles_tray_on_motorized_desktop_drive(self):
        args = SimpleNamespace(device="disc:1")
        with mock.patch.object(ripqueue, "unmount_blocking_volume") as unmount, \
             mock.patch.object(ripqueue, "_drive_identity", return_value="HL-DT-ST BD-RE WH16NS60 1.02"), \
             mock.patch.object(ripqueue.subprocess, "run") as srun, \
             mock.patch.object(ripqueue.time, "sleep"):
            cycled = ripqueue.respin_drive(args)
        self.assertTrue(cycled)
        unmount.assert_called_once_with(None, drive_idx=1)
        cmds = [call.args[0] for call in srun.call_args_list]
        self.assertEqual(
            cmds,
            [
                ["drutil", "-drive", "2", "tray", "eject"],
                ["drutil", "-drive", "2", "tray", "close"],
            ],
        )

    def test_01_probe_disc_gates_physical_disc_trouble_on_disc_present(self):
        args = SimpleNamespace(makemkvcon="/fake/makemkvcon", device="disc:0")
        lines = [
            'MSG:2003,0,1,"Error scsi tray open"',
            'MSG:5010,0,0,"Failed to open disc"',
        ]
        with mock.patch.object(ripqueue, "_optical_mounts", return_value=[]), \
             mock.patch.object(ripqueue.subprocess, "Popen", side_effect=lambda *a, **kw: FakePopen(lines)), \
             mock.patch.object(ripqueue, "disc_present", return_value=False):
            self.assertIsNone(ripqueue.probe_disc(args))

        with mock.patch.object(ripqueue, "_optical_mounts", return_value=[]), \
             mock.patch.object(ripqueue.subprocess, "Popen", side_effect=lambda *a, **kw: FakePopen(lines)), \
             mock.patch.object(ripqueue, "disc_present", return_value=True):
            res = ripqueue.probe_disc(args)
        self.assertIsNotNone(res)
        self.assertEqual(res["error"], "PHYSICAL_DISC_TROUBLE")
        self.assertEqual(res["codes"], [2003, 5010])

    def test_02_rip_double_feature_forces_sync_move_without_mutating_caller(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(sync_move=False, state=str(Path(tmp) / "state.json"))
            item = ripqueue.QueueItem(
                title="Double Feature",
                type="double-feature",
                discs=1,
                target_root=tmp,
                format="BD",
                features=[
                    {"title_id": 1, "name": "Movie A"},
                    {"title_id": 2, "name": "Movie B"},
                ],
            )
            info = {"titles": {1: {9: "1:30:00"}, 2: {9: "1:35:00"}}}
            seen_sync_flags = []

            def fake_rip(sub_args, sub_item, *rest):
                seen_sync_flags.append(sub_args.sync_move)
                return True, {
                    "elapsed_s": 10.0,
                    "files": [str(Path(sub_item.target_root) / f"{sub_item.title}.mkv")],
                }

            with mock.patch.object(ripqueue, "rip", side_effect=fake_rip):
                ok, res = ripqueue.rip_double_feature(args, item, info, {}, 1)
        self.assertTrue(ok)
        self.assertEqual(seen_sync_flags, [True, True])
        self.assertFalse(args.sync_move)
        self.assertEqual(len(res["files"]), 2)

    def test_03_background_move_fail_records_error_and_skips_subocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "staging" / "movie.mkv"
            src.parent.mkdir()
            src.write_bytes(b"data")
            dst = root / "out" / "movie.mkv"
            args = SimpleNamespace(
                verify=True,
                rerip_review=False,
                no_subocr=False,
                no_sound=True,
                state=str(root / "state.json"),
                disc_receipts=str(root / "receipts.jsonl"),
            )
            result = {
                "files": [str(dst)],
                "move_jobs": [(src, dst, "moving")],
                "staging": str(src.parent),
            }
            with mock.patch.object(ripqueue, "verify", return_value=(True, "")), \
                 mock.patch.object(ripqueue, "_await_dir_writable", return_value=None), \
                 mock.patch.object(ripqueue, "run_subocr_postrip") as subocr:
                ripqueue._run_finalize(args, result, _make_movie_item(root), 1)
            subocr.assert_not_called()
            errs = ripqueue.get_finalize_errors()
            self.assertEqual(len(errs), 1)
            self.assertIn("MOVE_FAIL", errs[0])

    def test_03_background_verify_fail_records_error_and_skips_subocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = SimpleNamespace(
                verify=True,
                rerip_review=False,
                no_subocr=False,
                no_sound=True,
                state=str(root / "state.json"),
                disc_receipts=str(root / "receipts.jsonl"),
            )
            result = {
                "files": [str(root / "ep1.mkv")],
                "move_jobs": [],
                "staging": None,
                "disc_fingerprint": "a" * 64,
            }
            with mock.patch.object(ripqueue, "verify", return_value=(False, "bad frame")), \
                 mock.patch.object(ripqueue, "append_disc_receipt") as receipt, \
                 mock.patch.object(ripqueue, "run_subocr_postrip") as subocr:
                ripqueue._run_finalize(args, result, _make_tv_item(root), 1)
            receipt.assert_not_called()
            subocr.assert_not_called()
            errs = ripqueue.get_finalize_errors()
            self.assertEqual(len(errs), 1)
            self.assertIn("VERIFY_FAIL", errs[0])

    def test_03_main_exits_2_on_sync_verify_fail_and_skips_subocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            qpath = root / "queue.csv"
            qpath.write_text(
                f"title,type,discs,target_root,format\nTest Movie,movie,1,{root},BD\n"
            )
            state_path = root / "state.json"
            fake_mkv = root / "Test Movie.mkv"
            fake_mkv.write_bytes(b"movie")
            info = {"cinfo": {2: "TEST_MOVIE"}, "titles": {0: {9: "1:40:00", 11: "1000"}}}
            argv = [
                "ripqueue.py",
                "--queue", str(qpath),
                "--state", str(state_path),
                "--makemkvcon", sys.executable,
                "--device", "iso:/fake.iso",
                "--non-interactive",
                "--sync-move",
                "--verify",
                "--no-eject",
                "--no-sound",
            ]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(ripqueue, "check_key_expiry"), \
                 mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info)), \
                 mock.patch.object(ripqueue, "rip", return_value=(True, {"elapsed_s": 5.0, "files": [str(fake_mkv)]})), \
                 mock.patch.object(ripqueue, "log_final_durations"), \
                 mock.patch.object(ripqueue, "verify", return_value=(False, "corrupt")), \
                 mock.patch.object(ripqueue, "run_subocr_postrip") as subocr:
                with self.assertRaises(SystemExit) as ctx:
                    ripqueue.main()
            self.assertEqual(ctx.exception.code, 2)
            subocr.assert_not_called()

    def test_04_planned_rip_selectors_never_skips_expected_title_ids(self):
        item = _make_tv_item(
            Path("/tmp/show"),
            expected_episodes=10,
            expected_disc_episodes=4,
            expected_physical_disc=1,
            expected_title_ids=[0, 1, 2, 4],
        )
        info = {
            "titles": {
                0: {9: "0:24:00", 11: str(5 * 10**9)},
                1: {9: "0:24:00", 11: str(5 * 10**9)},
                2: {9: "0:24:00", 11: str(5 * 10**9)},
                3: {9: "0:24:00", 11: str(5 * 10**9)},
                4: {9: "1:36:00", 11: str(20 * 10**9)},
            }
        }
        selectors, skipped = ripqueue.planned_rip_selectors(item, info, [0, 1, 2, 3])
        self.assertEqual(selectors, ["all"])
        self.assertEqual(skipped, set())

    def test_04_planned_rip_selectors_keeps_2part_featurette_and_sd_mismatch(self):
        item = _make_tv_item(Path("/tmp/show"))
        info_2part = {
            "titles": {
                0: {9: "0:24:00", 11: str(4 * 10**9)},
                1: {9: "0:24:00", 11: str(4 * 10**9)},
                2: {9: "0:46:00", 11: str(8 * 10**9)},
            }
        }
        self.assertIn(2, ripqueue.play_all_title_ids(info_2part))
        selectors, skipped = ripqueue.planned_rip_selectors(item, info_2part, [0, 1])
        self.assertEqual(selectors, ["all"])
        self.assertEqual(skipped, set())
        req_gb, sel_gb, fallback = ripqueue.staging_space_requirement(item, info_2part, [0, 1])
        self.assertFalse(fallback)
        self.assertAlmostEqual(sel_gb, 16.0, places=3)

        info_sd = {
            "titles": {
                0: {9: "0:22:00", 11: str(5 * 10**9)},
                1: {9: "0:22:00", 11: str(5 * 10**9)},
                2: {9: "0:22:00", 11: str(5 * 10**9)},
                3: {9: "1:06:00", 11: str(2 * 10**9)},
            }
        }
        selectors, skipped = ripqueue.planned_rip_selectors(item, info_sd, [0, 1, 2])
        self.assertEqual(selectors, ["all"])
        self.assertEqual(skipped, set())

        info_ok = {
            "titles": {
                0: {9: "0:24:00", 11: str(5 * 10**9)},
                1: {9: "0:24:00", 11: str(5 * 10**9)},
                2: {9: "0:24:00", 11: str(5 * 10**9)},
                3: {9: "0:24:00", 11: str(5 * 10**9)},
                4: {9: "1:36:00", 11: str(20_500_000_000)},
            }
        }
        selectors, skipped = ripqueue.planned_rip_selectors(item, info_ok, [0, 1, 2, 3])
        self.assertEqual(selectors, ["0", "1", "2", "3"])
        self.assertEqual(skipped, {4})
        req_gb, sel_gb, _ = ripqueue.staging_space_requirement(item, info_ok, [0, 1, 2, 3])
        self.assertAlmostEqual(sel_gb, 20.0, places=3)
        self.assertAlmostEqual(req_gb, 20.0 * 1.05 + 8.0, places=3)

    def test_05_starting_ep_respects_active_claims_and_target_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smb_fake = root / "smb" / "Show"
            nfs_real = root / "nfs" / "Show" / "Season 01"
            nfs_real.mkdir(parents=True)
            item = _make_tv_item(smb_fake, title="Show", season=1)
            for ep in (1, 2, 3):
                dst = nfs_real / f"Show - S01E{ep:02d}.mkv"
                ripqueue._create_tv_slot_claim(ripqueue.tv_slot_claim_path(dst))
            self.assertEqual(ripqueue.starting_ep(item, 1, {}, target_dir=nfs_real), 4)

    def test_05b_starting_ep_respects_explicit_gap_fill_when_higher_episodes_exist(self):
        """Explicit episode_start into an empty gap (e.g. E22 when E01-E21 and E27-E48 exist)
        must NOT be overridden to highest + 1 (E49)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            season = root / "TV Shows" / "Naruto Shippuden" / "Season 01"
            season.mkdir(parents=True)
            for ep in (20, 21, 27, 48):
                (season / f"Naruto Shippuden - S01E{ep:02d}.mkv").write_bytes(b"x")
            gap_item = _make_tv_item(season.parent, title="Naruto Shippuden", season=1, episode_start=22)
            self.assertEqual(ripqueue.starting_ep(gap_item, 1, {}, target_dir=season), 22)
            # Default/occupied starting slot still advances past highest.
            dup_item = _make_tv_item(season.parent, title="Naruto Shippuden", season=1, episode_start=20)
            self.assertEqual(ripqueue.starting_ep(dup_item, 1, {}, target_dir=season), 49)

    def test_06_resolve_nas_path_and_await_dir_writable_failover(self):
        with tempfile.TemporaryDirectory() as tmp:
            smb_root = Path(tmp) / "smb-media"
            nfs_root = Path(tmp) / "nfs-media"
            smb_root.mkdir()
            nfs_root.mkdir()
            smb_tv = smb_root / "TV Shows" / "Show" / "Season 01"
            nfs_tv = nfs_root / "TV Shows" / "Show" / "Season 01"
            smb_movie_dir = smb_root / "Movies" / "Test"
            nfs_movie_dir = nfs_root / "Movies" / "Test"

            with mock.patch.object(ripqueue, "SMB_MEDIA_PREFIX", str(smb_root)), \
                 mock.patch.object(ripqueue, "NFS_MEDIA_PREFIX", str(nfs_root)):
                with mock.patch.object(ripqueue, "_nfs_mount_available", return_value=True), \
                     mock.patch.object(ripqueue.os.path, "ismount", return_value=True):
                    self.assertEqual(ripqueue.resolve_nas_path(smb_tv, prefer_nfs=True), nfs_tv)
                    self.assertEqual(ripqueue.resolve_nas_path(smb_tv, prefer_nfs=False), smb_tv)
                with mock.patch.object(ripqueue, "_nfs_mount_available", return_value=True), \
                     mock.patch.object(ripqueue.os.path, "ismount", return_value=False):
                    self.assertEqual(ripqueue.resolve_nas_path(smb_tv, prefer_nfs=False), nfs_tv)

                with mock.patch.object(
                    ripqueue, "_dir_writable", side_effect=lambda p: Path(p) == nfs_movie_dir
                ), mock.patch.object(ripqueue.time, "sleep") as sleep_fn:
                    got = ripqueue._await_dir_writable(smb_movie_dir, str(Path(tmp) / "staging"))
                self.assertEqual(got, nfs_movie_dir)
                sleep_fn.assert_not_called()

    def test_06_run_finalize_updates_files_on_nfs_failover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "staging" / "movie.mkv"
            src.parent.mkdir()
            src.write_bytes(b"movie-bytes")
            nfs_dir = root / "nfs_movies"
            nfs_dir.mkdir()
            smb_dst = Path("/Volumes/Media/Movies/Test/Test.mkv")
            expected_nfs_dst = nfs_dir / "Test.mkv"
            args = SimpleNamespace(
                verify=True,
                rerip_review=False,
                no_subocr=True,
                no_sound=True,
                state=str(root / "state.json"),
                disc_receipts=str(root / "receipts.jsonl"),
            )
            result = {
                "files": [str(smb_dst)],
                "move_jobs": [(src, smb_dst, "moving")],
                "staging": str(src.parent),
            }
            verified_paths = []
            dst_existed_during_verify = []

            def fake_verify(paths):
                verified_paths.extend(paths)
                dst_existed_during_verify.append(expected_nfs_dst.exists())
                return True, ""

            with mock.patch.object(ripqueue, "_await_dir_writable", return_value=nfs_dir), \
                 mock.patch.object(ripqueue, "verify", side_effect=fake_verify):
                ripqueue._run_finalize(args, result, _make_movie_item(root), 1)
            self.assertTrue(expected_nfs_dst.is_file())
            self.assertEqual(result["files"], [str(expected_nfs_dst)])
            self.assertEqual(verified_paths, [str(src)])
            self.assertEqual(dst_existed_during_verify, [False])

    def test_07_move_extras_disambiguates_and_catches_fileexistserror(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "Media"
            show = media / "TV Shows" / "Show"
            season = show / "Season 01"
            extras = show / "Extras"
            season.mkdir(parents=True)
            extras.mkdir(parents=True)
            existing_extra = extras / "Show - S01D1 - extra - t01.mkv"
            existing_extra.write_bytes(b"existing-disc1-extra")

            item = _make_tv_item(show, title="Show", season=1)
            info = {
                "cinfo": {2: "SHOW_GENERIC"},
                "titles": {0: {9: "0:24:00", 11: "1000"}, 1: {9: "0:05:00", 11: "500"}},
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
                current_disc_fingerprint="abcdef1234567890" * 4,
                review_root=None,
                state=str(root / "state.json"),
            )

            def fake_popen(cmd, **kw):
                staging_dir = Path(cmd[-1])
                (staging_dir / "title_t00.mkv").write_bytes(b"episode-bytes")
                (staging_dir / "title_t01.mkv").write_bytes(b"new-disc2-extra")
                return FakePopen(['MSG:5036,0,1,"Copy complete. 2 titles saved."'])

            with mock.patch.object(ripqueue, "LOCAL_STAGING_BASE", root / "staging"), \
                 mock.patch.object(ripqueue, "LOCAL_LOCK_BASE", root / "locks"), \
                 mock.patch.object(ripqueue, "_free_gb", return_value=500.0), \
                 mock.patch.object(ripqueue.subprocess, "Popen", side_effect=fake_popen):
                ok, res = ripqueue.rip(args, item, info, [0], season, {}, 1)
            self.assertTrue(ok, res)
            self.assertEqual(existing_extra.read_bytes(), b"existing-disc1-extra")
            disamb = extras / "Show - S01D1 - extra - t01 - abcdef12.mkv"
            self.assertTrue(disamb.is_file())
            self.assertTrue((season / "Show - S01E01.mkv").is_file())

    def test_08_multi_selector_resets_sel_start_preserves_total_elapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            show = root / "Media" / "TV Shows" / "Show"
            season = show / "Season 01"
            season.mkdir(parents=True)
            item = _make_tv_item(show, title="Show", season=1)
            info = {
                "cinfo": {2: "SHOW"},
                "titles": {
                    0: {9: "0:24:00", 11: "1000"},
                    1: {9: "0:24:00", 11: "1000"},
                    2: {9: "0:24:00", 11: "1000"},
                    3: {9: "1:12:00", 11: "3000"},
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
                sync_move=False,
                no_split_playall=True,
                current_disc_fingerprint="b" * 64,
                review_root=None,
                state=str(root / "state.json"),
            )
            clock = [1000.0]
            draw_starts = []

            def fake_time():
                clock[0] += 1.0
                return clock[0]

            def fake_draw(s, total_v, mx, task, stall):
                draw_starts.append((task, s))

            sel_call = [0]

            def fake_popen(cmd, **kw):
                sel_call[0] += 1
                if sel_call[0] == 2:
                    clock[0] += 500.0
                staging_dir = Path(cmd[-1])
                tid = int(cmd[-2])
                (staging_dir / f"title_t{tid:02d}.mkv").write_bytes(b"ep")
                return FakePopen(
                    [
                        'PRGT:0,0,"Saving"',
                        "PRGV:5000,5000,65536",
                        'MSG:5036,0,1,"Copy complete. 1 titles saved."',
                    ]
                )

            with mock.patch.object(ripqueue, "LOCAL_STAGING_BASE", root / "staging"), \
                 mock.patch.object(ripqueue, "LOCAL_LOCK_BASE", root / "locks"), \
                 mock.patch.object(ripqueue, "_free_gb", return_value=500.0), \
                 mock.patch.object(ripqueue, "_alt_playlist_duplicate", return_value=False), \
                 mock.patch.object(ripqueue.time, "time", side_effect=fake_time), \
                 mock.patch.object(ripqueue, "_draw_progress", side_effect=fake_draw), \
                 mock.patch.object(ripqueue.subprocess, "Popen", side_effect=fake_popen):
                ok, res = ripqueue.rip(args, item, info, [0, 1, 2], season, {}, 1)
            self.assertTrue(ok, res)
            sel1_starts = [s for task, s in draw_starts if "[1/3]" in task]
            sel2_starts = [s for task, s in draw_starts if "[2/3]" in task]
            sel3_starts = [s for task, s in draw_starts if "[3/3]" in task]
            self.assertTrue(sel1_starts and sel2_starts and sel3_starts)
            self.assertGreater(sel2_starts[0], sel1_starts[0])
            self.assertGreater(sel3_starts[0], sel2_starts[0])
            self.assertGreater(res["elapsed_s"], 500.0)

    def test_09_claim_writes_pid_and_ignores_dead_pid_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            season = root / "Show" / "Season 01"
            season.mkdir(parents=True)
            item = _make_tv_item(root / "Show", title="Show", season=1)

            dst1 = season / "Show - S01E01.mkv"
            claim1 = ripqueue.tv_slot_claim_path(dst1)
            ripqueue._create_tv_slot_claim(claim1)
            self.assertEqual(claim1.read_text(), f"{os.getpid()}\n")

            dead_proc = subprocess.Popen([sys.executable, "-c", "pass"])
            dead_pid = dead_proc.pid
            dead_proc.wait()
            dst2 = season / "Show - S01E02.mkv"
            claim2 = ripqueue.tv_slot_claim_path(dst2)
            claim2.write_text(f"{dead_pid}\n")

            dst3 = season / "Show - S01E03.mkv"
            claim3 = ripqueue.tv_slot_claim_path(dst3)
            claim3.touch()

            slots = ripqueue.existing_episode_slots(item, season, include_claims=True)
            self.assertIn(1, slots)
            self.assertNotIn(2, slots)
            self.assertIn(3, slots)

    def test_10_unmount_double_timeout_and_drive_targeting(self):
        mounts_out = (
            "/dev/disk5 on /Volumes/DISC_A (udf, local, read-only)\n"
            "/dev/disk6 on /Volumes/DISC_B (udf, local, read-only)\n"
        )
        diskutil_cmds = []

        def fake_run(cmd, **kw):
            if cmd[0] == "pkill":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if cmd[0] == ripqueue.MOUNT_BIN:
                return SimpleNamespace(returncode=0, stdout=mounts_out, stderr="")
            if cmd[0] == ripqueue.DISKUTIL_BIN:
                diskutil_cmds.append(cmd)
                raise subprocess.TimeoutExpired(cmd, 30)
            raise AssertionError(f"unexpected cmd: {cmd}")

        with mock.patch.object(ripqueue, "_drutil_disk_node", return_value="/dev/disk5"), \
             mock.patch.object(ripqueue.subprocess, "run", side_effect=fake_run):
            res = ripqueue.unmount_blocking_volume(None, drive_idx=0)
        self.assertFalse(res)
        self.assertEqual(
            diskutil_cmds,
            [
                [ripqueue.DISKUTIL_BIN, "unmount", "/dev/disk5"],
                [ripqueue.DISKUTIL_BIN, "unmount", "force", "/dev/disk5"],
            ],
        )

    def test_11_log_final_durations_runs_after_async_nas_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "staging" / "movie.mkv"
            src.parent.mkdir()
            src.write_bytes(b"hello-world")
            dst = root / "nas" / "movie.mkv"
            dst.parent.mkdir()
            order = []

            real_move = ripqueue.move_with_progress

            def tracked_move(s, d, **kw):
                order.append(("move", d.exists()))
                real_move(s, d, **kw)

            def tracked_log(a, it, dn, fls):
                order.append(("log_final", Path(fls[0]).exists()))

            args = SimpleNamespace(
                verify=False,
                rerip_review=False,
                no_subocr=True,
                no_sound=True,
                state=str(root / "state.json"),
                disc_receipts=str(root / "receipts.jsonl"),
            )
            result = {
                "files": [str(dst)],
                "move_jobs": [(src, dst, "moving")],
                "staging": str(src.parent),
            }
            with mock.patch.object(ripqueue, "move_with_progress", side_effect=tracked_move), \
                 mock.patch.object(ripqueue, "log_final_durations", side_effect=tracked_log):
                ripqueue._run_finalize(args, result, _make_movie_item(root), 1)
            self.assertEqual(order, [("move", False), ("log_final", True)])

    def test_12_multi_disc_queue_advances_on_queue_disc_n(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            qpath = root / "queue.csv"
            qpath.write_text(
                f"title,type,discs,target_root,format,season\nShow,tv-season,2,{root / 'Show'},BD,1\n"
            )
            state_path = root / "state.json"
            infos = iter(
                [
                    ("disc", {"cinfo": {2: "SHOW_DISC4"}, "titles": {0: {9: "0:24:00", 11: "1000"}}}),
                    ("disc", {"cinfo": {2: "SHOW_DISC5"}, "titles": {0: {9: "0:24:00", 11: "1000"}}}),
                ]
            )
            rip_calls = []

            def fake_rip(d_args, it, inf, tids, tgt, st, dn):
                rip_calls.append(dn)
                return True, {"elapsed_s": 5.0, "files": [str(root / f"ep{dn}.mkv")]}

            argv = [
                "ripqueue.py",
                "--queue", str(qpath),
                "--state", str(state_path),
                "--makemkvcon", sys.executable,
                "--device", "iso:/fake.iso",
                "--non-interactive",
                "--sync-move",
                "--no-eject",
                "--no-sound",
                "--no-subocr",
            ]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(ripqueue, "check_key_expiry"), \
                 mock.patch.object(ripqueue, "wait_for_disc", side_effect=lambda a, s: next(infos)), \
                 mock.patch.object(ripqueue, "rip", side_effect=fake_rip), \
                 mock.patch.object(ripqueue, "log_final_durations"):
                ripqueue.main()
            self.assertEqual(rip_calls, [4, 5])
            final_state = json.loads(state_path.read_text())
            self.assertEqual(final_state["current_index"], 1)
            self.assertEqual(len(final_state["completed"]), 2)

    def test_13_monitor_fd_closed_once_on_post_copy_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src.mkv"
            dst = root / "dst.mkv"
            src.write_bytes(b"episode")
            dst.write_bytes(b"existing")

            real_dup = os.dup
            real_close = os.close
            dup_fds = []
            closed_fds = []

            def track_dup(fd):
                new_fd = real_dup(fd)
                dup_fds.append(new_fd)
                return new_fd

            def track_close(fd):
                closed_fds.append(fd)
                return real_close(fd)

            with mock.patch.object(ripqueue.os, "dup", side_effect=track_dup), \
                 mock.patch.object(ripqueue.os, "close", side_effect=track_close):
                with self.assertRaises(FileExistsError):
                    ripqueue.move_with_progress_noclobber(
                        src, dst, interval=0.001, partial_root=root / "partials"
                    )
            self.assertGreaterEqual(len(dup_fds), 2)
            monitor_fd = dup_fds[1]
            self.assertEqual(closed_fds.count(monitor_fd), 1)

    def test_14_dual_equal_length_play_all_both_detected(self):
        info = {
            "titles": {
                0: {9: "0:24:00"},
                1: {9: "0:24:00"},
                2: {9: "0:24:00"},
                3: {9: "0:24:00"},
                4: {9: "1:36:00"},
                5: {9: "1:36:00"},
            }
        }
        self.assertEqual(ripqueue.play_all_title_ids(info), {4, 5})

        finale_info = {
            "titles": {
                0: {9: "0:24:00"},
                1: {9: "0:24:00"},
                2: {9: "0:24:00"},
                3: {9: "0:24:00"},
                4: {9: "0:48:00"},
            }
        }
        self.assertEqual(ripqueue.play_all_title_ids(finale_info), set())

    def test_15_check_key_expiry_queries_disc_9999_not_live_device(self):
        args = SimpleNamespace(makemkvcon="/fake/makemkvcon", device="disc:0")
        seen_cmds = []

        def fake_popen(cmd, **kw):
            seen_cmds.append(cmd)
            return FakePopen(['MSG:1005,0,1,"Evaluation version, 25 day(s) out of 30 remaining"'])

        with mock.patch.object(ripqueue.subprocess, "Popen", side_effect=fake_popen):
            ripqueue.check_key_expiry(args)
        self.assertEqual(len(seen_cmds), 1)
        self.assertEqual(seen_cmds[0][-2:], ["info", "disc:9999"])
        self.assertNotIn("disc:0", seen_cmds[0])

    def test_16_absolute_bins_oserror_minlength_and_playall_only_budget(self):
        self.assertEqual(ripqueue.DISKUTIL_BIN, "/usr/sbin/diskutil")
        self.assertEqual(ripqueue.MOUNT_BIN, "/sbin/mount")

        args_pr = SimpleNamespace(makemkvcon="/fake/makemkvcon", device="disc:0")
        seen_cmds = []

        def fake_popen(cmd, **kw):
            seen_cmds.append(cmd)
            return FakePopen(['TINFO:0,9,0,"0:24:00"'])

        with mock.patch.object(ripqueue, "_optical_mounts", return_value=[]), \
             mock.patch.object(ripqueue.subprocess, "Popen", side_effect=fake_popen):
            ripqueue.probe_disc(args_pr)
        self.assertIn(f"--minlength={ripqueue.EXTRAS_MIN_DUR_S}", seen_cmds[0])

        item = _make_tv_item(Path("/tmp/show"))
        info_playall_only = {
            "titles": {
                0: {9: "2:40:00", 11: str(20 * 10**9)},
                1: {9: "0:08:00", 11: str(1 * 10**9)},
            }
        }
        req_gb, sel_gb, fallback = ripqueue.staging_space_requirement(item, info_playall_only, [0])
        self.assertFalse(fallback)
        self.assertAlmostEqual(sel_gb, 21.0, places=3)
        self.assertAlmostEqual(req_gb, 21.0 * 2.05 + 8.0, places=3)

    def test_17_run_subocr_postrip_uses_flock_not_mkdir_pgrep(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            subocr_bin = fake_home / ".local" / "bin" / "subocr"
            subocr_bin.parent.mkdir(parents=True)
            subocr_bin.write_text("#!/bin/bash\nexit 0\n")
            args = SimpleNamespace(no_subocr=False)
            seen = []

            def fake_popen(cmd, **kw):
                seen.append((cmd, kw))
                return SimpleNamespace(pid=12345)

            with mock.patch.object(Path, "home", return_value=fake_home), \
                 mock.patch.object(ripqueue.subprocess, "Popen", side_effect=fake_popen):
                ripqueue.run_subocr_postrip(args, ["/nas/ep1.mkv", "/nas/ep2.mkv"])

            self.assertEqual(len(seen), 1)
            cmd, kw = seen[0]
            self.assertNotIn("fcntl.flock", cmd[2])
            self.assertNotIn("mkdir", cmd[2])
            self.assertNotIn("pgrep", cmd[2])
            self.assertTrue(kw["start_new_session"])


if __name__ == "__main__":
    unittest.main()
