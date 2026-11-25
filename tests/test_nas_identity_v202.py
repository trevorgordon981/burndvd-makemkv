"""Regression tests for NAS mount-identity checks, source-inode retirement, and mover polish (v2.0.2)."""

from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from burndvd import nas, ripper
from burndvd.models import QueueItem


def _stage_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class TestNasMountIdentity(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_unmounted_local_backing_dirs_rejected_for_primary_and_nfs_failover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smb = root / "simulated-smb"
            nfs = root / "simulated-nfs"
            smb.mkdir()
            nfs.mkdir()
            self.assertFalse(os.path.ismount(smb))
            self.assertFalse(os.path.ismount(nfs))

            src = _stage_file(root / "stage" / "movie.mkv", b"ORIGINAL RIP")
            dst = smb / "Movies" / "Film" / "Film.mkv"
            args = SimpleNamespace(state=None, verify=False, no_subocr=True, no_sound=True)
            item = SimpleNamespace(title="Film", type="movie")
            result = {
                "files": [str(dst)],
                "move_jobs": [(src, dst, "movie")],
                "staging": str(root / "stage"),
            }

            with mock.patch.object(nas, "SMB_MEDIA_PREFIX", str(smb)), \
                 mock.patch.object(nas, "NFS_MEDIA_PREFIX", str(nfs)), \
                 mock.patch.object(nas.time, "sleep"), \
                 mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                self.assertFalse(nas._dir_writable(dst.parent))
                self.assertFalse(nas._dir_writable(nfs / "Movies" / "Film"))
                self.assertIsNone(nas._await_dir_writable(dst.parent, root / "stage"))
                ok = ripper._run_finalize(args, result, item, 1)

            self.assertFalse(ok)
            self.assertTrue(ripper.get_finalize_errors())
            self.assertTrue(src.is_file())
            self.assertEqual(src.read_bytes(), b"ORIGINAL RIP")
            self.assertFalse((smb / "Movies").exists())
            self.assertFalse((nfs / "Movies").exists())

    def test_mounted_nfs_failover_accepted_when_smb_unmounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smb = root / "simulated-smb"
            nfs = root / "simulated-nfs"
            smb.mkdir()
            nfs.mkdir()
            smb_dir = smb / "Movies" / "Film"
            nfs_dir = nfs / "Movies" / "Film"

            real_ismount = os.path.ismount

            def fake_ismount(path):
                if Path(path) == nfs:
                    return True
                if Path(path) == smb:
                    return False
                return real_ismount(path)

            with mock.patch.object(nas, "SMB_MEDIA_PREFIX", str(smb)), \
                 mock.patch.object(nas, "NFS_MEDIA_PREFIX", str(nfs)), \
                 mock.patch.object(nas.os.path, "ismount", side_effect=fake_ismount), \
                 mock.patch.object(nas.time, "sleep") as sleep_fn:
                self.assertFalse(nas._dir_writable(smb_dir))
                self.assertTrue(nas._dir_writable(nfs_dir))
                got = nas._await_dir_writable(smb_dir, root / "stage")

            self.assertEqual(got, nfs_dir)
            sleep_fn.assert_not_called()
            self.assertFalse(smb_dir.exists())
            self.assertTrue(nfs_dir.is_dir())

    def test_direct_move_refuses_unmounted_nas_backing_dir_and_preserves_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nfs = root / "simulated-nfs"
            nfs.mkdir()
            src = _stage_file(root / "stage" / "movie.mkv", b"STAGED BYTES")
            dst = nfs / "Movies" / "movie.mkv"

            with mock.patch.object(nas, "NFS_MEDIA_PREFIX", str(nfs)):
                with self.assertRaises(OSError) as ctx:
                    nas.move_with_progress(src, dst)

            self.assertEqual(ctx.exception.errno, errno.ENOTCONN)
            self.assertTrue(src.is_file())
            self.assertEqual(src.read_bytes(), b"STAGED BYTES")
            self.assertFalse(dst.exists())
            self.assertFalse((nfs / "Movies").exists())

    def test_local_non_nas_temp_directory_remains_writable_without_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp) / "plain-local-output"
            self.assertIsNone(nas._nas_root_for(local_dir))
            self.assertTrue(nas._verify_nas_mount_identity(local_dir))
            self.assertTrue(nas._dir_writable(local_dir))


class TestMovieSourceRetirementAndPolish(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_concurrent_source_replacement_publishes_original_and_preserves_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = _stage_file(root / "stage" / "movie.mkv", b"ORIGINAL PAYLOAD")
            dst = root / "library" / "movie.mkv"
            backup = src.with_name("original-backup.mkv")
            native_rename = nas._atomic_rename_noreplace

            def replace_source_before_publish(source: Path, destination: Path):
                src.rename(backup)
                _stage_file(src, b"UNRELATED REPLACEMENT")
                native_rename(source, destination)

            with mock.patch.object(
                nas, "_atomic_rename_noreplace", side_effect=replace_source_before_publish
            ):
                with self.assertRaises(OSError) as ctx:
                    nas.move_with_progress(src, dst)

            self.assertIn("replaced", str(ctx.exception).lower())
            self.assertEqual(dst.read_bytes(), b"ORIGINAL PAYLOAD")
            self.assertTrue(src.is_file())
            self.assertEqual(src.read_bytes(), b"UNRELATED REPLACEMENT")
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_bytes(), b"ORIGINAL PAYLOAD")

    def test_explicit_movie_salvage_overwrite_replaces_existing_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = _stage_file(root / "stage" / "movie_t01.mkv", b"NEW MOVIE SALVAGE")
            target_dir = root / "library"
            movie = QueueItem(
                title="Sample Film",
                type="movie",
                discs=1,
                target_root=str(target_dir),
                format="BD",
            )
            name = nas.movie_filename(movie, 1)[:-4]
            dest = _stage_file(target_dir / "_partial" / f"{name} - t01.mkv", b"OLD SALVAGE")
            args = SimpleNamespace(
                state=None,
                verify=False,
                no_subocr=True,
                no_sound=True,
                overwrite=True,
            )

            with mock.patch.object(nas, "_title_complete", return_value=True):
                ok, msg = nas._salvage_and_fail(
                    args, movie, {}, 1, src.parent, target_dir, "mock disc failure"
                )

            self.assertFalse(ok)
            self.assertIn("SALVAGED 1 complete title(s)", msg)
            self.assertEqual(dest.read_bytes(), b"NEW MOVIE SALVAGE")
            self.assertFalse(src.exists())

    def test_movie_salvage_without_overwrite_preserves_existing_and_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = _stage_file(root / "stage" / "movie_t01.mkv", b"NEW MOVIE SALVAGE")
            target_dir = root / "library"
            movie = QueueItem(
                title="Sample Film",
                type="movie",
                discs=1,
                target_root=str(target_dir),
                format="BD",
            )
            name = nas.movie_filename(movie, 1)[:-4]
            dest = _stage_file(target_dir / "_partial" / f"{name} - t01.mkv", b"OLD SALVAGE")
            args = SimpleNamespace(
                state=None,
                verify=False,
                no_subocr=True,
                no_sound=True,
                overwrite=False,
            )

            with mock.patch.object(nas, "_title_complete", return_value=True):
                ok, msg = nas._salvage_and_fail(
                    args, movie, {}, 1, src.parent, target_dir, "mock disc failure"
                )

            self.assertFalse(ok)
            self.assertIn("FAILED", msg)
            self.assertEqual(dest.read_bytes(), b"OLD SALVAGE")
            self.assertTrue(src.is_file())
            self.assertEqual(src.read_bytes(), b"NEW MOVIE SALVAGE")

    def test_fd_exhaustion_after_partial_creation_leaves_no_leaked_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = _stage_file(root / "stage" / "movie.mkv", b"RIP DATA")
            dst = root / "library" / "movie.mkv"

            with mock.patch.object(
                nas.os,
                "dup",
                side_effect=OSError(errno.EMFILE, "injected descriptor exhaustion"),
            ):
                with self.assertRaises(OSError) as ctx:
                    nas.move_with_progress(src, dst)

            self.assertEqual(ctx.exception.errno, errno.EMFILE)
            self.assertTrue(src.is_file())
            self.assertFalse(dst.exists())
            self.assertEqual(list(dst.parent.glob("*.partial")), [])

    def test_fd_exhaustion_in_protected_tv_move_leaves_no_leaked_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = _stage_file(root / "stage" / "ep1.mkv", b"TV RIP DATA")
            dst = root / "library" / "Show" / "Season 01" / "Show - S01E01.mkv"
            partial_root = root / "quarantine" / ".burndvd-partials"

            real_dup = os.dup
            dup_calls = 0

            def fail_on_second_dup(fd):
                nonlocal dup_calls
                dup_calls += 1
                if dup_calls == 2:
                    raise OSError(errno.EMFILE, "injected descriptor exhaustion")
                return real_dup(fd)

            with mock.patch.object(nas.os, "dup", side_effect=fail_on_second_dup):
                with self.assertRaises(OSError) as ctx:
                    nas.move_with_progress_noclobber(
                        src, dst, partial_root=partial_root
                    )

            self.assertEqual(ctx.exception.errno, errno.EMFILE)
            self.assertTrue(src.is_file())
            self.assertFalse(dst.exists())
            self.assertEqual(list(partial_root.glob("*.partial")), [])

    def test_atomic_publish_enotsup_raises_typed_error_and_preserves_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = _stage_file(root / "stage" / "movie.mkv", b"RIP PAYLOAD")
            dst = root / "library" / "movie.mkv"

            def reject(*_args):
                nas.ctypes.set_errno(errno.ENOTSUP)
                return -1

            fake_libc = SimpleNamespace(renameatx_np=reject, renameat2=reject)
            with mock.patch.object(nas.ctypes, "CDLL", return_value=fake_libc), \
                 mock.patch.object(
                     nas.os,
                     "link",
                     side_effect=OSError(errno.EOPNOTSUPP, "hard link unsupported"),
                 ):
                with self.assertRaises(nas.AtomicPublishUnsupportedError):
                    nas.move_with_progress(src, dst)

            self.assertTrue(src.is_file())
            self.assertEqual(src.read_bytes(), b"RIP PAYLOAD")
            self.assertFalse(dst.exists())
            self.assertEqual(list(dst.parent.glob("*.partial")), [])


if __name__ == "__main__":
    unittest.main()
