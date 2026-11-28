"""Unit tests for watch-mode early drive release, tray-ready vs finalization tracking,
and non-blocking drive detection when sibling drives are actively ripping.

100% mocked — never touches any real optical drive, MakeMKV binary, or live process.
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from burndvd import metadata, watch
from burndvd.drive import (
    DRIVE_DEVICE_ENV,
    DRIVE_SERIAL_ENV,
    DRUTIL_DRIVE_ENV,
    mark_drive_released,
    parse_makemkv_drives,
)
from burndvd.watch import (
    BoxSetWatcher,
    WatchConfig,
    list_makemkv_drives,
    read_drive_released_marker,
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


def _state_path_from_cmd(cmd: list[str]) -> Path:
    return Path(cmd[cmd.index("--state") + 1])


def _queue_path_from_cmd(cmd: list[str]) -> Path:
    return Path(cmd[cmd.index("--queue") + 1])


class TestWatchEarlyDriveReleaseAndHandoff(unittest.TestCase):
    """Released optical drives immediately accept the next disc while finalizers finish."""

    def test_released_drive_accepts_new_disc_while_previous_finalizes_and_preserves_reservations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            slots_dir = root / "slots"

            disc1 = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D1","/dev/rdisk5"'
            )
            empty = _drv_output(
                'DRV:0,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","",""'
            )
            disc2 = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D2","/dev/rdisk5"'
            )
            disc3 = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D3","/dev/rdisk5"'
            )

            spawned: list[_FakeChildProc] = []
            current_drives: list[dict] = list(disc1)
            slack_msgs: list[str] = []
            out_buf = io.StringIO()

            def fake_spawn(cmd, *, stdin=None, stdout=None, stderr=None, env=None, **_kw):
                pid = 91001 + len(spawned)
                proc = _FakeChildProc(pid=pid, cmd=cmd, env=env or {})
                spawned.append(proc)
                return proc

            cfg = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                poll_interval=0.05,
                scan_wait=0.5,
                log_dir=root / "logs",
                work_dir=root / "work",
                slots_dir=slots_dir,
            )

            with mock.patch.object(
                metadata, "_pid_alive", side_effect=lambda p: p in {91001, 91002, 91003}
            ):
                watcher = BoxSetWatcher(
                    cfg,
                    list_drives_fn=lambda: list(current_drives),
                    spawn_fn=fake_spawn,
                    discover_procs_fn=lambda: [],
                    notify_fn=slack_msgs.append,
                    out=out_buf,
                )

                # Tick 1: Launch Disc 1 (E1+)
                l1 = watcher.poll_once()
                self.assertEqual(len(l1), 1)
                self.assertEqual(l1[0].episode_start, 1)
                self.assertIn("DRVSER00001", watcher.active_procs)
                self.assertEqual(watcher.finalizing_procs, [])

                # Disc 1 publishes its TV slot reservation (E1..E4) and releases the optical drive
                # while still running ffmpeg --verify + NAS copy in background.
                metadata.write_tv_slot_reservation(
                    pid=spawned[0].pid,
                    title="Starlight Brigade",
                    season=1,
                    target_root=str(target_root),
                    episode_start=1,
                    episode_count=4,
                    slots_dir=slots_dir,
                )
                mark_drive_released(l1[0].state_path, device="disc:0", ejected=True)

                # Tick 2: Tray is empty -> rip1 moves to finalizing_procs & fires tray-ready!
                current_drives[:] = empty
                l2 = watcher.poll_once()
                self.assertEqual(l2, [])
                self.assertNotIn("DRVSER00001", watcher.active_procs)
                self.assertEqual(len(watcher.finalizing_procs), 1)
                self.assertTrue(watcher.finalizing_procs[0].drive_released)
                self.assertTrue(watcher.finalizing_procs[0].tray_ready_confirmed)
                self.assertEqual(len(slack_msgs), 1)
                self.assertIn("Drive tray ready", slack_msgs[0])
                self.assertIn("TRAY READY", out_buf.getvalue())

                # Tick 3: User inserts Disc 2 while Disc 1 is STILL finalizing -> launches immediately at E5+!
                current_drives[:] = disc2
                l3 = watcher.poll_once()
                self.assertEqual(len(l3), 1)
                self.assertEqual(l3[0].volume, "STARLIGHT_BRIGADE_D2")
                self.assertEqual(l3[0].episode_start, 5)
                self.assertIn("DRVSER00001", watcher.active_procs)
                self.assertEqual(watcher.active_procs["DRVSER00001"].volume, "STARLIGHT_BRIGADE_D2")
                self.assertEqual(len(watcher.finalizing_procs), 1)

                with _queue_path_from_cmd(spawned[1].cmd).open(encoding="utf-8", newline="") as fh:
                    row2 = next(csv.DictReader(fh))
                self.assertEqual(row2["episode_start"], "5")

                # Tick 4: Disc 2 ALSO releases drive into finalizing while Disc 1 is STILL finalizing,
                # and Disc 3 is inserted -> 2 concurrent finalizing procs from the same drive + Disc 3 active!
                metadata.write_tv_slot_reservation(
                    pid=spawned[1].pid,
                    title="Starlight Brigade",
                    season=1,
                    target_root=str(target_root),
                    episode_start=5,
                    episode_count=4,
                    slots_dir=slots_dir,
                )
                mark_drive_released(l3[0].state_path, device="disc:0", ejected=True)
                current_drives[:] = disc3
                l4 = watcher.poll_once()
                self.assertEqual(len(l4), 1)
                self.assertEqual(l4[0].volume, "STARLIGHT_BRIGADE_D3")
                self.assertEqual(l4[0].episode_start, 9)
                self.assertEqual(len(watcher.finalizing_procs), 2)
                self.assertEqual(watcher.active_procs["DRVSER00001"].volume, "STARLIGHT_BRIGADE_D3")

                # Clean up fake live processes from module registry
                for p in spawned:
                    p.finish(0)
                watcher.reap_finished()
                self.assertEqual(watcher.active_procs, {})
                self.assertEqual(watcher.finalizing_procs, [])
                self.assertEqual(len(watcher.results), 3)

    def test_finalizer_completion_and_failure_reporting_after_drive_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            slots_dir = root / "slots"

            disc1 = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D1","/dev/rdisk5"'
            )
            disc2 = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D2","/dev/rdisk5"'
            )
            empty = _drv_output(
                'DRV:0,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","",""'
            )

            spawned: list[_FakeChildProc] = []
            slack_msgs: list[str] = []
            out_buf = io.StringIO()
            tick = [0]

            def fake_spawn(cmd, *, stdin=None, stdout=None, stderr=None, env=None, **_kw):
                pid = 92001 + len(spawned)
                proc = _FakeChildProc(pid=pid, cmd=cmd, env=env or {})
                spawned.append(proc)
                # Write immediate TV slot reservation so subsequent disc advances cleanly.
                ep_start = 1 if len(spawned) == 1 else 5
                metadata.write_tv_slot_reservation(
                    pid=pid,
                    title="Starlight Brigade",
                    season=1,
                    target_root=str(target_root),
                    episode_start=ep_start,
                    episode_count=4,
                    slots_dir=slots_dir,
                )
                return proc

            def list_drives():
                tick[0] += 1
                if tick[0] == 1:
                    return disc1
                if tick[0] == 2:
                    # Disc 1 releases drive with eject=True, user immediately swaps in Disc 2.
                    mark_drive_released(
                        _state_path_from_cmd(spawned[0].cmd),
                        device="disc:0",
                        ejected=True,
                    )
                    return disc2
                if tick[0] == 3:
                    # Disc 1 fails during background NAS verification/publish while Disc 2 is still ripping!
                    spawned[0].finish(2)
                    return disc2
                if tick[0] == 4:
                    # Disc 2 releases drive into finalizing (must NOT exit run() yet because max_discs=2
                    # must wait for finalizing_procs to complete!).
                    mark_drive_released(
                        _state_path_from_cmd(spawned[1].cmd),
                        device="disc:0",
                        ejected=True,
                    )
                    return empty
                # Tick 5: Disc 2 finishes background publication cleanly.
                spawned[1].finish(0)
                return empty

            cfg = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                poll_interval=0.05,
                scan_wait=1.0,
                max_discs=2,
                log_dir=root / "logs",
                work_dir=root / "work",
                slots_dir=slots_dir,
            )

            with mock.patch.object(
                metadata, "_pid_alive", side_effect=lambda p: p in {92001, 92002}
            ):
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

            # Must not exit early on tick 4 while Disc 2 is still finalizing; finishes on tick >= 5.
            self.assertGreaterEqual(tick[0], 5)
            self.assertEqual(summary.launched, 2)
            self.assertEqual(summary.completed, 2)
            self.assertEqual(summary.succeeded, 1)
            self.assertEqual(summary.failed, 1)
            self.assertTrue(summary.results[0].was_finalizing)
            self.assertEqual(summary.results[0].volume, "STARLIGHT_BRIGADE_D1")
            self.assertEqual(summary.results[0].returncode, 2)
            self.assertTrue(summary.results[1].was_finalizing)
            self.assertEqual(summary.results[1].volume, "STARLIGHT_BRIGADE_D2")
            self.assertEqual(summary.results[1].returncode, 0)

            out_txt = out_buf.getvalue()
            self.assertIn("FINALIZE FAILED STARLIGHT_BRIGADE_D1 (exit 2", out_txt)
            self.assertIn("PUBLISHED STARLIGHT_BRIGADE_D2 (finalization complete, exit 0", out_txt)

            # Distinct Slack notifications: tray-ready vs finalization failure vs final publication complete
            self.assertTrue(any("Drive tray ready" in m and "STARLIGHT_BRIGADE_D1" in m for m in slack_msgs))
            self.assertTrue(any("Watch finalization FAILED" in m and "STARLIGHT_BRIGADE_D1" in m for m in slack_msgs))
            self.assertTrue(any("Drive tray ready" in m and "STARLIGHT_BRIGADE_D2" in m for m in slack_msgs))
            self.assertTrue(any("Watch publication complete" in m and "STARLIGHT_BRIGADE_D2" in m for m in slack_msgs))

            # Reaping Disc 1 while Disc 2 was active did NOT clobber Disc 2's handled_discs guard
            self.assertNotIn("DRVSER00001", watcher.active_procs)

    def test_signal_exit_reports_both_optical_active_and_finalizing_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            slots_dir = root / "slots"

            disc1 = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D1","/dev/rdisk5"'
            )
            disc2 = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D2","/dev/rdisk5"'
            )

            spawned: list[_FakeChildProc] = []
            tick = [0]

            def fake_spawn(cmd, *, stdin=None, stdout=None, stderr=None, env=None, **_kw):
                pid = 92501 + len(spawned)
                proc = _FakeChildProc(pid=pid, cmd=cmd, env=env or {})
                spawned.append(proc)
                if len(spawned) == 1:
                    metadata.write_tv_slot_reservation(
                        pid=pid,
                        title="Starlight Brigade",
                        season=1,
                        target_root=str(target_root),
                        episode_start=1,
                        episode_count=4,
                        slots_dir=slots_dir,
                    )
                return proc

            def list_drives():
                tick[0] += 1
                if tick[0] == 1:
                    return disc1
                mark_drive_released(
                    _state_path_from_cmd(spawned[0].cmd), device="disc:0", ejected=True
                )
                return disc2

            def stop_on_second_sleep(_s: float) -> None:
                if tick[0] >= 2:
                    raise KeyboardInterrupt()

            out_buf = io.StringIO()
            cfg = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                poll_interval=0.05,
                scan_wait=0.5,
                log_dir=root / "logs",
                work_dir=root / "work",
                slots_dir=slots_dir,
            )

            with mock.patch.object(
                metadata, "_pid_alive", side_effect=lambda p: p in {92501, 92502}
            ):
                watcher = BoxSetWatcher(
                    cfg,
                    list_drives_fn=list_drives,
                    spawn_fn=fake_spawn,
                    discover_procs_fn=lambda: [],
                    sleep_fn=stop_on_second_sleep,
                    notify_fn=lambda _m: False,
                    out=out_buf,
                )
                summary = watcher.run()

            self.assertTrue(summary.stopped_by_signal)
            self.assertEqual(len(summary.active_at_exit), 2)
            self.assertEqual(len(summary.finalizing_at_exit), 1)
            self.assertEqual(summary.finalizing_at_exit[0].proc.pid, 92501)
            out_txt = out_buf.getvalue()
            self.assertIn("1 optical, 1 finalizing", out_txt)
            self.assertIn("pid 92501", out_txt)
            self.assertIn("pid 92502", out_txt)

            # Clean up module registry
            for p in spawned:
                p.finish(0)
            watcher.reap_finished()


class TestTrayReadyConfirmationStatus(unittest.TestCase):
    """Tray-ready notifications must require confirmed eject and never overstate readiness."""

    def test_no_tray_ready_notification_when_ejected_false_and_no_rerip_while_disc_remains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            slots_dir = root / "slots"

            disc1 = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D1","/dev/rdisk5"'
            )
            empty = _drv_output(
                'DRV:0,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","",""'
            )
            disc2 = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D2","/dev/rdisk5"'
            )

            spawned: list[_FakeChildProc] = []
            current_drives = list(disc1)
            slack_msgs: list[str] = []
            out_buf = io.StringIO()

            def fake_spawn(cmd, *, stdin=None, stdout=None, stderr=None, env=None, **_kw):
                pid = 93001 + len(spawned)
                proc = _FakeChildProc(pid=pid, cmd=cmd, env=env or {})
                spawned.append(proc)
                if len(spawned) == 1:
                    metadata.write_tv_slot_reservation(
                        pid=pid,
                        title="Starlight Brigade",
                        season=1,
                        target_root=str(target_root),
                        episode_start=1,
                        episode_count=4,
                        slots_dir=slots_dir,
                    )
                return proc

            cfg = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                poll_interval=0.05,
                scan_wait=0.5,
                log_dir=root / "logs",
                work_dir=root / "work",
                slots_dir=slots_dir,
            )

            with mock.patch.object(
                metadata, "_pid_alive", side_effect=lambda p: p in {93001, 93002}
            ):
                watcher = BoxSetWatcher(
                    cfg,
                    list_drives_fn=lambda: list(current_drives),
                    spawn_fn=fake_spawn,
                    discover_procs_fn=lambda: [],
                    notify_fn=slack_msgs.append,
                    out=out_buf,
                )

                l1 = watcher.poll_once()
                self.assertEqual(len(l1), 1)

                # Write drive_released with ejected=False (e.g. --no-eject or eject failure).
                mark_drive_released(l1[0].state_path, device="disc:0", ejected=False)

                # Tick 2: Disc 1 still in tray -> moves to finalizing_procs, does NOT re-rip,
                # and MUST NOT fire tray-ready!
                self.assertEqual(watcher.poll_once(), [])
                self.assertEqual(len(watcher.finalizing_procs), 1)
                self.assertFalse(watcher.finalizing_procs[0].drive_ejected)
                self.assertFalse(watcher.finalizing_procs[0].tray_ready_confirmed)
                self.assertEqual(slack_msgs, [])
                self.assertNotIn("TRAY READY", out_buf.getvalue())
                self.assertIn("eject not confirmed", out_buf.getvalue())

                # Tick 3: User manually removes Disc 1 -> still MUST NOT fire tray-ready
                # because marker recorded ejected=False!
                current_drives[:] = empty
                self.assertEqual(watcher.poll_once(), [])
                self.assertEqual(slack_msgs, [])
                self.assertFalse(watcher.finalizing_procs[0].tray_ready_confirmed)

                # Tick 4: User inserts Disc 2 -> drive immediately accepts Disc 2 while Disc 1 finalizes!
                current_drives[:] = disc2
                l4 = watcher.poll_once()
                self.assertEqual(len(l4), 1)
                self.assertEqual(l4[0].volume, "STARLIGHT_BRIGADE_D2")
                self.assertEqual(l4[0].episode_start, 5)

                # Disc 1 finishes -> final publication notification fires cleanly!
                spawned[0].finish(0)
                reaped = watcher.reap_finished()
                self.assertEqual(len(reaped), 1)
                self.assertEqual(len(slack_msgs), 1)
                self.assertIn("Watch publication complete", slack_msgs[0])

                spawned[1].finish(0)
                watcher.reap_finished()

    def test_ejected_true_but_same_disc_still_present_waits_for_tray_absence_before_tray_ready(self):
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

            spawned: list[_FakeChildProc] = []
            current_drives = list(disc1)
            slack_msgs: list[str] = []
            out_buf = io.StringIO()

            def fake_spawn(cmd, *, stdin=None, stdout=None, stderr=None, env=None, **_kw):
                proc = _FakeChildProc(pid=93501 + len(spawned), cmd=cmd, env=env or {})
                spawned.append(proc)
                return proc

            cfg = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                poll_interval=0.05,
                scan_wait=0.5,
                log_dir=root / "logs",
                work_dir=root / "work",
                slots_dir=root / "slots",
            )
            watcher = BoxSetWatcher(
                cfg,
                list_drives_fn=lambda: list(current_drives),
                spawn_fn=fake_spawn,
                discover_procs_fn=lambda: [],
                notify_fn=slack_msgs.append,
                out=out_buf,
            )

            l1 = watcher.poll_once()
            self.assertEqual(len(l1), 1)

            # Marker optimistically claims ejected=True, but drive still shows Disc 1 in tray!
            mark_drive_released(l1[0].state_path, device="disc:0", ejected=True)

            # Tick 2: Disc 1 still detected -> must NOT falsely fire tray-ready or re-rip!
            self.assertEqual(watcher.poll_once(), [])
            self.assertEqual(len(watcher.finalizing_procs), 1)
            self.assertFalse(watcher.finalizing_procs[0].tray_ready_confirmed)
            self.assertEqual(slack_msgs, [])
            self.assertIn("disc still detected in tray", out_buf.getvalue())
            self.assertNotIn("TRAY READY", out_buf.getvalue())

            # Tick 3: Drive now reports empty tray -> tray-ready is NOW confirmed!
            current_drives[:] = empty
            self.assertEqual(watcher.poll_once(), [])
            self.assertTrue(watcher.finalizing_procs[0].tray_ready_confirmed)
            self.assertEqual(len(slack_msgs), 1)
            self.assertIn("Drive tray ready", slack_msgs[0])
            self.assertIn("TRAY READY", out_buf.getvalue())

            # Tick 4: No duplicate tray-ready notification on subsequent polls.
            self.assertEqual(watcher.poll_once(), [])
            self.assertEqual(len(slack_msgs), 1)

            spawned[0].finish(0)
            watcher.reap_finished()

    def test_corrupt_release_marker_releases_drive_without_claiming_eject(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            Path(f"{state_path}.drive_released").write_text("not-json\n", encoding="utf-8")
            marker = read_drive_released_marker(state_path)
            self.assertEqual(marker, {})
            self.assertFalse(watch.is_eject_confirmed(marker))


class TestNonBlockingDriveDetectionWithBusySibling(unittest.TestCase):
    """Avoid global `makemkvcon -r info list` timeouts when sibling drives are actively ripping."""

    def test_skips_global_info_list_when_sibling_busy_and_uses_drutil_topology(self):
        with tempfile.TemporaryDirectory() as tmp:
            topo_path = Path(tmp) / "drive-topology.json"
            topo_path.write_text(
                json.dumps(
                    {
                        "drutil_to_serial": {"1": "DRVSER00001", "2": "DRVSER00002"},
                        "serial_to_slot": {"DRVSER00001": 0, "DRVSER00002": 1},
                        "drutil_to_slot": {"1": 0, "2": 1},
                    }
                ),
                encoding="utf-8",
            )

            def fake_drutil_status(drv: int | None = None) -> str:
                if drv == 1:
                    return "  Type: BD-ROM  Name: /dev/disk5\n"
                if drv == 2:
                    return "  Type: BD-ROM  Name: /dev/disk6\n"
                return ""

            def forbid_global_makemkv(*_args, **_kwargs):
                raise AssertionError("global makemkvcon -r info list must NOT be called when sibling is busy!")

            with mock.patch.object(watch.subprocess, "run", side_effect=forbid_global_makemkv), \
                 mock.patch.object(watch, "_drutil_list_drives", return_value=[1, 2]), \
                 mock.patch.object(watch, "_drutil_status_output", side_effect=fake_drutil_status), \
                 mock.patch.object(watch, "_volume_for_device", side_effect=lambda dev: "STARLIGHT_BRIGADE_D2" if dev == "/dev/disk6" else "STARLIGHT_BRIGADE_D1"), \
                 mock.patch.object(watch, "_probe_free_makemkv_slot", side_effect=AssertionError("should not probe when topology resolves")):
                rows = list_makemkv_drives(
                    "/fake/makemkvcon",
                    busy_slots={0},
                    busy_devices={"/dev/disk5"},
                    busy_serials={"DRVSER00001"},
                    busy_drutil_drives={1},
                    busy_claims={},
                    topology_path=topo_path,
                )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["index"], 1)
            self.assertEqual(rows[0]["device_node"], "/dev/disk6")
            self.assertEqual(rows[0]["serial"], "DRVSER00002")
            self.assertEqual(rows[0]["volume"], "STARLIGHT_BRIGADE_D2")
            self.assertEqual(rows[0]["drutil_drive"], 2)
            self.assertTrue(rows[0]["has_media"])

    def test_targeted_free_slot_probe_never_touches_busy_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            topo_path = Path(tmp) / "drive-topology.json"
            probed_slots: list[int] = []

            def fake_drutil_status(drv: int | None = None) -> str:
                if drv == 1:
                    return "  Type: BD-ROM  Name: /dev/disk5\n"
                if drv == 2:
                    return "  Type: No Media Inserted\n"
                if drv == 3:
                    return "  Type: BD-ROM  Name: /dev/disk7\n"
                return ""

            def fake_probe(_mkv: str, slot_idx: int, **_kw):
                probed_slots.append(slot_idx)
                self.assertNotEqual(slot_idx, 0, "MUST NEVER probe busy disc:0!")
                if slot_idx == 1:
                    return ("empty", "DRVSER00002", "")
                if slot_idx == 2:
                    return ("loaded", "DRVSER00003", "STARLIGHT_BRIGADE_D3")
                return ("unknown", "", "")

            with mock.patch.object(
                watch.subprocess, "run", side_effect=AssertionError("global info list must be skipped")
            ), mock.patch.object(watch, "_drutil_list_drives", return_value=[1, 2, 3]), \
                 mock.patch.object(watch, "_drutil_status_output", side_effect=fake_drutil_status), \
                 mock.patch.object(watch, "_volume_for_device", return_value="STARLIGHT_BRIGADE_D3"), \
                 mock.patch.object(watch, "_probe_free_makemkv_slot", side_effect=fake_probe):
                rows = list_makemkv_drives(
                    "/fake/makemkvcon",
                    busy_claims={0: {"dev": "/dev/disk5", "serial": "DRVSER00001", "source": "claim"}},
                    topology_path=topo_path,
                )

            self.assertEqual(probed_slots, [1, 2])
            loaded_rows = [r for r in rows if r["has_media"]]
            self.assertEqual(len(loaded_rows), 1)
            self.assertEqual(loaded_rows[0]["index"], 2)
            self.assertEqual(loaded_rows[0]["device_node"], "/dev/disk7")
            self.assertEqual(loaded_rows[0]["serial"], "DRVSER00003")
            self.assertEqual(loaded_rows[0]["volume"], "STARLIGHT_BRIGADE_D3")
            self.assertEqual(loaded_rows[0]["drutil_drive"], 3)

    def test_refuses_unsafe_slot_assignment_when_all_slots_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            topo_path = Path(tmp) / "drive-topology.json"
            with mock.patch.object(
                watch.subprocess, "run", side_effect=AssertionError("global info list must be skipped")
            ), mock.patch.object(watch, "_drutil_list_drives", return_value=[1]), \
                 mock.patch.object(watch, "_drutil_status_output", return_value="  Type: BD-ROM  Name: /dev/disk5\n"), \
                 mock.patch.object(watch, "_volume_for_device", return_value="STARLIGHT_BRIGADE_D1"):
                rows = list_makemkv_drives(
                    "/fake/makemkvcon",
                    busy_slots={0},
                    busy_claims={},
                    topology_path=topo_path,
                )
            self.assertEqual(rows, [])

    def test_global_info_list_timeout_falls_back_safely_to_nonblocking_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            topo_path = Path(tmp) / "drive-topology.json"
            topo_path.write_text(
                json.dumps(
                    {
                        "drutil_to_serial": {"1": "DRVSER00001"},
                        "serial_to_slot": {"DRVSER00001": 0},
                        "drutil_to_slot": {"1": 0},
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                watch.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd=["makemkvcon"], timeout=20),
            ), mock.patch.object(watch, "_drutil_list_drives", return_value=[1]), \
                 mock.patch.object(watch, "_drutil_status_output", return_value="  Type: BD-ROM  Name: /dev/disk5\n"), \
                 mock.patch.object(watch, "_volume_for_device", return_value="STARLIGHT_BRIGADE_D1"):
                rows = list_makemkv_drives(
                    "/fake/makemkvcon",
                    busy_claims={},
                    topology_path=topo_path,
                )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["index"], 0)
            self.assertEqual(rows[0]["serial"], "DRVSER00001")
            self.assertEqual(rows[0]["volume"], "STARLIGHT_BRIGADE_D1")

    def test_watcher_end_to_end_uses_nonblocking_detection_when_sibling_ripping_and_reuses_released_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            slots_dir = root / "slots"
            topo_path = root / "drive-topology.json"

            # Phase 1 (idle): global info list succeeds and teaches watcher both drives' topology.
            initial_mkv_stdout = "\n".join(
                [
                    'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_D1","/dev/rdisk5"',
                    'DRV:1,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","",""',
                ]
            )
            mkv_global_calls: list[list[str]] = []

            def fake_subprocess_run(cmd, **_kw):
                if cmd and cmd[0] == "/fake/makemkvcon":
                    mkv_global_calls.append(list(cmd))
                    return subprocess.CompletedProcess(cmd, 0, stdout=initial_mkv_stdout, stderr="")
                raise AssertionError(f"unexpected subprocess.run: {cmd}")

            # Mutable optical state for drutil/diskutil during Phase 2 & Phase 3
            drutil_state = {
                1: "  Type: BD-ROM  Name: /dev/disk5\n",
                2: "  Type: No Media Inserted\n",
            }
            disk_vols = {
                "/dev/disk5": "STARLIGHT_BRIGADE_D1",
                "/dev/disk6": "",
            }

            spawned: list[_FakeChildProc] = []

            def fake_spawn(cmd, *, stdin=None, stdout=None, stderr=None, env=None, **_kw):
                pid = 94001 + len(spawned)
                proc = _FakeChildProc(pid=pid, cmd=cmd, env=env or {})
                spawned.append(proc)
                ep_start = 1 + (len(spawned) - 1) * 4
                metadata.write_tv_slot_reservation(
                    pid=pid,
                    title="Starlight Brigade",
                    season=1,
                    target_root=str(target_root),
                    episode_start=ep_start,
                    episode_count=4,
                    slots_dir=slots_dir,
                )
                return proc

            cfg = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                poll_interval=0.05,
                scan_wait=0.5,
                makemkvcon="/fake/makemkvcon",
                ripqueue_py="/fake/ripqueue.py",
                log_dir=root / "logs",
                work_dir=root / "work",
                slots_dir=slots_dir,
            )

            with mock.patch.object(watch, "DRIVE_TOPOLOGY_DEFAULT_PATH", topo_path), \
                 mock.patch.object(watch, "active_busy_disc_claims", return_value={}), \
                 mock.patch.object(watch.subprocess, "run", side_effect=fake_subprocess_run), \
                 mock.patch.object(watch, "_drutil_list_drives", return_value=[1, 2]), \
                 mock.patch.object(watch, "_drutil_status_output", side_effect=lambda ix=None: drutil_state.get(ix, "")), \
                 mock.patch.object(watch, "_volume_for_device", side_effect=lambda dev: disk_vols.get(dev, "")), \
                 mock.patch.object(metadata, "_pid_alive", side_effect=lambda p: p in {94001, 94002, 94003}):
                watcher = BoxSetWatcher(
                    cfg,
                    spawn_fn=fake_spawn,
                    discover_procs_fn=lambda: [],
                    notify_fn=lambda _m: False,
                    out=io.StringIO(),
                )

                # Tick 1 (no busy drives): global info list runs once, learns topology, launches Drive 0.
                l1 = watcher.poll_once()
                self.assertEqual(len(l1), 1)
                self.assertEqual(l1[0].drive_key, "DRVSER00001")
                self.assertEqual(len(mkv_global_calls), 1)

                # Tick 2: Drive 0 is actively ripping; user inserts Disc 2 into Drive 1.
                # Global `makemkvcon -r info list` MUST NOT be called again!
                drutil_state[2] = "  Type: BD-ROM  Name: /dev/disk6\n"
                disk_vols["/dev/disk6"] = "STARLIGHT_BRIGADE_D2"
                l2 = watcher.poll_once()
                self.assertEqual(len(mkv_global_calls), 1, "global info list must be skipped while Drive 0 is ripping")
                self.assertEqual(len(l2), 1)
                self.assertEqual(l2[0].drive_key, "DRVSER00002")
                self.assertEqual(l2[0].disc_index, 1)
                self.assertEqual(l2[0].episode_start, 5)
                self.assertEqual(spawned[1].env[DRIVE_SERIAL_ENV], "DRVSER00002")
                self.assertEqual(spawned[1].env[DRIVE_DEVICE_ENV], "/dev/disk6")
                self.assertEqual(spawned[1].env[DRUTIL_DRIVE_ENV], "2")

                # Tick 3: Drive 0 releases into background finalization and user inserts Disc 3
                # while Drive 1 is STILL actively ripping -> Drive 0 launches Disc 3 with ZERO global info list!
                mark_drive_released(l1[0].state_path, device="disc:0", ejected=True)
                disk_vols["/dev/disk5"] = "STARLIGHT_BRIGADE_D3"
                l3 = watcher.poll_once()
                self.assertEqual(len(mkv_global_calls), 1, "global info list must still be skipped while Drive 1 is ripping")
                self.assertEqual(len(l3), 1)
                self.assertEqual(l3[0].drive_key, "DRVSER00001")
                self.assertEqual(l3[0].disc_index, 0)
                self.assertEqual(l3[0].volume, "STARLIGHT_BRIGADE_D3")
                self.assertEqual(l3[0].episode_start, 9)
                self.assertEqual(len(watcher.finalizing_procs), 1)
                self.assertEqual(set(watcher.active_procs.keys()), {"DRVSER00001", "DRVSER00002"})

                for p in spawned:
                    p.finish(0)
                watcher.reap_finished()


if __name__ == "__main__":
    unittest.main()
