"""NAS path routing (SMB->NFS failover), atomic no-clobber publish, PID claims, locks, and quarantine."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from burndvd.config import (
    C,
    LOCAL_LOCK_BASE,
    NFS_MEDIA_PREFIX,
    SMB_MEDIA_PREFIX,
    fmt_dur,
    render_bar,
    sanitize,
)
from burndvd.drive import append_log
from burndvd.models import QueueItem
from burndvd.probe import ffprobe_dur_s, parse_duration, title_num_from_filename
from burndvd.selection import movie_filename


def _free_gb(path: Path) -> float:
    """Free space in decimal GB using `df -Pk` (avoids macOS SMB statvfs bugs)."""
    try:
        out = subprocess.check_output(["df", "-Pk", str(path)], text=True)
        avail_kb = int(out.strip().splitlines()[-1].split()[3])
        return avail_kb * 1024 / 1e9
    except Exception:
        return shutil.disk_usage(path).free / 1e9


def _nfs_mount_available() -> bool:
    """True when the configured NFS media root is an active mountpoint."""
    nfs_root = Path(NFS_MEDIA_PREFIX)
    try:
        return nfs_root.is_dir() and os.path.ismount(nfs_root)
    except OSError:
        return False


def _to_nfs_path(path: Path) -> Path | None:
    """Translate an SMB media path (`/Volumes/Media/...`) to its NFS mirror view."""
    raw = str(path)
    if raw == SMB_MEDIA_PREFIX:
        return Path(NFS_MEDIA_PREFIX)
    if raw.startswith(SMB_MEDIA_PREFIX + "/"):
        return Path(NFS_MEDIA_PREFIX + raw[len(SMB_MEDIA_PREFIX) :])
    return None


def resolve_nas_path(path: Path, prefer_nfs: bool = False) -> Path:
    """Route SMB media paths to NFS when preferred or when SMB is unmounted."""
    path = Path(path)
    nfs_alt = _to_nfs_path(path)
    if nfs_alt is None:
        return path
    if prefer_nfs and _nfs_mount_available():
        return nfs_alt
    smb_root = Path(SMB_MEDIA_PREFIX)
    try:
        smb_ok = smb_root.is_dir() and os.path.ismount(smb_root)
    except OSError:
        smb_ok = False
    if not smb_ok and _nfs_mount_available():
        return nfs_alt
    return path


def _dir_writable(dirpath: Path) -> bool:
    try:
        dirpath.mkdir(parents=True, exist_ok=True)
        probe = dirpath / f".writeprobe-{os.getpid()}-{threading.get_ident()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _await_dir_writable(dirpath: Path, staging) -> Path | None:
    """Wait up to ~10 min for `dirpath` (or its NFS mirror) to become writable."""
    dirpath = Path(dirpath)
    if _dir_writable(dirpath):
        return dirpath
    nfs_dir = _to_nfs_path(dirpath)
    for attempt in range(1, 13):
        if nfs_dir is not None and _dir_writable(nfs_dir):
            print(
                f"{C.GRN}  SMB target {dirpath} not writable; failing over "
                f"background transfer to NFS ({nfs_dir}).{C.R}",
                flush=True,
            )
            return nfs_dir
        print(
            f"{C.YLW}  Target {dirpath} not writable (attempt {attempt}/12) — "
            f"mount may be reconnecting; retrying in 50s. "
            f"Staged rip safe at {staging}.{C.R}",
            flush=True,
        )
        time.sleep(50)
        if _dir_writable(dirpath):
            print(f"{C.GRN}  Target writable again; continuing move.{C.R}", flush=True)
            return dirpath
    return None


def _rollback_incomplete_dst(src: Path, dst: Path) -> None:
    """Remove a 0-byte placeholder or incomplete copy; never remove a complete file."""
    try:
        if not dst.exists():
            return
        try:
            dsz = dst.stat().st_size
        except OSError:
            dsz = -1
        try:
            ssz = src.stat().st_size if src.exists() else -1
        except OSError:
            ssz = -1
        if dsz == 0 or (ssz >= 0 and 0 <= dsz < ssz):
            dst.unlink()
    except OSError:
        pass


def require_free_extra_destination(
    source: Path, destination: Path, overwrite: bool
) -> None:
    """Fail closed on an extras destination collision while preserving `source`."""
    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"extras destination exists; source preserved: {source} -> {destination}"
        )


def compute_target_dir(item: QueueItem) -> Path:
    root = Path(item.target_root)
    if item.type == "movie":
        return root
    return root / f"Season {item.season:02d}"


# --- Host-local season directory locking ---


def season_lock_path(target_dir: Path) -> Path:
    """Return one host-local lock key shared across SMB and NFS views of the same dir."""
    normalized = os.path.normpath(os.fspath(target_dir))
    for prefix in (SMB_MEDIA_PREFIX, NFS_MEDIA_PREFIX):
        if normalized == prefix or normalized.startswith(prefix + os.sep):
            relative = os.path.relpath(normalized, prefix)
            normalized = "media-nas/" + relative
            break
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return LOCAL_LOCK_BASE / f"season-{digest}.lock"


@contextlib.contextmanager
def season_dir_lock(target_dir: Path, what: str = "rename"):
    """Host-local cross-process `flock` for a logical NAS season directory."""
    lock_path = season_lock_path(target_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(
        str(lock_path),
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"invalid season lock file: {lock_path}")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass


# --- PID-bound hidden slot claims ---


def tv_slot_claim_path(destination: Path) -> Path:
    """Hidden reservation path; never expose a partial `.mkv` to media servers."""
    return destination.with_name(f".{destination.name}.burndvd-claim")


def _read_claim_bytes(path: Path, st: os.stat_result) -> bytes | None:
    if not stat.S_ISREG(st.st_mode) or st.st_size > 64:
        return None
    if st.st_size == 0:
        return b""
    fd = None
    try:
        fd = os.open(
            str(path),
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        opened = os.fstat(fd)
        if (
            (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size > 64
        ):
            return None
        return os.read(fd, 65)
    except OSError:
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _is_valid_claim_stat(path: Path, st: os.stat_result) -> bool:
    if not stat.S_ISREG(st.st_mode) or st.st_size > 64:
        return False
    if st.st_size == 0:
        return True
    data = _read_claim_bytes(path, st)
    return data is not None and bool(re.fullmatch(rb"\d+\n?", data))


def _claim_pid_is_dead(path: Path, st: os.stat_result) -> bool:
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0 or st.st_size > 64:
        return False
    data = _read_claim_bytes(path, st)
    if not data or not re.fullmatch(rb"\d+\n?", data):
        return False
    try:
        pid = int(data.strip())
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return False


def _remove_zero_placeholder(
    dst: Path, placeholder_identity: tuple[int, int] | None
) -> None:
    """Remove only an owned claim file matching its exact `(st_dev, st_ino)`."""
    try:
        current = os.lstat(dst)
        if (
            _is_valid_claim_stat(dst, current)
            and (current.st_dev, current.st_ino) == placeholder_identity
        ):
            dst.unlink()
    except OSError:
        pass


def _remove_claimed_placeholders(
    claimed: list[Path], identities: dict[Path, tuple[int, int]]
) -> None:
    for destination in claimed:
        _remove_zero_placeholder(destination, identities.get(destination))


def _create_tv_slot_claim(path: Path) -> tuple[int, int]:
    """Atomically create a PID-tagged claim file and return `(st_dev, st_ino)`."""
    descriptor = None
    identity = None
    payload = f"{os.getpid()}\n".encode("ascii")
    try:
        descriptor = os.open(
            str(path),
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            0o644,
        )
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError(f"short write creating TV slot claim at {path}")
        created = os.fstat(descriptor)
        identity = (created.st_dev, created.st_ino)
        if not stat.S_ISREG(created.st_mode) or created.st_size != len(payload):
            raise OSError(f"invalid TV slot claim created at {path}")
        return identity
    except BaseException:
        if identity is not None:
            _remove_zero_placeholder(path, identity)
        raise
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _claim_tv_destinations(
    destinations: list[Path],
) -> dict[Path, tuple[Path, tuple[int, int]]]:
    """Reserve every destination slot or inode-safely roll back the entire batch."""
    claims: dict[Path, tuple[Path, tuple[int, int]]] = {}
    try:
        for destination in destinations:
            claim_path = tv_slot_claim_path(destination)
            claims[destination] = (claim_path, _create_tv_slot_claim(claim_path))
    except BaseException:
        _remove_claimed_placeholders(
            [claim for claim, _ in claims.values()],
            {claim: identity for claim, identity in claims.values()},
        )
        raise
    return claims


def _emit_claimed_tv_moves(
    planned: list[tuple[Path, int, Path]],
    claims: dict[Path, tuple[Path, tuple[int, int]]],
    emit_move,
    finals: list[str],
) -> Exception | None:
    """Emit a claimed TV batch and clean up exact claim inodes on any failure/interrupt."""
    for src, episode, final in planned:
        claim_path, claim_identity = claims[final]
        try:
            emit_move(
                src,
                final,
                f"moving episode {episode:02d}",
                placeholder_identity=claim_identity,
                claim_path=claim_path,
                publish_lock_held=True,
            )
            finals.append(str(final))
        except BaseException as exc:
            _remove_claimed_placeholders(
                [claim for claim, _ in claims.values()],
                {claim: identity for claim, identity in claims.values()},
            )
            if not isinstance(exc, Exception):
                raise
            return exc
    return None


# --- Atomic no-clobber & progress movers ---


def move_with_progress(
    src: Path, dst: Path, label: str = "moving", interval: float = 5.0
) -> None:
    """Move a file across filesystems while printing periodic throughput progress."""
    try:
        if dst.is_file() and dst.stat().st_size == 0:
            dst.unlink()
    except OSError:
        pass
    total = src.stat().st_size
    done_evt = threading.Event()
    err_box: list[BaseException] = []

    def worker():
        try:
            shutil.move(str(src), str(dst))
        except BaseException as e:
            err_box.append(e)
        finally:
            done_evt.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    start = time.time()
    last_size = 0
    last_t = start
    print(f"{C.D}{label}: {src.name} -> {dst}{C.R}", flush=True)

    while not done_evt.wait(timeout=interval):
        try:
            cur = dst.stat().st_size if dst.exists() else 0
        except FileNotFoundError:
            cur = 0
        now = time.time()
        frac = cur / total if total > 0 else 0.0
        dt = now - last_t
        delta = cur - last_size
        mbps = delta / dt / (1024 * 1024) if dt > 0 else 0.0
        eta = (total - cur) / (delta / dt) if delta > 0 and dt > 0 else 0
        bar = render_bar(frac)
        elapsed = fmt_dur(now - start)
        print(
            f"  {bar} {frac * 100:5.1f}%  {cur / 1e9:5.2f}/{total / 1e9:5.2f} GB"
            f"  {mbps:5.1f} MB/s  eta {fmt_dur(eta)}  elapsed {elapsed}",
            flush=True,
        )
        last_size = cur
        last_t = now

    t.join()
    if err_box:
        raise err_box[0]
    elapsed = time.time() - start
    avg_mbps = total / elapsed / (1024 * 1024) if elapsed > 0 else 0.0
    print(
        f"  {C.GRN}done{C.R}  {total / 1e9:.2f} GB in {fmt_dur(elapsed)}"
        f"  ({avg_mbps:.1f} MB/s avg)",
        flush=True,
    )


def _atomic_rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish on one filesystem without overwriting an existing target.

    Uses `renameatx_np(RENAME_EXCL)` on macOS, `renameat2(RENAME_NOREPLACE)` on
    Linux, and `os.link()` + `unlink()` fallback where native no-replace rename
    is unsupported. Fails closed if neither atomic primitive works.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        fn = libc.renameatx_np
        fn.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        fn.restype = ctypes.c_int
        rc = fn(-2, os.fsencode(source), -2, os.fsencode(destination), 0x4)
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        fn = libc.renameat2
        fn.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        fn.restype = ctypes.c_int
        rc = fn(-100, os.fsencode(source), -100, os.fsencode(destination), 0x1)
    else:
        rc = -1
        ctypes.set_errno(errno.ENOTSUP)
    if rc == 0:
        return
    native_errno = ctypes.get_errno()
    if native_errno == errno.EEXIST:
        raise FileExistsError(native_errno, os.strerror(native_errno), destination)

    try:
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError:
        raise
    except OSError as fallback_error:
        raise OSError(
            native_errno,
            f"atomic no-replace rename unsupported ({os.strerror(native_errno)}); "
            f"hard-link fallback failed: {fallback_error}",
            destination,
        ) from fallback_error
    try:
        source.unlink()
    except OSError:
        pass


def move_with_progress_noclobber(
    src: Path,
    dst: Path,
    label: str = "moving",
    interval: float = 5.0,
    *,
    placeholder_identity: tuple[int, int] | None = None,
    claim_path: Path | None = None,
    partial_root: Path | None = None,
    lock_dir: Path | None = None,
    publish_lock_held: bool = False,
) -> None:
    """Copy to out-of-library `.partial`, fsync, atomically publish no-clobber, retire source."""
    src, dst = Path(src), Path(dst)
    if partial_root is None:
        raise ValueError("protected TV move requires an out-of-library partial_root")
    partial_root = Path(partial_root)
    dst.parent.mkdir(parents=True, exist_ok=True)
    partial_root.mkdir(parents=True, exist_ok=True)

    source_fd = os.open(
        str(src),
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise OSError(f"protected TV source is not a regular file: {src}")
        source_identity = (source_stat.st_dev, source_stat.st_ino)
        total = source_stat.st_size
        source_guard_fd = os.dup(source_fd)
    except BaseException:
        os.close(source_fd)
        raise

    token = f".{os.getpid()}-{time.time_ns()}-{os.urandom(8).hex()}.partial"
    partial = partial_root / token
    partial_fd = None
    monitor_fd = None
    try:
        partial_fd = os.open(
            str(partial),
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            0o644,
        )
        partial_stat = os.fstat(partial_fd)
        if not stat.S_ISREG(partial_stat.st_mode):
            raise OSError(f"protected TV partial is not a regular file: {partial}")
        partial_identity = (partial_stat.st_dev, partial_stat.st_ino)
        monitor_fd = os.dup(partial_fd)
    except BaseException:
        for descriptor in (monitor_fd, partial_fd, source_fd, source_guard_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise

    done_evt = threading.Event()
    err_box: list[BaseException] = []

    def worker():
        try:
            with os.fdopen(source_fd, "rb") as source_stream, os.fdopen(
                partial_fd, "wb"
            ) as output:
                shutil.copyfileobj(source_stream, output, length=8 * 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
        except BaseException as exc:
            err_box.append(exc)
        finally:
            done_evt.set()

    threading.Thread(target=worker, daemon=True).start()
    start = time.time()
    last_size = 0
    last_t = start
    print(f"{C.D}{label}: {src.name} -> {dst} (protected){C.R}", flush=True)
    published = False
    try:
        while not done_evt.wait(timeout=interval):
            current = os.fstat(monitor_fd).st_size
            now = time.time()
            delta_t = now - last_t
            delta = current - last_size
            mbps = delta / delta_t / (1024 * 1024) if delta_t > 0 else 0.0
            eta = (
                (total - current) / (delta / delta_t)
                if delta > 0 and delta_t > 0
                else 0
            )
            fraction = current / total if total > 0 else 0.0
            print(
                f"  {render_bar(fraction)} {fraction * 100:5.1f}%  "
                f"{current / 1e9:5.2f}/{total / 1e9:5.2f} GB  "
                f"{mbps:5.1f} MB/s  eta {fmt_dur(eta)}  "
                f"elapsed {fmt_dur(now - start)}",
                flush=True,
            )
            last_size, last_t = current, now
        os.close(monitor_fd)
        monitor_fd = None
        if err_box:
            raise err_box[0]
        copied = os.lstat(partial)
        guarded_source = os.fstat(source_guard_fd)
        if (copied.st_dev, copied.st_ino) != partial_identity or copied.st_size != total:
            raise OSError(f"protected TV partial identity/size changed: {partial}")
        if (
            (guarded_source.st_dev, guarded_source.st_ino) != source_identity
            or guarded_source.st_size != total
        ):
            raise OSError(f"protected TV source identity/size changed: {src}")

        publish_lock = (
            contextlib.nullcontext()
            if publish_lock_held or lock_dir is None
            else season_dir_lock(lock_dir, what="protected TV publish")
        )
        with publish_lock:
            if claim_path is not None:
                claim = os.lstat(claim_path)
                if (
                    not _is_valid_claim_stat(claim_path, claim)
                    or (claim.st_dev, claim.st_ino) != placeholder_identity
                ):
                    raise FileExistsError(
                        f"TV slot claim ownership changed: {claim_path}"
                    )
            _atomic_rename_noreplace(partial, dst)
            published = True
            if claim_path is not None:
                _remove_zero_placeholder(claim_path, placeholder_identity)

        retired = src.with_name(
            f".{src.name}.burndvd-consumed-{os.getpid()}-"
            f"{time.time_ns()}-{os.urandom(8).hex()}"
        )
        try:
            os.rename(src, retired)
        except OSError as exc:
            raise OSError(
                f"source path changed after publication; destination retained "
                f"at {dst}: {exc}"
            ) from exc
        retired_stat = os.lstat(retired)
        if (retired_stat.st_dev, retired_stat.st_ino) != source_identity:
            retained_at = retired
            if not src.exists():
                try:
                    os.rename(retired, src)
                    retained_at = src
                except OSError:
                    pass
            raise OSError(
                f"source path was replaced during protected copy; completed "
                f"destination retained at {dst}; foreign source retained at "
                f"{retained_at}"
            )
        retired.unlink()
    except BaseException:
        if monitor_fd is not None:
            try:
                os.close(monitor_fd)
            except OSError:
                pass
            monitor_fd = None
        if not published:
            try:
                current = os.lstat(partial)
                if (current.st_dev, current.st_ino) == partial_identity:
                    partial.unlink()
            except OSError:
                pass
        raise
    finally:
        try:
            os.close(source_guard_fd)
        except OSError:
            pass

    elapsed = time.time() - start
    avg_mbps = total / elapsed / (1024 * 1024) if elapsed > 0 else 0.0
    print(
        f"  {C.GRN}done{C.R}  {total / 1e9:.2f} GB in {fmt_dur(elapsed)}"
        f"  ({avg_mbps:.1f} MB/s avg, protected)",
        flush=True,
    )


# --- Season slot inspection & review/quarantine isolation ---


def existing_episode_slots(
    item: QueueItem, target_dir: Path, *, include_claims: bool = False
) -> dict[int, Path]:
    """Scan `target_dir` for landed episode `.mkv` files (and optionally active claims)."""
    slots: dict[int, Path] = {}
    media_pattern = re.compile(
        rf"^{re.escape(sanitize(item.title))} - S0*{item.season}E(\d+)\.mkv$",
        re.IGNORECASE,
    )
    claim_pattern = re.compile(
        rf"^\.{re.escape(sanitize(item.title))} - "
        rf"S0*{item.season}E(\d+)\.mkv\.burndvd-claim$",
        re.IGNORECASE,
    )
    try:
        entries = list(target_dir.iterdir()) if target_dir.exists() else []
    except OSError as exc:
        nfs_alt = _to_nfs_path(target_dir)
        if nfs_alt is not None and _nfs_mount_available():
            try:
                entries = list(nfs_alt.iterdir()) if nfs_alt.exists() else []
            except OSError as nfs_exc:
                raise RuntimeError(
                    f"cannot inspect season directory {target_dir} "
                    f"(NFS fallback {nfs_alt} failed: {nfs_exc}): {exc}"
                ) from nfs_exc
        else:
            raise RuntimeError(
                f"cannot inspect season directory {target_dir}: {exc}"
            ) from exc
    for entry in entries:
        match = media_pattern.match(entry.name)
        is_claim = False
        if match is None and include_claims:
            match = claim_pattern.match(entry.name)
            is_claim = match is not None
        if not match:
            continue
        try:
            if is_claim:
                st = entry.lstat()
                if _claim_pid_is_dead(entry, st):
                    continue
            elif entry.stat().st_size <= 0 and not include_claims:
                continue
        except OSError as exc:
            raise RuntimeError(f"cannot stat existing episode {entry}: {exc}") from exc
        slots[int(match.group(1))] = entry
    return slots


def tv_contract_output_mode(
    item: QueueItem,
    target_dir: Path,
    *,
    rerip_review: bool = False,
    auto_rerip_review: bool = False,
) -> tuple[str, str | None, list[int]]:
    """Resolve normal vs isolated review TV output from current season inventory."""
    if item.type != "tv-season":
        return "normal", None, []
    if rerip_review:
        return "review", None, []
    if item.expected_disc_episodes == 0:
        return "normal", None, []
    slots = existing_episode_slots(item, target_dir, include_claims=True)
    outside = sorted(ep for ep in slots if ep > item.expected_episodes)
    if outside:
        return (
            "normal",
            (
                f"season contains episode slots beyond authoritative total "
                f"{item.expected_episodes}: {outside}; repair inventory first"
            ),
            [],
        )
    wanted = set(range(item.episode_start, item.episode_start + item.expected_disc_episodes))
    occupied = sorted(wanted.intersection(slots))
    if occupied and auto_rerip_review:
        return "review", None, occupied
    if occupied:
        return (
            "normal",
            (
                f"disc's authoritative episode slots already exist: {occupied}; "
                "refusing append/re-rip (use --rerip-review for traceable review output)"
            ),
            occupied,
        )
    if len(slots) >= item.expected_episodes:
        return (
            "normal",
            (
                f"season already has {len(slots)}/{item.expected_episodes} episodes; "
                "refusing append/re-rip"
            ),
            [],
        )
    return "normal", None, []


def tv_contract_preflight_error(
    item: QueueItem, target_dir: Path, rerip_review: bool = False
) -> str | None:
    _, error, _ = tv_contract_output_mode(
        item, target_dir, rerip_review=rerip_review
    )
    return error


def apply_tv_output_mode(
    args, item: QueueItem, target_dir: Path, phase: str
) -> str | None:
    mode, error, occupied = tv_contract_output_mode(
        item,
        target_dir,
        rerip_review=getattr(args, "rerip_review", False),
        auto_rerip_review=getattr(args, "auto_rerip_review", False),
    )
    if error:
        return error
    if mode == "review" and not getattr(args, "rerip_review", False):
        args.rerip_review = True
        slots = ",".join(str(ep) for ep in occupied) or "unknown"
        print(
            f"{C.YLW}Occupied authoritative TV slot(s) {slots} detected "
            f"during {phase}; switching this disc to protected review output.{C.R}"
        )
        append_log(
            args,
            f"AUTO_RERIP_REVIEW {item.title} S{item.season:02d} "
            f"phase={phase} occupied={slots}",
        )
    return None


def _validated_tv_review_root(
    item: QueueItem,
    effective_target_dir: Path,
    review_root: str | Path | None = None,
) -> tuple[Path, list[Path]]:
    if item.type != "tv-season":
        raise ValueError("review capture directories are only valid for TV")
    effective_target_dir = effective_target_dir.expanduser().resolve(strict=False)
    library_anchor = next(
        (
            candidate
            for candidate in (effective_target_dir, *effective_target_dir.parents)
            if candidate.name.casefold() in {"tv shows", "tv shows 4k"}
        ),
        None,
    )
    if review_root is not None:
        quarantine_root = Path(review_root).expanduser()
        if not quarantine_root.is_absolute():
            raise ValueError("--review-root must be an absolute path")
    else:
        if library_anchor is None:
            raise ValueError(
                "cannot derive an out-of-library review destination; "
                "pass --review-root"
            )
        quarantine_root = (
            library_anchor.parent / ".repair-quarantine" / "burndvd-review"
        )

    quarantine_root = quarantine_root.resolve(strict=False)
    if any(
        candidate.name.casefold() in {"tv shows", "tv shows 4k"}
        for candidate in (quarantine_root, *quarantine_root.parents)
    ):
        raise ValueError("review destination must be outside every TV media library")

    forbidden_roots = [Path(item.target_root).expanduser().resolve(strict=False)]
    if library_anchor is not None:
        forbidden_roots.append(library_anchor)
    for forbidden in forbidden_roots:
        try:
            quarantine_root.relative_to(forbidden)
        except ValueError:
            continue
        raise ValueError("review destination must be outside the media library")
    return quarantine_root, forbidden_roots


def tv_partial_capture_root(
    item: QueueItem,
    effective_target_dir: Path,
    review_root: str | Path | None = None,
) -> Path:
    quarantine_root, forbidden_roots = _validated_tv_review_root(
        item, effective_target_dir, review_root
    )
    partial_root = (quarantine_root / ".burndvd-partials").resolve(strict=False)
    try:
        partial_root.relative_to(quarantine_root)
    except ValueError as exc:
        raise ValueError("TV partial destination escaped its quarantine root") from exc
    if any(
        candidate.name.casefold() in {"tv shows", "tv shows 4k"}
        for candidate in (partial_root, *partial_root.parents)
    ):
        raise ValueError("TV partial destination must be outside every TV library")
    for forbidden in forbidden_roots:
        try:
            partial_root.relative_to(forbidden)
        except ValueError:
            continue
        raise ValueError("TV partial destination must be outside the media library")
    return partial_root


def protected_quarantine_child(parent: Path, name: str) -> Path:
    parent = Path(parent).resolve(strict=False)
    child = (parent / name).resolve(strict=False)
    try:
        child.relative_to(parent)
    except ValueError as exc:
        raise ValueError(f"quarantine child {name!r} escaped {parent}") from exc
    if any(
        candidate.name.casefold() in {"tv shows", "tv shows 4k"}
        for candidate in (child, *child.parents)
    ):
        raise ValueError("quarantine child must be outside every TV library")
    return child


def tv_review_capture_dir(
    item: QueueItem,
    effective_target_dir: Path,
    fingerprint: str,
    review_root: str | Path | None = None,
) -> Path:
    compact_fingerprint = re.sub(r"[^0-9a-f]", "", fingerprint.casefold())
    if len(compact_fingerprint) < 16:
        raise ValueError("review capture requires a valid disc fingerprint")
    safe_title = sanitize(item.title)
    if safe_title in {"", ".", ".."}:
        raise ValueError("review title must be a safe non-empty path component")
    quarantine_root, forbidden_roots = _validated_tv_review_root(
        item, effective_target_dir, review_root
    )
    capture = (
        quarantine_root
        / safe_title
        / f"Season {item.season:02d}"
        / compact_fingerprint[:16]
    ).resolve(strict=False)
    try:
        capture.relative_to(quarantine_root)
    except ValueError as exc:
        raise ValueError("review destination escaped its quarantine root") from exc
    for forbidden in forbidden_roots:
        try:
            capture.relative_to(forbidden)
        except ValueError:
            continue
        raise ValueError("review destination must be outside the media library")
    return capture


def reserve_tv_review_attempt_dir(capture_dir: Path, staging: Path) -> Path:
    capture_dir = Path(capture_dir).resolve(strict=False)
    raw_attempt = Path(staging).name
    safe_attempt = re.sub(r"[^0-9A-Za-z._-]", "-", raw_attempt).strip(".-")
    if not safe_attempt:
        raise ValueError("review attempt requires a safe staging identifier")
    capture_dir.mkdir(parents=True, exist_ok=True)
    for sequence in range(1, 10_001):
        suffix = "" if sequence == 1 else f"-{sequence}"
        candidate = protected_quarantine_child(
            capture_dir, f"attempt-{safe_attempt}{suffix}"
        )
        try:
            candidate.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(
        f"could not reserve a unique review attempt under {capture_dir}"
    )


def tv_show_extras_dir(target_dir: Path, item: QueueItem) -> Path:
    if item.type != "tv-season":
        raise ValueError("show-level extras are only valid for TV seasons")
    season_dir = Path(target_dir)
    expected = f"Season {item.season:02d}"
    if season_dir.name.casefold() != expected.casefold():
        raise ValueError(
            f"TV target is not the expected season directory {expected}: {season_dir}"
        )
    return season_dir.parent / "Extras"


# --- Episode numbering & staging salvage ---


def ep_key(item: QueueItem) -> str:
    return f"{item.title}||S{item.season:02d}"


def starting_ep(
    item: QueueItem,
    disc_n: int,
    state: dict,
    target_dir: Path | None = None,
) -> int:
    """Compute the starting episode number for a TV disc (respecting contracts & claims)."""
    if item.type == "tv-season" and item.expected_disc_episodes > 0:
        return item.episode_start
    counts = state.get("disc_episode_counts", {}).get(ep_key(item), {})
    prior = sum(int(v) for k, v in counts.items() if int(k) < disc_n)
    state_based = item.episode_start + prior
    if not counts:
        season_dir = (
            Path(target_dir)
            if target_dir is not None
            else resolve_nas_path(compute_target_dir(item), prefer_nfs=True)
        )
        if not season_dir.exists():
            s_str = str(season_dir)
            if s_str.startswith(SMB_MEDIA_PREFIX + "/"):
                alt = Path(s_str.replace(SMB_MEDIA_PREFIX + "/", NFS_MEDIA_PREFIX + "/", 1))
                if alt.exists():
                    season_dir = alt
            elif s_str.startswith(NFS_MEDIA_PREFIX + "/"):
                alt = Path(s_str.replace(NFS_MEDIA_PREFIX + "/", SMB_MEDIA_PREFIX + "/", 1))
                if alt.exists():
                    season_dir = alt
        try:
            highest = 0
            if season_dir.exists():
                slots = existing_episode_slots(item, season_dir, include_claims=True)
                if slots:
                    highest = max(highest, max(slots))
                pat = re.compile(rf"S0*{item.season}E(\d+)")
                for entry in season_dir.iterdir():
                    if entry.suffix.lower() != ".mkv":
                        continue
                    try:
                        if entry.stat().st_size == 0:
                            continue
                    except OSError:
                        continue
                    m = pat.search(entry.name)
                    if m:
                        highest = max(highest, int(m.group(1)))
            if highest >= state_based:
                return highest + 1
        except (OSError, RuntimeError):
            pass
    return state_based


def record_episode_count(
    item: QueueItem, disc_n: int, count: int, state: dict
) -> None:
    state.setdefault("disc_episode_counts", {}).setdefault(ep_key(item), {})[
        str(disc_n)
    ] = int(count)


def _title_complete(staged: Path, info: dict) -> bool:
    tnum = title_num_from_filename(staged.name)
    t = info.get("titles", {}).get(tnum, {}) if tnum is not None else {}
    exp_dur = parse_duration(t.get(9, "0:00:00"))
    try:
        exp_bytes = int(t.get(11, "0") or "0")
    except ValueError:
        exp_bytes = 0
    try:
        act_bytes = staged.stat().st_size
    except OSError:
        act_bytes = 0
    act_dur = ffprobe_dur_s(str(staged))
    dur_ok = exp_dur > 0 and act_dur >= exp_dur - 3
    size_ok = exp_bytes > 0 and act_bytes >= exp_bytes * 0.97
    if exp_dur == 0 and exp_bytes == 0:
        return act_dur > 0
    return dur_ok or size_ok


def _salvage_and_fail(
    args,
    item: QueueItem,
    info: dict,
    disc_n: int,
    staging: Path,
    target_dir: Path,
    reason: str,
):
    """Quarantine any completed staged titles with traceable t-names before failing."""
    try:
        staged = sorted(staging.glob("*.mkv"))
    except OSError:
        staged = []
    complete, dropped = [], []
    for p in staged:
        (complete if _title_complete(p, info) else dropped).append(p)
    if not complete:
        shutil.rmtree(staging, ignore_errors=True)
        return False, f"{reason} (no complete titles to salvage)"

    if item.type == "tv-season":
        if str(target_dir).startswith(SMB_MEDIA_PREFIX + "/"):
            target_dir = Path(
                str(target_dir).replace(SMB_MEDIA_PREFIX + "/", NFS_MEDIA_PREFIX + "/", 1)
            )
        try:
            capture_dir = tv_review_capture_dir(
                item,
                target_dir,
                getattr(args, "current_disc_fingerprint", ""),
                getattr(args, "review_root", None),
            )
            attempt_dir = reserve_tv_review_attempt_dir(capture_dir, staging)
            partial_dir = protected_quarantine_child(attempt_dir, "_partial")
        except (OSError, ValueError) as exc:
            return False, (
                f"{reason}; cannot isolate partial review output ({exc}) — "
                f"staging PRESERVED at {staging}"
            )
    else:
        partial_dir = target_dir / "_partial"

    saved_names: list[str] = []
    try:
        partial_dir.mkdir(parents=True, exist_ok=True)
        planned = []
        for p in complete:
            tnum = title_num_from_filename(p.name)
            tag = f"t{tnum:02d}" if tnum is not None else p.stem
            if item.type == "movie":
                base = movie_filename(item, disc_n)[:-4]
                name = f"{base} - {tag}.mkv"
            else:
                name = f"{sanitize(item.title)} - S{item.season:02d}D{disc_n} - {tag}.mkv"
            dest = partial_dir / name
            allow_overwrite = bool(getattr(args, "overwrite", False) and item.type == "movie")
            require_free_extra_destination(p, dest, allow_overwrite)
            planned.append((p, dest, tag))
        for p, dest, tag in planned:
            if item.type == "tv-season":
                move_with_progress_noclobber(
                    p,
                    dest,
                    label=f"salvaging {tag}",
                    partial_root=tv_partial_capture_root(
                        item, target_dir, getattr(args, "review_root", None)
                    ),
                    lock_dir=target_dir,
                )
            else:
                move_with_progress(p, dest, label=f"salvaging {tag}")
            saved_names.append(dest.name)
    except OSError as e:
        return False, (
            f"{reason}; salvage of {len(complete)} complete title(s) "
            f"FAILED ({e}) — staging PRESERVED at {staging}"
        )

    shutil.rmtree(staging, ignore_errors=True)
    if dropped:
        print(
            f"{C.YLW}  salvage: dropped {len(dropped)} truncated/partial "
            f"title(s): {', '.join(p.name for p in dropped)}{C.R}"
        )
    print(f"{C.GRN}  SALVAGED {len(saved_names)} complete title(s) -> {partial_dir}{C.R}")
    print(
        f"{C.YLW}  Traceable t-names (NOT SxxEyy) — promote manually after "
        f"re-ripping the missing title(s).{C.R}"
    )
    return False, (
        f"{reason}; SALVAGED {len(saved_names)} complete title(s) to "
        f"{partial_dir} (traceable t-names; {len(dropped)} truncated "
        f"dropped; promote manually)"
    )
