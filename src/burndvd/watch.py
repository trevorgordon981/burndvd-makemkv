"""Zero-babysit multi-drive TV box-set watch mode (`burndvd --watch`).

Polls optical drives via MakeMKV `DRV:` enumeration, identifies each physical
drive by hardware serial (falling back to `/dev/diskN`), serializes disc
launches through `compute_next_tv_episode` so concurrent drives never collide
on TV episode numbers, guards against infinite re-rips of un-ejected discs,
and monitors child `ripqueue` processes until completion or shutdown.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import inspect
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, TextIO

from burndvd import config as burndvd_config
from burndvd import metadata as burndvd_metadata
from burndvd.config import (
    NFS_MEDIA_PREFIX,
    SMB_MEDIA_PREFIX,
    fmt_dur,
)
from burndvd.drive import (
    DRIVE_DEVICE_ENV,
    DRIVE_SERIAL_ENV,
    DRUTIL_DRIVE_ENV,
    parse_makemkv_drives,
)
from burndvd.metadata import (
    ACTIVE_TV_SLOTS_ENV,
    _compact,
    _norm_media_root,
    compute_next_tv_episode,
    episode_contract,
    find_episode_plan_conflict,
    scan_season_existing_episodes,
    volume_matches_show_intent,
    write_queue,
)

DEFAULT_WATCH_MAKEMKVCON = "/opt/homebrew/bin/makemkvcon"
DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_SCAN_WAIT_S = 120.0
USER_CONFIG_DEFAULT_PATH = Path.home() / ".config" / "burndvd" / "config"

_WATCHER_REGISTRY_LOCK = threading.Lock()
_SEASON_THREAD_LOCKS: dict[str, threading.Lock] = {}
_LIVE_WATCHER_RIPS: dict[str, list[ActiveRip]] = {}


def _season_coord_key(
    show: str,
    season: int,
    target_root: str,
    media_smb_root: str = SMB_MEDIA_PREFIX,
    media_nfs_root: str = NFS_MEDIA_PREFIX,
) -> str:
    norm_root = _norm_media_root(target_root, media_smb_root, media_nfs_root)
    return f"{_compact(show)}|S{int(season)}|{norm_root}"


def _get_season_thread_lock(season_key: str) -> threading.Lock:
    with _WATCHER_REGISTRY_LOCK:
        lock = _SEASON_THREAD_LOCKS.get(season_key)
        if lock is None:
            lock = threading.Lock()
            _SEASON_THREAD_LOCKS[season_key] = lock
        return lock


@contextlib.contextmanager
def _watch_season_lock(work_dir: Path, season_key: str) -> Iterator[None]:
    """Serialize Watch episode planning + launch across threads and independent processes."""
    tlock = _get_season_thread_lock(season_key)
    locks_dir = Path(work_dir) / ".locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(season_key.encode("utf-8")).hexdigest()[:16]
    lock_file = locks_dir / f"watch-season-{digest}.lock"
    with tlock:
        fh = lock_file.open("a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                fh.close()
            except OSError:
                pass


def _spawn_accepts_start_new_session(spawn_fn: Callable[..., Any]) -> bool:
    """Return True if `spawn_fn` accepts `start_new_session=True` (always True for Popen)."""
    if spawn_fn is subprocess.Popen:
        return True
    try:
        sig = inspect.signature(spawn_fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == "start_new_session":
            return True
    return False


def _parse_bool(value: Any) -> bool | None:
    """Parse common truthy/falsy representations into bool, or None if unset."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        cleaned = value.strip().strip('"').strip("'").lower()
        if cleaned in ("1", "true", "yes", "on"):
            return True
        if cleaned in ("0", "false", "no", "off"):
            return False
    return None


def _read_user_config_fallback(path: Path) -> dict[str, Any]:
    """Read `~/.config/burndvd/config` supporting JSON or simple key=value/INI."""
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    stripped = raw.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return data
        except ValueError:
            return {}
    parsed: dict[str, Any] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            key = k.strip().lower().replace("-", "_")
            val = v.split("#", 1)[0].strip().strip('"').strip("'")
            if key:
                parsed[key] = val
    return parsed


def load_user_config_safe(config_path: Path | None = None) -> dict[str, Any]:
    """Load user config via `burndvd.config.load_user_config` if available, else fallback."""
    loader = getattr(burndvd_config, "load_user_config", None)
    if callable(loader):
        try:
            data = loader() if config_path is None else loader(config_path)
        except TypeError:
            if config_path is not None:
                return _read_user_config_fallback(config_path)
            data = None
        except Exception:
            data = None
        if isinstance(data, Mapping):
            return dict(data)
        if data is not None:
            return {
                k: getattr(data, k)
                for k in dir(data)
                if not k.startswith("_") and not callable(getattr(data, k))
            }
    return _read_user_config_fallback(config_path or USER_CONFIG_DEFAULT_PATH)


def resolve_no_extras(
    cli_no_extras: bool | None,
    *,
    config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Resolve effective `no_extras` flag: CLI > BURNDVD_NO_EXTRAS > user config > False."""
    if cli_no_extras is not None:
        return bool(cli_no_extras)
    env_map = os.environ if env is None else env
    raw_env = env_map.get("BURNDVD_NO_EXTRAS", "")
    parsed_env = _parse_bool(raw_env)
    if parsed_env is not None:
        return parsed_env
    cfg = load_user_config_safe(config_path)
    for key in ("no_extras", "no-extras"):
        if key in cfg:
            parsed_cfg = _parse_bool(cfg[key])
            if parsed_cfg is not None:
                return parsed_cfg
    return False


def resolve_default_target_root(
    show: str,
    media_format: str = "BD",
    *,
    nfs_root: str | Path = NFS_MEDIA_PREFIX,
    smb_root: str | Path = SMB_MEDIA_PREFIX,
) -> str:
    """Compute default TV target root, preferring writable NFS over SMB."""
    nfs_path = Path(nfs_root)
    smb_path = Path(smb_root)
    subdir = "TV Shows 4K" if str(media_format).strip().upper() == "4K" else "TV Shows"
    if nfs_path.is_dir() and os.access(nfs_path, os.W_OK):
        nas_root = nfs_path
    else:
        nas_root = smb_path
    return str(nas_root / subdir / show)


def drive_identity_key(drive_row: Mapping[str, Any]) -> str:
    """Return stable physical identity for a drive: hardware serial, else device node."""
    serial = str(drive_row.get("serial") or "").strip()
    if serial:
        return serial
    node = str(drive_row.get("device_node") or "").strip()
    if node:
        return node
    raw_dev = str(drive_row.get("raw_device") or "").strip()
    if raw_dev:
        return raw_dev
    return f"disc:{drive_row.get('index', 0)}"


def slugify_volume(value: str) -> str:
    """Return a filesystem-safe lowercase slug for log/queue filenames."""
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or "disc"


def list_makemkv_drives(
    makemkvcon: str = DEFAULT_WATCH_MAKEMKVCON,
    *,
    timeout_s: float = 20.0,
) -> list[dict[str, Any]]:
    """Query MakeMKV `DRV:` rows via `makemkvcon -r info list` (read-only list)."""
    try:
        proc = subprocess.run(
            [makemkvcon, "-r", "info", "list"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        return []
    return parse_makemkv_drives(proc.stdout or "")


def resolve_ripqueue_base_cmd(
    ripqueue_py: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Resolve the command prefix to invoke `ripqueue`."""
    env_map = os.environ if env is None else env
    candidate = ripqueue_py or env_map.get("BURNDVD_RIPQUEUE_PY", "").strip()
    if not candidate:
        default_script = Path.home() / "scripts" / "ripqueue.py"
        if default_script.is_file():
            candidate = str(default_script)
    if candidate:
        if candidate.endswith(".py"):
            return [sys.executable, candidate]
        return [candidate]
    return [sys.executable, "-m", "burndvd.cli.ripqueue"]


def build_ripqueue_command(
    *,
    queue_path: Path,
    state_path: Path,
    disc_index: int,
    makemkvcon: str = DEFAULT_WATCH_MAKEMKVCON,
    no_extras: bool = False,
    ripqueue_py: str | None = None,
    env: Mapping[str, str] | None = None,
    expected_volume: str | None = None,
    strict_disc_match: bool = False,
    allowed_volumes: Iterable[str] = (),
    allowed_fingerprints: Iterable[str] = (),
) -> list[str]:
    """Build the non-interactive `ripqueue` subprocess command for one disc."""
    cmd = resolve_ripqueue_base_cmd(ripqueue_py, env=env)
    cmd.extend(
        [
            "--queue",
            str(queue_path),
            "--state",
            str(state_path),
            "--makemkvcon",
            str(makemkvcon),
            "--device",
            f"disc:{int(disc_index)}",
            "--non-interactive",
            "--on-fail=retry",
            "--verify",
        ]
    )
    if no_extras:
        cmd.append("--no-extras")
    if expected_volume:
        cmd.extend(["--expected-volume", str(expected_volume)])
    if strict_disc_match:
        cmd.append("--strict-disc-match")
    for pattern in allowed_volumes or ():
        cleaned = str(pattern).strip()
        if cleaned:
            cmd.extend(["--allow-volume", cleaned])
    for fp in allowed_fingerprints or ():
        cleaned = str(fp).strip()
        if cleaned:
            cmd.extend(["--allow-fingerprint", cleaned])
    return cmd


def build_ripqueue_env(
    *,
    device_node: str,
    drive_serial: str,
    drutil_drive: int,
    no_extras: bool,
    slots_dir: Path | str | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct child subprocess environment pinned to one physical drive."""
    env = dict(os.environ if base_env is None else base_env)
    if device_node:
        env[DRIVE_DEVICE_ENV] = device_node
    else:
        env.pop(DRIVE_DEVICE_ENV, None)
    if drive_serial:
        env[DRIVE_SERIAL_ENV] = drive_serial
    else:
        env.pop(DRIVE_SERIAL_ENV, None)
    env[DRUTIL_DRIVE_ENV] = str(int(drutil_drive))
    env["BURNDVD_NO_EXTRAS"] = "1" if no_extras else "0"
    if slots_dir is not None:
        env[ACTIVE_TV_SLOTS_ENV] = str(slots_dir)
    return env


def notify_slack(
    text: str,
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 10.0,
) -> bool:
    """Best-effort Slack notification via webhook or Bot API. Never raises."""
    env_map = os.environ if env is None else env
    webhook = env_map.get("BURNDVD_SLACK_WEBHOOK", "").strip()
    token = env_map.get("BURNDVD_SLACK_TOKEN", "").strip()
    channel = env_map.get("BURNDVD_SLACK_CHANNEL", "").strip()

    try:
        if webhook:
            payload = json.dumps({"text": text}).encode("utf-8")
            req = urllib.request.Request(
                webhook,
                data=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_s):
                return True
        if token and channel:
            payload = json.dumps(
                {"channel": channel, "text": text, "unfurl_links": False}
            ).encode("utf-8")
            req = urllib.request.Request(
                "https://slack.com/api/chat.postMessage",
                data=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_s):
                return True
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return False


@dataclass
class WatchConfig:
    """Runtime configuration for multi-drive box-set watch mode."""

    show: str
    season: int
    media_format: str = "BD"
    target_root: str = ""
    no_extras: bool = False
    poll_interval: float = DEFAULT_POLL_INTERVAL_S
    scan_wait: float = DEFAULT_SCAN_WAIT_S
    episode_start: int | None = None
    max_discs: int = 0
    makemkvcon: str = DEFAULT_WATCH_MAKEMKVCON
    ripqueue_py: str | None = None
    log_dir: Path | None = None
    work_dir: Path | None = None
    slots_dir: Path | None = None
    media_smb_root: str = SMB_MEDIA_PREFIX
    media_nfs_root: str = NFS_MEDIA_PREFIX
    allowed_volumes: tuple[str, ...] = ()
    allowed_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.media_format = str(self.media_format or "BD").strip().upper()
        self.allowed_volumes = tuple(
            str(v).strip() for v in (self.allowed_volumes or ()) if str(v).strip()
        )
        self.allowed_fingerprints = tuple(
            str(fp).strip().lower()
            for fp in (self.allowed_fingerprints or ())
            if str(fp).strip()
        )
        if not self.target_root:
            self.target_root = resolve_default_target_root(
                self.show,
                self.media_format,
                nfs_root=self.media_nfs_root,
                smb_root=self.media_smb_root,
            )
        if self.log_dir is None:
            self.log_dir = Path(
                os.environ.get("BURNDVD_LOG_DIR", str(Path.home() / "logs"))
            )
        else:
            self.log_dir = Path(self.log_dir)
        if self.work_dir is None:
            self.work_dir = Path.home() / ".cache" / "burndvd" / "watch"
        else:
            self.work_dir = Path(self.work_dir)
        if self.slots_dir is not None:
            self.slots_dir = Path(self.slots_dir)

    @property
    def season_dir(self) -> Path:
        return Path(self.target_root) / f"Season {int(self.season):02d}"


@dataclass
class ActiveRip:
    """Tracks one live `ripqueue` subprocess pinned to a physical drive."""

    drive_key: str
    drive_serial: str
    device_node: str
    disc_index: int
    drutil_drive: int
    volume: str
    episode_start: int
    proc: Any
    log_path: Path
    queue_path: Path
    state_path: Path
    started_at: float
    pending_launch_path: Path | None = None


@dataclass
class CompletedRip:
    """Summary of one reaped `ripqueue` child process."""

    drive_key: str
    device_node: str
    volume: str
    episode_start: int
    returncode: int
    log_path: Path
    elapsed_s: float


@dataclass
class WatchSummary:
    """Aggregate result returned when `BoxSetWatcher.run()` exits."""

    launched: int = 0
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    stopped_by_signal: bool = False
    active_at_exit: list[ActiveRip] = field(default_factory=list)
    results: list[CompletedRip] = field(default_factory=list)


class BoxSetWatcher:
    """Coordinates zero-babysit multi-drive TV box-set ripping."""

    def __init__(
        self,
        config: WatchConfig,
        *,
        list_drives_fn: Callable[[], list[dict[str, Any]]] | None = None,
        spawn_fn: Callable[..., Any] | None = None,
        compute_next_ep_fn: Callable[..., dict[str, Any]] | None = None,
        discover_procs_fn: Callable[[], list[dict[str, Any]]] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], datetime] = datetime.now,
        notify_fn: Callable[[str], Any] | None = None,
        out: TextIO | None = None,
    ) -> None:
        self.config = config
        self._list_drives_fn = list_drives_fn or (
            lambda: list_makemkv_drives(self.config.makemkvcon)
        )
        self._spawn_fn = spawn_fn or subprocess.Popen
        self._compute_next_ep_fn = compute_next_ep_fn or compute_next_tv_episode
        self._discover_procs_fn = (
            discover_procs_fn
            if discover_procs_fn is not None
            else burndvd_metadata._discover_active_ripqueue_procs
        )
        self._sleep_fn = sleep_fn
        self._clock_fn = clock_fn
        self._now_fn = now_fn
        self._notify_fn = notify_fn if notify_fn is not None else notify_slack
        self._out = out if out is not None else sys.stdout

        self.active_procs: dict[str, ActiveRip] = {}
        self.handled_discs: dict[str, str] = {}
        self.rejected_discs: dict[str, str] = {}
        self.results: list[CompletedRip] = []
        self._launched_count: int = 0
        self._first_episode_override: int | None = config.episode_start
        self._stop_requested: bool = False
        self._launch_counter: int = 0
        self.watcher_id: str = uuid.uuid4().hex[:8]

    def _season_key(self) -> str:
        return _season_coord_key(
            self.config.show,
            self.config.season,
            self.config.target_root,
            self.config.media_smb_root,
            self.config.media_nfs_root,
        )

    def _pending_dir(self) -> Path:
        return Path(self.config.work_dir) / ".pending"

    def _register_live_rip(self, rip: ActiveRip) -> None:
        skey = self._season_key()
        with _WATCHER_REGISTRY_LOCK:
            bucket = [
                r
                for r in _LIVE_WATCHER_RIPS.get(skey, [])
                if r.proc.poll() is None
            ]
            bucket.append(rip)
            _LIVE_WATCHER_RIPS[skey] = bucket

    def _unregister_live_rip(self, rip: ActiveRip) -> None:
        skey = self._season_key()
        with _WATCHER_REGISTRY_LOCK:
            bucket = [
                r
                for r in _LIVE_WATCHER_RIPS.get(skey, [])
                if r is not rip and r.proc.poll() is None
            ]
            if bucket:
                _LIVE_WATCHER_RIPS[skey] = bucket
            else:
                _LIVE_WATCHER_RIPS.pop(skey, None)
        if rip.pending_launch_path is not None:
            try:
                rip.pending_launch_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_pending_launch_record(
        self,
        *,
        pid: int,
        drive_key: str,
        queue_path: Path,
        state_path: Path,
        log_path: Path,
        attempt_token: str,
    ) -> Path | None:
        pdir = self._pending_dir()
        try:
            pdir.mkdir(parents=True, exist_ok=True)
            dest = pdir / f"launch-{os.getpid()}-{int(pid)}-{attempt_token}.json"
            tmp = pdir / f".launch-{os.getpid()}-{int(pid)}-{attempt_token}.tmp"
            payload = {
                "pid": int(pid),
                "owner_pid": os.getpid(),
                "watcher_id": self.watcher_id,
                "season_key": self._season_key(),
                "drive_key": drive_key,
                "state_file": str(state_path),
                "queue_file": str(queue_path),
                "log_file": str(log_path),
                "created_at": time.time(),
            }
            tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, dest)
            return dest
        except OSError:
            return None

    def _emit(self, message: str) -> None:
        ts = self._now_fn().strftime("%H:%M:%S")
        print(f"[{ts}] {message}", file=self._out, flush=True)

    def _safe_notify(self, message: str) -> None:
        try:
            self._notify_fn(message)
        except Exception:
            pass

    def request_stop(self) -> None:
        """Signal the watcher loop to stop accepting new discs cleanly."""
        self._stop_requested = True

    def reap_finished(self) -> list[CompletedRip]:
        """Reap any finished child processes and emit completion status lines."""
        reaped: list[CompletedRip] = []
        finished_keys: list[tuple[str, int]] = []
        for drive_key, rip in list(self.active_procs.items()):
            rc = rip.proc.poll()
            if rc is not None:
                finished_keys.append((drive_key, int(rc)))

        for drive_key, rc in finished_keys:
            rip = self.active_procs.pop(drive_key)
            self._unregister_live_rip(rip)
            elapsed = max(0.0, self._clock_fn() - rip.started_at)
            # Keep handled_discs pinned to this volume so an un-ejected disc is not re-ripped.
            self.handled_discs[drive_key] = rip.volume
            completed = CompletedRip(
                drive_key=drive_key,
                device_node=rip.device_node,
                volume=rip.volume,
                episode_start=rip.episode_start,
                returncode=rc,
                log_path=rip.log_path,
                elapsed_s=elapsed,
            )
            self.results.append(completed)
            reaped.append(completed)
            if rc == 0:
                self._emit(
                    f"Drive {drive_key} FINISHED {rip.volume} "
                    f"(exit 0, {fmt_dur(elapsed)})"
                )
                self._safe_notify(
                    f":white_check_mark: *Watch rip finished* — `{self.config.show}` "
                    f"S{self.config.season:02d} `{rip.volume}` (E{rip.episode_start}+, "
                    f"drive `{drive_key}`, {fmt_dur(elapsed)})."
                )
            else:
                self._emit(
                    f"Drive {drive_key} FAILED {rip.volume} "
                    f"(exit {rc}, {fmt_dur(elapsed)}, log: {rip.log_path}) — "
                    f"will not re-rip until disc is removed"
                )
                self._safe_notify(
                    f":x: *Watch rip FAILED* — `{self.config.show}` "
                    f"S{self.config.season:02d} `{rip.volume}` (exit {rc}, "
                    f"drive `{drive_key}`, log: `{rip.log_path.name}`)."
                )
        return reaped

    def _update_handled_guard(self, drives: list[dict[str, Any]]) -> None:
        """Clear `handled_discs` and `rejected_discs` once a drive reports empty/open tray or a new volume."""
        for d in drives:
            drive_key = drive_identity_key(d)
            if drive_key in self.active_procs:
                continue
            state = d.get("state")
            node = str(d.get("device_node") or "").strip()
            if state != 2 or not node:
                self.handled_discs.pop(drive_key, None)
                self.rejected_discs.pop(drive_key, None)
                continue
            raw_vol = str(d.get("volume") or "").strip()
            if (
                raw_vol
                and drive_key in self.handled_discs
                and self.handled_discs[drive_key] != raw_vol
            ):
                self.handled_discs.pop(drive_key, None)
            if (
                drive_key in self.rejected_discs
                and self.rejected_discs[drive_key] != raw_vol
            ):
                self.rejected_discs.pop(drive_key, None)

    def _eligible_drives(self, drives: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return idle drives holding a new, unhandled disc sorted deterministically."""
        candidates: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for d in sorted(
            drives, key=lambda row: (int(row.get("index", 0)), drive_identity_key(row))
        ):
            if d.get("state") != 2:
                continue
            node = str(d.get("device_node") or "").strip()
            if not node:
                continue
            drive_key = drive_identity_key(d)
            if drive_key in seen_keys or drive_key in self.active_procs:
                continue
            raw_vol = str(d.get("volume") or "").strip()
            if raw_vol and self.handled_discs.get(drive_key) == raw_vol:
                continue
            if drive_key in self.rejected_discs and self.rejected_discs[drive_key] == raw_vol:
                continue
            seen_keys.add(drive_key)
            candidates.append(d)
        return candidates

    def _known_active_processes(self) -> list[dict[str, Any]]:
        """Build active-process list combining our live children, sibling watchers, and external ripqueue procs."""
        procs: list[dict[str, Any]] = []
        seen_pids: set[int] = set()
        for rip in self.active_procs.values():
            pid = getattr(rip.proc, "pid", None)
            if isinstance(pid, int) and pid > 0 and rip.proc.poll() is None:
                seen_pids.add(pid)
                procs.append(
                    {
                        "pid": pid,
                        "state_file": str(rip.state_path),
                        "queue_file": str(rip.queue_path),
                        "log_file": str(rip.log_path),
                    }
                )
        skey = self._season_key()
        with _WATCHER_REGISTRY_LOCK:
            sibling_rips = [
                r
                for r in _LIVE_WATCHER_RIPS.get(skey, [])
                if r.proc.poll() is None
            ]
            if sibling_rips:
                _LIVE_WATCHER_RIPS[skey] = sibling_rips
            else:
                _LIVE_WATCHER_RIPS.pop(skey, None)
        for rip in sibling_rips:
            pid = getattr(rip.proc, "pid", None)
            if isinstance(pid, int) and pid > 0 and pid not in seen_pids:
                seen_pids.add(pid)
                procs.append(
                    {
                        "pid": pid,
                        "state_file": str(rip.state_path),
                        "queue_file": str(rip.queue_path),
                        "log_file": str(rip.log_path),
                    }
                )
        pdir = self._pending_dir()
        if pdir.is_dir():
            for path in sorted(pdir.glob("launch-*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict) or data.get("season_key") != skey:
                    continue
                try:
                    pid = int(data.get("pid", 0))
                    owner_pid = int(data.get("owner_pid", 0))
                except (TypeError, ValueError):
                    continue
                if pid <= 0 or pid in seen_pids:
                    continue
                if owner_pid != os.getpid() and not burndvd_metadata._pid_alive(pid):
                    if not burndvd_metadata._pid_alive(owner_pid):
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    continue
                if owner_pid == os.getpid():
                    # Same-process watcher launches are already tracked accurately
                    # via `_LIVE_WATCHER_RIPS` (including fake PIDs in unit tests).
                    continue
                seen_pids.add(pid)
                procs.append(
                    {
                        "pid": pid,
                        "state_file": str(data.get("state_file") or ""),
                        "queue_file": str(data.get("queue_file") or ""),
                        "log_file": str(data.get("log_file") or ""),
                    }
                )
        try:
            external = self._discover_procs_fn() or []
        except Exception:
            external = []
        for entry in external:
            try:
                pid = int(entry.get("pid", 0))
            except (TypeError, ValueError):
                continue
            if pid > 0 and pid not in seen_pids:
                seen_pids.add(pid)
                procs.append(dict(entry))
        return procs

    def _create_unique_attempt_paths(
        self, *, slug: str, drive_slug: str, ts: str
    ) -> tuple[Path, Path, Path, Path, int, str]:
        """Allocate collision-proof attempt directory and exclusive log file descriptor."""
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        pid = os.getpid()
        for _ in range(32):
            attempt_token = uuid.uuid4().hex[:8]
            unique_tag = (
                f"{slug}-{drive_slug}-{ts}-{pid}-"
                f"{self.watcher_id}-{self._launch_counter}-{attempt_token}"
            )
            attempt_dir = self.config.work_dir / f"attempt-{unique_tag}"
            try:
                attempt_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            log_path = self.config.log_dir / f"burndvd-watch-{unique_tag}.log"
            try:
                log_fd = os.open(
                    str(log_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                )
            except FileExistsError:
                try:
                    attempt_dir.rmdir()
                except OSError:
                    pass
                continue
            queue_path = attempt_dir / "queue.csv"
            state_path = attempt_dir / "state.json"
            return attempt_dir, queue_path, state_path, log_path, log_fd, attempt_token
        raise RuntimeError("failed to allocate unique watch attempt directory/log")

    def _launch_single_drive(self, drive_row: Mapping[str, Any]) -> ActiveRip | None:
        """Serialized, intent-bound launch gate for a single drive (zero slot or artifact race)."""
        # Reap any child that may have exited just before we compute the next episode.
        self.reap_finished()

        drive_key = drive_identity_key(drive_row)
        drive_serial = str(drive_row.get("serial") or "").strip()
        device_node = str(drive_row.get("device_node") or "").strip()
        disc_index = int(drive_row.get("index", 0))
        drutil_drive = disc_index + 1
        raw_vol = str(drive_row.get("volume") or "").strip()

        matched_intent, intent_reason = volume_matches_show_intent(
            raw_vol,
            self.config.show,
            self.config.season,
            allowed_volumes=self.config.allowed_volumes,
        )
        if not matched_intent:
            self.rejected_discs[drive_key] = raw_vol
            vol_disp = raw_vol or "(unlabeled)"
            self._emit(
                f"Drive {drive_key} ({device_node}) REJECTED {vol_disp} — "
                f"{intent_reason}; refusing unattended launch"
            )
            return None

        volume = raw_vol
        skey = self._season_key()
        with _watch_season_lock(self.config.work_dir, skey):
            # Guard against two independent watchers launching the same physical drive.
            with _WATCHER_REGISTRY_LOCK:
                already_active_drive = any(
                    r.drive_key == drive_key and r.proc.poll() is None
                    for r in _LIVE_WATCHER_RIPS.get(skey, [])
                )
            if already_active_drive:
                return None

            contract = episode_contract(self.config.show, self.config.season, volume)
            expected_episodes = ""
            expected_disc_episodes = ""
            expected_title_ids = ""
            expected_physical_disc = ""

            if self._first_episode_override is not None or contract is not None:
                if self._first_episode_override is not None:
                    ep_start = int(self._first_episode_override)
                    plan_count: int | None = None
                else:
                    assert contract is not None
                    ep_start = int(contract["episode_start"])
                    plan_count = int(contract["expected_disc_episodes"])
                    expected_episodes = str(contract["expected_episodes"])
                    expected_disc_episodes = str(contract["expected_disc_episodes"])
                    expected_title_ids = ",".join(
                        str(x) for x in contract["expected_title_ids"]
                    )
                    expected_physical_disc = str(contract["disc"])

                known_procs = self._known_active_processes()
                ep_info = self._compute_next_ep_fn(
                    title=self.config.show,
                    season=int(self.config.season),
                    target_root=self.config.target_root,
                    season_dir=self.config.season_dir,
                    slots_dir=self.config.slots_dir,
                    media_smb_root=self.config.media_smb_root,
                    media_nfs_root=self.config.media_nfs_root,
                    active_processes=known_procs,
                    wait_scan_s=0.0,
                    poll_interval_s=min(
                        2.0, max(0.05, float(self.config.poll_interval))
                    ),
                )
                existing_eps = ep_info.get("existing_episodes")
                if existing_eps is None:
                    existing_eps = scan_season_existing_episodes(
                        self.config.season_dir, int(self.config.season)
                    )
                conflict, blocking_pending = find_episode_plan_conflict(
                    ep_start,
                    episode_count=plan_count,
                    existing_episodes=existing_eps,
                    reservations=ep_info.get("reservations") or (),
                    pending_plans=ep_info.get("pending_plans") or (),
                    check_existing=True,
                )
                if conflict is None and blocking_pending and float(self.config.scan_wait) > 0:
                    ep_info = self._compute_next_ep_fn(
                        title=self.config.show,
                        season=int(self.config.season),
                        target_root=self.config.target_root,
                        season_dir=self.config.season_dir,
                        slots_dir=self.config.slots_dir,
                        media_smb_root=self.config.media_smb_root,
                        media_nfs_root=self.config.media_nfs_root,
                        active_processes=self._known_active_processes(),
                        wait_scan_s=float(self.config.scan_wait),
                        poll_interval_s=min(
                            2.0, max(0.05, float(self.config.poll_interval))
                        ),
                    )
                    existing_eps = ep_info.get("existing_episodes")
                    if existing_eps is None:
                        existing_eps = scan_season_existing_episodes(
                            self.config.season_dir, int(self.config.season)
                        )
                    conflict, blocking_pending = find_episode_plan_conflict(
                        ep_start,
                        episode_count=plan_count,
                        existing_episodes=existing_eps,
                        reservations=ep_info.get("reservations") or (),
                        pending_plans=ep_info.get("pending_plans") or (),
                        check_existing=True,
                    )
                if conflict is not None:
                    self.rejected_discs[drive_key] = volume
                    self._emit(
                        f"Drive {drive_key} ({device_node}) REJECTED {volume} — "
                        f"{conflict}; refusing overlapping launch"
                    )
                    return None
                if blocking_pending:
                    pids_s = ", ".join(str(p) for p in blocking_pending)
                    self._emit(
                        f"Drive {drive_key} ({device_node}) WAITING — concurrent scan "
                        f"pid(s) {pids_s} could overlap E{ep_start}+ and did not publish slot "
                        f"within {int(self.config.scan_wait)}s; deferring {volume}"
                    )
                    return None
                pending_scan_pids: list[int] = []
            else:
                known_procs = self._known_active_processes()
                ep_info = self._compute_next_ep_fn(
                    title=self.config.show,
                    season=int(self.config.season),
                    target_root=self.config.target_root,
                    season_dir=self.config.season_dir,
                    slots_dir=self.config.slots_dir,
                    media_smb_root=self.config.media_smb_root,
                    media_nfs_root=self.config.media_nfs_root,
                    active_processes=known_procs,
                    wait_scan_s=float(self.config.scan_wait),
                    poll_interval_s=min(2.0, max(0.05, float(self.config.poll_interval))),
                )
                pending_scan_pids = list(ep_info.get("pending_scan_pids") or [])
                if pending_scan_pids:
                    pids_s = ", ".join(str(p) for p in pending_scan_pids)
                    self._emit(
                        f"Drive {drive_key} ({device_node}) WAITING — concurrent scan "
                        f"pid(s) {pids_s} did not publish slot within {int(self.config.scan_wait)}s; "
                        f"deferring {volume}"
                    )
                    return None
                ep_start = int(ep_info.get("next_episode") or 1)

            self._launch_counter += 1
            ts = self._now_fn().strftime("%Y%m%dT%H%M%S")
            slug = slugify_volume(volume)
            drive_slug = slugify_volume(drive_key)
            (
                _attempt_dir,
                queue_path,
                state_path,
                log_path,
                log_fd,
                attempt_token,
            ) = self._create_unique_attempt_paths(
                slug=slug, drive_slug=drive_slug, ts=ts
            )

            write_queue(
                queue_path,
                title=self.config.show,
                item_type="tv-season",
                season=str(self.config.season),
                episode_start=str(ep_start),
                expected_episodes=expected_episodes,
                expected_disc_episodes=expected_disc_episodes,
                expected_title_ids=expected_title_ids,
                expected_physical_disc=expected_physical_disc,
                target_root=self.config.target_root,
                media_format=self.config.media_format,
                notes=f"watch:{drive_key}:{volume}",
            )

            cmd = build_ripqueue_command(
                queue_path=queue_path,
                state_path=state_path,
                disc_index=disc_index,
                makemkvcon=self.config.makemkvcon,
                no_extras=self.config.no_extras,
                ripqueue_py=self.config.ripqueue_py,
                expected_volume=volume,
                strict_disc_match=True,
                allowed_volumes=self.config.allowed_volumes,
                allowed_fingerprints=self.config.allowed_fingerprints,
            )
            child_env = build_ripqueue_env(
                device_node=device_node,
                drive_serial=drive_serial,
                drutil_drive=drutil_drive,
                no_extras=self.config.no_extras,
                slots_dir=self.config.slots_dir,
            )

            log_fh = os.fdopen(log_fd, "w", encoding="utf-8")
            spawn_kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": log_fh,
                "stderr": subprocess.STDOUT,
                "env": child_env,
            }
            if _spawn_accepts_start_new_session(self._spawn_fn):
                spawn_kwargs["start_new_session"] = True
            try:
                proc = self._spawn_fn(cmd, **spawn_kwargs)
            except Exception:
                log_fh.close()
                raise
            # Child inherited the fd; close parent copy so watcher shutdown never leaks FDs.
            log_fh.close()

            # Consume one-shot --episode-start override only after spawn succeeded.
            if self._first_episode_override is not None:
                self._first_episode_override = None

            pid_val = getattr(proc, "pid", None)
            pending_launch_path = (
                self._write_pending_launch_record(
                    pid=int(pid_val),
                    drive_key=drive_key,
                    queue_path=queue_path,
                    state_path=state_path,
                    log_path=log_path,
                    attempt_token=attempt_token,
                )
                if isinstance(pid_val, int) and pid_val > 0
                else None
            )

            rip = ActiveRip(
                drive_key=drive_key,
                drive_serial=drive_serial,
                device_node=device_node,
                disc_index=disc_index,
                drutil_drive=drutil_drive,
                volume=volume,
                episode_start=ep_start,
                proc=proc,
                log_path=log_path,
                queue_path=queue_path,
                state_path=state_path,
                started_at=self._clock_fn(),
                pending_launch_path=pending_launch_path,
            )
            self.active_procs[drive_key] = rip
            self.handled_discs[drive_key] = volume
            self._launched_count += 1
            self._register_live_rip(rip)

        pid_s = getattr(proc, "pid", "?")
        self._emit(
            f"Drive {drive_key} ({device_node}) STARTED {volume} -> "
            f"E{ep_start}+ (pid {pid_s}, log: {log_path})"
        )
        return rip

    def poll_once(self) -> list[ActiveRip]:
        """Run one poll-reap-launch tick; return newly launched rips."""
        self.reap_finished()
        drives = self._list_drives_fn()
        if drives:
            self._update_handled_guard(drives)

        launched_this_tick: list[ActiveRip] = []
        if self._stop_requested:
            return launched_this_tick

        for drive_row in self._eligible_drives(drives):
            if self._stop_requested:
                break
            if (
                self.config.max_discs > 0
                and self._launched_count >= self.config.max_discs
            ):
                break
            rip = self._launch_single_drive(drive_row)
            if rip is not None:
                launched_this_tick.append(rip)
        return launched_this_tick

    def _reached_max_and_idle(self) -> bool:
        return (
            self.config.max_discs > 0
            and self._launched_count >= self.config.max_discs
            and not self.active_procs
        )

    def run(self) -> WatchSummary:
        """Main watch loop until `--max-discs` completes or Ctrl-C / SIGTERM."""
        extras_label = "no-extras" if self.config.no_extras else "with-extras"
        max_label = (
            f", max-discs={self.config.max_discs}"
            if self.config.max_discs > 0
            else ""
        )
        ep_label = (
            f", initial-ep=E{self.config.episode_start}"
            if self.config.episode_start is not None
            else ""
        )
        self._emit(
            f"Watch mode started: {self.config.show} S{self.config.season:02d} "
            f"({self.config.media_format}, {extras_label}{ep_label}{max_label}) -> "
            f"{self.config.target_root}"
        )

        prev_sigterm = None
        installed_sigterm = False
        if threading.current_thread() is threading.main_thread():
            def _handle_sigterm(_signum, _frame):
                self._stop_requested = True

            prev_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _handle_sigterm)
            installed_sigterm = True

        stopped_by_signal = False
        try:
            while not self._stop_requested:
                self.poll_once()
                if self._reached_max_and_idle():
                    break
                if self._stop_requested:
                    break
                self._sleep_fn(float(self.config.poll_interval))
        except KeyboardInterrupt:
            stopped_by_signal = True
            self._stop_requested = True
        finally:
            if installed_sigterm:
                signal.signal(signal.SIGTERM, prev_sigterm)

        if self._stop_requested and not self._reached_max_and_idle():
            stopped_by_signal = True
            # Final non-blocking reap before reporting active PIDs.
            self.reap_finished()
            if self.active_procs:
                self._emit(
                    f"Stopping watch mode (no new discs will start). "
                    f"{len(self.active_procs)} active rip(s) left running safely:"
                )
                for rip in self.active_procs.values():
                    pid_s = getattr(rip.proc, "pid", "?")
                    self._emit(
                        f"  pid {pid_s}  Drive {rip.drive_key} ({rip.device_node})  "
                        f"{rip.volume} -> E{rip.episode_start}+  log: {rip.log_path}"
                    )
            else:
                self._emit("Watch mode stopped cleanly (no active rips).")
        else:
            self._emit(
                f"Watch mode complete: {len(self.results)} disc(s) finished "
                f"({sum(1 for r in self.results if r.returncode == 0)} ok, "
                f"{sum(1 for r in self.results if r.returncode != 0)} failed)."
            )

        succeeded = sum(1 for r in self.results if r.returncode == 0)
        failed = sum(1 for r in self.results if r.returncode != 0)
        return WatchSummary(
            launched=self._launched_count,
            completed=len(self.results),
            succeeded=succeeded,
            failed=failed,
            stopped_by_signal=stopped_by_signal,
            active_at_exit=list(self.active_procs.values()),
            results=list(self.results),
        )
