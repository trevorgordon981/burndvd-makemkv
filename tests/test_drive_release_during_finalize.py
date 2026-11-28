"""Tests verifying optical drives are released immediately after staging finishes.

Ensures `ffmpeg --verify` and NAS transfer in background finalizers never hold
optical drive locks, wrapper claims, or busy-slot markers, and never double-eject
a drive after releasing it for the next disc.
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
import unittest
from pathlib import Path
from unittest import mock

from burndvd import drive, ripper
from burndvd.cli import checkrip, ripqueue

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "bin" / "burndvd"


def _write_exec(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _external_slot_locked(
    device_spec: str,
    lock_dir: Path,
    *,
    drive_device: str | None = None,
    drive_serial: str | None = None,
) -> bool:
    code = f"""
import sys
from pathlib import Path
from burndvd import drive
try:
    with drive.disc_slot_lock(
        {device_spec!r},
        lock_dir=Path({str(lock_dir)!r}),
        drive_device={drive_device!r},
        drive_serial={drive_serial!r},
    ):
        sys.exit(0)
except drive.DriveSlotConflictError:
    sys.exit(42)
"""
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        return False
    if r.returncode == 42:
        return True
    raise AssertionError(f"unexpected rc={r.returncode}: stderr={r.stderr}")


class TestDriveReleaseDuringBackgroundFinalize(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_drive_lock_and_claim_released_before_verify_and_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nas = root / "nas"
            nas.mkdir()
            lock_dir = root / "locks"
            claim_dir = root / "claims" / "serial-DRVSER00001.claim"
            claim_dir.mkdir(parents=True)
            state_path = root / "state.json"
            (claim_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
            (claim_dir / "state").write_text(str(state_path), encoding="utf-8")
            (claim_dir / "dev").write_text("/dev/disk5", encoding="utf-8")
            (claim_dir / "serial").write_text("DRVSER00001", encoding="utf-8")

            qpath = root / "queue.csv"
            qpath.write_text(
                f"title,type,discs,target_root,format\nFast Release Movie (2024),movie,1,{nas},4K\n",
                encoding="utf-8",
            )
            staging = root / "staging"
            staging.mkdir()
            src = staging / "movie.mkv"
            src.write_bytes(b"valid-4k-stream")
            dst = nas / "Fast Release Movie (2024).mkv"
            info = {"cinfo": {2: "FAST_RELEASE"}, "titles": {0: {9: "2:00:00", 11: "50000"}}}

            observed: dict[str, object] = {}
            events: list[str] = []
            verify_entered = threading.Event()
            allow_verify_finish = threading.Event()

            def fake_rip(*_a, **_kw):
                # During optical read, the slot + physical drive locks MUST be held.
                observed["locked_during_rip"] = _external_slot_locked(
                    "disc:1", lock_dir, drive_device="/dev/disk5", drive_serial="DRVSER00001"
                )
                return (
                    True,
                    {
                        "elapsed_s": 5.0,
                        "files": [str(dst)],
                        "move_jobs": [(src, dst, "moving")],
                        "staging": str(staging),
                    },
                )

            def tracked_verify(_paths):
                events.append("verify")
                # Wait briefly for main thread to finish _release_optical_drive()
                # right after start_background_finalize().
                for _ in range(50):
                    if Path(f"{state_path}.drive_released").is_file():
                        break
                    threading.Event().wait(0.02)
                observed["marker_during_verify"] = Path(f"{state_path}.drive_released").is_file()
                observed["claim_exists_during_verify"] = claim_dir.exists()
                observed["locked_during_verify"] = _external_slot_locked(
                    "disc:1", lock_dir, drive_device="/dev/disk5", drive_serial="DRVSER00001"
                )
                verify_entered.set()
                allow_verify_finish.wait(timeout=5)
                return True, ""

            real_move = ripper.move_with_progress

            def tracked_move(s, d, **kw):
                events.append("move")
                observed["locked_during_move"] = _external_slot_locked(
                    "disc:1", lock_dir, drive_device="/dev/disk5", drive_serial="DRVSER00001"
                )
                return real_move(s, d, **kw)

            argv = [
                "ripqueue.py",
                "--queue", str(qpath),
                "--state", str(state_path),
                "--makemkvcon", sys.executable,
                "--device", "disc:1",
                "--non-interactive",
                "--verify",
                "--no-subocr",
                "--disc-receipts", str(root / "receipts.jsonl"),
            ]
            env_patch = {
                "BURNDVD_LOCK_DIR": str(lock_dir),
                "BURNDVD_DRIVE_DEVICE": "/dev/disk5",
                "BURNDVD_DRIVE_SERIAL": "DRVSER00001",
                "BURNDVD_DRIVE_CLAIM": str(claim_dir),
            }
            resolved = drive.ResolvedMakemkvDevice("disc:1", device_node="/dev/disk5", serial="DRVSER00001")
            allow_verify_finish.set()

            with mock.patch.dict(os.environ, env_patch), \
                 mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(ripqueue, "resolve_makemkv_device", return_value=resolved), \
                 mock.patch.object(ripqueue, "_drive_idx_from_args", return_value=None), \
                 mock.patch.object(ripqueue, "check_key_expiry"), \
                 mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info)), \
                 mock.patch.object(ripqueue, "rip", side_effect=fake_rip), \
                 mock.patch.object(ripper, "verify", side_effect=tracked_verify), \
                 mock.patch.object(ripper, "move_with_progress", side_effect=tracked_move), \
                 mock.patch.object(ripper, "_await_dir_writable", return_value=nas), \
                 mock.patch.object(ripper, "log_final_durations"), \
                 mock.patch.object(ripqueue, "beep"), \
                 mock.patch.object(ripqueue, "eject", side_effect=lambda _a: events.append("eject")), \
                 contextlib.redirect_stdout(io.StringIO()):
                ripqueue.main()

            self.assertTrue(observed["locked_during_rip"])
            self.assertTrue(observed["marker_during_verify"])
            self.assertFalse(observed["claim_exists_during_verify"])
            self.assertFalse(observed["locked_during_verify"])
            self.assertFalse(observed["locked_during_move"])
            self.assertEqual(events.count("eject"), 1, f"must eject exactly once: {events}")
            self.assertTrue(dst.is_file())

    def test_no_eject_still_releases_drive_lock_and_writes_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nas = root / "nas"
            nas.mkdir()
            lock_dir = root / "locks"
            state_path = root / "state.json"
            qpath = root / "queue.csv"
            qpath.write_text(
                f"title,type,discs,target_root,format\nNo Eject Movie (2024),movie,1,{nas},4K\n",
                encoding="utf-8",
            )
            staging = root / "staging"
            staging.mkdir()
            src = staging / "movie.mkv"
            src.write_bytes(b"ok")
            dst = nas / "No Eject Movie (2024).mkv"
            info = {"cinfo": {2: "NO_EJECT"}, "titles": {0: {9: "1:40:00", 11: "1000"}}}
            ejects: list[int] = []
            locked_during_verify: list[bool] = []

            def tracked_verify(_paths):
                for _ in range(50):
                    if Path(f"{state_path}.drive_released").is_file():
                        break
                    threading.Event().wait(0.02)
                locked_during_verify.append(
                    _external_slot_locked("disc:0", lock_dir, drive_device="/dev/disk5")
                )
                return True, ""

            argv = [
                "ripqueue.py",
                "--queue", str(qpath),
                "--state", str(state_path),
                "--makemkvcon", sys.executable,
                "--device", "disc:0",
                "--non-interactive",
                "--verify",
                "--no-eject",
                "--no-subocr",
                "--disc-receipts", str(root / "receipts.jsonl"),
            ]
            resolved = drive.ResolvedMakemkvDevice("disc:0", device_node="/dev/disk5", serial="DRVSER00001")
            with mock.patch.dict(os.environ, {"BURNDVD_LOCK_DIR": str(lock_dir), "BURNDVD_DRIVE_DEVICE": "/dev/disk5"}), \
                 mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(ripqueue, "resolve_makemkv_device", return_value=resolved), \
                 mock.patch.object(ripqueue, "_drive_idx_from_args", return_value=None), \
                 mock.patch.object(ripqueue, "check_key_expiry"), \
                 mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info)), \
                 mock.patch.object(ripqueue, "rip", return_value=(True, {"elapsed_s": 2.0, "files": [str(dst)], "move_jobs": [(src, dst, "moving")], "staging": str(staging)})), \
                 mock.patch.object(ripper, "verify", side_effect=tracked_verify), \
                 mock.patch.object(ripper, "_await_dir_writable", return_value=nas), \
                 mock.patch.object(ripper, "log_final_durations"), \
                 mock.patch.object(ripqueue, "beep"), \
                 mock.patch.object(ripqueue, "eject", side_effect=lambda _a: ejects.append(1)), \
                 contextlib.redirect_stdout(io.StringIO()):
                ripqueue.main()

            self.assertEqual(ejects, [])
            self.assertEqual(locked_during_verify, [False])
            marker = json.loads(Path(f"{state_path}.drive_released").read_text(encoding="utf-8"))
            self.assertFalse(marker["ejected"])

    def test_active_busy_disc_claims_ignores_drive_released_processes_and_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claim_dir = root / "drive-claims"
            claim_dir.mkdir()
            released_state = root / "released-state.json"
            active_state = root / "active-state.json"
            Path(f"{released_state}.drive_released").write_text('{"ejected": true}\n', encoding="utf-8")

            c_rel = claim_dir / "serial-MO8Q4283637.claim"
            c_rel.mkdir()
            (c_rel / "pid").write_text(str(os.getpid()), encoding="utf-8")
            (c_rel / "state").write_text(str(released_state), encoding="utf-8")
            (c_rel / "dev").write_text("/dev/disk5", encoding="utf-8")
            (c_rel / "disc").write_text("0", encoding="utf-8")

            c_act = claim_dir / "serial-MOBP8JD4640.claim"
            c_act.mkdir()
            (c_act / "pid").write_text(str(os.getpid()), encoding="utf-8")
            (c_act / "state").write_text(str(active_state), encoding="utf-8")
            (c_act / "dev").write_text("/dev/disk7", encoding="utf-8")
            (c_act / "disc").write_text("2", encoding="utf-8")

            fake_procs = [
                (8001, f"python3 /u/ripqueue.py --state {released_state} --device disc:0"),
                (8002, f"python3 /u/ripqueue.py --state {active_state} --device disc:2"),
            ]
            with mock.patch.object(drive, "_active_process_lines", return_value=fake_procs), \
                 mock.patch.object(drive, "_self_and_ancestor_pids", return_value=set()):
                busy = drive.active_busy_disc_claims(claim_dir=claim_dir)

            self.assertNotIn(0, busy)
            self.assertIn(2, busy)

    def test_wrapper_reuses_released_drive_and_skips_duplicate_eject_on_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            media = root / "media"
            (media / "Movies").mkdir(parents=True)
            logs = root / "logs"
            logs.mkdir()
            claim_dir = root / ".cache" / "burndvd" / "drive-claims"
            claim_dir.mkdir(parents=True)

            # Simulate an already-released Iron Man 3 run still verifying in background on disk5 / disc:0
            old_state = root / "burndvd-state.old.json"
            Path(f"{old_state}.drive_released").write_text('{"ejected": true}\n', encoding="utf-8")
            old_claim = claim_dir / "dev-disk5.claim"
            old_claim.mkdir()
            (old_claim / "pid").write_text(str(os.getpid()), encoding="utf-8")
            (old_claim / "state").write_text(str(old_state), encoding="utf-8")
            (old_claim / "dev").write_text("/dev/disk5", encoding="utf-8")
            (old_claim / "disc").write_text("0", encoding="utf-8")

            capture = root / "captured.json"
            eject_log = root / "eject.log"

            _write_exec(
                fake_bin / "makemkvcon",
                '#!/bin/bash\necho \'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 MO8Q4283637","NEXT_DISC_2025","/dev/rdisk5"\'\n',
            )
            _write_exec(
                fake_bin / "drutil",
                """#!/bin/bash
if [ "$1" = "list" ]; then
    echo "  1  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported"
    exit 0
fi
if [ "$1" = "status" ]; then
    echo "  Type: BD-ROM  Name: /dev/disk5"
    echo "  Space Used: 25.00GB"
    exit 0
fi
exit 0
""",
            )
            _write_exec(
                fake_bin / "diskutil",
                '#!/bin/bash\nif [ "$1" = "info" ]; then echo "   Volume Name:               NEXT_DISC_2025"; fi\nexit 0\n',
            )
            _write_exec(
                fake_bin / "ejectdisc",
                f'#!/bin/bash\necho "EJECT:$*" >> "{eject_log}"\n',
            )
            _write_exec(
                fake_bin / "ps",
                f'#!/bin/bash\necho "77777 python3 /u/ripqueue.py --state {old_state} --device disc:0"\n',
            )
            _write_exec(
                fake_bin / "pgrep",
                f'#!/bin/bash\nif printf "%s\\n" "$*" | grep -q "{old_state.name}"; then echo "77777"; exit 0; fi\nexit 1\n',
            )
            _write_exec(
                fake_bin / "caffeinate",
                '#!/bin/bash\nwhile [[ "${1:-}" == -* ]]; do\n  if [ "$1" = "-t" ]; then shift 2; else shift; fi\ndone\n[ "$#" -gt 0 ] && exec "$@"\nexit 0\n',
            )
            _write_exec(
                fake_bin / "ripqueue.py",
                f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
if "--validate-queue" in sys.argv:
    sys.exit(0)
if "--scan" in sys.argv:
    print("[]")
    sys.exit(0)
state = sys.argv[sys.argv.index("--state") + 1]
Path({str(capture)!r}).write_text(json.dumps({{
    "argv": sys.argv[1:],
    "BURNDVD_DRIVE_DEVICE": os.environ.get("BURNDVD_DRIVE_DEVICE"),
    "BURNDVD_DRIVE_CLAIM": os.environ.get("BURNDVD_DRIVE_CLAIM"),
}}))
Path(f"{{state}}.drive_released").write_text('{{"ejected": true}}\\n')
Path(f"{{state}}.exit").write_text('{{"exit_code": 0, "reason": "queue_complete"}}')
""",
            )

            env = {
                **os.environ,
                "HOME": str(root),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "PYTHONPATH": str(REPO_ROOT / "src"),
                "BURNDVD_MAKEMKVCON": str(fake_bin / "makemkvcon"),
                "BURNDVD_RIPQUEUE_PY": str(fake_bin / "ripqueue.py"),
                "BURNDVD_MEDIA_ROOT": str(media),
                "BURNDVD_NFS_MEDIA_ROOT": str(media),
                "BURNDVD_LOG_DIR": str(logs),
            }
            res = subprocess.run(
                [str(WRAPPER)],
                input="Next Disc (2025)\nmovie\nBD\n",
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(res.returncode, 0, f"stdout={res.stdout}\nstderr={res.stderr}")
            cap = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(cap["BURNDVD_DRIVE_DEVICE"], "/dev/disk5")
            self.assertTrue(cap["BURNDVD_DRIVE_CLAIM"].endswith("serial-MO8Q4283637.claim"))
            # Because ripqueue wrote <state>.drive_released, wrapper must NOT double-eject!
            self.assertFalse(eject_log.exists(), "wrapper double-ejected a drive already released by ripqueue")

    def test_checkrip_separates_background_finalizing_from_ready_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st1 = root / "s1.json"
            Path(f"{st1}.drive_released").write_text('{"ejected": true}\n', encoding="utf-8")
            log1 = root / "ironman3.log"
            log1.write_text(
                "[#############################-] 100.0%  el 45m01s  ETA 0m00s  15:53:50  Saving all titles to MKV files\n"
                "Staged in 45m01s; finalizing to NAS...\n"
                "  Optical drive ejected & unlocked — ready for the next disc while verify/NAS finishes in background.\n"
                "Waiting for 1 background NAS transfer(s) to finish before exit...\n",
                encoding="utf-8",
            )
            fake_rips = [
                {
                    "pid": 24671,
                    "state": str(st1),
                    "queue": None,
                    "device": "disc:1",
                    "drive_device": "/dev/disk5",
                    "drive_serial": "MO8Q4283637",
                    "drive_released": True,
                    "verifying_child": True,
                    "title": "Iron Man 3 (2013)",
                    "season": None,
                    "volume": "IRON_MAN_3",
                    "episode_range": None,
                    "log_path": str(log1),
                    "staging": str(root / "staging"),
                }
            ]
            fake_drives = [
                {
                    "slot": 1,
                    "model": "HL-DT-ST BD-RE BU40N 1.03",
                    "media_type": "DVD-ROM",
                    "device": "/dev/disk5",
                    "volume": "NarutoShippuden_BS2_D6",
                },
            ]
            with mock.patch.object(checkrip, "active_rips", return_value=fake_rips), \
                 mock.patch.object(checkrip, "active_watchers", return_value=[]), \
                 mock.patch.object(checkrip, "connected_optical_drives", return_value=fake_drives), \
                 mock.patch.object(checkrip, "_active_claim_devices", return_value={}), \
                 mock.patch.object(checkrip, "measure_rip_speeds", return_value={}):
                buf = io.StringIO()
                with mock.patch.object(sys, "argv", ["checkrip"]), contextlib.redirect_stdout(buf):
                    rc = checkrip.main()
                self.assertEqual(rc, 0)
                out = buf.getvalue()
                self.assertIn("Optical Drives (0/1 ripping)", out)
                self.assertIn("Drive 1  /dev/disk5  READY      [NarutoShippuden_BS2_D6]", out)
                self.assertIn("Background Finalizing (1 — drive free)", out)
                self.assertIn("VERIFYING   Iron Man 3 (2013) [IRON_MAN_3]", out)


if __name__ == "__main__":
    unittest.main()
