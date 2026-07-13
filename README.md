# FeedMeSomePi

A tiny RSS aggregator built for Raspberry Pi Zero and up, running 32-bit
Raspberry Pi OS (Bullseye / Bookworm Lite or Desktop). Runs happily on a
Pi Zero W with 512 MB RAM — Tkinter for the UI, `feedparser` for parsing,
and a JSON file on disk for feed storage.

## Features

- Add feeds by pasting a URL — no OPML importer, no cloud sync, no telemetry.
- Refresh feeds manually or on a background timer.
- Article list with title, source, published-at, and an inline preview pane.
- Image thumbnails are downscaled and compressed to keep RAM use flat.
- Notification chime is a tiny bundled WAV (~4 KB) played via `aplay`.
- Reader-mode preview strips scripts and inline styles for a clean read.
- Feed store lives in `~/.feedmesomepi/feeds.json`; delete it to reset.

## Install (on the Pi)

```sh
git clone https://github.com/eagnespuerto/FeedMeSomePi.git
cd FeedMeSomePi
./setup.sh
```

`setup.sh` installs the two system packages the app needs
(`python3-tk`, `python3-pil.imagetk`), sets up a virtualenv, installs
`feedparser` + `requests`, and drops a desktop entry into
`~/.local/share/applications/` so **FeedMeSomePi** shows up under
**Internet** in the PiOS menu. On a Pi Zero the whole thing takes a
couple of minutes.

## Run

Click **Internet → FeedMeSomePi** in the PiOS menu, or from a terminal:

```sh
./run.sh
```

Or, if you prefer to activate the venv yourself:

```sh
. .venv/bin/activate
python3 rss_aggregator.py
```

## Adding a feed

Paste a feed URL into the box at the top and hit **Add**. If the URL is a
site homepage rather than a feed, the app tries the common autodiscovery
paths (`/feed`, `/rss`, `/atom.xml`, and `<link rel="alternate">` tags in
the HTML) before giving up.

## Uninstall the menu entry

```sh
./uninstall.sh
```

Leaves the venv, saved feeds, and article cache in place. Delete the
repo directory and `~/.feedmesomepi/` for a full wipe.

## Why the constraints

Pi Zero (single-core ARM11, 512 MB) sets the ceiling. Everything in this
repo is chosen to fit under it:

- **Tkinter** ships with Raspberry Pi OS — no Qt/GTK download, no Electron.
- **JSON storage** — no SQLite dependency; the feed list is tiny.
- **Text-only article cache** — parsed article metadata (title, summary
  text, link, published, seen) is persisted to
  `~/.feedmesomepi/articles.json` so the app opens with content instantly
  and background refresh is delta-only. Audio enclosures and full-size
  images are never stored; thumbnails live separately as small JPEGs.
- **Compressed assets** — thumbnails are resized to 96×96 with PIL and
  cached to disk; the notify sound is a mono 8-bit WAV.
- **Background refresh** runs on a single worker thread with a cap on
  concurrent requests so we don't thrash the CPU. `ETag` /
  `If-Modified-Since` conditional GETs avoid re-downloading unchanged
  feeds altogether.

## License

MIT — see [LICENSE](LICENSE).
