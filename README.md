# burndvd-makemkv

Smart-defaults wrapper around MakeMKV for ripping discs to a Plex/Jellyfin
library on macOS. One command per disc, Enter through the prompts, walks
away while it rips. Multi-disc CSV mode for batch jobs.

```
$ burndvd
Detected disc:
  device:  /dev/disk6
  volume:  PARKS_AND_RECREATION_S6_D3
  size:    ~23 GiB
  format:  BD (auto)

Title (include year): [Parks And Recreation]
Type [tv-season]:
Format [BD]:
Season number [6]:
Episode start [18]:                  # auto-detected from existing files

Queue: /var/folders/.../burndvd-queue.XXXX.csv
Target: /Volumes/Media/TV Shows/Parks and Recreation
Log:    ~/logs/burndvd-parks-and-recreation-s6-d3-20260525T164353.log

Rip launched detached (pid 56857). Tailing log;
When the rip finishes, the disc auto-ejects and the tail exits.
```

## What it does

- **Auto-detect** the inserted disc, drive, format (DVD/BD/4K), and best-guess
  title/season/disc-number from the volume label.
- **Canonical movie folders**: movie titles must end in `(YYYY)`. Known compact
  Fast-franchise labels such as `2F2F` and `FFTokyoDriftUHDPK75` expand to their
  official title, punctuation, and release year.
- **Auto-next-episode** prompt: scans the season folder, defaults to
  highest-existing + 1. Press Enter to accept.
- **Plex/Jellyfin naming**: writes `Title - SxxEyy.mkv` under
  `/Volumes/Media/TV Shows/...` (configurable per format).
- **Detached rip**: closes terminal-safe. Ripping continues after Ctrl-C or
  shell exit; reattach with `tail -f <log>`.
- **Local staging**: in-flight files land on the local disk, then atomic-move
  to the library. Avoids fragmentation and scanner races over SMB.
- **Multi-drive aware**: if more than one optical drive is connected, picks
  the first one with media that isn't already being ripped.
- **Auto-eject** when the queue completes (uses `ejectdisc`).
- **Optional**: post-rip subtitle OCR (`subocr`) turns HDMV PGS bitmap subs
  into `.srt` sidecars so web Jellyfin clients can render them.

## Requirements

- macOS (uses `drutil`, `diskutil`, `caffeinate`, Apple Vision for OCR).
- [MakeMKV](https://www.makemkv.com/) at the default path
  `/Applications/MakeMKV.app/Contents/MacOS/makemkvcon`, or override with
  `--makemkvcon`.
- Python 3.10+ (stdlib only for `ripqueue.py`; `subocr.py` needs PyObjC).
- An optical drive. Tested against the LG BU40N (USB-C external BD/UHD).

## Install

```sh
git clone https://github.com/trevorgordon981/burndvd-makemkv.git ~/burndvd-makemkv
ln -s ~/burndvd-makemkv/bin/burndvd    ~/.local/bin/burndvd
ln -s ~/burndvd-makemkv/bin/ejectdisc  ~/.local/bin/ejectdisc
ln -s ~/burndvd-makemkv/bin/subocr     ~/.local/bin/subocr   # optional
```

Make sure `~/.local/bin` is on your `PATH`.

### Subtitle OCR (optional)

`subocr` needs PyObjC (Apple Vision bindings):

```sh
python3 -m venv ~/.local/share/subocr/venv
~/.local/share/subocr/venv/bin/pip install pyobjc
```

Or point `SUBOCR_VENV` at an existing venv. Per-show overrides
(language, filters) go in `~/.config/subocr/shows.json`; see
`examples/subocr-shows.json`.

## Single-disc mode

Just run `burndvd`. It detects the disc and asks 3-4 questions, each with
a sensible default. Episode-start defaults to the next slot after whatever
already exists in the season folder, so back-to-back disc rips don't need
manual counting.

Prompts accept terminal or piped input. If headless stdin ends, a prompt uses
its safe default; required values without a default fail explicitly. Unknown
movie titles never fall through without a release year.

## Multi-disc / queue mode

Drive `ripqueue.py` directly with a CSV:

```csv
title,type,season,episode_start,discs,target_root,format,notes
Parks and Recreation,tv-season,6,1,3,/Volumes/Media/TV Shows/Parks and Recreation,BD,
The Bear,tv-season,2,1,2,/Volumes/Media/TV Shows/The Bear,BD,
Mean Streets (1973),movie,,,1,/Volumes/Media/Movies/Mean Streets (1973),BD,
```

```sh
caffeinate -dimsu ripqueue.py --queue queue.csv --state ~/ripqueue-state.json
```

The state file persists progress across runs, so resuming after a crash
or a shell exit picks up where it left off. An empty state file created by
`mktemp` is treated as a new run; malformed non-empty JSON is refused so real
resume data is never silently overwritten.

Useful flags:

- `--overwrite` — clobber existing `.mkv` files instead of refusing.
- `--verify` — re-check titles after the rip (slow; doubles disc time).
- `--min-free-gb 200` — bail before a rip if the target volume is low.
- `--device disc:1` — force a specific drive.
- `--no-eject` / `--no-sound` — quieter operation.
- `--validate-queue queue.csv` — CPU-only CSV/embedded-JSON validation; exits
  before drive discovery or any MakeMKV operation.

## Layout

```
bin/
  burndvd        smart single-disc wrapper (bash)
  burndvd_metadata.py  hardware-free title inference + queue writer
  ripqueue.py    queue worker (python, stdlib only)
  ejectdisc      tiny drutil wrapper
  subocr         bash wrapper around subocr.py (uses pyobjc venv)
  subocr.py      Apple Vision OCR for PGS subs (python + pyobjc)
examples/
  subocr-shows.json   per-show config example
tests/
  test_fast_rip_pipeline.py  prompt, metadata, CSV/JSON, state + dry-run tests
```

## Pitfalls

- **macOS auto-mounts DVDs** at `/Volumes/<label>`, which holds the optical
  drive exclusively and locks MakeMKV out. `burndvd` unmounts (without
  ejecting) before launching the rip; BD/UHD discs aren't affected because
  macOS can't mount their filesystems natively.
- **Closing the terminal**: the rip is launched detached (`nohup` + `sleep |
  script`), so a SIGHUP to your shell won't kill the rip. Reattach via the
  log file printed at launch.
- **NAS direct writes**: don't rip straight to SMB. The staging dir lives
  under `~/.cache/burndvd/staging` and only the finished `.mkv` is moved
  atomically into the library.

## License

MIT — see `LICENSE`.
