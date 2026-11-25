"""Unit tests covering stoprip, burndvd wrapper, checkrip, ejectdisc, discinfo, and rip-progress."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from burndvd.cli import checkrip, rip_progress, stoprip

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


def _write_exec(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class TestStopripFixes(unittest.TestCase):
    def test_process_grouping_multi_drive_and_lists(self):
        procs = [
            (100, 1, "/bin/bash /u/bin/burndvd --internal-pty-run /tmp/d0.log caffeinate -dimsu /u/ripqueue.py --queue /tmp/q0.csv --state /tmp/s0.json --device disc:0"),
            (101, 100, "python3 -c 'import select, sys; select.poll().poll()'"),
            (102, 100, "/usr/bin/script -q /tmp/d0.log caffeinate -dimsu /u/ripqueue.py --queue /tmp/q0.csv --state /tmp/s0.json --device disc:0"),
            (103, 102, "python3 /u/ripqueue.py --queue /tmp/q0.csv --state /tmp/s0.json --device disc:0"),
            (104, 103, "caffeinate -dimsu /u/ripqueue.py --queue /tmp/q0.csv --state /tmp/s0.json --device disc:0"),
            (105, 103, "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon -r mkv disc:0 1 /tmp/staging/111-103"),
            (200, 1, "/bin/bash /u/bin/burndvd --internal-pty-run /tmp/d1.log caffeinate -dimsu /u/ripqueue.py --queue /tmp/q1.csv --state /tmp/s1.json --device disc:1"),
            (202, 200, "/usr/bin/script -q /tmp/d1.log caffeinate -dimsu /u/ripqueue.py --queue /tmp/q1.csv --state /tmp/s1.json --device disc:1"),
            (203, 202, "python3 /u/ripqueue.py --queue /tmp/q1.csv --state /tmp/s1.json --device disc:1"),
            (204, 203, "caffeinate -dimsu /u/ripqueue.py --queue /tmp/q1.csv --state /tmp/s1.json --device disc:1"),
            (205, 203, "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon -r mkv disc:1 2 /tmp/staging/222-203"),
            (300, 1, "caffeinate -du -t 21600"),
            (301, 1, "vim /u/bin/burndvd"),
            (302, 1, "python3 /u/.local/bin/stoprip"),
        ]
        sessions = stoprip.discover_rip_sessions(procs)
        self.assertEqual(len(sessions), 2)
        by_dev = {s["device"]: s for s in sessions}
        self.assertEqual(by_dev["disc:0"]["pids"], [100, 101, 102, 103, 104, 105])
        self.assertEqual(by_dev["disc:1"]["pids"], [200, 202, 203, 204, 205])
        all_captured = set(by_dev["disc:0"]["pids"]) | set(by_dev["disc:1"]["pids"])
        self.assertTrue({300, 301, 302}.isdisjoint(all_captured))

    def test_nas_path_fallback_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            nas = Path(tmp) / "nas"
            vol = Path(tmp) / "vol"
            nas.mkdir()
            resolved = stoprip.resolve_nas_path(vol / "TV Shows" / "Show", nas_root=nas, vol_root=vol)
            self.assertEqual(resolved, nas / "TV Shows" / "Show")
            nas.rmdir()
            vol.mkdir()
            resolved2 = stoprip.resolve_nas_path(vol / "Movies" / "Tenet", nas_root=nas, vol_root=vol)
            self.assertEqual(resolved2, vol / "Movies" / "Tenet")

    def test_try_finally_always_sigkills_even_on_rescue_error(self):
        signals_sent = []

        def fake_kill(pids, sig):
            signals_sent.append((list(pids), sig))

        session = {
            "key": "/tmp/s.json",
            "state": "/tmp/s.json",
            "queue": "/tmp/q.csv",
            "device": "disc:0",
            "roles": {"ripqueue": [103], "makemkv": [105]},
            "pids": [103, 105],
            "argv_by_pid": {},
        }
        with mock.patch.object(stoprip, "locate_staging", return_value=Path("/nonexistent")), \
             mock.patch.object(stoprip, "rescue_staging", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                stoprip.stop_session(session, kill_fn=fake_kill, sleep_fn=lambda _: None)

        self.assertEqual(signals_sent, [([103, 105], signal.SIGSTOP), ([103, 105], signal.SIGKILL)])

    def test_tv_season_rescues_all_completed_mkvs_into_season_subdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nas = root / "nas"
            vol = root / "vol"
            nas.mkdir()
            staging_base = root / "staging"
            my_staging = staging_base / "1700000-103"
            my_staging.mkdir(parents=True)
            (my_staging / "title_t00.mkv").write_bytes(b"ep1data")
            (my_staging / "title_t01.mkv").write_bytes(b"ep2data")
            (my_staging / "title_t02.mkv").write_bytes(b"inprogress")
            (my_staging / "empty.mkv").write_bytes(b"")

            q = root / "q.csv"
            q.write_text(f"title,type,season,target_root\nSouth Park,tv-season,08,{vol}/TV Shows/South Park\n")
            st = root / "s.json"
            st.write_text(json.dumps({"current_index": 0}))

            session = {
                "key": str(st),
                "state": str(st),
                "queue": str(q),
                "device": "disc:0",
                "roles": {"ripqueue": [103], "makemkv": [105]},
                "pids": [103, 105],
                "argv_by_pid": {105: ["makemkvcon", "mkv", "disc:0", "2", str(my_staging)]},
            }
            with mock.patch.object(stoprip, "_open_write_paths", return_value={(my_staging / "title_t02.mkv").resolve()}):
                rescued = stoprip.stop_session(
                    session,
                    no_save=False,
                    staging_base=staging_base,
                    nas_root=nas,
                    vol_root=vol,
                    kill_fn=lambda p, s: None,
                    sleep_fn=lambda _: None,
                )

            season_dir = nas / "TV Shows" / "South Park" / "Season 08"
            self.assertTrue((season_dir / "title_t00.mkv").is_file())
            self.assertTrue((season_dir / "title_t01.mkv").is_file())
            self.assertFalse((season_dir / "title_t02.mkv").exists())
            self.assertEqual(len(rescued), 2)
            self.assertFalse(my_staging.exists())


class TestBurndvdWrapperFixes(unittest.TestCase):
    WRAPPER = BIN_DIR / "burndvd"

    def test_octal_season_08_09_and_literal_title_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            season = Path(tmp)
            (season / "What If...? [US] - S08E01.mkv").write_bytes(b"real")
            (season / "What If...X [US] - S08E99.mkv").write_bytes(b"decoy")
            script = r'''
set -euo pipefail
export BURNDVD_LIBRARY_ONLY=1
source "$WRAPPER"
TV_OUTPUT_MODE="normal"
RIPQUEUE_MODE_ARGS=()
select_tv_output_mode_for_range "$SEASON_DIR" "What If...? [US]" 08 01 02 09 1
printf 'MODE=%s ARGS=%s\n' "$TV_OUTPUT_MODE" "${RIPQUEUE_MODE_ARGS[*]:-}"
'''
            res = subprocess.run(
                ["/bin/bash", "-c", script],
                env={**os.environ, "WRAPPER": str(self.WRAPPER), "SEASON_DIR": str(season)},
                text=True,
                capture_output=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("MODE=review ARGS=--auto-rerip-review", res.stdout)

    def test_drive_claim_order_and_static_guards(self):
        src = self.WRAPPER.read_text()
        idx_assign = src.index('DRIVE_CLAIM_HELD="$drive_claim"')
        idx_state_write = src.index('echo "$STATE" > "$DRIVE_CLAIM_HELD/state"')
        self.assertGreater(idx_state_write, idx_assign)
        self.assertIn('kill -0 "$claim_pid"', src)
        self.assertIn('drutil status -drive "$DRUTIL_INDEX"', src)
        self.assertIn('"$HOME/.local/bin/ejectdisc" "$DRUTIL_INDEX"', src)
        self.assertIn("SEASON=$((10#$SEASON))", src)
        self.assertIn('[[ "$RIP_EXIT_CODE" =~ ^[0-9]+$ ]] || RIP_EXIT_CODE=1', src)
        self.assertIn('exit "$RIP_EXIT_CODE"', src)


class TestCheckripFixes(unittest.TestCase):
    def test_tv_shows_4k_and_bracketed_dir_scandir_and_json_exit_code(self):
        self.assertIn("TV Shows 4K", checkrip._MEDIA_SUBDIRS)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "Movie [tmdbid-12345]"
            d.mkdir()
            (d / "Show - S01E01.mkv").write_bytes(b"aaaa")
            (d / "Show - S01E02.MKV").write_bytes(b"bbbbbbbb")
            (d / "notes.txt").write_text("ignore")

            with mock.patch.object(checkrip, "probe_duration", return_value=1300.0):
                entries = checkrip.scan(str(d))
            self.assertEqual([e["name"] for e in entries], ["Show - S01E01.mkv", "Show - S01E02.MKV"])

            with mock.patch.object(checkrip, "probe_duration", return_value=1300.0), \
                 mock.patch.object(sys, "argv", ["checkrip", str(d), "--expected", "5", "--json"]):
                rc = checkrip.main()
            self.assertEqual(rc, 1)

            with mock.patch.object(checkrip, "probe_duration", return_value=1300.0), \
                 mock.patch.object(sys, "argv", ["checkrip", str(d), "--expected", "2", "--json"]):
                rc_ok = checkrip.main()
            self.assertEqual(rc_ok, 0)

    def test_active_rips_between_titles_and_multi_drive_and_direct_log_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            log0 = Path(tmp) / "d0.log"
            log1 = Path(tmp) / "d1.log"
            log0.write_text("line 1\n[###---] 45%\r[####--] 60%\n")
            log1.write_text("drive 1 probing...\n")

            procs = [
                (500, 1, f"/bin/bash /u/burndvd --internal-pty-run {log0} caffeinate -dimsu /u/ripqueue.py --state /tmp/s0.json --device disc:0"),
                (501, 500, f"/usr/bin/script -q {log0} caffeinate -dimsu /u/ripqueue.py --state /tmp/s0.json --device disc:0"),
                (502, 501, "python3 /u/ripqueue.py --queue /tmp/q0.csv --state /tmp/s0.json --device disc:0"),
                (600, 1, f"/bin/bash /u/burndvd --internal-pty-run {log1} caffeinate -dimsu /u/ripqueue.py --state /tmp/s1.json --device disc:1"),
                (601, 600, f"/usr/bin/script -q {log1} caffeinate -dimsu /u/ripqueue.py --state /tmp/s1.json --device disc:1"),
                (602, 601, "python3 /u/ripqueue.py --queue /tmp/q1.csv --state /tmp/s1.json --device disc:1"),
                (603, 602, "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon mkv disc:1 3 /tmp/staging/123-602"),
            ]
            rips = checkrip.active_rips(procs)
            self.assertEqual(len(rips), 2)
            by_pid = {r["pid"]: r for r in rips}
            self.assertEqual(by_pid[502]["log_path"], str(log0))
            self.assertIsNone(by_pid[502]["staging"])
            self.assertEqual(by_pid[602]["staging"], "/tmp/staging/123-602")

            with mock.patch.object(checkrip, "active_rips", return_value=[]):
                tail = checkrip.active_rip_tail(str(log0))
            self.assertEqual(tail, ["line 1", "[####--] 60%"])


class TestCliToolsFixes(unittest.TestCase):
    def test_ejectdisc_drive_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp)
            _write_exec(fake_bin / "drutil", '#!/bin/bash\necho "DRUTIL:$*"\n')
            env = {**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"}
            cases = [
                ([], "DRUTIL:tray eject"),
                (["close"], "DRUTIL:tray close"),
                (["2"], "DRUTIL:-drive 2 tray eject"),
                (["2", "close"], "DRUTIL:-drive 2 tray close"),
                (["-drive", "3"], "DRUTIL:-drive 3 tray eject"),
                (["-drive", "3", "close"], "DRUTIL:-drive 3 tray close"),
            ]
            for argv, expected in cases:
                res = subprocess.run([str(BIN_DIR / "ejectdisc"), *argv], env=env, text=True, capture_output=True)
                self.assertEqual(res.returncode, 0, f"argv={argv} stderr={res.stderr}")
                self.assertEqual(res.stdout.strip(), expected)

    def test_discinfo_busy_guard_drive2_and_real_rc(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp)
            _write_exec(fake_bin / "pgrep", "#!/bin/bash\nexit 0\n")
            _write_exec(fake_bin / "makemkvcon", "#!/bin/bash\nexit 0\n")
            env_busy = {**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"}
            res_busy = subprocess.run([str(BIN_DIR / "discinfo")], env=env_busy, text=True, capture_output=True)
            self.assertEqual(res_busy.returncode, 1)
            self.assertIn("busy", res_busy.stderr)

            _write_exec(fake_bin / "pgrep", "#!/bin/bash\nexit 1\n")
            _write_exec(
                fake_bin / "drutil",
                """#!/bin/bash
if [ "$1" = "list" ]; then
    echo "  1 HL-DT-ST BD-RE BU40N 1.03 USB Apple Supported"
    echo "  2 HL-DT-ST BD-RE BU40N 1.03 USB Apple Supported"
    exit 0
fi
if [ "$1" = "status" ] && [ "$2" = "-drive" ] && [ "$3" = "1" ]; then
    echo " Type: No Media Inserted"
    exit 0
fi
if [ "$1" = "status" ] && [ "$2" = "-drive" ] && [ "$3" = "2" ]; then
    echo " Type: BD-ROM Name: /dev/disk8"
    exit 0
fi
exit 1
""",
            )
            _write_exec(fake_bin / "diskutil", '#!/bin/bash\necho "Volume Name: TEST_BD"\n')
            _write_exec(fake_bin / "makemkvcon", '#!/bin/bash\necho "ERR: failed" >&2; exit 7\n')
            res_rc = subprocess.run([str(BIN_DIR / "discinfo")], env=env_busy, text=True, capture_output=True)
            self.assertEqual(res_rc.returncode, 1)
            self.assertIn("rc=7", res_rc.stderr)
            self.assertIn("drive 2", res_rc.stdout)

    def test_rip_progress_detects_partial_and_prefers_nfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            sub = staging / "1700-123"
            sub.mkdir(parents=True)
            src_mkv = sub / "title_t00.mkv"
            src_mkv.write_bytes(b"x" * 1000)
            old_t = time.time() - 30
            os.utime(src_mkv, (old_t, old_t))

            partials = root / "nas" / ".repair-quarantine" / ".burndvd-partials"
            partials.mkdir(parents=True)
            part = partials / ".123-456-abcd.partial"
            part.write_bytes(b"x" * 400)

            with mock.patch.object(rip_progress, "STAGING", staging):
                pair = rip_progress.find_pair(search_roots=[partials])
            self.assertEqual(pair, (src_mkv, part))
            self.assertEqual(rip_progress.MEDIA_BASES[0], Path("/private/nas/media"))


if __name__ == "__main__":
    unittest.main()
