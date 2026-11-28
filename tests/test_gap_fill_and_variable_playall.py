"""Tests for gap-aware next-episode defaults and variable-chapter play-all splitting."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from burndvd import metadata, selection


class GapAwareNextEpisodeTests(unittest.TestCase):
    def _touch_eps(self, season_dir: Path, eps: list[int], show: str = "Naruto Shippuden") -> None:
        season_dir.mkdir(parents=True, exist_ok=True)
        for ep in eps:
            (season_dir / f"{show} - S01E{ep:02d}.mkv").write_bytes(b"mkv")

    def test_defaults_to_first_missing_gap_when_higher_episodes_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            season_dir = root / "Naruto Shippuden" / "Season 01"
            self._touch_eps(season_dir, list(range(1, 78)) + list(range(138, 145)))

            res = metadata.compute_next_tv_episode(
                title="Naruto Shippuden",
                season=1,
                target_root="/private/nas/media/TV Shows/Naruto Shippuden",
                season_dir=season_dir,
                slots_dir=root / "slots",
                active_processes=[],
            )
            self.assertEqual(res["nas_highest"], 144)
            self.assertEqual(res["next_episode"], 78)
            self.assertTrue(res["gap_fill"])

    def test_advances_across_concurrent_reservations_inside_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            season_dir = root / "Naruto Shippuden" / "Season 01"
            self._touch_eps(season_dir, list(range(1, 78)) + list(range(138, 145)))
            slots_dir = root / "slots"

            p1 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
            p2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
            try:
                metadata.write_tv_slot_reservation(
                    pid=p1.pid,
                    title="Naruto Shippuden",
                    season=1,
                    target_root="/private/nas/media/TV Shows/Naruto Shippuden",
                    episode_start=78,
                    episode_count=4,
                    slots_dir=slots_dir,
                )
                res2 = metadata.compute_next_tv_episode(
                    title="Naruto Shippuden",
                    season=1,
                    target_root="/private/nas/media/TV Shows/Naruto Shippuden",
                    season_dir=season_dir,
                    slots_dir=slots_dir,
                    active_processes=[],
                )
                self.assertEqual(res2["next_episode"], 82)
                self.assertTrue(res2["gap_fill"])

                metadata.write_tv_slot_reservation(
                    pid=p2.pid,
                    title="Naruto Shippuden",
                    season=1,
                    target_root="/private/nas/media/TV Shows/Naruto Shippuden",
                    episode_start=82,
                    episode_count=4,
                    slots_dir=slots_dir,
                )
                res3 = metadata.compute_next_tv_episode(
                    title="Naruto Shippuden",
                    season=1,
                    target_root="/private/nas/media/TV Shows/Naruto Shippuden",
                    season_dir=season_dir,
                    slots_dir=slots_dir,
                    active_processes=[],
                )
                self.assertEqual(res3["next_episode"], 86)
                self.assertTrue(res3["gap_fill"])
            finally:
                for p in (p1, p2):
                    p.terminate()
                    p.wait(timeout=5)

    def test_high_reservation_above_nas_highest_does_not_mask_lower_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            season_dir = root / "Naruto Shippuden" / "Season 01"
            self._touch_eps(season_dir, list(range(1, 78)) + list(range(138, 145)))
            slots_dir = root / "slots"

            p1 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
            try:
                metadata.write_tv_slot_reservation(
                    pid=p1.pid,
                    title="Naruto Shippuden",
                    season=1,
                    target_root="/private/nas/media/TV Shows/Naruto Shippuden",
                    episode_start=145,
                    episode_count=4,
                    slots_dir=slots_dir,
                )
                res = metadata.compute_next_tv_episode(
                    title="Naruto Shippuden",
                    season=1,
                    target_root="/private/nas/media/TV Shows/Naruto Shippuden",
                    season_dir=season_dir,
                    slots_dir=slots_dir,
                    active_processes=[],
                )
                self.assertEqual(res["next_episode"], 78)
                self.assertTrue(res["gap_fill"])
            finally:
                p1.terminate()
                p1.wait(timeout=5)

    def test_skips_past_higher_block_once_gap_is_filled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            season_dir = root / "Naruto Shippuden" / "Season 01"
            self._touch_eps(season_dir, list(range(1, 134)) + list(range(138, 145)))
            slots_dir = root / "slots"

            p1 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
            try:
                metadata.write_tv_slot_reservation(
                    pid=p1.pid,
                    title="Naruto Shippuden",
                    season=1,
                    target_root="/private/nas/media/TV Shows/Naruto Shippuden",
                    episode_start=134,
                    episode_count=4,
                    slots_dir=slots_dir,
                )
                res = metadata.compute_next_tv_episode(
                    title="Naruto Shippuden",
                    season=1,
                    target_root="/private/nas/media/TV Shows/Naruto Shippuden",
                    season_dir=season_dir,
                    slots_dir=slots_dir,
                    active_processes=[],
                )
                self.assertEqual(res["next_episode"], 145)
                self.assertFalse(res["gap_fill"])
            finally:
                p1.terminate()
                p1.wait(timeout=5)

    def test_contiguous_mid_series_start_continues_forward(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            season_dir = root / "Dragon Ball Super" / "Season 01"
            self._touch_eps(season_dir, [90, 91], show="Dragon Ball Super")

            res = metadata.compute_next_tv_episode(
                title="Dragon Ball Super",
                season=1,
                target_root="/private/nas/media/TV Shows/Dragon Ball Super",
                season_dir=season_dir,
                slots_dir=root / "slots",
                active_processes=[],
            )
            self.assertEqual(res["next_episode"], 92)
            self.assertFalse(res["gap_fill"])


class VariableChapterPlayallTests(unittest.TestCase):
    # Exact chapter start times (in seconds) from real Naruto Shippuden Set 6 D2
    # (7, 6, 7, 7 chapters + tiny 0.13m trailing cell; total 94.53m = 5671.8s).
    SET6_D2_MARKS_S = [
        0.0 * 60,
        0.53 * 60,
        2.21 * 60,
        10.29 * 60,
        20.30 * 60,
        21.81 * 60,
        22.48 * 60,
        23.62 * 60,  # Ep 71 start (Ch 8)
        24.31 * 60,
        25.98 * 60,
        37.78 * 60,
        44.94 * 60,
        46.45 * 60,
        47.21 * 60,  # Ep 72 start (Ch 14)
        50.94 * 60,
        52.61 * 60,
        60.80 * 60,
        67.51 * 60,
        69.02 * 60,
        69.70 * 60,
        70.80 * 60,  # Ep 73 start (Ch 21)
        73.78 * 60,
        75.45 * 60,
        80.72 * 60,
        91.11 * 60,
        92.61 * 60,
        93.29 * 60,
        94.40 * 60,  # Tiny 0.13m trailing cell
    ]
    SET6_D2_TOTAL_S = 94.53 * 60

    def test_splits_variable_chapter_7_6_7_7_anime_playall(self) -> None:
        plan = selection._playall_plan(self.SET6_D2_MARKS_S, self.SET6_D2_TOTAL_S)
        self.assertIsNotNone(plan)
        spread, stride, edges, lens = plan
        self.assertEqual(stride, 0)
        self.assertEqual(len(lens), 4)
        self.assertLess(spread, 15.0)
        self.assertAlmostEqual(edges[0], 0.0, delta=0.5)
        self.assertAlmostEqual(edges[1], 23.62 * 60, delta=0.5)
        self.assertAlmostEqual(edges[2], 47.21 * 60, delta=0.5)
        self.assertAlmostEqual(edges[3], 70.80 * 60, delta=0.5)
        self.assertAlmostEqual(edges[4], self.SET6_D2_TOTAL_S, delta=0.5)

    def test_estimate_playall_episode_count_uses_variable_plan(self) -> None:
        args = SimpleNamespace(no_split_playall=False)
        fake_rip = Path("/tmp/NarutoShippudenSet6D2_t00.mkv")
        with (
            mock.patch.object(selection, "ffprobe_dur_s", return_value=self.SET6_D2_TOTAL_S),
            mock.patch.object(selection, "chapter_marks_s", return_value=self.SET6_D2_MARKS_S),
        ):
            count = selection.estimate_playall_episode_count(args, [fake_rip])
        self.assertEqual(count, 4)

    def test_uniform_fixed_stride_still_uses_fixed_stride(self) -> None:
        # 4 identical 24-minute episodes with 6 chapters each (OP/Act/Act/ED/Prev/End)
        ep_pattern = [0, 90, 600, 1200, 1290, 1380]
        marks = [ep * 1440 + off for ep in range(4) for off in ep_pattern]
        plan = selection._playall_plan(marks, 4 * 1440.0)
        self.assertIsNotNone(plan)
        spread, stride, _edges, lens = plan
        self.assertEqual(stride, 6)
        self.assertEqual(len(lens), 4)
        self.assertAlmostEqual(spread, 0.0, delta=0.1)

    def test_rejects_uniform_chapter_movie_without_episode_shape(self) -> None:
        # 120-minute movie with evenly spaced 8-minute chapters (shape ratio = 1.0)
        marks = [i * 480.0 for i in range(15)]
        plan = selection._playall_plan(marks, 7200.0)
        self.assertIsNone(plan)

    def test_rejects_irregular_movie_with_large_segment_spread(self) -> None:
        # 100-minute movie with irregular chapter gaps that cannot make even episodes
        marks = [
            0.0,
            180.0,
            1100.0,
            1900.0,
            2100.0,
            3400.0,
            4500.0,
            4600.0,
            5500.0,
        ]
        plan = selection._playall_plan(marks, 6000.0)
        self.assertIsNone(plan)


if __name__ == "__main__":
    unittest.main()
