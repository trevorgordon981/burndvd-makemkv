"""Unit tests for multi-drive and watch-mode awareness in checkrip."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from burndvd.cli import checkrip


class TestCheckripMultiDrive(unittest.TestCase):
    def test_active_rips_detects_two_drives_and_watch_module_ripqueue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            q1 = root / "q1.csv"
            q2 = root / "q2.csv"
            log1 = root / "burndvd-d1.log"
            log2 = root / "burndvd-watch-d2.log"
            slots = root / "slots"
            slots.mkdir()
            q1.write_text(
                "title,type,season,episode_start,expected_episodes,expected_disc_episodes,"
                "expected_title_ids,expected_physical_disc,discs,target_root,format,notes,features\n"
                "Dragon Ball Super,tv-season,1,105,,,,,1,/private/nas/media/TV Shows/Dragon Ball Super,BD,auto-built from DBS_P9_D1,\n",
                encoding="utf-8",
            )
            q2.write_text(
                "title,type,season,episode_start,expected_episodes,expected_disc_episodes,"
                "expected_title_ids,expected_physical_disc,discs,target_root,format,notes,features\n"
                "Dragon Ball Super,tv-season,1,114,,,,,1,/private/nas/media/TV Shows/Dragon Ball Super,BD,auto-built from DBS_P9_D2,\n",
                encoding="utf-8",
            )
            (slots / "pid-1001.json").write_text(
                json.dumps({"pid": 1001, "title": "Dragon Ball Super", "season": 1, "episode_start": 105, "episode_count": 9}),
                encoding="utf-8",
            )
            (slots / "pid-2001.json").write_text(
                json.dumps({"pid": 2001, "title": "Dragon Ball Super", "season": 1, "episode_start": 114, "episode_count": 4}),
                encoding="utf-8",
            )
            log1.write_text("[###---] 20.0% [1/9] Saving\n", encoding="utf-8")
            log2.write_text("[#####-] 50.0% [2/4] Saving\n", encoding="utf-8")

            procs = [
                (900, 1, "python3 -m burndvd.cli.watch --show 'Dragon Ball Super' --season 1"),
                (1000, 1, f"/usr/bin/script -q {log1} caffeinate -dimsu /usr/bin/ripqueue.py --queue {q1} --state /tmp/s1.json --device disc:0"),
                (1001, 1000, f"python3 /usr/bin/ripqueue.py --queue {q1} --state /tmp/s1.json --device disc:0"),
                (1002, 1001, "makemkvcon -r mkv disc:0 0 /tmp/staging/111-1001"),
                (2001, 900, f"python3 -m burndvd.cli.ripqueue --queue {q2} --state /tmp/s2.json --device disc:1"),
                (2002, 2001, "makemkvcon -r mkv disc:1 1 /tmp/staging/222-2001"),
            ]

            with mock.patch.dict(os.environ, {"BURNDVD_ACTIVE_TV_SLOTS_DIR": str(slots)}), \
                 mock.patch.object(checkrip, "_claim_hints_for_rip", side_effect=[("/dev/disk5", "DRVSER00001"), ("/dev/disk6", "DRVSER00002")]):
                rips = checkrip.active_rips(procs)
                watchers = checkrip.active_watchers(procs)

            self.assertEqual(len(watchers), 1)
            self.assertEqual(watchers[0]["show"], "Dragon Ball Super")
            self.assertEqual(len(rips), 2)
            self.assertEqual(rips[0]["drive_device"], "/dev/disk5")
            self.assertEqual(rips[0]["drive_serial"], "DRVSER00001")
            self.assertEqual(rips[0]["episode_range"], "S01E105-E113")
            self.assertEqual(rips[0]["volume"], "DBS_P9_D1")
            self.assertEqual(rips[1]["drive_device"], "/dev/disk6")
            self.assertEqual(rips[1]["drive_serial"], "DRVSER00002")
            self.assertEqual(rips[1]["episode_range"], "S01E114-E117")
            self.assertEqual(rips[1]["volume"], "DBS_P9_D2")

            # Verify human summary & JSON rendering for 2 simultaneous drives
            fake_rips = [
                {**rips[0], "log_path": str(log1)},
                {**rips[1], "log_path": str(log2)},
            ]
            with mock.patch.object(checkrip, "_list_processes", return_value=procs), \
                 mock.patch.object(checkrip, "active_rips", return_value=fake_rips), \
                 mock.patch.object(checkrip, "active_watchers", return_value=watchers), \
                 mock.patch.object(checkrip, "measure_rip_speeds", return_value={"/tmp/staging/111-1001": 18.4, "/tmp/staging/222-2001": 19.2}):
                buf = io.StringIO()
                with mock.patch.object(sys, "argv", ["checkrip"]), contextlib.redirect_stdout(buf):
                    rc = checkrip.main()
                self.assertEqual(rc, 0)
                out = buf.getvalue()
                self.assertIn("Active Rips (2 drives)", out)
                self.assertIn("/dev/disk5 (DRVSER00001)", out)
                self.assertIn("/dev/disk6 (DRVSER00002)", out)
                self.assertIn("S01E105-E113", out)
                self.assertIn("S01E114-E117", out)
                self.assertIn("18.4 MB/s", out)
                self.assertIn("19.2 MB/s", out)

                buf_json = io.StringIO()
                with mock.patch.object(sys, "argv", ["checkrip", "--json"]), contextlib.redirect_stdout(buf_json):
                    rc_j = checkrip.main()
                self.assertEqual(rc_j, 0)
                parsed = json.loads(buf_json.getvalue())
                self.assertTrue(parsed["active"])
                self.assertEqual(len(parsed["rips"]), 2)
                self.assertEqual(parsed["rips"][0]["drive_serial"], "DRVSER00001")
                self.assertEqual(parsed["rips"][1]["episode_range"], "S01E114-E117")
                self.assertEqual(len(parsed["watchers"]), 1)

    def test_measure_rip_speeds_returns_none_when_empty_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            speeds = checkrip.measure_rip_speeds([tmp], secs=0.01)
            self.assertEqual(speeds, {tmp: None})
