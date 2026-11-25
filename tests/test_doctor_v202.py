"""Isolated unit tests for `burndvd.cli.doctor`."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import namedtuple
from pathlib import Path
from typing import Any, Sequence

import pytest

from burndvd.cli import doctor

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
GIB = 1024**3


def _fake_usage(free_gb: float, total_gb: float = 1000.0) -> DiskUsage:
    free_b = int(free_gb * GIB)
    total_b = int(total_gb * GIB)
    return DiskUsage(total=total_b, used=max(0, total_b - free_b), free=free_b)


def _setup_healthy_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    missing_binaries: set[str] | None = None,
    smb_ismount: bool = True,
    nfs_ismount: bool = True,
    smb_free_gb: float = 500.0,
    nfs_free_gb: float = 400.0,
    staging_free_gb: float = 200.0,
    drutil_list_out: str | None = None,
    drutil_status_out: str | None = None,
    ocr_missing: list[str] | None = None,
    subocr_bin_present: bool = True,
    platform: str = "darwin",
) -> dict[str, Path]:
    """Configure a completely isolated host environment for doctor checks."""
    missing = set(missing_binaries or ())

    smb_dir = tmp_path / "nas_smb"
    nfs_dir = tmp_path / "nas_nfs"
    staging_dir = tmp_path / "staging"
    venv_dir = tmp_path / "subocr_venv"
    venv_python = venv_dir / "bin" / "python"
    bin_dir = tmp_path / "bin"
    for d in (smb_dir, nfs_dir, staging_dir, venv_dir / "bin", bin_dir):
        d.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    venv_python.chmod(0o755)

    subocr_bin = bin_dir / "subocr"
    if subocr_bin_present:
        subocr_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        subocr_bin.chmod(0o755)

    # Clear binary env overrides so host env cannot leak into tests.
    for spec in doctor.CORE_BINARIES:
        monkeypatch.delenv(spec.env_var, raising=False)
    monkeypatch.setenv("NAS_SMB_ROOT", str(smb_dir))
    monkeypatch.setenv("NAS_NFS_ROOT", str(nfs_dir))
    monkeypatch.setenv("STAGING_ROOT", str(staging_dir))
    monkeypatch.setenv("SUBOCR_VENV", str(venv_dir))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(doctor.sys, "platform", platform)

    bin_paths = {
        "makemkvcon": "/mock/bin/makemkvcon",
        "mkvmerge": "/mock/bin/mkvmerge",
        "mkvextract": "/mock/bin/mkvextract",
        "ffmpeg": "/mock/bin/ffmpeg",
        "ffprobe": "/mock/bin/ffprobe",
        "drutil": "/mock/bin/drutil",
        "diskutil": "/mock/bin/diskutil",
        "lsof": "/mock/bin/lsof",
    }

    def fake_which(name: str) -> str | None:
        if name == "subocr":
            return str(subocr_bin) if subocr_bin_present else None
        if name in missing:
            return None
        return bin_paths.get(name)

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(
        doctor,
        "_is_executable_file",
        lambda p: p == str(venv_python)
        or (subocr_bin_present and p == str(subocr_bin)),
    )
    monkeypatch.setattr(
        doctor,
        "_read_makemkv_bundle_version",
        lambda path: "v1.18.3" if path == bin_paths["makemkvcon"] else None,
    )

    def fake_ismount(path: Any) -> bool:
        p = os.fspath(path)
        if p == str(smb_dir):
            return smb_ismount
        if p == str(nfs_dir):
            return nfs_ismount
        return False

    monkeypatch.setattr(os.path, "ismount", fake_ismount)

    def fake_disk_usage(path: Any) -> DiskUsage:
        p = os.fspath(path)
        if p == str(smb_dir):
            return _fake_usage(smb_free_gb, 4000.0)
        if p == str(nfs_dir):
            return _fake_usage(nfs_free_gb, 4000.0)
        return _fake_usage(staging_free_gb, 1000.0)

    monkeypatch.setattr(shutil, "disk_usage", fake_disk_usage)

    default_list = (
        "   Vendor   Product           Rev   Bus       SupportLevel\n"
        "1  HL-DT-ST BD-RE BU40N       1.03  USB       Unsupported\n"
    )
    default_status = (
        " Vendor   Product           Rev \n"
        " HL-DT-ST BD-RE BU40N       1.03\n\n"
        "           Type: BD-ROM               Name: /dev/disk5\n"
    )
    ocr_missing_list = list(ocr_missing or [])

    def fake_run(
        cmd: Sequence[str],
        timeout: float = doctor.SUBPROCESS_TIMEOUT_S,
    ) -> subprocess.CompletedProcess[str]:
        exe = cmd[0]
        args = list(cmd[1:])
        if exe == str(venv_python):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(ocr_missing_list) + "\n", stderr=""
            )
        if exe.endswith("mkvmerge"):
            return subprocess.CompletedProcess(
                cmd, 0, stdout="mkvmerge v99.0 ('Buka') 64-bit\n", stderr=""
            )
        if exe.endswith("mkvextract"):
            return subprocess.CompletedProcess(
                cmd, 0, stdout="mkvextract v99.0 ('Buka') 64-bit\n", stderr=""
            )
        if exe.endswith("ffmpeg"):
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="ffmpeg version 8.1 Copyright (c) 2000-2026 the FFmpeg developers\n",
                stderr="",
            )
        if exe.endswith("ffprobe"):
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="ffprobe version 8.1 Copyright (c) 2007-2026 the FFmpeg developers\n",
                stderr="",
            )
        if exe.endswith("lsof"):
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="",
                stderr="lsof version information:\n    revision: 4.91\n",
            )
        if exe.endswith("drutil") and args == ["list"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=default_list if drutil_list_out is None else drutil_list_out,
                stderr="",
            )
        if exe.endswith("drutil") and args == ["status"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=default_status if drutil_status_out is None else drutil_status_out,
                stderr="",
            )
        raise AssertionError(f"Unexpected subprocess command in test: {cmd!r}")

    monkeypatch.setattr(doctor, "_default_run_cmd", fake_run)

    return {
        "smb": smb_dir,
        "nfs": nfs_dir,
        "staging": staging_dir,
        "venv": venv_dir,
    }


def _by_name(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["name"]: check for check in report["checks"]}


def test_all_healthy_json_and_exit_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup_healthy_env(monkeypatch, tmp_path)

    rc = doctor.main(["--json"])
    out = capsys.readouterr().out
    report = json.loads(out)

    assert rc == 0
    assert report["ok"] is True
    assert report["exit_code"] == 0
    assert report["summary"]["fail"] == 0
    assert report["summary"]["warn"] == 0
    assert report["summary"]["ok"] == len(report["checks"])

    checks = _by_name(report)
    assert checks["binary:makemkvcon"]["details"]["version"] == "v1.18.3"
    assert checks["binary:ffmpeg"]["details"]["version"] == "ffmpeg version 8.1"
    assert checks["binary:lsof"]["details"]["version"] == "lsof 4.91"
    assert checks["optical_drive"]["status"] == "ok"
    assert checks["optical_drive"]["details"]["media_type"] == "BD-ROM"
    assert checks["optical_drive"]["details"]["device_name"] == "/dev/disk5"
    assert checks["nas:smb"]["status"] == "ok"
    assert checks["nas:nfs"]["status"] == "ok"
    assert checks["staging"]["status"] == "ok"
    assert checks["subocr"]["status"] == "ok"


def test_all_healthy_human_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup_healthy_env(monkeypatch, tmp_path)

    rc = doctor.main([])
    out = capsys.readouterr().out

    assert rc == 0
    assert "burndvd doctor" in out
    assert "[OK]" in out
    assert "[FAIL]" not in out
    assert "Summary:" in out
    assert "(exit 0)" in out


@pytest.mark.parametrize(
    ("missing_bin", "expected_hint_fragment"),
    [
        ("ffmpeg", "brew install ffmpeg"),
        ("makemkvcon", "makemkv"),
    ],
)
def test_missing_required_binary_fails_with_install_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    missing_bin: str,
    expected_hint_fragment: str,
) -> None:
    _setup_healthy_env(monkeypatch, tmp_path, missing_binaries={missing_bin})

    rc = doctor.main(["--json"])
    report = json.loads(capsys.readouterr().out)
    checks = _by_name(report)

    assert rc == 1
    assert report["ok"] is False
    assert report["exit_code"] == 1
    target = checks[f"binary:{missing_bin}"]
    assert target["status"] == "fail"
    assert target["required"] is True
    assert expected_hint_fragment.lower() in target["hint"].lower()


def test_unmounted_local_nas_directory_flagged_not_healthy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup_healthy_env(monkeypatch, tmp_path, smb_ismount=False)

    rc = doctor.main(["--json"])
    report = json.loads(capsys.readouterr().out)
    checks = _by_name(report)

    smb = checks["nas:smb"]
    assert smb["status"] in ("warn", "fail")
    assert smb["status"] == "fail"
    assert smb["details"]["exists"] is True
    assert smb["details"]["ismount"] is False
    assert "mount" in smb["hint"].lower()
    assert rc == 1


def test_low_staging_space_flagged_with_remediation_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup_healthy_env(monkeypatch, tmp_path, staging_free_gb=12.5)

    rc = doctor.main(["--json", "--min-staging-gb", "50"])
    report = json.loads(capsys.readouterr().out)
    checks = _by_name(report)

    staging = checks["staging"]
    assert rc == 1
    assert staging["status"] == "fail"
    assert staging["details"]["free_gb"] == 12.5
    assert "below 50.0 GiB" in staging["summary"]
    assert staging["hint"]
    assert "staging" in staging["hint"].lower() or "disk space" in staging["hint"].lower()


def test_staging_uses_nearest_existing_parent_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup_healthy_env(monkeypatch, tmp_path, staging_free_gb=120.0)
    missing_nested = tmp_path / "does_not_exist" / "nested" / "staging"

    result = doctor.check_staging_space(50.0, staging_root=str(missing_nested))

    assert result.status == "ok"
    assert not missing_nested.exists()
    assert result.details["checked_path"] == str(tmp_path)


def test_low_nas_space_warns_without_failing_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup_healthy_env(monkeypatch, tmp_path, smb_free_gb=45.0)

    rc = doctor.main(["--json", "--min-nas-gb", "100"])
    report = json.loads(capsys.readouterr().out)
    checks = _by_name(report)

    assert rc == 0
    assert checks["nas:smb"]["status"] == "warn"
    assert "--min-nas-gb" in checks["nas:smb"]["hint"]


def test_no_optical_drive_and_non_macos_are_graceful(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup_healthy_env(
        monkeypatch,
        tmp_path,
        drutil_list_out="No drives found.\n",
    )

    rc = doctor.main(["--json"])
    report = json.loads(capsys.readouterr().out)
    checks = _by_name(report)
    assert rc == 0
    assert checks["optical_drive"]["status"] == "warn"
    assert checks["optical_drive"]["details"]["drives"] == []

    # Non-macOS platform skips darwin-only tools gracefully without failing.
    _setup_healthy_env(
        monkeypatch,
        tmp_path,
        missing_binaries={"drutil", "diskutil"},
        platform="linux",
    )
    rc_linux = doctor.main(["--json"])
    report_linux = json.loads(capsys.readouterr().out)
    checks_linux = _by_name(report_linux)
    assert rc_linux == 0
    assert checks_linux["binary:drutil"]["status"] == "warn"
    assert checks_linux["optical_drive"]["status"] == "warn"
    assert checks_linux["subocr"]["status"] == "warn"


def test_subocr_missing_modules_warns_with_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _setup_healthy_env(monkeypatch, tmp_path, ocr_missing=["Vision", "Quartz"])

    rc = doctor.main(["--json"])
    report = json.loads(capsys.readouterr().out)
    checks = _by_name(report)

    assert rc == 0
    assert checks["subocr"]["status"] == "warn"
    assert checks["subocr"]["details"]["missing_modules"] == ["Vision", "Quartz"]
    assert "pyobjc-framework-Vision" in checks["subocr"]["hint"]
