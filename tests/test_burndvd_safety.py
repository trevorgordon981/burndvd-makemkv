import json
import errno
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import burndvd_metadata as metadata
import ripqueue

WRAPPER = BIN / "burndvd"


def tv_item(root: Path, **changes) -> ripqueue.QueueItem:
    values = dict(
        title="Game of Thrones",
        type="tv-season",
        discs=1,
        target_root=str(root),
        format="BD",
        season=4,
        episode_start=3,
        expected_episodes=10,
        expected_disc_episodes=3,
        expected_title_ids=[1, 2, 3],
        expected_physical_disc=2,
    )
    values.update(changes)
    return ripqueue.QueueItem(**values)


class DetachedVerifierTests(unittest.TestCase):
    def test_verify_uses_absolute_ffmpeg_binary(self):
        completed = SimpleNamespace(returncode=0, stderr="")
        with mock.patch.object(ripqueue.subprocess, "run", return_value=completed) as run:
            self.assertEqual(ripqueue.verify(["episode.mkv"]), (True, ""))
        self.assertEqual(run.call_args.args[0][0], ripqueue.FFMPEG_BIN)
        self.assertTrue(Path(ripqueue.FFMPEG_BIN).is_absolute())


class EpisodeContractTests(unittest.TestCase):
    def test_game_of_thrones_compact_disc_labels_infer_safe_defaults(self):
        for label, season in (
            ("GAMEOFTHRONES_S4_DISC2", "4"),
            ("GAMEOFTHRONES_S4_DISC_2", "4"),
            ("GAMEOFTHRONES_S4_D2", "4"),
            ("GAMEOFTHRONES_S1_D1", "1"),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    metadata.infer_defaults(label),
                    ("tv-season", season, "Game of Thrones"),
                )
        self.assertEqual(metadata.infer_defaults("NOTGAMEOFTHRONES_S4_DISC2")[0], "movie")
        self.assertEqual(metadata.infer_defaults("GAMEOFTHRONES_S4_DISCOVERY2")[0], "movie")

    def test_game_of_thrones_s4_disc1_skips_bonus_title_2(self):
        contract = metadata.episode_contract(
            "Game of Thrones", 4, "GAMEOFTHRONES_S4_DISC1"
        )
        self.assertEqual(contract["episode_start"], 1)
        self.assertEqual(contract["expected_title_ids"], [1, 3])
        self.assertEqual(contract["expected_disc_episodes"], 2)
        self.assertEqual(contract["expected_episodes"], 10)

    def test_s4_disc1_contract_selects_t01_and_t03_not_25_minute_bonus(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(
                Path(tmp), episode_start=1, expected_disc_episodes=2,
                expected_title_ids=[1, 3],
            )
            paths = [Path("B1_t01.mkv"), Path("B1_t02.mkv"), Path("B1_t03.mkv")]
            selected, extras, missing = ripqueue.partition_episode_contract(paths, item)
            self.assertEqual(selected, [paths[0], paths[2]])
            self.assertEqual(extras, [paths[1]])
            self.assertEqual(missing, [])

    def test_all_repaired_seasons_sum_to_ten(self):
        for season in range(1, 7):
            contracts = [
                metadata.episode_contract(
                    "Game of Thrones", season,
                    f"GAMEOFTHRONES_S{season}_DISC{disc}",
                )
                for disc in range(1, 5)
            ]
            self.assertEqual(sum(c["expected_disc_episodes"] for c in contracts), 10)
            self.assertEqual(contracts[-1]["episode_start"]
                             + contracts[-1]["expected_disc_episodes"] - 1, 10)

    def test_d_label_is_parsed_without_confusing_season(self):
        contract = metadata.episode_contract(
            "Gameofthrones", 1, "GAMEOFTHRONES_S1_D3"
        )
        self.assertEqual(contract["disc"], 3)
        self.assertEqual(contract["episode_start"], 6)

    def test_s6_disc1_uses_audited_noncontiguous_title_ids(self):
        contract = metadata.episode_contract(
            "Game of Thrones", 6, "GAMEOFTHRONES_S6_DISC1"
        )
        self.assertEqual(contract["expected_title_ids"], [1, 2, 5])

    def test_runtime_disc_label_parser_accepts_underscore_after_number(self):
        info = {"cinfo": {2: "GAMEOFTHRONES_DISC2_US"}, "titles": {}}
        self.assertEqual(ripqueue.disc_n_from_label(info), 2)


class QueueContractTests(unittest.TestCase):
    def test_queue_round_trip_preserves_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "queue.csv"
            metadata.write_queue(
                queue,
                title="Game of Thrones",
                item_type="tv-season",
                season="4",
                episode_start="3",
                expected_episodes="10",
                expected_disc_episodes="3",
                expected_title_ids="[1,2,3]",
                expected_physical_disc="2",
                target_root=str(Path(tmp).resolve()),
                media_format="BD",
                notes="test",
            )
            item = ripqueue.load_queue(queue)[0]
            self.assertEqual(item.expected_title_ids, [1, 2, 3])
            self.assertEqual(item.episode_start, 3)
            self.assertEqual(item.expected_physical_disc, 2)

    def test_missing_tv_contract_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp), expected_episodes=0)
            with self.assertRaisesRegex(ValueError, "expected_episodes"):
                ripqueue.validate_queue_item(item)

    def test_title_id_count_must_match_disc_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp), expected_title_ids=[1, 3])
            with self.assertRaisesRegex(ValueError, "length"):
                ripqueue.validate_queue_item(item)

    def test_missing_physical_disc_binding_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp), expected_physical_disc=0)
            with self.assertRaisesRegex(ValueError, "expected_physical_disc"):
                ripqueue.validate_queue_item(item)


class PublicationGuardTests(unittest.TestCase):
    def test_explicit_contract_is_never_replaced_by_highest_plus_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp))
            season = ripqueue.compute_target_dir(item)
            season.mkdir()
            (season / "Game of Thrones - S04E26.mkv").write_bytes(b"x")
            self.assertEqual(ripqueue.starting_ep(item, 2, {}), 3)

    def test_occupied_disc_range_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp))
            season = ripqueue.compute_target_dir(item)
            season.mkdir()
            (season / "Game of Thrones - S04E03.mkv").write_bytes(b"x")
            error = ripqueue.tv_contract_preflight_error(item, season)
            self.assertIn("already exist", error)

    def test_later_disc_slots_do_not_block_missing_exact_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp))
            season = ripqueue.compute_target_dir(item)
            season.mkdir()
            for episode in range(6, 11):
                (season / f"Game of Thrones - S04E{episode:02d}.mkv").write_bytes(b"x")
            self.assertIsNone(ripqueue.tv_contract_preflight_error(item, season))

    def test_episode_beyond_season_total_blocks_every_normal_rip(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp))
            season = ripqueue.compute_target_dir(item)
            season.mkdir()
            (season / "Game of Thrones - S04E13.mkv").write_bytes(b"x")
            error = ripqueue.tv_contract_preflight_error(item, season)
            self.assertIn("beyond authoritative total", error)

    def test_rerip_review_bypasses_slot_guard_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp))
            season = ripqueue.compute_target_dir(item)
            season.mkdir()
            (season / "Game of Thrones - S04E03.mkv").write_bytes(b"x")
            self.assertIsNone(
                ripqueue.tv_contract_preflight_error(item, season, rerip_review=True)
            )

    def test_normal_tv_overwrite_flag_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            error = ripqueue.run_policy_error(
                [tv_item(Path(tmp))], overwrite=True, rerip_review=False
            )
        self.assertIn("--overwrite is forbidden", error)

    def test_overwrite_remains_available_for_movies_but_not_tv_review(self):
        movie = SimpleNamespace(type="movie")
        self.assertIsNone(
            ripqueue.run_policy_error(
                [movie], overwrite=True, rerip_review=False
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn(
                "forbidden for TV",
                ripqueue.run_policy_error(
                    [tv_item(Path(tmp))], overwrite=True, rerip_review=True
                ),
            )

    def test_default_wrapper_enables_verification(self):
        wrapper = WRAPPER.read_text()
        self.assertIn("--non-interactive --on-fail=retry --verify", wrapper)

    def test_public_slack_failure_notification_is_opt_in(self):
        wrapper = WRAPPER.read_text()
        self.assertIn('SLACK_CHANNEL_FAIL="${BURNDVD_SLACK_CHANNEL:-}"', wrapper)
        self.assertIn('[ -n "$SLACK_CHANNEL_FAIL" ] || return 0', wrapper)
        self.assertIn("BURNDVD_SLACK_BOT_TOKEN", wrapper)
        self.assertIn("BURNDVD_SLACK_ENV", wrapper)

    def test_empty_mode_array_is_safe_under_macos_bash_nounset(self):
        wrapper = WRAPPER.read_text()
        safe = '${RIPQUEUE_MODE_ARGS[@]+"${RIPQUEUE_MODE_ARGS[@]}"}'
        self.assertEqual(wrapper.count(safe), 3)

    def _wrapper_mode(self, season_dir: Path, first_ep: int = 9,
                      last_ep: int = 10, expected: int = 10,
                      recognized: bool = True, initial_args=()):
        wrapper = WRAPPER
        script = r'''
set -euo pipefail
export BURNDVD_LIBRARY_ONLY=1
source "$WRAPPER"
TV_OUTPUT_MODE="normal"
RIPQUEUE_MODE_ARGS=()
for arg in $INITIAL_ARGS; do
    append_ripqueue_mode_arg_once "$arg"
done
select_tv_output_mode_for_range \
    "$SEASON_DIR" "Game of Thrones" 4 "$FIRST_EP" "$LAST_EP" "$EXPECTED" \
    "$RECOGNIZED"
if [ "$RECOGNIZED" = "1" ] && [ "$TV_OUTPUT_MODE" = "normal" ]; then
    append_ripqueue_mode_arg_once "--auto-rerip-review"
fi
printf 'MODE=%s ARGS=%s\n' "$TV_OUTPUT_MODE" "${RIPQUEUE_MODE_ARGS[*]:-}"
'''
        env = dict(os.environ)
        env.update(
            WRAPPER=str(wrapper), SEASON_DIR=str(season_dir),
            FIRST_EP=str(first_ep), LAST_EP=str(last_ep), EXPECTED=str(expected),
            RECOGNIZED="1" if recognized else "0",
            INITIAL_ARGS=" ".join(initial_args),
        )
        return subprocess.run(
            ["/bin/bash", "-c", script], env=env,
            text=True, capture_output=True,
        )

    def test_wrapper_automatically_routes_occupied_range_to_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            season = Path(tmp)
            (season / "Game of Thrones - S04E09.mkv").write_bytes(b"episode")
            result = self._wrapper_mode(season)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Existing authoritative slot E9 detected", result.stdout)
        self.assertIn("MODE=review ARGS=--auto-rerip-review", result.stdout)

    def test_wrapper_keeps_vacant_authoritative_range_in_normal_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            season = Path(tmp)
            (season / "Game of Thrones - S04E08.mkv").write_bytes(b"episode")
            result = self._wrapper_mode(season)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MODE=normal ARGS=--auto-rerip-review", result.stdout)

    def test_wrapper_manual_contract_does_not_auto_review_occupied_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            season = Path(tmp)
            (season / "Game of Thrones - S04E09.mkv").write_bytes(b"episode")
            result = self._wrapper_mode(season, recognized=False)
        self.assertEqual(result.returncode, 65)
        self.assertIn("no verified metadata contract", result.stderr)

    def test_wrapper_mode_append_preserves_existing_args_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._wrapper_mode(
                Path(tmp), initial_args=("--companion", "--auto-rerip-review")
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "MODE=normal ARGS=--companion --auto-rerip-review", result.stdout
        )

    def test_wrapper_still_rejects_episode_beyond_season_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            season = Path(tmp)
            (season / "Game of Thrones - S04E11.mkv").write_bytes(b"episode")
            result = self._wrapper_mode(season)
        self.assertEqual(result.returncode, 65)
        self.assertIn("beyond authoritative total 10", result.stderr)

    def test_wrapper_uses_explicit_mode_not_argument_string_equality(self):
        wrapper = WRAPPER.read_text()
        self.assertIn('TV_OUTPUT_MODE="normal"', wrapper)
        self.assertIn('TV_OUTPUT_MODE="review"', wrapper)
        self.assertNotIn('${RIPQUEUE_MODE_ARGS[*]:-}" != "--rerip-review"', wrapper)

    def test_unverified_finalize_never_writes_disc_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                verify=False,
                disc_receipts=str(Path(tmp) / "receipts.jsonl"),
                rerip_review=False,
                no_subocr=True,
                no_sound=True,
            )
            result = {
                "files": [str(Path(tmp) / "episode.mkv")],
                "move_jobs": [],
                "staging": None,
                "disc_fingerprint": "f" * 64,
            }
            with mock.patch.object(ripqueue, "append_disc_receipt") as append:
                ripqueue._run_finalize(args, result, tv_item(Path(tmp)), 2)
            append.assert_not_called()

    def test_auto_review_resolves_only_an_occupied_authoritative_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp))
            season = ripqueue.compute_target_dir(item)
            season.mkdir()
            mode, error, occupied = ripqueue.tv_contract_output_mode(
                item, season, auto_rerip_review=True
            )
            self.assertEqual((mode, error, occupied), ("normal", None, []))
            (season / "Game of Thrones - S04E03.mkv").write_bytes(b"episode")
            mode, error, occupied = ripqueue.tv_contract_output_mode(
                item, season, auto_rerip_review=True
            )
            self.assertEqual((mode, error, occupied), ("review", None, [3]))

    def test_auto_review_treats_zero_byte_slot_claim_as_occupied(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp))
            season = ripqueue.compute_target_dir(item)
            season.mkdir()
            (season / "Game of Thrones - S04E03.mkv").touch()
            mode, error, occupied = ripqueue.tv_contract_output_mode(
                item, season, auto_rerip_review=True
            )
        self.assertEqual((mode, error, occupied), ("review", None, [3]))

    def test_auto_review_treats_hidden_slot_claim_as_occupied(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp))
            season = ripqueue.compute_target_dir(item)
            season.mkdir()
            destination = season / "Game of Thrones - S04E03.mkv"
            ripqueue._create_tv_slot_claim(
                ripqueue.tv_slot_claim_path(destination)
            )
            mode, error, occupied = ripqueue.tv_contract_output_mode(
                item, season, auto_rerip_review=True
            )
        self.assertEqual((mode, error, occupied), ("review", None, [3]))

    def test_auto_review_still_rejects_inventory_beyond_season_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp))
            season = ripqueue.compute_target_dir(item)
            season.mkdir()
            (season / "Game of Thrones - S04E11.mkv").write_bytes(b"episode")
            mode, error, occupied = ripqueue.tv_contract_output_mode(
                item, season, auto_rerip_review=True
            )
        self.assertEqual(mode, "normal")
        self.assertIn("beyond authoritative total", error)
        self.assertEqual(occupied, [])

    def test_auto_review_decision_isolated_to_one_disc_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp))
            season = ripqueue.compute_target_dir(item)
            season.mkdir()
            (season / "Game of Thrones - S04E03.mkv").write_bytes(b"episode")
            shared = SimpleNamespace(
                rerip_review=False,
                auto_rerip_review=True,
                state=str(Path(tmp) / "review-state.json"),
            )
            first_disc = SimpleNamespace(**vars(shared))
            error = ripqueue.apply_tv_output_mode(
                first_disc, item, season, "test race"
            )
        self.assertIsNone(error)
        self.assertTrue(first_disc.rerip_review)
        self.assertFalse(shared.rerip_review)

    def test_rip_boundary_rejects_tv_overwrite_even_if_cli_check_is_bypassed(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp))
            args = SimpleNamespace(overwrite=True)
            ok, message = ripqueue.rip(
                args, item, {"titles": {}}, [],
                ripqueue.compute_target_dir(item), {}, 2,
            )
        self.assertFalse(ok)
        self.assertIn("forbidden for TV", message)

    def test_auto_review_requires_exact_registry_contract(self):
        item = tv_item(Path("/tmp"))
        info = {
            "cinfo": {2: "GAME OF THRONES SEASON 4 DISC 2"},
            "titles": {},
        }
        self.assertIsNone(ripqueue.automatic_review_contract_error(item, info))
        tampered = tv_item(Path("/tmp"), episode_start=4)
        self.assertIn(
            "does not match",
            ripqueue.automatic_review_contract_error(tampered, info),
        )
        unknown = tv_item(Path("/tmp"), title="Unknown Show")
        self.assertIn(
            "registry-recognized",
            ripqueue.automatic_review_contract_error(unknown, info),
        )

    def test_verified_review_finalize_never_writes_publication_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                verify=True,
                disc_receipts=str(Path(tmp) / "receipts.jsonl"),
                rerip_review=True,
                no_subocr=True,
                no_sound=True,
            )
            result = {
                "files": [str(Path(tmp) / "review-title-t01.mkv")],
                "move_jobs": [],
                "staging": None,
                "disc_fingerprint": "f" * 64,
            }
            with mock.patch.object(ripqueue, "verify", return_value=(True, "")), \
                 mock.patch.object(ripqueue, "append_disc_receipt") as append, \
                 mock.patch.object(ripqueue, "run_subocr_postrip"):
                ripqueue._run_finalize(args, result, tv_item(Path(tmp)), 2)
            append.assert_not_called()


class ReviewIsolationTests(unittest.TestCase):
    def test_review_path_is_outside_smb_tv_library(self):
        item = tv_item(Path("/Volumes/Media/TV Shows/Game of Thrones"))
        target = ripqueue.compute_target_dir(item)
        review = ripqueue.tv_review_capture_dir(item, target, "a" * 64)
        self.assertEqual(
            review,
            Path("/Volumes/Media/.repair-quarantine/burndvd-review/")
            / "Game of Thrones/Season 04" / ("a" * 16),
        )
        with self.assertRaises(ValueError):
            review.relative_to(Path(item.target_root))

    def test_review_path_derives_nfs_and_4k_storage_roots(self):
        item = tv_item(Path("/private/nas/media/TV Shows 4K/Game of Thrones"))
        target = ripqueue.compute_target_dir(item)
        review = ripqueue.tv_review_capture_dir(item, target, "b" * 64)
        self.assertEqual(
            review,
            Path("/private/nas/media/.repair-quarantine/burndvd-review/")
            / "Game of Thrones/Season 04" / ("b" * 16),
        )

    def test_custom_review_root_must_be_absolute_and_outside_library(self):
        item = tv_item(Path("/custom/library/Game of Thrones"))
        target = ripqueue.compute_target_dir(item)
        with self.assertRaisesRegex(ValueError, "pass --review-root"):
            ripqueue.tv_review_capture_dir(item, target, "c" * 64)
        with self.assertRaisesRegex(ValueError, "absolute"):
            ripqueue.tv_review_capture_dir(
                item, target, "c" * 64, Path("relative/review")
            )
        with self.assertRaisesRegex(ValueError, "outside"):
            ripqueue.tv_review_capture_dir(
                item, target, "c" * 64,
                Path("/custom/library/Game of Thrones/review"),
            )
        self.assertEqual(
            ripqueue.tv_review_capture_dir(
                item, target, "c" * 64, Path("/custom/quarantine")
            ),
            Path("/custom/quarantine/Game of Thrones/Season 04") / ("c" * 16),
        )

    def test_custom_review_root_rejects_normalized_and_other_tv_library_paths(self):
        item = tv_item(Path("/Volumes/Media/TV Shows/Game of Thrones"))
        target = ripqueue.compute_target_dir(item)
        for unsafe in (
            Path("/Volumes/Media/foo/../TV Shows/evil"),
            Path("/Volumes/Media/TV Shows 4K/evil"),
            Path("/private/nas/media/TV Shows/evil"),
        ):
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(
                ValueError, "outside"
            ):
                ripqueue.tv_review_capture_dir(
                    item, target, "c" * 64, unsafe
                )

    def test_custom_review_root_rejects_symlink_into_tv_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "Media"
            library = storage / "TV Shows"
            library.mkdir(parents=True)
            alias = storage / "review-alias"
            alias.symlink_to(library, target_is_directory=True)
            item = tv_item(library / "Game of Thrones")
            with self.assertRaisesRegex(ValueError, "outside"):
                ripqueue.tv_review_capture_dir(
                    item, ripqueue.compute_target_dir(item), "c" * 64, alias
                )

    def test_review_path_rejects_child_symlinks_into_tv_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "Media"
            library = storage / "TV Shows"
            show = library / "Game of Thrones"
            show.mkdir(parents=True)
            root = storage / ".repair-quarantine" / "burndvd-review"
            root.mkdir(parents=True)
            item = tv_item(show)

            (root / "Game of Thrones").symlink_to(show, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "outside|escaped"):
                ripqueue.tv_review_capture_dir(
                    item, ripqueue.compute_target_dir(item), "c" * 64
                )

            (root / "Game of Thrones").unlink()
            (root / "Game of Thrones").mkdir()
            (root / "Game of Thrones" / "Season 04").symlink_to(
                show, target_is_directory=True
            )
            with self.assertRaisesRegex(ValueError, "outside|escaped"):
                ripqueue.tv_review_capture_dir(
                    item, ripqueue.compute_target_dir(item), "c" * 64
                )

    def test_review_path_rejects_dotdot_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(
                Path(tmp) / "Media" / "TV Shows" / "Bad", title=".."
            )
            with self.assertRaisesRegex(ValueError, "safe"):
                ripqueue.validate_queue_item(item)
            with self.assertRaisesRegex(ValueError, "safe"):
                ripqueue.tv_review_capture_dir(
                    item, ripqueue.compute_target_dir(item), "c" * 64
                )

    def test_protected_move_never_replaces_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mkv"
            destination = Path(tmp) / "destination.mkv"
            source.write_bytes(b"fresh-review-bytes")
            destination.write_bytes(b"existing-review-bytes")
            with self.assertRaises(FileExistsError):
                ripqueue.move_with_progress_noclobber(
                    source, destination, interval=0.001,
                    partial_root=Path(tmp) / "partials",
                )
            self.assertEqual(source.read_bytes(), b"fresh-review-bytes")
            self.assertEqual(destination.read_bytes(), b"existing-review-bytes")

    def test_hardlink_publish_remains_successful_if_partial_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "private.partial"
            destination = Path(tmp) / "episode.mkv"
            source.write_bytes(b"complete-episode")
            real_unlink = os.unlink

            def fail_only_partial(path, *args, **kwargs):
                if os.fspath(path) in {os.fspath(source), source.name}:
                    raise OSError(errno.EIO, "injected partial cleanup failure")
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(ripqueue.sys, "platform", "unsupported"), \
                    mock.patch.object(ripqueue.os, "unlink", new=fail_only_partial):
                ripqueue._atomic_rename_noreplace(source, destination)

            self.assertEqual(destination.read_bytes(), b"complete-episode")
            self.assertEqual(source.read_bytes(), b"complete-episode")

    def test_protected_move_publishes_through_owned_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mkv"
            destination = root / "destination.mkv"
            lock_dir = root / "Season 04"
            source.write_bytes(b"new-episode")
            claim_path = ripqueue.tv_slot_claim_path(destination)
            claim_identity = ripqueue._create_tv_slot_claim(claim_path)
            ripqueue.move_with_progress_noclobber(
                source, destination, interval=0.001,
                placeholder_identity=claim_identity,
                claim_path=claim_path,
                partial_root=root / "partials",
                lock_dir=lock_dir,
            )
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"new-episode")
            self.assertFalse(claim_path.exists())

    def test_protected_move_rejects_replaced_zero_byte_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mkv"
            destination = root / "destination.mkv"
            source.write_bytes(b"new-episode")
            claim_path = ripqueue.tv_slot_claim_path(destination)
            original = ripqueue._create_tv_slot_claim(claim_path)
            claim_path.unlink()
            claim_path.touch()
            with self.assertRaisesRegex(FileExistsError, "claim ownership changed"):
                ripqueue.move_with_progress_noclobber(
                    source, destination, interval=0.001,
                    placeholder_identity=original,
                    claim_path=claim_path,
                    partial_root=root / "partials",
                    lock_dir=root / "Season 04",
                )
            self.assertEqual(source.read_bytes(), b"new-episode")
            self.assertFalse(destination.exists())
            self.assertEqual(claim_path.read_bytes(), b"")

    def test_protected_move_rejects_fifo_claim_without_hanging(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = r'''
from pathlib import Path
import os, sys
sys.path.insert(0, sys.argv[1])
import ripqueue
root = Path(sys.argv[2])
source = root / "source.mkv"
destination = root / "destination.mkv"
source.write_bytes(b"episode")
claim_path = ripqueue.tv_slot_claim_path(destination)
claim = ripqueue._create_tv_slot_claim(claim_path)
claim_path.unlink()
os.mkfifo(claim_path)
try:
    ripqueue.move_with_progress_noclobber(
        source, destination,
        placeholder_identity=claim,
        claim_path=claim_path,
        partial_root=root / "partials",
        lock_dir=root / "Season 04",
        interval=0.001,
    )
except OSError:
    pass
else:
    raise AssertionError("FIFO replacement was accepted")
assert source.read_bytes() == b"episode"
assert claim_path.is_fifo()
assert not destination.exists()
'''
            result = subprocess.run(
                [sys.executable, "-c", script, str(BIN), tmp],
                text=True, capture_output=True, timeout=2,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_protected_sync_move_does_not_reacquire_held_season_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = r'''
from pathlib import Path
import os, sys
sys.path.insert(0, sys.argv[1])
import ripqueue
root = Path(sys.argv[2])
source = root / "source.mkv"
destination = root / "Season 04" / "destination.mkv"
destination.parent.mkdir()
source.write_bytes(b"episode")
claim_path = ripqueue.tv_slot_claim_path(destination)
claim = ripqueue._create_tv_slot_claim(claim_path)
with ripqueue.season_dir_lock(destination.parent, what="outer test"):
    ripqueue.move_with_progress_noclobber(
        source, destination,
        placeholder_identity=claim,
        claim_path=claim_path,
        partial_root=root / "partials",
        lock_dir=destination.parent,
        publish_lock_held=True,
        interval=0.001,
    )
assert destination.read_bytes() == b"episode"
'''
            result = subprocess.run(
                [sys.executable, "-c", script, str(BIN), tmp],
                text=True, capture_output=True, timeout=3,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_season_lock_is_local_and_unifies_smb_and_nfs_views(self):
        smb = Path(
            "/Volumes/Media/TV Shows/Game of Thrones/Season 04"
        )
        nfs = Path(
            "/private/nas/media/TV Shows/Game of Thrones/Season 04"
        )
        smb_lock = ripqueue.season_lock_path(smb)
        nfs_lock = ripqueue.season_lock_path(nfs)
        self.assertEqual(smb_lock, nfs_lock)
        self.assertEqual(smb_lock.parent, ripqueue.LOCAL_LOCK_BASE)
        self.assertNotIn("TV Shows", str(smb_lock))

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Season 04"
            target.mkdir()
            with mock.patch.object(
                ripqueue, "LOCAL_LOCK_BASE", Path(tmp) / "host-locks"
            ):
                with ripqueue.season_dir_lock(target, what="test"):
                    lock_path = ripqueue.season_lock_path(target)
                    self.assertTrue(lock_path.is_file())
            self.assertFalse((target / ".burndvd.lock").exists())

    def test_sync_interrupt_removes_every_owned_claim_before_propagating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = [root / "t01.mkv", root / "t02.mkv"]
            destinations = [root / "E01.mkv", root / "E02.mkv"]
            for source in sources:
                source.write_bytes(b"episode")
            claims = ripqueue._claim_tv_destinations(destinations)
            planned = [
                (source, episode, destination)
                for episode, (source, destination) in enumerate(
                    zip(sources, destinations), start=1
                )
            ]

            def interrupted_emit(*args, **kwargs):
                raise KeyboardInterrupt()

            with self.assertRaises(KeyboardInterrupt):
                ripqueue._emit_claimed_tv_moves(
                    planned, claims, interrupted_emit, []
                )

            self.assertTrue(all(source.exists() for source in sources))
            self.assertTrue(all(not claim.exists()
                                for claim, _ in claims.values()))
            self.assertTrue(all(not destination.exists()
                                for destination in destinations))

    def test_protected_move_never_unlinks_replaced_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mkv"
            destination = root / "destination.mkv"
            source.write_bytes(b"original-episode")

            def replace_during_copy(source_stream, output, length):
                data = source_stream.read()
                source.unlink()
                source.write_bytes(b"foreign-source")
                output.write(data)

            with mock.patch.object(
                ripqueue.shutil, "copyfileobj", side_effect=replace_during_copy
            ), self.assertRaisesRegex(OSError, "source path was replaced"):
                ripqueue.move_with_progress_noclobber(
                    source, destination, interval=0.001,
                    partial_root=root / "partials",
                    lock_dir=root / "Season 04",
                )
            self.assertEqual(source.read_bytes(), b"foreign-source")
            self.assertEqual(destination.read_bytes(), b"original-episode")

    def test_protected_move_fsync_failure_never_publishes_or_loses_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mkv"
            destination = root / "destination.mkv"
            source.write_bytes(b"episode")
            with mock.patch.object(
                ripqueue.os, "fsync", side_effect=OSError(errno.EIO, "fsync")
            ), self.assertRaises(OSError):
                ripqueue.move_with_progress_noclobber(
                    source, destination, interval=0.001,
                    partial_root=root / "partials",
                    lock_dir=root / "Season 04",
                )
            self.assertEqual(source.read_bytes(), b"episode")
            self.assertFalse(destination.exists())

    def test_placeholder_cleanup_requires_original_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "destination.mkv"
            destination.touch()
            original = os.lstat(destination)
            destination.unlink()
            destination.touch()
            ripqueue._remove_zero_placeholder(
                destination, (original.st_dev, original.st_ino)
            )
            self.assertTrue(destination.exists())

    def test_later_claim_failure_never_deletes_replaced_earlier_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "episode-01.mkv"
            second = root / "episode-02.mkv"
            first.touch()
            second.touch()
            first_original = os.lstat(first)
            second_original = os.lstat(second)
            identities = {
                first: (first_original.st_dev, first_original.st_ino),
                second: (second_original.st_dev, second_original.st_ino),
            }

            # Simulate another publisher replacing our first claim just before
            # creation of a later slot fails and triggers claim rollback.
            first.unlink()
            first.write_bytes(b"foreign-completed-episode")
            ripqueue._remove_claimed_placeholders([first, second], identities)

            self.assertEqual(first.read_bytes(), b"foreign-completed-episode")
            self.assertFalse(second.exists())

    def test_noncollision_claim_failure_rolls_back_only_owned_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destinations = [root / "episode-01.mkv", root / "episode-02.mkv"]
            real_create = ripqueue._create_tv_slot_claim
            first_claim = ripqueue.tv_slot_claim_path(destinations[0])
            calls = 0

            def fail_second(path):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return real_create(path)
                first_claim.unlink()
                first_claim.write_bytes(b"foreign-claim")
                raise OSError(errno.ENOSPC, "disk full")

            with mock.patch.object(
                ripqueue, "_create_tv_slot_claim", side_effect=fail_second
            ), self.assertRaises(OSError):
                ripqueue._claim_tv_destinations(destinations)
            self.assertEqual(first_claim.read_bytes(), b"foreign-claim")

    def test_background_collision_never_deletes_foreign_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "staging" / "source.mkv"
            source.parent.mkdir()
            source.write_bytes(b"fresh-review-bytes-longer")
            destination = root / "review" / "destination.mkv"
            destination.parent.mkdir()
            destination.write_bytes(b"old")
            args = SimpleNamespace(
                verify=False,
                rerip_review=True,
                no_subocr=True,
                no_sound=True,
                state=str(root / "state.json"),
            )
            result = {
                "files": [str(destination)],
                "move_jobs": [
                    (source, destination, "review move", True, None, None,
                     root / "partials", destination.parent)
                ],
                "staging": str(source.parent),
            }
            ripqueue._run_finalize(args, result, tv_item(root), 2)
            self.assertEqual(source.read_bytes(), b"fresh-review-bytes-longer")
            self.assertEqual(destination.read_bytes(), b"old")

    def test_failed_normal_tv_rip_salvages_outside_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "Media"
            item = tv_item(storage / "TV Shows" / "Game of Thrones")
            target = ripqueue.compute_target_dir(item)
            staging = Path(tmp) / "staging"
            staging.mkdir()
            (staging / "title_t01.mkv").write_bytes(b"complete-title")
            args = SimpleNamespace(
                rerip_review=False,
                auto_rerip_review=True,
                overwrite=False,
                current_disc_fingerprint="e" * 64,
                review_root=None,
                state=str(Path(tmp) / "review-state.json"),
            )
            with mock.patch.object(ripqueue, "_title_complete", return_value=True):
                ok, message = ripqueue._salvage_and_fail(
                    args, item, {"titles": {}}, 2, staging, target, "read failed"
                )
            review = ripqueue.tv_review_capture_dir(
                item, target, "e" * 64
            ) / "_partial" / "Game of Thrones - S04D2 - t01.mkv"
            self.assertFalse(ok)
            self.assertIn("SALVAGED", message)
            self.assertEqual(review.read_bytes(), b"complete-title")
            self.assertFalse((target / "_partial").exists())

    def test_review_partial_collision_preserves_both_files_even_with_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "Media"
            item = tv_item(storage / "TV Shows" / "Game of Thrones")
            target = ripqueue.compute_target_dir(item)
            staging = Path(tmp) / "staging"
            staging.mkdir()
            source = staging / "title_t01.mkv"
            source.write_bytes(b"fresh-review-bytes")
            review = ripqueue.tv_review_capture_dir(item, target, "d" * 64)
            partial = review / "_partial"
            partial.mkdir(parents=True)
            destination = partial / "Game of Thrones - S04D2 - t01.mkv"
            destination.write_bytes(b"existing-review-bytes")
            args = SimpleNamespace(
                rerip_review=True,
                auto_rerip_review=False,
                overwrite=True,
                current_disc_fingerprint="d" * 64,
                review_root=None,
                state=str(Path(tmp) / "review-state.json"),
            )
            with mock.patch.object(ripqueue, "_title_complete", return_value=True):
                ok, message = ripqueue._salvage_and_fail(
                    args, item, {"titles": {}}, 2, staging, target, "read failed"
                )
            self.assertFalse(ok)
            self.assertIn("staging PRESERVED", message)
            self.assertEqual(source.read_bytes(), b"fresh-review-bytes")
            self.assertEqual(destination.read_bytes(), b"existing-review-bytes")


class HardeningRegressionTests(unittest.TestCase):
    def _finalize_args(self, root: Path, *, verify=False, overwrite=False):
        return SimpleNamespace(
            verify=verify,
            overwrite=overwrite,
            disc_receipts=str(root / "receipts.jsonl"),
            rerip_review=False,
            no_subocr=True,
            no_sound=True,
            state=str(root / "state.json"),
        )

    def test_verify_uses_xerror_and_treats_error_stderr_as_failure(self):
        completed = SimpleNamespace(returncode=0, stderr="decode corruption")
        with mock.patch.object(
            ripqueue.subprocess, "run", return_value=completed
        ) as run:
            ok, message = ripqueue.verify(["episode.mkv"])
        self.assertFalse(ok)
        self.assertIn("decode corruption", message)
        self.assertIn("-xerror", run.call_args.args[0])

    def test_sync_verify_movie_defers_transaction_and_preserves_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "movie-staging"
            staging.mkdir()
            source = staging / "movie.mkv"
            source.write_bytes(b"movie")
            destination = Path(tmp) / "Movies" / "Movie.mkv"
            jobs = [(source, destination, "movie")]
            result = ripqueue._rip_publication_result(
                SimpleNamespace(sync_move=True, verify=True),
                3.0, [str(destination)], jobs, staging,
            )
            self.assertEqual(result["move_jobs"], jobs)
            self.assertEqual(result["staging"], str(staging))
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_sync_verify_tv_extras_defers_transaction_and_preserves_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "tv-staging"
            staging.mkdir()
            source = staging / "disc_t09.mkv"
            source.write_bytes(b"extra")
            destination = Path(tmp) / "Season 01" / "Extras" / "extra.mkv"
            jobs = [(source, destination, "extra")]
            result = ripqueue._rip_publication_result(
                SimpleNamespace(sync_move=True, verify=True),
                2.0, [str(destination)], jobs, staging,
            )
            self.assertEqual(result["move_jobs"], jobs)
            self.assertEqual(result["staging"], str(staging))
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())

    def test_contractless_unknown_tv_keeps_zero_friction_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = ripqueue.QueueItem(
                title="Unknown Show", type="tv-season", discs=2,
                target_root=tmp, format="BD", season=1, episode_start=4,
                contract_source="none",
            )
            ripqueue.validate_queue_item(item)
            paths = [Path("disc_t01.mkv"), Path("disc_t02.mkv")]
            self.assertEqual(
                ripqueue.partition_episode_contract(paths, item),
                (paths, [], []),
            )
            self.assertEqual(
                ripqueue.tv_contract_output_mode(
                    item, ripqueue.compute_target_dir(item)
                ),
                ("normal", None, []),
            )

    def test_contract_requires_exact_ids_and_one_row_per_disc(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "exact expected_title_ids"):
                ripqueue.validate_queue_item(
                    tv_item(Path(tmp), expected_title_ids=[])
                )
            with self.assertRaisesRegex(ValueError, "one queue row"):
                ripqueue.validate_queue_item(tv_item(Path(tmp), discs=4))

    def test_stale_state_version_and_changed_live_contract_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = tv_item(root)
            state_path = root / "state.json"
            state = ripqueue.default_state([item])
            state["version"] = ripqueue.STATE_VERSION - 1
            state_path.write_text(json.dumps(state))
            args = SimpleNamespace(state=str(state_path))
            with self.assertRaisesRegex(ValueError, "schema version"):
                ripqueue.load_or_init(args, [item])

            state = ripqueue.default_state([item])
            state_path.write_text(json.dumps(state))
            corrected = tv_item(root, expected_title_ids=[4, 5, 6])
            with self.assertRaisesRegex(ValueError, "stale state contract"):
                ripqueue.load_or_init(args, [corrected])

    def test_manual_generic_label_allowed_but_registry_binding_stays_strict(self):
        manual = tv_item(Path("/tmp"), contract_source="manual")
        generic = {"cinfo": {2: "DISC_2"}, "titles": {}}
        self.assertEqual(ripqueue.bound_physical_disc(manual, generic, 1), 2)
        registry = tv_item(Path("/tmp"), contract_source="registry")
        with self.assertRaisesRegex(ValueError, "title mismatch"):
            ripqueue.bound_physical_disc(registry, generic, 1)

    def test_short_title_must_match_complete_label_token(self):
        item = tv_item(
            Path("/tmp"), title="Up", contract_source="manual"
        )
        info = {"cinfo": {2: "CUP_SEASON_4_DISC_2"}, "titles": {}}
        with self.assertRaisesRegex(ValueError, "title mismatch"):
            ripqueue.bound_physical_disc(item, info, 1)

    def test_probe_retry_exhaustion_is_nonzero_policy(self):
        self.assertEqual(ripqueue.probe_exhaustion_action("retry"), "abort")
        self.assertEqual(ripqueue.probe_exhaustion_action("abort"), "abort")
        self.assertEqual(ripqueue.probe_exhaustion_action("skip"), "skip")

    def test_dead_owner_claim_is_reclaimed_before_review_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp))
            season = ripqueue.compute_target_dir(item)
            season.mkdir()
            destination = season / "Game of Thrones - S04E03.mkv"
            claim = ripqueue.tv_slot_claim_path(destination)
            claim.write_text(json.dumps({
                "version": 1,
                "pid": 999_999_999,
                "host": os.uname().nodename,
                "boot_id": ripqueue._host_boot_id(),
                "created_at": time.time() - 10,
                "lease_expires_at": time.time() + 3600,
                "token": "dead",
            }))
            with ripqueue.season_dir_lock(season, what="test stale claim"):
                mode = ripqueue.tv_contract_output_mode(
                    item, season, auto_rerip_review=True
                )
            self.assertEqual(mode, ("normal", None, []))
            self.assertFalse(claim.exists())

    def test_review_root_swap_to_library_symlink_cannot_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "Media" / "TV Shows"
            show = library / "Show"
            target = show / "Season 01"
            target.mkdir(parents=True)
            review_root = root / "review"
            review_root.mkdir()
            item = ripqueue.QueueItem(
                title="Show", type="tv-season", discs=1,
                target_root=str(show), format="BD", season=1,
                episode_start=1, expected_episodes=1,
                expected_disc_episodes=1, expected_title_ids=[1],
                expected_physical_disc=1, contract_source="manual",
            )
            capture = ripqueue.tv_review_capture_dir(
                item, target, "a" * 64, review_root
            )
            original = root / "review-original"
            review_root.rename(original)
            review_root.symlink_to(library, target_is_directory=True)
            source = root / "episode.mkv"
            source.write_bytes(b"episode")
            with self.assertRaises(OSError):
                ripqueue.move_with_progress_noclobber(
                    source, capture / "title-t01.mkv",
                    partial_root=capture / ".partials",
                    interval=0.001,
                )
            self.assertEqual(source.read_bytes(), b"episode")
            self.assertFalse((library / "Show" / "Season 01" / "title-t01.mkv").exists())

    def test_multi_job_second_publish_failure_rolls_back_entire_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            sources = [staging / "one.mkv", staging / "two.mkv"]
            for index, source in enumerate(sources, start=1):
                source.write_bytes(f"episode-{index}".encode())
            destinations = [root / "out" / "one.mkv", root / "out" / "two.mkv"]
            result = {
                "files": [str(path) for path in destinations],
                "move_jobs": [
                    (source, destination, "move")
                    for source, destination in zip(sources, destinations)
                ],
                "staging": str(staging),
            }
            real_publish = ripqueue._publish_prepared
            calls = 0

            def fail_second(prepared):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected second publish failure")
                return real_publish(prepared)

            with mock.patch.object(
                ripqueue, "_publish_prepared", side_effect=fail_second
            ):
                ok, message = ripqueue._run_finalize(
                    self._finalize_args(root), result,
                    ripqueue.QueueItem(
                        "Movie (2000)", "movie", 1, str(root / "out"), "BD"
                    ), 1,
                )
            self.assertFalse(ok)
            self.assertIn("second publish", message)
            self.assertTrue(all(source.exists() for source in sources))
            self.assertTrue(all(not path.exists() for path in destinations))
            self.assertTrue(staging.exists())

    def test_receipt_failure_rolls_media_back_and_preserves_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            sources = [staging / "movie-a.mkv", staging / "movie-b.mkv"]
            for index, source in enumerate(sources):
                source.write_bytes(f"movie-{index}".encode())
            destinations = [
                root / "out" / "movie-a.mkv",
                root / "out" / "movie-b.mkv",
            ]
            result = {
                "files": [str(destination) for destination in destinations],
                "move_jobs": [
                    (source, destination, "move")
                    for source, destination in zip(sources, destinations)
                ],
                "staging": str(staging),
                "disc_fingerprint": "f" * 64,
            }
            with mock.patch.object(
                ripqueue, "verify", return_value=(True, "")
            ), mock.patch.object(
                ripqueue, "append_disc_receipt",
                side_effect=OSError("receipt unavailable"),
            ):
                ok, message = ripqueue._run_finalize(
                    self._finalize_args(root, verify=True), result,
                    ripqueue.QueueItem(
                        "Movie (2000)", "movie", 1, str(root / "out"), "BD"
                    ), 1,
                )
            self.assertFalse(ok)
            self.assertIn("receipt unavailable", message)
            self.assertTrue(all(source.exists() for source in sources))
            self.assertTrue(all(not destination.exists()
                                for destination in destinations))
            self.assertTrue(staging.exists())

    def test_corrupt_prepared_movie_never_reaches_visible_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            source = staging / "movie.mkv"
            source.write_bytes(b"corrupt movie")
            destination = root / "out" / "movie.mkv"
            result = {
                "files": [str(destination)],
                "move_jobs": [(source, destination, "move")],
                "staging": str(staging),
            }
            with mock.patch.object(
                ripqueue, "verify", return_value=(False, "decode corruption")
            ), mock.patch.object(
                ripqueue, "_publish_prepared", wraps=ripqueue._publish_prepared
            ) as publish:
                ok, message = ripqueue._run_finalize(
                    self._finalize_args(root, verify=True), result,
                    ripqueue.QueueItem(
                        "Movie (2000)", "movie", 1, str(root / "out"), "BD"
                    ), 1,
                )
            self.assertFalse(ok)
            self.assertIn("before publication", message)
            publish.assert_not_called()
            self.assertFalse(destination.exists())
            self.assertTrue(source.exists())

    def test_replaced_verified_partial_is_rejected_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            source = staging / "movie.mkv"
            source.write_bytes(b"GOOD")
            destination = root / "out" / "movie.mkv"
            item = ripqueue.QueueItem(
                "Movie", "movie", 1, str(root / "out"), "BD"
            )
            with mock.patch.object(
                ripqueue, "verify", return_value=(True, "")
            ):
                prepared = ripqueue._prepare_publication(
                    self._finalize_args(root, verify=True), item,
                    (source, destination, "move"),
                )
            partial = prepared.partial_root / prepared.partial_name
            partial.unlink()
            partial.write_bytes(b"EVIL")
            try:
                with self.assertRaisesRegex(OSError, "identity changed"):
                    ripqueue._publish_prepared(prepared)
                self.assertFalse(destination.exists())
                self.assertEqual(source.read_bytes(), b"GOOD")
            finally:
                ripqueue._close_prepared(prepared)

    def test_partial_swap_between_precheck_and_rename_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            source = staging / "movie.mkv"
            source.write_bytes(b"GOOD")
            destination = root / "out" / "movie.mkv"
            result = {
                "files": [str(destination)],
                "move_jobs": [(source, destination, "move")],
                "staging": str(staging),
            }
            real_publish = ripqueue._atomic_rename_noreplace_at
            swapped = False

            def swap_then_publish(source_name, source_fd,
                                  destination_name, destination_fd):
                nonlocal swapped
                if not swapped and source_name.endswith(".partial"):
                    swapped = True
                    os.unlink(source_name, dir_fd=source_fd)
                    replacement = os.open(
                        source_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600, dir_fd=source_fd,
                    )
                    os.write(replacement, b"EVIL")
                    os.close(replacement)
                return real_publish(
                    source_name, source_fd, destination_name, destination_fd
                )

            with mock.patch.object(
                ripqueue, "verify", return_value=(True, "")
            ), mock.patch.object(
                ripqueue, "_atomic_rename_noreplace_at",
                side_effect=swap_then_publish,
            ):
                ok, message = ripqueue._run_finalize(
                    self._finalize_args(root, verify=True), result,
                    ripqueue.QueueItem(
                        "Movie", "movie", 1, str(root / "out"), "BD"
                    ), 1,
                )
            self.assertFalse(ok)
            self.assertIn("differs from verified partial", message)
            self.assertFalse(destination.exists())
            self.assertEqual(source.read_bytes(), b"GOOD")

    def test_legacy_noclobber_rejects_swap_during_atomic_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mkv"
            source.write_bytes(b"GOOD")
            destination = root / "out" / "episode.mkv"
            partial_root = root / "partials"
            real_publish = ripqueue._atomic_rename_noreplace_at
            swapped = False

            def swap_then_publish(source_name, source_fd,
                                  destination_name, destination_fd):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    os.unlink(source_name, dir_fd=source_fd)
                    replacement = os.open(
                        source_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600, dir_fd=source_fd,
                    )
                    os.write(replacement, b"EVIL")
                    os.close(replacement)
                return real_publish(
                    source_name, source_fd, destination_name, destination_fd
                )

            with mock.patch.object(
                ripqueue, "_atomic_rename_noreplace_at",
                side_effect=swap_then_publish,
            ), self.assertRaisesRegex(OSError, "differs from copied partial"):
                ripqueue.move_with_progress_noclobber(
                    source, destination, partial_root=partial_root,
                    interval=0.001,
                )
            self.assertFalse(destination.exists())
            self.assertEqual(source.read_bytes(), b"GOOD")

    def test_movie_late_collision_preserves_foreign_file_and_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            source = staging / "movie.mkv"
            source.write_bytes(b"NEW")
            destination = root / "out" / "movie.mkv"
            destination.parent.mkdir()
            destination.write_bytes(b"FOREIGN")
            result = {
                "files": [str(destination)],
                "move_jobs": [(source, destination, "move")],
                "staging": str(staging),
            }
            ok, message = ripqueue._run_finalize(
                self._finalize_args(root), result,
                ripqueue.QueueItem(
                    "Movie", "movie", 1, str(root / "out"), "BD"
                ), 1,
            )
            self.assertFalse(ok)
            self.assertIn("destination exists", message)
            self.assertEqual(destination.read_bytes(), b"FOREIGN")
            self.assertEqual(source.read_bytes(), b"NEW")

    def test_explicit_movie_overwrite_uses_transactional_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mkv"
            source.write_bytes(b"NEW")
            destination = root / "out" / "movie.mkv"
            destination.parent.mkdir()
            destination.write_bytes(b"OLD")
            ripqueue.move_with_progress(
                source, destination, overwrite=True, interval=0.001
            )
            self.assertEqual(destination.read_bytes(), b"NEW")
            self.assertFalse(source.exists())

    def test_receipt_error_after_replace_reconciles_as_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "receipts.jsonl"
            item = tv_item(root)
            real_replace = os.replace

            def replace_then_raise(source, destination):
                real_replace(source, destination)
                raise OSError("ambiguous post-replace failure")

            with mock.patch.object(
                ripqueue.os, "replace", side_effect=replace_then_raise
            ):
                transaction = ripqueue.append_disc_receipt(
                    ledger, item, 2, "a" * 64, ["episode.mkv"],
                    run_id="receipt-test-run",
                )
            self.assertTrue(
                ripqueue._receipt_transaction_seen(ledger, transaction)
            )
            self.assertEqual(len(ledger.read_text().splitlines()), 1)

    def test_staging_cleanup_failure_after_receipt_keeps_published_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            source = staging / "movie.mkv"
            source.write_bytes(b"movie")
            destination = root / "out" / "movie.mkv"
            item = ripqueue.QueueItem(
                "Movie (2000)", "movie", 1, str(root / "out"), "BD"
            )
            result = {
                "files": [str(destination)],
                "move_jobs": [(source, destination, "move")],
                "staging": str(staging),
                "disc_fingerprint": "f" * 64,
                "queue_index": 0,
                "run_id": "cleanup-test-run",
                "elapsed_s": 12.0,
            }
            with mock.patch.object(
                ripqueue, "verify", return_value=(True, "")
            ), mock.patch.object(
                ripqueue.shutil, "rmtree",
                side_effect=OSError("injected staging cleanup failure"),
            ):
                ok, message = ripqueue._run_finalize(
                    self._finalize_args(root, verify=True), result, item, 1
                )
            self.assertTrue(ok, message)
            self.assertEqual(destination.read_bytes(), b"movie")
            self.assertTrue(source.exists())
            self.assertTrue(ripqueue.disc_receipt_seen(
                root / "receipts.jsonl", "f" * 64
            ))

    def test_post_receipt_batch_cleanup_failure_keeps_published_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            source = staging / "movie.mkv"
            source.write_bytes(b"movie")
            destination = root / "out" / "movie.mkv"
            item = ripqueue.QueueItem(
                "Movie (2000)", "movie", 1, str(root / "out"), "BD"
            )
            result = {
                "files": [str(destination)],
                "move_jobs": [(source, destination, "move")],
                "staging": str(staging),
                "disc_fingerprint": "e" * 64,
                "queue_index": 0,
                "run_id": "cleanup-test-run",
            }
            with mock.patch.object(
                ripqueue, "verify", return_value=(True, "")
            ), mock.patch.object(
                ripqueue, "_finish_prepared_batch",
                side_effect=OSError("injected post-receipt cleanup failure"),
            ):
                ok, message = ripqueue._run_finalize(
                    self._finalize_args(root, verify=True), result, item, 1
                )
            self.assertTrue(ok, message)
            self.assertEqual(destination.read_bytes(), b"movie")
            self.assertTrue(ripqueue.disc_receipt_seen(
                root / "receipts.jsonl", "e" * 64
            ))

    def test_crash_after_receipt_before_state_save_reconciles_pending_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "out"
            output.mkdir()
            final = output / "Movie (2000).mkv"
            final.write_bytes(b"verified movie")
            item = ripqueue.QueueItem(
                "Movie (2000)", "movie", 1, str(output), "BD"
            )
            state = ripqueue.default_state([item])
            ledger = root / "receipts.jsonl"
            transaction = ripqueue.append_disc_receipt(
                ledger, item, 1, "d" * 64, [str(final)],
                queue_index=0, run_id=state["run_id"], elapsed_s=42.0,
            )
            args = SimpleNamespace(disc_receipts=str(ledger))
            with mock.patch.object(
                ripqueue, "verify", return_value=(True, "")
            ) as verify:
                recovered = ripqueue.reconcile_committed_receipts(state, args)
            self.assertEqual(recovered, 1)
            self.assertEqual(state["current_index"], 1)
            self.assertEqual(state["disc_index_in_item"], 0)
            self.assertEqual(state["completed"][0]["files"], [str(final)])
            self.assertEqual(
                state["completed"][0]["recovered_from_receipt"], transaction
            )
            verify.assert_called_once_with([str(final)])

    def test_crash_recovery_matches_contracted_physical_disc_not_row_ordinal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = tv_item(root, contract_source="manual")
            state = ripqueue.default_state([item])
            season = ripqueue.compute_target_dir(item)
            season.mkdir(parents=True)
            final = season / "Game of Thrones - S04E03.mkv"
            final.write_bytes(b"episode")
            ledger = root / "receipts.jsonl"
            ripqueue.append_disc_receipt(
                ledger, item, 2, "8" * 64, [str(final)],
                queue_index=0, run_id=state["run_id"], queue_disc_n=1,
                disc_episode_count=1,
            )
            with mock.patch.object(
                ripqueue, "verify", return_value=(True, "")
            ):
                recovered = ripqueue.reconcile_committed_receipts(
                    state, SimpleNamespace(disc_receipts=str(ledger))
                )
            self.assertEqual(recovered, 1)
            self.assertEqual(state["current_index"], 1)
            self.assertEqual(state["completed"][0]["disc_n"], 2)

    def test_fresh_run_does_not_recover_an_old_runs_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "out"
            output.mkdir()
            final = output / "Movie.mkv"
            final.write_bytes(b"movie")
            item = ripqueue.QueueItem("Movie", "movie", 1, str(output), "BD")
            old_state = ripqueue.default_state([item])
            ledger = root / "receipts.jsonl"
            ripqueue.append_disc_receipt(
                ledger, item, 1, "c" * 64, [str(final)], queue_index=0,
                run_id=old_state["run_id"],
            )
            fresh_state = ripqueue.default_state([item])
            with mock.patch.object(ripqueue, "verify") as verify:
                recovered = ripqueue.reconcile_committed_receipts(
                    fresh_state, SimpleNamespace(disc_receipts=str(ledger))
                )
            self.assertEqual(recovered, 0)
            self.assertEqual(fresh_state["current_index"], 0)
            verify.assert_not_called()

    def test_receipt_recovery_restores_contractless_multidisc_episode_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = ripqueue.QueueItem(
                "Unknown Show", "tv-season", 3, str(root / "Unknown Show"),
                "BD", season=1, episode_start=1, contract_source="none",
            )
            state = ripqueue.default_state([item])
            ripqueue.record_episode_count(item, 1, 4, state)
            state["disc_index_in_item"] = 1  # disc 2 was pending at crash
            season = ripqueue.compute_target_dir(item)
            season.mkdir(parents=True)
            finals = []
            for episode in range(5, 8):
                final = season / f"Unknown Show - S01E{episode:02d}.mkv"
                final.write_bytes(f"episode-{episode}".encode())
                finals.append(str(final))
            ledger = root / "receipts.jsonl"
            ripqueue.append_disc_receipt(
                ledger, item, 2, "9" * 64, finals,
                queue_index=0, run_id=state["run_id"],
                disc_episode_count=3,
            )
            with mock.patch.object(
                ripqueue, "verify", return_value=(True, "")
            ):
                recovered = ripqueue.reconcile_committed_receipts(
                    state, SimpleNamespace(disc_receipts=str(ledger))
                )
            self.assertEqual(recovered, 1)
            self.assertEqual(
                state["disc_episode_counts"][ripqueue.ep_key(item)],
                {"1": 4, "2": 3},
            )
            self.assertEqual(state["disc_index_in_item"], 2)
            self.assertEqual(ripqueue.starting_ep(item, 3, state), 8)

    def test_receipt_recovery_resolves_path_before_target_containment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            outside = root / "outside"
            outside.mkdir()
            escaped = outside / "Movie.mkv"
            escaped.write_bytes(b"movie")
            link = target / "redirect"
            link.symlink_to(outside, target_is_directory=True)
            item = ripqueue.QueueItem("Movie", "movie", 1, str(target), "BD")
            state = ripqueue.default_state([item])
            ledger = root / "receipts.jsonl"
            ripqueue.append_disc_receipt(
                ledger, item, 1, "b" * 64, [str(link / escaped.name)],
                queue_index=0, run_id=state["run_id"],
            )
            with self.assertRaisesRegex(RuntimeError, "escapes target root"):
                ripqueue.reconcile_committed_receipts(
                    state, SimpleNamespace(disc_receipts=str(ledger))
                )

    def test_save_state_preserves_old_file_on_pre_replace_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            old = {"version": 1, "value": "old"}
            new = {"version": 1, "value": "new"}
            ripqueue.save_state(old, str(path))
            with mock.patch.object(
                ripqueue.os, "replace", side_effect=OSError("replace denied")
            ), self.assertRaisesRegex(OSError, "replace denied"):
                ripqueue.save_state(new, str(path))
            self.assertEqual(json.loads(path.read_text()), old)
            self.assertEqual(list(path.parent.glob(".state.json.*.tmp")), [])

    def test_save_state_reconciles_error_after_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            ripqueue.save_state({"value": "old"}, str(path))
            real_replace = os.replace

            def replace_then_raise(source, destination):
                real_replace(source, destination)
                raise OSError("ambiguous replace result")

            with mock.patch.object(
                ripqueue.os, "replace", side_effect=replace_then_raise
            ):
                ripqueue.save_state({"value": "new"}, str(path))
            self.assertEqual(json.loads(path.read_text()), {"value": "new"})

    def test_noverify_overwrite_cleanup_failure_never_loses_final_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            sources = [staging / "one.mkv", staging / "two.mkv"]
            destinations = [root / "out" / "one.mkv", root / "out" / "two.mkv"]
            for index, (source, destination) in enumerate(
                zip(sources, destinations), start=1
            ):
                source.write_bytes(f"NEW-{index}".encode())
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(f"OLD-{index}".encode())
            result = {
                "files": [str(path) for path in destinations],
                "move_jobs": [
                    (source, destination, "move")
                    for source, destination in zip(sources, destinations)
                ],
                "staging": str(staging),
            }
            real_unlink = os.unlink
            backup_unlinks = 0

            def fail_second_backup_cleanup(path, *args, **kwargs):
                nonlocal backup_unlinks
                if "burndvd-backup" in os.fspath(path):
                    backup_unlinks += 1
                    if backup_unlinks == 2:
                        raise OSError("injected second backup cleanup failure")
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(
                ripqueue.os, "unlink", side_effect=fail_second_backup_cleanup
            ):
                ok, message = ripqueue._run_finalize(
                    self._finalize_args(root, overwrite=True), result,
                    ripqueue.QueueItem(
                        "Movie", "movie", 1, str(root / "out"), "BD"
                    ), 1,
                )
            self.assertTrue(ok, message)
            self.assertEqual(destinations[0].read_bytes(), b"NEW-1")
            self.assertEqual(destinations[1].read_bytes(), b"NEW-2")

    def test_backup_restore_collision_preserves_foreign_and_old_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            source = staging / "movie.mkv"
            source.write_bytes(b"NEW")
            destination = root / "out" / "movie.mkv"
            destination.parent.mkdir()
            destination.write_bytes(b"OLD")
            result = {
                "files": [str(destination)],
                "move_jobs": [(source, destination, "move")],
                "staging": str(staging),
                "disc_fingerprint": "7" * 64,
            }
            real_publish = ripqueue._atomic_rename_noreplace_at

            def collide_with_backup_restore(source_name, source_fd,
                                            destination_name, destination_fd):
                if "burndvd-backup" in source_name:
                    foreign = os.open(
                        destination_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600, dir_fd=destination_fd,
                    )
                    os.write(foreign, b"FOREIGN")
                    os.close(foreign)
                return real_publish(
                    source_name, source_fd, destination_name, destination_fd
                )

            with mock.patch.object(
                ripqueue, "verify", return_value=(True, "")
            ), mock.patch.object(
                ripqueue, "append_disc_receipt",
                side_effect=OSError("receipt failed"),
            ), mock.patch.object(
                ripqueue, "_atomic_rename_noreplace_at",
                side_effect=collide_with_backup_restore,
            ):
                ok, message = ripqueue._run_finalize(
                    self._finalize_args(root, verify=True, overwrite=True),
                    result,
                    ripqueue.QueueItem(
                        "Movie", "movie", 1, str(root / "out"), "BD"
                    ), 1,
                )
            self.assertFalse(ok)
            self.assertIn("ROLLBACK INCOMPLETE", message)
            self.assertEqual(destination.read_bytes(), b"FOREIGN")
            backups = list(destination.parent.glob(
                ".movie.mkv.burndvd-backup-*"
            ))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"OLD")

    def test_rollback_failure_is_reported_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "staging"
            staging.mkdir()
            source = staging / "movie.mkv"
            source.write_bytes(b"movie")
            destination = root / "out" / "movie.mkv"
            result = {
                "files": [str(destination)],
                "move_jobs": [(source, destination, "move")],
                "staging": str(staging),
                "disc_fingerprint": "f" * 64,
            }
            real_unlink = os.unlink

            def refuse_destination_unlink(path, *args, **kwargs):
                if os.fspath(path) == destination.name and kwargs.get("dir_fd"):
                    raise OSError("injected rollback denial")
                return real_unlink(path, *args, **kwargs)

            with mock.patch.object(
                ripqueue, "verify", return_value=(True, "")
            ), mock.patch.object(
                ripqueue, "append_disc_receipt",
                side_effect=OSError("receipt unavailable"),
            ), mock.patch.object(
                ripqueue.os, "unlink", side_effect=refuse_destination_unlink
            ):
                ok, message = ripqueue._run_finalize(
                    self._finalize_args(root, verify=True), result,
                    ripqueue.QueueItem(
                        "Movie (2000)", "movie", 1, str(root / "out"), "BD"
                    ), 1,
                )
            self.assertFalse(ok)
            self.assertIn("ROLLBACK INCOMPLETE", message)
            self.assertTrue(destination.exists())


class DiscIdentityTests(unittest.TestCase):
    def test_wrong_physical_disc_is_rejected_before_rip(self):
        item = tv_item(Path("/tmp"), expected_physical_disc=2)
        info = {"cinfo": {2: "GAMEOFTHRONES_S4_DISC3"}, "titles": {}}
        with self.assertRaisesRegex(ValueError, "expects disc 2"):
            ripqueue.bound_physical_disc(item, info, 1)

    def test_same_disc_number_wrong_show_is_rejected(self):
        item = tv_item(Path("/tmp"), expected_physical_disc=2)
        info = {"cinfo": {2: "SOUTH PARK SEASON 4 DISC 2"}, "titles": {}}
        with self.assertRaisesRegex(ValueError, "title mismatch"):
            ripqueue.bound_physical_disc(item, info, 1)

    def test_same_show_and_disc_wrong_season_is_rejected(self):
        item = tv_item(Path("/tmp"), expected_physical_disc=2)
        info = {"cinfo": {2: "GAME OF THRONES SEASON 5 DISC 2"}, "titles": {}}
        with self.assertRaisesRegex(ValueError, "season mismatch"):
            ripqueue.bound_physical_disc(item, info, 1)

    def test_exact_title_season_disc_tuple_is_accepted(self):
        item = tv_item(Path("/tmp"), expected_physical_disc=2)
        info = {"cinfo": {2: "GAME OF THRONES SEASON 4 DISC 2"}, "titles": {}}
        self.assertEqual(ripqueue.bound_physical_disc(item, info, 1), 2)

    def test_unlabelled_tv_disc_fails_closed(self):
        item = tv_item(Path("/tmp"), expected_physical_disc=2)
        info = {"cinfo": {2: "GAMEOFTHRONES"}, "titles": {}}
        with self.assertRaisesRegex(ValueError, "cannot verify"):
            ripqueue.bound_physical_disc(item, info, 1)

    def test_disc_fingerprint_is_stable_and_title_sensitive(self):
        first = {
            "cinfo": {2: "GAMEOFTHRONES_S4_DISC2"},
            "titles": {2: {9: "0:55:00", 11: "2"}, 1: {9: "0:56:00", 11: "1"}},
        }
        reordered = {
            "titles": {1: {11: "1", 9: "0:56:00"}, 2: {11: "2", 9: "0:55:00"}},
            "cinfo": {2: "GAMEOFTHRONES_S4_DISC2"},
        }
        self.assertEqual(
            ripqueue.disc_content_fingerprint(first),
            ripqueue.disc_content_fingerprint(reordered),
        )
        reordered["titles"][2][9] = "0:54:00"
        self.assertNotEqual(
            ripqueue.disc_content_fingerprint(first),
            ripqueue.disc_content_fingerprint(reordered),
        )

    def test_published_receipt_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "receipts.jsonl"
            item = tv_item(Path(tmp))
            ripqueue.append_disc_receipt(
                ledger, item, 2, "abc", ["episode.mkv"], run_id="ledger-test"
            )
            self.assertTrue(ripqueue.disc_receipt_seen(ledger, "abc"))
            self.assertFalse(ripqueue.disc_receipt_seen(ledger, "def"))

    def test_corrupt_receipt_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "receipts.jsonl"
            ledger.write_text("{broken\n")
            with self.assertRaisesRegex(RuntimeError, "corrupt"):
                ripqueue.disc_receipt_seen(ledger, "abc")

    def test_extra_collision_preserves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fresh.mkv"
            destination = Path(tmp) / "existing.mkv"
            source.write_bytes(b"fresh")
            destination.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                ripqueue.require_free_extra_destination(source, destination, False)
            self.assertEqual(source.read_bytes(), b"fresh")
            self.assertEqual(destination.read_bytes(), b"existing")

    def test_decoded_duplicate_blocks_publication(self):
        candidate = Path("candidate.mkv")
        existing = {1: Path("episode.mkv")}
        with mock.patch.object(ripqueue, "ffprobe_dur_s", return_value=3300), \
             mock.patch.object(ripqueue, "_content_relation", return_value="duplicate"):
            duplicate, error = ripqueue.existing_content_collision(
                candidate, 3300, existing
            )
        self.assertEqual(duplicate, existing[1])
        self.assertIsNone(error)

    def test_unreadable_same_duration_identity_fails_closed(self):
        candidate = Path("candidate.mkv")
        existing = {1: Path("episode.mkv")}
        with mock.patch.object(ripqueue, "ffprobe_dur_s", return_value=3300), \
             mock.patch.object(ripqueue, "_content_relation", return_value="unknown"):
            duplicate, error = ripqueue.existing_content_collision(
                candidate, 3300, existing
            )
        self.assertIsNone(duplicate)
        self.assertIn("cannot prove", error)
