"""Unit tests for zero-babysit multi-drive box-set watch mode (`burndvd --watch`).

100% mocked — never touches any real optical drive, MakeMKV binary, or live process.
"""

from __future__ import annotations

import csv
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from burndvd import metadata
from burndvd.cli import watch as watch_cli
from burndvd.drive import (
    DRIVE_DEVICE_ENV,
    DRIVE_SERIAL_ENV,
    DRUTIL_DRIVE_ENV,
    parse_makemkv_drives,
)
from burndvd.watch import (
    BoxSetWatcher,
    WatchConfig,
    drive_identity_key,
    notify_slack,
    resolve_default_target_root,
    resolve_no_extras,
)


def _drv_output(*rows: str) -> list[dict]:
    return parse_makemkv_drives("\n".join(rows))


class _FakeChildProc:
    """Minimal Popen-compatible mock with controllable exit status."""

    def __init__(self, pid: int, cmd: list[str], env: dict[str, str]):
        self.pid = pid
        self.cmd = list(cmd)
        self.env = dict(env)
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def finish(self, code: int = 0) -> None:
        self.returncode = code


class TestWatchModeSimultaneousTwoDrives(unittest.TestCase):
    """Two drives loaded simultaneously must serialize launch & avoid episode overlap."""

    def test_two_drives_serialize_launch_and_pin_distinct_serials_and_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            season_dir = target_root / "Season 01"
            season_dir.mkdir(parents=True)
            # Pre-existing episodes E01..E117 on NAS
            (season_dir / "Starlight Brigade - S01E117.mkv").write_bytes(b"x" * 1024)

            slots_dir = root / "slots"
            log_dir = root / "logs"
            work_dir = root / "work"

            two_drives_loaded = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_P10_D1","/dev/rdisk5"',
                'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","STARLIGHT_BRIGADE_P10_D2","/dev/rdisk6"',
            )
            two_drives_empty = _drv_output(
                'DRV:0,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","",""',
                'DRV:1,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","",""',
            )

            spawned: list[_FakeChildProc] = []
            next_pid = [50001]
            slot_written_event = threading.Event()

            def fake_spawn(cmd, *, stdin=None, stdout=None, stderr=None, env=None):
                pid = next_pid[0]
                next_pid[0] += 1
                proc = _FakeChildProc(pid=pid, cmd=cmd, env=env or {})
                spawned.append(proc)

                # Simulate Drive 1 taking ~120ms to probe its disc before writing its slot reservation.
                if len(spawned) == 1:
                    def _publish_drive1_slot():
                        time.sleep(0.12)
                        metadata.write_tv_slot_reservation(
                            pid=pid,
                            title="Starlight Brigade",
                            season=1,
                            target_root=str(target_root),
                            episode_start=118,
                            episode_count=7,  # Reserves E118..E124 -> Drive 2 MUST get E125!
                            slots_dir=slots_dir,
                        )
                        slot_written_event.set()

                    threading.Thread(target=_publish_drive1_slot, daemon=True).start()
                return proc

            tick = [0]

            def list_drives():
                tick[0] += 1
                if tick[0] == 1:
                    return two_drives_loaded
                # Finish both children on tick 2 and empty trays
                for p in spawned:
                    p.finish(0)
                return two_drives_empty

            out_buf = io.StringIO()
            cfg = WatchConfig(
                show="Starlight Brigade",
                season=1,
                media_format="BD",
                target_root=str(target_root),
                no_extras=True,
                poll_interval=0.05,
                scan_wait=5.0,
                max_discs=2,
                makemkvcon="/fake/makemkvcon",
                ripqueue_py="/fake/ripqueue.py",
                log_dir=log_dir,
                work_dir=work_dir,
                slots_dir=slots_dir,
            )

            with mock.patch.object(
                metadata, "_pid_alive", side_effect=lambda p: p in {50001, 50002}
            ):
                watcher = BoxSetWatcher(
                    cfg,
                    list_drives_fn=list_drives,
                    spawn_fn=fake_spawn,
                    discover_procs_fn=lambda: [],
                    sleep_fn=lambda _s: None,
                    notify_fn=lambda _msg: False,
                    out=out_buf,
                )
                summary = watcher.run()

            self.assertTrue(slot_written_event.is_set(), "Drive 2 did not wait for Drive 1 slot!")
            self.assertEqual(summary.launched, 2)
            self.assertEqual(summary.completed, 2)
            self.assertEqual(summary.succeeded, 2)
            self.assertEqual(summary.failed, 0)
            self.assertEqual(len(spawned), 2)

            p1, p2 = spawned
            # Distinct hardware serials, device nodes, drutil indices, and MakeMKV disc selectors
            self.assertEqual(p1.env[DRIVE_SERIAL_ENV], "DRVSER00001")
            self.assertEqual(p1.env[DRIVE_DEVICE_ENV], "/dev/disk5")
            self.assertEqual(p1.env[DRUTIL_DRIVE_ENV], "1")
            self.assertIn("disc:0", p1.cmd)

            self.assertEqual(p2.env[DRIVE_SERIAL_ENV], "DRVSER00002")
            self.assertEqual(p2.env[DRIVE_DEVICE_ENV], "/dev/disk6")
            self.assertEqual(p2.env[DRUTIL_DRIVE_ENV], "2")
            self.assertIn("disc:1", p2.cmd)

            # Verify non-overlapping episode_start written to each queue CSV
            q1 = Path(p1.cmd[p1.cmd.index("--queue") + 1])
            q2 = Path(p2.cmd[p2.cmd.index("--queue") + 1])
            with q1.open(encoding="utf-8", newline="") as fh:
                row1 = next(csv.DictReader(fh))
            with q2.open(encoding="utf-8", newline="") as fh:
                row2 = next(csv.DictReader(fh))
            self.assertEqual(row1["episode_start"], "118")
            self.assertEqual(row2["episode_start"], "125")

            # Verify status output lines
            text = out_buf.getvalue()
            self.assertIn("Drive DRVSER00001 (/dev/disk5) STARTED STARLIGHT_BRIGADE_P10_D1 -> E118+", text)
            self.assertIn("Drive DRVSER00002 (/dev/disk6) STARTED STARLIGHT_BRIGADE_P10_D2 -> E125+", text)
            self.assertIn("Drive DRVSER00001 FINISHED STARLIGHT_BRIGADE_P10_D1 (exit 0", text)
            self.assertIn("Drive DRVSER00002 FINISHED STARLIGHT_BRIGADE_P10_D2 (exit 0", text)

    def test_episode_start_override_applies_only_to_first_disc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            slots_dir = root / "slots"

            two_drives = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D1","/dev/rdisk5"',
                'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","STARLIGHT_BRIGADE_D2","/dev/rdisk6"',
            )
            spawned: list[_FakeChildProc] = []

            def fake_spawn(cmd, *, stdin=None, stdout=None, stderr=None, env=None):
                pid = 60000 + len(spawned) + 1
                proc = _FakeChildProc(pid=pid, cmd=cmd, env=env or {})
                spawned.append(proc)
                if len(spawned) == 1:
                    metadata.write_tv_slot_reservation(
                        pid=pid,
                        title="Starlight Brigade",
                        season=1,
                        target_root=str(target_root),
                        episode_start=50,
                        episode_count=6,
                        slots_dir=slots_dir,
                    )
                return proc

            cfg = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                episode_start=50,
                scan_wait=2.0,
                poll_interval=0.05,
                log_dir=root / "logs",
                work_dir=root / "work",
                slots_dir=slots_dir,
            )

            with mock.patch.object(
                metadata, "_pid_alive", side_effect=lambda p: p in {60001, 60002}
            ):
                watcher = BoxSetWatcher(
                    cfg,
                    list_drives_fn=lambda: two_drives,
                    spawn_fn=fake_spawn,
                    discover_procs_fn=lambda: [],
                    notify_fn=lambda _m: False,
                    out=io.StringIO(),
                )
                launched = watcher.poll_once()

            self.assertEqual(len(launched), 2)
            self.assertEqual(launched[0].episode_start, 50)
            self.assertEqual(launched[1].episode_start, 56)

    def test_pending_scan_timeout_defers_second_drive_instead_of_colliding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            two_drives = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D1","/dev/rdisk5"',
                'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","STARLIGHT_BRIGADE_D2","/dev/rdisk6"',
            )
            spawned: list[_FakeChildProc] = []

            def fake_spawn(cmd, *, stdin=None, stdout=None, stderr=None, env=None):
                proc = _FakeChildProc(pid=61001 + len(spawned), cmd=cmd, env=env or {})
                spawned.append(proc)
                return proc

            out_buf = io.StringIO()
            cfg = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                scan_wait=0.1,
                poll_interval=0.05,
                log_dir=root / "logs",
                work_dir=root / "work",
                slots_dir=root / "slots",
            )
            watcher = BoxSetWatcher(
                cfg,
                list_drives_fn=lambda: two_drives,
                spawn_fn=fake_spawn,
                discover_procs_fn=lambda: [],
                notify_fn=lambda _m: False,
                out=out_buf,
            )
            launched = watcher.poll_once()
            # Drive 1 never wrote a slot -> Drive 2 MUST defer rather than collide on E1
            self.assertEqual(len(launched), 1)
            self.assertEqual(launched[0].drive_key, "DRVSER00001")
            self.assertNotIn("DRVSER00002", watcher.handled_discs)
            self.assertIn("deferring STARLIGHT_BRIGADE_D2", out_buf.getvalue())


class TestHandledDiscsAntiReripGuard(unittest.TestCase):
    """Ensure an un-ejected disc is never re-ripped, while eject -> new disc rips cleanly."""

    def test_same_disc_still_in_tray_not_reripped_until_tray_empties(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)

            disc1 = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D1","/dev/rdisk5"'
            )
            empty = _drv_output(
                'DRV:0,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","",""'
            )
            disc2 = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D2","/dev/rdisk5"'
            )

            # Poll timeline:
            # 1: disc1 inserted -> launches disc1
            # 2: disc1 fails (exit 2) and stays in tray -> reaped, MUST NOT re-rip
            # 3: disc1 STILL in tray -> MUST NOT re-rip
            # 4: tray emptied (state=0) -> handled_discs cleared
            # 5: disc2 inserted -> launches disc2 cleanly!
            # 6: disc2 finishes (exit 0), tray empty
            states = [disc1, disc1, disc1, empty, disc2, empty]
            idx = [0]
            spawned: list[_FakeChildProc] = []

            def fake_spawn(cmd, *, stdin=None, stdout=None, stderr=None, env=None):
                proc = _FakeChildProc(pid=70001 + len(spawned), cmd=cmd, env=env or {})
                spawned.append(proc)
                return proc

            def list_drives():
                cur = states[min(idx[0], len(states) - 1)]
                # Finish disc1 with failure right before tick 2 reap
                if idx[0] == 1 and len(spawned) == 1 and spawned[0].returncode is None:
                    spawned[0].finish(2)
                # Finish disc2 with success right before tick 6 reap
                if idx[0] == 5 and len(spawned) == 2 and spawned[1].returncode is None:
                    spawned[1].finish(0)
                idx[0] += 1
                return cur

            slack_msgs: list[str] = []
            cfg = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                poll_interval=0.05,
                scan_wait=1.0,
                max_discs=2,
                log_dir=root / "logs",
                work_dir=root / "work",
                slots_dir=root / "slots",
            )
            out_buf = io.StringIO()
            watcher = BoxSetWatcher(
                cfg,
                list_drives_fn=list_drives,
                spawn_fn=fake_spawn,
                discover_procs_fn=lambda: [],
                sleep_fn=lambda _s: None,
                notify_fn=slack_msgs.append,
                out=out_buf,
            )
            summary = watcher.run()

            self.assertEqual(len(spawned), 2)
            self.assertEqual(summary.launched, 2)
            self.assertEqual(summary.completed, 2)
            self.assertEqual(summary.failed, 1)
            self.assertEqual(summary.succeeded, 1)
            self.assertEqual(summary.results[0].volume, "STARLIGHT_BRIGADE_D1")
            self.assertEqual(summary.results[0].returncode, 2)
            self.assertEqual(summary.results[1].volume, "STARLIGHT_BRIGADE_D2")
            self.assertEqual(summary.results[1].returncode, 0)
            self.assertIn("FAILED STARLIGHT_BRIGADE_D1 (exit 2", out_buf.getvalue())
            self.assertEqual(len(slack_msgs), 2)
            self.assertIn("FAILED", slack_msgs[0])
            self.assertIn("finished", slack_msgs[1])

    def test_drive_identity_falls_back_to_device_node_when_serial_empty(self):
        row = _drv_output(
            'DRV:0,2,999,12,"DVD+R-DL OPTIARC DVD RW 1.03","OLD_DVD_D1","/dev/rdisk9"'
        )[0]
        self.assertEqual(row["serial"], "")
        self.assertEqual(drive_identity_key(row), "/dev/disk9")


class TestExtrasFlagsConfigAndCli(unittest.TestCase):
    """Test --no-extras / --with-extras pass-through, config/env precedence, and --max-discs."""

    def test_no_extras_and_with_extras_precedence_and_passthrough(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_file = root / "config"
            cfg_file.write_text("no_extras = true\n", encoding="utf-8")

            # Config file enables no_extras when env & CLI are unset
            self.assertTrue(resolve_no_extras(None, config_path=cfg_file, env={}))
            # Env overrides config file
            self.assertFalse(
                resolve_no_extras(None, config_path=cfg_file, env={"BURNDVD_NO_EXTRAS": "0"})
            )
            # CLI --with-extras overrides both env=1 and config=true
            self.assertFalse(
                resolve_no_extras(False, config_path=cfg_file, env={"BURNDVD_NO_EXTRAS": "1"})
            )
            # CLI --no-extras overrides env=0
            self.assertTrue(
                resolve_no_extras(True, config_path=cfg_file, env={"BURNDVD_NO_EXTRAS": "0"})
            )

            # Verify CLI parsing for --no-extras and --with-extras mutually overriding
            parser = watch_cli.build_parser()
            a_no = parser.parse_args(
                ["--show", "Starlight Brigade", "--season", "1", "--no-extras"]
            )
            self.assertIs(a_no.no_extras, True)

            a_with = parser.parse_args(
                [
                    "--show",
                    "Starlight Brigade",
                    "--season",
                    "1",
                    "--no-extras",
                    "--with-extras",
                ]
            )
            self.assertIs(a_with.no_extras, False)

            cfg_parsed = watch_cli.config_from_args(a_with, parser=parser)
            self.assertFalse(cfg_parsed.no_extras)
            self.assertEqual(cfg_parsed.media_format, "BD")

            # Verify --no-extras flag & BURNDVD_NO_EXTRAS env pass-through to ripqueue
            one_drive = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D1","/dev/rdisk5"'
            )
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)

            for no_extras_flag in (True, False):
                spawned: list[_FakeChildProc] = []

                def fake_spawn(cmd, *, stdin=None, stdout=None, stderr=None, env=None):
                    proc = _FakeChildProc(pid=80001, cmd=cmd, env=env or {})
                    proc.finish(0)
                    spawned.append(proc)
                    return proc

                cfg = WatchConfig(
                    show="Starlight Brigade",
                    season=1,
                    target_root=str(target_root),
                    no_extras=no_extras_flag,
                    max_discs=1,
                    poll_interval=0.05,
                    scan_wait=1.0,
                    log_dir=root / "logs",
                    work_dir=root / "work",
                    slots_dir=root / "slots",
                )
                watcher = BoxSetWatcher(
                    cfg,
                    list_drives_fn=lambda: one_drive,
                    spawn_fn=fake_spawn,
                    discover_procs_fn=lambda: [],
                    sleep_fn=lambda _s: None,
                    notify_fn=lambda _m: False,
                    out=io.StringIO(),
                )
                summary = watcher.run()
                self.assertEqual(summary.launched, 1)
                self.assertEqual(summary.succeeded, 1)
                if no_extras_flag:
                    self.assertIn("--no-extras", spawned[0].cmd)
                    self.assertEqual(spawned[0].env["BURNDVD_NO_EXTRAS"], "1")
                else:
                    self.assertNotIn("--no-extras", spawned[0].cmd)
                    self.assertEqual(spawned[0].env["BURNDVD_NO_EXTRAS"], "0")
                self.assertIn("--non-interactive", spawned[0].cmd)
                self.assertIn("--on-fail=retry", spawned[0].cmd)
                self.assertIn("--verify", spawned[0].cmd)

    def test_max_discs_caps_launch_when_two_drives_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            two_drives = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D1","/dev/rdisk5"',
                'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","STARLIGHT_BRIGADE_D2","/dev/rdisk6"',
            )
            spawned: list[_FakeChildProc] = []

            def fake_spawn(cmd, *, stdin=None, stdout=None, stderr=None, env=None):
                proc = _FakeChildProc(pid=81001 + len(spawned), cmd=cmd, env=env or {})
                proc.finish(0)
                spawned.append(proc)
                return proc

            cfg = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                max_discs=1,
                poll_interval=0.05,
                log_dir=root / "logs",
                work_dir=root / "work",
                slots_dir=root / "slots",
            )
            watcher = BoxSetWatcher(
                cfg,
                list_drives_fn=lambda: two_drives,
                spawn_fn=fake_spawn,
                discover_procs_fn=lambda: [],
                sleep_fn=lambda _s: None,
                notify_fn=lambda _m: False,
                out=io.StringIO(),
            )
            summary = watcher.run()
            self.assertEqual(summary.launched, 1)
            self.assertEqual(summary.completed, 1)
            self.assertEqual(len(spawned), 1)

    def test_keyboard_interrupt_leaves_active_rips_running_and_prints_pids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            one_drive = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D1","/dev/rdisk5"'
            )

            def fake_spawn(cmd, *, stdin=None, stdout=None, stderr=None, env=None):
                return _FakeChildProc(pid=82001, cmd=cmd, env=env or {})

            def raise_sigint(_s):
                raise KeyboardInterrupt()

            out_buf = io.StringIO()
            cfg = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                max_discs=0,
                poll_interval=0.05,
                log_dir=root / "logs",
                work_dir=root / "work",
                slots_dir=root / "slots",
            )
            watcher = BoxSetWatcher(
                cfg,
                list_drives_fn=lambda: one_drive,
                spawn_fn=fake_spawn,
                discover_procs_fn=lambda: [],
                sleep_fn=raise_sigint,
                notify_fn=lambda _m: False,
                out=out_buf,
            )
            summary = watcher.run()
            self.assertTrue(summary.stopped_by_signal)
            self.assertEqual(len(summary.active_at_exit), 1)
            self.assertEqual(summary.active_at_exit[0].proc.pid, 82001)
            self.assertIn("pid 82001", out_buf.getvalue())
            self.assertIn("left running safely", out_buf.getvalue())

    def test_default_target_root_prefers_writable_nfs_and_4k_subdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            nfs = Path(tmp) / "nfs"
            smb = Path(tmp) / "smb"
            nfs.mkdir()
            smb.mkdir()
            self.assertEqual(
                resolve_default_target_root(
                    "Starlight Brigade", "BD", nfs_root=nfs, smb_root=smb
                ),
                str(nfs / "TV Shows" / "Starlight Brigade"),
            )
            self.assertEqual(
                resolve_default_target_root(
                    "Starlight Brigade", "4K", nfs_root=nfs, smb_root=smb
                ),
                str(nfs / "TV Shows 4K" / "Starlight Brigade"),
            )
            # Non-existent NFS falls back to SMB
            self.assertEqual(
                resolve_default_target_root(
                    "Starlight Brigade",
                    "BD",
                    nfs_root=Path(tmp) / "missing_nfs",
                    smb_root=smb,
                ),
                str(smb / "TV Shows" / "Starlight Brigade"),
            )

    def test_notify_slack_is_best_effort_noop_when_unset_or_network_error(self):
        self.assertFalse(notify_slack("hello", env={}))
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertFalse(
                notify_slack(
                    "hello", env={"BURNDVD_SLACK_WEBHOOK": "https://example.invalid/hook"}
                )
            )


if __name__ == "__main__":
    unittest.main()
