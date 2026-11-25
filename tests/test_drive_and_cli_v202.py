"""Regression tests for v2.0.2 two-drive targeting and burndvd CLI help handling."""

from __future__ import annotations

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

from burndvd import drive

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "bin" / "burndvd"


def _write_exec(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _two_drive_drutil_runner(calls: list[list[str]], *, drive2_node: str = "/dev/disk9"):
    """Return a subprocess.run side_effect for a 2-drive host (drive 1 empty, drive 2 loaded)."""

    def _run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == [drive.DRUTIL_BIN, "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "  1  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported\n"
                    "  2  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported\n"
                ),
                stderr="",
            )
        if cmd == [drive.DRUTIL_BIN, "status", "-drive", "1"]:
            return SimpleNamespace(
                returncode=0,
                stdout=" Vendor   Product           Rev\n HL-DT-ST BD-RE BU40N       1.03\n\n           Type: No Media Inserted\n",
                stderr="",
            )
        if cmd == [drive.DRUTIL_BIN, "status", "-drive", "2"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    " Vendor   Product           Rev\n"
                    " HL-DT-ST BD-RE BU40N       1.03\n\n"
                    f"           Type: DVD-ROM              Name: {drive2_node}\n"
                    "     Space Used: 4.32GB\n"
                ),
                stderr="",
            )
        if cmd[:3] == [drive.DRUTIL_BIN, "-drive", "2"] and cmd[3:] in (
            ["eject"],
            ["tray", "eject"],
            ["tray", "close"],
        ):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == [drive.DRUTIL_BIN, "-drive", "1"]:
            raise AssertionError(f"must never operate on empty drive 1: {cmd}")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return _run


class TestTwoDrivePhysicalTargeting(unittest.TestCase):
    def setUp(self):
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                k: v
                for k, v in os.environ.items()
                if k not in (drive.DRUTIL_DRIVE_ENV, drive.DRIVE_DEVICE_ENV)
            },
            clear=True,
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_explicit_env_targets_drive_2_for_disc_0_status_and_eject(self):
        calls: list[list[str]] = []
        args = SimpleNamespace(device="disc:0")
        with mock.patch.dict(
            os.environ,
            {
                drive.DRUTIL_DRIVE_ENV: "2",
                drive.DRIVE_DEVICE_ENV: "/dev/disk9",
            },
        ), mock.patch.object(
            drive.subprocess, "run", side_effect=_two_drive_drutil_runner(calls)
        ):
            self.assertEqual(drive.drutil_drive_index("disc:0"), 2)
            self.assertEqual(drive._drive_idx_from_args(args), 1)
            status = drive.drive_status(args)
            self.assertEqual(status["drutil_drive"], 2)
            self.assertEqual(status["device_node"], "/dev/disk9")
            self.assertTrue(status["media_present"])
            self.assertTrue(drive.disc_present(args))
            self.assertTrue(drive.eject_disc(args))
            drive.eject(args)

        self.assertIn([drive.DRUTIL_BIN, "-drive", "2", "eject"], calls)
        for cmd in calls:
            self.assertNotIn(
                "-drive 1",
                " ".join(cmd),
                f"drive 1 must not be queried or ejected when verified target is drive 2: {calls}",
            )

    def test_fallback_resolution_maps_disc_0_to_loaded_drive_2_without_env(self):
        calls: list[list[str]] = []
        args = SimpleNamespace(device="disc:0")
        with mock.patch.object(
            drive.subprocess, "run", side_effect=_two_drive_drutil_runner(calls)
        ):
            self.assertEqual(drive.drutil_drive_index("disc:0"), 2)
            status = drive.drive_status(args)
            self.assertEqual(status["drutil_drive"], 2)
            self.assertEqual(status["device_node"], "/dev/disk9")
            self.assertTrue(status["media_present"])
            self.assertTrue(drive.disc_present(args))
            self.assertTrue(drive.eject_disc(args))

        eject_calls = [c for c in calls if "eject" in c]
        self.assertEqual(eject_calls, [[drive.DRUTIL_BIN, "-drive", "2", "eject"]])

    def test_explicit_kwargs_override_disc_selector(self):
        calls: list[list[str]] = []
        with mock.patch.object(
            drive.subprocess, "run", side_effect=_two_drive_drutil_runner(calls)
        ):
            self.assertEqual(
                drive.drutil_drive_index("disc:0", drutil_drive=2, device_node="/dev/disk9"),
                2,
            )
            self.assertTrue(
                drive.eject_disc(
                    SimpleNamespace(device="disc:0"),
                    drutil_drive=2,
                    device_node="/dev/disk9",
                )
            )
        self.assertIn([drive.DRUTIL_BIN, "-drive", "2", "eject"], calls)

    def test_mismatched_device_node_refuses_wrong_drive_eject(self):
        calls: list[list[str]] = []
        args = SimpleNamespace(device="disc:0")
        with mock.patch.dict(
            os.environ,
            {
                drive.DRUTIL_DRIVE_ENV: "2",
                drive.DRIVE_DEVICE_ENV: "/dev/disk99",
            },
        ), mock.patch.object(
            drive.subprocess,
            "run",
            side_effect=_two_drive_drutil_runner(calls, drive2_node="/dev/disk9"),
        ):
            self.assertIsNone(drive.drutil_drive_index(args))
            self.assertFalse(drive.eject_disc(args))
        self.assertEqual([c for c in calls if "eject" in c], [])

    def test_wrapper_exports_drive_2_and_disk9_to_child_ripqueue_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            logs = root / "logs"
            logs.mkdir()
            media = root / "media"
            (media / "Movies").mkdir(parents=True)
            capture_file = root / "captured_env.json"

            _write_exec(
                fake_bin / "drutil",
                """#!/bin/bash
if [ "$1" = "list" ]; then
    echo "  1  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported"
    echo "  2  HL-DT-ST BD-RE BU40N  1.03  USB  Apple Supported"
    exit 0
fi
if [ "$1" = "status" ] && [ "$2" = "-drive" ] && [ "$3" = "1" ]; then
    echo "  Type: No Media Inserted"
    exit 0
fi
if [ "$1" = "status" ] && [ "$2" = "-drive" ] && [ "$3" = "2" ]; then
    echo "  Type: DVD-ROM  Name: /dev/disk9"
    echo "  Space Used: 4.50GB"
    exit 0
fi
exit 1
""",
            )
            _write_exec(
                fake_bin / "diskutil",
                """#!/bin/bash
if [ "$1" = "info" ] && [ "$2" = "/dev/disk9" ]; then
    echo "   Volume Name:               SAMPLE_MOVIE_2003"
    exit 0
fi
exit 1
""",
            )
            _write_exec(fake_bin / "pgrep", "#!/bin/bash\nexit 1\n")
            _write_exec(fake_bin / "ps", "#!/bin/bash\nexit 0\n")
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

            res = subprocess.run(
                [str(WRAPPER)],
                input="Sample Movie (2003)\nmovie\nDVD\n",
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(res.returncode, 0, f"stdout={res.stdout}\nstderr={res.stderr}")
            self.assertTrue(capture_file.is_file(), "child ripqueue was not invoked")
            captured = json.loads(capture_file.read_text())
            self.assertEqual(captured["BURNDVD_DRUTIL_DRIVE"], "2")
            self.assertEqual(captured["BURNDVD_DRIVE_DEVICE"], "/dev/disk9")
            self.assertIn("disc:0", captured["argv"])

            # Feed the exact child environment into drive.eject_disc to confirm end-to-end agreement.
            drive_calls: list[list[str]] = []
            with mock.patch.dict(
                os.environ,
                {
                    drive.DRUTIL_DRIVE_ENV: captured["BURNDVD_DRUTIL_DRIVE"],
                    drive.DRIVE_DEVICE_ENV: captured["BURNDVD_DRIVE_DEVICE"],
                },
            ), mock.patch.object(
                drive.subprocess, "run", side_effect=_two_drive_drutil_runner(drive_calls)
            ):
                self.assertTrue(drive.eject_disc(SimpleNamespace(device="disc:0")))
            self.assertEqual(
                drive_calls,
                [
                    [drive.DRUTIL_BIN, "status", "-drive", "2"],
                    [drive.DRUTIL_BIN, "-drive", "2", "eject"],
                ],
            )


class TestBurndvdHelpCli(unittest.TestCase):
    def test_help_and_short_h_exit_zero_with_zero_drive_or_process_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shims = root / "shims"
            shims.mkdir()
            probe_log = root / "probe.log"
            for tool in ("drutil", "diskutil", "ps", "pgrep", "lsof"):
                _write_exec(
                    shims / tool,
                    f"#!/bin/sh\necho {tool} >> {str(probe_log)!r}\nexit 99\n",
                )

            env = {
                "PATH": f"{shims}:/usr/bin:/bin",
                "HOME": str(root),
                "TMPDIR": str(root),
            }

            for flag in ("--help", "-h", "--rerip-review"):
                argv = [str(WRAPPER), flag] if flag != "--rerip-review" else [str(WRAPPER), "--rerip-review", "--help"]
                res = subprocess.run(
                    argv,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(res.returncode, 0, f"argv={argv} stderr={res.stderr}")
                self.assertIn("Usage:", res.stdout)
                self.assertIn("--rerip-review", res.stdout)
                self.assertIn("--queue", res.stdout)
                self.assertIn("BURNDVD_DRUTIL_DRIVE", res.stdout)
                self.assertIn("BURNDVD_DRIVE_DEVICE", res.stdout)
                self.assertFalse(
                    probe_log.exists(),
                    f"help invocation must not probe optical drives or processes: {argv}",
                )


if __name__ == "__main__":
    unittest.main()
