# Sadpanda Downloader

Sadpanda Downloader is a local ExHentai favorites manager with a Streamlit UI and a background worker.

It can:

- scrape your favorites list
- store gallery metadata in SQLite
- queue torrent downloads to qBittorrent
- download archives directly
- import finished files into a media library with hardlinks
- track missing files and operational errors

This README is intentionally focused on basic local setup.

## What runs

- `app.py`: the Streamlit web UI
- `worker.py`: the background sync/download loop
- `run_all.py`: starts both together

## Requirements

- Python 3.12 recommended
- qBittorrent with the Web UI enabled
- ExHentai cookies for authenticated requests

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Quick start

1. Copy `config.example.json` to `config.json`.
2. Fill in your ExHentai cookies, qBittorrent connection info, and folder paths.
3. Start the app:

```bash
python run_all.py
```

That opens the Streamlit UI and starts the worker in the background.

If you prefer to run them separately:

```bash
python worker.py
python -m streamlit run app.py
```

## Basic config

The app expects a `config.json` file in the project root.

Important fields:

- `exhentai.cookies`: your authenticated ExHentai cookie string
- `qbittorrent.host`: qBittorrent Web UI base URL, for example `http://localhost`
- `qbittorrent.port`: qBittorrent Web UI port, usually `8080`
- `qbittorrent.username`
- `qbittorrent.password`
- `qbittorrent.download_path`: where qBittorrent should save Sadpanda torrents
- `paths.media_library`: where imported readable files should live
- `paths.archive_library`: where direct archive downloads should live

## Folder structure

Typical layout:

```text
project/
  app.py
  worker.py
  run_all.py
  config.json
  dashboard.db
  thumbnails/
  Download_folders/
    Sadpanda_Media/
    Sadpanda_Archives/
```

What each path is for:

- `dashboard.db`: local application state
- `thumbnails/`: cached gallery thumbnails
- `Sadpanda_Media/`: imported library files used for reading
- `Sadpanda_Archives/`: direct archive downloads
- `qbittorrent.download_path`: torrent payload location used by qBittorrent

`Sadpanda_Media` and the qBittorrent download path should be on the same drive if you want hardlink imports to work efficiently.

## First use flow

1. Start the app with `python run_all.py`.
2. Open the Streamlit page shown in the terminal.
3. Confirm the config in the sidebar.
4. Let the worker scrape favorites and populate the database.
5. Use the table filters or rules to queue downloads.

## Notes

- `config.json`, `dashboard.db`, `thumbnails/`, and download folders are local runtime data and should not be committed.
- The app currently assumes qBittorrent as the active torrent backend.
- Advanced features such as archive-to-torrent upload exist in the codebase but are not covered in this quick-start guide.
- Docker-related notes are in `README-deploy.md`.

## Troubleshooting

If the UI starts but nothing syncs:

- check that `config.json` exists
- check that your ExHentai cookies are valid
- check that qBittorrent Web UI is reachable
- check that your configured folders exist and are writable

If qBittorrent downloads finish but files do not appear in the media library:

- verify `qbittorrent.download_path` is correct
- verify `paths.media_library` is correct
- verify both locations are on the same filesystem if you expect hardlinks
