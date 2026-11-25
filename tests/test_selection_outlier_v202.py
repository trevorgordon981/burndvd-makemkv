"""Tests for TV episode duration-outlier demotion guard in burndvd.selection."""

from __future__ import annotations

import unittest

from burndvd.models import QueueItem
from burndvd.selection import episode_bonus_split, select_titles


def _hms(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}"


def _tv_item(**overrides) -> QueueItem:
    values = dict(
        title="Dragon Ball Super",
        type="tv-season",
        discs=2,
        target_root="/Volumes/Media/TV Shows/Dragon Ball Super",
        format="BD",
        season=1,
        episode_start=10,
    )
    values.update(overrides)
    return QueueItem(**values)


class TvDurationOutlierGuardTests(unittest.TestCase):
    def test_dragon_ball_super_p1_d2_demotes_11m59s_bonus_to_extras(self):
        # Four ~23m (1380s) episodes plus one 11m59s (719s) bonus interview.
        candidates = [
            (0, 1380),
            (1, 1380),
            (2, 1380),
            (3, 1380),
            (4, 719),
        ]
        episodes, extras, threshold = episode_bonus_split(candidates)
        self.assertEqual(episodes, [0, 1, 2, 3])
        self.assertEqual(extras, [4])
        self.assertAlmostEqual(threshold, 0.60 * 1380)

        info = {
            "cinfo": {2: "DRAGON_BALL_SUPER_P1_D2"},
            "titles": {
                tid: {9: _hms(dur), 11: "4000000000"}
                for tid, dur in candidates
            },
        }
        selected = select_titles(_tv_item(), info)
        self.assertEqual(selected, [0, 1, 2, 3])

    def test_normal_half_hour_runtime_variance_keeps_all_episodes(self):
        candidates = [
            (1, 21 * 60),
            (2, 22 * 60),
            (3, 23 * 60),
            (4, 24 * 60),
        ]
        episodes, extras, _thr = episode_bonus_split(candidates)
        self.assertEqual(episodes, [1, 2, 3, 4])
        self.assertEqual(extras, [])

    def test_double_length_finale_keeps_all_episodes(self):
        candidates = [
            (1, 22 * 60),
            (2, 22 * 60),
            (3, 23 * 60),
            (4, 45 * 60),
        ]
        episodes, extras, _thr = episode_bonus_split(candidates)
        self.assertEqual(episodes, [1, 2, 3, 4])
        self.assertEqual(extras, [])

    def test_short_11m_animated_show_keeps_all_episodes(self):
        candidates = [
            (1, 11 * 60),
            (2, 11 * 60),
            (3, 11 * 60),
            (4, 11 * 60),
        ]
        episodes, extras, _thr = episode_bonus_split(candidates)
        self.assertEqual(episodes, [1, 2, 3, 4])
        self.assertEqual(extras, [])

    def test_two_episode_disc_below_cluster_threshold_never_demotes(self):
        candidates = [
            (1, 23 * 60),
            (2, 12 * 60),
        ]
        episodes, extras, threshold = episode_bonus_split(candidates)
        self.assertEqual(episodes, [1, 2])
        self.assertEqual(extras, [])
        self.assertIsNone(threshold)

    def test_one_hour_drama_demotes_12m_bonus_featurette(self):
        candidates = [
            (1, 44 * 60),
            (2, 44 * 60),
            (3, 44 * 60),
            (4, 12 * 60),
        ]
        episodes, extras, _thr = episode_bonus_split(candidates)
        self.assertEqual(episodes, [1, 2, 3])
        self.assertEqual(extras, [4])

    def test_strict_sixty_percent_boundary(self):
        # Median = 1380s -> 60% = 828s; 828s is kept, 827s is demoted.
        at_boundary = [(1, 1380), (2, 1380), (3, 1380), (4, 828)]
        eps_at, extras_at, _ = episode_bonus_split(at_boundary)
        self.assertEqual(eps_at, [1, 2, 3, 4])
        self.assertEqual(extras_at, [])

        below_boundary = [(1, 1380), (2, 1380), (3, 1380), (4, 827)]
        eps_below, extras_below, _ = episode_bonus_split(below_boundary)
        self.assertEqual(eps_below, [1, 2, 3])
        self.assertEqual(extras_below, [4])

    def test_explicit_expected_title_ids_override_protects_short_title(self):
        candidates = [
            (0, 1380),
            (1, 1380),
            (2, 1380),
            (3, 719),
        ]
        episodes, extras, _thr = episode_bonus_split(
            candidates, protected_ids=[0, 1, 2, 3]
        )
        self.assertEqual(episodes, [0, 1, 2, 3])
        self.assertEqual(extras, [])

        info = {
            "cinfo": {2: "CONTRACTED_SHOW_S1_D1"},
            "titles": {
                tid: {9: _hms(dur), 11: "3000000000"}
                for tid, dur in candidates
            },
        }
        item = _tv_item(
            expected_episodes=4,
            expected_disc_episodes=4,
            expected_physical_disc=1,
            expected_title_ids=[0, 1, 2, 3],
        )
        self.assertEqual(select_titles(item, info), [0, 1, 2, 3])


if __name__ == "__main__":
    unittest.main()
