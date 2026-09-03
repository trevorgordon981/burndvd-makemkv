# burndvd-makemkv

Smart-defaults wrapper around MakeMKV for ripping discs to a Plex/Jellyfin
library on macOS. Run one command per disc, accept the detected metadata, and
let the detached worker rip, verify, publish, and eject.

```text
$ burndvd
Detected disc:
  device:  /dev/disk4
  volume:  GAMEOFTHRONES_S4_DISC4
  size:    ~45 GiB
  format:  BD (auto)

Title (include year): [Game of Thrones]
Type [tv-season]:
Format [BD]:
Season number [4]:
Episode contract: disc 4 -> S04E09-E10 (10 total).
```

## Safety model

TV publication is contract-driven. It does not infer the next episode by
scanning for the highest existing filename.

- A TV queue row binds the show, season, physical-disc number, season total,
  episode range, and MakeMKV title IDs.
- The backend rechecks the observed disc label and content fingerprint before
  reading or publishing it.
- A recognized physical disc whose contracted slots are vacant publishes to
  `SxxEyy`. If those slots are already occupied, plain `burndvd` automatically
  captures the disc to a fingerprint-scoped review directory outside the
  Jellyfin library. Existing episodes are never replaced.
- An unrecognized/manual contract does not get that automatic privilege. If
  its slots are occupied, it fails closed; use explicit `--rerip-review` only
  after checking the contract.
- Inventory beyond the authoritative season total is always a hard stop.
- `--overwrite` is forbidden for TV in normal and review modes. It remains
  available for movies.

Review captures use traceable title names rather than `SxxEyy` names and do
not create publication receipts. The default review root for the standard
library is:

```text
/Volumes/Media/.repair-quarantine/burndvd-review/<Show>/Season NN/<fingerprint>/
```

Use `--review-root /absolute/out-of-library/path` for another layout. Relative
paths, paths inside any `TV Shows`/`TV Shows 4K` tree, and symlink escapes are
rejected. Incomplete TV salvage and transfer partials are kept outside the
media library as well.

### Atomic TV publication

Completed TV files are copied to a same-filesystem quarantine path, flushed,
then published with an atomic no-clobber operation. Hidden per-episode claims
reserve the contracted range without creating zero-byte media files. A
collision preserves the staged rip and the pre-existing destination.

On the standard macOS layout, `/Volumes/Media` is the SMB view used by the
library and `/private/nas/media` is the equivalent NFS view used for TV
publication. The backend switches to NFS because that mount supports the
same-filesystem hard-link fallback needed when SMB lacks atomic no-replace.
If the NFS view is unavailable or unwritable, publication fails closed and
staging is retained.

Season coordination is host-local under `~/.cache/burndvd/locks`; equivalent
SMB and NFS season paths map to the same lock key. Therefore every writer for
a season must run through `burndvd` on the same host. This design does not
claim multi-host locking.

## Built-in TV contracts

The current registry contains the audited four-disc layouts for **Game of
Thrones seasons 1–6**, ten episodes per season. Contracts include exact title
IDs, including non-contiguous selections where a disc contains bonus material
(for example Season 4 Disc 1 uses titles 1 and 3).

Other shows can be entered with a complete manual contract. Automatic review
switching is reserved for registry-recognized label/title/season/disc tuples.

## Verification and receipts

The smart wrapper enables `--verify` by default. A successful verified normal
publication records the disc content fingerprint and contract in:

```text
~/.local/state/burndvd/disc-receipts.jsonl
```

A later attempt to publish that same physical-disc fingerprint is refused.
Use `--disc-receipts` to select another ledger. Corrupt or unreadable ledgers
fail closed. Review captures never masquerade as published discs.

## What else it does

- Detects the inserted disc, drive, format, and best-guess title/season/disc.
- Requires canonical movie titles ending in `(YYYY)` and normalizes known
  compact Fast-franchise labels.
- Uses Plex/Jellyfin movie and TV naming.
- Rips to local staging, then transfers complete files to the NAS.
- Runs detached so closing the terminal or pressing Ctrl-C on the log tail does
  not kill the rip.
- Supports multiple optical drives and refuses a drive already claimed by
  another worker.
- Auto-ejects after a successful queue.
- Optionally runs `subocr` to convert PGS subtitles to `.srt` sidecars.

## Requirements

- macOS (`drutil`, `diskutil`, and `caffeinate` are used).
- [MakeMKV](https://www.makemkv.com/) at
  `/Applications/MakeMKV.app/Contents/MacOS/makemkvcon`, or pass
  `--makemkvcon`.
- Python 3.10+. `ripqueue.py` uses only the standard library; `subocr.py`
  requires PyObjC.
- An optical drive. The project has been exercised with the LG BU40N.
- For the standard NAS layout, equivalent writable SMB `/Volumes/Media` and
  NFS `/private/nas/media` mounts.

## Install

```sh
git clone https://github.com/trevorgordon981/burndvd-makemkv.git ~/burndvd-makemkv
ln -s ~/burndvd-makemkv/bin/burndvd   ~/.local/bin/burndvd
ln -s ~/burndvd-makemkv/bin/ejectdisc ~/.local/bin/ejectdisc
ln -s ~/burndvd-makemkv/bin/subocr    ~/.local/bin/subocr  # optional
```

Put `~/.local/bin` on `PATH`.

For subtitle OCR:

```sh
python3 -m venv ~/.local/share/subocr/venv
~/.local/share/subocr/venv/bin/pip install pyobjc
```

Per-show OCR overrides go in `~/.config/subocr/shows.json`; see
`examples/subocr-shows.json`.

## Single-disc mode

Run `burndvd`. It detects the disc and prompts for metadata. Recognized TV
labels receive a built-in immutable contract; unknown TV labels require these
values explicitly:

- authoritative season episode count;
- first episode on this physical disc;
- number of episodes on the disc;
- episode-bearing MakeMKV title IDs as JSON; and
- physical-disc number.

Prompt input may come from a terminal or a pipe. Headless EOF uses a safe
default where one exists and fails explicitly for required values.

Use `burndvd --rerip-review` to force a TV read into protected review output.
This never publishes `SxxEyy` files.

Failure notifications are optional. Set both a destination and token:

```sh
export BURNDVD_SLACK_CHANNEL='<channel-id>'
export BURNDVD_SLACK_BOT_TOKEN='<bot-token>'
```

Alternatively, set `BURNDVD_SLACK_ENV` to an env file containing
`SLACK_BOT_TOKEN`. With no channel configured, notification is a no-op.

## Multi-disc / queue mode

Drive `ripqueue.py` directly with a CSV:

```csv
title,type,season,episode_start,expected_episodes,expected_disc_episodes,expected_title_ids,expected_physical_disc,discs,target_root,format,notes,features
Game of Thrones,tv-season,4,3,10,3,"[1,2,3]",2,1,/Volumes/Media/TV Shows/Game of Thrones,BD,,
Mean Streets (1973),movie,,,,,,,1,/Volumes/Media/Movies/Mean Streets (1973),BD,,
```

```sh
caffeinate -dimsu bin/ripqueue.py --queue queue.csv \
  --state ~/.local/state/burndvd/ripqueue-state.json --verify
```

The state file persists queue progress. An empty state file is treated as a
new run; malformed non-empty JSON is refused. Use the hardware-free validator
before a run:

```sh
bin/ripqueue.py --validate-queue queue.csv
```

Useful flags:

- `--verify` — decode-check output before recording a publication receipt.
- `--rerip-review` — force TV output to protected review storage.
- `--review-root PATH` — choose an absolute out-of-library review root.
- `--disc-receipts PATH` — choose the durable fingerprint ledger.
- `--min-free-gb 200` — refuse a rip when staging/target space is low.
- `--device disc:1` — select a drive.
- `--sync-move` — finish NAS publication before freeing the drive.
- `--no-eject`, `--no-sound`, `--no-subocr` — disable optional behavior.
- `--overwrite` — movie-only; rejected whenever a TV row is present.

## Layout

```text
bin/
  burndvd              smart single-disc wrapper
  burndvd_metadata.py  hardware-free inference, contracts, queue writer
  ripqueue.py          queue worker and protected publisher
  ejectdisc            small drutil wrapper
  subocr               subocr launcher
  subocr.py             Apple Vision OCR for PGS subtitles
examples/
  subocr-shows.json
tests/
  test_fast_rip_pipeline.py
  test_burndvd_safety.py
```

## Common pitfalls

- macOS may auto-mount DVDs and hold the optical drive. `burndvd` unmounts the
  volume without ejecting before MakeMKV starts.
- Do not bypass local staging or write in-progress media directly to SMB.
- Do not run independent multi-host writers against the same season; the
  coordination lock is intentionally host-local.
- A failed transfer leaves staging in place. Fix the mount problem and inspect
  the logged paths instead of deleting or retrying blindly.

## License

MIT — see `LICENSE`.
