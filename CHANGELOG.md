# Changelog

All notable changes to `burndvd-makemkv` are documented in this file.

## [0.1.0] - 2026-11-27

### Added
- **Crash-recoverable SHA-256 publish-intent journal:** Two-phase NAS publication writes and `fsync`s a verified SHA-256 publish intent before atomic `renameatx_np(RENAME_EXCL)` / `os.link()` placement and `.burndvd-committed.json` commit persistence, retiring staged sources only after durable commit and reconciling crash-interrupted replays without clobbering or self-collision. Includes streamed partial SHA-256 verification before publish (`DVD-10`).
- **Multi-drive hardware-pinned locking & session drive cache:** Re-entrant per-slot and physical-drive `flock` locking across MakeMKV dispatches, full resolved drive-identity pinning prior to scan unmounts, verified MakeMKV drive-binding cache within locked `slot_lock_scope`, ancestor wrapper PID exclusion on active disc claims, and hardware-identity attribution in `checkrip` (safe under duplicate volume labels).
- **Early optical release & watcher separation:** Releases optical drive locks, claims, and ejects immediately after local staging finishes while finalizers publish asynchronously; `burndvd --watch` separates optical occupancy from finalizer jobs, binds discs strictly to show/season/allowlist rules, respects explicit `episode_start` gap-fills, and spawns session-isolated workers (`start_new_session=True`).
- **MakeMKV direct-access stall handling & playback/media hardening:** Extended probe stall timeouts during DVD direct disc access (`MSG:3007`), concrete `--noscan` disc probes, multi-drive free-slot discovery fallback, unencrypted Blu-ray `mkvmerge` failover (fail-closed on `AACS/`), and benign UHD TrueHD splice / HEVC PPS notice handling in the `ffmpeg` pre-publish verifier.
- **Zero-hardware synthetic demo & regression suite:** Added `examples/synthetic_demo.py` (exercising strict watch-mode binding, multi-drive slot serialization, publish-journal crash recovery, and duplicate-label `checkrip` JSON output with zero optical hardware) alongside the `pytest` regression suite (`322 passed, 6 skipped` on Python 3.11 and macOS CI Python 3.12).
