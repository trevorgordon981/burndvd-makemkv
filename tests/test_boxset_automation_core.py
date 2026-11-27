"""Mocked unit tests for box-set automation features #2, #3, #4, and #5."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import burndvd.nas
from burndvd import config, metadata, models, ripper, selection
from burndvd.cli import metadata as cli_metadata
from burndvd.cli import ripqueue as cli_ripqueue


class FakePopen:
    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self.stdout = io.StringIO("\n".join(lines) + ("\n" if lines else ""))
        self.returncode = returncode

    def wait(self, timeout=None) -> int:
        return self.returncode

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


def _hms(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}"


def _tv_item(target_root: Path | str, **overrides) -> models.QueueItem:
    values = dict(
        title="Sample Anime",
        type="tv-season",
        discs=2,
        target_root=str(target_root),
        format="BD",
        season=1,
        episode_start=79,
    )
    values.update(overrides)
    return models.QueueItem(**values)


class TestFeature5StickyUserConfig(unittest.TestCase):
    def test_load_user_config_parses_comments_blanks_case_and_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config"
            cfg_file.write_text(
                "\n# User preferences\n"
                "  NO_EXTRAS = true  \n"
                "invalid_line_without_equals\n"
                "  =empty_key\n"
                "Default_Format = 'BD'\n"
                'Custom_Note = "box set"\n',
                encoding="utf-8",
            )
            loaded = config.load_user_config(cfg_file)
            self.assertEqual(
                loaded,
                {
                    "no_extras": "true",
                    "default_format": "BD",
                    "custom_note": "box set",
                },
            )
            self.assertTrue(config.config_bool(loaded, "no_extras", default=False))
            self.assertTrue(config.config_bool(loaded, "NO_EXTRAS", default=False))
            self.assertFalse(config.config_bool(loaded, "missing_key", default=False))
            self.assertTrue(config.config_bool(loaded, "missing_key", default=True))
            self.assertFalse(config.config_bool({"no_extras": "false"}, "no_extras", default=True))
            self.assertFalse(config.config_bool({"no_extras": "0"}, "no_extras", default=True))
            self.assertTrue(config.config_bool({"no_extras": "yes"}, "no_extras", default=False))
            self.assertTrue(config.config_bool({"no_extras": "unrecognized"}, "no_extras", default=True))

    def test_load_user_config_uses_env_and_handles_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"
            with mock.patch.dict(os.environ, {config.USER_CONFIG_ENV: str(missing)}):
                self.assertEqual(config.load_user_config(), {})

            cfg_file = Path(tmp) / "burndvd.conf"
            cfg_file.write_text("no_extras=1\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {config.USER_CONFIG_ENV: str(cfg_file)}):
                self.assertEqual(config.load_user_config(), {"no_extras": "1"})

    def test_ripqueue_no_extras_precedence_cli_env_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_true = Path(tmp) / "cfg_true"
            cfg_true.write_text("no_extras=true\n", encoding="utf-8")
            cfg_false = Path(tmp) / "cfg_false"
            cfg_false.write_text("no_extras=false\n", encoding="utf-8")
            cfg_missing = Path(tmp) / "cfg_missing"

            # Config absent, env absent => False
            with mock.patch.dict(
                os.environ,
                {config.USER_CONFIG_ENV: str(cfg_missing)},
                clear=False,
            ):
                os.environ.pop("BURNDVD_NO_EXTRAS", None)
                self.assertFalse(cli_ripqueue.build_parser().parse_args([]).no_extras)
                self.assertTrue(
                    cli_ripqueue.build_parser().parse_args(["--no-extras"]).no_extras
                )
                self.assertFalse(
                    cli_ripqueue.build_parser().parse_args(["--with-extras"]).no_extras
                )

            # Config no_extras=true => default True, overridden by --with-extras or BURNDVD_NO_EXTRAS=0
            with mock.patch.dict(
                os.environ,
                {config.USER_CONFIG_ENV: str(cfg_true)},
                clear=False,
            ):
                os.environ.pop("BURNDVD_NO_EXTRAS", None)
                self.assertTrue(cli_ripqueue.build_parser().parse_args([]).no_extras)
                self.assertFalse(
                    cli_ripqueue.build_parser().parse_args(["--with-extras"]).no_extras
                )
                self.assertFalse(
                    cli_ripqueue.build_parser()
                    .parse_args(["--no-extras", "--with-extras"])
                    .no_extras
                )
                self.assertTrue(
                    cli_ripqueue.build_parser()
                    .parse_args(["--with-extras", "--no-extras"])
                    .no_extras
                )

            with mock.patch.dict(
                os.environ,
                {
                    config.USER_CONFIG_ENV: str(cfg_true),
                    "BURNDVD_NO_EXTRAS": "0",
                },
                clear=False,
            ):
                self.assertFalse(cli_ripqueue.build_parser().parse_args([]).no_extras)
                self.assertTrue(
                    cli_ripqueue.build_parser().parse_args(["--no-extras"]).no_extras
                )

            # Env BURNDVD_NO_EXTRAS=1 overrides config false; --with-extras overrides env 1
            with mock.patch.dict(
                os.environ,
                {
                    config.USER_CONFIG_ENV: str(cfg_false),
                    "BURNDVD_NO_EXTRAS": "1",
                },
                clear=False,
            ):
                self.assertTrue(cli_ripqueue.build_parser().parse_args([]).no_extras)
                self.assertFalse(
                    cli_ripqueue.build_parser().parse_args(["--with-extras"]).no_extras
                )


class TestFeature2SmartTvAutoFill(unittest.TestCase):
    def test_matches_existing_nas_show_and_infers_season(self):
        with tempfile.TemporaryDirectory() as tmp:
            tv_hd = Path(tmp) / "TV Shows"
            tv_4k = Path(tmp) / "TV Shows 4K"
            (tv_hd / "Dragon Ball Super" / "Season 01").mkdir(parents=True)
            (tv_hd / "Arcane" / "Season 01").mkdir(parents=True)
            (tv_hd / "Arcane" / "Season 02").mkdir(parents=True)
            (tv_4k / "Andor").mkdir(parents=True)  # no Season dir yet -> defaults to 1

            roots = [tv_hd, tv_4k, Path(tmp) / "NonExistent"]

            self.assertEqual(
                metadata.infer_defaults("DragonBallSuper_P9_D2", tv_roots=roots),
                ("tv-season", "1", "Dragon Ball Super"),
            )
            self.assertEqual(
                metadata.infer_defaults("DRAGON_BALL_SUPER_PART10_DISC1", tv_roots=roots),
                ("tv-season", "1", "Dragon Ball Super"),
            )
            # Highest existing Season dir on NAS when volume has no explicit season
            self.assertEqual(
                metadata.infer_defaults("ARCANE_D2", tv_roots=roots),
                ("tv-season", "2", "Arcane"),
            )
            # Explicit season in volume overrides NAS highest season
            self.assertEqual(
                metadata.infer_defaults("ARCANE_S01_D3", tv_roots=roots),
                ("tv-season", "1", "Arcane"),
            )
            self.assertEqual(
                metadata.infer_defaults("Arcane_Season_3_Disc_1", tv_roots=roots),
                ("tv-season", "3", "Arcane"),
            )
            # Show dir in second root with no Season dir defaults to "1"
            self.assertEqual(
                metadata.infer_defaults("ANDOR_P1_D2", tv_roots=roots),
                ("tv-season", "1", "Andor"),
            )
            # Unmatched movie stays movie
            self.assertEqual(
                metadata.infer_defaults("INCEPTION", tv_roots=roots),
                ("movie", "", "Inception"),
            )
            # Existing GoT and tv_roots=None backwards compatibility intact
            self.assertEqual(
                metadata.infer_defaults("GAMEOFTHRONES_S4_DISC2", tv_roots=roots),
                ("tv-season", "4", "Game of Thrones"),
            )
            self.assertEqual(
                metadata.infer_defaults("DragonBallSuper_P9_D2"),
                ("movie", "", "Dragonballsuper P9 D2"),
            )

    def test_cli_metadata_defaults_accepts_repeatable_tv_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "TV Shows"
            root_b = Path(tmp) / "TV Shows 4K"
            root_a.mkdir()
            (root_b / "Dragon Ball Super" / "Season 01").mkdir(parents=True)

            buf = io.StringIO()
            argv = [
                "burndvd-metadata",
                "defaults",
                "DragonBallSuper_P9_D2",
                "--tv-root",
                str(root_a),
                "--tv-root",
                str(root_b),
            ]
            with mock.patch.object(sys, "argv", argv), redirect_stdout(buf):
                rc = cli_metadata.main()
            self.assertEqual(rc, 0)
            self.assertEqual(
                buf.getvalue().splitlines(),
                ["tv-season", "1", "Dragon Ball Super"],
            )


class TestFeature3SeasonMedianDurationAnchor(unittest.TestCase):
    def test_existing_season_median_dur_s_samples_and_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdir = Path(tmp) / "Season 01"
            sdir.mkdir()
            # Empty dir -> None
            self.assertIsNone(selection.existing_season_median_dur_s(sdir))

            # Single file -> None
            (sdir / "Show - S01E01.mkv").write_bytes(b"ep1")
            self.assertIsNone(
                selection.existing_season_median_dur_s(sdir, probe_fn=lambda _: 1387.0)
            )

            # 0-byte file and non-episode file ignored
            (sdir / "Show - S01E02.mkv").write_bytes(b"")
            (sdir / "Bonus Featurette.mkv").write_bytes(b"bonus")
            self.assertIsNone(
                selection.existing_season_median_dur_s(sdir, probe_fn=lambda _: 1387.0)
            )

            # Populate 12 valid episodes; ensure sample_limit=5 caps probes
            for ep in range(2, 13):
                (sdir / f"Show - S01E{ep:02d}.mkv").write_bytes(b"episode-bytes")

            probed_paths: list[str] = []
            durs_by_ep = {
                "S01E01": 1380.0,
                "S01E04": 1385.0,
                "S01E07": 1387.0,
                "S01E09": 1390.0,
                "S01E12": 1395.0,
            }

            def fake_probe(path_str: str) -> float:
                probed_paths.append(path_str)
                for key, val in durs_by_ep.items():
                    if key in path_str:
                        return val
                return 1387.0

            med = selection.existing_season_median_dur_s(
                sdir, sample_limit=5, probe_fn=fake_probe
            )
            self.assertEqual(len(probed_paths), 5)
            self.assertAlmostEqual(med, 1387.0)

            # Out-of-range durations (<600 or >7200) are filtered out
            self.assertIsNone(
                selection.existing_season_median_dur_s(
                    sdir, sample_limit=5, probe_fn=lambda _: 300.0
                )
            )
            self.assertIsNone(
                selection.existing_season_median_dur_s(
                    sdir, sample_limit=5, probe_fn=lambda _: 9000.0
                )
            )

    def test_episode_bonus_split_with_season_median_demotes_12m_even_with_two_eps(self):
        # Without season_median_s, 2 candidates never demote; with 1387s anchor, 12m (720s) is demoted.
        two_cand = [(1, 1385), (2, 720)]
        eps_legacy, bonus_legacy, thr_legacy = selection.episode_bonus_split(two_cand)
        self.assertEqual(eps_legacy, [1, 2])
        self.assertEqual(bonus_legacy, [])
        self.assertIsNone(thr_legacy)

        eps_anchored, bonus_anchored, thr_anchored = selection.episode_bonus_split(
            two_cand, season_median_s=1387.0
        )
        self.assertEqual(eps_anchored, [1])
        self.assertEqual(bonus_anchored, [2])
        self.assertAlmostEqual(thr_anchored, 1387.0 * 0.65)

        # Even if disc has MORE 12m bonuses than real episodes, bonuses never win
        skewed = [(1, 1385), (2, 1390), (3, 720), (4, 715), (5, 710)]
        eps_s, bonus_s, _ = selection.episode_bonus_split(
            skewed, season_median_s=1387.0
        )
        self.assertEqual(eps_s, [1, 2])
        self.assertEqual(bonus_s, [3, 4, 5])

        # Protected IDs are never demoted
        eps_p, bonus_p, _ = selection.episode_bonus_split(
            two_cand, protected_ids=[2], season_median_s=1387.0
        )
        self.assertEqual(eps_p, [1, 2])
        self.assertEqual(bonus_p, [])

        # Short-form show (< TV_MIN_DUR_S = 600s) uses 0.65 * season_median_s floor
        short_cand = [(1, 480), (2, 470), (3, 200)]
        eps_sh, bonus_sh, thr_sh = selection.episode_bonus_split(
            short_cand, season_median_s=480.0
        )
        self.assertEqual(eps_sh, [1, 2])
        self.assertEqual(bonus_sh, [3])
        self.assertAlmostEqual(thr_sh, 480.0 * 0.65)

    def test_play_all_classified_above_185x_even_when_bytes_mismatch(self):
        # 92m (5520s) play-all with wildly mismatched bytes + 2 real 23m eps + 12m bonus
        info = {
            "cinfo": {2: "SAMPLE_ANIME_P9_D2"},
            "titles": {
                0: {9: _hms(5520), 11: "25000000000"},  # 92m play-all, bad byte sum
                1: {9: _hms(1385), 11: "3000000000"},   # 23m ep
                2: {9: _hms(1390), 11: "3000000000"},   # 23m ep
                3: {9: _hms(720), 11: "1500000000"},    # 12m bonus
                4: {9: _hms(240), 11: "500000000"},     # 4m extra
            },
        }
        item = _tv_item("/Volumes/Media/TV Shows/Sample Anime")
        selected = selection.select_titles(item, info, season_median_s=1387.0)
        self.assertEqual(selected, [1, 2])

        # Pre-skips t00 play-all even though only 2 episodes exist and byte sum is off
        sels_with_extras, skipped = selection.planned_rip_selectors(
            item, info, selected, skip_extras=False, season_median_s=1387.0
        )
        self.assertEqual(skipped, {0})
        self.assertEqual(sels_with_extras, ["1", "2", "3", "4"])

        # With skip_extras=True, also omits 12m bonus (t03) and 4m extra (t04)
        sels_no_extras, skipped_ne = selection.planned_rip_selectors(
            item, info, selected, skip_extras=True, season_median_s=1387.0
        )
        self.assertEqual(skipped_ne, {0})
        self.assertEqual(sels_no_extras, ["1", "2"])

        # Never overrides contracted expected_title_ids
        contracted = _tv_item(
            "/Volumes/Media/TV Shows/Sample Anime",
            expected_episodes=10,
            expected_disc_episodes=3,
            expected_physical_disc=1,
            expected_title_ids=[0, 1, 2],
        )
        self.assertEqual(
            selection.select_titles(contracted, info, season_median_s=1387.0),
            [0, 1, 2],
        )
        sels_c, skipped_c = selection.planned_rip_selectors(
            contracted, info, [0, 1, 2], skip_extras=True, season_median_s=1387.0
        )
        self.assertEqual(skipped_c, set())
        self.assertEqual(sels_c, ["0", "1", "2"])

    def test_lone_play_all_only_disc_not_excluded_by_season_median(self):
        info = {
            "cinfo": {2: "SAMPLE_ANIME_PLAYALL_ONLY"},
            "titles": {
                0: {9: _hms(5520), 11: "12000000000"},
            },
        }
        item = _tv_item("/Volumes/Media/TV Shows/Sample Anime")
        self.assertEqual(
            selection.select_titles(item, info, season_median_s=1387.0),
            [0],
        )
        sels, skipped = selection.planned_rip_selectors(
            item, info, [0], skip_extras=True, season_median_s=1387.0
        )
        self.assertEqual(skipped, set())
        self.assertEqual(sels, ["0"])

    def test_rip_auto_wires_season_median_from_existing_season_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            show = root / "Media" / "TV Shows" / "Sample Anime"
            season = show / "Season 01"
            season.mkdir(parents=True)
            for ep in (1, 2, 3):
                (season / f"Sample Anime - S01E{ep:02d}.mkv").write_bytes(b"existing")

            item = _tv_item(show, episode_start=4)
            info = {
                "cinfo": {2: "SAMPLE_ANIME_P2_D1"},
                "titles": {
                    0: {9: _hms(5520), 11: "25000000000"},  # play-all (off bytes)
                    1: {9: _hms(1385), 11: "3000000000"},
                    2: {9: _hms(1390), 11: "3000000000"},
                    3: {9: _hms(720), 11: "1500000000"},    # 12m bonus
                },
            }
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                overwrite=False,
                rerip_review=False,
                auto_rerip_review=False,
                min_free_gb=0,
                non_interactive=True,
                sync_move=True,
                no_split_playall=True,
                no_extras=True,
                verify=False,
                current_disc_fingerprint="a" * 64,
                review_root=None,
                state=str(root / "state.json"),
            )

            seen_selectors: list[str] = []

            def fake_popen(cmd, **kw):
                sel = cmd[-2]
                seen_selectors.append(sel)
                staging_dir = Path(cmd[-1])
                tid = int(sel)
                (staging_dir / f"title_t{tid:02d}.mkv").write_bytes(f"ep-{tid}".encode())
                return FakePopen(['MSG:5036,0,1,"Copy complete. 1 titles saved."'])

            def fake_ffprobe(path_str: str) -> float:
                if "S01E" in path_str:
                    return 1387.0
                return 1385.0

            with mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "staging"), \
                 mock.patch.object(burndvd.nas, "LOCAL_LOCK_BASE", root / "locks"), \
                 mock.patch.object(ripper, "_free_gb", return_value=500.0), \
                 mock.patch.object(ripper.subprocess, "Popen", side_effect=fake_popen), \
                 mock.patch.object(selection, "ffprobe_dur_s", side_effect=fake_ffprobe), \
                 mock.patch.object(ripper, "ffprobe_dur_s", side_effect=fake_ffprobe), \
                 mock.patch.object(selection, "_content_relation", return_value="distinct"), \
                 mock.patch.object(ripper, "_alt_playlist_duplicate", return_value=False):
                ok, res = ripper.rip(args, item, info, [1, 2], season, {}, 1)

            self.assertTrue(ok, res)
            self.assertEqual(seen_selectors, ["1", "2"])
            self.assertTrue((season / "Sample Anime - S01E04.mkv").is_file())
            self.assertTrue((season / "Sample Anime - S01E05.mkv").is_file())


class TestFeature4SkipBadTitleExactSlots(unittest.TestCase):
    def _build_9ep_disc(self) -> dict:
        titles = {0: {9: _hms(1385 * 9), 11: str(3_000_000_000 * 9)}}
        for tid in range(1, 10):
            titles[tid] = {9: _hms(1385), 11: "3000000000"}
        return {"cinfo": {2: "SAMPLE_ANIME_P9_D2"}, "titles": titles}

    def test_skip_bad_title_preserves_exact_non_shifted_slots_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            show = root / "Media" / "TV Shows" / "Sample Anime"
            season = show / "Season 01"
            season.mkdir(parents=True)
            missing_log = root / "logs" / "missing-episodes.log"

            item = _tv_item(show, episode_start=79, discs=2)
            info = self._build_9ep_disc()
            title_ids = list(range(1, 10))  # t01..t09 -> E79..E87; t07 is E85
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                overwrite=False,
                rerip_review=False,
                auto_rerip_review=False,
                min_free_gb=0,
                non_interactive=True,
                sync_move=False,
                no_split_playall=True,
                no_extras=True,
                verify=True,
                current_disc_fingerprint="b" * 64,
                review_root=None,
                state=str(root / "state.json"),
                disc_receipts=str(root / "receipts.jsonl"),
                no_subocr=True,
                no_sound=True,
            )

            seen_selectors: list[str] = []
            verified_paths: list[str] = []
            partial_path_holder: list[Path] = []

            def fake_popen(cmd, **kw):
                sel = cmd[-2]
                seen_selectors.append(sel)
                staging_dir = Path(cmd[-1])
                tid = int(sel)
                if tid == 7:
                    # Write a partial truncated file before MakeMKV fails with SCSI error
                    partial = staging_dir / f"title_t{tid:02d}.mkv"
                    partial.write_bytes(b"truncated-partial")
                    partial_path_holder.append(partial)
                    return FakePopen(
                        [
                            'MSG:2003,0,3,"Error \'Scsi error - MEDIUM ERROR\' occurred while reading"',
                            'MSG:5037,0,2,"Copy complete. 0 titles saved, 1 failed."',
                        ],
                        returncode=0,
                    )
                (staging_dir / f"title_t{tid:02d}.mkv").write_bytes(
                    f"episode-data-t{tid:02d}".encode()
                )
                return FakePopen(['MSG:5036,0,1,"Copy complete. 1 titles saved."'])

            def fake_verify(paths: list[str]) -> tuple[bool, str]:
                verified_paths.extend(paths)
                return True, ""

            state: dict = {}
            with mock.patch.dict(
                os.environ, {"BURNDVD_MISSING_EPISODES_LOG": str(missing_log)}
            ), mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "staging"), \
                 mock.patch.object(burndvd.nas, "LOCAL_LOCK_BASE", root / "locks"), \
                 mock.patch.object(ripper, "_free_gb", return_value=500.0), \
                 mock.patch.object(ripper.subprocess, "Popen", side_effect=fake_popen), \
                 mock.patch.object(ripper, "verify", side_effect=fake_verify), \
                 mock.patch.object(ripper, "log_final_durations"), \
                 mock.patch.object(ripper, "_alt_playlist_duplicate", return_value=False):
                ok, res = ripper.rip(args, item, info, title_ids, season, state, 1)
                self.assertTrue(ok, res)
                # Partial file for failed t07 must have been removed from staging
                self.assertEqual(len(partial_path_holder), 1)
                self.assertFalse(partial_path_holder[0].exists())
                # Finalize (pre-publish verify + atomic move)
                fin_ok = ripper._run_finalize(args, res, item, 1)
                self.assertTrue(fin_ok)

            # All 9 selectors were attempted despite t07 failing
            self.assertEqual(seen_selectors, [str(i) for i in range(1, 10)])
            # Exact non-shifted episode files exist: E79..E84 and E86..E87
            for ep in (79, 80, 81, 82, 83, 84, 86, 87):
                self.assertTrue(
                    (season / f"Sample Anime - S01E{ep:02d}.mkv").is_file(),
                    f"Missing expected S01E{ep:02d}",
                )
            # E85 was left cleanly unconsumed (never shifted!)
            self.assertFalse((season / "Sample Anime - S01E85.mkv").exists())
            # Strict verify ran on all 8 surviving files
            self.assertEqual(len(set(verified_paths)), 8)
            # Next disc starts at E88 (all 9 disc slots accounted for)
            self.assertEqual(
                burndvd.nas.starting_ep(item, 2, state, target_dir=season),
                88,
            )
            # Result payload records skipped_bad_episodes
            self.assertEqual(len(res["skipped_bad_episodes"]), 1)
            self.assertEqual(res["skipped_bad_episodes"][0]["episode"], 85)
            self.assertEqual(res["skipped_bad_episodes"][0]["episode_code"], "S01E85")
            self.assertEqual(res["skipped_bad_episodes"][0]["title_id"], 7)
            # Missing episodes log written with required fields
            log_text = missing_log.read_text(encoding="utf-8")
            self.assertIn("Sample Anime", log_text)
            self.assertIn("Season 01", log_text)
            self.assertIn("S01E85", log_text)
            self.assertIn("t07", log_text)
            self.assertIn("0 saved, 1 failed", log_text)

    def test_fatal_drive_loss_aborts_instead_of_skipping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            show = root / "Media" / "TV Shows" / "Sample Anime"
            season = show / "Season 01"
            season.mkdir(parents=True)

            item = _tv_item(show, episode_start=1, discs=1)
            info = self._build_9ep_disc()
            title_ids = list(range(1, 10))
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                overwrite=False,
                rerip_review=False,
                auto_rerip_review=False,
                min_free_gb=0,
                non_interactive=True,
                sync_move=True,
                no_split_playall=True,
                no_extras=True,
                allow_mkvmerge_fallback=False,
                verify=False,
                current_disc_fingerprint="c" * 64,
                review_root=None,
                state=str(root / "state.json"),
            )

            seen_selectors: list[str] = []

            def fake_popen(cmd, **kw):
                sel = cmd[-2]
                seen_selectors.append(sel)
                staging_dir = Path(cmd[-1])
                tid = int(sel)
                if tid == 2:
                    return FakePopen(
                        ['MSG:5010,0,0,"Failed to open disc"'],
                        returncode=1,
                    )
                (staging_dir / f"title_t{tid:02d}.mkv").write_bytes(b"ok")
                return FakePopen(['MSG:5036,0,1,"Copy complete. 1 titles saved."'])

            with mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "staging"), \
                 mock.patch.object(burndvd.nas, "LOCAL_LOCK_BASE", root / "locks"), \
                 mock.patch.object(ripper, "_free_gb", return_value=500.0), \
                 mock.patch.object(ripper.subprocess, "Popen", side_effect=fake_popen), \
                 mock.patch.object(burndvd.nas, "_title_complete", return_value=False):
                ok, reason = ripper.rip(args, item, info, title_ids, season, {}, 1)

            self.assertFalse(ok)
            self.assertIn("Failed to open disc", reason)
            # Stopped immediately at t02; did NOT keep looping through t03..t09
            self.assertEqual(seen_selectors, ["1", "2"])

    def test_all_selectors_failing_falls_through_to_failure_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            show = root / "Media" / "TV Shows" / "Sample Anime"
            season = show / "Season 01"
            season.mkdir(parents=True)

            item = _tv_item(show, episode_start=1, discs=1)
            info = {
                "cinfo": {2: "SAMPLE_ANIME_P1_D1"},
                "titles": {
                    0: {9: _hms(1385 * 3), 11: str(3_000_000_000 * 3)},
                    1: {9: _hms(1385), 11: "3000000000"},
                    2: {9: _hms(1385), 11: "3000000000"},
                    3: {9: _hms(1385), 11: "3000000000"},
                },
            }
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                overwrite=False,
                rerip_review=False,
                auto_rerip_review=False,
                min_free_gb=0,
                non_interactive=True,
                sync_move=True,
                no_split_playall=True,
                no_extras=True,
                allow_mkvmerge_fallback=False,
                verify=False,
                current_disc_fingerprint="d" * 64,
                review_root=None,
                state=str(root / "state.json"),
            )

            def fake_popen(cmd, **kw):
                return FakePopen(
                    ['MSG:5037,0,2,"Copy complete. 0 titles saved, 1 failed."'],
                    returncode=0,
                )

            with mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "staging"), \
                 mock.patch.object(burndvd.nas, "LOCAL_LOCK_BASE", root / "locks"), \
                 mock.patch.object(ripper, "_free_gb", return_value=500.0), \
                 mock.patch.object(ripper.subprocess, "Popen", side_effect=fake_popen):
                ok, reason = ripper.rip(args, item, info, [1, 2, 3], season, {}, 1)

            self.assertFalse(ok)
            self.assertIn("all 3 titles failed", reason)

    def test_contract_bound_tv_disc_still_fails_closed_on_single_bad_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            show = root / "Media" / "TV Shows" / "Game of Thrones"
            season = show / "Season 01"
            season.mkdir(parents=True)

            item = _tv_item(
                show,
                title="Game of Thrones",
                season=1,
                episode_start=1,
                discs=1,
                expected_episodes=10,
                expected_disc_episodes=3,
                expected_physical_disc=2,
                expected_title_ids=[1, 2, 3],
            )
            info = {
                "cinfo": {2: "GAMEOFTHRONES_S1_DISC2"},
                "titles": {
                    0: {9: _hms(3300 * 3), 11: str(5_000_000_000 * 3)},
                    1: {9: _hms(3300), 11: "5000000000"},
                    2: {9: _hms(3300), 11: "5000000000"},
                    3: {9: _hms(3300), 11: "5000000000"},
                },
            }
            args = SimpleNamespace(
                makemkvcon="/fake/makemkvcon",
                device="disc:0",
                overwrite=False,
                rerip_review=False,
                auto_rerip_review=False,
                min_free_gb=0,
                non_interactive=True,
                sync_move=True,
                no_split_playall=True,
                no_extras=True,
                allow_mkvmerge_fallback=False,
                verify=False,
                current_disc_fingerprint="e" * 64,
                review_root=None,
                state=str(root / "state.json"),
            )

            seen_selectors: list[str] = []

            def fake_popen(cmd, **kw):
                sel = cmd[-2]
                seen_selectors.append(sel)
                staging_dir = Path(cmd[-1])
                tid = int(sel)
                if tid == 2:
                    return FakePopen(
                        ['MSG:5037,0,2,"Copy complete. 0 titles saved, 1 failed."'],
                        returncode=0,
                    )
                (staging_dir / f"title_t{tid:02d}.mkv").write_bytes(b"ok")
                return FakePopen(['MSG:5036,0,1,"Copy complete. 1 titles saved."'])

            with mock.patch.object(ripper, "LOCAL_STAGING_BASE", root / "staging"), \
                 mock.patch.object(burndvd.nas, "LOCAL_LOCK_BASE", root / "locks"), \
                 mock.patch.object(ripper, "_free_gb", return_value=500.0), \
                 mock.patch.object(ripper.subprocess, "Popen", side_effect=fake_popen), \
                 mock.patch.object(burndvd.nas, "_title_complete", return_value=False):
                ok, reason = ripper.rip(args, item, info, [1, 2, 3], season, {}, 2)

            self.assertFalse(ok)
            self.assertIn("0 saved, 1 failed", reason)
            # Contract-bound disc must fail closed immediately at t02
            self.assertEqual(seen_selectors, ["1", "2"])


if __name__ == "__main__":
    unittest.main()
