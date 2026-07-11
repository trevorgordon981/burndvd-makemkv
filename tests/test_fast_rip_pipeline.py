from __future__ import annotations

import csv
import importlib.util
import json
import os
import pty
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BURNDVD = Path(os.environ.get("BURNDVD_TEST_WRAPPER", ROOT / "bin" / "burndvd"))
METADATA_PATH = Path(
    os.environ.get("BURNDVD_TEST_METADATA", ROOT / "bin" / "burndvd_metadata.py")
)
RIPQUEUE_PATH = Path(
    os.environ.get("BURNDVD_TEST_RIPQUEUE", ROOT / "bin" / "ripqueue.py")
)


def import_file(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


metadata = import_file("burndvd_metadata_test", METADATA_PATH)
ripqueue = import_file("ripqueue_test", RIPQUEUE_PATH)


def prompt_command(default: str = "Default Title", required: int = 1) -> str:
    return (
        "BURNDVD_LIBRARY_ONLY=1; "
        f"source {shlex.quote(str(BURNDVD))}; "
        f"prompt_value RESULT 'Title: ' {shlex.quote(default)} {required}; "
        "printf 'RESULT=%s\\n' \"$RESULT\""
    )


def test_prompt_reads_interactive_tty_answer():
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["/bin/bash", "-c", prompt_command()],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    os.close(slave)
    os.write(master, b"Custom Title (2003)\n")
    stdout, stderr = proc.communicate(timeout=5)
    os.close(master)
    assert proc.returncode == 0, stderr.decode()
    assert b"RESULT=Custom Title (2003)" in stdout


def test_prompt_accepts_piped_answer_under_errexit():
    result = subprocess.run(
        ["/bin/bash", "-c", prompt_command()],
        input="2 Fast 2 Furious (2003)\n",
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "RESULT=2 Fast 2 Furious (2003)" in result.stdout


def test_prompt_headless_eof_uses_default_instead_of_aborting():
    with open(os.devnull, "r", encoding="utf-8") as devnull:
        result = subprocess.run(
            ["/bin/bash", "-c", prompt_command("Headless Default (2006)")],
            stdin=devnull,
            capture_output=True,
            text=True,
            timeout=5,
        )
    assert result.returncode == 0, result.stderr
    assert "RESULT=Headless Default (2006)" in result.stdout
    assert "stdin ended; using default" in result.stderr


def test_prompt_headless_required_value_without_default_fails_explicitly():
    with open(os.devnull, "r", encoding="utf-8") as devnull:
        result = subprocess.run(
            ["/bin/bash", "-c", prompt_command("", required=1)],
            stdin=devnull,
            capture_output=True,
            text=True,
            timeout=5,
        )
    assert result.returncode == 64
    assert "a value is required" in result.stderr


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("2F2F", "2 Fast 2 Furious (2003)"),
        ("2_FAST_2_FURIOUS", "2 Fast 2 Furious (2003)"),
        (
            "FFTokyoDriftUHDPK75",
            "The Fast and the Furious: Tokyo Drift (2006)",
        ),
        (
            "THE_FAST_AND_THE_FURIOUS_TOKYO_DRIFT_UHD",
            "The Fast and the Furious: Tokyo Drift (2006)",
        ),
    ],
)
def test_fast_volume_labels_get_canonical_title_and_year(label, expected):
    item_type, season, title = metadata.infer_defaults(label)
    assert (item_type, season, title) == ("movie", "", expected)
    assert metadata.movie_title_has_year(title)


def test_explicit_tokyo_title_preserves_colon_and_path():
    title = "The Fast and the Furious: Tokyo Drift (2006)"
    assert metadata.normalize_movie_title(title) == title
    item = ripqueue.QueueItem(
        title=title,
        type="movie",
        discs=1,
        target_root=f"/Volumes/Media/Movies 4K/{title}",
        format="4K",
    )
    assert str(ripqueue.compute_target_dir(item)) == item.target_root
    # The folder preserves the official colon; the filename keeps the existing
    # cross-platform sanitization policy and removes only the colon character.
    assert ripqueue.movie_filename(item, 1) == (
        "The Fast and the Furious Tokyo Drift (2006).mkv"
    )


@pytest.mark.parametrize(
    ("title", "media_format", "root"),
    [
        (
            "2 Fast 2 Furious (2003)",
            "BD",
            "/Volumes/Media/Movies/2 Fast 2 Furious (2003)",
        ),
        (
            "The Fast and the Furious: Tokyo Drift (2006)",
            "4K",
            "/Volumes/Media/Movies 4K/The Fast and the Furious: Tokyo Drift (2006)",
        ),
    ],
)
def test_queue_round_trip_fast_titles_and_csv_escaping(tmp_path, title, media_format, root):
    queue_path = tmp_path / "queue.csv"
    notes = 'disc label, remaster "special"\nsecond line'
    metadata.write_queue(
        queue_path,
        title=title,
        item_type="movie",
        season="",
        episode_start="",
        target_root=root,
        media_format=media_format,
        notes=notes,
    )
    items = ripqueue.load_queue(queue_path)
    assert len(items) == 1
    assert items[0].title == title
    assert items[0].target_root == root
    assert items[0].notes == notes


def test_embedded_features_json_survives_csv_commas_and_quotes(tmp_path):
    queue_path = tmp_path / "double.csv"
    features = [
        {"title_id": 0, "name": 'Fast, Loud & "Quoted" (2003)'},
        {
            "title_id": 3,
            "name": "The Fast and the Furious: Tokyo Drift (2006)",
        },
    ]
    metadata.write_queue(
        queue_path,
        title="Fast Double Feature (2006)",
        item_type="double-feature",
        season="",
        episode_start="",
        target_root="/Volumes/Media/Movies",
        media_format="BD",
        notes='comma, quote " and newline\nare all legal CSV',
        features_json=json.dumps(features),
    )
    item = ripqueue.load_queue(queue_path)[0]
    assert item.features == features


def test_manual_queue_without_optional_features_column_is_valid(tmp_path):
    queue_path = tmp_path / "manual.csv"
    queue_path.write_text(
        "title,type,season,episode_start,discs,target_root,format,notes\n"
        "2 Fast 2 Furious (2003),movie,,,1,"
        "/Volumes/Media/Movies/2 Fast 2 Furious (2003),BD,manual relaunch\n",
        encoding="utf-8",
    )
    item = ripqueue.load_queue(queue_path)[0]
    assert item.title == "2 Fast 2 Furious (2003)"
    assert item.features == []


def test_empty_mktemp_state_is_treated_as_new_state(tmp_path):
    queue_path = tmp_path / "queue.csv"
    metadata.write_queue(
        queue_path,
        title="2 Fast 2 Furious (2003)",
        item_type="movie",
        season="",
        episode_start="",
        target_root="/Volumes/Media/Movies/2 Fast 2 Furious (2003)",
        media_format="BD",
        notes="manual",
    )
    items = ripqueue.load_queue(queue_path)
    state_path = tmp_path / "state.json"
    state_path.touch()
    state = ripqueue.load_or_init(SimpleNamespace(state=str(state_path)), items)
    assert state["current_index"] == 0
    assert state["queue"][0]["title"] == "2 Fast 2 Furious (2003)"


def test_nonempty_corrupt_state_is_not_silently_overwritten(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text("{broken", encoding="utf-8")
    item = ripqueue.QueueItem(
        title="2 Fast 2 Furious (2003)",
        type="movie",
        discs=1,
        target_root="/Volumes/Media/Movies/2 Fast 2 Furious (2003)",
        format="BD",
    )
    with pytest.raises(ValueError, match="refusing to overwrite resume data"):
        ripqueue.load_or_init(SimpleNamespace(state=str(state_path)), [item])


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [["title", "type"], ["Movie (2000)", "movie"]],
            "missing required column",
        ),
        (
            [
                ["title", "title", "type", "discs", "target_root", "format"],
                [
                    "Movie (2000)",
                    "Duplicate title",
                    "movie",
                    "1",
                    "/Volumes/Media/Movies/Movie (2000)",
                    "BD",
                ],
            ],
            "duplicate column",
        ),
        (
            [
                [
                    "title",
                    "type",
                    "season",
                    "episode_start",
                    "discs",
                    "target_root",
                    "format",
                    "notes",
                    "features",
                ],
                [
                    "Broken (2000)",
                    "double-feature",
                    "",
                    "",
                    "1",
                    "/Volumes/Media/Movies",
                    "BD",
                    "",
                    "not-json",
                ],
            ],
            "Expecting value",
        ),
        (
            [
                ["title", "type", "discs", "target_root", "format"],
                ["Movie (2000)", "movie", "1", "relative/path", "BD"],
            ],
            "target_root must be an absolute path",
        ),
        (
            [
                ["title", "type", "discs", "target_root", "format"],
                ["Movie (2000)", "movie", "1"],
            ],
            "format must be one of",
        ),
    ],
)
def test_queue_validation_rejects_bad_rows(tmp_path, rows, message):
    queue_path = tmp_path / "invalid.csv"
    with queue_path.open("w", newline="", encoding="utf-8") as stream:
        csv.writer(stream).writerows(rows)
    with pytest.raises(ValueError, match=message):
        ripqueue.load_queue(queue_path)


def test_validate_queue_cli_is_hardware_free(tmp_path):
    queue_path = tmp_path / "queue.csv"
    metadata.write_queue(
        queue_path,
        title="2 Fast 2 Furious (2003)",
        item_type="movie",
        season="",
        episode_start="",
        target_root="/Volumes/Media/Movies/2 Fast 2 Furious (2003)",
        media_format="BD",
        notes="dry run",
    )
    result = subprocess.run(
        [sys.executable, str(RIPQUEUE_PATH), "--validate-queue", str(queue_path)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "Queue valid: 1 item(s)." in result.stdout
