"""Targeted regression tests covering DVD-1 through DVD-9 acceptance criteria.

100% hardware-free: never touches real optical drives, MakeMKV binaries, or live NAS mounts.
"""

from __future__ import annotations

import contextlib
import csv
import datetime
import errno
import fcntl
import io
import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from burndvd import metadata, nas, ripper
from burndvd.cli import checkrip, ripqueue
from burndvd.cli import watch as watch_cli
from burndvd.drive import parse_makemkv_drives
from burndvd.watch import BoxSetWatcher, WatchConfig

REPO_ROOT = Path(__file__).resolve().parent.parent


def _drv_output(*rows: str) -> list[dict]:
    return parse_makemkv_drives("\n".join(rows))


class _FakeChildProc:
    def __init__(self, pid: int, cmd: list[str], env: dict[str, str] | None = None, **extra):
        self.pid = pid
        self.cmd = list(cmd)
        self.env = dict(env or {})
        self.extra = dict(extra)
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def finish(self, code: int = 0) -> None:
        self.returncode = code


class TestDVD1WatchStrictBindingAndPreStagingRevalidation(unittest.TestCase):
    """DVD-1: Unrelated media must never be queued or staged under a configured TV show."""

    def test_watch_rejects_unrelated_unlabeled_and_wrong_season_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Test Show"
            (target_root / "Season 01").mkdir(parents=True)
            work_dir = root / "work"
            log_dir = root / "logs"

            unrelated_drives = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","RANDOM_MOVIE_2024","/dev/rdisk5"',
                'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","TEST_SHOW_S02_D1","/dev/rdisk6"',
                'DRV:2,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00003","","/dev/rdisk7"',
            )
            spawned: list[_FakeChildProc] = []

            def fake_spawn(cmd, **kw):
                proc = _FakeChildProc(pid=90001 + len(spawned), cmd=cmd, **kw)
                spawned.append(proc)
                return proc

            out_buf = io.StringIO()
            cfg = WatchConfig(
                show="Test Show",
                season=1,
                target_root=str(target_root),
                poll_interval=0.05,
                scan_wait=0.5,
                log_dir=log_dir,
                work_dir=work_dir,
                slots_dir=root / "slots",
            )
            watcher = BoxSetWatcher(
                cfg,
                list_drives_fn=lambda: unrelated_drives,
                spawn_fn=fake_spawn,
                discover_procs_fn=lambda: [],
                notify_fn=lambda _m: False,
                out=out_buf,
            )
            launched = watcher.poll_once()

            self.assertEqual(launched, [])
            self.assertEqual(spawned, [])
            self.assertEqual(watcher._launched_count, 0)
            self.assertFalse(work_dir.exists() and list(work_dir.rglob("*.csv")))
            out_txt = out_buf.getvalue()
            self.assertIn("REJECTED RANDOM_MOVIE_2024", out_txt)
            self.assertIn("REJECTED TEST_SHOW_S02_D1", out_txt)
            self.assertIn("REJECTED (unlabeled)", out_txt)

            # Second poll with same rejected discs stays quiet (no re-launch or log spam).
            out_before = out_buf.getvalue()
            self.assertEqual(watcher.poll_once(), [])
            self.assertEqual(out_buf.getvalue(), out_before)

    def test_watch_allowlist_restricts_admission_and_forwards_intent_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Test Show"
            (target_root / "Season 01").mkdir(parents=True)

            drives = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","CUSTOM_BOX_D1","/dev/rdisk5"',
                'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","TEST_SHOW_S01_D2","/dev/rdisk6"',
            )
            spawned: list[_FakeChildProc] = []

            def fake_spawn(cmd, **kw):
                proc = _FakeChildProc(pid=91001 + len(spawned), cmd=cmd, **kw)
                proc.finish(0)
                spawned.append(proc)
                return proc

            parser = watch_cli.build_parser()
            args = parser.parse_args(
                [
                    "--show", "Test Show",
                    "--season", "1",
                    "--target-root", str(target_root),
                    "--allow-volume", "CUSTOM_BOX_*",
                    "--allow-fingerprint", "a" * 64,
                ]
            )
            cfg = watch_cli.config_from_args(args, parser=parser)
            cfg.log_dir = root / "logs"
            cfg.work_dir = root / "work"
            cfg.slots_dir = root / "slots"

            watcher = BoxSetWatcher(
                cfg,
                list_drives_fn=lambda: drives,
                spawn_fn=fake_spawn,
                discover_procs_fn=lambda: [],
                notify_fn=lambda _m: False,
                out=io.StringIO(),
            )
            launched = watcher.poll_once()

            # Only CUSTOM_BOX_D1 matches explicit allowlist; TEST_SHOW_S01_D2 is excluded by explicit allowlist.
            self.assertEqual(len(launched), 1)
            self.assertEqual(launched[0].volume, "CUSTOM_BOX_D1")
            cmd = spawned[0].cmd
            self.assertIn("--strict-disc-match", cmd)
            self.assertIn("--expected-volume", cmd)
            self.assertEqual(cmd[cmd.index("--expected-volume") + 1], "CUSTOM_BOX_D1")
            self.assertIn("--allow-volume", cmd)
            self.assertIn("CUSTOM_BOX_*", cmd)
            self.assertIn("--allow-fingerprint", cmd)
            self.assertIn("a" * 64, cmd)

    def test_ripqueue_revalidates_probed_disc_against_watch_intent_before_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            q = root / "queue.csv"
            st = root / "state.json"
            target_root = root / "TV Shows" / "Test Show"
            (target_root / "Season 01").mkdir(parents=True)
            metadata.write_queue(
                q,
                title="Test Show",
                item_type="tv-season",
                season="1",
                episode_start="1",
                target_root=str(target_root),
                media_format="BD",
                notes="watch:DRVSER00001:TEST_SHOW_S01_D1",
            )
            # Probed physical disc turns out to be unrelated media (e.g. swapped disc).
            wrong_info = {
                "cinfo": {2: "UNRELATED_MOVIE_DISC"},
                "titles": {0: {9: "1:45:00", 11: "1000000000"}},
            }
            rip_called = []

            argv = [
                "burndvd-queue",
                "--queue", str(q),
                "--state", str(st),
                "--makemkvcon", sys.executable,
                "--device", "iso:/fake",
                "--non-interactive",
                "--no-eject",
                "--no-sound",
                "--no-subocr",
                "--expected-volume", "TEST_SHOW_S01_D1",
                "--strict-disc-match",
            ]
            stderr_buf = io.StringIO()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(sys, "argv", argv))
                stack.enter_context(mock.patch.object(ripqueue.signal, "signal"))
                stack.enter_context(mock.patch.object(ripqueue, "check_key_expiry"))
                stack.enter_context(
                    mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", wrong_info))
                )
                stack.enter_context(
                    mock.patch.object(ripqueue, "rip", side_effect=lambda *a, **k: rip_called.append(1))
                )
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr_buf):
                    with self.assertRaises(SystemExit) as ctx:
                        ripqueue.main()

            self.assertEqual(ctx.exception.code, 4)
            self.assertEqual(rip_called, [], "rip() must never be entered when probed disc fails intent check")
            self.assertIn("refusing optical read before staging", stderr_buf.getvalue())

    def test_ripqueue_fingerprint_allowlist_blocks_unknown_disc_before_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            q = root / "queue.csv"
            st = root / "state.json"
            target_root = root / "TV Shows" / "Test Show"
            (target_root / "Season 01").mkdir(parents=True)
            metadata.write_queue(
                q,
                title="Test Show",
                item_type="tv-season",
                season="1",
                episode_start="1",
                target_root=str(target_root),
                media_format="BD",
                notes="watch:DRVSER00001:TEST_SHOW_S01_D1",
            )
            info = {
                "cinfo": {2: "TEST_SHOW_S01_D1"},
                "titles": {0: {9: "0:42:00", 11: "2000000000"}},
            }
            rip_called = []
            argv = [
                "burndvd-queue",
                "--queue", str(q),
                "--state", str(st),
                "--makemkvcon", sys.executable,
                "--device", "iso:/fake",
                "--non-interactive",
                "--no-eject",
                "--no-sound",
                "--no-subocr",
                "--allow-fingerprint", "0" * 64,
                "--strict-disc-match",
            ]
            stderr_buf = io.StringIO()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(sys, "argv", argv))
                stack.enter_context(mock.patch.object(ripqueue.signal, "signal"))
                stack.enter_context(mock.patch.object(ripqueue, "check_key_expiry"))
                stack.enter_context(
                    mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info))
                )
                stack.enter_context(
                    mock.patch.object(ripqueue, "rip", side_effect=lambda *a, **k: rip_called.append(1))
                )
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr_buf):
                    with self.assertRaises(SystemExit) as ctx:
                        ripqueue.main()

            self.assertEqual(ctx.exception.code, 4)
            self.assertEqual(rip_called, [])
            self.assertIn("--allow-fingerprint", stderr_buf.getvalue())

    def test_dvd1_unattended_watch_rejects_partial_title_and_acronym_without_allowlist(self):
        """DVD-1 residual: partial prefix (STARLIGHT_D1) and acronym (DBS_P10_D1) must NOT be admitted unattended."""
        # Direct unit checks on volume_matches_show_intent
        ok_partial, reason_partial = metadata.volume_matches_show_intent(
            "STARLIGHT_D1", "Starlight Brigade", 1
        )
        self.assertFalse(ok_partial)
        self.assertIn("partially matches", reason_partial)

        ok_acronym, reason_acronym = metadata.volume_matches_show_intent(
            "DBS_P10_D1", "Dragon Ball Super", 1
        )
        self.assertFalse(ok_acronym)
        self.assertIn("partially matches", reason_acronym)

        # Exact normalized full show title IS admitted
        ok_exact, _ = metadata.volume_matches_show_intent(
            "STARLIGHT_BRIGADE_S01_D1", "Starlight Brigade", 1
        )
        self.assertTrue(ok_exact)

        # Explicit allowlist admits partial/alias volume cleanly
        ok_allow, _ = metadata.volume_matches_show_intent(
            "STARLIGHT_D1", "Starlight Brigade", 1, allowed_volumes=("STARLIGHT_D*",)
        )
        self.assertTrue(ok_allow)

        # Interactive confirmation opt-in retains partial matching when allow_partial=True
        ok_interactive, _ = metadata.volume_matches_show_intent(
            "STARLIGHT_D1", "Starlight Brigade", 1, allow_partial=True
        )
        self.assertTrue(ok_interactive)

        # End-to-end BoxSetWatcher: STARLIGHT_D1 & STARLIGHT_S01_D1 under Starlight Brigade -> zero launches/artifacts
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            work_dir = root / "work"
            partial_drives = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_D1","/dev/rdisk5"',
                'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","STARLIGHT_S01_D1","/dev/rdisk6"',
            )
            spawned: list[_FakeChildProc] = []
            out_buf = io.StringIO()
            cfg = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                poll_interval=0.05,
                scan_wait=0.2,
                log_dir=root / "logs",
                work_dir=work_dir,
                slots_dir=root / "slots",
            )
            watcher = BoxSetWatcher(
                cfg,
                list_drives_fn=lambda: partial_drives,
                spawn_fn=lambda cmd, **kw: spawned.append(_FakeChildProc(91501, cmd, **kw)) or spawned[-1],
                discover_procs_fn=lambda: [],
                notify_fn=lambda _m: False,
                out=out_buf,
            )
            self.assertEqual(watcher.poll_once(), [])
            self.assertEqual(spawned, [])
            self.assertFalse(work_dir.exists() and list(work_dir.rglob("*.csv")))
            self.assertIn("REJECTED STARLIGHT_D1", out_buf.getvalue())
            self.assertIn("REJECTED STARLIGHT_S01_D1", out_buf.getvalue())

            # With explicit --allow-volume, STARLIGHT_D1 IS admitted cleanly
            cfg_allow = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                poll_interval=0.05,
                scan_wait=0.2,
                log_dir=root / "logs2",
                work_dir=root / "work2",
                slots_dir=root / "slots2",
                allowed_volumes=("STARLIGHT_D1",),
            )
            spawned2: list[_FakeChildProc] = []

            def fake_spawn2(cmd, **kw):
                proc = _FakeChildProc(91601 + len(spawned2), cmd, **kw)
                proc.finish(0)
                spawned2.append(proc)
                return proc

            watcher2 = BoxSetWatcher(
                cfg_allow,
                list_drives_fn=lambda: partial_drives,
                spawn_fn=fake_spawn2,
                discover_procs_fn=lambda: [],
                notify_fn=lambda _m: False,
                out=io.StringIO(),
            )
            launched2 = watcher2.poll_once()
            self.assertEqual(len(launched2), 1)
            self.assertEqual(launched2[0].volume, "STARLIGHT_D1")
            watcher2.reap_finished()

    def test_dvd1_ripqueue_strict_match_rejects_partial_title_probed_disc_before_staging(self):
        """DVD-1 child revalidation: ripqueue --strict-disc-match rejects partial-prefix STARLIGHT_D1."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            q = root / "queue.csv"
            st = root / "state.json"
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            metadata.write_queue(
                q,
                title="Starlight Brigade",
                item_type="tv-season",
                season="1",
                episode_start="1",
                target_root=str(target_root),
                media_format="BD",
                notes="watch:DRVSER00001:STARLIGHT_D1",
            )
            partial_info = {
                "cinfo": {2: "STARLIGHT_D1"},
                "titles": {0: {9: "0:42:00", 11: "2000000000"}},
            }
            rip_called = []
            argv = [
                "burndvd-queue",
                "--queue", str(q),
                "--state", str(st),
                "--makemkvcon", sys.executable,
                "--device", "iso:/fake",
                "--non-interactive",
                "--no-eject",
                "--no-sound",
                "--no-subocr",
                "--strict-disc-match",
            ]
            stderr_buf = io.StringIO()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(sys, "argv", argv))
                stack.enter_context(mock.patch.object(ripqueue.signal, "signal"))
                stack.enter_context(mock.patch.object(ripqueue, "check_key_expiry"))
                stack.enter_context(
                    mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", partial_info))
                )
                stack.enter_context(
                    mock.patch.object(ripqueue, "rip", side_effect=lambda *a, **k: rip_called.append(1))
                )
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr_buf):
                    with self.assertRaises(SystemExit) as ctx:
                        ripqueue.main()

            self.assertEqual(ctx.exception.code, 4)
            self.assertEqual(rip_called, [])
            self.assertIn("partially matches", stderr_buf.getvalue())


class TestDVD2PublishJournalCrashRecovery(unittest.TestCase):
    """DVD-2: Fault-inject every rename/journal/retire boundary and verify clean crash recovery."""

    def setUp(self):
        ripper.clear_finalize_errors()

    def _base_args(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            state=None,
            verify=False,
            rerip_review=False,
            no_subocr=True,
            no_sound=True,
            overwrite=False,
            disc_receipts=str(root / "receipts.jsonl"),
        )

    def test_abrupt_crash_at_manifest_write_resumes_cleanly_without_self_collision(self):
        """Inject abrupt BaseException after job 1 publishes and BEFORE commit manifest is written."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            lib = root / "library"
            stage.mkdir()
            lib.mkdir()

            src1 = stage / "ep1.mkv"
            src2 = stage / "ep2.mkv"
            src1.write_bytes(b"EPISODE-ONE-CONTENT-BYTES")
            src2.write_bytes(b"EPISODE-TWO-CONTENT-BYTES")
            dst1 = lib / "Show - S01E01.mkv"
            dst2 = lib / "Show - S01E02.mkv"

            args = self._base_args(root)
            item = SimpleNamespace(title="Show", type="movie")
            result = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "ep1"), (src2, dst2, "ep2")],
                "staging": str(stage),
            }

            class _SyntheticPowerLoss(BaseException):
                pass

            with mock.patch.object(
                ripper, "_save_committed_manifest", side_effect=_SyntheticPowerLoss("crash before manifest")
            ), mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                with self.assertRaises(_SyntheticPowerLoss):
                    ripper._run_finalize(args, result, item, 1)

            # At crash boundary: dst1 has exact bytes, manifest is absent, intent is fsync'd,
            # AND src1 was NOT prematurely retired before durable commit!
            self.assertTrue(dst1.is_file())
            self.assertEqual(dst1.read_bytes(), b"EPISODE-ONE-CONTENT-BYTES")
            self.assertFalse((stage / ripper._COMMITTED_MANIFEST_NAME).exists())
            self.assertTrue((stage / ripper._PUBLISH_INTENT_NAME).is_file())
            self.assertTrue(src1.is_file(), "staged source must stay intact until commit manifest is durable")
            self.assertFalse(dst2.exists())
            self.assertTrue(src2.is_file())

            # Fresh replay with brand-new result dict (no in-memory committed_jobs) must succeed!
            fresh_result = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "ep1"), (src2, dst2, "ep2")],
                "staging": str(stage),
            }
            committed_events: list[dict] = []
            completions: list[dict] = []
            with mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok = ripper._run_finalize(
                    args,
                    fresh_result,
                    item,
                    1,
                    on_complete=completions.append,
                    on_file_committed=committed_events.append,
                )

            self.assertTrue(ok)
            self.assertEqual(len(completions), 1)
            self.assertEqual(len(committed_events), 2)
            self.assertTrue(committed_events[0].get("reconciled_from_intent"))
            self.assertEqual(dst1.read_bytes(), b"EPISODE-ONE-CONTENT-BYTES")
            self.assertEqual(dst2.read_bytes(), b"EPISODE-TWO-CONTENT-BYTES")
            self.assertFalse(stage.exists())

    def test_crash_at_manifest_write_with_source_already_gone_still_reconciles_via_intent(self):
        """Even if staged source 1 was already unlinked before manifest crash, replay reconciles via SHA-256 intent."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            lib = root / "library"
            stage.mkdir()
            lib.mkdir()

            src1 = stage / "ep1.mkv"
            src2 = stage / "ep2.mkv"
            src1.write_bytes(b"FIRST-PAYLOAD-12345")
            src2.write_bytes(b"SECOND-PAYLOAD-67890")
            dst1 = lib / "Movie.mkv"
            dst2 = lib / "Movie - Extra.mkv"

            args = self._base_args(root)
            item = SimpleNamespace(title="Movie", type="movie")
            result = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "film"), (src2, dst2, "extra")],
                "staging": str(stage),
            }
            real_move = ripper.move_with_progress

            def legacy_retiring_move(s, d, **kw):
                kw["retire_source"] = True
                return real_move(s, d, **kw)

            with mock.patch.object(ripper, "move_with_progress", side_effect=legacy_retiring_move), \
                 mock.patch.object(ripper, "_save_committed_manifest", side_effect=OSError("disk hiccup")), \
                 mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok1 = ripper._run_finalize(args, result, item, 1)

            self.assertFalse(ok1)
            self.assertTrue(dst1.is_file())
            self.assertFalse(src1.exists())
            self.assertFalse((stage / ripper._COMMITTED_MANIFEST_NAME).exists())

            fresh = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "film"), (src2, dst2, "extra")],
                "staging": str(stage),
            }
            with mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok2 = ripper._run_finalize(args, fresh, item, 1)

            self.assertTrue(ok2)
            self.assertEqual(dst1.read_bytes(), b"FIRST-PAYLOAD-12345")
            self.assertEqual(dst2.read_bytes(), b"SECOND-PAYLOAD-67890")
            self.assertFalse(stage.exists())

    def test_crash_between_commit_manifest_and_source_retirement_resumes_cleanly(self):
        """Crash after durable manifest write and BEFORE retire_staged_source completes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            lib = root / "library"
            stage.mkdir()
            lib.mkdir()

            src1 = stage / "ep1.mkv"
            src2 = stage / "ep2.mkv"
            src1.write_bytes(b"EP1-BYTES")
            src2.write_bytes(b"EP2-BYTES")
            dst1 = lib / "Movie.mkv"
            dst2 = lib / "Movie - Extra.mkv"

            args = self._base_args(root)
            item = SimpleNamespace(title="Movie", type="movie")
            result = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "film"), (src2, dst2, "extra")],
                "staging": str(stage),
            }

            with mock.patch.object(
                ripper, "retire_staged_source", side_effect=OSError("crash during retire")
            ), mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok1 = ripper._run_finalize(args, result, item, 1)

            self.assertFalse(ok1)
            self.assertTrue(dst1.is_file())
            self.assertTrue(src1.is_file())
            self.assertTrue((stage / ripper._COMMITTED_MANIFEST_NAME).is_file())

            fresh = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "film"), (src2, dst2, "extra")],
                "staging": str(stage),
            }
            with mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok2 = ripper._run_finalize(args, fresh, item, 1)

            self.assertTrue(ok2)
            self.assertEqual(dst1.read_bytes(), b"EP1-BYTES")
            self.assertEqual(dst2.read_bytes(), b"EP2-BYTES")
            self.assertFalse(stage.exists())

    def test_crash_after_intent_before_rename_replays_cleanly(self):
        """Crash after fsync'ing publish intent and BEFORE atomic rename executes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            lib = root / "library"
            stage.mkdir()
            lib.mkdir()

            src1 = stage / "ep1.mkv"
            src2 = stage / "ep2.mkv"
            src1.write_bytes(b"ONE-AAA")
            src2.write_bytes(b"TWO-BBB")
            dst1 = lib / "One.mkv"
            dst2 = lib / "Two.mkv"

            args = self._base_args(root)
            item = SimpleNamespace(title="One", type="movie")
            result = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "one"), (src2, dst2, "two")],
                "staging": str(stage),
            }

            with mock.patch.object(
                ripper, "move_with_progress", side_effect=OSError(errno.EIO, "crash before rename")
            ), mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok1 = ripper._run_finalize(args, result, item, 1)

            self.assertFalse(ok1)
            self.assertFalse(dst1.exists())
            self.assertTrue((stage / ripper._PUBLISH_INTENT_NAME).is_file())

            fresh = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "one"), (src2, dst2, "two")],
                "staging": str(stage),
            }
            with mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok2 = ripper._run_finalize(args, fresh, item, 1)

            self.assertTrue(ok2)
            self.assertEqual(dst1.read_bytes(), b"ONE-AAA")
            self.assertEqual(dst2.read_bytes(), b"TWO-BBB")
            self.assertFalse(stage.exists())

    def test_intent_reconciliation_fails_closed_on_same_size_tampered_destination(self):
        """A foreign or bit-flipped destination with identical size MUST fail closed, never clobber."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            lib = root / "library"
            stage.mkdir()
            lib.mkdir()

            src1 = stage / "ep1.mkv"
            src2 = stage / "ep2.mkv"
            src1.write_bytes(b"AUTHENTIC-BYTES!")
            src2.write_bytes(b"SECOND-FILE-DATA")
            dst1 = lib / "Show - S01E01.mkv"
            dst2 = lib / "Show - S01E02.mkv"

            args = self._base_args(root)
            item = SimpleNamespace(title="Show", type="movie")
            result = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "ep1"), (src2, dst2, "ep2")],
                "staging": str(stage),
            }

            with mock.patch.object(
                ripper, "_save_committed_manifest", side_effect=OSError("crash before manifest")
            ), mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                self.assertFalse(ripper._run_finalize(args, result, item, 1))

            # Tamper dst1 with same byte length (16 bytes) but different content!
            dst1.write_bytes(b"FOREIGN---BYTES!")
            self.assertEqual(len(dst1.read_bytes()), len(b"AUTHENTIC-BYTES!"))

            fresh = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "ep1"), (src2, dst2, "ep2")],
                "staging": str(stage),
            }
            with mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok2 = ripper._run_finalize(args, fresh, item, 1)

            self.assertFalse(ok2)
            # Foreign destination untouched, second job not published, staged sources preserved.
            self.assertEqual(dst1.read_bytes(), b"FOREIGN---BYTES!")
            self.assertFalse(dst2.exists())
            self.assertTrue(src1.is_file())
            self.assertTrue(src2.is_file())

    def test_dvd8_transient_prehash_oserror_retries_before_publish_and_preserves_crash_recovery(self):
        """DVD-8: Single transient prehash OSError is retried before mutation so intent exists on crash."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            lib = root / "library"
            stage.mkdir()
            lib.mkdir()

            src1 = stage / "ep1.mkv"
            src2 = stage / "ep2.mkv"
            src1.write_bytes(b"EPISODE-ONE-TRANSIENT")
            src2.write_bytes(b"EPISODE-TWO-TRANSIENT")
            dst1 = lib / "Show - S01E01.mkv"
            dst2 = lib / "Show - S01E02.mkv"

            args = self._base_args(root)
            item = SimpleNamespace(title="Show", type="movie")
            result = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "ep1"), (src2, dst2, "ep2")],
                "staging": str(stage),
            }

            real_sha = ripper._sha256_file
            sha_calls = [0]

            def flaky_once_sha(path):
                sha_calls[0] += 1
                if sha_calls[0] == 1:
                    raise OSError(errno.EIO, "transient prehash read error")
                return real_sha(path)

            with mock.patch.object(ripper, "_sha256_file", side_effect=flaky_once_sha), \
                 mock.patch.object(ripper, "_save_committed_manifest", side_effect=OSError("crash before manifest")), \
                 mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok1 = ripper._run_finalize(args, result, item, 1)

            self.assertFalse(ok1)
            self.assertGreaterEqual(sha_calls[0], 2, "prehash must retry transient OSError before publish")
            self.assertTrue(dst1.is_file())
            self.assertTrue((stage / ripper._PUBLISH_INTENT_NAME).is_file(), "intent MUST be written before publish")
            self.assertTrue(src1.is_file())
            self.assertFalse(dst2.exists())

            # Fresh replay reconciles dst1 via intent and publishes dst2 cleanly (zero self-collision!).
            fresh = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "ep1"), (src2, dst2, "ep2")],
                "staging": str(stage),
            }
            with mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok2 = ripper._run_finalize(args, fresh, item, 1)

            self.assertTrue(ok2)
            self.assertEqual(dst1.read_bytes(), b"EPISODE-ONE-TRANSIENT")
            self.assertEqual(dst2.read_bytes(), b"EPISODE-TWO-TRANSIENT")
            self.assertFalse(stage.exists())

    def test_dvd8_persistent_prehash_failure_aborts_fail_closed_before_any_publish(self):
        """DVD-8: Persistent prehash failure MUST abort before rename/publish (never publish un-hashed files)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            lib = root / "library"
            stage.mkdir()
            lib.mkdir()

            src1 = stage / "ep1.mkv"
            src2 = stage / "ep2.mkv"
            src1.write_bytes(b"EPISODE-ONE-SAFE")
            src2.write_bytes(b"EPISODE-TWO-SAFE")
            dst1 = lib / "Show - S01E01.mkv"
            dst2 = lib / "Show - S01E02.mkv"

            args = self._base_args(root)
            item = SimpleNamespace(title="Show", type="movie")
            result = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "ep1"), (src2, dst2, "ep2")],
                "staging": str(stage),
            }

            move_calls = []
            with mock.patch.object(ripper, "_sha256_file", side_effect=OSError(errno.EIO, "persistent read error")), \
                 mock.patch.object(ripper, "move_with_progress", side_effect=lambda *a, **k: move_calls.append(a)), \
                 mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok1 = ripper._run_finalize(args, result, item, 1)

            self.assertFalse(ok1)
            self.assertEqual(move_calls, [], "must NEVER publish/move when prehash fails")
            self.assertFalse(dst1.exists())
            self.assertFalse(dst2.exists())
            self.assertTrue(src1.is_file())
            self.assertTrue(src2.is_file())
            self.assertFalse((stage / ripper._PUBLISH_INTENT_NAME).exists())

            # Once I/O recovers, replay succeeds cleanly with both files published.
            fresh = {
                "files": [str(dst1), str(dst2)],
                "move_jobs": [(src1, dst1, "ep1"), (src2, dst2, "ep2")],
                "staging": str(stage),
            }
            with mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok2 = ripper._run_finalize(args, fresh, item, 1)

            self.assertTrue(ok2)
            self.assertEqual(dst1.read_bytes(), b"EPISODE-ONE-SAFE")
            self.assertEqual(dst2.read_bytes(), b"EPISODE-TWO-SAFE")
            self.assertFalse(stage.exists())

    def test_dvd8_intent_write_failure_aborts_before_any_publish(self):
        """DVD-8: Failure to persist publish-intent journal MUST abort before rename/publish."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            lib = root / "library"
            stage.mkdir()
            lib.mkdir()

            src1 = stage / "ep1.mkv"
            src1.write_bytes(b"EPISODE-ONE-INTENT")
            dst1 = lib / "Show - S01E01.mkv"

            args = self._base_args(root)
            item = SimpleNamespace(title="Show", type="movie")
            result = {
                "files": [str(dst1)],
                "move_jobs": [(src1, dst1, "ep1")],
                "staging": str(stage),
            }
            move_calls = []
            with mock.patch.object(ripper, "_save_publish_intents", side_effect=OSError(errno.ENOSPC, "intent write failed")), \
                 mock.patch.object(ripper, "move_with_progress", side_effect=lambda *a, **k: move_calls.append(a)), \
                 mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok = ripper._run_finalize(args, result, item, 1)

            self.assertFalse(ok)
            self.assertEqual(move_calls, [])
            self.assertFalse(dst1.exists())
            self.assertTrue(src1.is_file())


class TestDVD3IndependentWatchersUniquenessAndLock(unittest.TestCase):
    """DVD-3: Independent watchers in the same second/volume must not collide on files or E1."""

    def test_two_independent_watchers_same_second_same_volume_get_unique_paths_and_no_e1_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            work_dir = root / "work"
            log_dir = root / "logs"
            slots_dir = root / "slots"

            drive1 = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_S01_D1","/dev/rdisk5"'
            )
            drive2 = _drv_output(
                'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","STARLIGHT_BRIGADE_S01_D1","/dev/rdisk6"'
            )
            frozen_now = lambda: datetime.datetime(2026, 11, 26, 12, 0, 0)
            spawned: list[_FakeChildProc] = []

            def fake_spawn(cmd, **kw):
                proc = _FakeChildProc(pid=95001 + len(spawned), cmd=cmd, **kw)
                spawned.append(proc)
                return proc

            cfg1 = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                poll_interval=0.05,
                scan_wait=0.05,
                log_dir=log_dir,
                work_dir=work_dir,
                slots_dir=slots_dir,
            )
            cfg2 = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                poll_interval=0.05,
                scan_wait=0.05,
                log_dir=log_dir,
                work_dir=work_dir,
                slots_dir=slots_dir,
            )

            w1 = BoxSetWatcher(
                cfg1,
                list_drives_fn=lambda: drive1,
                spawn_fn=fake_spawn,
                discover_procs_fn=lambda: [],
                now_fn=frozen_now,
                notify_fn=lambda _m: False,
                out=io.StringIO(),
            )
            w2_out = io.StringIO()
            w2 = BoxSetWatcher(
                cfg2,
                list_drives_fn=lambda: drive2,
                spawn_fn=fake_spawn,
                discover_procs_fn=lambda: [],
                now_fn=frozen_now,
                notify_fn=lambda _m: False,
                out=w2_out,
            )

            # 1) Watcher 1 launches Drive 1 at E1; before child writes slot reservation,
            # Watcher 2 MUST defer rather than colliding on E1!
            launched1 = w1.poll_once()
            self.assertEqual(len(launched1), 1)
            self.assertEqual(launched1[0].episode_start, 1)

            launched2_deferred = w2.poll_once()
            self.assertEqual(launched2_deferred, [])
            self.assertIn("deferring STARLIGHT_BRIGADE_S01_D1", w2_out.getvalue())

            # 2) Once Watcher 1's child publishes slot reservation (E1..E4), Watcher 2 launches at E5
            # with completely distinct attempt directory, queue/state/log paths (even in same second + same volume!).
            metadata.write_tv_slot_reservation(
                pid=spawned[0].pid,
                title="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                episode_start=1,
                episode_count=4,
                slots_dir=slots_dir,
            )
            with mock.patch.object(metadata, "_pid_alive", side_effect=lambda p: p in {95001, 95002}):
                launched2 = w2.poll_once()

            self.assertEqual(len(launched2), 1)
            self.assertEqual(launched2[0].episode_start, 5)
            self.assertNotEqual(launched1[0].queue_path, launched2[0].queue_path)
            self.assertNotEqual(launched1[0].state_path, launched2[0].state_path)
            self.assertNotEqual(launched1[0].log_path, launched2[0].log_path)
            self.assertNotEqual(launched1[0].queue_path.parent, launched2[0].queue_path.parent)
            self.assertTrue(launched1[0].queue_path.is_file())
            self.assertTrue(launched2[0].queue_path.is_file())

            # Clean up fake active procs from module registry
            for p in spawned:
                p.finish(0)
            w1.reap_finished()
            w2.reap_finished()

    def test_dvd9_paired_watchers_with_matching_explicit_e1_reject_overlap(self):
        """DVD-9 counterexample: paired watchers with matching explicit E1 override MUST NOT both launch for E1."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            work_dir = root / "work"
            log_dir = root / "logs"
            slots_dir = root / "slots"

            drive1 = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_S01_D1","/dev/rdisk5"'
            )
            drive2 = _drv_output(
                'DRV:1,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00002","STARLIGHT_BRIGADE_S01_D2","/dev/rdisk6"'
            )
            spawned: list[_FakeChildProc] = []

            def fake_spawn(cmd, **kw):
                proc = _FakeChildProc(pid=95201 + len(spawned), cmd=cmd, **kw)
                spawned.append(proc)
                return proc

            cfg1 = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                episode_start=1,
                poll_interval=0.05,
                scan_wait=0.05,
                log_dir=log_dir,
                work_dir=work_dir,
                slots_dir=slots_dir,
            )
            cfg2 = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                episode_start=1,
                poll_interval=0.05,
                scan_wait=0.05,
                log_dir=log_dir,
                work_dir=work_dir,
                slots_dir=slots_dir,
            )

            w1 = BoxSetWatcher(
                cfg1,
                list_drives_fn=lambda: drive1,
                spawn_fn=fake_spawn,
                discover_procs_fn=lambda: [],
                notify_fn=lambda _m: False,
                out=io.StringIO(),
            )
            w2_out = io.StringIO()
            w2 = BoxSetWatcher(
                cfg2,
                list_drives_fn=lambda: drive2,
                spawn_fn=fake_spawn,
                discover_procs_fn=lambda: [],
                notify_fn=lambda _m: False,
                out=w2_out,
            )

            launched1 = w1.poll_once()
            self.assertEqual(len(launched1), 1)
            self.assertEqual(launched1[0].episode_start, 1)

            # Second watcher with identical explicit episode_start=1 MUST reject overlap before spawning!
            launched2 = w2.poll_once()
            self.assertEqual(launched2, [])
            self.assertEqual(len(spawned), 1)
            self.assertIn("REJECTED STARLIGHT_BRIGADE_S01_D2", w2_out.getvalue())
            self.assertIn("overlaps active scanning rip", w2_out.getvalue())
            self.assertEqual(len(list(work_dir.glob("attempt-*"))), 1)

            for p in spawned:
                p.finish(0)
            w1.reap_finished()
            w2.reap_finished()

    def test_dvd9_explicit_override_rejects_reserved_and_existing_but_allows_vacant_ranges(self):
        """DVD-9: Reject overlaps with active slot reservations & existing files; preserve vacant-range overrides."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            season_dir = target_root / "Season 01"
            season_dir.mkdir(parents=True)
            # Pre-existing E01, E02, and E10 on disk; E03..E09 are vacant, except active rip reserves E05..E08.
            for ep in (1, 2, 10):
                (season_dir / f"Starlight Brigade - S01E{ep:02d}.mkv").write_bytes(b"existing")

            slots_dir = root / "slots"
            metadata.write_tv_slot_reservation(
                pid=95301,
                title="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                episode_start=5,
                episode_count=4,  # Reserves E05..E08
                slots_dir=slots_dir,
            )

            drive = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_S01_D1","/dev/rdisk5"'
            )

            def _try_override(ep_start: int):
                spawned: list[_FakeChildProc] = []
                out_buf = io.StringIO()
                cfg = WatchConfig(
                    show="Starlight Brigade",
                    season=1,
                    target_root=str(target_root),
                    episode_start=ep_start,
                    poll_interval=0.05,
                    scan_wait=0.05,
                    log_dir=root / f"logs-{ep_start}",
                    work_dir=root / f"work-{ep_start}",
                    slots_dir=slots_dir,
                )
                with mock.patch.object(metadata, "_pid_alive", side_effect=lambda p: p == 95301):
                    w = BoxSetWatcher(
                        cfg,
                        list_drives_fn=lambda: drive,
                        spawn_fn=lambda cmd, **kw: spawned.append(_FakeChildProc(95400 + ep_start, cmd, **kw)) or spawned[-1],
                        discover_procs_fn=lambda: [],
                        notify_fn=lambda _m: False,
                        out=out_buf,
                    )
                    launched = w.poll_once()
                    for p in spawned:
                        p.finish(0)
                    w.reap_finished()
                return launched, out_buf.getvalue()

            # E01 & E10 already exist on disk -> rejected
            l1, out1 = _try_override(1)
            self.assertEqual(l1, [])
            self.assertIn("already exists in season directory", out1)

            l10, out10 = _try_override(10)
            self.assertEqual(l10, [])
            self.assertIn("already exists in season directory", out10)

            # E06 overlaps active reservation E05..E08 -> rejected
            l6, out6 = _try_override(6)
            self.assertEqual(l6, [])
            self.assertIn("overlaps active rip pid 95301 reservation E5-E8", out6)

            # E03 is a VACANT gap (not on disk, not in E05..E08) -> admitted!
            l3, out3 = _try_override(3)
            self.assertEqual(len(l3), 1)
            self.assertEqual(l3[0].episode_start, 3)

            # E09 is VACANT between E05..E08 and E10 -> admitted!
            l9, out9 = _try_override(9)
            self.assertEqual(len(l9), 1)
            self.assertEqual(l9[0].episode_start, 9)

    def test_dvd9_ripqueue_rejects_explicit_queue_episode_overlap_with_active_rip_before_staging(self):
        """DVD-9: Direct burndvd-queue with overlapping queue episode_start rejects before rip()/staging."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            q = root / "queue.csv"
            st = root / "state.json"
            slots_dir = root / "slots"
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            metadata.write_queue(
                q,
                title="Starlight Brigade",
                item_type="tv-season",
                season="1",
                episode_start="2",
                target_root=str(target_root),
                media_format="BD",
                notes="manual",
            )
            # Active concurrent rip reserves E01..E04
            metadata.write_tv_slot_reservation(
                pid=95501,
                title="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                episode_start=1,
                episode_count=4,
                slots_dir=slots_dir,
            )
            info = {
                "cinfo": {2: "STARLIGHT_BRIGADE_S01_D1"},
                "titles": {
                    0: {9: "0:42:00", 11: "2000000000"},
                    1: {9: "0:43:00", 11: "2000000000"},
                },
            }
            rip_called = []
            argv = [
                "burndvd-queue",
                "--queue", str(q),
                "--state", str(st),
                "--makemkvcon", sys.executable,
                "--device", "iso:/fake",
                "--non-interactive",
                "--no-eject",
                "--no-sound",
                "--no-subocr",
            ]
            stderr_buf = io.StringIO()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {metadata.ACTIVE_TV_SLOTS_ENV: str(slots_dir)}))
                stack.enter_context(mock.patch.object(metadata, "_pid_alive", side_effect=lambda p: p == 95501))
                stack.enter_context(mock.patch.object(metadata, "_discover_active_ripqueue_procs", return_value=[]))
                stack.enter_context(mock.patch.object(sys, "argv", argv))
                stack.enter_context(mock.patch.object(ripqueue.signal, "signal"))
                stack.enter_context(mock.patch.object(ripqueue, "check_key_expiry"))
                stack.enter_context(
                    mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info))
                )
                stack.enter_context(
                    mock.patch.object(ripqueue, "rip", side_effect=lambda *a, **k: rip_called.append(1))
                )
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr_buf):
                    with self.assertRaises(SystemExit) as ctx:
                        ripqueue.main()

            self.assertEqual(ctx.exception.code, 4)
            self.assertEqual(rip_called, [], "rip() must never be entered on overlapping active reservation")
            self.assertIn("overlaps active rip pid 95501 reservation E1-E4", stderr_buf.getvalue())


class TestDVD4ChildSessionIsolationAndPTYCtrlC(unittest.TestCase):
    """DVD-4: Ctrl-C on a real foreground PTY stops watcher while session-isolated child survives."""

    def test_spawn_passes_start_new_session_true_when_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            drive = _drv_output(
                'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001","STARLIGHT_BRIGADE_S01_D1","/dev/rdisk5"'
            )
            seen_kwargs: list[dict] = []

            def spy_spawn(cmd, **kw):
                seen_kwargs.append(dict(kw))
                proc = _FakeChildProc(pid=96001, cmd=cmd, env=kw.get("env") or {})
                proc.finish(0)
                return proc

            cfg = WatchConfig(
                show="Starlight Brigade",
                season=1,
                target_root=str(target_root),
                log_dir=root / "logs",
                work_dir=root / "work",
                slots_dir=root / "slots",
            )
            watcher = BoxSetWatcher(
                cfg,
                list_drives_fn=lambda: drive,
                spawn_fn=spy_spawn,
                discover_procs_fn=lambda: [],
                notify_fn=lambda _m: False,
                out=io.StringIO(),
            )
            launched = watcher.poll_once()
            self.assertEqual(len(launched), 1)
            self.assertIs(seen_kwargs[0].get("start_new_session"), True)
            watcher.reap_finished()

    def test_synthetic_pty_ctrl_c_stops_watcher_while_isolated_child_survives(self):
        """Real PTY + real subprocess.Popen: Ctrl-C (SIGINT) stops watcher, child stays alive in new session."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target_root = root / "TV Shows" / "Starlight Brigade"
            (target_root / "Season 01").mkdir(parents=True)
            child_marker = root / "child-ready.json"
            fake_ripqueue = root / "fake_ripqueue.py"
            fake_ripqueue.write_text(
                "import json, os, pathlib, sys, time\n"
                f"pathlib.Path({str(child_marker)!r}).write_text("
                "json.dumps({'pid': os.getpid(), 'sid': os.getsid(0), 'pgid': os.getpgrp()}))\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            runner_script = root / "run_watcher_in_pty.py"
            runner_script.write_text(
                "import fcntl, json, os, pathlib, sys, termios\n"
                "try:\n"
                "    fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
                "except OSError:\n"
                "    pass\n"
                "from burndvd.drive import parse_makemkv_drives\n"
                "from burndvd.watch import BoxSetWatcher, WatchConfig\n"
                "drives = parse_makemkv_drives("
                "'DRV:0,2,999,12,\"BD-RE HL-DT-ST BD-RE BU40N 1.03 DRVSER00001\",\"STARLIGHT_BRIGADE_S01_D1\",\"/dev/rdisk5\"')\n"
                f"cfg = WatchConfig(show='Starlight Brigade', season=1, target_root={str(target_root)!r}, "
                f"poll_interval=0.1, scan_wait=1.0, ripqueue_py={str(fake_ripqueue)!r}, "
                f"log_dir=pathlib.Path({str(root / 'logs')!r}), "
                f"work_dir=pathlib.Path({str(root / 'work')!r}), "
                f"slots_dir=pathlib.Path({str(root / 'slots')!r}))\n"
                "w = BoxSetWatcher(cfg, list_drives_fn=lambda: drives, discover_procs_fn=lambda: [], "
                "notify_fn=lambda _m: False)\n"
                "summary = w.run()\n"
                "print('WATCHER_SUMMARY:' + json.dumps({\n"
                "  'parent_sid': os.getsid(0),\n"
                "  'stopped_by_signal': summary.stopped_by_signal,\n"
                "  'active_pids': [r.proc.pid for r in summary.active_at_exit],\n"
                "}), flush=True)\n",
                encoding="utf-8",
            )

            master_fd, slave_fd = pty.openpty()
            env = dict(os.environ)
            env["PYTHONPATH"] = str(REPO_ROOT / "src") + (
                ":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
            )
            parent_proc = None
            child_pid: int | None = None
            chunks: list[bytes] = []
            stop_reader = threading.Event()

            def _drain_master() -> None:
                while not stop_reader.is_set():
                    try:
                        ready, _, _ = select.select([master_fd], [], [], 0.05)
                    except (OSError, ValueError):
                        break
                    if not ready:
                        continue
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    chunks.append(data)

            reader_thread = threading.Thread(target=_drain_master, daemon=True)
            reader_thread.start()
            try:
                parent_proc = subprocess.Popen(
                    [sys.executable, str(runner_script)],
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    start_new_session=True,
                    env=env,
                    close_fds=True,
                )
                os.close(slave_fd)
                slave_fd = -1

                # Wait until the session-isolated child worker starts and writes its marker.
                deadline = time.monotonic() + 10.0
                while not child_marker.is_file() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(child_marker.is_file(), "child worker did not start within 10s")
                child_info = json.loads(child_marker.read_text(encoding="utf-8"))
                child_pid = int(child_info["pid"])
                child_sid = int(child_info["sid"])

                # Send real Ctrl-C (0x03) through the controlling PTY line discipline.
                os.write(master_fd, b"\x03")
                try:
                    parent_proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    os.killpg(parent_proc.pid, signal.SIGINT)
                    parent_proc.wait(timeout=5.0)

                time.sleep(0.2)
                stop_reader.set()
                reader_thread.join(timeout=2.0)
                pty_output = b"".join(chunks).decode("utf-8", errors="replace")

                self.assertEqual(parent_proc.returncode, 0, f"PTY output:\n{pty_output}")
                self.assertIn("left running safely", pty_output)
                self.assertIn(f"pid {child_pid}", pty_output)

                # Verify the child process is STILL ALIVE in its own isolated session!
                os.kill(child_pid, 0)
                summary_line = next(
                    (ln for ln in pty_output.splitlines() if "WATCHER_SUMMARY:" in ln),
                    "",
                )
                self.assertTrue(summary_line, f"missing summary line in:\n{pty_output}")
                summary_data = json.loads(summary_line.split("WATCHER_SUMMARY:", 1)[1])
                self.assertTrue(summary_data["stopped_by_signal"])
                self.assertEqual(summary_data["active_pids"], [child_pid])
                self.assertNotEqual(summary_data["parent_sid"], child_sid)
            finally:
                stop_reader.set()
                reader_thread.join(timeout=1.0)
                if slave_fd >= 0:
                    os.close(slave_fd)
                os.close(master_fd)
                if parent_proc is not None and parent_proc.poll() is None:
                    parent_proc.kill()
                    parent_proc.wait(timeout=5.0)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


class TestDVD6AndDVD7CheckripAttributionAndJsonSchema(unittest.TestCase):
    """DVD-6: Duplicate volume label never steals another drive's rip row.
    DVD-7: Single-rip JSON preserves legacy flat keys consistently + supports opt-in version 2.
    """

    def test_duplicate_volume_label_never_steals_exact_device_match(self):
        """Reproduces exact DVD-6 fixture: two drives with SAME label, rip bound to /dev/disk6."""
        fake_rips = [
            {
                "pid": 4242,
                "state": "/tmp/s6.json",
                "queue": None,
                "device": "disc:1",
                "drive_device": "/dev/disk6",
                "drive_serial": "DRVSER00002",
                "title": "Starlight Brigade",
                "season": 1,
                "volume": "DUP_DISC_LABEL",
                "episode_range": "S01E01-E04",
                "log_path": None,
                "staging": None,
            }
        ]
        fake_drives = [
            {
                "slot": 1,
                "model": "HL-DT-ST BD-RE BU40N 1.03",
                "media_type": "BD-ROM",
                "device": "/dev/disk5",
                "volume": "DUP_DISC_LABEL",
            },
            {
                "slot": 2,
                "model": "HL-DT-ST BD-RE BU40N 1.03",
                "media_type": "BD-ROM",
                "device": "/dev/disk6",
                "volume": "DUP_DISC_LABEL",
            },
        ]
        with mock.patch.object(checkrip, "active_rips", return_value=fake_rips), \
             mock.patch.object(checkrip, "active_watchers", return_value=[]), \
             mock.patch.object(checkrip, "connected_optical_drives", return_value=fake_drives), \
             mock.patch.object(checkrip, "_active_claim_devices", return_value={}), \
             mock.patch.object(checkrip, "measure_rip_speeds", return_value={}):
            buf = io.StringIO()
            with mock.patch.object(sys, "argv", ["checkrip", "--json"]), contextlib.redirect_stdout(buf):
                rc = checkrip.main()

        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        by_dev = {d["device"]: d for d in payload["drives"]}
        self.assertEqual(by_dev["/dev/disk5"]["status"], "READY")
        self.assertIsNone(by_dev["/dev/disk5"]["pid"])
        self.assertEqual(by_dev["/dev/disk6"]["status"], "STARTING")
        self.assertEqual(by_dev["/dev/disk6"]["pid"], 4242)

    def test_conflicting_hardware_identity_and_ambiguous_duplicate_labels_never_misattribute(self):
        # Case A: Rip explicitly bound to /dev/disk6 must NOT fall back by label onto /dev/disk5.
        rips_conflict = [
            {
                "pid": 5151,
                "device": "disc:1",
                "drive_device": "/dev/disk6",
                "drive_serial": "DRVSER00002",
                "volume": "SHARED_VOL",
                "stage": "RIPPING",
            }
        ]
        drives_only_5 = [
            {"slot": 1, "device": "/dev/disk5", "media_type": "BD-ROM", "volume": "SHARED_VOL"},
        ]
        rows_conflict = checkrip._match_drives_to_rips(drives_only_5, rips_conflict)
        self.assertEqual(len(rows_conflict), 2)
        self.assertEqual(rows_conflict[0]["drive"]["device"], "/dev/disk5")
        self.assertIsNone(rows_conflict[0]["rip"])
        self.assertEqual(rows_conflict[1]["drive"]["device"], "/dev/disk6")
        self.assertEqual(rows_conflict[1]["rip"]["pid"], 5151)

        # Case B: Identity-less rip + TWO drives sharing the same label -> ambiguous, neither steals.
        rips_noid = [
            {
                "pid": 6161,
                "device": "disc:0",
                "drive_device": None,
                "drive_serial": None,
                "volume": "SHARED_VOL",
                "stage": "RIPPING",
            }
        ]
        drives_dup = [
            {"slot": 1, "device": "/dev/disk5", "media_type": "BD-ROM", "volume": "SHARED_VOL"},
            {"slot": 2, "device": "/dev/disk6", "media_type": "BD-ROM", "volume": "SHARED_VOL"},
        ]
        rows_ambig = checkrip._match_drives_to_rips(drives_dup, rips_noid)
        self.assertIsNone(rows_ambig[0]["rip"])
        self.assertIsNone(rows_ambig[1]["rip"])
        self.assertEqual(rows_ambig[2]["rip"]["pid"], 6161)

        # Case C: Identity-less rip + ONE unambiguous drive matching label -> matches cleanly.
        drives_unique = [
            {"slot": 1, "device": "/dev/disk5", "media_type": "BD-ROM", "volume": "SHARED_VOL"},
            {"slot": 2, "device": "/dev/disk6", "media_type": "BD-ROM", "volume": "OTHER_VOL"},
        ]
        rows_ok = checkrip._match_drives_to_rips(drives_unique, rips_noid)
        self.assertEqual(rows_ok[0]["rip"]["pid"], 6161)
        self.assertIsNone(rows_ok[1]["rip"])

    def test_single_rip_json_schema_stable_with_and_without_drive_discovery_and_versioned(self):
        """DVD-7: Top-level pid/stage/speed_mbps stay intact whether discovery returns 0 or N drives."""
        fake_rips = [
            {
                "pid": 7777,
                "state": "/tmp/s1.json",
                "queue": None,
                "device": "disc:0",
                "drive_device": "/dev/disk5",
                "drive_serial": "DRVSER00001",
                "title": "Example Show",
                "season": 1,
                "volume": "EXAMPLE_S01_D1",
                "episode_range": "S01E01-E03",
                "log_path": None,
                "staging": "/tmp/stg1",
            }
        ]
        discovered_drives = [
            {
                "slot": 1,
                "model": "HL-DT-ST BD-RE BU40N 1.03",
                "media_type": "BD-ROM",
                "device": "/dev/disk5",
                "volume": "EXAMPLE_S01_D1",
            }
        ]

        outputs = {}
        for key, drv_rows in (("no_discovery", []), ("with_discovery", discovered_drives)):
            with mock.patch.object(checkrip, "active_rips", return_value=fake_rips), \
                 mock.patch.object(checkrip, "active_watchers", return_value=[]), \
                 mock.patch.object(checkrip, "connected_optical_drives", return_value=drv_rows), \
                 mock.patch.object(checkrip, "_active_claim_devices", return_value={}), \
                 mock.patch.object(checkrip, "measure_rip_speeds", return_value={"/tmp/stg1": 17.3}):
                buf = io.StringIO()
                with mock.patch.object(sys, "argv", ["checkrip", "--json"]), contextlib.redirect_stdout(buf):
                    rc = checkrip.main()
            self.assertEqual(rc, 0)
            outputs[key] = json.loads(buf.getvalue())

        for label, payload in outputs.items():
            self.assertEqual(payload["schema_version"], 1, label)
            self.assertIs(payload["active"], True, label)
            # Legacy flat single-rip keys preserved consistently in both snapshots!
            self.assertEqual(payload["pid"], 7777, label)
            self.assertEqual(payload["stage"], "RIPPING", label)
            self.assertEqual(payload["speed_mbps"], 17.3, label)
            self.assertEqual(payload["drive_device"], "/dev/disk5", label)
            # Structured arrays also appended consistently!
            self.assertEqual(len(payload["rips"]), 1, label)
            self.assertEqual(payload["rips"][0]["pid"], 7777, label)
            self.assertEqual(len(payload["drives"]), 1, label)

        # Opt-in --json-version 2 returns strict versioned envelope only.
        with mock.patch.object(checkrip, "active_rips", return_value=fake_rips), \
             mock.patch.object(checkrip, "active_watchers", return_value=[]), \
             mock.patch.object(checkrip, "connected_optical_drives", return_value=discovered_drives), \
             mock.patch.object(checkrip, "_active_claim_devices", return_value={}), \
             mock.patch.object(checkrip, "measure_rip_speeds", return_value={"/tmp/stg1": 17.3}):
            buf_v2 = io.StringIO()
            with mock.patch.object(sys, "argv", ["checkrip", "--json", "--json-version", "2"]), \
                 contextlib.redirect_stdout(buf_v2):
                rc_v2 = checkrip.main()
        self.assertEqual(rc_v2, 0)
        v2 = json.loads(buf_v2.getvalue())
        self.assertEqual(v2["schema_version"], 2)
        self.assertNotIn("pid", v2)
        self.assertEqual(v2["rips"][0]["pid"], 7777)
        self.assertEqual(v2["drives"][0]["pid"], 7777)

    def test_checkrip_avoids_duplicate_empty_ready_row_when_makemkv_unmounts_active_drive(self):
        """When MakeMKV auto-unmounts /dev/diskN during active ripping, drutil reports device=None; no extra bogus READY row."""
        # Case 1: Single active rip on Drive 1 (unmounted by MakeMKV: device=None, volume=None), Drive 2 idle
        fake_rips = [
            {
                "pid": 8801,
                "state": "/tmp/s1.json",
                "queue": None,
                "device": "disc:0",
                "drive_device": "/dev/disk5",
                "drive_serial": "DRVSER00001",
                "title": "Starlight Brigade",
                "season": 1,
                "volume": "STARLIGHT_BRIGADE_S01_D1",
                "episode_range": "S01E01-E04",
                "log_path": None,
                "staging": "/tmp/stg1",
            }
        ]
        unmounted_drives = [
            {
                "slot": 1,
                "model": "HL-DT-ST BD-RE BU40N 1.03",
                "media_type": "BD-ROM",
                "device": None,
                "volume": "STARLIGHT_BRIGADE_S01_D1",
            },
            {
                "slot": 2,
                "model": "HL-DT-ST BD-RE BU40N 1.03",
                "media_type": None,
                "device": None,
                "volume": None,
            },
        ]
        with mock.patch.object(checkrip, "active_rips", return_value=fake_rips), \
             mock.patch.object(checkrip, "active_watchers", return_value=[]), \
             mock.patch.object(checkrip, "connected_optical_drives", return_value=unmounted_drives), \
             mock.patch.object(checkrip, "_active_claim_devices", return_value={}), \
             mock.patch.object(checkrip, "measure_rip_speeds", return_value={"/tmp/stg1": 18.5}):
            buf_txt = io.StringIO()
            with mock.patch.object(sys, "argv", ["checkrip"]), contextlib.redirect_stdout(buf_txt):
                self.assertEqual(checkrip.main(), 0)
            txt = buf_txt.getvalue()
            self.assertIn("Optical Drives (1/2 ripping)", txt)
            self.assertIn("Drive 1  /dev/disk5 (DRVSER00001)  RIPPING", txt)
            self.assertIn("Drive 2  (empty)     IDLE", txt)
            self.assertNotIn("READY", txt)

            buf_json = io.StringIO()
            with mock.patch.object(sys, "argv", ["checkrip", "--json"]), contextlib.redirect_stdout(buf_json):
                self.assertEqual(checkrip.main(), 0)
            j = json.loads(buf_json.getvalue())
            self.assertEqual(len(j["drives"]), 2)
            self.assertEqual(j["drives"][0]["slot"], 1)
            self.assertEqual(j["drives"][0]["device"], "/dev/disk5")
            self.assertEqual(j["drives"][0]["status"], "RIPPING")
            self.assertEqual(j["drives"][0]["pid"], 8801)
            self.assertEqual(j["drives"][1]["status"], "IDLE")

        # Case 2: Both drives actively ripping & auto-unmounted by MakeMKV (device=None, volume=None)
        both_rips = [
            {
                "pid": 8801,
                "device": "disc:0",
                "drive_device": "/dev/disk5",
                "drive_serial": "DRVSER00001",
                "volume": "DISC_ONE",
                "stage": "RIPPING",
            },
            {
                "pid": 8802,
                "device": "disc:1",
                "drive_device": "/dev/disk6",
                "drive_serial": "DRVSER00002",
                "volume": "DISC_TWO",
                "stage": "RIPPING",
            },
        ]
        both_unmounted = [
            {"slot": 1, "media_type": "BD-ROM", "device": None, "volume": None},
            {"slot": 2, "media_type": "BD-ROM", "device": None, "volume": None},
        ]
        rows = checkrip._match_drives_to_rips(both_unmounted, both_rips)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["rip"]["pid"], 8801)
        self.assertEqual(rows[1]["rip"]["pid"], 8802)

        # Case 3: Unmounted drive with contradicting explicit volume MUST NOT be misattributed!
        mismatch_drives = [
            {"slot": 1, "media_type": "BD-ROM", "device": None, "volume": "DIFFERENT_DISC"},
        ]
        rows_mismatch = checkrip._match_drives_to_rips(mismatch_drives, [both_rips[0]])
        self.assertEqual(len(rows_mismatch), 2)
        self.assertIsNone(rows_mismatch[0]["rip"])
        self.assertEqual(rows_mismatch[1]["rip"]["pid"], 8801)


class TestDVD5DocsDemoAndCiContract(unittest.TestCase):
    """DVD-5: README documents --watch, dev install, prerequisites, synthetic demo, and CI runs demo."""

    def test_readme_and_ci_cover_watch_dev_install_and_synthetic_demo(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("--watch", readme)
        self.assertIn('pip install -e ".[dev]"', readme)
        self.assertIn("examples/synthetic_demo.py", readme)
        self.assertIn("Limitations", readme)

        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("examples/synthetic_demo.py", ci)
        self.assertIn("--watch --help", ci)

        demo = REPO_ROOT / "examples" / "synthetic_demo.py"
        self.assertTrue(demo.is_file())


if __name__ == "__main__":
    unittest.main()
