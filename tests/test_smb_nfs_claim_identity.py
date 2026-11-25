"""Regression tests for SMB->NFS st_dev claim identity handling and cleanup."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from burndvd import nas, ripper


def _fake_stat_with_dev(real_stat: os.stat_result, fake_dev: int) -> os.stat_result:
    values = list(real_stat)
    values[2] = fake_dev  # st_dev index in os.stat_result
    return os.stat_result(values)


def _make_dual_mount_stat_mocks(
    smb: Path, nfs: Path, smb_dev: int, nfs_dev: int, *, smb_offline: bool = False
):
    """Return consistent (fake_lstat, fake_fstat, fake_open) simulating SMB vs NFS st_dev."""
    real_lstat = os.lstat
    real_fstat = os.fstat
    real_open = os.open
    fd_dev_map: dict[int, int] = {}

    def _dev_for_path(p: str) -> int | None:
        if p == str(nfs) or p.startswith(str(nfs) + os.sep):
            return nfs_dev
        if p == str(smb) or p.startswith(str(smb) + os.sep):
            return smb_dev
        return None

    def fake_lstat(path, *args, **kwargs):
        p = os.fspath(path)
        if smb_offline and (p == str(smb) or p.startswith(str(smb) + os.sep)):
            raise OSError("SMB offline")
        st = real_lstat(path, *args, **kwargs)
        dev = _dev_for_path(p)
        return _fake_stat_with_dev(st, dev) if dev is not None else st

    def fake_open(path, *args, **kwargs):
        p = os.fspath(path)
        if smb_offline and (p == str(smb) or p.startswith(str(smb) + os.sep)):
            raise OSError("SMB offline")
        fd = real_open(path, *args, **kwargs)
        dev = _dev_for_path(p)
        if dev is not None:
            fd_dev_map[fd] = dev
        else:
            fd_dev_map.pop(fd, None)
        return fd

    def fake_fstat(fd, *args, **kwargs):
        st = real_fstat(fd, *args, **kwargs)
        dev = fd_dev_map.get(fd)
        return _fake_stat_with_dev(st, dev) if dev is not None else st

    return fake_lstat, fake_fstat, fake_open


class TestSmbToNfsClaimIdentity(unittest.TestCase):
    def setUp(self):
        ripper.clear_finalize_errors()

    def test_claim_tv_destinations_normalizes_smb_to_nfs_before_claiming(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smb = root / "smb"
            nfs = root / "nfs"
            smb_season = smb / "TV Shows" / "Dragon Ball Super" / "Season 01"
            nfs_season = nfs / "TV Shows" / "Dragon Ball Super" / "Season 01"
            smb_season.mkdir(parents=True)
            nfs_season.mkdir(parents=True)

            smb_dst = smb_season / "Dragon Ball Super - S01E27.mkv"
            nfs_dst = nfs_season / "Dragon Ball Super - S01E27.mkv"

            real_ismount = os.path.ismount

            def fake_ismount(path):
                if Path(path) in (smb, nfs):
                    return True
                return real_ismount(path)

            with mock.patch.object(nas, "SMB_MEDIA_PREFIX", str(smb)), \
                 mock.patch.object(nas, "NFS_MEDIA_PREFIX", str(nfs)), \
                 mock.patch.object(nas.os.path, "ismount", side_effect=fake_ismount):
                claims = nas._claim_tv_destinations([smb_dst])
                self.assertIn(smb_dst, claims)
                claim_path, claim_identity = claims[smb_dst]
                self.assertEqual(claim_path, nas.tv_slot_claim_path(nfs_dst))
                self.assertTrue(claim_path.is_file())
                st = os.lstat(claim_path)
                self.assertEqual(claim_identity, (st.st_dev, st.st_ino))
                nas._remove_zero_placeholder(claim_path, claim_identity)
                self.assertFalse(claim_path.exists())

    def test_finalize_smb_to_nfs_failover_rebinds_st_dev_and_cleans_claim(self):
        """Reproduces exact SMB(st_dev=905970169) -> NFS(st_dev=436208140) same-inode
        claim mismatch during background finalize failover."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smb = root / "smb"
            nfs = root / "nfs"
            stage = root / "stage"
            stage.mkdir()

            smb_season = smb / "TV Shows" / "Dragon Ball Super" / "Season 01"
            nfs_season = nfs / "TV Shows" / "Dragon Ball Super" / "Season 01"
            smb_partial = smb / ".repair-quarantine" / "partials"
            nfs_partial = nfs / ".repair-quarantine" / "partials"
            smb_season.mkdir(parents=True)
            nfs_season.mkdir(parents=True)
            smb_partial.mkdir(parents=True)
            nfs_partial.mkdir(parents=True)

            src = stage / "Dragon Ball Super P3 D1_t00.mkv"
            src.write_bytes(b"EPISODE-27-PAYLOAD")

            smb_dst = smb_season / "Dragon Ball Super - S01E27.mkv"
            nfs_dst = nfs_season / "Dragon Ball Super - S01E27.mkv"
            smb_claim = nas.tv_slot_claim_path(smb_dst)
            nfs_claim = nas.tv_slot_claim_path(nfs_dst)

            # Create claim via SMB path, hardlink into NFS path so both views share
            # the exact same st_ino (like Synology SMB+NFS), then record SMB st_dev.
            real_ident = nas._create_tv_slot_claim(smb_claim)
            os.link(smb_claim, nfs_claim)
            smb_dev = 905970169
            nfs_dev = 436208140
            smb_placeholder_identity = (smb_dev, real_ident[1])

            fake_lstat, fake_fstat, fake_open = _make_dual_mount_stat_mocks(
                smb, nfs, smb_dev, nfs_dev
            )
            real_ismount = os.path.ismount

            def fake_ismount(path):
                if Path(path) == nfs:
                    return True
                if Path(path) == smb:
                    return False
                return real_ismount(path)

            args = SimpleNamespace(
                state=None,
                verify=False,
                no_subocr=True,
                no_sound=True,
                overwrite=False,
            )
            item = SimpleNamespace(title="Dragon Ball Super", type="tv-season")
            result = {
                "files": [str(smb_dst)],
                "move_jobs": [
                    (
                        src,
                        smb_dst,
                        "moving episode 27",
                        True,
                        smb_placeholder_identity,
                        smb_claim,
                        smb_partial,
                        smb_season,
                    )
                ],
                "staging": str(stage),
            }

            with mock.patch.object(nas, "SMB_MEDIA_PREFIX", str(smb)), \
                 mock.patch.object(nas, "NFS_MEDIA_PREFIX", str(nfs)), \
                 mock.patch.object(nas.os.path, "ismount", side_effect=fake_ismount), \
                 mock.patch.object(nas.os, "lstat", side_effect=fake_lstat), \
                 mock.patch.object(nas.os, "fstat", side_effect=fake_fstat), \
                 mock.patch.object(nas.os, "open", side_effect=fake_open), \
                 mock.patch.object(ripper.os, "lstat", side_effect=fake_lstat), \
                 mock.patch.object(nas.time, "sleep"), \
                 mock.patch.object(ripper, "beep"), \
                 mock.patch.object(ripper, "append_log"), \
                 mock.patch.object(ripper, "run_subocr_postrip"):
                ok = ripper._run_finalize(args, result, item, 1)

            self.assertTrue(
                ok,
                f"expected finalize success, got errors: {ripper.get_finalize_errors()}",
            )
            self.assertEqual(ripper.get_finalize_errors(), [])
            self.assertTrue(nfs_dst.is_file())
            self.assertEqual(nfs_dst.read_bytes(), b"EPISODE-27-PAYLOAD")
            self.assertFalse(nfs_claim.exists(), "NFS claim must be removed after publish")
            self.assertFalse(src.exists(), "staged source must be retired after publish")
            self.assertEqual(result["files"], [str(nfs_dst)])

    def test_cleanup_owned_claims_removes_smb_claim_via_nfs_when_smb_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smb = root / "smb"
            nfs = root / "nfs"
            smb_season = smb / "TV Shows" / "Dragon Ball Super" / "Season 01"
            nfs_season = nfs / "TV Shows" / "Dragon Ball Super" / "Season 01"
            smb_season.mkdir(parents=True)
            nfs_season.mkdir(parents=True)

            smb_dst = smb_season / "Dragon Ball Super - S01E28.mkv"
            nfs_dst = nfs_season / "Dragon Ball Super - S01E28.mkv"
            smb_claim = nas.tv_slot_claim_path(smb_dst)
            nfs_claim = nas.tv_slot_claim_path(nfs_dst)

            real_ident = nas._create_tv_slot_claim(nfs_claim)
            smb_dev = 905970169
            nfs_dev = 436208140
            smb_placeholder_identity = (smb_dev, real_ident[1])

            fake_lstat, fake_fstat, fake_open = _make_dual_mount_stat_mocks(
                smb, nfs, smb_dev, nfs_dev, smb_offline=True
            )
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
                 mock.patch.object(nas.os, "lstat", side_effect=fake_lstat), \
                 mock.patch.object(nas.os, "fstat", side_effect=fake_fstat), \
                 mock.patch.object(nas.os, "open", side_effect=fake_open):
                nas._remove_zero_placeholder(smb_claim, smb_placeholder_identity)

            self.assertFalse(nfs_claim.exists(), "owned claim must be cleaned via NFS fallback")

    def test_different_st_dev_rejected_if_foreign_pid_or_zero_byte_or_wrong_ino_or_non_nas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smb = root / "smb"
            nfs = root / "nfs"
            nfs_season = nfs / "TV Shows" / "Dragon Ball Super" / "Season 01"
            nfs_season.mkdir(parents=True)

            nfs_dst = nfs_season / "Dragon Ball Super - S01E29.mkv"
            nfs_claim = nas.tv_slot_claim_path(nfs_dst)
            smb_dev = 905970169
            nfs_dev = 436208140
            fake_lstat, fake_fstat, fake_open = _make_dual_mount_stat_mocks(
                smb, nfs, smb_dev, nfs_dev
            )
            real_ismount = os.path.ismount

            def fake_ismount(path):
                if Path(path) == nfs:
                    return True
                return real_ismount(path)

            # Case 1: Foreign PID on same st_ino across different st_dev -> MUST REJECT
            foreign_pid = os.getpid() + 12345
            nfs_claim.write_text(f"{foreign_pid}\n", encoding="utf-8")
            st_real = os.lstat(nfs_claim)
            with mock.patch.object(nas, "SMB_MEDIA_PREFIX", str(smb)), \
                 mock.patch.object(nas, "NFS_MEDIA_PREFIX", str(nfs)), \
                 mock.patch.object(nas.os.path, "ismount", side_effect=fake_ismount), \
                 mock.patch.object(nas.os, "lstat", side_effect=fake_lstat), \
                 mock.patch.object(nas.os, "fstat", side_effect=fake_fstat), \
                 mock.patch.object(nas.os, "open", side_effect=fake_open):
                self.assertFalse(
                    nas._claim_identity_matches(
                        nfs_claim,
                        _fake_stat_with_dev(st_real, nfs_dev),
                        (smb_dev, st_real.st_ino),
                    )
                )
                nas._remove_zero_placeholder(nfs_claim, (smb_dev, st_real.st_ino))
            self.assertTrue(nfs_claim.exists(), "foreign PID claim must never be unlinked")

            # Case 2: Zero-byte claim on same st_ino across different st_dev -> MUST REJECT
            nfs_claim.write_bytes(b"")
            st_zero = os.lstat(nfs_claim)
            with mock.patch.object(nas, "SMB_MEDIA_PREFIX", str(smb)), \
                 mock.patch.object(nas, "NFS_MEDIA_PREFIX", str(nfs)), \
                 mock.patch.object(nas.os.path, "ismount", side_effect=fake_ismount), \
                 mock.patch.object(nas.os, "lstat", side_effect=fake_lstat), \
                 mock.patch.object(nas.os, "fstat", side_effect=fake_fstat), \
                 mock.patch.object(nas.os, "open", side_effect=fake_open):
                self.assertFalse(
                    nas._claim_identity_matches(
                        nfs_claim,
                        _fake_stat_with_dev(st_zero, nfs_dev),
                        (smb_dev, st_zero.st_ino),
                    )
                )

            # Case 3: Current PID but DIFFERENT st_ino across different st_dev -> MUST REJECT
            nfs_claim.write_text(f"{os.getpid()}\n", encoding="utf-8")
            st_cur = os.lstat(nfs_claim)
            with mock.patch.object(nas, "SMB_MEDIA_PREFIX", str(smb)), \
                 mock.patch.object(nas, "NFS_MEDIA_PREFIX", str(nfs)), \
                 mock.patch.object(nas.os.path, "ismount", side_effect=fake_ismount), \
                 mock.patch.object(nas.os, "lstat", side_effect=fake_lstat), \
                 mock.patch.object(nas.os, "fstat", side_effect=fake_fstat), \
                 mock.patch.object(nas.os, "open", side_effect=fake_open):
                self.assertFalse(
                    nas._claim_identity_matches(
                        nfs_claim,
                        _fake_stat_with_dev(st_cur, nfs_dev),
                        (smb_dev, st_cur.st_ino + 1),
                    )
                )

            # Case 4: Non-NAS local path with different st_dev -> MUST REJECT
            local_claim = root / ".local.mkv.burndvd-claim"
            local_claim.write_text(f"{os.getpid()}\n", encoding="utf-8")
            st_local = os.lstat(local_claim)
            with mock.patch.object(nas, "SMB_MEDIA_PREFIX", str(smb)), \
                 mock.patch.object(nas, "NFS_MEDIA_PREFIX", str(nfs)):
                self.assertFalse(
                    nas._claim_identity_matches(
                        local_claim,
                        _fake_stat_with_dev(st_local, nfs_dev),
                        (smb_dev, st_local.st_ino),
                    )
                )


if __name__ == "__main__":
    unittest.main()
