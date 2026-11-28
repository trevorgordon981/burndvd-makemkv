"""Regression tests for multi-drive reverse BSD-to-MakeMKV slot mapping,
busy disc:K exclusion, and probe_disc MakeMKV diagnostic surfacing.
100% mocked, zero hardware I/O.
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
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from burndvd import drive, probe, ripper

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "bin" / "burndvd"


def _write_exec(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _FakePopen:
    def __init__(self, lines: list[str], returncode: int = 0):
        self._lines = [ln if ln.endswith("\n") else ln + "\n" for ln in lines]
        self.stdout = io.StringIO("".join(self._lines))
        self.returncode = returncode
        self.killed = False

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        self.killed = True


class _StallingStream:
    """Stream that yields initial lines and then blocks until released."""

    def __init__(self, initial_lines: list[str]):
        import threading

        self._initial = [ln if ln.endswith("\n") else ln + "\n" for ln in initial_lines]
        self._release = threading.Event()

    def release(self):
        self._release.set()

    def __iter__(self):
        for ln in self._initial:
            yield ln
        self._release.wait(timeout=5.0)


class _StallingPopen:
    def __init__(self, initial_lines: list[str]):
        self.stdout = _StallingStream(initial_lines)
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.release()

    def wait(self, timeout=None):
        return self.returncode if self.returncode is not None else -9


def _two_drive_drutil_script() -> str:
    return """#!/bin/bash
if [ "$1" = "list" ]; then
    echo "  1  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported"
    echo "  2  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported"
    exit 0
fi
if [ "$1" = "status" ] && [ "$3" = "1" ]; then
    echo "  Type: BD-ROM  Name: /dev/disk5"
    echo "  Space Used: 32.00GB"
    exit 0
fi
if [ "$1" = "status" ] && [ "$3" = "2" ]; then
    echo "  Type: DVD-ROM  Name: /dev/disk6"
    echo "  Space Used: 7.20GB"
    exit 0
fi
exit 1
"""


def _two_drive_diskutil_script() -> str:
    return """#!/bin/bash
if [ "$1" = "info" ] && [ "$2" = "/dev/disk5" ]; then
    echo "   Volume Name:               MOVIE_ONE_2013"
    exit 0
fi
if [ "$1" = "info" ] && [ "$2" = "/dev/disk6" ]; then
    echo "   Volume Name:               SHOW_VOL1_D1"
    exit 0
fi
exit 1
"""


def _build_wrapper_env(
    root: Path,
    *,
    makemkv_script: str,
    drutil_script: str,
    diskutil_script: str,
    ps_script: str = "#!/bin/bash\nexit 0\n",
    pgrep_script: str = "#!/bin/bash\nexit 1\n",
    lsof_script: str = "#!/bin/bash\nexit 1\n",
) -> tuple[dict[str, str], Path]:
    fake_bin = root / "bin"
    fake_bin.mkdir(exist_ok=True)
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    media = root / "media"
    (media / "Movies").mkdir(parents=True, exist_ok=True)
    (media / "TV Shows").mkdir(parents=True, exist_ok=True)
    capture_file = root / "captured.json"

    _write_exec(fake_bin / "makemkvcon", makemkv_script)
    _write_exec(fake_bin / "drutil", drutil_script)
    _write_exec(fake_bin / "diskutil", diskutil_script)
    _write_exec(fake_bin / "ps", ps_script)
    _write_exec(fake_bin / "pgrep", pgrep_script)
    _write_exec(fake_bin / "lsof", lsof_script)
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
capture.write_text(json.dumps(rec), encoding="utf-8")
if state_path is not None:
    Path(str(state_path) + ".exit").write_text(
        json.dumps({{"exit_code": 0, "reason": "ok"}}), encoding="utf-8"
    )
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


class TestReverseSlotAndBusyExclusion(unittest.TestCase):
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

    def test_resolve_makemkv_device_reverse_bsd_order_binds_disk6_to_disc0(self):
        """Reverse BSD-to-MakeMKV ordering (/dev/disk5->disc:1, /dev/disk6->disc:0)
        must bind /dev/disk6 to disc:0 and /dev/disk5 to disc:1."""
        reverse_drv = "\n".join(
            [
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","SHOW_VOL1_D1","/dev/rdisk6"',
                'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","MOVIE_ONE_2013","/dev/rdisk5"',
            ]
        )
        args_disk6 = SimpleNamespace(
            device="disc:1",
            drive_device="/dev/disk6",
            drive_serial="DRVSER00002",
            makemkvcon="/fake/makemkvcon",
        )
        args_disk5 = SimpleNamespace(
            device="disc:0",
            drive_device="/dev/disk5",
            drive_serial="DRVSER00001",
            makemkvcon="/fake/makemkvcon",
        )
        with mock.patch.object(
            drive.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=reverse_drv, stderr=""),
        ):
            self.assertEqual(drive.resolve_makemkv_device(args_disk6), "disc:0")
            self.assertEqual(drive.resolve_makemkv_device(args_disk5), "disc:1")

    def test_resolve_makemkv_device_excludes_busy_disc1_on_fallback_for_disk6(self):
        """When makemkvcon info list fails while disc:1 is actively claimed by /dev/disk5,
        resolve_makemkv_device MUST NEVER hand busy disc:1 to /dev/disk6."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claim_dir = root / ".cache" / "burndvd" / "drive-claims"
            busy_claim = claim_dir / "serial-DRVSER00001.claim"
            busy_claim.mkdir(parents=True)
            # Live PID belonging to another process (spawned sleeper so pid != os.getpid()).
            sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
            try:
                (busy_claim / "pid").write_text(str(sleeper.pid), encoding="utf-8")
                (busy_claim / "dev").write_text("/dev/disk5", encoding="utf-8")
                (busy_claim / "disc").write_text("1", encoding="utf-8")
                (busy_claim / "state").write_text(str(root / "thor.state.json"), encoding="utf-8")

                def fake_run(cmd, *a, **kw):
                    if cmd[0] == "/fake/makemkvcon":
                        raise subprocess.TimeoutExpired(cmd, 15)
                    if cmd[:2] == ["ps", "-Ao"]:
                        return SimpleNamespace(
                            returncode=0,
                            stdout=(
                                f"  {sleeper.pid} python3 /u/ripqueue.py --state "
                                f"{root / 'thor.state.json'} --device disc:1\n"
                                "  55555 /Applications/MakeMKV.app/Contents/MacOS/makemkvcon -r mkv disc:1 0 /tmp/st\n"
                            ),
                            stderr="",
                        )
                    if cmd[:2] == [drive.DRUTIL_BIN, "list"]:
                        return SimpleNamespace(
                            returncode=0,
                            stdout=(
                                "  1  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported\n"
                                "  2  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported\n"
                            ),
                            stderr="",
                        )
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                args_disk6 = SimpleNamespace(
                    device="disc:1",
                    drive_device="/dev/disk6",
                    drive_serial="",
                    state=str(root / "naruto.state.json"),
                    makemkvcon="/fake/makemkvcon",
                )
                with mock.patch.dict(os.environ, {"HOME": str(root)}), \
                     mock.patch.object(drive.subprocess, "run", side_effect=fake_run):
                    resolved = drive.resolve_makemkv_device(args_disk6)
                self.assertEqual(resolved, "disc:0")

                # Own claim on /dev/disk5 (matching exclude_device_node) must NOT conflict with itself.
                args_disk5 = SimpleNamespace(
                    device="disc:1",
                    drive_device="/dev/disk5",
                    drive_serial="DRVSER00001",
                    state=str(root / "thor.state.json"),
                    makemkvcon="/fake/makemkvcon",
                )
                with mock.patch.dict(os.environ, {"HOME": str(root)}), \
                     mock.patch.object(drive.subprocess, "run", side_effect=fake_run):
                    self.assertEqual(drive.resolve_makemkv_device(args_disk5), "disc:1")
            finally:
                sleeper.terminate()
                sleeper.wait(timeout=5)

    def test_resolve_makemkv_device_refuses_when_all_slots_busy_and_probe_never_spawns(self):
        """If fallback slot disc:1 is busy and NO free slot exists, raise DriveSlotConflictError
        and ensure probe_disc() + rip() refuse cleanly without launching makemkvcon."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "state.json"

            def fake_run(cmd, *a, **kw):
                if cmd[0] == "/fake/makemkvcon":
                    return SimpleNamespace(returncode=1, stdout="", stderr="busy")
                if cmd[:2] == ["ps", "-Ao"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=(
                            "  44444 /usr/bin/makemkvcon -r mkv disc:0 0 /tmp/a\n"
                            "  44445 /usr/bin/makemkvcon -r mkv disc:1 0 /tmp/b\n"
                        ),
                        stderr="",
                    )
                if cmd[:2] == [drive.DRUTIL_BIN, "list"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=(
                            "  1  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported\n"
                            "  2  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported\n"
                        ),
                        stderr="",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            args = SimpleNamespace(
                device="disc:1",
                drive_device="/dev/disk6",
                state=str(state_path),
                makemkvcon="/fake/makemkvcon",
            )
            with mock.patch.dict(os.environ, {"HOME": str(root)}), \
                 mock.patch.object(drive.subprocess, "run", side_effect=fake_run):
                with self.assertRaises(drive.DriveSlotConflictError):
                    drive.resolve_makemkv_device(args)

            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"HOME": str(root)}), \
                 mock.patch.object(drive.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(probe, "_optical_mounts", return_value=[]), \
                 mock.patch.object(probe, "_drive_idx_from_args", return_value=1), \
                 mock.patch.object(probe.subprocess, "Popen") as popen_mock, \
                 contextlib.redirect_stdout(buf):
                res = probe.probe_disc(args)

            self.assertIsNone(res)
            popen_mock.assert_not_called()
            self.assertIn("probe refused:", buf.getvalue())
            self.assertIn("disc:1", buf.getvalue())
            self.assertIn("PROBE_REFUSED", state_path.with_suffix(".log").read_text(encoding="utf-8"))

    def test_wrapper_reverse_bsd_order_with_busy_disk5_binds_disk6_to_disc0(self):
        """Wrapper with MakeMKV DRV:0=/dev/rdisk6, DRV:1=/dev/rdisk5 while /dev/disk5 (disc:1)
        is actively ripping binds /dev/disk6 to --device disc:0 and records dev/disc in claim."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claim_dir = root / ".cache" / "burndvd" / "drive-claims"
            busy_claim = claim_dir / "serial-DRVSER00001.claim"
            busy_claim.mkdir(parents=True)
            sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
            try:
                (busy_claim / "pid").write_text(str(sleeper.pid), encoding="utf-8")
                (busy_claim / "dev").write_text("/dev/disk5", encoding="utf-8")
                (busy_claim / "disc").write_text("1", encoding="utf-8")
                (busy_claim / "state").write_text(str(root / "other.state.json"), encoding="utf-8")

                mkv_sh = """#!/bin/bash
cat <<'EOF'
DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","SHOW_VOL1_D1","/dev/rdisk6"
DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","MOVIE_ONE_2013","/dev/rdisk5"
EOF
exit 0
"""
                ps_sh = f"""#!/bin/bash
if [ "$1" = "-Ao" ]; then
    echo "  {sleeper.pid} python3 /u/ripqueue.py --state {root / 'other.state.json'} --device disc:1"
    echo "  55555 makemkvcon -r mkv disc:1 0 /tmp/stage"
    exit 0
fi
exit 0
"""
                env, capture = _build_wrapper_env(
                    root,
                    makemkv_script=mkv_sh,
                    drutil_script=_two_drive_drutil_script(),
                    diskutil_script=_two_drive_diskutil_script(),
                    ps_script=ps_sh,
                )
                res = subprocess.run(
                    [str(WRAPPER)],
                    input="Sample Show\ntv-season\nDVD\n1\n1\n",
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(res.returncode, 0, f"stdout={res.stdout}\nstderr={res.stderr}")
                cap = json.loads(capture.read_text(encoding="utf-8"))
                self.assertEqual(cap["BURNDVD_DRIVE_DEVICE"], "/dev/disk6")
                self.assertEqual(cap["BURNDVD_DRIVE_SERIAL"], "DRVSER00002")
                self.assertEqual(cap["BURNDVD_DRUTIL_DRIVE"], "2")
                dev_idx = cap["argv"].index("--device") + 1
                self.assertEqual(cap["argv"][dev_idx], "disc:0")

                new_claim = claim_dir / "serial-DRVSER00002.claim"
                self.assertTrue(new_claim.is_dir())
                self.assertEqual((new_claim / "dev").read_text(encoding="utf-8").strip(), "/dev/disk6")
                self.assertEqual((new_claim / "disc").read_text(encoding="utf-8").strip(), "0")
            finally:
                sleeper.terminate()
                sleeper.wait(timeout=5)

    def test_wrapper_fallback_when_makemkv_list_fails_never_assigns_busy_disc1_to_disk6(self):
        """Exact live-bug repro: makemkvcon info list fails while /dev/disk5 is ripping disc:1.
        Old code sorted drutil_loaded [disk5, disk6] -> disk5=0, disk6=1 and handed busy disc:1
        to /dev/disk6! Fixed wrapper MUST exclude busy disc:1 and bind /dev/disk6 to disc:0."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claim_dir = root / ".cache" / "burndvd" / "drive-claims"
            busy_claim = claim_dir / "dev-disk5.claim"
            busy_claim.mkdir(parents=True)
            sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
            try:
                (busy_claim / "pid").write_text(str(sleeper.pid), encoding="utf-8")
                (busy_claim / "state").write_text(str(root / "thor.state.json"), encoding="utf-8")

                # makemkvcon info list fails/returns empty (e.g. timeout/lock while other drive rips).
                mkv_fail_sh = "#!/bin/bash\nexit 1\n"
                ps_sh = f"""#!/bin/bash
if [ "$1" = "-Ao" ]; then
    echo "  {sleeper.pid} python3 /u/ripqueue.py --state {root / 'thor.state.json'} --device disc:1"
    echo "  55555 makemkvcon -r mkv disc:1 0 /tmp/stage"
    exit 0
fi
exit 0
"""
                pgrep_sh = """#!/bin/bash
if printf '%s\\n' "$*" | grep -q 'makemkvcon'; then
    echo "55555"
    exit 0
fi
exit 1
"""
                lsof_sh = """#!/bin/bash
echo "makemkvco 55555 user  5r  CHR  1,12  /dev/rdisk5"
exit 0
"""
                env, capture = _build_wrapper_env(
                    root,
                    makemkv_script=mkv_fail_sh,
                    drutil_script=_two_drive_drutil_script(),
                    diskutil_script=_two_drive_diskutil_script(),
                    ps_script=ps_sh,
                    pgrep_script=pgrep_sh,
                    lsof_script=lsof_sh,
                )
                res = subprocess.run(
                    [str(WRAPPER)],
                    input="Sample Show\ntv-season\nDVD\n1\n1\n",
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(res.returncode, 0, f"stdout={res.stdout}\nstderr={res.stderr}")
                cap = json.loads(capture.read_text(encoding="utf-8"))
                self.assertEqual(cap["BURNDVD_DRIVE_DEVICE"], "/dev/disk6")
                self.assertEqual(cap["BURNDVD_DRUTIL_DRIVE"], "2")
                dev_idx = cap["argv"].index("--device") + 1
                self.assertEqual(
                    cap["argv"][dev_idx],
                    "disc:0",
                    f"must NEVER assign busy disc:1 to /dev/disk6: {cap}",
                )
            finally:
                sleeper.terminate()
                sleeper.wait(timeout=5)

    def test_wrapper_fallback_refuses_when_all_slots_busy(self):
        """If makemkvcon info list fails and both disc:0 and disc:1 are actively busy,
        wrapper MUST exit non-zero and never invoke ripqueue."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mkv_fail_sh = "#!/bin/bash\nexit 1\n"
            ps_sh = """#!/bin/bash
if [ "$1" = "-Ao" ]; then
    echo "  55555 makemkvcon -r mkv disc:0 0 /tmp/stage0"
    echo "  55556 makemkvcon -r mkv disc:1 0 /tmp/stage1"
    exit 0
fi
exit 0
"""
            env, capture = _build_wrapper_env(
                root,
                makemkv_script=mkv_fail_sh,
                drutil_script=_two_drive_drutil_script(),
                diskutil_script=_two_drive_diskutil_script(),
                ps_script=ps_sh,
            )
            res = subprocess.run(
                [str(WRAPPER)],
                input="",
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertFalse(capture.exists(), "ripqueue must not be launched when all slots are busy")
            self.assertIn("refusing unsafe fallback", res.stderr)

    def test_wrapper_3drive_fallback_skips_empty_disc1_and_finds_full_disc2(self):
        """Exact live 3-drive repro: disc:0 (/dev/disk6, drutil 3) is ripping BS4_D11,
        disc:1 (drutil 2) is EMPTY, disc:2 (/dev/disk5, drutil 1) has BS4_D12.
        When makemkvcon info list fails, wrapper probes free slots [1, 2] with --noscan,
        skips empty disc:1 (MSG:5010), finds full disc:2 (MSG:3007), and launches on disc:2."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claim_dir = root / ".cache" / "burndvd" / "drive-claims"
            busy_claim = claim_dir / "serial-MOEQ41G4438.claim"
            busy_claim.mkdir(parents=True)
            sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
            try:
                (busy_claim / "pid").write_text(str(sleeper.pid), encoding="utf-8")
                (busy_claim / "dev").write_text("/dev/disk6", encoding="utf-8")
                (busy_claim / "disc").write_text("0", encoding="utf-8")
                (busy_claim / "state").write_text(str(root / "d11.state.json"), encoding="utf-8")

                drutil_3 = """#!/bin/bash
if [ "$1" = "list" ]; then
    echo "   Vendor   Product           Rev   Bus       SupportLevel"
    echo "1  HL-DT-ST BD-RE BU40N       1.03  USB       Unsupported"
    echo "2  HL-DT-ST BD-RE BU40N       1.03  USB       Unsupported"
    echo "3  HL-DT-ST BD-RE BU40N       1.03  USB       Unsupported"
    exit 0
fi
if [ "$1" = "status" ] && [ "$2" = "-drive" ]; then
    case "$3" in
        1) echo "Type: DVD-ROM   Name: /dev/disk5" ;;
        2) echo "Type: No Media Inserted" ;;
        3) echo "Type: DVD-ROM   Name: /dev/disk6" ;;
    esac
    exit 0
fi
exit 0
"""
                diskutil_3 = """#!/bin/bash
case "$2" in
    /dev/disk5) echo "   Volume Name:               NarutoShippuden_BS4_D12" ;;
    /dev/disk6) echo "   Volume Name:               NarutoShippuden_BS4_D11" ;;
esac
exit 0
"""
                # info list fails (simulating timeout while disc:0 rips);
                # --noscan info disc:1 returns empty (MSG:5010);
                # --noscan info disc:2 returns loaded (DRV:2 + MSG:3007).
                mkv_3 = """#!/bin/bash
case "$*" in
    *"info list"*)
        exit 1
        ;;
    *"info disc:1"*)
        echo 'DRV:0,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 MOEQ41G4438","",""'
        echo 'DRV:1,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 MOBP8JD4640","",""'
        echo 'DRV:2,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 MO8Q4283637","",""'
        echo 'MSG:5010,0,0,"Failed to open disc","Failed to open disc"'
        exit 0
        ;;
    *"info disc:2"*)
        echo 'DRV:0,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 MOEQ41G4438","",""'
        echo 'DRV:1,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 MOBP8JD4640","",""'
        echo 'DRV:2,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 MO8Q4283637","",""'
        echo 'MSG:3007,0,0,"Using direct disc access mode","Using direct disc access mode"'
        sleep 2
        exit 0
        ;;
esac
exit 1
"""
                ps_3 = f"""#!/bin/bash
if [ "$1" = "-Ao" ]; then
    echo "  {sleeper.pid} python3 /u/ripqueue.py --state {root / 'd11.state.json'} --device disc:0"
    echo "  55555 makemkvcon -r mkv disc:0 0 /tmp/stage0"
    exit 0
fi
exit 0
"""
                env, capture = _build_wrapper_env(
                    root,
                    makemkv_script=mkv_3,
                    drutil_script=drutil_3,
                    diskutil_script=diskutil_3,
                    ps_script=ps_3,
                )
                res = subprocess.run(
                    [str(WRAPPER)],
                    input="Naruto Shippuden\ntv-season\nDVD\n1\n49\n",
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(res.returncode, 0, f"stdout={res.stdout}\nstderr={res.stderr}")
                self.assertNotIn("Multiple drives have discs inserted", res.stdout)
                cap = json.loads(capture.read_text(encoding="utf-8"))
                self.assertEqual(cap["BURNDVD_DRIVE_DEVICE"], "/dev/disk5")
                self.assertEqual(cap["BURNDVD_DRIVE_SERIAL"], "MO8Q4283637")
                self.assertEqual(cap["BURNDVD_DRUTIL_DRIVE"], "1")
                dev_idx = cap["argv"].index("--device") + 1
                self.assertEqual(cap["argv"][dev_idx], "disc:2")
            finally:
                sleeper.terminate()
                sleeper.wait(timeout=5)

    def test_wrapper_3drive_fallback_prompts_choice_when_multiple_free_drives_full(self):
        """When disc:0 is busy and BOTH disc:1 and disc:2 have discs inserted while
        makemkvcon info list fails, wrapper MUST present the multi-drive selection menu
        and bind the chosen drive to its exact disc:K slot using cached topology."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / ".cache" / "burndvd"
            claim_dir = cache_dir / "drive-claims"
            busy_claim = claim_dir / "serial-MOEQ41G4438.claim"
            busy_claim.mkdir(parents=True)
            # Pre-seed drive-topology.json as populated by an earlier info list run.
            (cache_dir / "drive-topology.json").write_text(
                json.dumps(
                    {
                        "drutil_to_serial": {
                            "1": "MO8Q4283637",
                            "2": "MOBP8JD4640",
                            "3": "MOEQ41G4438",
                        }
                    }
                ),
                encoding="utf-8",
            )
            sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
            try:
                (busy_claim / "pid").write_text(str(sleeper.pid), encoding="utf-8")
                (busy_claim / "dev").write_text("/dev/disk6", encoding="utf-8")
                (busy_claim / "disc").write_text("0", encoding="utf-8")
                (busy_claim / "state").write_text(str(root / "d11.state.json"), encoding="utf-8")

                drutil_3 = """#!/bin/bash
if [ "$1" = "list" ]; then
    echo "   Vendor   Product           Rev   Bus       SupportLevel"
    echo "1  HL-DT-ST BD-RE BU40N       1.03  USB       Unsupported"
    echo "2  HL-DT-ST BD-RE BU40N       1.03  USB       Unsupported"
    echo "3  HL-DT-ST BD-RE BU40N       1.03  USB       Unsupported"
    exit 0
fi
if [ "$1" = "status" ] && [ "$2" = "-drive" ]; then
    case "$3" in
        1) echo "Type: DVD-ROM   Name: /dev/disk5" ;;
        2) echo "Type: DVD-ROM   Name: /dev/disk7" ;;
        3) echo "Type: DVD-ROM   Name: /dev/disk6" ;;
    esac
    exit 0
fi
exit 0
"""
                diskutil_3 = """#!/bin/bash
case "$2" in
    /dev/disk5) echo "   Volume Name:               NarutoShippuden_BS4_D12" ;;
    /dev/disk6) echo "   Volume Name:               NarutoShippuden_BS4_D11" ;;
    /dev/disk7) echo "   Volume Name:               NarutoShippuden_BS2_D6" ;;
esac
exit 0
"""
                mkv_3 = """#!/bin/bash
case "$*" in
    *"info list"*)
        exit 1
        ;;
    *"info disc:1"*)
        echo 'DRV:1,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 MOBP8JD4640","",""'
        echo 'MSG:3007,0,0,"Using direct disc access mode","Using direct disc access mode"'
        sleep 2
        exit 0
        ;;
    *"info disc:2"*)
        echo 'DRV:2,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 MO8Q4283637","",""'
        echo 'MSG:3007,0,0,"Using direct disc access mode","Using direct disc access mode"'
        sleep 2
        exit 0
        ;;
esac
exit 1
"""
                ps_3 = f"""#!/bin/bash
if [ "$1" = "-Ao" ]; then
    echo "  {sleeper.pid} python3 /u/ripqueue.py --state {root / 'd11.state.json'} --device disc:0"
    echo "  55555 makemkvcon -r mkv disc:0 0 /tmp/stage0"
    exit 0
fi
exit 0
"""
                env, capture = _build_wrapper_env(
                    root,
                    makemkv_script=mkv_3,
                    drutil_script=drutil_3,
                    diskutil_script=diskutil_3,
                    ps_script=ps_3,
                )
                # User chooses [2] (/dev/disk7 = MOBP8JD4640 = disc:1 = BS2_D6).
                res = subprocess.run(
                    [str(WRAPPER)],
                    input="2\nNaruto Shippuden\ntv-season\nDVD\n1\n22\n",
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(res.returncode, 0, f"stdout={res.stdout}\nstderr={res.stderr}")
                self.assertIn("Multiple drives have discs inserted:", res.stdout)
                self.assertIn("NarutoShippuden_BS4_D12", res.stdout)
                self.assertIn("NarutoShippuden_BS2_D6", res.stdout)
                cap = json.loads(capture.read_text(encoding="utf-8"))
                self.assertEqual(cap["BURNDVD_DRIVE_DEVICE"], "/dev/disk7")
                self.assertEqual(cap["BURNDVD_DRIVE_SERIAL"], "MOBP8JD4640")
                self.assertEqual(cap["BURNDVD_DRUTIL_DRIVE"], "2")
                dev_idx = cap["argv"].index("--device") + 1
                self.assertEqual(cap["argv"][dev_idx], "disc:1")
            finally:
                sleeper.terminate()
                sleeper.wait(timeout=5)

    def test_wrapper_3drive_fallback_refuses_when_all_free_slots_empty(self):
        """If every free disc:K slot reports MSG:5010 empty, never launch ripqueue on an empty slot."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claim_dir = root / ".cache" / "burndvd" / "drive-claims"
            busy_claim = claim_dir / "serial-MOEQ41G4438.claim"
            busy_claim.mkdir(parents=True)
            sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
            try:
                (busy_claim / "pid").write_text(str(sleeper.pid), encoding="utf-8")
                (busy_claim / "dev").write_text("/dev/disk6", encoding="utf-8")
                (busy_claim / "disc").write_text("0", encoding="utf-8")
                (busy_claim / "state").write_text(str(root / "d11.state.json"), encoding="utf-8")

                drutil_3 = """#!/bin/bash
if [ "$1" = "list" ]; then
    echo "   Vendor   Product           Rev   Bus       SupportLevel"
    echo "1  HL-DT-ST BD-RE BU40N       1.03  USB       Unsupported"
    echo "2  HL-DT-ST BD-RE BU40N       1.03  USB       Unsupported"
    echo "3  HL-DT-ST BD-RE BU40N       1.03  USB       Unsupported"
    exit 0
fi
if [ "$1" = "status" ] && [ "$2" = "-drive" ]; then
    case "$3" in
        1) echo "Type: DVD-ROM   Name: /dev/disk5" ;;
        2) echo "Type: No Media Inserted" ;;
        3) echo "Type: DVD-ROM   Name: /dev/disk6" ;;
    esac
    exit 0
fi
exit 0
"""
                diskutil_3 = """#!/bin/bash
echo "   Volume Name:               NarutoShippuden_BS4_D12"
exit 0
"""
                mkv_empty = """#!/bin/bash
case "$*" in
    *"info list"*) exit 1 ;;
    *"info disc:"*)
        echo 'MSG:5010,0,0,"Failed to open disc","Failed to open disc"'
        exit 0
        ;;
esac
exit 1
"""
                ps_3 = f"""#!/bin/bash
if [ "$1" = "-Ao" ]; then
    echo "  {sleeper.pid} python3 /u/ripqueue.py --state {root / 'd11.state.json'} --device disc:0"
    echo "  55555 makemkvcon -r mkv disc:0 0 /tmp/stage0"
    exit 0
fi
exit 0
"""
                env, capture = _build_wrapper_env(
                    root,
                    makemkv_script=mkv_empty,
                    drutil_script=drutil_3,
                    diskutil_script=diskutil_3,
                    ps_script=ps_3,
                )
                res = subprocess.run(
                    [str(WRAPPER)],
                    input="",
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertNotEqual(res.returncode, 0)
                self.assertFalse(capture.exists())
                self.assertIn("no available drive", res.stderr)
            finally:
                sleeper.terminate()
                sleeper.wait(timeout=5)


class TestProbeDiscDiagnosticSurfacing(unittest.TestCase):
    def test_probe_disc_surfaces_msg2003_tray_open_and_msg5010_instead_of_silent_swallow(self):
        """Even when disc_present() is False (returning None so wait_for_disc keeps polling),
        probe_disc() must surface concise MSG:2003 / TRAY OPEN / MSG:5010 diagnostics."""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "probe-state.json"
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:1",
                state=str(state_path),
            )
            lines = [
                'MSG:1005,0,1,"MakeMKV v1.17.7 darwin(arm64-release) started"',
                'MSG:2003,0,3,"Error \'Scsi error - NOT READY:MEDIUM NOT PRESENT - TRAY OPEN\' occurred while reading \'/BD-RE HL-DT-ST BD-RE BU40N 1.03\'"',
                'MSG:5010,0,0,"Failed to open disc"',
            ]

            buf = io.StringIO()
            with mock.patch.object(probe, "_optical_mounts", return_value=[]), \
                 mock.patch.object(probe, "_drive_idx_from_args", return_value=1), \
                 mock.patch.object(probe, "resolve_makemkv_device", return_value="disc:1"), \
                 mock.patch.object(probe, "disc_present", return_value=False), \
                 mock.patch.object(probe.subprocess, "Popen", side_effect=lambda *a, **kw: _FakePopen(lines)), \
                 contextlib.redirect_stdout(buf):
                res = probe.probe_disc(args)

            self.assertIsNone(res)
            out = buf.getvalue()
            self.assertIn("probe diagnostic (disc:1):", out)
            self.assertIn("MSG:2003", out)
            self.assertIn("TRAY OPEN", out)
            self.assertIn("MSG:5010", out)
            self.assertIn("Failed to open disc", out)

            log_text = state_path.with_suffix(".log").read_text(encoding="utf-8")
            self.assertIn("PROBE_DIAG", log_text)
            self.assertIn("MSG:2003", log_text)
            self.assertIn("MSG:5010", log_text)

    def test_probe_disc_surfaces_stall_kill_and_partial_diagnostic_messages(self):
        """When probe_disc kills makemkvcon on stall, surface the stall reason plus any
        captured MakeMKV diagnostic line instead of returning None silently."""
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "stall-state.json"
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                state=str(state_path),
            )
            stalling_proc = _StallingPopen(
                [
                    'MSG:2003,0,3,"Error \'Scsi error - NOT READY:MEDIUM NOT PRESENT\' occurred"',
                ]
            )
            # Simulated monotonic-ish clock that trips PROBE_STALL_S after reading initial line.
            clock_vals = iter([1000.0, 1000.0, 1000.5, 1001.0, 1500.0, 1500.0, 1500.0])

            def fake_time():
                try:
                    return next(clock_vals)
                except StopIteration:
                    return 1500.0

            buf = io.StringIO()
            with mock.patch.object(probe, "_optical_mounts", return_value=[]), \
                 mock.patch.object(probe, "_drive_idx_from_args", return_value=0), \
                 mock.patch.object(probe, "resolve_makemkv_device", return_value="disc:0"), \
                 mock.patch.object(probe, "PROBE_STALL_S", 10), \
                 mock.patch.object(probe, "PROBE_HARD_S", 600), \
                 mock.patch.object(probe.time, "time", side_effect=fake_time), \
                 mock.patch.object(probe.subprocess, "Popen", return_value=stalling_proc), \
                 contextlib.redirect_stdout(buf):
                res = probe.probe_disc(args)

            self.assertIsNone(res)
            self.assertTrue(stalling_proc.killed)
            out = buf.getvalue()
            self.assertIn("killed on stall", out)
            self.assertIn("disc:0", out)
            self.assertIn("MSG:2003", out)
            self.assertIn("MEDIUM NOT PRESENT", out)

            log_text = state_path.with_suffix(".log").read_text(encoding="utf-8")
            self.assertIn("PROBE_DIAG", log_text)
            self.assertIn("killed on stall", log_text)
            self.assertIn("MSG:2003", log_text)

    def test_probe_disc_passes_noscan_for_concrete_disc_selector_only(self):
        """Concrete disc:N probes MUST pass --noscan so MakeMKV does not stall waiting
        on a sibling optical drive that is actively ripping."""
        seen_cmds: list[list[str]] = []

        def fake_popen(cmd, **kwargs):
            seen_cmds.append(list(cmd))
            return _FakePopen(
                [
                    'CINFO:2,0,"SAMPLE_DISC"',
                    'TINFO:0,9,0,"0:42:00"',
                    'TINFO:0,11,0,"4000000000"',
                ]
            )

        args_disc = SimpleNamespace(makemkvcon="/fake/makemkvcon", device="disc:0")
        args_iso = SimpleNamespace(makemkvcon="/fake/makemkvcon", device="iso:/tmp/sample.iso")
        with mock.patch.object(probe, "_optical_mounts", return_value=[]), \
             mock.patch.object(probe, "_drive_idx_from_args", return_value=0), \
             mock.patch.object(probe, "resolve_makemkv_device", side_effect=lambda a: a.device), \
             mock.patch.object(probe.subprocess, "Popen", side_effect=fake_popen):
            info_disc = probe.probe_disc(args_disc)
            info_iso = probe.probe_disc(args_iso)

        self.assertIsNotNone(info_disc)
        self.assertIsNotNone(info_iso)
        self.assertIn("--noscan", seen_cmds[0])
        self.assertEqual(seen_cmds[0][-2:], ["info", "disc:0"])
        self.assertNotIn("--noscan", seen_cmds[1])
        self.assertEqual(seen_cmds[1][-2:], ["info", "iso:/tmp/sample.iso"])

    def test_probe_disc_extends_stall_window_during_direct_disc_access(self):
        """Dual-layer DVD-9 scans stay silent after MSG:3007 (direct disc access)
        past the initial 60s window; probe_disc must extend stall_s instead of
        prematurely killing the probe."""
        class _SlowDirectAccessPopen:
            def __init__(self, lines):
                self._lines = list(lines)
                self.killed = False
                self.returncode = 0
                self.stdout = self._gen()

            def _gen(self):
                for line in self._lines:
                    yield line + "\n"

            def kill(self):
                self.killed = True
                self.returncode = -9

            def wait(self, timeout=None):
                return self.returncode

        proc = _SlowDirectAccessPopen(
            [
                'MSG:3007,0,0,"Using direct disc access mode"',
                'CINFO:2,0,"NarutoShippuden_BS4_D12"',
                'TINFO:0,9,0,"1:57:00"',
                'TINFO:0,11,0,"6200000000"',
            ]
        )
        # Jump 90s between MSG:3007 and CINFO:2 (>60s PROBE_STALL_S, <180s direct-access window).
        clock_vals = iter([1000.0, 1000.0, 1001.0, 1002.0, 1092.0, 1092.5, 1093.0, 1093.5])

        def fake_time():
            try:
                return next(clock_vals)
            except StopIteration:
                return 1094.0

        args = SimpleNamespace(makemkvcon="/fake/makemkvcon", device="disc:2")
        with mock.patch.object(probe, "_optical_mounts", return_value=[]), \
             mock.patch.object(probe, "_drive_idx_from_args", return_value=2), \
             mock.patch.object(probe, "resolve_makemkv_device", return_value="disc:2"), \
             mock.patch.object(probe, "PROBE_STALL_S", 60), \
             mock.patch.object(probe, "PROBE_DIRECT_ACCESS_STALL_S", 180), \
             mock.patch.object(probe, "PROBE_HARD_S", 300), \
             mock.patch.object(probe.time, "time", side_effect=fake_time), \
             mock.patch.object(probe.subprocess, "Popen", return_value=proc):
            info = probe.probe_disc(args)

        self.assertFalse(proc.killed)
        self.assertIsNotNone(info)
        self.assertEqual(info["cinfo"][2], "NarutoShippuden_BS4_D12")


if __name__ == "__main__":
    unittest.main()
