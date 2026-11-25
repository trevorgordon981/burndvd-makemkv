"""Safety, episode-contract, review-isolation, and atomic no-clobber tests."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from burndvd import compat_ripqueue as ripqueue
from burndvd import metadata

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER_PATH = REPO_ROOT / "bin" / "burndvd"


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
        contract = metadata.episode_contract("Game of Thrones", 4, "GAMEOFTHRONES_S4_DISC1")
        self.assertEqual(contract["episode_start"], 1)
        self.assertEqual(contract["expected_title_ids"], [1, 3])
        self.assertEqual(contract["expected_disc_episodes"], 2)
        self.assertEqual(contract["expected_episodes"], 10)

    def test_s4_disc1_contract_selects_t01_and_t03_not_25_minute_bonus(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(
                Path(tmp),
                episode_start=1,
                expected_disc_episodes=2,
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
                    "Game of Thrones", season, f"GAMEOFTHRONES_S{season}_DISC{disc}"
                )
                for disc in range(1, 5)
            ]
            self.assertEqual(sum(c["expected_disc_episodes"] for c in contracts), 10)
            self.assertEqual(
                contracts[-1]["episode_start"] + contracts[-1]["expected_disc_episodes"] - 1,
                10,
            )

    def test_d_label_is_parsed_without_confusing_season(self):
        contract = metadata.episode_contract("Gameofthrones", 1, "GAMEOFTHRONES_S1_D3")
        self.assertEqual(contract["disc"], 3)
        self.assertEqual(contract["episode_start"], 6)

    def test_s6_disc1_uses_audited_noncontiguous_title_ids(self):
        contract = metadata.episode_contract("Game of Thrones", 6, "GAMEOFTHRONES_S6_DISC1")
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
            ripqueue.run_policy_error([movie], overwrite=True, rerip_review=False)
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn(
                "forbidden for TV",
                ripqueue.run_policy_error(
                    [tv_item(Path(tmp))], overwrite=True, rerip_review=True
                ),
            )

    def test_default_wrapper_enables_verification(self):
        wrapper = WRAPPER_PATH.read_text()
        self.assertIn("--non-interactive --on-fail=retry --verify", wrapper)

    def test_empty_mode_array_is_safe_under_macos_bash_nounset(self):
        wrapper = WRAPPER_PATH.read_text()
        safe = '${RIPQUEUE_MODE_ARGS[@]+"${RIPQUEUE_MODE_ARGS[@]}"}'
        self.assertEqual(wrapper.count(safe), 3)

    def _wrapper_mode(
        self,
        season_dir: Path,
        first_ep: int = 9,
        last_ep: int = 10,
        expected: int = 10,
        recognized: bool = True,
        initial_args=(),
    ):
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
            WRAPPER=str(WRAPPER_PATH),
            SEASON_DIR=str(season_dir),
            FIRST_EP=str(first_ep),
            LAST_EP=str(last_ep),
            EXPECTED=str(expected),
            RECOGNIZED="1" if recognized else "0",
            INITIAL_ARGS=" ".join(initial_args),
        )
        return subprocess.run(
            ["/bin/bash", "-c", script], env=env, text=True, capture_output=True
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

    def test_wrapper_still_rejects_episode_beyond_season_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            season = Path(tmp)
            (season / "Game of Thrones - S04E11.mkv").write_bytes(b"episode")
            result = self._wrapper_mode(season)
        self.assertEqual(result.returncode, 65)
        self.assertIn("beyond authoritative total 10", result.stderr)

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

    def test_auto_review_treats_hidden_slot_claim_as_occupied(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp))
            season = ripqueue.compute_target_dir(item)
            season.mkdir()
            destination = season / "Game of Thrones - S04E03.mkv"
            ripqueue._create_tv_slot_claim(ripqueue.tv_slot_claim_path(destination))
            mode, error, occupied = ripqueue.tv_contract_output_mode(
                item, season, auto_rerip_review=True
            )
        self.assertEqual((mode, error, occupied), ("review", None, [3]))

    def test_rip_boundary_rejects_tv_overwrite_even_if_cli_check_is_bypassed(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = tv_item(Path(tmp))
            args = SimpleNamespace(overwrite=True)
            ok, message = ripqueue.rip(
                args, item, {"titles": {}}, [], ripqueue.compute_target_dir(item), {}, 2
            )
        self.assertFalse(ok)
        self.assertIn("forbidden for TV", message)

    def test_auto_review_requires_exact_registry_contract(self):
        item = tv_item(Path("/tmp"))
        info = {"cinfo": {2: "GAME OF THRONES SEASON 4 DISC 2"}, "titles": {}}
        self.assertIsNone(ripqueue.automatic_review_contract_error(item, info))
        tampered = tv_item(Path("/tmp"), episode_start=4)
        self.assertIn("does not match", ripqueue.automatic_review_contract_error(tampered, info))
        unknown = tv_item(Path("/tmp"), title="Unknown Show")
        self.assertIn("registry-recognized", ripqueue.automatic_review_contract_error(unknown, info))


class ReviewIsolationTests(unittest.TestCase):
    def test_review_path_is_outside_smb_tv_library(self):
        item = tv_item(Path("/Volumes/Media/TV Shows/Game of Thrones"))
        target = ripqueue.compute_target_dir(item)
        review = ripqueue.tv_review_capture_dir(item, target, "a" * 64)
        self.assertEqual(
            review,
            Path("/Volumes/Media/.repair-quarantine/burndvd-review/")
            / "Game of Thrones/Season 04"
            / ("a" * 16),
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
            / "Game of Thrones/Season 04"
            / ("b" * 16),
        )

    def test_repeated_review_reads_reserve_distinct_attempt_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "review" / ("a" * 16)
            staging = Path(tmp) / "staging" / "1788465079-17165"
            first = ripqueue.reserve_tv_review_attempt_dir(capture, staging)
            second = ripqueue.reserve_tv_review_attempt_dir(capture, staging)
            capture = capture.resolve()
            self.assertEqual(first, capture / "attempt-1788465079-17165")
            self.assertEqual(second, capture / "attempt-1788465079-17165-2")

    def test_preexisting_attempt_symlink_is_not_followed_or_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "review" / ("c" * 16)
            capture.mkdir(parents=True)
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (capture / "attempt-staging").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "escaped"):
                ripqueue.reserve_tv_review_attempt_dir(capture, Path(tmp) / "staging")
            self.assertEqual(list(outside.iterdir()), [])

    def test_protected_move_never_replaces_existing_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mkv"
            destination = Path(tmp) / "destination.mkv"
            source.write_bytes(b"fresh-review-bytes")
            destination.write_bytes(b"existing-review-bytes")
            with self.assertRaises(FileExistsError):
                ripqueue.move_with_progress_noclobber(
                    source, destination, interval=0.001, partial_root=Path(tmp) / "partials"
                )
            self.assertEqual(source.read_bytes(), b"fresh-review-bytes")
            self.assertEqual(destination.read_bytes(), b"existing-review-bytes")

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
                source,
                destination,
                interval=0.001,
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
                    source,
                    destination,
                    interval=0.001,
                    placeholder_identity=original,
                    claim_path=claim_path,
                    partial_root=root / "partials",
                    lock_dir=root / "Season 04",
                )
            self.assertEqual(source.read_bytes(), b"new-episode")
            self.assertFalse(destination.exists())

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
                    source,
                    destination,
                    interval=0.001,
                    partial_root=root / "partials",
                    lock_dir=root / "Season 04",
                )
            self.assertEqual(source.read_bytes(), b"episode")
            self.assertFalse(destination.exists())

    def test_season_lock_is_local_and_unifies_smb_and_nfs_views(self):
        smb = Path("/Volumes/Media/TV Shows/Game of Thrones/Season 04")
        nfs = Path("/private/nas/media/TV Shows/Game of Thrones/Season 04")
        smb_lock = ripqueue.season_lock_path(smb)
        nfs_lock = ripqueue.season_lock_path(nfs)
        self.assertEqual(smb_lock, nfs_lock)
        self.assertEqual(smb_lock.parent, ripqueue.LOCAL_LOCK_BASE)

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
            review = (
                ripqueue.tv_review_capture_dir(item, target, "e" * 64)
                / "attempt-staging"
                / "_partial"
                / "Game of Thrones - S04D2 - t01.mkv"
            )
            self.assertFalse(ok)
            self.assertIn("SALVAGED", message)
            self.assertEqual(review.read_bytes(), b"complete-title")
            self.assertFalse((target / "_partial").exists())


class DiscIdentityTests(unittest.TestCase):
    def test_wrong_physical_disc_is_rejected_before_rip(self):
        item = tv_item(Path("/tmp"), expected_physical_disc=2)
        info = {"cinfo": {2: "GAMEOFTHRONES_S4_DISC3"}, "titles": {}}
        with self.assertRaisesRegex(ValueError, "expects disc 2"):
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

    def test_published_receipt_and_corrupt_ledger_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "receipts.jsonl"
            item = tv_item(Path(tmp))
            ripqueue.append_disc_receipt(ledger, item, 2, "abc", ["episode.mkv"])
            self.assertTrue(ripqueue.disc_receipt_seen(ledger, "abc"))
            self.assertFalse(ripqueue.disc_receipt_seen(ledger, "def"))
            ledger.write_text("{broken\n")
            with self.assertRaisesRegex(RuntimeError, "corrupt"):
                ripqueue.disc_receipt_seen(ledger, "abc")

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


if __name__ == "__main__":
    unittest.main()
