"""Tests for per-slot flock guard, auto-built expected-volume guard, and year-stripped loose_match."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from burndvd import drive, probe, ripper
from burndvd.cli import ripqueue
from burndvd.models import QueueItem
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

    def _external_slot_locked(
        self,
        device_spec: str,
        lock_dir: Path,
        *,
        drive_device: str = "",
        drive_serial: str = "",
    ) -> bool:
        probe_code = textwrap.dedent(
            """
            import sys
            from pathlib import Path
            from burndvd import drive
            try:
                with drive.disc_slot_lock(
                    sys.argv[1],
                    lock_dir=Path(sys.argv[2]),
                    drive_device=sys.argv[3] or None,
                    drive_serial=sys.argv[4] or None,
                ):
                    sys.exit(0)
            except drive.DriveSlotConflictError:
                sys.exit(42)
            """
        )
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in (drive.DRIVE_DEVICE_ENV, drive.DRIVE_SERIAL_ENV, "BURNDVD_LOCK_DIR")
        }
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        res = subprocess.run(
            [sys.executable, "-c", probe_code, device_spec, str(lock_dir), drive_device, drive_serial],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertIn(res.returncode, (0, 42), f"unexpected probe rc={res.returncode}: {res.stderr}")
        return res.returncode == 42

    def _spawn_external_lock_holder(self, device_spec: str, lock_dir: Path) -> subprocess.Popen:
        holder_code = textwrap.dedent(
            f"""
            import time
            from pathlib import Path
            from burndvd import drive
            with drive.disc_slot_lock({device_spec!r}, lock_dir=Path({str(lock_dir)!r})):
                print("LOCKED", flush=True)
                time.sleep(20)
            """
        )
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in (drive.DRIVE_DEVICE_ENV, drive.DRIVE_SERIAL_ENV, "BURNDVD_LOCK_DIR")
        }
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert holder.stdout is not None
        line = holder.stdout.readline().strip()
        if line != "LOCKED":
            err = holder.stderr.read() if holder.stderr else ""
            holder.terminate()
            holder.wait(timeout=5)
            self.fail(f"expected LOCKED from holder, got {line!r}; stderr={err}")
        return holder

    def test_disc_slot_lock_reentrant_same_scope_and_physical_drive_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp) / "locks"
            env_patch = {
                "BURNDVD_LOCK_DIR": str(lock_dir),
                drive.DRIVE_DEVICE_ENV: "/dev/disk6",
                drive.DRIVE_SERIAL_ENV: "DRVSER00001",
            }
            with mock.patch.dict(os.environ, env_patch, clear=False):
                with drive.disc_slot_lock("disc:0"):
                    self.assertTrue((lock_dir / "disc-0.lock").exists())
                    self.assertTrue((lock_dir / "dev-disk6.lock").exists())
                    self.assertTrue((lock_dir / "serial-DRVSER00001.lock").exists())
                    # Same-scope re-entry succeeds without self-deadlock.
                    with drive.disc_slot_lock("disc:0"):
                        self.assertTrue(self._external_slot_locked("disc:0", lock_dir))
                        # Another process targeting a different slot but same physical drive/serial is blocked.
                        self.assertTrue(
                            self._external_slot_locked("disc:1", lock_dir, drive_device="/dev/disk6")
                        )
                        self.assertTrue(
                            self._external_slot_locked("disc:1", lock_dir, drive_serial="DRVSER00001")
                        )
                        # Different slot AND different physical drive is free.
                        self.assertFalse(
                            self._external_slot_locked(
                                "disc:1",
                                lock_dir,
                                drive_device="/dev/disk5",
                                drive_serial="DRVSER00002",
                            )
                        )
                    # Outer lock stays held after inner re-entrant block exits.
                    self.assertTrue(self._external_slot_locked("disc:0", lock_dir))
                    self.assertTrue(
                        self._external_slot_locked("disc:1", lock_dir, drive_device="/dev/disk6")
                    )
            # All locks released after outermost block exits.
            self.assertFalse(self._external_slot_locked("disc:0", lock_dir))
            self.assertFalse(
                self._external_slot_locked("disc:1", lock_dir, drive_device="/dev/disk6")
            )
            self.assertFalse(
                self._external_slot_locked("disc:1", lock_dir, drive_serial="DRVSER00001")
            )

    def test_remap_to_externally_locked_slot_refuses_probe_and_rip_without_popen(self):
        """DVD-slot-re-resolution regression:
        Startup holds disc:0 lock; mid-run resolve_makemkv_device remaps to disc:1
        while another process holds disc-1.lock -> BOTH probe_disc and rip_disc
        MUST raise DriveSlotConflictError and NEVER call subprocess.Popen."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "locks"
            state_path = root / "state.json"
            target_dir = root / "library" / "Sample Movie (2024)"
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                drive_device="/dev/disk6",
                state=str(state_path),
                overwrite=False,
                min_free_gb=0,
                no_extras=False,
                no_split_playall=False,
                verify=False,
                sync_move=True,
                allow_mkvmerge_fallback=False,
                non_interactive=True,
            )
            item = QueueItem(
                title="Sample Movie (2024)",
                type="movie",
                discs=1,
                target_root=str(target_dir),
                format="DVD",
            )
            info = {
                "cinfo": {2: "SAMPLE_MOVIE", 32: "SAMPLE_MOVIE"},
                "titles": {0: {9: "1:45:00", 11: "4000000000", 2: "SAMPLE_MOVIE"}},
            }
            state = {
                "queue": [],
                "current_index": 0,
                "disc_index_in_item": 0,
                "completed": [],
                "rip_durations_s": [],
                "disc_episode_counts": {},
            }

            holder = self._spawn_external_lock_holder("disc:1", lock_dir)
            try:
                with mock.patch.dict(
                    os.environ,
                    {"BURNDVD_LOCK_DIR": str(lock_dir), drive.DRIVE_DEVICE_ENV: "/dev/disk6"},
                    clear=False,
                ):
                    with drive.disc_slot_lock("disc:0", target=args):
                        # 1. probe_disc remaps to busy disc:1 -> raises DriveSlotConflictError, zero Popen.
                        with mock.patch.object(probe, "_optical_mounts", return_value=[]), \
                             mock.patch.object(probe, "_drive_idx_from_args", return_value=None), \
                             mock.patch.object(probe, "resolve_makemkv_device", return_value="disc:1"), \
                             mock.patch.object(probe.subprocess, "Popen") as probe_popen:
                            with self.assertRaises(drive.DriveSlotConflictError):
                                probe.probe_disc(args)
                        probe_popen.assert_not_called()

                        # 2. rip_disc remaps to busy disc:1 -> raises DriveSlotConflictError, zero Popen.
                        with mock.patch.object(ripper, "resolve_makemkv_device", return_value="disc:1"), \
                             mock.patch.object(ripper, "resolve_nas_path", side_effect=lambda p, **_: p), \
                             mock.patch.object(ripper, "_free_gb", return_value=500.0), \
                             mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "stage"), \
                             mock.patch.object(ripper.subprocess, "Popen") as rip_popen:
                            with self.assertRaises(drive.DriveSlotConflictError):
                                ripper.rip_disc(args, item, info, [0], target_dir, state, 1)
                        rip_popen.assert_not_called()

                        # Outer disc:0 and dev-disk6 locks remain intact after failed re-acquisition.
                        self.assertTrue(self._external_slot_locked("disc:0", lock_dir))
                        self.assertTrue(
                            self._external_slot_locked("disc:2", lock_dir, drive_device="/dev/disk6")
                        )

                log_text = state_path.with_suffix(".log").read_text(encoding="utf-8")
                self.assertIn("PROBE_REFUSED", log_text)
                self.assertIn("RIP_REFUSED", log_text)
            finally:
                holder.terminate()
                holder.wait(timeout=5)

    def test_remap_to_free_slot_acquires_disc1_across_popen_and_releases_cleanly(self):
        """When startup holds disc:0 and mid-run resolve_makemkv_device remaps to a FREE disc:1,
        rip_disc acquires disc-1.lock across Popen and releases disc-1.lock cleanly on exit."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "locks"
            state_path = root / "state.json"
            target_dir = root / "library" / "Sample Movie (2024)"
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                drive_device="/dev/disk6",
                state=str(state_path),
                overwrite=False,
                min_free_gb=0,
                no_extras=False,
                no_split_playall=False,
                verify=False,
                sync_move=True,
                allow_mkvmerge_fallback=False,
                non_interactive=True,
            )
            item = QueueItem(
                title="Sample Movie (2024)",
                type="movie",
                discs=1,
                target_root=str(target_dir),
                format="DVD",
            )
            info = {
                "cinfo": {2: "SAMPLE_MOVIE", 32: "SAMPLE_MOVIE"},
                "titles": {0: {9: "1:45:00", 11: "100000", 2: "SAMPLE_MOVIE"}},
            }
            state = {
                "queue": [],
                "current_index": 0,
                "disc_index_in_item": 0,
                "completed": [],
                "rip_durations_s": [],
                "disc_episode_counts": {},
            }
            observed: dict[str, object] = {}
            real_popen = subprocess.Popen

            def fake_popen(cmd, *a, **kwargs):
                if not cmd or cmd[0] != args.makemkvcon:
                    return real_popen(cmd, *a, **kwargs)
                observed["cmd"] = list(cmd)
                observed["disc0_locked_during_popen"] = self._external_slot_locked("disc:0", lock_dir)
                observed["disc1_locked_during_popen"] = self._external_slot_locked("disc:1", lock_dir)
                staging_dir = Path(cmd[-1])
                (staging_dir / "title_t00.mkv").write_bytes(b"fake-mkv-payload")
                return SimpleNamespace(
                    stdout=io.StringIO('MSG:5036,0,1,"Copy complete. 1 titles saved."\n'),
                    returncode=0,
                    wait=lambda timeout=None: 0,
                    terminate=lambda: None,
                    kill=lambda: None,
                )

            with mock.patch.dict(
                os.environ,
                {"BURNDVD_LOCK_DIR": str(lock_dir), drive.DRIVE_DEVICE_ENV: "/dev/disk6"},
                clear=False,
            ):
                with drive.disc_slot_lock("disc:0", target=args):
                    with mock.patch.object(ripper, "resolve_makemkv_device", return_value="disc:1"), \
                         mock.patch.object(ripper, "resolve_nas_path", side_effect=lambda p, **_: p), \
                         mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "stage"), \
                         mock.patch.object(ripper.subprocess, "Popen", side_effect=fake_popen):
                        ok, payload = ripper.rip_disc(args, item, info, [0], target_dir, state, 1)

                    self.assertTrue(ok, payload)
                    self.assertEqual(observed["cmd"][observed["cmd"].index("mkv") + 1], "disc:1")
                    self.assertTrue(observed["disc0_locked_during_popen"])
                    self.assertTrue(observed["disc1_locked_during_popen"])
                    # After rip_disc returns, remapped disc-1.lock is released while outer disc-0.lock stays held.
                    self.assertFalse(self._external_slot_locked("disc:1", lock_dir))
                    self.assertTrue(self._external_slot_locked("disc:0", lock_dir))

            self.assertFalse(self._external_slot_locked("disc:0", lock_dir))

    def test_stable_mapping_disc0_reentrant_across_probe_and_rip(self):
        """Stable mapping (disc:0 -> disc:0) works cleanly with in-process re-entrancy
        for both probe_disc and rip_disc while holding the outer disc:0 lock."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "locks"
            state_path = root / "state.json"
            target_dir = root / "library" / "Sample Movie (2024)"
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                drive_device="/dev/disk6",
                state=str(state_path),
                overwrite=False,
                min_free_gb=0,
                no_extras=False,
                no_split_playall=False,
                verify=False,
                sync_move=True,
                allow_mkvmerge_fallback=False,
                non_interactive=True,
            )
            item = QueueItem(
                title="Sample Movie (2024)",
                type="movie",
                discs=1,
                target_root=str(target_dir),
                format="DVD",
            )
            state = {
                "queue": [],
                "current_index": 0,
                "disc_index_in_item": 0,
                "completed": [],
                "rip_durations_s": [],
                "disc_episode_counts": {},
            }
            seen: list[tuple[str, str, bool]] = []
            real_popen = subprocess.Popen

            def fake_popen(cmd, *a, **kwargs):
                if not cmd or cmd[0] != args.makemkvcon:
                    return real_popen(cmd, *a, **kwargs)
                locked_now = self._external_slot_locked("disc:0", lock_dir)
                if "info" in cmd:
                    seen.append(("probe", cmd[-1], locked_now))
                    return SimpleNamespace(
                        stdout=io.StringIO(
                            'CINFO:2,0,"SAMPLE_MOVIE"\n'
                            'CINFO:32,0,"SAMPLE_MOVIE"\n'
                            'TINFO:0,9,0,"1:45:00"\n'
                            'TINFO:0,11,0,"100000"\n'
                        ),
                        returncode=0,
                        wait=lambda timeout=None: 0,
                        terminate=lambda: None,
                        kill=lambda: None,
                    )
                seen.append(("rip", cmd[cmd.index("mkv") + 1], locked_now))
                staging_dir = Path(cmd[-1])
                (staging_dir / "title_t00.mkv").write_bytes(b"fake-mkv-payload")
                return SimpleNamespace(
                    stdout=io.StringIO('MSG:5036,0,1,"Copy complete. 1 titles saved."\n'),
                    returncode=0,
                    wait=lambda timeout=None: 0,
                    terminate=lambda: None,
                    kill=lambda: None,
                )

            with mock.patch.dict(
                os.environ,
                {"BURNDVD_LOCK_DIR": str(lock_dir), drive.DRIVE_DEVICE_ENV: "/dev/disk6"},
                clear=False,
            ):
                with drive.disc_slot_lock("disc:0", target=args):
                    with mock.patch.object(probe, "_optical_mounts", return_value=[]), \
                         mock.patch.object(probe, "_drive_idx_from_args", return_value=None), \
                         mock.patch.object(probe, "resolve_makemkv_device", return_value="disc:0"), \
                         mock.patch.object(probe.subprocess, "Popen", side_effect=fake_popen):
                        info = probe.probe_disc(args)
                    self.assertIsNotNone(info)

                    with mock.patch.object(ripper, "resolve_makemkv_device", return_value="disc:0"), \
                         mock.patch.object(ripper, "resolve_nas_path", side_effect=lambda p, **_: p), \
                         mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "stage"), \
                         mock.patch.object(ripper.subprocess, "Popen", side_effect=fake_popen):
                        ok, payload = ripper.rip_disc(args, item, info, [0], target_dir, state, 1)
                    self.assertTrue(ok, payload)
                    self.assertTrue(self._external_slot_locked("disc:0", lock_dir))

            self.assertEqual(seen, [("probe", "disc:0", True), ("rip", "disc:0", True)])
            self.assertFalse(self._external_slot_locked("disc:0", lock_dir))

    def test_ripqueue_main_blocks_midrun_remap_when_sibling_holds_disc1_same_process(self):
        """End-to-end ripqueue.main() regression matching the exact audit scenario:
        a sibling opener holds disc-1.lock (even in the same process), ripqueue starts
        on disc:0, scans disc:0, then enumeration remaps /dev/disk6 to disc:1 before rip
        -> ripqueue exits 4 and NEVER invokes Popen for mkv disc:1."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "locks"
            queue_csv = root / "queue.csv"
            state_json = root / "state.json"
            fake_mkv = root / "makemkvcon"
            fake_mkv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_mkv.chmod(0o755)
            queue_csv.write_text(
                "title,type,season,discs,episode_start,expected_episodes,target_root,format,notes\n"
                f"Sample Movie (2024),movie,0,1,1,0,{root / 'library'},DVD,auto-built from SAMPLE_MOVIE\n",
                encoding="utf-8",
            )
            argv = [
                "ripqueue.py",
                "--queue", str(queue_csv),
                "--state", str(state_json),
                "--device", "disc:0",
                "--makemkvcon", str(fake_mkv),
                "--non-interactive",
                "--min-free-gb", "0",
                "--no-sound",
            ]
            enum_calls = 0
            dispatched: list[tuple[str, str]] = []
            real_run = subprocess.run
            real_popen = subprocess.Popen

            def fake_run(cmd, *a, **kw):
                nonlocal enum_calls
                if cmd[0] != str(fake_mkv):
                    return real_run(cmd, *a, **kw)
                slot = 1 if enum_calls >= 2 else 0
                enum_calls += 1
                row = f'DRV:{slot},2,999,12,"FAKE","SAMPLE_MOVIE","/dev/rdisk6"\n'
                return SimpleNamespace(returncode=0, stdout=row, stderr="")

            def fake_popen(cmd, *a, **kw):
                if not cmd or cmd[0] != str(fake_mkv):
                    return real_popen(cmd, *a, **kw)
                if "info" in cmd:
                    dispatched.append(("scan", cmd[-1]))
                    data = (
                        'CINFO:2,0,"SAMPLE_MOVIE"\nCINFO:32,0,"SAMPLE_MOVIE"\n'
                        'TINFO:0,9,0,"1:45:00"\nTINFO:0,11,0,"100000"\n'
                    )
                    return SimpleNamespace(
                        stdout=io.StringIO(data),
                        returncode=0,
                        wait=lambda timeout=None: 0,
                        terminate=lambda: None,
                        kill=lambda: None,
                    )
                dispatched.append(("rip", cmd[cmd.index("mkv") + 1]))
                raise AssertionError(f"must never spawn makemkvcon mkv on locked remapped slot: {cmd}")

            with drive.disc_slot_lock("disc:1", lock_dir=lock_dir):
                with mock.patch.dict(
                    os.environ,
                    {"BURNDVD_LOCK_DIR": str(lock_dir), drive.DRIVE_DEVICE_ENV: "/dev/disk6"},
                    clear=False,
                ), \
                     mock.patch.object(sys, "argv", argv), \
                     mock.patch.object(drive.subprocess, "run", side_effect=fake_run), \
                     mock.patch.object(probe.subprocess, "Popen", side_effect=fake_popen), \
                     mock.patch.object(ripper.subprocess, "Popen", side_effect=fake_popen), \
                     mock.patch.object(ripqueue, "_drive_idx_from_args", return_value=None), \
                     mock.patch.object(ripqueue, "check_key_expiry"), \
                     mock.patch.object(probe, "_optical_mounts", return_value=[]), \
                     mock.patch.object(probe, "_drive_idx_from_args", return_value=None), \
                     mock.patch.object(
                         ripqueue,
                         "wait_for_disc",
                         side_effect=lambda a, s: ("ok", probe.probe_disc(a)),
                     ), \
                     mock.patch.object(ripqueue, "resolve_nas_path", side_effect=lambda p, **_: p), \
                     mock.patch.object(ripper, "resolve_nas_path", side_effect=lambda p, **_: p), \
                     mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "stage"):
                    with self.assertRaises(SystemExit) as ctx:
                        ripqueue.main()

            self.assertEqual(ctx.exception.code, 4)
            self.assertEqual(dispatched, [("scan", "disc:0")])
            log_text = state_json.with_suffix(".log").read_text(encoding="utf-8")
            self.assertIn("RIP_REFUSED", log_text)
            self.assertIn("DRIVE_SLOT_BUSY", log_text)


if __name__ == "__main__":
    unittest.main()
