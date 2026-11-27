#!/usr/bin/env python3
"""Zero-hardware 5-minute synthetic demo of burndvd's safety & automation guarantees.

Exercises real burndvd library code in an isolated temporary directory without
touching any optical drive, MakeMKV binary, or live NAS mount:

  1. Watch mode strict show/season binding (rejects unrelated media, admits valid discs,
     serializes multi-drive episode slots, creates unique attempt dirs, isolates child sessions).
  2. Crash-recovery publish journal (injects a crash AFTER atomic publish and BEFORE commit
     manifest write, then replays cleanly via fsync'd SHA-256 intent reconciliation).
  3. Multi-drive checkrip attribution & versioned JSON schema (duplicate volume labels never
     steal another drive's active rip row; legacy flat keys remain stable under discovery).

Usage:
    python3 examples/synthetic_demo.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Allow running directly from a source checkout without installing first.
_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if _REPO_SRC.is_dir() and str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from burndvd import metadata, ripper
from burndvd.cli import checkrip
from burndvd.drive import parse_makemkv_drives
from burndvd.watch import BoxSetWatcher, WatchConfig


class _SyntheticWorker:
    def __init__(self, pid: int, cmd: list[str], start_new_session: bool):
        self.pid = pid
        self.cmd = list(cmd)
        self.start_new_session = bool(start_new_session)
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def finish(self, code: int = 0) -> None:
        self.returncode = code


def _demo_watch_mode(root: Path) -> None:
    print("== 1. Watch Mode: Strict Disc Binding, Multi-Drive Slot Serialization & Ctrl-C Isolation ==")
    target_root = root / "nas" / "TV Shows" / "Starlight Brigade"
    season_dir = target_root / "Season 01"
    season_dir.mkdir(parents=True)
    for ep in (1, 2, 3):
        (season_dir / f"Starlight Brigade - S01E{ep:02d}.mkv").write_bytes(b"existing-episode")

    slots_dir = root / "slots"
    drives_tick1 = parse_makemkv_drives(
        "\n".join(
            [
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","RANDOM_MOVIE_2024","/dev/rdisk5"',
                'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00004","STARLIGHT_D1","/dev/rdisk8"',
                'DRV:2,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","STARLIGHT_BRIGADE_S01_D1","/dev/rdisk6"',
                'DRV:3,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00003","STARLIGHT_BRIGADE_S01_D2","/dev/rdisk7"',
            ]
        )
    )
    spawned: list[_SyntheticWorker] = []

    def fake_spawn(cmd, *, stdin=None, stdout=None, stderr=None, env=None, start_new_session=False):
        pid = 41001 + len(spawned)
        worker = _SyntheticWorker(pid=pid, cmd=cmd, start_new_session=start_new_session)
        spawned.append(worker)
        if len(spawned) == 1:
            def _publish_slot():
                time.sleep(0.08)
                metadata.write_tv_slot_reservation(
                    pid=pid,
                    title="Starlight Brigade",
                    season=1,
                    target_root=str(target_root),
                    episode_start=4,
                    episode_count=3,
                    slots_dir=slots_dir,
                )

            threading.Thread(target=_publish_slot, daemon=True).start()
        return worker

    watch_out = io.StringIO()
    cfg = WatchConfig(
        show="Starlight Brigade",
        season=1,
        target_root=str(target_root),
        no_extras=True,
        poll_interval=0.05,
        scan_wait=2.0,
        log_dir=root / "logs",
        work_dir=root / "work",
        slots_dir=slots_dir,
    )

    def _simulate_ctrl_c(_sleep_s: float) -> None:
        raise KeyboardInterrupt()

    with mock.patch.object(metadata, "_pid_alive", side_effect=lambda p: p in {41001, 41002}):
        watcher = BoxSetWatcher(
            cfg,
            list_drives_fn=lambda: drives_tick1,
            spawn_fn=fake_spawn,
            discover_procs_fn=lambda: [],
            sleep_fn=_simulate_ctrl_c,
            notify_fn=lambda _m: False,
            out=watch_out,
        )
        summary = watcher.run()

    assert summary.launched == 2, f"expected 2 valid launches, got {summary.launched}"
    assert summary.stopped_by_signal is True
    assert len(summary.active_at_exit) == 2
    assert all(w.start_new_session for w in spawned), "workers must launch in isolated sessions"
    assert spawned[0].cmd != spawned[1].cmd
    assert summary.active_at_exit[0].episode_start == 4
    assert summary.active_at_exit[1].episode_start == 7
    assert summary.active_at_exit[0].queue_path.parent != summary.active_at_exit[1].queue_path.parent

    for line in watch_out.getvalue().splitlines():
        print(f"  {line}")
    print(
        f"  [OK] Unrelated & partial-prefix discs rejected; 2 exact-match discs serialized E4+ and E7+ "
        f"in distinct attempt dirs with start_new_session=True.\n"
    )
    for w in spawned:
        w.finish(0)
    watcher.reap_finished()


def _demo_crash_recovery(root: Path) -> None:
    print("== 2. Crash-Recovery Publish Journal: Fault Between Publish & Manifest ==")
    ripper.clear_finalize_errors()
    stage = root / "stage"
    lib = root / "library"
    stage.mkdir()
    lib.mkdir()

    src1 = stage / "ep1.mkv"
    src2 = stage / "ep2.mkv"
    src1.write_bytes(b"SYNTHETIC-EPISODE-1-PAYLOAD")
    src2.write_bytes(b"SYNTHETIC-EPISODE-2-PAYLOAD")
    dst1 = lib / "Starlight Brigade - S01E04.mkv"
    dst2 = lib / "Starlight Brigade - S01E05.mkv"

    args = SimpleNamespace(
        state=None,
        verify=False,
        rerip_review=False,
        no_subocr=True,
        no_sound=True,
        overwrite=False,
        disc_receipts=str(root / "receipts.jsonl"),
    )
    item = SimpleNamespace(title="Starlight Brigade", type="movie")
    first_attempt = {
        "files": [str(dst1), str(dst2)],
        "move_jobs": [(src1, dst1, "S01E04"), (src2, dst2, "S01E05")],
        "staging": str(stage),
    }

    with mock.patch.object(
        ripper, "_save_committed_manifest", side_effect=OSError("injected crash before manifest write")
    ), mock.patch.object(ripper, "beep"), \
         mock.patch.object(ripper, "append_log"), \
         mock.patch.object(ripper, "run_subocr_postrip"), \
         contextlib.redirect_stdout(io.StringIO()):
        ok1 = ripper._run_finalize(args, first_attempt, item, 1)

    assert ok1 is False
    assert dst1.is_file() and src1.is_file(), "dst1 published; src1 retained until commit manifest durable"
    assert not (stage / ripper._COMMITTED_MANIFEST_NAME).exists()
    assert (stage / ripper._PUBLISH_INTENT_NAME).is_file()
    print("  Injected crash after atomic publish of S01E04 and before .burndvd-committed.json write:")
    print(f"    dst1 exists={dst1.is_file()}, manifest exists=False, intent journal exists=True, src1 retained={src1.is_file()}")

    replay = {
        "files": [str(dst1), str(dst2)],
        "move_jobs": [(src1, dst1, "S01E04"), (src2, dst2, "S01E05")],
        "staging": str(stage),
    }
    events: list[dict] = []
    with mock.patch.object(ripper, "beep"), \
         mock.patch.object(ripper, "append_log"), \
         mock.patch.object(ripper, "run_subocr_postrip"), \
         contextlib.redirect_stdout(io.StringIO()):
        ok2 = ripper._run_finalize(args, replay, item, 1, on_file_committed=events.append)

    assert ok2 is True
    assert dst1.read_bytes() == b"SYNTHETIC-EPISODE-1-PAYLOAD"
    assert dst2.read_bytes() == b"SYNTHETIC-EPISODE-2-PAYLOAD"
    assert not stage.exists()
    assert events[0].get("reconciled_from_intent") is True
    print("  Fresh replay succeeded: S01E04 reconciled via SHA-256 intent, S01E05 published, staging cleaned.")
    print("  [OK] Zero self-collision, zero overwrite, zero duplicate episodes.\n")


def _demo_checkrip_dashboard(root: Path) -> None:
    print("== 3. Multi-Drive checkrip: Duplicate-Label Isolation & Stable Versioned JSON ==")
    fake_rips = [
        {
            "pid": 42001,
            "state": str(root / "s6.json"),
            "queue": None,
            "device": "disc:1",
            "drive_device": "/dev/disk6",
            "drive_serial": "DRVSER00002",
            "title": "Starlight Brigade",
            "season": 1,
            "volume": "STARLIGHT_BRIGADE_S01_D1",
            "episode_range": "S01E04-E06",
            "log_path": None,
            "staging": str(root / "stg6"),
        }
    ]
    fake_drives = [
        {
            "slot": 1,
            "model": "HL-DT-ST BD-RE BU40N 1.03",
            "media_type": "BD-ROM",
            "device": "/dev/disk5",
            "volume": "STARLIGHT_BRIGADE_S01_D1",
        },
        {
            "slot": 2,
            "model": "HL-DT-ST BD-RE BU40N 1.03",
            "media_type": "BD-ROM",
            "device": "/dev/disk6",
            "volume": "STARLIGHT_BRIGADE_S01_D1",
        },
    ]
    with mock.patch.object(checkrip, "active_rips", return_value=fake_rips), \
         mock.patch.object(checkrip, "active_watchers", return_value=[]), \
         mock.patch.object(checkrip, "connected_optical_drives", return_value=fake_drives), \
         mock.patch.object(checkrip, "_active_claim_devices", return_value={}), \
         mock.patch.object(checkrip, "measure_rip_speeds", return_value={str(root / "stg6"): 19.8}):
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["checkrip", "--json"]), contextlib.redirect_stdout(buf):
            rc = checkrip.main()

    assert rc == 0
    payload = json.loads(buf.getvalue())
    by_dev = {d["device"]: d for d in payload["drives"]}
    assert by_dev["/dev/disk5"]["status"] == "READY" and by_dev["/dev/disk5"]["pid"] is None
    assert by_dev["/dev/disk6"]["status"] == "RIPPING" and by_dev["/dev/disk6"]["pid"] == 42001
    assert payload["schema_version"] == 1
    assert payload["pid"] == 42001
    assert payload["speed_mbps"] == 19.8
    print(
        f"  schema_version={payload['schema_version']} top-level pid={payload['pid']} "
        f"speed_mbps={payload['speed_mbps']}"
    )
    for d in payload["drives"]:
        print(f"    Drive {d['slot']} {d['device']}: status={d['status']} pid={d['pid']} volume={d['volume']}")
    print("  [OK] Duplicate volume label did not steal /dev/disk6's rip; legacy flat JSON keys preserved.\n")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="burndvd-synthetic-demo-") as tmp:
        root = Path(tmp)
        _demo_watch_mode(root)
        _demo_crash_recovery(root)
        _demo_checkrip_dashboard(root)
    print("Synthetic demo completed successfully (zero optical hardware touched).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
