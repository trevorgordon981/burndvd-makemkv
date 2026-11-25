"""Durable JSONL physical-disc receipt ledger & duplicate-disc publication guard."""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

from burndvd.config import DISC_RECEIPT_DEFAULT
from burndvd.models import QueueItem


def disc_receipt_seen(path: Path, fingerprint: str) -> bool:
    """Return True if `fingerprint` already has a `published` receipt in `path`.

    Fails closed (`RuntimeError`) if the ledger exists but is unreadable or
    contains malformed JSON.
    """
    if not path.exists():
        return False
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"corrupt disc receipt ledger {path}: line {line_number}"
                    ) from exc
                if (
                    row.get("fingerprint") == fingerprint
                    and row.get("status") == "published"
                ):
                    return True
    except OSError as exc:
        raise RuntimeError(f"cannot read disc receipt ledger: {path}") from exc
    return False


def append_disc_receipt(
    path: Path,
    item: QueueItem,
    disc_n: int,
    fingerprint: str,
    files: list[str],
) -> None:
    """Append and fsync a locked JSONL publication receipt for a verified disc."""
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "version": 1,
        "status": "published",
        "fingerprint": fingerprint,
        "title": item.title,
        "season": item.season,
        "disc": int(disc_n),
        "expected_physical_disc": item.expected_physical_disc,
        "episode_start": item.episode_start,
        "expected_disc_episodes": item.expected_disc_episodes,
        "expected_title_ids": list(item.expected_title_ids),
        "expected_episodes": item.expected_episodes,
        "files": list(files),
        "ts": time.time(),
    }
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


__all__ = [
    "DISC_RECEIPT_DEFAULT",
    "disc_receipt_seen",
    "append_disc_receipt",
]
