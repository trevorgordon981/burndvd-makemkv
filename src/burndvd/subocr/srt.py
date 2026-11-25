"""Atomic SubRip (`.srt`) writer with fsync + rename."""

from __future__ import annotations

import os
from pathlib import Path


def _ms_to_srt_ts(ms: int) -> str:
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[tuple[int, int, str]], path: Path) -> None:
    """Atomically write SRT cues via sibling `.part.<pid>` file + `fsync` + `os.replace`."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.part.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            idx = 1
            for start, end, text in cues:
                text = text.strip()
                if not text:
                    continue
                f.write(
                    f"{idx}\n{_ms_to_srt_ts(start)} --> {_ms_to_srt_ts(end)}\n{text}\n\n"
                )
                idx += 1
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
