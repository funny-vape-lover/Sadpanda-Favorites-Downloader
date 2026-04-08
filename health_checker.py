from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import database
import download_manager
import external_manager
import media_importer


def _now() -> int:
    return int(time.time())


def _safe_title(item: dict[str, Any]) -> str:
    return str(item.get("title", "") or item.get("id", ""))


def check_missing_torrents(
    db_path: str | Path,
    adapter,
) -> list[dict[str, Any]]:
    """Detect items whose torrents disappeared from the client."""
    findings: list[dict[str, Any]] = []
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                local_status,
                torrent_downloaded
            FROM items
            WHERE COALESCE(is_latest_revision, 1) = 1
              AND torrent_downloaded = 1
            """
        ).fetchall()

    for row in rows:
        item = dict(row)
        candidate = download_manager.select_best_candidate(db_path, item["id"])
        if candidate is None:
            continue
        try:
            status = adapter.get_status(candidate["hash_string"]) or {}
        except Exception as exc:  # noqa: BLE001
            database.record_error(
                item_id=item["id"],
                error_type="torrent_status_failed",
                message=f"qBittorrent status check failed: {exc}",
                fix_hint="Verify qBittorrent connectivity.",
                db_path=db_path,
            )
            continue

        if not bool(status.get("found")):
            database.record_error(
                item_id=item["id"],
                error_type="missing_torrent",
                message=f"Torrent {candidate['hash_string']} missing from client.",
                fix_hint="Re-enqueue torrent to qBittorrent.",
                db_path=db_path,
            )
            database.update_item_status(item["id"], "error", db_path=db_path, error_message="Torrent missing from client")
            findings.append({"item_id": item["id"], "type": "missing_torrent"})
            continue

        database.resolve_error(item_id=item["id"], error_type="missing_torrent", db_path=db_path)
        database.resolve_error(item_id=item["id"], error_type="torrent_status_failed", db_path=db_path)
    return findings


def check_missing_archives(db_path: str | Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect missing archive files for items marked archive_downloaded."""
    findings: list[dict[str, Any]] = []
    archive_library = Path(external_manager._resolve_external_save_dir(config)).expanduser()  # type: ignore[attr-defined]
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, title, archive_path
            FROM items
            WHERE archive_downloaded = 1
              AND COALESCE(is_latest_revision, 1) = 1
            """
        ).fetchall()
    for row in rows:
        item = dict(row)
        stored_path = str(item.get("archive_path") or "")
        path = Path(stored_path) if stored_path else None
        if path and not path.is_absolute():
            path = archive_library / path

        missing = path is None or not path.exists() or external_manager._looks_like_html_file(path)  # type: ignore[attr-defined]
        if missing:
            fallback = external_manager._find_existing_archive(archive_library, item)  # type: ignore[attr-defined]
            if fallback and not external_manager._looks_like_html_file(fallback):  # type: ignore[attr-defined]
                missing = False
                if str(item.get("archive_path") or "") != str(fallback):
                    database.update_item_archive_path(item["id"], str(fallback), db_path=db_path)  # type: ignore[attr-defined]
                database.update_item_download_flags(item["id"], archive_downloaded=True, db_path=db_path)

        if missing:
            database.record_error(
                item_id=item["id"],
                error_type="missing_archive",
                message=f"Archive file missing at {stored_path or archive_library}",
                fix_hint="Re-download archive or re-link existing file.",
                db_path=db_path,
            )
            database.update_item_status(item["id"], "error", db_path=db_path, error_message="Archive missing on disk")
            findings.append({"item_id": item["id"], "type": "missing_archive"})
            continue

        database.resolve_error(item_id=item["id"], error_type="missing_archive", db_path=db_path)
    return findings


def check_missing_media(db_path: str | Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect missing media library files for completed items."""
    findings: list[dict[str, Any]] = []
    media_library = media_importer._resolve_media_library(config)  # type: ignore[attr-defined]
    media_library.mkdir(parents=True, exist_ok=True)
    media_entries = media_importer._list_media_library_entries(media_library)  # type: ignore[attr-defined]
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, title, local_status
            FROM items
            WHERE local_status IN ('completed', 'seeding', 'complete')
              AND COALESCE(is_latest_revision, 1) = 1
            """
        ).fetchall()
    for row in rows:
        item = dict(row)
        try:
            _ = media_importer._find_media_library_file(  # type: ignore[attr-defined]
                media_library,
                item["id"],
                item["title"],
                media_entries=media_entries,
            )
        except FileNotFoundError:
            database.record_error(
                item_id=item["id"],
                error_type="missing_media",
                message=f"Media library file missing for {_safe_title(item)}",
                fix_hint="Re-import from archive or torrent source.",
                db_path=db_path,
            )
            database.update_item_status(item["id"], "error", db_path=db_path, error_message="Media file missing")
            findings.append({"item_id": item["id"], "type": "missing_media"})
        except ValueError as exc:
            database.record_error(
                item_id=item["id"],
                error_type="ambiguous_media",
                message=str(exc),
                fix_hint="Manually resolve duplicate media files.",
                db_path=db_path,
            )
            findings.append({"item_id": item["id"], "type": "ambiguous_media"})
        else:
            database.resolve_error(item_id=item["id"], error_type="missing_media", db_path=db_path)
            database.resolve_error(item_id=item["id"], error_type="ambiguous_media", db_path=db_path)
    return findings


def check_missing_thumbnails(db_path: str | Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, title, thumbnail_local_path
            FROM items
            WHERE COALESCE(thumbnail_local_path, '') != ''
              AND COALESCE(is_latest_revision, 1) = 1
            """
        ).fetchall()
    for row in rows:
        item = dict(row)
        thumb_path = Path(database.APP_DIR / str(item.get("thumbnail_local_path")))
        if not thumb_path.exists():
            database.record_error(
                item_id=item["id"],
                error_type="missing_thumbnail",
                message=f"Thumbnail missing at {thumb_path}",
                fix_hint="Re-download metadata to fetch thumbnails.",
                db_path=db_path,
            )
            findings.append({"item_id": item["id"], "type": "missing_thumbnail"})
            continue

        database.resolve_error(item_id=item["id"], error_type="missing_thumbnail", db_path=db_path)
    return findings


def run_health_checks(
    db_path: str | Path,
    config: dict[str, Any] | None,
    adapter,
) -> list[dict[str, Any]]:
    """Run all health checks; return list of findings."""
    if not config:
        return []
    findings: list[dict[str, Any]] = []
    if adapter is not None:
        findings.extend(check_missing_torrents(db_path, adapter))
    findings.extend(check_missing_archives(db_path, config))
    findings.extend(check_missing_media(db_path, config))
    findings.extend(check_missing_thumbnails(db_path))
    return findings
