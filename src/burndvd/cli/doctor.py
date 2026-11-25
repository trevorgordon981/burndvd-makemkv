"""Read-only host readiness diagnostics for `burndvd doctor`."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from burndvd.config import (
    C,
    LOCAL_STAGING_BASE,
    NFS_MEDIA_PREFIX,
    SMB_MEDIA_PREFIX,
)

DEFAULT_MIN_STAGING_GB = 50.0
DEFAULT_MIN_NAS_GB = 100.0
SUBPROCESS_TIMEOUT_S = 3.0

STANDARD_BIN_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
)

OCR_REQUIRED_MODULES = ("Vision", "Quartz", "PIL")

_OCR_PROBE_CODE = (
    "import json\n"
    "missing = []\n"
    "for mod in ('Vision', 'Quartz', 'PIL'):\n"
    "    try:\n"
    "        __import__(mod)\n"
    "    except Exception:\n"
    "        missing.append(mod)\n"
    "print(json.dumps(missing))\n"
)


@dataclass(frozen=True)
class BinarySpec:
    """Metadata describing an external binary checked by `burndvd doctor`."""

    name: str
    env_var: str
    required: bool
    darwin_only: bool = False
    extra_fallbacks: tuple[str, ...] = ()
    install_hint: str = ""
    version_args: tuple[str, ...] = ()


CORE_BINARIES: tuple[BinarySpec, ...] = (
    BinarySpec(
        name="makemkvcon",
        env_var="BURNDVD_MAKEMKVCON",
        required=True,
        extra_fallbacks=("/Applications/MakeMKV.app/Contents/MacOS/makemkvcon",),
        install_hint="Install MakeMKV.app (brew install --cask makemkv) or set BURNDVD_MAKEMKVCON.",
    ),
    BinarySpec(
        name="mkvmerge",
        env_var="BURNDVD_MKVMERGE",
        required=True,
        install_hint="Install MKVToolNix: brew install mkvtoolnix",
        version_args=("--version",),
    ),
    BinarySpec(
        name="mkvextract",
        env_var="BURNDVD_MKVEXTRACT",
        required=True,
        install_hint="Install MKVToolNix: brew install mkvtoolnix",
        version_args=("--version",),
    ),
    BinarySpec(
        name="ffmpeg",
        env_var="BURNDVD_FFMPEG",
        required=True,
        install_hint="Install FFmpeg: brew install ffmpeg",
        version_args=("-version",),
    ),
    BinarySpec(
        name="ffprobe",
        env_var="BURNDVD_FFPROBE",
        required=True,
        install_hint="Install FFmpeg: brew install ffmpeg",
        version_args=("-version",),
    ),
    BinarySpec(
        name="drutil",
        env_var="BURNDVD_DRUTIL",
        required=True,
        darwin_only=True,
        install_hint="macOS system utility (/usr/bin/drutil); requires macOS.",
    ),
    BinarySpec(
        name="diskutil",
        env_var="BURNDVD_DISKUTIL",
        required=True,
        darwin_only=True,
        install_hint="macOS system utility (/usr/sbin/diskutil); requires macOS.",
    ),
    BinarySpec(
        name="lsof",
        env_var="BURNDVD_LSOF",
        required=False,
        install_hint="Install lsof (included on macOS at /usr/sbin/lsof).",
        version_args=("-v",),
    ),
)


@dataclass(frozen=True)
class CheckResult:
    """Structured result for a single diagnostic check."""

    name: str
    category: str
    status: str  # "ok" | "warn" | "fail"
    required: bool
    summary: str
    hint: str = ""
    details: dict[str, Any] = field(default_factory=dict)


CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


def _default_run_cmd(
    cmd: Sequence[str],
    timeout: float = SUBPROCESS_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    """Run a read-only diagnostic subprocess with captured text output."""
    return subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _is_executable_file(path: str) -> bool:
    """Return True if *path* is an existing executable regular file."""
    return os.path.isfile(path) and os.access(path, os.X_OK)


def find_binary(spec: BinarySpec) -> str | None:
    """Resolve an external binary via env override, PATH, or standard locations."""
    override = os.environ.get(spec.env_var, "").strip()
    if override:
        if os.sep in override or "/" in override:
            return override if _is_executable_file(override) else None
        return shutil.which(override)

    found = shutil.which(spec.name)
    if found:
        return found

    for directory in STANDARD_BIN_DIRS:
        candidate = os.path.join(directory, spec.name)
        if _is_executable_file(candidate):
            return candidate

    for candidate in spec.extra_fallbacks:
        if _is_executable_file(candidate):
            return candidate

    return None


def _read_makemkv_bundle_version(binary_path: str) -> str | None:
    """Read MakeMKV version from its macOS Info.plist without touching drives."""
    try:
        real = Path(os.path.realpath(binary_path))
        plist_path = real.parent.parent / "Info.plist"
        if not plist_path.is_file():
            return None
        data = plistlib.loads(plist_path.read_bytes())
        version = data.get("CFBundleShortVersionString") or data.get("CFBundleVersion")
        return str(version).strip() if version else None
    except Exception:
        return None


def _extract_version(
    spec: BinarySpec,
    binary_path: str,
    runner: CommandRunner,
) -> str | None:
    """Return a concise version string for *binary_path*, or None if unavailable."""
    if spec.name == "makemkvcon":
        return _read_makemkv_bundle_version(binary_path)

    if not spec.version_args:
        return None

    try:
        proc = runner([binary_path, *spec.version_args], SUBPROCESS_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None

    combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    if not combined:
        return None

    if spec.name == "lsof":
        match = re.search(r"revision:\s*(\S+)", combined)
        if match:
            return f"lsof {match.group(1)}"

    first_line = combined.splitlines()[0].strip()
    if spec.name in ("ffmpeg", "ffprobe") and " Copyright" in first_line:
        first_line = first_line.split(" Copyright", 1)[0].strip()
    return first_line or None


def check_binaries(
    *,
    platform: str | None = None,
    runner: CommandRunner | None = None,
) -> tuple[list[CheckResult], dict[str, str | None]]:
    """Verify core external binaries and report versions or install hints."""
    plat = platform or sys.platform
    run_cmd = runner or _default_run_cmd
    results: list[CheckResult] = []
    resolved: dict[str, str | None] = {}

    for spec in CORE_BINARIES:
        path = find_binary(spec)
        resolved[spec.name] = path
        is_required = spec.required and (not spec.darwin_only or plat == "darwin")

        if not path:
            status = "fail" if is_required else "warn"
            results.append(
                CheckResult(
                    name=f"binary:{spec.name}",
                    category="binaries",
                    status=status,
                    required=is_required,
                    summary=f"{spec.name} not found",
                    hint=spec.install_hint,
                    details={"binary": spec.name, "path": None, "version": None},
                )
            )
            continue

        version = _extract_version(spec, path, run_cmd)
        summary = f"{path} ({version})" if version else path
        results.append(
            CheckResult(
                name=f"binary:{spec.name}",
                category="binaries",
                status="ok",
                required=is_required,
                summary=summary,
                details={"binary": spec.name, "path": path, "version": version},
            )
        )

    return results, resolved


def _parse_drutil_list(output: str) -> list[str]:
    """Parse drive rows from `drutil list` output."""
    drives: list[str] = []
    for line in output.splitlines():
        match = re.match(r"^\s*\d+\s+(.+?)\s*$", line)
        if match:
            drives.append(re.sub(r"\s{2,}", " ", match.group(1).strip()))
    return drives


def _parse_drutil_status(output: str) -> tuple[str | None, str | None]:
    """Parse media type and BSD device name from `drutil status` output."""
    media_type: str | None = None
    device_name: str | None = None
    for line in output.splitlines():
        if "Type:" in line:
            match = re.search(
                r"Type:\s*(.+?)(?:\s{2,}Name:\s*(\S+)|\s*$)",
                line,
            )
            if match:
                media_type = match.group(1).strip()
                if match.group(2):
                    device_name = match.group(2).strip()
            break
    return media_type, device_name


def check_optical_drive(
    drutil_path: str | None,
    *,
    platform: str | None = None,
    runner: CommandRunner | None = None,
) -> CheckResult:
    """Inspect attached optical drives and media state via read-only `drutil`."""
    plat = platform or sys.platform
    run_cmd = runner or _default_run_cmd

    if plat != "darwin":
        return CheckResult(
            name="optical_drive",
            category="optical_drive",
            status="warn",
            required=False,
            summary=f"Skipped optical drive check on {plat}",
            hint="Optical drive detection uses macOS drutil.",
            details={"drives": [], "media_type": None, "device_name": None},
        )

    if not drutil_path:
        return CheckResult(
            name="optical_drive",
            category="optical_drive",
            status="warn",
            required=False,
            summary="drutil unavailable; cannot inspect optical drives",
            hint="Ensure /usr/bin/drutil is accessible on macOS.",
            details={"drives": [], "media_type": None, "device_name": None},
        )

    try:
        list_proc = run_cmd([drutil_path, "list"], SUBPROCESS_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            name="optical_drive",
            category="optical_drive",
            status="warn",
            required=False,
            summary=f"drutil list failed: {exc}",
            hint="Check optical drive USB/Thunderbolt connection.",
            details={"drives": [], "media_type": None, "device_name": None},
        )

    drives = _parse_drutil_list(list_proc.stdout)
    if not drives:
        return CheckResult(
            name="optical_drive",
            category="optical_drive",
            status="warn",
            required=False,
            summary="No optical drives detected",
            hint="Connect an optical drive and verify power before ripping.",
            details={"drives": [], "media_type": None, "device_name": None},
        )

    media_type: str | None = None
    device_name: str | None = None
    try:
        status_proc = run_cmd([drutil_path, "status"], SUBPROCESS_TIMEOUT_S)
        media_type, device_name = _parse_drutil_status(status_proc.stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass

    media_desc = media_type or "media status unknown"
    if device_name:
        media_desc = f"{media_desc} at {device_name}"
    plural = "drive" if len(drives) == 1 else "drives"
    summary = f"{len(drives)} {plural} detected ({drives[0]}; {media_desc})"
    return CheckResult(
        name="optical_drive",
        category="optical_drive",
        status="ok",
        required=False,
        summary=summary,
        details={
            "drives": drives,
            "media_type": media_type,
            "device_name": device_name,
        },
    )


def _resolve_smb_root(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    return (
        os.environ.get("NAS_SMB_ROOT", "").strip()
        or os.environ.get("BURNDVD_MEDIA_ROOT", "").strip()
        or SMB_MEDIA_PREFIX
    )


def _resolve_nfs_root(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    return (
        os.environ.get("NAS_NFS_ROOT", "").strip()
        or os.environ.get("BURNDVD_NFS_MEDIA_ROOT", "").strip()
        or NFS_MEDIA_PREFIX
    )


def _check_single_nas_mount(
    name: str,
    label: str,
    root: str,
    min_nas_gb: float,
) -> CheckResult:
    """Verify that a NAS mount root is a real mountpoint with adequate free space."""
    if not os.path.exists(root):
        return CheckResult(
            name=name,
            category="nas",
            status="warn",
            required=False,
            summary=f"{label} root not present: {root}",
            hint=f"Mount the {label} NAS share at {root} before ripping.",
            details={
                "path": root,
                "exists": False,
                "ismount": False,
                "free_gb": None,
                "total_gb": None,
            },
        )

    if not os.path.ismount(root):
        return CheckResult(
            name=name,
            category="nas",
            status="fail",
            required=True,
            summary=f"{label} path {root} exists as an unmounted local directory (ismount=False)",
            hint=(
                f"Remove or move the local directory at {root} and mount the {label} "
                "NAS share so rips cannot accidentally fill the local boot disk."
            ),
            details={
                "path": root,
                "exists": True,
                "ismount": False,
                "free_gb": None,
                "total_gb": None,
            },
        )

    try:
        usage = shutil.disk_usage(root)
    except OSError as exc:
        return CheckResult(
            name=name,
            category="nas",
            status="fail",
            required=True,
            summary=f"{label} mount {root} is unreadable: {exc}",
            hint=f"Remount {root} and verify NAS connectivity.",
            details={
                "path": root,
                "exists": True,
                "ismount": True,
                "free_gb": None,
                "total_gb": None,
            },
        )

    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)
    details = {
        "path": root,
        "exists": True,
        "ismount": True,
        "free_gb": round(free_gb, 2),
        "total_gb": round(total_gb, 2),
        "min_nas_gb": min_nas_gb,
    }

    if free_gb < min_nas_gb:
        return CheckResult(
            name=name,
            category="nas",
            status="warn",
            required=False,
            summary=(
                f"{label} mounted at {root} ({free_gb:.1f} GiB free / {total_gb:.1f} GiB total"
                f" — below {min_nas_gb:.1f} GiB minimum)"
            ),
            hint=f"Free space on {root} or lower threshold with --min-nas-gb.",
            details=details,
        )

    return CheckResult(
        name=name,
        category="nas",
        status="ok",
        required=False,
        summary=f"{label} mounted at {root} ({free_gb:.1f} GiB free / {total_gb:.1f} GiB total)",
        details=details,
    )


def check_nas_mounts(
    min_nas_gb: float = DEFAULT_MIN_NAS_GB,
    *,
    smb_root: str | None = None,
    nfs_root: str | None = None,
) -> list[CheckResult]:
    """Check configured SMB and NFS NAS roots for real mount status and free space."""
    return [
        _check_single_nas_mount("nas:smb", "SMB", _resolve_smb_root(smb_root), min_nas_gb),
        _check_single_nas_mount("nas:nfs", "NFS", _resolve_nfs_root(nfs_root), min_nas_gb),
    ]


def _resolve_staging_root(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    raw = (
        os.environ.get("STAGING_ROOT", "").strip()
        or os.environ.get("BURNDVD_STAGING_DIR", "").strip()
    )
    return Path(raw).expanduser() if raw else LOCAL_STAGING_BASE


def _nearest_existing_parent(path: Path) -> Path:
    """Walk upward until an existing directory is found (strictly read-only)."""
    cur = path
    while not os.path.exists(cur) and cur != cur.parent:
        cur = cur.parent
    return cur


def check_staging_space(
    min_staging_gb: float = DEFAULT_MIN_STAGING_GB,
    *,
    staging_root: str | None = None,
) -> CheckResult:
    """Check free space on the local staging filesystem against *min_staging_gb*."""
    target = _resolve_staging_root(staging_root)
    probe = _nearest_existing_parent(target)

    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        return CheckResult(
            name="staging",
            category="staging",
            status="fail",
            required=True,
            summary=f"Cannot inspect staging path {probe}: {exc}",
            hint="Verify BURNDVD_STAGING_DIR / STAGING_ROOT points to an accessible local volume.",
            details={
                "path": str(target),
                "checked_path": str(probe),
                "free_gb": None,
                "total_gb": None,
                "min_staging_gb": min_staging_gb,
            },
        )

    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)
    via = f" (via {probe})" if probe != target else ""
    details = {
        "path": str(target),
        "checked_path": str(probe),
        "free_gb": round(free_gb, 2),
        "total_gb": round(total_gb, 2),
        "min_staging_gb": min_staging_gb,
    }

    if free_gb < min_staging_gb:
        return CheckResult(
            name="staging",
            category="staging",
            status="fail",
            required=True,
            summary=(
                f"{target}{via}: {free_gb:.1f} GiB free / {total_gb:.1f} GiB total"
                f" (below {min_staging_gb:.1f} GiB minimum)"
            ),
            hint=(
                f"Clean old staging files under {target} or free local disk space "
                "(or pass --min-staging-gb)."
            ),
            details=details,
        )

    return CheckResult(
        name="staging",
        category="staging",
        status="ok",
        required=True,
        summary=f"{target}{via}: {free_gb:.1f} GiB free / {total_gb:.1f} GiB total",
        details=details,
    )


def _probe_ocr_modules(
    python_bin: str | None,
    runner: CommandRunner,
) -> tuple[list[str], str | None]:
    """Return `(missing_modules, error)` for Vision, Quartz, and PIL."""
    if python_bin:
        try:
            proc = runner([python_bin, "-c", _OCR_PROBE_CODE], SUBPROCESS_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return list(OCR_REQUIRED_MODULES), str(exc)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
            return list(OCR_REQUIRED_MODULES), err
        try:
            parsed = json.loads(proc.stdout.strip())
            if isinstance(parsed, list):
                return [str(m) for m in parsed], None
        except json.JSONDecodeError as exc:
            return list(OCR_REQUIRED_MODULES), f"invalid probe output: {exc}"
        return list(OCR_REQUIRED_MODULES), "unexpected probe output"

    missing = [
        mod for mod in OCR_REQUIRED_MODULES if importlib.util.find_spec(mod) is None
    ]
    return missing, None


def check_subocr_readiness(
    *,
    platform: str | None = None,
    runner: CommandRunner | None = None,
) -> CheckResult:
    """Check Apple Vision OCR dependencies (`Vision`, `Quartz`, `PIL`) and `subocr` install."""
    plat = platform or sys.platform
    run_cmd = runner or _default_run_cmd

    subocr_bin = shutil.which("subocr")
    if not subocr_bin:
        local_candidate = str(Path.home() / ".local" / "bin" / "subocr")
        if _is_executable_file(local_candidate):
            subocr_bin = local_candidate

    venv_dir = Path(
        os.environ.get(
            "SUBOCR_VENV",
            str(Path.home() / ".local" / "share" / "subocr" / "venv"),
        )
    ).expanduser()
    venv_python: str | None = None
    for candidate_name in ("python", "python3"):
        candidate = str(venv_dir / "bin" / candidate_name)
        if _is_executable_file(candidate):
            venv_python = candidate
            break

    missing, probe_err = _probe_ocr_modules(venv_python, run_cmd)
    interpreter_label = str(venv_dir) if venv_python else sys.executable

    details = {
        "subocr_bin": subocr_bin,
        "subocr_venv": str(venv_dir) if venv_python else None,
        "checked_python": venv_python or sys.executable,
        "missing_modules": missing,
        "probe_error": probe_err,
    }

    if plat != "darwin":
        return CheckResult(
            name="subocr",
            category="subocr",
            status="warn",
            required=False,
            summary=f"Apple Vision OCR unavailable on {plat}",
            hint="PGS subtitle OCR requires macOS with pyobjc-framework-Vision and Pillow.",
            details=details,
        )

    if probe_err:
        return CheckResult(
            name="subocr",
            category="subocr",
            status="warn",
            required=False,
            summary=f"Failed to probe OCR environment ({interpreter_label}): {probe_err}",
            hint=(
                "Recreate ~/.local/share/subocr/venv and install OCR extras: "
                "pip install 'burndvd[ocr]'."
            ),
            details=details,
        )

    if missing:
        return CheckResult(
            name="subocr",
            category="subocr",
            status="warn",
            required=False,
            summary=f"Missing OCR modules in {interpreter_label}: {', '.join(missing)}",
            hint=(
                "Install OCR extras in ~/.local/share/subocr/venv: "
                "pip install pillow pyobjc-framework-Vision"
            ),
            details=details,
        )

    if not subocr_bin:
        return CheckResult(
            name="subocr",
            category="subocr",
            status="warn",
            required=False,
            summary=(
                f"Vision/Quartz/PIL importable ({interpreter_label}), "
                "but `subocr` binary not found"
            ),
            hint="Install burndvd or link bin/subocr into ~/.local/bin/subocr.",
            details=details,
        )

    return CheckResult(
        name="subocr",
        category="subocr",
        status="ok",
        required=False,
        summary=(
            f"Ready (Vision, Quartz, PIL via {interpreter_label}; subocr: {subocr_bin})"
        ),
        details=details,
    )


def run_doctor(
    *,
    min_staging_gb: float = DEFAULT_MIN_STAGING_GB,
    min_nas_gb: float = DEFAULT_MIN_NAS_GB,
    platform: str | None = None,
    runner: CommandRunner | None = None,
    smb_root: str | None = None,
    nfs_root: str | None = None,
    staging_root: str | None = None,
) -> dict[str, Any]:
    """Run all read-only diagnostic checks and return a structured report dict."""
    plat = platform or sys.platform
    run_cmd = runner or _default_run_cmd

    binary_checks, resolved_bins = check_binaries(platform=plat, runner=run_cmd)
    checks: list[CheckResult] = [
        *binary_checks,
        check_optical_drive(resolved_bins.get("drutil"), platform=plat, runner=run_cmd),
        *check_nas_mounts(min_nas_gb, smb_root=smb_root, nfs_root=nfs_root),
        check_staging_space(min_staging_gb, staging_root=staging_root),
        check_subocr_readiness(platform=plat, runner=run_cmd),
    ]

    ok_count = sum(1 for c in checks if c.status == "ok")
    warn_count = sum(1 for c in checks if c.status == "warn")
    fail_count = sum(1 for c in checks if c.status == "fail")
    required_fail_count = sum(1 for c in checks if c.required and c.status == "fail")
    exit_code = 1 if required_fail_count > 0 else 0

    return {
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "platform": plat,
        "thresholds": {
            "min_staging_gb": min_staging_gb,
            "min_nas_gb": min_nas_gb,
        },
        "summary": {
            "ok": ok_count,
            "warn": warn_count,
            "fail": fail_count,
            "required_fail": required_fail_count,
        },
        "checks": [asdict(c) for c in checks],
    }


def format_report(report: dict[str, Any], *, color: bool = False) -> str:
    """Format a doctor report dict into aligned human-readable lines."""
    checks = report["checks"]
    name_width = max((len(c["name"]) for c in checks), default=12)

    badges = {
        "ok": f"{C.GRN}[OK]{C.R}  " if color else "[OK]  ",
        "warn": f"{C.YLW}[WARN]{C.R}" if color else "[WARN]",
        "fail": f"{C.RED}[FAIL]{C.R}" if color else "[FAIL]",
    }

    lines = ["burndvd doctor — host readiness check", ""]
    for check in checks:
        badge = badges.get(check["status"], f"[{check['status'].upper()}]")
        lines.append(f"{badge} {check['name']:<{name_width}}  {check['summary']}")
        if check.get("hint"):
            lines.append(f"       hint: {check['hint']}")

    summary = report["summary"]
    lines.append("")
    lines.append(
        f"Summary: {summary['ok']} ok, {summary['warn']} warn, "
        f"{summary['fail']} fail (exit {report['exit_code']})"
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for `burndvd doctor`."""
    parser = argparse.ArgumentParser(
        prog="burndvd doctor",
        description="Run read-only host readiness checks for burndvd.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit machine-readable JSON output.",
    )
    parser.add_argument(
        "--min-staging-gb",
        type=float,
        default=DEFAULT_MIN_STAGING_GB,
        help=f"Minimum required local staging free space in GiB (default: {DEFAULT_MIN_STAGING_GB}).",
    )
    parser.add_argument(
        "--min-nas-gb",
        type=float,
        default=DEFAULT_MIN_NAS_GB,
        help=f"Minimum recommended NAS mount free space in GiB (default: {DEFAULT_MIN_NAS_GB}).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for `burndvd doctor`."""
    args = build_parser().parse_args(argv)
    report = run_doctor(
        min_staging_gb=args.min_staging_gb,
        min_nas_gb=args.min_nas_gb,
    )
    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        print(format_report(report, color=use_color))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
