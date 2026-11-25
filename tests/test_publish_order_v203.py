"""Regression tests for v2.0.3: verify-before-publish, descendant symlink containment,
resumable per-file batch commits, and packaged CLI entrypoints.
"""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import burndvd
from burndvd import models, nas, ripper
from burndvd.cli import ripqueue

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_sample_mkv(dst: Path, *, duration_s: int = 2) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
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


def _make_truncated_mkv(dst: Path) -> bytes:
    good = dst.with_name(dst.name + ".good.mkv")
    _make_sample_mkv(good, duration_s=2)
    raw = good.read_bytes()
    good.unlink()
    truncated = raw[: len(raw) // 2]
    dst.write_bytes(truncated)
    return truncated


class TestVerifyBeforePublish(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_truncated_mkv_fails_before_library_visibility_and_retains_staging_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            lib = root / "library"
            stage.mkdir()
            lib.mkdir()
            src = stage / "sample.mkv"
            dst = lib / "Movie.mkv"
            truncated_bytes = _make_truncated_mkv(src)
            ledger = root / "receipts.jsonl"

            args = SimpleNamespace(
                state=None,
                verify=True,
                rerip_review=False,
                no_subocr=True,
                no_sound=True,
                overwrite=False,
                disc_receipts=str(ledger),
            )
            item = SimpleNamespace(title="Movie", type="movie")
            result = {
                "files": [str(dst)],
                "move_jobs": [(src, dst, "fixture")],
                "staging": str(stage),
                "disc_fingerprint": "a" * 64,
            }
            completions: list[dict] = []
            failures: list[str] = []

            with mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok = ripper._run_finalize(
                    args,
                    result,
                    item,
                    1,
                    on_complete=completions.append,
                    on_failure=failures.append,
                )

            self.assertFalse(ok)
            self.assertFalse(dst.exists(), "invalid truncated bytes must never reach final library filename")
            self.assertTrue(stage.is_dir())
            self.assertTrue(src.is_file(), "staged source must remain intact on verify failure")
            self.assertEqual(src.read_bytes(), truncated_bytes)
            self.assertFalse(ledger.exists())
            self.assertEqual(len(completions), 0)
            self.assertEqual(len(failures), 1)
            self.assertIn("VERIFY_FAIL", failures[0])

    def test_truncated_mkv_end_to_end_queue_leaves_final_absent_and_staging_intact(self):
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
            truncated_bytes = _make_truncated_mkv(src)

            info = {"cinfo": {2: "TEST_MOVIE"}, "titles": {0: {9: "1:40:00", 11: "1000"}}}
            argv = [
                "ripqueue.py",
                "--queue", str(q),
                "--state", str(st),
                "--makemkvcon", sys.executable,
                "--device", "iso:/fake",
                "--non-interactive",
                "--no-eject",
                "--no-sound",
                "--no-subocr",
                "--verify",
                "--disc-receipts", str(ledger),
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
                                "disc_fingerprint": "b" * 64,
                            },
                        ),
                    )
                )
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    try:
                        ripqueue.main()
                        code = 0
                    except SystemExit as exc:
                        code = exc.code

            self.assertNotEqual(code, 0)
            self.assertFalse(dest.exists(), "final library filename must not exist after verify failure")
            self.assertTrue(src.is_file(), "staged source file must survive verify failure")
            self.assertEqual(src.read_bytes(), truncated_bytes)
            self.assertFalse(ledger.exists())
            saved = json.loads(st.read_text(encoding="utf-8"))
            self.assertEqual(saved["completed"], [])
            self.assertEqual(len(saved["failed_finalize"]), 1)

    def test_protected_tv_verify_failure_cleans_owned_claim_and_preserves_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            season = root / "Season 01"
            partial_root = root / ".partials"
            stage.mkdir()
            season.mkdir()
            partial_root.mkdir()
            src = stage / "ep1.mkv"
            _make_truncated_mkv(src)
            dst = season / "Show - S01E01.mkv"
            claim = nas.tv_slot_claim_path(dst)
            claim_id = nas._create_tv_slot_claim(claim)

            args = SimpleNamespace(
                state=None,
                verify=True,
                rerip_review=False,
                no_subocr=True,
                no_sound=True,
                overwrite=False,
                disc_receipts=str(root / "receipts.jsonl"),
            )
            item = SimpleNamespace(title="Show", type="tv-season")
            result = {
                "files": [str(dst)],
                "move_jobs": [
                    (src, dst, "ep1", True, claim_id, claim, partial_root, season)
                ],
                "staging": str(stage),
                "disc_fingerprint": "c" * 64,
            }

            with mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok = ripper._run_finalize(args, result, item, 1)

            self.assertFalse(ok)
            self.assertFalse(dst.exists())
            self.assertFalse(claim.exists(), "owned TV claim placeholder must be cleaned on pre-publish verify failure")
            self.assertTrue(src.is_file())

    def test_valid_mkv_verified_before_move_then_published_with_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            lib = root / "library"
            stage.mkdir()
            lib.mkdir()
            src = stage / "good.mkv"
            dst = lib / "Good.mkv"
            _make_sample_mkv(src, duration_s=2)
            ledger = root / "receipts.jsonl"
            seen_events: list[tuple[str, bool]] = []
            real_verify = ripper.verify

            def spy_verify(paths):
                seen_events.append(("verify", dst.exists()))
                return real_verify(paths)

            args = SimpleNamespace(
                state=None,
                verify=True,
                rerip_review=False,
                no_subocr=True,
                no_sound=True,
                overwrite=False,
                disc_receipts=str(ledger),
            )
            item = models.QueueItem(title="Good", type="movie", discs=1, target_root=str(lib), format="BD")
            result = {
                "files": [str(dst)],
                "move_jobs": [(src, dst, "film")],
                "staging": str(stage),
                "disc_fingerprint": "d" * 64,
            }

            with mock.patch.object(ripper, "verify", side_effect=spy_verify), \
                 mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok = ripper._run_finalize(args, result, item, 1)

            self.assertTrue(ok)
            self.assertEqual(seen_events, [("verify", False)])
            self.assertTrue(dst.is_file())
            self.assertFalse(stage.exists())
            self.assertTrue(ledger.is_file())


class TestDescendantSymlinkContainment(unittest.TestCase):
    def test_descendant_symlink_escape_rejected_across_mount_and_move_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smb = root / "simulated-smb"
            outside = root / "outside-escape"
            smb.mkdir()
            outside.mkdir()
            movies = smb / "Movies"
            movies.mkdir()

            escape_link = movies / "Escape"
            escape_link.symlink_to(outside)

            dangling_link = movies / "Dangling"
            dangling_link.symlink_to(root / "nonexistent-outside")

            rel_link = movies / "RelEscape"
            rel_link.symlink_to(Path("..") / ".." / "outside-escape")

            loop_a = movies / "LoopA"
            loop_b = movies / "LoopB"
            loop_a.symlink_to(loop_b)
            loop_b.symlink_to(loop_a)

            normal_dir = movies / "SafeFilm"

            real_ismount = os.path.ismount

            def fake_ismount(path):
                if Path(path) == smb:
                    return True
                return real_ismount(path)

            src = root / "stage" / "movie.mkv"
            src.parent.mkdir()
            src.write_bytes(b"VALID PAYLOAD")

            with mock.patch.object(nas, "SMB_MEDIA_PREFIX", str(smb)), \
                 mock.patch.object(nas, "NFS_MEDIA_PREFIX", str(root / "unused-nfs")), \
                 mock.patch.object(nas.os.path, "ismount", side_effect=fake_ismount):
                # Normal path inside mount is accepted
                self.assertTrue(nas._verify_nas_mount_identity(normal_dir))
                self.assertTrue(nas._dir_writable(normal_dir))

                # All escaping / cyclic descendant symlinks are rejected fail-closed
                for bad_path in (
                    escape_link,
                    escape_link / "Nested" / "Subdir",
                    dangling_link,
                    dangling_link / "Sub",
                    rel_link,
                    loop_a,
                ):
                    self.assertFalse(
                        nas._verify_nas_mount_identity(bad_path),
                        f"expected symlink escape to be rejected: {bad_path}",
                    )
                    self.assertFalse(
                        nas._dir_writable(bad_path),
                        f"expected write probe to reject symlink escape: {bad_path}",
                    )

                # move_with_progress rejects escaping destination parent
                with self.assertRaises(OSError) as ctx:
                    nas.move_with_progress(src, escape_link / "Movie.mkv")
                self.assertEqual(ctx.exception.errno, errno.ENOTCONN)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertTrue(src.is_file())

                # move_with_progress_noclobber rejects escaping partial_root too
                with self.assertRaises(OSError) as ctx2:
                    nas.move_with_progress_noclobber(
                        src,
                        normal_dir / "Show - S01E01.mkv",
                        partial_root=escape_link / "partials",
                        lock_dir=normal_dir,
                    )
                self.assertEqual(ctx2.exception.errno, errno.ENOTCONN)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertTrue(src.is_file())


class TestResumablePerFileCommits(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_two_file_partial_batch_replays_cleanly_without_self_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            lib = root / "library"
            stage.mkdir()
            lib.mkdir()

            src1 = stage / "first.mkv"
            src2 = stage / "second.mkv"
            src1.write_bytes(b"FIRST-FILE-BYTES")
            src2.write_bytes(b"SECOND-FILE-BYTES")
            dst1 = lib / "Movie.mkv"
            dst2 = lib / "Movie - Extra.mkv"

            args = SimpleNamespace(
                state=None,
                verify=False,
                rerip_review=False,
                no_subocr=True,
                no_sound=True,
                overwrite=False,
                disc_receipts=str(root / "receipts.jsonl"),
            )
            item = SimpleNamespace(title="Movie", type="movie")
            result = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [
                    (src1, dst1, "film"),
                    (src2, dst2, "extra"),
                ],
                "staging": str(stage),
            }

            real_move = ripper.move_with_progress
            call_count = 0

            def fail_on_second(s, d, **kw):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise OSError(errno.ENOSPC, "synthetic second-job failure")
                return real_move(s, d, **kw)

            committed_events: list[dict] = []
            with mock.patch.object(ripper, "move_with_progress", side_effect=fail_on_second), \
                 mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok1 = ripper._run_finalize(
                    args,
                    result,
                    item,
                    1,
                    on_file_committed=committed_events.append,
                )

            self.assertFalse(ok1)
            self.assertTrue(dst1.is_file())
            self.assertEqual(dst1.read_bytes(), b"FIRST-FILE-BYTES")
            self.assertFalse(src1.exists())
            self.assertFalse(dst2.exists())
            self.assertTrue(src2.is_file())
            self.assertEqual(len(committed_events), 1)
            self.assertEqual(committed_events[0]["dst"], str(dst1))
            self.assertTrue((stage / ripper._COMMITTED_MANIFEST_NAME).is_file())

            # Replay with a brand-new result dict (simulating restart loading from staging manifest)
            fresh_result = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [
                    (src1, dst1, "film"),
                    (src2, dst2, "extra"),
                ],
                "staging": str(stage),
            }
            completions: list[dict] = []
            with mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok2 = ripper._run_finalize(
                    args,
                    fresh_result,
                    item,
                    1,
                    on_complete=completions.append,
                    on_file_committed=committed_events.append,
                )

            self.assertTrue(ok2)
            self.assertEqual(len(completions), 1)
            self.assertEqual(len(committed_events), 2)
            self.assertTrue(dst1.is_file())
            self.assertEqual(dst1.read_bytes(), b"FIRST-FILE-BYTES")
            self.assertTrue(dst2.is_file())
            self.assertEqual(dst2.read_bytes(), b"SECOND-FILE-BYTES")
            self.assertFalse(stage.exists(), "staging should be cleaned after full batch commits")

    def test_replay_fails_closed_if_previously_committed_destination_was_tampered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            lib = root / "library"
            stage.mkdir()
            lib.mkdir()

            src1 = stage / "first.mkv"
            src2 = stage / "second.mkv"
            src1.write_bytes(b"FIRST-FILE-BYTES")
            src2.write_bytes(b"SECOND-FILE-BYTES")
            dst1 = lib / "Movie.mkv"
            dst2 = lib / "Movie - Extra.mkv"

            args = SimpleNamespace(
                state=None,
                verify=False,
                rerip_review=False,
                no_subocr=True,
                no_sound=True,
                overwrite=False,
                disc_receipts=str(root / "receipts.jsonl"),
            )
            item = SimpleNamespace(title="Movie", type="movie")
            result = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "film"), (src2, dst2, "extra")],
                "staging": str(stage),
            }

            real_move = ripper.move_with_progress
            n = 0

            def fail_second(s, d, **kw):
                nonlocal n
                n += 1
                if n == 2:
                    raise OSError("synthetic second failure")
                return real_move(s, d, **kw)

            with mock.patch.object(ripper, "move_with_progress", side_effect=fail_second), \
                 mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                self.assertFalse(ripper._run_finalize(args, result, item, 1))

            # Tamper with previously committed file 1 (change its size)
            dst1.write_bytes(b"CORRUPTED")

            with mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                self.assertFalse(ripper._run_finalize(args, result, item, 1))
            self.assertFalse(dst2.exists())
            self.assertTrue(src2.is_file())

    def test_pending_finalize_state_records_committed_jobs_and_survives_reconcile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            q = root / "queue.csv"
            lib_dir = root / "lib"
            q.write_text(
                f"title,type,discs,target_root,format\nDual,movie,1,{lib_dir},BD\n",
                encoding="utf-8",
            )
            st = root / "state.json"
            stage = root / "stage"
            stage.mkdir()
            src1 = stage / "first.mkv"
            src2 = stage / "second.mkv"
            src1.write_bytes(b"ONE")
            src2.write_bytes(b"TWO")
            dst1 = root / "lib" / "Dual.mkv"
            dst2 = root / "lib" / "Dual - Extra.mkv"

            info = {"cinfo": {2: "DUAL"}, "titles": {0: {9: "1:40:00", 11: "1000"}}}
            argv = [
                "ripqueue.py",
                "--queue", str(q),
                "--state", str(st),
                "--makemkvcon", sys.executable,
                "--device", "iso:/fake",
                "--non-interactive",
                "--no-eject",
                "--no-sound",
                "--no-subocr",
            ]
            real_move = ripper.move_with_progress
            n = 0

            def fail_second(s, d, **kw):
                nonlocal n
                n += 1
                if n == 2:
                    raise OSError("synthetic second failure")
                return real_move(s, d, **kw)

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
                                "elapsed_s": 2.0,
                                "files": [str(dst1), str(dst2)],
                                "move_jobs": [(src1, dst1, "film"), (src2, dst2, "extra")],
                                "staging": str(stage),
                            },
                        ),
                    )
                )
                stack.enter_context(
                    mock.patch.object(ripper, "move_with_progress", side_effect=fail_second)
                )
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    try:
                        ripqueue.main()
                        code = 0
                    except SystemExit as exc:
                        code = exc.code

            self.assertNotEqual(code, 0)
            saved = json.loads(st.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["failed_finalize"]), 1)
            committed = saved["failed_finalize"][0].get("committed_jobs", [])
            self.assertEqual(len(committed), 1)
            self.assertEqual(committed[0]["dst"], str(dst1))

            # Verify models._reconcile_pending_finalize preserves committed_jobs
            reconcile_state = {
                "queue": saved["queue"],
                "current_index": 1,
                "disc_index_in_item": 0,
                "completed": [],
                "pending_finalize": [dict(saved["failed_finalize"][0], status="pending_finalize")],
            }
            unresolved = models._reconcile_pending_finalize(reconcile_state)
            self.assertEqual(len(unresolved), 1)
            self.assertEqual(len(unresolved[0]["committed_jobs"]), 1)
            self.assertEqual(reconcile_state["current_index"], 0)


class TestPackagingAndCiContract(unittest.TestCase):
    def test_version_and_packaged_scripts_metadata(self):
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["version"], "2.0.3")
        self.assertEqual(burndvd.__version__, "2.0.3")

        script_files = pyproject.get("tool", {}).get("setuptools", {}).get("script-files", [])
        self.assertEqual(
            set(script_files),
            {"bin/burndvd", "bin/discinfo", "bin/ejectdisc"},
        )
        for rel in script_files:
            script_path = REPO_ROOT / rel
            self.assertTrue(script_path.is_file(), f"missing script file: {rel}")

        scripts = pyproject["project"]["scripts"]
        for expected_cmd in (
            "burndvd-queue",
            "burndvd-metadata",
            "checkrip",
            "stoprip",
            "rip-progress",
            "subocr",
            "burndvd-doctor",
        ):
            self.assertIn(expected_cmd, scripts)

    def test_ci_checks_each_shell_script_individually(self):
        ci_text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertNotIn("bash -n bin/burndvd bin/discinfo", ci_text)
        self.assertIn('bash -n "$script"', ci_text)


if __name__ == "__main__":
    unittest.main()
