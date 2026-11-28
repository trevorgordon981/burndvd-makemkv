"""Tests for Improvement #3: early optical drive release before local split/classify/verify/NAS,
accurate eject confirmation in `<state>.drive_released`, no optical reread on downstream failures,
and safe TV episode reservation across early release.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from burndvd import drive, metadata, ripper, selection
from burndvd.cli import ripqueue
from burndvd.models import FallbackResult, QueueItem

REPO_ROOT = Path(__file__).resolve().parent.parent
_REAL_POPEN = subprocess.Popen


def _makemkv_popen_dispatch(handler):
    """Wrap `subprocess.Popen` so only MakeMKV `mkv` invocations hit `handler`."""
    def _dispatch(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and "mkv" in cmd:
            return handler(cmd, *args, **kwargs)
        return _REAL_POPEN(cmd, *args, **kwargs)
    return _dispatch


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
    r = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if r.returncode == 0:
        return False
    if r.returncode == 42:
        return True
    raise AssertionError(f"unexpected rc={r.returncode}: stderr={r.stderr}")


def _external_compute_next_ep(
    *,
    title: str,
    season: int,
    target_root: Path,
    season_dir: Path,
    slots_dir: Path,
) -> dict:
    """Run `metadata.compute_next_tv_episode` in a separate PID to simulate a concurrent rip."""
    code = f"""
import json
from pathlib import Path
from burndvd import metadata
res = metadata.compute_next_tv_episode(
    title={title!r},
    season={season!r},
    target_root={str(target_root)!r},
    season_dir=Path({str(season_dir)!r}),
    slots_dir=Path({str(slots_dir)!r}),
    active_processes=[],
    wait_scan_s=0.0,
)
print(json.dumps(res))
"""
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    r = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if r.returncode != 0:
        raise AssertionError(f"subprocess failed rc={r.returncode}: {r.stderr}")
    return json.loads(r.stdout)


class _FakeMakemkvProc:
    def __init__(self, lines: list[str], returncode: int = 0):
        self.stdout = io.StringIO("".join(lines))
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


def _make_playall_chapters(n_eps: int = 4, ep_len_s: float = 1380.0) -> list[float]:
    """Generate chapter timestamps with clear intra-episode shape ratio (stride 4)."""
    # Offsets inside each 23-min (1380s) episode: 0s, 90s (intro), 150s, 750s -> gaps 90, 60, 600, 630
    intra = (0.0, 90.0, 150.0, 750.0)
    marks: list[float] = []
    for e in range(n_eps):
        base = e * ep_len_s
        for off in intra:
            marks.append(base + off)
    marks.append(n_eps * ep_len_s)
    return marks


def _make_rip_args(
    *,
    device: str = "iso:/fake.iso",
    state_path: Path | None = None,
    sync_move: bool = False,
    verify: bool = False,
    no_eject: bool = False,
    no_split_playall: bool = False,
    allow_mkvmerge_fallback: bool = True,
) -> argparse.Namespace:
    return argparse.Namespace(
        makemkvcon="/fake/makemkvcon",
        device=device,
        state=str(state_path) if state_path else "",
        overwrite=False,
        rerip_review=False,
        review_root=None,
        min_free_gb=1,
        no_eject=no_eject,
        sync_move=sync_move,
        no_sound=True,
        no_subocr=True,
        no_split_playall=no_split_playall,
        no_extras=False,
        allow_mkvmerge_fallback=allow_mkvmerge_fallback,
        non_interactive=True,
        verify=verify,
        current_disc_fingerprint="deadbeef12345678",
    )


def _enter_all(stack: contextlib.ExitStack, cms: list) -> None:
    for cm in cms:
        stack.enter_context(cm)


class TestEarlyReleaseBeforeSplit(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_on_optical_done_fires_after_makemkv_and_before_playall_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging_base = root / "staging"
            slots_dir = root / "slots"
            show_root = root / "TV Shows" / "Playall Show"
            season_dir = show_root / "Season 01"
            season_dir.mkdir(parents=True)

            item = QueueItem(
                title="Playall Show",
                type="tv-season",
                discs=1,
                target_root=str(show_root),
                format="DVD",
                season=1,
                episode_start=1,
            )
            info = {
                "cinfo": {2: "PLAYALL_SHOW_S1_D1"},
                "titles": {0: {9: "1:32:00", 11: "4000000000"}},
            }
            args = _make_rip_args(state_path=root / "state.json")
            state: dict = {}
            events: list[str] = []
            res_at_optical_done: dict = {}
            res_at_split: dict = {}
            marks = _make_playall_chapters(n_eps=4, ep_len_s=1380.0)

            def fake_popen(cmd, **_kw):
                events.append("makemkv")
                stage_dir = Path(cmd[-1])
                (stage_dir / "title_t00.mkv").write_bytes(b"playall-blob")
                return _FakeMakemkvProc(
                    ['MSG:5036,0,2,"Copy complete. 1 titles saved.","%1","1"\n'],
                    returncode=0,
                )

            def fake_split(_args, rips, staging):
                events.append("playall_split")
                slot_file = slots_dir / f"pid-{os.getpid()}.json"
                if slot_file.is_file():
                    res_at_split.update(json.loads(slot_file.read_text(encoding="utf-8")))
                out = []
                for idx in range(4):
                    p = staging / f"title_t00_split_t{900 + idx:03d}.mkv"
                    p.write_bytes(f"ep-{idx + 1}".encode())
                    out.append(p)
                return out

            def on_optical_done():
                events.append("on_optical_done")
                slot_file = slots_dir / f"pid-{os.getpid()}.json"
                if slot_file.is_file():
                    res_at_optical_done.update(
                        json.loads(slot_file.read_text(encoding="utf-8"))
                    )

            def fake_ffprobe_dur(path_str: str) -> float:
                return 1380.0 if "_split_" in path_str else 5520.0

            with contextlib.ExitStack() as stack:
                _enter_all(
                    stack,
                    [
                        mock.patch.dict(os.environ, {"BURNDVD_ACTIVE_TV_SLOTS_DIR": str(slots_dir)}),
                        mock.patch.object(ripper, "LOCAL_STAGING_BASE", staging_base),
                        mock.patch.object(ripper, "_free_gb", return_value=500.0),
                        mock.patch.object(ripper.subprocess, "Popen", side_effect=_makemkv_popen_dispatch(fake_popen)),
                        mock.patch.object(selection, "ffprobe_dur_s", side_effect=fake_ffprobe_dur),
                        mock.patch.object(ripper, "ffprobe_dur_s", side_effect=fake_ffprobe_dur),
                        mock.patch.object(selection, "chapter_marks_s", return_value=marks),
                        mock.patch.object(ripper, "split_chaptered_playall", side_effect=fake_split),
                        mock.patch.object(ripper, "_alt_playlist_duplicate", return_value=False),
                        mock.patch.object(ripper, "existing_content_collision", return_value=(None, None)),
                        contextlib.redirect_stdout(io.StringIO()),
                    ],
                )
                ok, payload = ripper.rip(
                    args,
                    item,
                    info,
                    [0],
                    season_dir,
                    state,
                    1,
                    on_optical_done=on_optical_done,
                )

            self.assertTrue(ok, f"rip failed: {payload}")
            self.assertEqual(events, ["makemkv", "on_optical_done", "playall_split"])
            # Slot reservation MUST already reserve all 4 play-all episodes BEFORE optical release.
            self.assertEqual(res_at_optical_done.get("episode_start"), 1)
            self.assertEqual(res_at_optical_done.get("episode_count"), 4)
            self.assertEqual(res_at_optical_done.get("next_episode"), 5)
            self.assertEqual(res_at_split.get("episode_count"), 4)
            self.assertEqual(len(payload["files"]), 4)
            self.assertEqual(
                [Path(f).name for f in payload["files"]],
                [
                    "Playall Show - S01E01.mkv",
                    "Playall Show - S01E02.mkv",
                    "Playall Show - S01E03.mkv",
                    "Playall Show - S01E04.mkv",
                ],
            )
            self.assertEqual(state["disc_episode_counts"]["Playall Show||S01"]["1"], 4)
            metadata.clear_tv_slot_reservation(os.getpid(), slots_dir=slots_dir)

    def test_on_optical_done_waits_for_mkvmerge_fallback_and_never_fires_on_optical_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging_base = root / "staging"
            slots_dir = root / "slots"
            show_root = root / "TV Shows" / "Fallback Show"
            season_dir = show_root / "Season 01"
            season_dir.mkdir(parents=True)

            item = QueueItem(
                title="Fallback Show",
                type="tv-season",
                discs=1,
                target_root=str(show_root),
                format="BD",
                season=1,
                episode_start=1,
            )
            info = {
                "cinfo": {2: "FALLBACK_SHOW_S1_D1"},
                "titles": {
                    0: {9: "0:23:00", 11: "2000000000", 16: "00001.mpls"},
                    1: {9: "0:23:00", 11: "2000000000", 16: "00002.mpls"},
                },
            }
            args = _make_rip_args(state_path=root / "state.json")
            events: list[str] = []

            def failing_makemkv(cmd, **_kw):
                events.append("makemkv")
                return _FakeMakemkvProc(
                    ['MSG:5003,0,0,"Failed to save title 0"\n'], returncode=1
                )

            def succeeding_fallback(_args, _item, staging):
                events.append("mkvmerge_fallback")
                fb_dir = Path(staging) / "mkvmerge-fallback"
                fb_dir.mkdir(parents=True, exist_ok=True)
                f0 = fb_dir / "Fallback Show_t00.mkv"
                f1 = fb_dir / "Fallback Show_t01.mkv"
                f0.write_bytes(b"ep1")
                f1.write_bytes(b"ep2")
                return FallbackResult(
                    ok=True,
                    files=[f0, f1],
                    playlists_remuxed=2,
                    file_playlists={str(f0): "00001.mpls", str(f1): "00002.mpls"},
                )

            def fake_split(_args, rips, _staging):
                events.append("playall_split")
                return rips

            with contextlib.ExitStack() as stack:
                _enter_all(
                    stack,
                    [
                        mock.patch.dict(os.environ, {"BURNDVD_ACTIVE_TV_SLOTS_DIR": str(slots_dir)}),
                        mock.patch.object(ripper, "LOCAL_STAGING_BASE", staging_base),
                        mock.patch.object(ripper, "_free_gb", return_value=500.0),
                        mock.patch.object(ripper.subprocess, "Popen", side_effect=_makemkv_popen_dispatch(failing_makemkv)),
                        mock.patch.object(ripper, "mkvmerge_fallback_rip", side_effect=succeeding_fallback),
                        mock.patch.object(ripper, "ffprobe_dur_s", return_value=1380.0),
                        mock.patch.object(selection, "ffprobe_dur_s", return_value=1380.0),
                        mock.patch.object(ripper, "split_chaptered_playall", side_effect=fake_split),
                        mock.patch.object(ripper, "_alt_playlist_duplicate", return_value=False),
                        mock.patch.object(ripper, "existing_content_collision", return_value=(None, None)),
                        contextlib.redirect_stdout(io.StringIO()),
                    ],
                )
                ok, payload = ripper.rip(
                    args,
                    item,
                    info,
                    [0, 1],
                    season_dir,
                    {},
                    1,
                    on_optical_done=lambda: events.append("on_optical_done"),
                )

            self.assertTrue(ok, f"fallback rip failed: {payload}")
            # Strictly AFTER fallback, strictly BEFORE local split.
            self.assertEqual(
                events,
                ["makemkv", "mkvmerge_fallback", "on_optical_done", "playall_split"],
            )
            metadata.clear_tv_slot_reservation(os.getpid(), slots_dir=slots_dir)

            # Sub-case: both MakeMKV AND fallback fail -> on_optical_done MUST NOT fire!
            fail_events: list[str] = []

            def fail_mk(cmd, **_kw):
                fail_events.append("makemkv")
                return _FakeMakemkvProc([], returncode=1)

            with contextlib.ExitStack() as stack:
                _enter_all(
                    stack,
                    [
                        mock.patch.dict(os.environ, {"BURNDVD_ACTIVE_TV_SLOTS_DIR": str(slots_dir)}),
                        mock.patch.object(ripper, "LOCAL_STAGING_BASE", staging_base),
                        mock.patch.object(ripper, "_free_gb", return_value=500.0),
                        mock.patch.object(ripper.subprocess, "Popen", side_effect=_makemkv_popen_dispatch(fail_mk)),
                        mock.patch.object(
                            ripper,
                            "mkvmerge_fallback_rip",
                            return_value=FallbackResult(ok=False, error="encrypted"),
                        ),
                        contextlib.redirect_stdout(io.StringIO()),
                    ],
                )
                ok2, _err2 = ripper.rip(
                    args,
                    item,
                    info,
                    [0, 1],
                    season_dir,
                    {},
                    1,
                    on_optical_done=lambda: fail_events.append("on_optical_done"),
                )
            self.assertFalse(ok2)
            self.assertEqual(fail_events, ["makemkv"])
            self.assertNotIn("on_optical_done", fail_events)

    def test_cli_releases_drive_before_playall_split_and_preserves_tv_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging_base = root / "staging"
            lock_dir = root / "locks"
            slots_dir = root / "slots"
            claim_dir = root / "claims" / "serial-DRVPLAYALL1.claim"
            claim_dir.mkdir(parents=True)
            show_root = root / "nas" / "TV Shows" / "Early Split Show"
            season_dir = show_root / "Season 01"
            season_dir.mkdir(parents=True)
            state_path = root / "state.json"
            (claim_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
            (claim_dir / "state").write_text(str(state_path), encoding="utf-8")
            (claim_dir / "dev").write_text("/dev/disk5", encoding="utf-8")
            (claim_dir / "serial").write_text("DRVPLAYALL1", encoding="utf-8")

            qpath = root / "queue.csv"
            qpath.write_text(
                f"title,type,season,episode_start,discs,target_root,format\n"
                f"Early Split Show,tv-season,1,1,1,{show_root},DVD\n",
                encoding="utf-8",
            )
            info = {
                "cinfo": {2: "EARLY_SPLIT_SHOW_S1_D1"},
                "titles": {0: {9: "1:32:00", 11: "4000000000"}},
            }
            marks = _make_playall_chapters(n_eps=4, ep_len_s=1380.0)
            observed: dict[str, object] = {}
            events: list[str] = []

            def fake_popen(cmd, **_kw):
                events.append("makemkv")
                observed["locked_during_makemkv"] = _external_slot_locked(
                    "disc:1",
                    lock_dir,
                    drive_device="/dev/disk5",
                    drive_serial="DRVPLAYALL1",
                )
                stage_dir = Path(cmd[-1])
                (stage_dir / "title_t00.mkv").write_bytes(b"playall-raw")
                return _FakeMakemkvProc(
                    ['MSG:5036,0,2,"Copy complete. 1 titles saved.","%1","1"\n'],
                    returncode=0,
                )

            def fake_split(_args, rips, staging):
                events.append("playall_split")
                marker_path = Path(f"{state_path}.drive_released")
                observed["marker_exists_during_split"] = marker_path.is_file()
                if marker_path.is_file():
                    observed["marker_during_split"] = json.loads(
                        marker_path.read_text(encoding="utf-8")
                    )
                observed["claim_exists_during_split"] = claim_dir.exists()
                observed["locked_during_split"] = _external_slot_locked(
                    "disc:1",
                    lock_dir,
                    drive_device="/dev/disk5",
                    drive_serial="DRVPLAYALL1",
                )
                # Simulate a concurrent same-season job checking next episode while split is running.
                observed["concurrent_next_ep_during_split"] = _external_compute_next_ep(
                    title="Early Split Show",
                    season=1,
                    target_root=show_root,
                    season_dir=season_dir,
                    slots_dir=slots_dir,
                )
                out = []
                for idx in range(4):
                    p = staging / f"title_t00_split_t{900 + idx:03d}.mkv"
                    p.write_bytes(f"split-ep-{idx + 1}".encode())
                    out.append(p)
                return out

            def fake_ffprobe_dur(path_str: str) -> float:
                return 1380.0 if "_split_" in path_str else 5520.0

            def fake_eject_disc(_args, **_kw):
                events.append("eject")
                return True

            def fake_verify(_paths):
                events.append("verify")
                return True, ""

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
                "BURNDVD_ACTIVE_TV_SLOTS_DIR": str(slots_dir),
                "BURNDVD_DRIVE_DEVICE": "/dev/disk5",
                "BURNDVD_DRIVE_SERIAL": "DRVPLAYALL1",
                "BURNDVD_DRIVE_CLAIM": str(claim_dir),
            }
            resolved = drive.ResolvedMakemkvDevice(
                "disc:1", device_node="/dev/disk5", serial="DRVPLAYALL1"
            )

            with contextlib.ExitStack() as stack:
                _enter_all(
                    stack,
                    [
                        mock.patch.dict(os.environ, env_patch),
                        mock.patch.object(sys, "argv", argv),
                        mock.patch.object(ripper, "LOCAL_STAGING_BASE", staging_base),
                        mock.patch.object(ripper, "_free_gb", return_value=500.0),
                        mock.patch.object(ripqueue, "resolve_makemkv_device", return_value=resolved),
                        mock.patch.object(ripper, "resolve_makemkv_device", return_value=resolved),
                        mock.patch.object(ripqueue, "_drive_idx_from_args", return_value=None),
                        mock.patch.object(ripqueue, "check_key_expiry"),
                        mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info)),
                        mock.patch.object(ripper.subprocess, "Popen", side_effect=_makemkv_popen_dispatch(fake_popen)),
                        mock.patch.object(selection, "ffprobe_dur_s", side_effect=fake_ffprobe_dur),
                        mock.patch.object(ripper, "ffprobe_dur_s", side_effect=fake_ffprobe_dur),
                        mock.patch.object(selection, "chapter_marks_s", return_value=marks),
                        mock.patch.object(ripper, "split_chaptered_playall", side_effect=fake_split),
                        mock.patch.object(ripper, "_alt_playlist_duplicate", return_value=False),
                        mock.patch.object(ripper, "existing_content_collision", return_value=(None, None)),
                        mock.patch.object(ripper, "verify", side_effect=fake_verify),
                        mock.patch.object(ripper, "_await_dir_writable", return_value=season_dir),
                        mock.patch.object(ripper, "log_final_durations"),
                        mock.patch.object(ripqueue, "beep"),
                        mock.patch.object(drive, "eject_disc", side_effect=fake_eject_disc),
                        contextlib.redirect_stdout(io.StringIO()),
                    ],
                )
                ripqueue.main()

            self.assertTrue(observed["locked_during_makemkv"])
            self.assertTrue(observed["marker_exists_during_split"])
            self.assertTrue(observed["marker_during_split"]["ejected"])
            self.assertFalse(observed["claim_exists_during_split"])
            self.assertFalse(observed["locked_during_split"])
            # Concurrent same-season job MUST see E01-E04 reserved and advance to E05!
            conc = observed["concurrent_next_ep_during_split"]
            self.assertEqual(conc["next_episode"], 5)
            self.assertEqual(len(conc["reservations"]), 1)
            self.assertEqual(conc["reservations"][0]["episode_start"], 1)
            self.assertEqual(conc["reservations"][0]["episode_end"], 4)
            # Exact lifecycle order: makemkv -> eject -> playall_split -> verify
            self.assertEqual(events.count("eject"), 1, f"must eject once: {events}")
            self.assertLess(events.index("makemkv"), events.index("eject"))
            self.assertLess(events.index("eject"), events.index("playall_split"))
            self.assertLess(events.index("playall_split"), events.index("verify"))
            for ep in range(1, 5):
                self.assertTrue(
                    (season_dir / f"Early Split Show - S01E{ep:02d}.mkv").is_file()
                )
            metadata.clear_tv_slot_reservation(os.getpid(), slots_dir=slots_dir)

    def test_downstream_split_contract_failure_after_release_never_rereads_disc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging_base = root / "staging"
            lock_dir = root / "locks"
            slots_dir = root / "slots"
            show_root = root / "nas" / "TV Shows" / "Broken Split Show"
            season_dir = show_root / "Season 01"
            season_dir.mkdir(parents=True)
            state_path = root / "state.json"

            # Contract expects 4 episodes on disc 1, but play-all split fails and leaves 1 blob.
            qpath = root / "queue.csv"
            qpath.write_text(
                "title,type,season,episode_start,expected_episodes,expected_disc_episodes,"
                "expected_title_ids,expected_physical_disc,discs,target_root,format\n"
                f"Broken Split Show,tv-season,1,1,4,4,,1,1,{show_root},DVD\n",
                encoding="utf-8",
            )
            info = {
                "cinfo": {2: "BROKEN_SPLIT_SHOW_S1_D1"},
                "titles": {0: {9: "1:32:00", 11: "4000000000"}},
            }
            marks = _make_playall_chapters(n_eps=4, ep_len_s=1380.0)
            makemkv_calls = 0
            wait_calls = 0
            eject_calls = 0
            staged_paths: list[Path] = []

            def fake_wait(_a, _s):
                nonlocal wait_calls
                wait_calls += 1
                return ("disc", info)

            def fake_popen(cmd, **_kw):
                nonlocal makemkv_calls
                makemkv_calls += 1
                stage_dir = Path(cmd[-1])
                p = stage_dir / "title_t00.mkv"
                p.write_bytes(b"playall-preserved")
                staged_paths.append(p)
                return _FakeMakemkvProc(
                    ['MSG:5036,0,2,"Copy complete. 1 titles saved.","%1","1"\n'],
                    returncode=0,
                )

            def failed_split(_args, rips, _staging):
                return rips

            def fake_eject_disc(_args, **_kw):
                nonlocal eject_calls
                eject_calls += 1
                return True

            argv = [
                "ripqueue.py",
                "--queue", str(qpath),
                "--state", str(state_path),
                "--makemkvcon", sys.executable,
                "--device", "disc:0",
                "--non-interactive",
                "--on-fail=retry",
                "--no-subocr",
                "--disc-receipts", str(root / "receipts.jsonl"),
            ]
            resolved = drive.ResolvedMakemkvDevice(
                "disc:0", device_node="/dev/disk5", serial="DRVFAIL1"
            )
            env_patch = {
                "BURNDVD_LOCK_DIR": str(lock_dir),
                "BURNDVD_ACTIVE_TV_SLOTS_DIR": str(slots_dir),
                "BURNDVD_DRIVE_DEVICE": "/dev/disk5",
                "BURNDVD_DRIVE_SERIAL": "DRVFAIL1",
            }
            with contextlib.ExitStack() as stack:
                _enter_all(
                    stack,
                    [
                        mock.patch.dict(os.environ, env_patch),
                        mock.patch.object(sys, "argv", argv),
                        mock.patch.object(ripper, "LOCAL_STAGING_BASE", staging_base),
                        mock.patch.object(ripper, "_free_gb", return_value=500.0),
                        mock.patch.object(ripqueue, "resolve_makemkv_device", return_value=resolved),
                        mock.patch.object(ripper, "resolve_makemkv_device", return_value=resolved),
                        mock.patch.object(ripqueue, "_drive_idx_from_args", return_value=None),
                        mock.patch.object(ripqueue, "check_key_expiry"),
                        mock.patch.object(ripqueue, "wait_for_disc", side_effect=fake_wait),
                        mock.patch.object(ripper.subprocess, "Popen", side_effect=_makemkv_popen_dispatch(fake_popen)),
                        mock.patch.object(selection, "ffprobe_dur_s", return_value=5520.0),
                        mock.patch.object(ripper, "ffprobe_dur_s", return_value=5520.0),
                        mock.patch.object(selection, "chapter_marks_s", return_value=marks),
                        mock.patch.object(ripper, "split_chaptered_playall", side_effect=failed_split),
                        mock.patch.object(ripqueue, "beep"),
                        mock.patch.object(drive, "eject_disc", side_effect=fake_eject_disc),
                        contextlib.redirect_stdout(io.StringIO()),
                        contextlib.redirect_stderr(io.StringIO()),
                    ],
                )
                with self.assertRaises(SystemExit) as ctx:
                    ripqueue.main()

            self.assertEqual(ctx.exception.code, 2)
            # Even with --on-fail=retry, downstream failure MUST NOT re-read optical disc!
            self.assertEqual(makemkv_calls, 1)
            self.assertEqual(wait_calls, 1)
            self.assertEqual(eject_calls, 1)
            self.assertTrue(staged_paths and staged_paths[0].is_file())
            self.assertTrue(Path(f"{state_path}.drive_released").is_file())
            self.assertFalse(
                (slots_dir / f"pid-{os.getpid()}.json").exists(),
                "slot reservation must be cleared when rip fails downstream",
            )

    def test_downstream_verify_failure_after_release_never_rereads_disc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging_base = root / "staging"
            lock_dir = root / "locks"
            nas = root / "nas"
            nas.mkdir()
            state_path = root / "state.json"
            qpath = root / "queue.csv"
            qpath.write_text(
                f"title,type,discs,target_root,format\nCorrupt Movie (2024),movie,1,{nas},BD\n",
                encoding="utf-8",
            )
            info = {"cinfo": {2: "CORRUPT_MOVIE"}, "titles": {0: {9: "1:45:00", 11: "10000"}}}
            makemkv_calls = 0
            wait_calls = 0
            eject_calls = 0

            def fake_wait(_a, _s):
                nonlocal wait_calls
                wait_calls += 1
                return ("disc", info)

            def fake_popen(cmd, **_kw):
                nonlocal makemkv_calls
                makemkv_calls += 1
                stage_dir = Path(cmd[-1])
                (stage_dir / "title_t00.mkv").write_bytes(b"bad-frames")
                return _FakeMakemkvProc(
                    ['MSG:5036,0,2,"Copy complete. 1 titles saved.","%1","1"\n'],
                    returncode=0,
                )

            def fake_eject_disc(_args, **_kw):
                nonlocal eject_calls
                eject_calls += 1
                return True

            argv = [
                "ripqueue.py",
                "--queue", str(qpath),
                "--state", str(state_path),
                "--makemkvcon", sys.executable,
                "--device", "disc:0",
                "--non-interactive",
                "--on-fail=retry",
                "--verify",
                "--no-subocr",
                "--disc-receipts", str(root / "receipts.jsonl"),
            ]
            resolved = drive.ResolvedMakemkvDevice(
                "disc:0", device_node="/dev/disk5", serial="DRVVFAIL1"
            )
            env_patch = {
                "BURNDVD_LOCK_DIR": str(lock_dir),
                "BURNDVD_DRIVE_DEVICE": "/dev/disk5",
                "BURNDVD_DRIVE_SERIAL": "DRVVFAIL1",
            }
            with contextlib.ExitStack() as stack:
                _enter_all(
                    stack,
                    [
                        mock.patch.dict(os.environ, env_patch),
                        mock.patch.object(sys, "argv", argv),
                        mock.patch.object(ripper, "LOCAL_STAGING_BASE", staging_base),
                        mock.patch.object(ripper, "_free_gb", return_value=500.0),
                        mock.patch.object(ripqueue, "resolve_makemkv_device", return_value=resolved),
                        mock.patch.object(ripper, "resolve_makemkv_device", return_value=resolved),
                        mock.patch.object(ripqueue, "_drive_idx_from_args", return_value=None),
                        mock.patch.object(ripqueue, "check_key_expiry"),
                        mock.patch.object(ripqueue, "wait_for_disc", side_effect=fake_wait),
                        mock.patch.object(ripper.subprocess, "Popen", side_effect=_makemkv_popen_dispatch(fake_popen)),
                        mock.patch.object(ripper, "verify", return_value=(False, "corrupt decode")),
                        mock.patch.object(ripqueue, "beep"),
                        mock.patch.object(ripper, "beep"),
                        mock.patch.object(drive, "eject_disc", side_effect=fake_eject_disc),
                        contextlib.redirect_stdout(io.StringIO()),
                        contextlib.redirect_stderr(io.StringIO()),
                    ],
                )
                with self.assertRaises(SystemExit) as ctx:
                    ripqueue.main()

            self.assertEqual(ctx.exception.code, 2)
            self.assertEqual(makemkv_calls, 1)
            self.assertEqual(wait_calls, 1)
            self.assertEqual(eject_calls, 1)
            self.assertFalse((nas / "Corrupt Movie (2024).mkv").exists())

    def test_eject_confirmation_accuracy_true_false_and_no_eject(self):
        cases = (
            ("confirmed_eject_true", True, False, True, 1),
            ("failed_eject_false", False, False, False, 1),
            ("no_eject_flag", True, True, False, 0),
        )
        for case_name, eject_return, no_eject, expected_marker_ejected, expected_eject_calls in cases:
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                staging_base = root / "staging"
                lock_dir = root / "locks"
                nas = root / "nas"
                nas.mkdir()
                state_path = root / "state.json"
                qpath = root / "queue.csv"
                qpath.write_text(
                    f"title,type,discs,target_root,format\nEject Test (2025),movie,1,{nas},BD\n",
                    encoding="utf-8",
                )
                info = {"cinfo": {2: "EJECT_TEST"}, "titles": {0: {9: "1:40:00", 11: "1000"}}}
                eject_calls = 0

                def fake_popen(cmd, **_kw):
                    stage_dir = Path(cmd[-1])
                    (stage_dir / "title_t00.mkv").write_bytes(b"movie-data")
                    return _FakeMakemkvProc(
                        ['MSG:5036,0,2,"Copy complete. 1 titles saved.","%1","1"\n'],
                        returncode=0,
                    )

                def fake_eject_disc(_args, **_kw):
                    nonlocal eject_calls
                    eject_calls += 1
                    return eject_return

                argv = [
                    "ripqueue.py",
                    "--queue", str(qpath),
                    "--state", str(state_path),
                    "--makemkvcon", sys.executable,
                    "--device", "disc:0",
                    "--non-interactive",
                    "--no-subocr",
                    "--disc-receipts", str(root / "receipts.jsonl"),
                ]
                if no_eject:
                    argv.append("--no-eject")
                resolved = drive.ResolvedMakemkvDevice(
                    "disc:0", device_node="/dev/disk5", serial="DRVEJECT1"
                )
                env_patch = {
                    "BURNDVD_LOCK_DIR": str(lock_dir),
                    "BURNDVD_DRIVE_DEVICE": "/dev/disk5",
                    "BURNDVD_DRIVE_SERIAL": "DRVEJECT1",
                }
                with contextlib.ExitStack() as stack:
                    _enter_all(
                        stack,
                        [
                            mock.patch.dict(os.environ, env_patch),
                            mock.patch.object(sys, "argv", argv),
                            mock.patch.object(ripper, "LOCAL_STAGING_BASE", staging_base),
                            mock.patch.object(ripper, "_free_gb", return_value=500.0),
                            mock.patch.object(ripqueue, "resolve_makemkv_device", return_value=resolved),
                            mock.patch.object(ripper, "resolve_makemkv_device", return_value=resolved),
                            mock.patch.object(ripqueue, "_drive_idx_from_args", return_value=None),
                            mock.patch.object(ripqueue, "check_key_expiry"),
                            mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info)),
                            mock.patch.object(ripper.subprocess, "Popen", side_effect=_makemkv_popen_dispatch(fake_popen)),
                            mock.patch.object(ripper, "_await_dir_writable", return_value=nas),
                            mock.patch.object(ripper, "log_final_durations"),
                            mock.patch.object(ripqueue, "beep"),
                            mock.patch.object(drive, "eject_disc", side_effect=fake_eject_disc),
                            contextlib.redirect_stdout(io.StringIO()),
                        ],
                    )
                    ripqueue.main()

                self.assertEqual(eject_calls, expected_eject_calls)
                marker_path = Path(f"{state_path}.drive_released")
                self.assertTrue(marker_path.is_file())
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                self.assertIs(marker["ejected"], expected_marker_ejected)

    def test_drive_eject_returns_bool_from_eject_disc(self):
        args = argparse.Namespace(device="disc:0", no_eject=False)
        with mock.patch.object(drive, "eject_disc", return_value=True):
            self.assertIs(drive.eject(args), True)
        with mock.patch.object(drive, "eject_disc", return_value=False):
            self.assertIs(drive.eject(args), False)

    def test_sync_move_and_double_feature_keep_drive_ownership_until_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging_base = root / "staging"
            lock_dir = root / "locks"
            nas = root / "nas"
            nas.mkdir()
            state_path = root / "state.json"
            qpath = root / "queue.csv"
            qpath.write_text(
                f"title,type,discs,target_root,format\nSync Movie (2024),movie,1,{nas},BD\n",
                encoding="utf-8",
            )
            info = {"cinfo": {2: "SYNC_MOVIE"}, "titles": {0: {9: "1:40:00", 11: "1000"}}}
            observed: dict[str, bool] = {}
            real_move = ripper.move_with_progress

            def fake_popen(cmd, **_kw):
                stage_dir = Path(cmd[-1])
                (stage_dir / "title_t00.mkv").write_bytes(b"sync-movie")
                return _FakeMakemkvProc(
                    ['MSG:5036,0,2,"Copy complete. 1 titles saved.","%1","1"\n'],
                    returncode=0,
                )

            def tracked_move(s, d, **kw):
                observed["locked_during_sync_move"] = _external_slot_locked(
                    "disc:0", lock_dir, drive_device="/dev/disk5", drive_serial="DRVSYNC1"
                )
                observed["marker_during_sync_move"] = Path(
                    f"{state_path}.drive_released"
                ).is_file()
                return real_move(s, d, **kw)

            argv = [
                "ripqueue.py",
                "--queue", str(qpath),
                "--state", str(state_path),
                "--makemkvcon", sys.executable,
                "--device", "disc:0",
                "--non-interactive",
                "--sync-move",
                "--no-subocr",
                "--disc-receipts", str(root / "receipts.jsonl"),
            ]
            resolved = drive.ResolvedMakemkvDevice(
                "disc:0", device_node="/dev/disk5", serial="DRVSYNC1"
            )
            env_patch = {
                "BURNDVD_LOCK_DIR": str(lock_dir),
                "BURNDVD_DRIVE_DEVICE": "/dev/disk5",
                "BURNDVD_DRIVE_SERIAL": "DRVSYNC1",
            }
            with contextlib.ExitStack() as stack:
                mock_eject = stack.enter_context(
                    mock.patch.object(drive, "eject_disc", return_value=True)
                )
                _enter_all(
                    stack,
                    [
                        mock.patch.dict(os.environ, env_patch),
                        mock.patch.object(sys, "argv", argv),
                        mock.patch.object(ripper, "LOCAL_STAGING_BASE", staging_base),
                        mock.patch.object(ripper, "_free_gb", return_value=500.0),
                        mock.patch.object(ripqueue, "resolve_makemkv_device", return_value=resolved),
                        mock.patch.object(ripper, "resolve_makemkv_device", return_value=resolved),
                        mock.patch.object(ripqueue, "_drive_idx_from_args", return_value=None),
                        mock.patch.object(ripqueue, "check_key_expiry"),
                        mock.patch.object(ripqueue, "wait_for_disc", return_value=("disc", info)),
                        mock.patch.object(ripper.subprocess, "Popen", side_effect=_makemkv_popen_dispatch(fake_popen)),
                        mock.patch.object(ripper, "move_with_progress", side_effect=tracked_move),
                        mock.patch.object(ripqueue, "log_final_durations"),
                        mock.patch.object(ripqueue, "beep"),
                        contextlib.redirect_stdout(io.StringIO()),
                    ],
                )
                ripqueue.main()

            self.assertTrue(observed["locked_during_sync_move"])
            self.assertFalse(observed["marker_during_sync_move"])
            mock_eject.assert_called_once()
            self.assertTrue((nas / "Sync Movie (2024).mkv").is_file())

            # Double-feature: verify rip_double_feature does NOT pass on_optical_done
            # and keeps sync_move=True across all features.
            df_item = QueueItem(
                title="Double Feature",
                type="double-feature",
                discs=1,
                target_root=str(nas),
                format="BD",
                features=[
                    {"title_id": 1, "name": "Feat A (2001)"},
                    {"title_id": 2, "name": "Feat B (2002)"},
                ],
            )
            df_info = {
                "cinfo": {2: "DOUBLE_FEAT"},
                "titles": {1: {9: "1:30:00", 11: "1000"}, 2: {9: "1:35:00", 11: "1000"}},
            }
            df_calls: list[tuple[bool, object]] = []

            def fake_df_rip(sub_args, sub_item, *_rest, **kw):
                df_calls.append((bool(sub_args.sync_move), kw.get("on_optical_done")))
                return True, {"elapsed_s": 1.0, "files": [str(nas / f"{sub_item.title}.mkv")]}

            with mock.patch.object(ripper, "rip", side_effect=fake_df_rip), \
                 contextlib.redirect_stdout(io.StringIO()):
                ok_df, res_df = ripper.rip_double_feature(
                    _make_rip_args(state_path=state_path), df_item, df_info, {}, 1
                )
            self.assertTrue(ok_df)
            self.assertEqual(df_calls, [(True, None), (True, None)])
            self.assertEqual(len(res_df["files"]), 2)

    def test_tv_episode_numbering_preserved_with_existing_season_episodes_across_early_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging_base = root / "staging"
            slots_dir = root / "slots"
            show_root = root / "TV Shows" / "Continue Show"
            season_dir = show_root / "Season 01"
            season_dir.mkdir(parents=True)
            # Pre-populate E01 and E02 on NAS.
            (season_dir / "Continue Show - S01E01.mkv").write_bytes(b"existing-ep1")
            (season_dir / "Continue Show - S01E02.mkv").write_bytes(b"existing-ep2")

            item = QueueItem(
                title="Continue Show",
                type="tv-season",
                discs=1,
                target_root=str(show_root),
                format="DVD",
                season=1,
                episode_start=1,  # default; must advance past existing E01-E02 -> E03
            )
            info = {
                "cinfo": {2: "CONTINUE_SHOW_S1_D2"},
                "titles": {0: {9: "1:12:00", 11: "3000000000"}},
            }
            args = _make_rip_args(state_path=root / "state.json")
            marks = _make_playall_chapters(n_eps=3, ep_len_s=1440.0)
            res_at_release: dict = {}

            def fake_popen(cmd, **_kw):
                stage_dir = Path(cmd[-1])
                (stage_dir / "title_t00.mkv").write_bytes(b"disc2-playall")
                return _FakeMakemkvProc(
                    ['MSG:5036,0,2,"Copy complete. 1 titles saved.","%1","1"\n'],
                    returncode=0,
                )

            def on_optical_done():
                slot_file = slots_dir / f"pid-{os.getpid()}.json"
                if slot_file.is_file():
                    res_at_release.update(json.loads(slot_file.read_text(encoding="utf-8")))

            def fake_split(_args, rips, staging):
                out = []
                for idx in range(3):
                    p = staging / f"title_t00_split_t{900 + idx:03d}.mkv"
                    p.write_bytes(f"disc2-ep-{idx + 3}".encode())
                    out.append(p)
                return out

            def fake_ffprobe_dur(path_str: str) -> float:
                return 4320.0 if Path(path_str).name == "title_t00.mkv" else 1440.0

            with contextlib.ExitStack() as stack:
                _enter_all(
                    stack,
                    [
                        mock.patch.dict(os.environ, {"BURNDVD_ACTIVE_TV_SLOTS_DIR": str(slots_dir)}),
                        mock.patch.object(ripper, "LOCAL_STAGING_BASE", staging_base),
                        mock.patch.object(ripper, "_free_gb", return_value=500.0),
                        mock.patch.object(ripper.subprocess, "Popen", side_effect=_makemkv_popen_dispatch(fake_popen)),
                        mock.patch.object(selection, "ffprobe_dur_s", side_effect=fake_ffprobe_dur),
                        mock.patch.object(ripper, "ffprobe_dur_s", side_effect=fake_ffprobe_dur),
                        mock.patch.object(selection, "chapter_marks_s", return_value=marks),
                        mock.patch.object(ripper, "split_chaptered_playall", side_effect=fake_split),
                        mock.patch.object(ripper, "_alt_playlist_duplicate", return_value=False),
                        mock.patch.object(ripper, "existing_content_collision", return_value=(None, None)),
                        contextlib.redirect_stdout(io.StringIO()),
                    ],
                )
                ok, payload = ripper.rip(
                    args,
                    item,
                    info,
                    [0],
                    season_dir,
                    {},
                    1,
                    on_optical_done=on_optical_done,
                )

            self.assertTrue(ok, f"rip failed: {payload}")
            # Before releasing the drive, reservation MUST reflect E03-E05 (next_episode=6)!
            self.assertEqual(res_at_release.get("episode_start"), 3)
            self.assertEqual(res_at_release.get("episode_count"), 3)
            self.assertEqual(res_at_release.get("next_episode"), 6)
            self.assertEqual(
                [Path(f).name for f in payload["files"]],
                [
                    "Continue Show - S01E03.mkv",
                    "Continue Show - S01E04.mkv",
                    "Continue Show - S01E05.mkv",
                ],
            )
            metadata.clear_tv_slot_reservation(os.getpid(), slots_dir=slots_dir)


if __name__ == "__main__":
    unittest.main()
