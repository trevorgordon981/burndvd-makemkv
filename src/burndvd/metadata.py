"""Pure, hardware-free metadata, title inference, and TV episode-contract helpers."""

from __future__ import annotations

import atexit
import csv
import os
import subprocess
import time
import json
import re
from pathlib import Path

QUEUE_FIELDS = (
    "title",
    "type",
    "season",
    "episode_start",
    "expected_episodes",
    "expected_disc_episodes",
    "expected_title_ids",
    "expected_physical_disc",
    "discs",
    "target_root",
    "format",
    "notes",
    "features",
)

# Verified physical-disc title contracts for multi-disc TV box sets where a
# bonus featurette shares episode-length runtime and could shift numbering.
TV_EPISODE_CONTRACTS: dict[str, dict[int, tuple[int, tuple[tuple[int, ...], ...]]]] = {
    "gameofthrones": {
        1: (10, ((1, 2), (1, 2, 3), (1, 2, 3), (1, 2))),
        2: (10, ((1, 2, 3), (1, 2, 3), (1, 2, 3), (1,))),
        3: (10, ((1, 2, 3), (1, 2, 3), (1, 2, 3), (1,))),
        4: (10, ((1, 3), (1, 2, 3), (1, 2, 3), (1, 2))),
        5: (10, ((1, 2), (1, 2, 3), (1, 2, 3), (1, 2))),
        6: (10, ((1, 2, 5), (1, 2), (1, 2, 3), (1, 2))),
    },
}

_YEAR_SUFFIX_RE = re.compile(r"\((?:18|19|20)\d{2}\)$")
_GOT_DISC_RE = re.compile(
    r"(?i)^game[._\s-]*of[._\s-]*thrones[._\s-]*"
    r"s0*(\d+)[._\s-]*(?:disc|d)[._\s-]*0*(\d+)(?:$|[._\s-])"
)


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def canonical_fast_title(value: str) -> str | None:
    """Return a canonical movie title for abbreviated Fast-franchise disc labels."""
    key = _compact(value)
    if "tokyodrift" in key:
        return "The Fast and the Furious: Tokyo Drift (2006)"
    if key == "2f2f" or "2fast2furious" in key:
        return "2 Fast 2 Furious (2003)"
    if key in {"thefastandthefurious", "fastandfurious2001"}:
        return "The Fast and the Furious (2001)"
    if key in {"fastandfurious", "fastandfurious2009"}:
        return "Fast & Furious (2009)"
    if "fastfive" in key:
        return "Fast Five (2011)"
    if key in {"fastfurious6", "fastandfurious6", "furious6"}:
        return "Fast & Furious 6 (2013)"
    if "furious7" in key:
        return "Furious 7 (2015)"
    if "fateofthefurious" in key:
        return "The Fate of the Furious (2017)"
    if key in {"f9", "f9thefastsaga"}:
        return "F9: The Fast Saga (2021)"
    if "fastx" in key:
        return "Fast X (2023)"
    return None


def movie_title_has_year(value: str) -> bool:
    return bool(_YEAR_SUFFIX_RE.search(value.strip()))


def normalize_movie_title(value: str) -> str:
    title = re.sub(r"\s+", " ", value).strip()
    if not title:
        return ""
    if movie_title_has_year(title):
        return title
    return canonical_fast_title(title) or title


_TV_TRAILING_CLUSTER_RE = re.compile(
    r"(?i)[\s._-]+(?:(?:season|s|part|pt|p|disc|disk|d|vol(?:ume)?)[\s._-]*\d+)+$"
)
_EXPLICIT_SEASON_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9])(?:S0*(\d+)|Season[\s._-]*0*(\d+))(?:$|[^0-9])"
)
_SEASON_DIR_RE = re.compile(r"(?i)^Season\s+0*(\d+)$")


def _strip_tv_disc_suffix(volume: str) -> str:
    """Strip trailing TV disc/part/season/volume tokens from a volume label."""
    prefix = volume.strip()
    while True:
        updated = _TV_TRAILING_CLUSTER_RE.sub("", prefix).strip()
        if not updated or updated == prefix:
            break
        prefix = updated
    return prefix


def _infer_season_from_volume_or_dir(volume: str, show_dir: Path) -> str:
    """Infer TV season number from explicit volume token or highest NAS Season dir."""
    season_match = _EXPLICIT_SEASON_RE.search(volume.strip())
    if season_match:
        return str(int(season_match.group(1) or season_match.group(2)))
    highest = 0
    try:
        for child in show_dir.iterdir():
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            m = _SEASON_DIR_RE.match(child.name.strip())
            if m:
                num = int(m.group(1))
                if num > highest:
                    highest = num
    except OSError:
        pass
    return str(highest) if highest > 0 else "1"


def infer_defaults(
    volume: str, *, tv_roots: list[Path | str] | None = None
) -> tuple[str, str, str]:
    """Infer `(type, season, title)` from an optical disc volume label."""
    got_disc = _GOT_DISC_RE.match(volume.strip())
    if got_disc:
        return "tv-season", got_disc.group(1), "Game of Thrones"

    if tv_roots:
        vol_prefix = _strip_tv_disc_suffix(volume)
        target_key = _compact(vol_prefix)
        if target_key:
            for root_raw in tv_roots:
                if not root_raw:
                    continue
                root = Path(root_raw)
                try:
                    if not root.is_dir():
                        continue
                    entries = sorted(root.iterdir(), key=lambda p: p.name.casefold())
                except OSError:
                    continue
                for show_dir in entries:
                    try:
                        if not show_dir.is_dir():
                            continue
                    except OSError:
                        continue
                    if _compact(show_dir.name) == target_key:
                        season = _infer_season_from_volume_or_dir(volume, show_dir)
                        return "tv-season", season, show_dir.name

    spaced = volume.replace("_", " ").strip()
    tv_pat = r"(?i)(S\d+\s*D\d+|Season[\s-]+\d+|Disc[\s-]+\d+|Vol(?:ume)?[\s-]+\d+)"
    marker = re.search(tv_pat, spaced)
    if marker:
        season_match = re.search(
            r"(?i)(?<![A-Za-z])S0*(\d+)|Season[\s-]+0*(\d+)", spaced
        )
        season = (
            (season_match.group(1) or season_match.group(2))
            if season_match
            else ""
        )
        title = re.sub(tv_pat + r".*$", "", spaced).strip().rstrip("-").strip().title()
        return "tv-season", season, title

    mapped = canonical_fast_title(spaced)
    if mapped:
        title = mapped
    elif movie_title_has_year(spaced):
        title = spaced
    else:
        title = spaced.title()
    return "movie", "", title


def disc_number_from_label(value: str) -> int | None:
    """Extract a physical disc number without treating a season number as a disc."""
    match = re.search(
        r"(?i)(?:^|[^a-z0-9])(?:disc|disk|d)[._\s-]*0*(\d{1,2})(?:$|[^\d])",
        value,
    )
    return int(match.group(1)) if match else None


def episode_contract(title: str, season: int, volume: str) -> dict | None:
    """Return the immutable physical-disc episode contract for a registered TV disc."""
    season_contract = TV_EPISODE_CONTRACTS.get(_compact(title), {}).get(int(season))
    disc = disc_number_from_label(volume)
    if season_contract is None or disc is None:
        return None
    total, per_disc = season_contract
    if disc < 1 or disc > len(per_disc):
        return None
    title_ids = [int(value) for value in per_disc[disc - 1]]
    count = len(title_ids)
    start = 1 + sum(len(value) for value in per_disc[: disc - 1])
    return {
        "disc": disc,
        "episode_start": start,
        "expected_disc_episodes": count,
        "expected_episodes": int(total),
        "expected_title_ids": title_ids,
    }


def parse_features(raw: str) -> list[dict[str, object]]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"features is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, list):
        raise ValueError("features must be a JSON array")

    result: list[dict[str, object]] = []
    seen: set[int] = set()
    for index, feature in enumerate(data, start=1):
        if not isinstance(feature, dict):
            raise ValueError(f"feature {index} must be an object")
        try:
            title_id = int(feature["title_id"])
            raw_name = feature["name"]
            if not isinstance(raw_name, str):
                raise TypeError("name is not a string")
            name = raw_name.strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"feature {index} requires integer title_id and non-empty name"
            ) from exc
        if title_id < 0 or not name:
            raise ValueError(
                f"feature {index} requires non-negative title_id and non-empty name"
            )
        if title_id in seen:
            raise ValueError(f"duplicate feature title_id {title_id}")
        seen.add(title_id)
        result.append({"title_id": title_id, "name": name})
    return result


def write_queue(
    output: Path,
    *,
    title: str,
    item_type: str,
    season: str,
    episode_start: str,
    target_root: str,
    media_format: str,
    notes: str,
    features_json: str = "",
    expected_episodes: str = "",
    expected_disc_episodes: str = "",
    expected_title_ids: str = "",
    expected_physical_disc: str = "",
) -> None:
    """Write a single-item queue CSV with RFC-4180 quoting and embedded JSON."""
    features = parse_features(features_json)
    normalized_features = (
        json.dumps(features, ensure_ascii=False, separators=(",", ":"))
        if features
        else ""
    )
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(QUEUE_FIELDS)
        writer.writerow(
            [
                title,
                item_type,
                season,
                episode_start,
                expected_episodes,
                expected_disc_episodes,
                expected_title_ids,
                expected_physical_disc,
                "1",
                target_root,
                media_format,
                notes,
                normalized_features,
            ]
        )

ACTIVE_TV_SLOTS_ENV = "BURNDVD_ACTIVE_TV_SLOTS_DIR"
_REGISTERED_ATEXIT_PIDS: set[int] = set()


def active_tv_slots_dir(slots_dir: Path | str | None = None) -> Path:
    if slots_dir is not None:
        return Path(slots_dir)
    env_dir = os.environ.get(ACTIVE_TV_SLOTS_ENV, "").strip()
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".cache" / "burndvd" / "active-tv-slots"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def clear_tv_slot_reservation(
    pid: int | None = None, *, slots_dir: Path | str | None = None
) -> None:
    target_pid = os.getpid() if pid is None else int(pid)
    try:
        (active_tv_slots_dir(slots_dir) / f"pid-{target_pid}.json").unlink(
            missing_ok=True
        )
    except OSError:
        pass


def write_tv_slot_reservation(
    *,
    pid: int,
    title: str,
    season: int,
    target_root: str,
    episode_start: int,
    episode_count: int,
    slots_dir: Path | str | None = None,
) -> Path | None:
    if episode_start <= 0 or episode_count <= 0:
        return None
    sdir = active_tv_slots_dir(slots_dir)
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        dest = sdir / f"pid-{int(pid)}.json"
        tmp = sdir / f"pid-{int(pid)}.json.tmp.{os.getpid()}"
        payload = {
            "pid": int(pid),
            "title": str(title),
            "season": int(season),
            "target_root": str(target_root),
            "episode_start": int(episode_start),
            "episode_count": int(episode_count),
            "next_episode": int(episode_start) + int(episode_count),
            "created_at": time.time(),
        }
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, dest)
        if int(pid) == os.getpid() and int(pid) not in _REGISTERED_ATEXIT_PIDS:
            _REGISTERED_ATEXIT_PIDS.add(int(pid))
            atexit.register(clear_tv_slot_reservation, int(pid), slots_dir=sdir)
        return dest
    except OSError:
        return None


def scan_season_highest_episode(
    season_dir: Path | str | None, season: int
) -> int:
    if not season_dir:
        return 0
    sdir = Path(season_dir)
    if not sdir.is_dir():
        return 0
    pat = re.compile(rf"S0*{int(season)}E(\d+)", re.IGNORECASE)
    highest = 0
    try:
        for entry in sdir.iterdir():
            if not entry.name.lower().endswith(".mkv"):
                continue
            try:
                if not entry.is_file() or entry.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            m = pat.search(entry.name)
            if m:
                ep = int(m.group(1))
                if ep > highest:
                    highest = ep
    except OSError:
        return highest
    return highest


def _norm_media_root(p: str, smb: str, nfs: str) -> str:
    norm = os.path.normpath((p or "").strip())
    if not norm or norm == ".":
        return ""
    for pref in (smb.rstrip("/"), nfs.rstrip("/")):
        if pref and (norm == pref or norm.startswith(pref + "/")):
            return "<MEDIA>" + norm[len(pref):]
    return norm


def _tv_matches(
    want_title: str,
    want_season: int,
    want_root: str,
    cand_title: str,
    cand_season: object,
    cand_root: str,
    smb: str,
    nfs: str,
) -> bool:
    try:
        c_season = int(str(cand_season).strip())
    except (TypeError, ValueError):
        return False
    if c_season != int(want_season):
        return False
    if want_title and cand_title and _compact(want_title) == _compact(cand_title):
        return True
    if want_root and cand_root:
        if _norm_media_root(want_root, smb, nfs) == _norm_media_root(cand_root, smb, nfs):
            return True
    return False


def _selected_count_from_log(log_path: str | Path) -> int:
    try:
        txt = Path(log_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    txt = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", txt)
    matches = re.findall(r"Selected\s+(\d+)\s+title\(s\):", txt)
    if not matches:
        return 0
    try:
        return int(matches[-1])
    except ValueError:
        return 0


def _discover_active_ripqueue_procs() -> list[dict]:
    try:
        r = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if r.returncode != 0:
        return []
    by_pid: dict[int, tuple[int, str]] = {}
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        by_pid[pid] = (ppid, parts[2])

    out: list[dict] = []
    for pid, (ppid, cmdline) in by_pid.items():
        if not re.search(r"python.*(?:ripqueue\.py|burndvd-queue)", cmdline, re.IGNORECASE):
            continue
        m_state = re.search(r"--state\s+([^\s]+)", cmdline)
        m_queue = re.search(r"--queue\s+([^\s]+)", cmdline)
        log_path = ""
        cur_ppid = ppid
        for _ in range(3):
            parent = by_pid.get(cur_ppid)
            if not parent:
                break
            p_ppid, p_cmd = parent
            m_log = re.search(r"\bscript\s+-q\s+([^\s]+\.log)\b", p_cmd)
            if m_log:
                log_path = m_log.group(1)
                break
            cur_ppid = p_ppid
        out.append(
            {
                "pid": pid,
                "state_file": m_state.group(1).strip(chr(39) + chr(34)) if m_state else "",
                "queue_file": m_queue.group(1).strip(chr(39) + chr(34)) if m_queue else "",
                "log_file": log_path,
            }
        )
    return out


def compute_next_tv_episode(
    *,
    title: str,
    season: int,
    target_root: str = "",
    season_dir: Path | str | None = None,
    slots_dir: Path | str | None = None,
    media_smb_root: str = "/Volumes/Media",
    media_nfs_root: str = "/private/nas/media",
    active_processes: list[dict] | None = None,
    wait_scan_s: float = 0.0,
    poll_interval_s: float = 2.0,
) -> dict:
    deadline = time.monotonic() + max(0.0, float(wait_scan_s))
    notified_wait = False
    while True:
        nas_highest = scan_season_highest_episode(season_dir, int(season))
        next_ep = nas_highest + 1
        reservations: list[dict] = []
        seen_pids: set[int] = set()
        pending_scan_pids: list[int] = []

        sdir = active_tv_slots_dir(slots_dir)
        if sdir.is_dir():
            for path in sorted(sdir.glob("pid-*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                try:
                    pid = int(data.get("pid", 0))
                    ep_start = int(data.get("episode_start", 0))
                    ep_count = int(data.get("episode_count", 0))
                except (TypeError, ValueError):
                    continue
                if not _pid_alive(pid):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                if pid == os.getpid():
                    continue
                if _tv_matches(
                    title,
                    int(season),
                    target_root,
                    str(data.get("title") or ""),
                    data.get("season"),
                    str(data.get("target_root") or ""),
                    media_smb_root,
                    media_nfs_root,
                ):
                    seen_pids.add(pid)
                    if ep_start > 0 and ep_count > 0:
                        end_ep = ep_start + ep_count - 1
                        reservations.append(
                            {
                                "pid": pid,
                                "episode_start": ep_start,
                                "episode_count": ep_count,
                                "episode_end": end_ep,
                                "source": "slot",
                            }
                        )
                        next_ep = max(next_ep, ep_start + ep_count)

        procs = (
            _discover_active_ripqueue_procs()
            if active_processes is None
            else list(active_processes)
        )
        for proc in procs:
            try:
                pid = int(proc.get("pid", 0))
            except (TypeError, ValueError):
                continue
            if pid <= 0 or pid == os.getpid() or pid in seen_pids:
                continue
            rows: list[dict] = []
            sp = str(proc.get("state_file") or "")
            qp = str(proc.get("queue_file") or "")
            if sp and os.path.isfile(sp):
                try:
                    st = json.loads(Path(sp).read_text(encoding="utf-8"))
                    rows.extend(st.get("queue") or [])
                except (OSError, ValueError):
                    pass
            if not rows and qp and os.path.isfile(qp):
                try:
                    with open(qp, encoding="utf-8", newline="") as fh:
                        rows.extend(csv.DictReader(fh))
                except OSError:
                    pass
            matched_row = None
            for q in rows:
                if _tv_matches(
                    title,
                    int(season),
                    target_root,
                    str(q.get("title") or ""),
                    q.get("season"),
                    str(q.get("target_root") or ""),
                    media_smb_root,
                    media_nfs_root,
                ):
                    matched_row = q
                    break
            if matched_row is None:
                continue
            seen_pids.add(pid)
            raw_start = str(matched_row.get("episode_start") or "").strip()
            ep_start = int(raw_start) if raw_start.isdigit() and int(raw_start) > 0 else (nas_highest + 1)
            raw_cnt = str(matched_row.get("expected_disc_episodes") or "").strip()
            ep_count = int(raw_cnt) if raw_cnt.isdigit() and int(raw_cnt) > 0 else 0
            source = "queue"
            if ep_count <= 0:
                log_file = str(proc.get("log_file") or "")
                if log_file:
                    ep_count = _selected_count_from_log(log_file)
                    if ep_count > 0:
                        source = "log"
            if ep_count > 0:
                end_ep = ep_start + ep_count - 1
                reservations.append(
                    {
                        "pid": pid,
                        "episode_start": ep_start,
                        "episode_count": ep_count,
                        "episode_end": end_ep,
                        "source": source,
                    }
                )
                next_ep = max(next_ep, ep_start + ep_count)
            else:
                pending_scan_pids.append(pid)

        result = {
            "next_episode": next_ep,
            "nas_highest": nas_highest,
            "reservations": reservations,
            "pending_scan_pids": pending_scan_pids,
        }
        if not pending_scan_pids or time.monotonic() >= deadline:
            return result
        if not notified_wait:
            notified_wait = True
            import sys as _sys
            pids_s = ", ".join(str(p) for p in pending_scan_pids)
            print(
                f"Waiting up to {int(wait_scan_s)}s for active rip (pid {pids_s}) to finish scanning disc...",
                file=_sys.stderr,
                flush=True,
            )
        time.sleep(min(poll_interval_s, max(0.05, deadline - time.monotonic())))
