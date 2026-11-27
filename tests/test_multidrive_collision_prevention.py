"""Tests for per-slot flock guard, auto-built expected-volume guard, and year-stripped loose_match."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from burndvd import drive
from burndvd.cli import ripqueue
from burndvd.selection import loose_match


class MultiDriveCollisionPreventionTests(unittest.TestCase):
    def test_loose_match_strips_trailing_release_year(self):
        self.assertTrue(loose_match("THOR", "Thor (2011)"))
        self.assertTrue(loose_match("THOR_THE_DARK_WORLD", "Thor: The Dark World (2013)"))
        self.assertFalse(loose_match("THOR", "Naruto Shippuden"))
        self.assertFalse(loose_match("THOR", "The Irishman (2019)"))

    def test_disc_slot_lock_blocks_second_process_on_same_slot_allows_other_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "locks"
            with drive.disc_slot_lock("disc:0", lock_dir=lock_dir):
                probe_code = textwrap.dedent(f"""
                    import sys
                    from pathlib import Path
                    from burndvd import drive
                    lock_dir = Path({str(lock_dir)!r})
                    try:
                        with drive.disc_slot_lock("disc:0", lock_dir=lock_dir):
                            sys.exit(0)
                    except drive.DriveSlotConflictError:
                        sys.exit(42)
                """)
                r0 = subprocess.run(
                    [sys.executable, "-c", probe_code],
                    env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r0.returncode, 42, r0.stderr)

                probe_code_1 = probe_code.replace('"disc:0"', '"disc:1"')
                r1 = subprocess.run(
                    [sys.executable, "-c", probe_code_1],
                    env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r1.returncode, 0, r1.stderr)

            with drive.disc_slot_lock("disc:0", lock_dir=lock_dir):
                pass

    def test_bin_burndvd_passes_expected_volume_to_ripqueue(self):
        script = (Path(__file__).resolve().parents[1] / "bin" / "burndvd").read_text(encoding="utf-8")
        self.assertIn('--expected-volume "$VOLUME"', script)

    def test_ripqueue_refuses_mismatched_disc_on_auto_built_queue_even_noninteractive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            queue_csv = tmp_path / "queue.csv"
            queue_csv.write_text(
                "title,type,season,discs,episode_start,expected_episodes,target_root,format,notes\n"
                "Naruto Shippuden,tv-season,1,1,14,0,"
                f"{tmp_path / 'nas'},DVD,auto-built from NarutoShippuden_Vol4\n",
                encoding="utf-8",
            )
            state_json = tmp_path / "state.json"
            fake_mkv = tmp_path / "makemkvcon"
            fake_mkv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_mkv.chmod(0o755)

            wrong_disc_info = {
                "cinfo": {2: "THOR", 32: "THOR"},
                "titles": {0: {9: "1:54:00", 11: "57000000000", 2: "THOR"}},
            }

            argv = [
                "ripqueue.py",
                "--queue", str(queue_csv),
                "--state", str(state_json),
                "--device", "disc:1",
                "--makemkvcon", str(fake_mkv),
                "--non-interactive",
                "--on-fail=retry",
            ]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict(os.environ, {"BURNDVD_LOCK_DIR": str(tmp_path / "locks")}, clear=False), \
                 mock.patch.object(ripqueue, "_drive_idx_from_args", return_value=None), \
                 mock.patch.object(ripqueue, "check_key_expiry"), \
                 mock.patch.object(ripqueue, "resolve_makemkv_device", return_value="disc:1"), \
                 mock.patch.object(ripqueue, "wait_for_disc", return_value=("ok", wrong_disc_info)), \
                 mock.patch.object(ripqueue, "rip") as mock_rip:
                with self.assertRaises(SystemExit) as ctx:
                    ripqueue.main()
            self.assertEqual(ctx.exception.code, 4)
            mock_rip.assert_not_called()
            log_text = state_json.with_suffix(".log").read_text(encoding="utf-8")
            self.assertIn("DISC_INTENT_MISMATCH", log_text)
            self.assertIn("NarutoShippuden_Vol4", log_text)
            self.assertIn("THOR", log_text)

    def test_ripqueue_refuses_when_disc_slot_flock_already_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            lock_dir = tmp_path / "locks"
            queue_csv = tmp_path / "queue.csv"
            queue_csv.write_text(
                "title,type,season,discs,episode_start,expected_episodes,target_root,format,notes\n"
                f"Thor (2011),movie,0,1,1,0,{tmp_path / 'nas'},4K,auto-built from THOR\n",
                encoding="utf-8",
            )
            state_json = tmp_path / "state.json"
            fake_mkv = tmp_path / "makemkvcon"
            fake_mkv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_mkv.chmod(0o755)

            argv = [
                "ripqueue.py",
                "--queue", str(queue_csv),
                "--state", str(state_json),
                "--device", "disc:0",
                "--makemkvcon", str(fake_mkv),
                "--non-interactive",
            ]
            holder_code = textwrap.dedent(f"""
                import time
                from pathlib import Path
                from burndvd import drive
                with drive.disc_slot_lock("disc:0", lock_dir=Path({str(lock_dir)!r})):
                    print("LOCKED", flush=True)
                    time.sleep(15)
            """)
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_code],
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert holder.stdout is not None
                line = holder.stdout.readline().strip()
                if line != "LOCKED":
                    self.fail(f"expected LOCKED, got {line!r}; stderr={holder.stderr.read() if holder.stderr else ''}")
                with mock.patch.object(sys, "argv", argv), \
                     mock.patch.dict(os.environ, {"BURNDVD_LOCK_DIR": str(lock_dir)}, clear=False), \
                     mock.patch.object(ripqueue, "_drive_idx_from_args", return_value=None), \
                     mock.patch.object(ripqueue, "resolve_makemkv_device", return_value="disc:0"), \
                     mock.patch.object(ripqueue, "wait_for_disc") as mock_wait:
                    with self.assertRaises(SystemExit) as ctx:
                        ripqueue.main()
                self.assertEqual(ctx.exception.code, 4)
                mock_wait.assert_not_called()
                log_text = state_json.with_suffix(".log").read_text(encoding="utf-8")
                self.assertIn("DRIVE_SLOT_BUSY", log_text)
            finally:
                holder.terminate()
                holder.wait(timeout=5)

    def test_compute_next_tv_episode_ignores_same_queue_or_state_wrapper_parent(self):
        from burndvd.metadata import compute_next_tv_episode

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            season_dir = tmp_path / "TV Shows" / "Naruto Shippuden" / "Season 01"
            season_dir.mkdir(parents=True)
            queue_csv = tmp_path / "queue.csv"
            queue_csv.write_text(
                "title,type,season,discs,episode_start,expected_episodes,target_root,format,notes\n"
                f"Naruto Shippuden,tv-season,1,1,14,0,{tmp_path / 'TV Shows' / 'Naruto Shippuden'},DVD,test\n",
                encoding="utf-8",
            )
            state_json = tmp_path / "state.json"
            wrapper_procs = [
                {
                    "pid": 999991,
                    "queue_file": str(queue_csv),
                    "state_file": str(state_json),
                    "log_file": "",
                }
            ]
            info = compute_next_tv_episode(
                title="Naruto Shippuden",
                season=1,
                target_root=str(tmp_path / "TV Shows" / "Naruto Shippuden"),
                season_dir=season_dir,
                slots_dir=tmp_path / "slots",
                active_processes=wrapper_procs,
                exclude_queue_file=str(queue_csv),
                exclude_state_file=str(state_json),
            )
            self.assertEqual(info["pending_plans"], [])
            self.assertEqual(info["reservations"], [])

    def test_expected_volume_matches_cinfo_32_udf_volume_when_cinfo_2_differs(self):
        from burndvd.probe import disc_volume_candidates

        info = {
            "cinfo": {2: "MARVEL'S THOR: RAGNAROK", 32: "THOR_RAGNAROK"},
            "titles": {0: {9: "2:10:00", 11: "62000000000", 2: "THOR_RAGNAROK"}},
        }
        cands = disc_volume_candidates(info)
        self.assertEqual(cands[0], "THOR_RAGNAROK")
        self.assertIn("MARVEL'S THOR: RAGNAROK", cands)

    def test_active_busy_disc_claims_ignores_ancestor_wrapper_pids(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_procs = [
                (90010, "/bin/bash /path/bin/burndvd --internal-pty-run /log caffeinate ripqueue.py --device disc:1"),
                (90011, "/opt/homebrew/bin/makemkvcon -r mkv disc:0 0 /tmp/stage"),
            ]
            with mock.patch.object(drive, "_active_process_lines", return_value=fake_procs), \
                 mock.patch.object(drive, "_self_and_ancestor_pids", return_value={os.getpid(), 90010}):
                busy = drive.active_busy_disc_claims(
                    exclude_device_node="/dev/disk5",
                    exclude_pid=os.getpid(),
                    claim_dir=Path(tmp) / "empty-claims",
                )
            self.assertNotIn(1, busy)
            self.assertIn(0, busy)


if __name__ == "__main__":
    unittest.main()
