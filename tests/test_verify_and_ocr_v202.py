"""Regression tests for strict media verification, checkrip truncation detection,
subtitle OCR lock ownership, and post-commit OCR error isolation (v2.0.2).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from burndvd import ripper
from burndvd.cli import checkrip, ripqueue


def _make_sample_mkv(dst: Path, *, duration_s: int = 2) -> None:
    """Generate a valid 30fps test-pattern MKV (60 frames at 2s)."""
    subprocess.run(
        [
            ripper.FFMPEG_BIN,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=256x144:rate=30",
            "-t",
            str(duration_s),
            str(dst),
        ],
        check=True,
    )


class TestStrictMediaVerificationAndCheckrip(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_valid_generated_mkv_accepted_by_verify_and_checkrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            mkv = d / "Test Show - S01E01.mkv"
            _make_sample_mkv(mkv, duration_s=2)

            ok, err = ripper.verify([str(mkv)])
            self.assertTrue(ok, f"valid MKV unexpectedly rejected: {err}")
            self.assertEqual(err, "")

            buf_json = io.StringIO()
            with mock.patch.object(
                sys, "argv", ["checkrip", str(d), "--expected", "1", "--json"]
            ), contextlib.redirect_stdout(buf_json):
                rc_json = checkrip.main()
            self.assertEqual(rc_json, 0)
            payload = json.loads(buf_json.getvalue())
            self.assertEqual(payload["real_count"], 1)
            self.assertEqual(payload["unreadable"], [])
            self.assertEqual(payload["missing_vs_expected"], 0)

            buf_human = io.StringIO()
            with mock.patch.object(
                sys, "argv", ["checkrip", str(d), "--expected", "1"]
            ), contextlib.redirect_stdout(buf_human):
                rc_human = checkrip.main()
            self.assertEqual(rc_human, 0)
            self.assertIn("OK", buf_human.getvalue())

    def test_truncated_60_to_25_frame_mkv_rejected_by_verify_and_checkrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            good = d / "good.mkv"
            _make_sample_mkv(good, duration_s=2)
            original = good.read_bytes()
            good.unlink()

            trunc = d / "Test Show - S01E01.mkv"
            trunc.write_bytes(original[: len(original) // 2])

            ok, reason = ripper.verify([str(trunc)])
            self.assertFalse(ok)
            self.assertTrue(reason)
            self.assertIn("prematurely", reason.lower())

            buf_json = io.StringIO()
            with mock.patch.object(
                sys, "argv", ["checkrip", str(d), "--expected", "1", "--json"]
            ), contextlib.redirect_stdout(buf_json):
                rc_json = checkrip.main()
            self.assertEqual(rc_json, 1)
            payload = json.loads(buf_json.getvalue())
            self.assertEqual(payload["real_count"], 0)
            self.assertEqual(payload["missing_vs_expected"], 1)
            self.assertEqual(len(payload["unreadable"]), 1)
            self.assertEqual(payload["unreadable"][0]["name"], trunc.name)
            self.assertIn("prematurely", payload["unreadable"][0]["reason"].lower())

            buf_human = io.StringIO()
            with mock.patch.object(
                sys, "argv", ["checkrip", str(d), "--expected", "1"]
            ), contextlib.redirect_stdout(buf_human):
                rc_human = checkrip.main()
            self.assertEqual(rc_human, 1)
            self.assertIn("UNREADABLE", buf_human.getvalue())

    def test_truncated_mkv_end_to_end_queue_no_receipt_and_staging_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            q = d / "queue.csv"
            q.write_text(
                f"title,type,discs,target_root,format\nTest Movie,movie,1,{d},BD\n",
                encoding="utf-8",
            )
            st = d / "state.json"
            ledger = d / "receipts.jsonl"
            stage = d / "staging"
            stage.mkdir()
            src = stage / "title.mkv"
            dest = d / "Test Movie.mkv"

            _make_sample_mkv(src, duration_s=2)
            original = src.read_bytes()
            src.write_bytes(original[: len(original) // 2])

            info = {"cinfo": {2: "TEST_MOVIE"}, "titles": {0: {9: "1:40:00", 11: "1000"}}}
            argv = [
                "ripqueue.py",
                "--queue",
                str(q),
                "--state",
                str(st),
                "--makemkvcon",
                sys.executable,
                "--device",
                "iso:/fake",
                "--non-interactive",
                "--no-eject",
                "--no-sound",
                "--no-subocr",
                "--verify",
                "--disc-receipts",
                str(ledger),
            ]
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(sys, "argv", argv))
                stack.enter_context(mock.patch.object(ripqueue.signal, "signal"))
                stack.enter_context(mock.patch.object(ripqueue, "check_key_expiry"))
                stack.enter_context(
                    mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info))
                )
                stack.enter_context(
                    mock.patch.object(
                        ripqueue,
                        "rip",
                        return_value=(
                            True,
                            {
                                "elapsed_s": 5.0,
                                "files": [str(dest)],
                                "move_jobs": [(src, dest, "film")],
                                "staging": str(stage),
                                "disc_fingerprint": "f" * 64,
                            },
                        ),
                    )
                )
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    try:
                        ripqueue.main()
                        code = 0
                    except SystemExit as exc:
                        code = exc.code

            self.assertNotEqual(code, 0)
            state = json.loads(st.read_text(encoding="utf-8"))
            self.assertEqual(len(state["completed"]), 0)
            self.assertEqual(len(state["failed_finalize"]), 1)
            self.assertEqual(state["current_index"], 0)
            self.assertFalse(ledger.exists(), "truncated video must never receive a published receipt")
            self.assertTrue(stage.exists(), "staging directory must not be retired when verification fails")


class TestSubocrLockOwnershipAndFailureIsolation(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_run_subocr_postrip_does_not_deadlock_with_subocr_acquire_lock(self):
        repo_src = str(Path(__file__).resolve().parent.parent / "src")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            child = home / ".local" / "bin" / "subocr"
            child.parent.mkdir(parents=True)
            child.write_text(
                "#!" + sys.executable + "\n"
                "import fcntl, sys\n"
                "original = fcntl.flock\n"
                "def nonblocking(fd, operation):\n"
                "    try:\n"
                "        return original(fd, operation | fcntl.LOCK_NB)\n"
                "    except BlockingIOError:\n"
                "        print(LOCK_CONFLICT, flush=True)\n"
                "        raise SystemExit(73)\n"
                "fcntl.flock = nonblocking\n"
                "from burndvd.cli.subocr import main\n"
                "main()\n",
                encoding="utf-8",
            )
            child.chmod(0o700)

            env = dict(os.environ)
            env["HOME"] = str(home)
            env["PYTHONPATH"] = repo_src
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env.pop("BURNDVD_SUBOCR_LOCK", None)

            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(ripper.subprocess, "Popen") as start:
                    ripper.run_subocr_postrip(SimpleNamespace(no_subocr=False), ["nonexistent.mkv"])
                self.assertEqual(start.call_count, 1)
                cmd = start.call_args.args[0]
                self.assertNotIn("fcntl.flock", cmd[2])

                got = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)

            self.assertNotEqual(
                got.returncode,
                73,
                f"launcher held subocr.flock and deadlocked child: stdout={got.stdout!r} stderr={got.stderr!r}",
            )
            self.assertNotIn("LOCK_CONFLICT", got.stdout)
            self.assertIn(f"[subocr-postrip] exit={got.returncode}", got.stdout)
            self.assertTrue((home / ".cache" / "burndvd" / "subocr.flock").exists())

    def test_ocr_launch_permission_error_preserves_committed_completion_without_failed_finalize(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            q = d / "queue.csv"
            q.write_text(
                f"title,type,discs,target_root,format\nTest Movie,movie,1,{d},BD\n",
                encoding="utf-8",
            )
            st = d / "state.json"
            ledger = d / "receipts.jsonl"
            dest = d / "Test Movie.mkv"
            dest.write_bytes(b"fixture")
            info = {"cinfo": {2: "TEST_MOVIE"}, "titles": {0: {9: "1:40:00", 11: "1000"}}}
            argv = [
                "ripqueue.py",
                "--queue",
                str(q),
                "--state",
                str(st),
                "--makemkvcon",
                sys.executable,
                "--device",
                "iso:/fake.iso",
                "--non-interactive",
                "--no-eject",
                "--no-sound",
                "--verify",
                "--disc-receipts",
                str(ledger),
            ]
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(sys, "argv", argv))
                stack.enter_context(mock.patch.object(ripqueue.signal, "signal"))
                stack.enter_context(mock.patch.object(ripqueue, "check_key_expiry"))
                stack.enter_context(
                    mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info))
                )
                stack.enter_context(
                    mock.patch.object(
                        ripqueue,
                        "rip",
                        return_value=(
                            True,
                            {
                                "elapsed_s": 5.0,
                                "files": [str(dest)],
                                "move_jobs": [],
                                "disc_fingerprint": "f" * 64,
                            },
                        ),
                    )
                )
                stack.enter_context(mock.patch.object(ripper, "log_final_durations"))
                stack.enter_context(mock.patch.object(ripper, "verify", return_value=(True, "")))
                stack.enter_context(
                    mock.patch.object(
                        ripper,
                        "run_subocr_postrip",
                        side_effect=PermissionError("mock OCR launcher permission denied"),
                    )
                )
                out_buf = io.StringIO()
                with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(
                    io.StringIO()
                ):
                    try:
                        ripqueue.main()
                        code = 0
                    except SystemExit as exc:
                        code = exc.code

            state = json.loads(st.read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertEqual(state["current_index"], 1)
            self.assertEqual(len(state["completed"]), 1)
            self.assertEqual(len(state["failed_finalize"]), 0)
            self.assertEqual(len(ledger.read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(ripper.get_finalize_errors(), [])
            self.assertIn("subtitle OCR launch failed", out_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
