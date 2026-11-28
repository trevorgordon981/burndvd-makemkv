"""Tests for locked-session MakeMKV drive resolution caching and strict invalidation.

Verifies Improvement #2:
- Eliminates repeated 15-second global `makemkvcon -r info list` calls within a
  positively verified, lock-held `slot_lock_scope()` session (across startup ->
  `probe_disc` -> per-title `rip()` loops).
- Strictly invalidates/bypasses the cache on lock/scope exit, `force_refresh=True`
  (unmount recovery, respin, retries), unverified/fallback resolutions, pin/device
  mismatches, foreign claim conflicts, and `DriveSlotConflictError`.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import burndvd.nas
from burndvd import drive, probe, ripper
from burndvd.cli import ripqueue
from burndvd.models import QueueItem


class _FakePopen:
    def __init__(self, lines: list[str], returncode: int = 0):
        self.stdout = io.StringIO("\n".join(lines) + ("\n" if lines else ""))
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class TestSessionDriveCache(unittest.TestCase):
    def setUp(self):
        self._clean_env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                drive.DRUTIL_DRIVE_ENV,
                drive.DRIVE_DEVICE_ENV,
                drive.DRIVE_SERIAL_ENV,
                drive.DRIVE_CLAIM_ENV,
                "BURNDVD_LOCK_DIR",
            )
        }
        self._env_patch = mock.patch.dict(os.environ, self._clean_env, clear=True)
        self._env_patch.start()
        ripper.clear_finalize_errors()

    def tearDown(self):
        self._env_patch.stop()

    def test_multititle_locked_session_invokes_makemkv_info_list_once(self):
        """Startup resolve -> disc_slot_lock -> probe_disc -> 4-title rip() loop
        invokes `makemkvcon -r info list` ONCE total (not N+2 = 6 times), while
        preserving reverse BSD-to-MakeMKV slot mapping."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "locks"
            target = root / "TV Shows" / "Sample Show" / "Season 01"
            target.mkdir(parents=True)
            item = QueueItem(
                title="Sample Show",
                type="tv-season",
                discs=1,
                target_root=str(root / "TV Shows" / "Sample Show"),
                format="DVD",
                season=1,
                episode_start=1,
            )
            args = argparse.Namespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:1",  # reverse-ordered initial selector; /dev/disk6 is actually disc:0
                drive_device="/dev/disk6",
                drive_serial="DRVSER00002",
                overwrite=False,
                rerip_review=False,
                review_root=None,
                min_free_gb=0,
                no_split_playall=True,
                no_extras=True,
                verify=False,
                sync_move=False,
                non_interactive=True,
                allow_mkvmerge_fallback=False,
                state=str(root / "state.json"),
            )
            reverse_drv = "\n".join(
                [
                    'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","SHOW_S1_D1","/dev/rdisk6"',
                    'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","OTHER_DISC","/dev/rdisk5"',
                ]
            )
            info_list_calls = 0

            def fake_run(cmd, *a, **kw):
                nonlocal info_list_calls
                if cmd == ["/fake/makemkvcon", "-r", "info", "list"]:
                    info_list_calls += 1
                    return SimpleNamespace(returncode=0, stdout=reverse_drv, stderr="")
                raise AssertionError(f"unexpected subprocess.run call: {cmd}")

            probed_devices: list[str] = []
            ripped_calls: list[tuple[str, str]] = []

            def fake_popen(cmd, **kw):
                if "info" in cmd:
                    probed_devices.append(cmd[-1])
                    return _FakePopen(
                        [
                            'CINFO:2,0,"SHOW_S1_D1"',
                            'CINFO:32,0,"SHOW_S1_D1"',
                            'TINFO:0,9,0,"0:22:00"',
                            'TINFO:0,11,0,"1000000"',
                            'TINFO:1,9,0,"0:22:00"',
                            'TINFO:1,11,0,"1000000"',
                            'TINFO:2,9,0,"0:22:00"',
                            'TINFO:2,11,0,"1000000"',
                            'TINFO:3,9,0,"0:22:00"',
                            'TINFO:3,11,0,"1000000"',
                        ]
                    )
                dev = cmd[cmd.index("mkv") + 1]
                sel = cmd[cmd.index("mkv") + 2]
                out_dir = Path(cmd[cmd.index("mkv") + 3])
                ripped_calls.append((dev, sel))
                (out_dir / f"title_t{int(sel):02d}.mkv").write_bytes(b"fake-mkv")
                return _FakePopen(['MSG:5036,0,1,"1 titles saved"'])

            with mock.patch.dict(
                os.environ,
                {"HOME": str(root), "BURNDVD_LOCK_DIR": str(lock_dir)},
                clear=False,
            ), mock.patch.object(drive.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(probe, "_optical_mounts", return_value=[]), \
                 mock.patch.object(probe, "_drive_idx_from_args", return_value=None), \
                 mock.patch.object(subprocess, "Popen", side_effect=fake_popen), \
                 mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "staging"), \
                 mock.patch.object(burndvd.nas, "LOCAL_LOCK_BASE", root / "nas-locks"), \
                 mock.patch.object(ripper, "_free_gb", return_value=500.0), \
                 mock.patch.object(ripper, "_alt_playlist_duplicate", return_value=False), \
                 contextlib.redirect_stdout(io.StringIO()):
                with drive.slot_lock_scope():
                    resolved = drive.resolve_makemkv_device(args)
                    self.assertEqual(resolved, "disc:0")
                    self.assertTrue(resolved.verified)
                    args.device = resolved
                    with drive.disc_slot_lock(args.device, target=args):
                        info = probe.probe_disc(args)
                        self.assertIsNotNone(info)
                        disc_args = argparse.Namespace(**vars(args))
                        ok, result = ripper.rip(
                            disc_args, item, info, [0, 1, 2, 3], target, {}, 1
                        )

            self.assertTrue(ok, result)
            self.assertEqual(
                info_list_calls,
                1,
                f"expected exactly 1 makemkvcon info list call across startup+probe+4 titles, got {info_list_calls}",
            )
            self.assertEqual(probed_devices, ["disc:0"])
            self.assertEqual(
                ripped_calls,
                [("disc:0", "0"), ("disc:0", "1"), ("disc:0", "2"), ("disc:0", "3")],
            )

    def test_direct_locked_session_without_pre_resolve_invokes_info_list_once(self):
        """Even when entering `disc_slot_lock('disc:0', target=args)` directly with
        a plain string and node-only pin, the first verified lookup inside `probe_disc`
        is reused across all N titles in `rip()`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "locks"
            target = root / "TV Shows" / "Direct Show" / "Season 01"
            target.mkdir(parents=True)
            item = QueueItem(
                title="Direct Show",
                type="tv-season",
                discs=1,
                target_root=str(root / "TV Shows" / "Direct Show"),
                format="DVD",
                season=1,
                episode_start=1,
            )
            args = argparse.Namespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                drive_device="/dev/disk6",
                drive_serial=None,
                overwrite=False,
                rerip_review=False,
                review_root=None,
                min_free_gb=0,
                no_split_playall=True,
                no_extras=True,
                verify=False,
                sync_move=False,
                non_interactive=True,
                allow_mkvmerge_fallback=False,
                state=str(root / "state.json"),
            )
            drv_out = 'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","DIRECT_S1_D1","/dev/rdisk6"\n'
            info_list_calls = 0

            def fake_run(cmd, *a, **kw):
                nonlocal info_list_calls
                if cmd == ["/fake/makemkvcon", "-r", "info", "list"]:
                    info_list_calls += 1
                    return SimpleNamespace(returncode=0, stdout=drv_out, stderr="")
                raise AssertionError(f"unexpected subprocess.run call: {cmd}")

            def fake_popen(cmd, **kw):
                if "info" in cmd:
                    return _FakePopen(
                        [
                            'CINFO:2,0,"DIRECT_S1_D1"',
                            'TINFO:0,9,0,"0:22:00"',
                            'TINFO:0,11,0,"1000000"',
                            'TINFO:1,9,0,"0:22:00"',
                            'TINFO:1,11,0,"1000000"',
                            'TINFO:2,9,0,"0:22:00"',
                            'TINFO:2,11,0,"1000000"',
                        ]
                    )
                sel = cmd[cmd.index("mkv") + 2]
                out_dir = Path(cmd[cmd.index("mkv") + 3])
                (out_dir / f"title_t{int(sel):02d}.mkv").write_bytes(b"fake-mkv")
                return _FakePopen(['MSG:5036,0,1,"1 titles saved"'])

            with mock.patch.dict(
                os.environ,
                {"HOME": str(root), "BURNDVD_LOCK_DIR": str(lock_dir)},
                clear=False,
            ), mock.patch.object(drive.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(probe, "_optical_mounts", return_value=[]), \
                 mock.patch.object(probe, "_drive_idx_from_args", return_value=None), \
                 mock.patch.object(subprocess, "Popen", side_effect=fake_popen), \
                 mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "staging"), \
                 mock.patch.object(burndvd.nas, "LOCAL_LOCK_BASE", root / "nas-locks"), \
                 mock.patch.object(ripper, "_free_gb", return_value=500.0), \
                 mock.patch.object(ripper, "_alt_playlist_duplicate", return_value=False), \
                 contextlib.redirect_stdout(io.StringIO()):
                with drive.slot_lock_scope():
                    with drive.disc_slot_lock("disc:0", target=args):
                        info = probe.probe_disc(args)
                        self.assertIsNotNone(info)
                        ok, result = ripper.rip(
                            args, item, info, [0, 1, 2], target, {}, 1
                        )

            self.assertTrue(ok, result)
            self.assertEqual(info_list_calls, 1)

    def test_exiting_lock_or_scope_invalidates_cache_and_re_verifies(self):
        """Cache is ONLY active while `disc_slot_lock` for the resolved slot is held
        in the current `slot_lock_scope()`. Exiting lock, exiting scope, entering a
        nested scope, or locking the wrong slot must all re-query MakeMKV."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "locks"
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                drive_device="/dev/disk6",
                drive_serial="DRVSER00002",
            )
            current_slot = {"idx": 0}
            info_list_calls = 0

            def fake_run(cmd, *a, **kw):
                nonlocal info_list_calls
                info_list_calls += 1
                idx = current_slot["idx"]
                row = f'DRV:{idx},2,999,12,"BD-RE BU40N 1.03 DRVSER00002","VOL","/dev/rdisk6"\n'
                return SimpleNamespace(returncode=0, stdout=row, stderr="")

            with mock.patch.dict(
                os.environ,
                {"HOME": str(root), "BURNDVD_LOCK_DIR": str(lock_dir)},
                clear=False,
            ), mock.patch.object(drive.subprocess, "run", side_effect=fake_run):
                # 1. No disc_slot_lock held -> NEVER caches, even inside slot_lock_scope().
                with drive.slot_lock_scope():
                    self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                    self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                    self.assertEqual(info_list_calls, 2)

                    # 2. Wrong slot locked (disc:1 locked while drive resolves to disc:0) -> NOT cached.
                    with drive.disc_slot_lock("disc:1", target=args):
                        self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                        self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                        self.assertEqual(info_list_calls, 4)

                    # 3. Matching slot locked -> caches after first verified lookup.
                    with drive.disc_slot_lock("disc:0", target=args):
                        self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                        self.assertEqual(info_list_calls, 5)
                        self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                        self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                        self.assertEqual(info_list_calls, 5)

                        # 4. Nested slot_lock_scope() does NOT inherit parent's cache or locks.
                        with drive.slot_lock_scope():
                            self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                            self.assertEqual(info_list_calls, 6)

                        # Outer scope cache still intact while outer lock remains held.
                        self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                        self.assertEqual(info_list_calls, 6)

                    # 5. Exiting disc_slot_lock invalidated cache -> must re-verify!
                    current_slot["idx"] = 1
                    self.assertEqual(drive.resolve_makemkv_device(args), "disc:1")
                    self.assertEqual(info_list_calls, 7)

                # 6. Fresh slot_lock_scope() starts with empty cache -> re-verifies!
                with drive.slot_lock_scope():
                    resolved = drive.resolve_makemkv_device(args)
                    self.assertEqual(resolved, "disc:1")
                    self.assertEqual(info_list_calls, 8)
                    args.device = resolved
                    with drive.disc_slot_lock(args.device, target=args):
                        self.assertEqual(drive.resolve_makemkv_device(args), "disc:1")
                        self.assertEqual(info_list_calls, 8)

    def test_force_refresh_and_unmount_recovery_and_lifecycle_bypass_cache(self):
        """`force_refresh=True`, `probe_disc` unmount recovery retry, `respin_drive`,
        `eject_disc`, `mark_drive_released`, and rip selector errors all bypass or
        invalidate the session cache."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "locks"
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                drive_device="/dev/disk6",
                drive_serial="DRVSER00002",
                state=str(root / "state.json"),
                no_eject=False,
            )
            current_slot = {"idx": 0}
            info_list_calls = 0

            def fake_run(cmd, *a, **kw):
                nonlocal info_list_calls
                if cmd == ["/fake/makemkvcon", "-r", "info", "list"]:
                    info_list_calls += 1
                    idx = current_slot["idx"]
                    row = f'DRV:{idx},2,999,12,"BD-RE BU40N 1.03 DRVSER00002","VOL","/dev/rdisk6"\n'
                    return SimpleNamespace(returncode=0, stdout=row, stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.dict(
                os.environ,
                {"HOME": str(root), "BURNDVD_LOCK_DIR": str(lock_dir)},
                clear=False,
            ), mock.patch.object(drive.subprocess, "run", side_effect=fake_run):
                with drive.slot_lock_scope():
                    resolved = drive.resolve_makemkv_device(args)
                    self.assertEqual(info_list_calls, 1)
                    args.device = resolved
                    with drive.disc_slot_lock(args.device, target=args):
                        # Cached hit
                        self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                        self.assertEqual(info_list_calls, 1)

                        # Direct force_refresh=True bypasses cache and re-queries!
                        self.assertEqual(
                            drive.resolve_makemkv_device(args, force_refresh=True),
                            "disc:0",
                        )
                        self.assertEqual(info_list_calls, 2)

                        # Cached again after positive force_refresh while still locked
                        self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                        self.assertEqual(info_list_calls, 2)

                        # probe_disc unmount recovery retry calls resolve_makemkv_device(force_refresh=True)
                        popen_attempts = 0
                        seen_probe_devs: list[str] = []

                        def fake_recovery_popen(cmd, **kw):
                            nonlocal popen_attempts
                            popen_attempts += 1
                            seen_probe_devs.append(cmd[-1])
                            if popen_attempts == 1:
                                # First attempt fails due to mounted volume; shift slot before retry.
                                current_slot["idx"] = 1
                                return _FakePopen(
                                    ['DRV:0,2,999,12,"BD-RE BU40N 1.03 DRVSER00002","VOL","/dev/rdisk6"']
                                )
                            return _FakePopen(
                                [
                                    'CINFO:2,0,"VOL"',
                                    'TINFO:0,9,0,"1:30:00"',
                                    'TINFO:0,11,0,"100000"',
                                ]
                            )

                        with mock.patch.object(probe, "_optical_mounts", return_value=["/dev/disk6"]), \
                             mock.patch.object(probe, "_drive_idx_from_args", return_value=0), \
                             mock.patch.object(probe, "unmount_blocking_volume", return_value=True), \
                             mock.patch.object(probe.subprocess, "Popen", side_effect=fake_recovery_popen), \
                             contextlib.redirect_stdout(io.StringIO()):
                            info = probe.probe_disc(args)

                        self.assertIsNotNone(info)
                        self.assertEqual(seen_probe_devs, ["disc:0", "disc:1"])
                        self.assertEqual(info_list_calls, 3)

                        # Re-establish cache on disc:0, then verify respin_drive invalidates it.
                        current_slot["idx"] = 0
                        self.assertEqual(
                            drive.resolve_makemkv_device(args, force_refresh=True),
                            "disc:0",
                        )
                        self.assertEqual(info_list_calls, 4)
                        self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                        self.assertEqual(info_list_calls, 4)

                        with mock.patch.object(drive, "unmount_blocking_volume", return_value=False), \
                             mock.patch.object(drive, "_drive_identity", return_value="HL-DT-ST BD-RE BU40N 1.03"), \
                             mock.patch.object(drive.time, "sleep"), \
                             contextlib.redirect_stdout(io.StringIO()):
                            drive.respin_drive(args)

                        self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                        self.assertEqual(info_list_calls, 5)

                        # Verify eject_disc invalidates cache.
                        with mock.patch.object(drive, "_resolve_drive_target", return_value=(1, "/dev/disk6", "")), \
                             contextlib.redirect_stdout(io.StringIO()):
                            drive.eject_disc(args)
                        self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                        self.assertEqual(info_list_calls, 6)

                        # Verify mark_drive_released invalidates cache.
                        drive.mark_drive_released(args.state, device="disc:0", ejected=True)
                        self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                        self.assertEqual(info_list_calls, 7)

    def test_unverified_fallback_resolution_is_never_cached(self):
        """Timeouts/fallback slots, unloaded (state!=2) rows, and manually constructed
        unverified ResolvedMakemkvDevice objects MUST NEVER be cached."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "locks"
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                drive_device="/dev/disk6",
                drive_serial="DRVSER00002",
                state=str(root / "state.json"),
            )
            mode = {"value": "timeout"}
            info_list_calls = 0

            def fake_run(cmd, *a, **kw):
                nonlocal info_list_calls
                if cmd == ["/fake/makemkvcon", "-r", "info", "list"]:
                    info_list_calls += 1
                    if mode["value"] == "timeout":
                        raise subprocess.TimeoutExpired(cmd, 15)
                    if mode["value"] == "empty":
                        return SimpleNamespace(
                            returncode=0,
                            stdout='DRV:0,0,999,0,"BD-RE BU40N 1.03 DRVSER00002","","/dev/rdisk6"\n',
                            stderr="",
                        )
                    return SimpleNamespace(
                        returncode=0,
                        stdout='DRV:0,2,999,12,"BD-RE BU40N 1.03 DRVSER00002","LOADED_VOL","/dev/rdisk6"\n',
                        stderr="",
                    )
                if cmd[:2] == ["ps", "-Ao"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if cmd[:2] == [drive.DRUTIL_BIN, "list"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.dict(
                os.environ,
                {"HOME": str(root), "BURNDVD_LOCK_DIR": str(lock_dir)},
                clear=False,
            ), mock.patch.object(drive.subprocess, "run", side_effect=fake_run):
                # 1. Manually constructed unverified ResolvedMakemkvDevice does NOT seed cache.
                unverified = drive.ResolvedMakemkvDevice(
                    "disc:0", device_node="/dev/disk6", serial="DRVSER00002"
                )
                self.assertFalse(unverified.verified)
                with drive.slot_lock_scope():
                    with drive.disc_slot_lock(unverified, target=args):
                        # Timeout -> unverified fallback ("disc:0"); MUST NOT cache.
                        r1 = drive.resolve_makemkv_device(args)
                        self.assertEqual(r1, "disc:0")
                        self.assertFalse(getattr(r1, "verified", False))
                        r2 = drive.resolve_makemkv_device(args)
                        self.assertEqual(r2, "disc:0")
                        self.assertEqual(info_list_calls, 2)

                        # Empty/unloaded row (has_media=False) -> unverified; MUST NOT cache.
                        mode["value"] = "empty"
                        r3 = drive.resolve_makemkv_device(args)
                        self.assertEqual(r3, "disc:0")
                        self.assertFalse(r3.verified)
                        r4 = drive.resolve_makemkv_device(args)
                        self.assertEqual(r4, "disc:0")
                        self.assertEqual(info_list_calls, 4)

                        # Positive loaded match -> verified=True -> NOW cached!
                        mode["value"] = "verified"
                        r5 = drive.resolve_makemkv_device(args)
                        self.assertEqual(r5, "disc:0")
                        self.assertTrue(r5.verified)
                        self.assertEqual(info_list_calls, 5)
                        r6 = drive.resolve_makemkv_device(args)
                        self.assertEqual(r6, "disc:0")
                        self.assertEqual(info_list_calls, 5)

    def test_different_device_node_or_serial_pin_misses_cache_cleanly(self):
        """Changing requested `args.device`, pinned `drive_device`, or pinned
        `drive_serial` must miss the cache and re-query MakeMKV, while normalized
        aliases (`/dev/rdisk6`, lowercase serial) hit cleanly."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "locks"
            drv_out = "\n".join(
                [
                    'DRV:0,2,999,12,"BD-RE BU40N 1.03 DRVSER00002","VOL_A","/dev/rdisk6"',
                    'DRV:1,2,999,12,"BD-RE BU40N 1.03 DRVSER00001","VOL_B","/dev/rdisk5"',
                ]
            )
            info_list_calls = 0

            def fake_run(cmd, *a, **kw):
                nonlocal info_list_calls
                if cmd == ["/fake/makemkvcon", "-r", "info", "list"]:
                    info_list_calls += 1
                    return SimpleNamespace(returncode=0, stdout=drv_out, stderr="")
                raise AssertionError(f"unexpected subprocess.run: {cmd}")

            args_a = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                drive_device="/dev/disk6",
                drive_serial="DRVSER00002",
            )
            args_a_alias = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                drive_device="/dev/rdisk6",
                drive_serial="drvser00002",
            )
            args_diff_device = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:1",
                drive_device="/dev/disk6",
                drive_serial="DRVSER00002",
            )
            args_diff_node = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                drive_device="/dev/disk5",
                drive_serial="DRVSER00002",
            )
            args_diff_serial = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                drive_device="/dev/disk6",
                drive_serial="DRVSER00001",
            )

            with mock.patch.dict(
                os.environ,
                {"HOME": str(root), "BURNDVD_LOCK_DIR": str(lock_dir)},
                clear=False,
            ), mock.patch.object(drive.subprocess, "run", side_effect=fake_run):
                with drive.slot_lock_scope():
                    resolved = drive.resolve_makemkv_device(args_a)
                    self.assertEqual(info_list_calls, 1)
                    with drive.disc_slot_lock(resolved, target=args_a):
                        # Exact and normalized equivalent hit cache.
                        self.assertEqual(drive.resolve_makemkv_device(args_a), "disc:0")
                        self.assertEqual(drive.resolve_makemkv_device(args_a_alias), "disc:0")
                        self.assertEqual(info_list_calls, 1)

                        # Different requested args.device -> cache miss!
                        self.assertEqual(drive.resolve_makemkv_device(args_diff_device), "disc:0")
                        self.assertEqual(info_list_calls, 2)

                        # Different drive_device -> cache miss!
                        self.assertEqual(drive.resolve_makemkv_device(args_diff_node), "disc:1")
                        self.assertEqual(info_list_calls, 3)

                        # Different drive_serial -> cache miss!
                        self.assertEqual(drive.resolve_makemkv_device(args_diff_serial), "disc:0")
                        self.assertEqual(info_list_calls, 4)

    def test_foreign_claim_conflict_and_slot_conflict_error_bypass_and_invalidate_cache(self):
        """A live foreign drive-claim conflicting with the cached slot must bypass
        and invalidate the cache, and a DriveSlotConflictError must invalidate."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "locks"
            claim_dir = root / ".cache" / "burndvd" / "drive-claims"
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                drive_device="/dev/disk6",
                drive_serial="DRVSER00002",
                state=str(root / "mine.state.json"),
            )
            mode = {"value": "verified"}
            info_list_calls = 0

            sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
            try:
                def fake_run(cmd, *a, **kw):
                    nonlocal info_list_calls
                    if cmd == ["/fake/makemkvcon", "-r", "info", "list"]:
                        info_list_calls += 1
                        if mode["value"] == "verified":
                            return SimpleNamespace(
                                returncode=0,
                                stdout='DRV:0,2,999,12,"BD-RE BU40N 1.03 DRVSER00002","VOL","/dev/rdisk6"\n',
                                stderr="",
                            )
                        return SimpleNamespace(returncode=1, stdout="", stderr="busy")
                    if cmd[:2] == ["ps", "-Ao"]:
                        return SimpleNamespace(
                            returncode=0,
                            stdout=f"  {sleeper.pid} python3 /u/ripqueue.py --state {root / 'other.state.json'} --device disc:0\n",
                            stderr="",
                        )
                    if cmd[:2] == [drive.DRUTIL_BIN, "list"]:
                        return SimpleNamespace(
                            returncode=0,
                            stdout="  1  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported\n",
                            stderr="",
                        )
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                with mock.patch.dict(
                    os.environ,
                    {"HOME": str(root), "BURNDVD_LOCK_DIR": str(lock_dir)},
                    clear=False,
                ), mock.patch.object(drive.subprocess, "run", side_effect=fake_run):
                    with drive.slot_lock_scope():
                        resolved = drive.resolve_makemkv_device(args)
                        self.assertEqual(info_list_calls, 1)
                        args.device = resolved
                        with drive.disc_slot_lock(args.device, target=args):
                            # Cached hit while no foreign claim exists.
                            self.assertEqual(drive.resolve_makemkv_device(args), "disc:0")
                            self.assertEqual(info_list_calls, 1)

                            # Introduce a live foreign claim for disc:0 from another drive/state.
                            foreign_claim = claim_dir / "serial-OTHERDRV999.claim"
                            foreign_claim.mkdir(parents=True)
                            (foreign_claim / "pid").write_text(str(sleeper.pid), encoding="utf-8")
                            (foreign_claim / "dev").write_text("/dev/disk5", encoding="utf-8")
                            (foreign_claim / "serial").write_text("OTHERDRV999", encoding="utf-8")
                            (foreign_claim / "disc").write_text("0", encoding="utf-8")
                            (foreign_claim / "state").write_text(
                                str(root / "other.state.json"), encoding="utf-8"
                            )

                            # Cache MUST be bypassed and invalidated -> re-queries MakeMKV, hits busy conflict!
                            mode["value"] = "fail"
                            with self.assertRaises(drive.DriveSlotConflictError):
                                drive.resolve_makemkv_device(args)
                            self.assertEqual(info_list_calls, 2)
            finally:
                sleeper.terminate()
                sleeper.wait(timeout=5)

    def test_expected_volume_safety_check_remains_intact_with_session_cache(self):
        """End-to-end ripqueue.main() uses the cached verified binding (1 info list call)
        and still rejects an `--expected-volume` mismatch with exit 4 before staging."""
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
                f"Naruto Shippuden,tv-season,1,1,1,0,{root / 'nas'},DVD,auto-built from NarutoShippuden_Vol4\n",
                encoding="utf-8",
            )
            info_list_calls = 0

            def fake_run(cmd, *a, **kw):
                nonlocal info_list_calls
                if cmd == [str(fake_mkv), "-r", "info", "list"]:
                    info_list_calls += 1
                    return SimpleNamespace(
                        returncode=0,
                        stdout='DRV:0,2,999,12,"BD-RE BU40N 1.03 DRVSER00002","THOR","/dev/rdisk6"\n',
                        stderr="",
                    )
                raise AssertionError(f"unexpected subprocess.run: {cmd}")

            def fake_probe_popen(cmd, **kw):
                return _FakePopen(
                    [
                        'CINFO:2,0,"THOR"',
                        'CINFO:32,0,"THOR"',
                        'TINFO:0,9,0,"1:55:00"',
                        'TINFO:0,11,0,"4000000000"',
                    ]
                )

            argv = [
                "ripqueue.py",
                "--queue", str(queue_csv),
                "--state", str(state_json),
                "--device", "disc:1",
                "--makemkvcon", str(fake_mkv),
                "--expected-volume", "NarutoShippuden_Vol4",
                "--non-interactive",
                "--min-free-gb", "0",
                "--no-sound",
            ]
            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(root),
                    "BURNDVD_LOCK_DIR": str(lock_dir),
                    drive.DRIVE_DEVICE_ENV: "/dev/disk6",
                    drive.DRIVE_SERIAL_ENV: "DRVSER00002",
                },
                clear=False,
            ), mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(drive.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(ripqueue, "_drive_idx_from_args", return_value=None), \
                 mock.patch.object(ripqueue, "check_key_expiry"), \
                 mock.patch.object(probe, "_optical_mounts", return_value=[]), \
                 mock.patch.object(probe, "_drive_idx_from_args", return_value=None), \
                 mock.patch.object(probe.subprocess, "Popen", side_effect=fake_probe_popen), \
                 mock.patch.object(ripqueue, "wait_for_disc", side_effect=lambda a, s: ("disc", probe.probe_disc(a))), \
                 mock.patch.object(ripqueue, "rip") as rip_mock, \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    ripqueue.main()

            self.assertEqual(ctx.exception.code, 4)
            self.assertEqual(info_list_calls, 1)
            rip_mock.assert_not_called()

    def test_rip_selector_error_invalidates_cache_and_refreshes_next_selector(self):
        """When a title fails in `rip()` (and is skipped under bad-title skip), the
        next selector MUST bypass the cache and re-query `makemkvcon -r info list`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_dir = root / "locks"
            target = root / "TV Shows" / "Retry Show" / "Season 01"
            target.mkdir(parents=True)
            item = QueueItem(
                title="Retry Show",
                type="tv-season",
                discs=1,
                target_root=str(root / "TV Shows" / "Retry Show"),
                format="DVD",
                season=1,
                episode_start=1,
            )
            info = {
                "cinfo": {2: "RETRY_SHOW_S1_D1"},
                "titles": {
                    0: {9: "0:22:00", 11: "1000000"},
                    1: {9: "0:22:00", 11: "1000000"},
                    2: {9: "0:22:00", 11: "1000000"},
                },
            }
            args = argparse.Namespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                drive_device="/dev/disk6",
                drive_serial="DRVSER00002",
                overwrite=False,
                rerip_review=False,
                review_root=None,
                min_free_gb=0,
                no_split_playall=True,
                no_extras=True,
                verify=False,
                sync_move=False,
                non_interactive=True,
                allow_mkvmerge_fallback=False,
                state=str(root / "state.json"),
            )
            drv_out = 'DRV:0,2,999,12,"BD-RE BU40N 1.03 DRVSER00002","RETRY_SHOW_S1_D1","/dev/rdisk6"\n'
            info_list_calls = 0

            def fake_run(cmd, *a, **kw):
                nonlocal info_list_calls
                if cmd == ["/fake/makemkvcon", "-r", "info", "list"]:
                    info_list_calls += 1
                    return SimpleNamespace(returncode=0, stdout=drv_out, stderr="")
                raise AssertionError(f"unexpected subprocess.run: {cmd}")

            def fake_popen(cmd, **kw):
                sel = cmd[cmd.index("mkv") + 2]
                out_dir = Path(cmd[cmd.index("mkv") + 3])
                if sel == "0":
                    return _FakePopen(['MSG:5003,0,0,"Failed to save title 0"'], returncode=1)
                (out_dir / f"title_t{int(sel):02d}.mkv").write_bytes(b"fake-mkv")
                return _FakePopen(['MSG:5036,0,1,"1 titles saved"'])

            with mock.patch.dict(
                os.environ,
                {"HOME": str(root), "BURNDVD_LOCK_DIR": str(lock_dir)},
                clear=False,
            ), mock.patch.object(drive.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "staging"), \
                 mock.patch.object(burndvd.nas, "LOCAL_LOCK_BASE", root / "nas-locks"), \
                 mock.patch.object(ripper, "_free_gb", return_value=500.0), \
                 mock.patch.object(ripper, "_alt_playlist_duplicate", return_value=False), \
                 mock.patch.object(subprocess, "Popen", side_effect=fake_popen), \
                 contextlib.redirect_stdout(io.StringIO()):
                with drive.slot_lock_scope():
                    resolved = drive.resolve_makemkv_device(args)
                    args.device = resolved
                    with drive.disc_slot_lock(args.device, target=args):
                        ok, result = ripper.rip(
                            args, item, info, [0, 1, 2], target, {}, 1
                        )

            self.assertTrue(ok, result)
            # Call 1 at startup; sel 0 hit cache & failed -> invalidated; sel 1 forced refresh (call 2); sel 2 hit cache.
            self.assertEqual(info_list_calls, 2)


if __name__ == "__main__":
    unittest.main()
