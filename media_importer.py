from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Protocol

from database import (
    get_connection,
    get_item,
    update_archive_auto_queue,
    update_item_download_flags,
    update_media_primary_source,
    update_item_progress,
    update_item_status,
)
from download_manager import select_best_candidate


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]+')
WHITESPACE_RUN = re.compile(r"\s+")
ITEM_ID_GID_PATTERN = re.compile(r"^g-(\d+)$", re.IGNORECASE)
MEDIA_SOURCE_PATTERN = re.compile(r"^\[(torrent|archive)\]\s+", re.IGNORECASE)
MAX_NAME_BYTES = 240


class ImportAdapter(Protocol):
    def get_save_path(self, torrent_hash: str) -> str | None:
        ...


def import_completed_torrents(
    db_path: str | Path,
    config: dict[str, Any],
    download_adapter: ImportAdapter,
) -> list[dict[str, Any]]:
    media_library = _resolve_media_library(config)
    media_library.mkdir(parents=True, exist_ok=True)

    actions: list[dict[str, Any]] = []
    completed_items = _get_items_by_status(db_path, "complete")

    for item in completed_items:
        try:
            candidate = select_best_candidate(db_path, item["id"])
            if candidate is None:
                print(
                    f"Skipping import for {item['id']} ({_safe_text(item['title'])}): "
                    "no best torrent candidate."
                )
                continue

            save_path = download_adapter.get_save_path(candidate["hash_string"])
            if not save_path:
                print(
                    f"Skipping import for {item['id']} ({_safe_text(item['title'])}): "
                    "adapter returned no save path."
                )
                continue

            try:
                source_path = _resolve_source_path(Path(save_path))
            except FileNotFoundError as exc:
                print(
                    f"Skipping import for {item['id']} ({_safe_text(item['title'])}): {exc}"
                )
                continue

            if source_path.is_file():
                destination_path = media_library / _build_media_filename(
                    item_id=item["id"],
                    title=item["title"],
                    suffix=source_path.suffix,
                    source="torrent",
                )
                import_result = _import_single_file(source_path, destination_path)
            else:
                destination_path = media_library / _build_media_dirname(
                    item_id=item["id"],
                    title=item["title"],
                    source="torrent",
                )
                import_result = _import_directory_tree(source_path, destination_path)

            update_item_download_flags(item["id"], torrent_downloaded=True, db_path=db_path)
            update_media_primary_source(item["id"], "torrent", db_path=db_path)
            if source_path.is_dir() and not bool(item.get("archive_downloaded", False)):
                update_archive_auto_queue(
                    item["id"],
                    enabled=True,
                    retry_after_epoch=0,
                    last_error="",
                    db_path=db_path,
                )
            final_status = _resolve_torrent_terminal_status(download_adapter, candidate["hash_string"])
            update_item_status(item["id"], final_status, db_path)

            action = {
                "item_id": item["id"],
                "title": item["title"],
                "torrent_hash": candidate["hash_string"],
                "source_path": str(source_path),
                "destination_path": str(destination_path),
                "method": import_result["method"],
                "file_count": import_result["file_count"],
                "status": final_status,
            }
            actions.append(action)
            print(
                f"Imported {item['id']} ({_safe_text(item['title'])}) -> "
                f"{destination_path} via {import_result['method']} "
                f"({import_result['file_count']} file(s)). Status: {final_status}."
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Import failed for {item['id']} ({_safe_text(item['title'])}): {exc}")
            update_item_status(item["id"], "error", db_path)
            update_item_progress(item["id"], 0.0, db_path)

    return actions


def import_completed_archives(
    db_path: str | Path,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    media_library = _resolve_media_library(config)
    archive_library = _resolve_archive_library(config)
    media_library.mkdir(parents=True, exist_ok=True)
    archive_library.mkdir(parents=True, exist_ok=True)

    actions: list[dict[str, Any]] = []
    archive_items = _get_archive_downloaded_items(db_path)

    for item in archive_items:
        try:
            stored_path = str(item.get("archive_path", "") or "")
            source_path = Path(stored_path) if stored_path else None
            if source_path and not source_path.is_absolute():
                source_path = archive_library / source_path

            if source_path is None or not source_path.exists():
                source_path = _find_existing_archive_source(archive_library, item["id"])
            if source_path is None:
                continue

            destination_path = media_library / _build_media_filename(
                item_id=item["id"],
                title=item["title"],
                suffix=source_path.suffix,
                source="archive",
            )
            import_result = _import_single_file(source_path, destination_path)
            actions.append(
                {
                    "item_id": item["id"],
                    "title": item["title"],
                    "source_path": str(source_path),
                    "destination_path": str(destination_path),
                    "method": import_result["method"],
                    "file_count": import_result["file_count"],
                }
            )
            if import_result["method"] != "existing":
                print(
                    f"Linked archive {item['id']} ({_safe_text(item['title'])}) -> "
                    f"{destination_path} via {import_result['method']}."
                )
            if not bool(item.get("torrent_downloaded", False)):
                update_media_primary_source(item["id"], "archive", db_path=db_path)
            update_archive_auto_queue(
                item["id"],
                enabled=False,
                retry_after_epoch=0,
                last_error="",
                db_path=db_path,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Archive import failed for {item['id']} ({_safe_text(item['title'])}): {exc}")
            update_item_status(item["id"], "error", db_path)
            update_item_progress(item["id"], 0.0, db_path)

    return actions


def migrate_legacy_archive_storage(
    db_path: str | Path,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    media_library = _resolve_media_library(config)
    archive_library = _resolve_archive_library(config)
    media_library.mkdir(parents=True, exist_ok=True)
    archive_library.mkdir(parents=True, exist_ok=True)

    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                archive_downloaded,
                torrent_downloaded,
                external_method
            FROM items
            WHERE archive_downloaded = 1
              AND torrent_downloaded = 0
              AND TRIM(COALESCE(external_method, '')) != ''
            ORDER BY last_updated_epoch ASC, title COLLATE NOCASE ASC
            """
        ).fetchall()

    actions: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if _find_existing_archive_source(archive_library, item["id"]) is not None:
            continue

        media_path = _find_standardized_media_path(
            media_library,
            item["id"],
            item["title"],
            preferred_source="archive",
        )
        if media_path is None or not media_path.is_file():
            continue

        archive_path = archive_library / _build_archive_filename(item["title"], media_path.suffix)
        if archive_path.exists():
            continue

        archive_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(media_path), str(archive_path))
        relink_method = _hardlink_back_or_copy(archive_path, media_path)
        actions.append(
            {
                "item_id": item["id"],
                "title": item["title"],
                "archive_path": str(archive_path),
                "media_path": str(media_path),
                "method": f"move+{relink_method}",
            }
        )
        print(
            f"Migrated legacy archive storage for {item['id']} "
            f"({_safe_text(item['title'])}) -> {archive_path} via move+{relink_method}."
        )

    return actions


def reconcile_torrent_imports(
    db_path: str | Path,
    config: dict[str, Any],
    download_adapter: ImportAdapter,
) -> list[dict[str, Any]]:
    media_library = _resolve_media_library(config)
    media_library.mkdir(parents=True, exist_ok=True)
    media_entries = _list_media_library_entries(media_library)

    actions: list[dict[str, Any]] = []
    imported_items = _get_torrent_downloaded_items(db_path)

    for item in imported_items:
        standardized_path = _find_standardized_media_path(
            media_library,
            item["id"],
            item["title"],
            media_entries=media_entries,
        )
        if standardized_path is not None:
            actions.append(
                {
                    "item_id": item["id"],
                    "title": item["title"],
                    "action": "ok",
                    "path": str(standardized_path),
                }
            )
            continue

        legacy_path = _find_legacy_media_path(
            media_library,
            item["title"],
            media_entries=media_entries,
        )
        if legacy_path is not None:
            destination_path = media_library / _build_expected_media_name(
                item["id"],
                item["title"],
                legacy_path,
                source="torrent",
            )
            if not destination_path.exists():
                legacy_path.rename(destination_path)
                print(
                    f"Standardized legacy media path for {item['id']} "
                    f"({_safe_text(item['title'])}) -> {destination_path}."
                )
                try:
                    media_entries.remove(legacy_path)
                except ValueError:
                    pass
                media_entries.append(destination_path)
                actions.append(
                    {
                        "item_id": item["id"],
                        "title": item["title"],
                        "action": "renamed",
                        "from": str(legacy_path),
                        "to": str(destination_path),
                    }
                )
                continue

        candidate = select_best_candidate(db_path, item["id"])
        if candidate is None:
            continue

        save_path = download_adapter.get_save_path(candidate["hash_string"])
        if not save_path:
            continue

        try:
            _resolve_source_path(Path(save_path))
        except FileNotFoundError:
            continue

        update_item_download_flags(item["id"], torrent_downloaded=False, db_path=db_path)
        update_item_status(item["id"], "complete", db_path)
        print(
            f"Reset missing torrent import for {item['id']} "
            f"({_safe_text(item['title'])}); source still exists in qB path."
        )
        actions.append(
            {
                "item_id": item["id"],
                "title": item["title"],
                "action": "reset_for_reimport",
                "source_path": str(save_path),
            }
        )

    return actions


def recover_misclassified_torrent_imports(
    db_path: str | Path,
    config: dict[str, Any],
    download_adapter: ImportAdapter,
) -> list[dict[str, Any]]:
    media_library = _resolve_media_library(config)
    media_library.mkdir(parents=True, exist_ok=True)
    media_entries = _list_media_library_entries(media_library)

    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                local_status,
                torrent_downloaded,
                archive_downloaded,
                external_method
            FROM items
            WHERE local_status IN ('imported', 'completed', 'seeding')
              AND torrent_downloaded = 0
              AND archive_downloaded = 1
              AND TRIM(COALESCE(external_method, '')) = ''
            ORDER BY last_updated_epoch ASC, title COLLATE NOCASE ASC
            """
        ).fetchall()

    actions: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        candidate = select_best_candidate(db_path, item["id"])
        if candidate is None:
            continue

        status = download_adapter.get_status(candidate["hash_string"]) or {}
        if not bool(status.get("found")) or not bool(status.get("is_completed")):
            continue

        save_path = download_adapter.get_save_path(candidate["hash_string"])
        if not save_path:
            continue

        try:
            _resolve_source_path(Path(save_path))
        except FileNotFoundError:
            continue

        media_path = (
            _find_standardized_media_path(
                media_library,
                item["id"],
                item["title"],
                media_entries=media_entries,
            )
            or _find_legacy_media_path(
                media_library,
                item["title"],
                media_entries=media_entries,
            )
        )
        if media_path is None:
            continue

        update_item_download_flags(
            item["id"],
            torrent_downloaded=True,
            archive_downloaded=False,
            db_path=db_path,
        )
        update_item_status(
            item["id"],
            _resolve_torrent_terminal_status(download_adapter, candidate["hash_string"]),
            db_path,
        )
        actions.append(
            {
                "item_id": item["id"],
                "title": item["title"],
                "action": "recovered_torrent_import_state",
                "media_path": str(media_path),
                "source_path": str(save_path),
            }
        )
        print(
            f"Recovered torrent import state for {item['id']} "
            f"({_safe_text(item['title'])}) from qB + Media Library."
        )

    return actions


def sync_torrent_terminal_statuses(
    db_path: str | Path,
    download_adapter: ImportAdapter,
) -> list[dict[str, Any]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                local_status
            FROM items
            WHERE torrent_downloaded = 1
              AND local_status NOT IN ('downloading', 'force_queued', 'force_external', 'downloading_external')
            ORDER BY last_updated_epoch ASC, title COLLATE NOCASE ASC
            """
        ).fetchall()

    actions: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        candidate = select_best_candidate(db_path, item["id"])
        target_status = (
            _resolve_torrent_terminal_status(download_adapter, candidate["hash_string"])
            if candidate is not None
            else "completed"
        )
        current_status = str(item.get("local_status", "")).strip().lower()
        if current_status == target_status:
            continue

        update_item_status(item["id"], target_status, db_path)
        actions.append(
            {
                "item_id": item["id"],
                "title": item["title"],
                "from_status": current_status,
                "to_status": target_status,
            }
        )
        print(
            f"Updated torrent terminal status for {item['id']} "
            f"({_safe_text(item['title'])}): {current_status or 'n/a'} -> {target_status}."
        )

    return actions


def normalize_media_gid_bracket_format(
    db_path: str | Path,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    media_library = _resolve_media_library(config)
    media_library.mkdir(parents=True, exist_ok=True)
    media_entries = _list_media_library_entries(media_library)

    actions: list[dict[str, Any]] = []
    for item in _get_media_linked_items(db_path):
        source = _preferred_source_for_item(item)
        candidate_path = _find_legacy_prefixed_media_path(
            media_library,
            item["id"],
            item["title"],
            media_entries=media_entries,
        )
        if candidate_path is None:
            candidate_path = _find_unsourced_standardized_media_path(
                media_library,
                item["id"],
                media_entries=media_entries,
            )
        if candidate_path is None:
            continue

        expected_name = _build_expected_media_name(
            item["id"],
            item["title"],
            candidate_path,
            source=source,
        )
        destination_path = media_library / expected_name
        if candidate_path.name == expected_name:
            continue

        if destination_path.exists():
            try:
                same_target = destination_path.samefile(candidate_path)
            except OSError:
                same_target = False
            if same_target and candidate_path != destination_path:
                if candidate_path.is_file():
                    candidate_path.unlink()
                    method = "removed_duplicate_alias"
                    try:
                        media_entries.remove(candidate_path)
                    except ValueError:
                        pass
                elif candidate_path.is_dir() and not any(candidate_path.iterdir()):
                    candidate_path.rmdir()
                    method = "removed_empty_duplicate_alias"
                    try:
                        media_entries.remove(candidate_path)
                    except ValueError:
                        pass
                else:
                    print(
                        f"Skipped gid bracket rename for {item['id']} "
                        f"({_safe_text(item['title'])}): non-empty duplicate directory {candidate_path}."
                    )
                    continue
            else:
                print(
                    f"Skipped gid bracket rename for {item['id']} "
                    f"({_safe_text(item['title'])}): target already exists at {destination_path}."
                )
                continue
        else:
            candidate_path.rename(destination_path)
            method = "renamed"
            try:
                media_entries.remove(candidate_path)
            except ValueError:
                pass
            media_entries.append(destination_path)

        actions.append(
            {
                "item_id": item["id"],
                "title": item["title"],
                "from": str(candidate_path),
                "to": str(destination_path),
                "method": method,
            }
        )
        print(
            f"Normalized media gid format for {item['id']} "
            f"({_safe_text(item['title'])}) -> {destination_path} ({method})."
        )

    return actions


def promote_media_to_seed(
    db_path: str | Path,
    item_id: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    item = get_item(item_id, db_path)
    if item is None:
        raise ValueError(f"Item not found: {item_id}")

    media_library = _resolve_media_library(config)
    qb_download_path = _resolve_qb_download_path(config)
    qb_download_path.mkdir(parents=True, exist_ok=True)

    library_path = _find_media_library_file(media_library, item["id"], item["title"])
    destination_path = qb_download_path / library_path.name

    if destination_path.exists():
        raise FileExistsError(f"Destination already exists: {destination_path}")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(library_path), str(destination_path))
    relink_method = _hardlink_back_or_copy(destination_path, library_path)

    action = {
        "item_id": item["id"],
        "title": item["title"],
        "source_path": str(library_path),
        "seed_path": str(destination_path),
        "library_path": str(library_path),
        "method": f"move+{relink_method}",
    }
    print(
        f"Promoted {item['id']} ({_safe_text(item['title'])}) for seeding: "
        f"{destination_path} via move+{relink_method}."
    )
    return action


def _get_items_by_status(db_path: str | Path, status: str) -> list[dict[str, Any]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                site_category,
                custom_folder_slot,
                gallery_filesize,
                progress_pct,
                last_updated_epoch,
                local_status,
                thumbnail_local_path,
                archive_downloaded
            FROM items
            WHERE local_status = ?
            ORDER BY last_updated_epoch ASC, title COLLATE NOCASE ASC
            """,
            (status,),
        ).fetchall()
    return [dict(row) for row in rows]


def _get_torrent_downloaded_items(db_path: str | Path) -> list[dict[str, Any]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                local_status,
                torrent_downloaded,
                archive_downloaded
            FROM items
            WHERE torrent_downloaded = 1
            ORDER BY last_updated_epoch ASC, title COLLATE NOCASE ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _get_archive_downloaded_items(db_path: str | Path) -> list[dict[str, Any]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                local_status,
                torrent_downloaded,
                archive_downloaded,
                external_method,
                archive_path
            FROM items
            WHERE archive_downloaded = 1
              AND TRIM(COALESCE(external_method, '')) != ''
            ORDER BY last_updated_epoch ASC, title COLLATE NOCASE ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _get_media_linked_items(db_path: str | Path) -> list[dict[str, Any]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                torrent_downloaded,
                archive_downloaded
            FROM items
            WHERE torrent_downloaded = 1
               OR archive_downloaded = 1
            ORDER BY last_updated_epoch ASC, title COLLATE NOCASE ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _resolve_media_library(config: dict[str, Any]) -> Path:
    paths_config = config.get("paths", {})
    if not isinstance(paths_config, dict):
        raise ValueError("config['paths'] must be a dictionary.")

    media_library = paths_config.get("media_library")
    if not isinstance(media_library, str) or not media_library.strip():
        raise ValueError("config['paths']['media_library'] is required.")
    return Path(media_library).expanduser()


def _resolve_archive_library(config: dict[str, Any]) -> Path:
    paths_config = config.get("paths", {})
    if not isinstance(paths_config, dict):
        raise ValueError("config['paths'] must be a dictionary.")

    archive_library = paths_config.get("archive_library") or paths_config.get("archive_downloads")
    if isinstance(archive_library, str) and archive_library.strip():
        return Path(archive_library).expanduser()

    legacy_external = paths_config.get("external_archive")
    if isinstance(legacy_external, str) and legacy_external.strip():
        return Path(legacy_external).expanduser()

    media_library = _resolve_media_library(config)
    sibling_name = f"{media_library.name}_Archives"
    return media_library.parent / sibling_name


def _resolve_qb_download_path(config: dict[str, Any]) -> Path:
    qb_config = config.get("qbittorrent", {})
    if not isinstance(qb_config, dict):
        raise ValueError("config['qbittorrent'] must be a dictionary.")

    download_path = qb_config.get("download_path")
    if not isinstance(download_path, str) or not download_path.strip():
        raise ValueError("config['qbittorrent']['download_path'] is required.")
    return Path(download_path).expanduser()


def _resolve_source_path(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"Source path does not exist: {path}")
    if path.is_file():
        _ensure_not_partial(path)
        return path

    source_files = [
        candidate
        for candidate in sorted(path.rglob("*"))
        if candidate.is_file() and _is_not_partial(candidate)
    ]
    if not source_files:
        raise FileNotFoundError(f"Source directory has no files: {path}")
    if len(source_files) == 1:
        return source_files[0]
    return path


_PARTIAL_SUFFIXES = {".part", ".!qB", ".!qb", ".partial"}


def _is_not_partial(path: Path) -> bool:
    return path.suffix not in _PARTIAL_SUFFIXES


def _ensure_not_partial(path: Path) -> None:
    if not _is_not_partial(path):
        raise FileNotFoundError(f"Source path is a partial/incomplete file: {path}")


def _build_media_filename(
    item_id: str,
    title: str,
    suffix: str,
    source: str | None = None,
) -> str:
    clean_title = sanitize_filename_component(title)
    clean_suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ""
    source_marker = _build_source_marker(source)
    stem = f"{_build_media_item_prefix(item_id)}{source_marker} {clean_title}".strip()
    return _truncate_name_with_suffix(stem, clean_suffix, MAX_NAME_BYTES)


def _build_archive_filename(title: str, suffix: str) -> str:
    clean_title = sanitize_filename_component(title)
    clean_suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ""
    return _truncate_name_with_suffix(clean_title or "archive", clean_suffix, MAX_NAME_BYTES)


def _build_media_dirname(item_id: str, title: str, source: str | None = None) -> str:
    source_marker = _build_source_marker(source)
    base = f"{_build_media_item_prefix(item_id)}{source_marker} {sanitize_filename_component(title)}".strip()
    return _trim_utf8_bytes(base, MAX_NAME_BYTES)


def _build_expected_media_name(
    item_id: str,
    title: str,
    existing_path: Path,
    source: str | None = None,
) -> str:
    if existing_path.is_file():
        return _build_media_filename(item_id, title, existing_path.suffix, source=source)
    return _build_media_dirname(item_id, title, source=source)


def _build_media_item_prefix(item_id: str) -> str:
    clean_id = sanitize_filename_component(item_id)
    match = ITEM_ID_GID_PATTERN.match(clean_id)
    if not match:
        return clean_id
    return f"g-[{match.group(1)}]"


def _build_source_marker(source: str | None) -> str:
    normalized = str(source or "").strip().lower()
    if normalized in {"torrent", "archive"}:
        return f" [{normalized}]"
    return ""


def _build_legacy_media_item_prefix(item_id: str) -> str:
    return sanitize_filename_component(item_id)


def _build_legacy_media_filename(item_id: str, title: str, suffix: str) -> str:
    clean_title = sanitize_filename_component(title)
    clean_suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ""
    stem = f"{_build_legacy_media_item_prefix(item_id)} {clean_title}".strip()
    return _truncate_name_with_suffix(stem, clean_suffix, MAX_NAME_BYTES)


def _build_legacy_media_dirname(item_id: str, title: str) -> str:
    base = f"{_build_legacy_media_item_prefix(item_id)} {sanitize_filename_component(title)}".strip()
    return _trim_utf8_bytes(base, MAX_NAME_BYTES)


def sanitize_filename_component(value: Any) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub(" ", str(value))
    cleaned = cleaned.replace("\0", " ")
    cleaned = WHITESPACE_RUN.sub(" ", cleaned).strip().rstrip(".")
    if not cleaned:
        return "untitled"
    return _trim_utf8_bytes(cleaned, MAX_NAME_BYTES)


def _trim_utf8_bytes(value: str, max_bytes: int) -> str:
    raw = str(value or "")
    encoded = raw.encode("utf-8")
    if len(encoded) <= max_bytes:
        return raw
    trimmed = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return trimmed.rstrip(" .") or "untitled"


def _truncate_name_with_suffix(stem: str, suffix: str, max_bytes: int) -> str:
    safe_suffix = str(suffix or "")
    suffix_bytes = len(safe_suffix.encode("utf-8"))
    if suffix_bytes >= max_bytes:
        safe_suffix = ""
        suffix_bytes = 0
    budget = max(1, max_bytes - suffix_bytes)
    safe_stem = _trim_utf8_bytes(stem or "untitled", budget)
    return f"{safe_stem}{safe_suffix}".strip() or "untitled"


def _hardlink_or_copy(source_path: Path, destination_path: Path) -> str:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists():
        return "existing"

    try:
        os.link(source_path, destination_path)
        return "hardlink"
    except OSError:
        shutil.copy2(source_path, destination_path)
        return "copy"


def _import_single_file(source_path: Path, destination_path: Path) -> dict[str, Any]:
    method = _hardlink_or_copy(source_path, destination_path)
    return {
        "method": method,
        "file_count": 1,
    }


def _import_directory_tree(source_root: Path, destination_root: Path) -> dict[str, Any]:
    source_files = sorted(candidate for candidate in source_root.rglob("*") if candidate.is_file())
    if not source_files:
        raise FileNotFoundError(f"Source directory has no files: {source_root}")

    destination_root.mkdir(parents=True, exist_ok=True)

    hardlink_count = 0
    copy_count = 0
    existing_count = 0

    for source_file in source_files:
        relative_path = source_file.relative_to(source_root)
        destination_file = destination_root / relative_path
        method = _hardlink_or_copy(source_file, destination_file)
        if method == "hardlink":
            hardlink_count += 1
        elif method == "copy":
            copy_count += 1
        else:
            existing_count += 1

    method_parts: list[str] = []
    if hardlink_count:
        method_parts.append(f"hardlink:{hardlink_count}")
    if copy_count:
        method_parts.append(f"copy:{copy_count}")
    if existing_count:
        method_parts.append(f"existing:{existing_count}")

    return {
        "method": ", ".join(method_parts) if method_parts else "existing",
        "file_count": len(source_files),
    }


def _find_media_library_file(
    media_library: Path,
    item_id: str,
    title: str,
    media_entries: list[Path] | None = None,
) -> Path:
    standardized_match = _find_standardized_media_path(
        media_library,
        item_id,
        title,
        preferred_source="torrent",
        media_entries=media_entries,
    )
    if standardized_match is not None:
        return standardized_match

    if _find_legacy_media_path(media_library, title, media_entries=media_entries) is None:
        raise FileNotFoundError(
            f"No imported media file found in {media_library} for item {item_id}."
        )

    raise ValueError(
        f"Multiple media files found in {media_library} for item {item_id}; "
        "cannot safely promote."
    )


def _find_standardized_media_path(
    media_library: Path,
    item_id: str,
    title: str,
    preferred_source: str = "torrent",
    media_entries: list[Path] | None = None,
) -> Path | None:
    return _find_standardized_media_path_with_preference(
        media_library,
        item_id,
        title,
        preferred_source=preferred_source,
        media_entries=media_entries,
    )


def _find_standardized_media_path_with_preference(
    media_library: Path,
    item_id: str,
    title: str,
    preferred_source: str = "torrent",
    media_entries: list[Path] | None = None,
) -> Path | None:
    entries = media_entries if media_entries is not None else _list_media_library_entries(media_library)
    expected_prefix = f"{_build_media_item_prefix(item_id)} "
    legacy_prefix = f"{_build_legacy_media_item_prefix(item_id)} "
    prefix_matches = [
        candidate
        for candidate in entries
        if candidate.name.startswith(expected_prefix)
        or (legacy_prefix != expected_prefix and candidate.name.startswith(legacy_prefix))
    ]
    picked = _pick_preferred_media_candidate(prefix_matches, item_id, preferred_source, media_library)
    if picked is not None:
        return picked

    expected_stem = {
        _build_media_filename(item_id, title, "", source="torrent").strip(),
        _build_media_filename(item_id, title, "", source="archive").strip(),
        _build_media_filename(item_id, title, "", source=None).strip(),
        _build_legacy_media_filename(item_id, title, "").strip(),
    }
    stem_matches = [
        candidate
        for candidate in entries
        if candidate.is_file() and candidate.stem in expected_stem
    ]
    picked = _pick_preferred_media_candidate(stem_matches, item_id, preferred_source, media_library)
    if picked is not None:
        return picked

    expected_dirnames = {
        _build_media_dirname(item_id, title, source="torrent"),
        _build_media_dirname(item_id, title, source="archive"),
        _build_media_dirname(item_id, title, source=None),
        _build_legacy_media_dirname(item_id, title),
    }
    dir_matches = [
        candidate
        for candidate in entries
        if candidate.is_dir() and candidate.name in expected_dirnames
    ]
    return _pick_preferred_media_candidate(dir_matches, item_id, preferred_source, media_library)


def _pick_preferred_media_candidate(
    candidates: list[Path],
    item_id: str,
    preferred_source: str,
    media_library: Path,
) -> Path | None:
    if not candidates:
        return None

    normalized_preferred = "archive" if str(preferred_source).strip().lower() == "archive" else "torrent"
    scored: dict[int, list[Path]] = {0: [], 1: [], 2: []}
    for candidate in candidates:
        source = _extract_media_source_marker(candidate.name)
        if source == normalized_preferred:
            scored[0].append(candidate)
        elif not source:
            scored[1].append(candidate)
        else:
            scored[2].append(candidate)

    for score in (0, 1, 2):
        pool = scored[score]
        if not pool:
            continue
        if len(pool) == 1:
            return pool[0]
        raise ValueError(
            f"Multiple media paths found in {media_library} for item {item_id} "
            f"with preferred source {normalized_preferred}."
        )
    return None


def _extract_media_source_marker(name: str) -> str:
    candidate = str(name or "")
    marker = MEDIA_SOURCE_PATTERN.search(candidate.split(" ", 1)[1] if " " in candidate else candidate)
    if not marker:
        return ""
    return str(marker.group(1)).strip().lower()


def _preferred_source_for_item(item: dict[str, Any]) -> str:
    primary = str(item.get("media_primary_source", "") or "").strip().lower()
    if primary in {"torrent", "archive"}:
        return primary
    if bool(item.get("torrent_downloaded", False)):
        return "torrent"
    if bool(item.get("archive_downloaded", False)):
        return "archive"
    return "torrent"


def _find_legacy_prefixed_media_path(
    media_library: Path,
    item_id: str,
    title: str,
    media_entries: list[Path] | None = None,
) -> Path | None:
    entries = media_entries if media_entries is not None else _list_media_library_entries(media_library)
    legacy_prefix = f"{_build_legacy_media_item_prefix(item_id)} "
    legacy_prefix_matches = [
        candidate
        for candidate in entries
        if candidate.name.startswith(legacy_prefix)
    ]
    if len(legacy_prefix_matches) == 1:
        return legacy_prefix_matches[0]
    if len(legacy_prefix_matches) > 1:
        raise ValueError(
            f"Multiple legacy media paths found in {media_library} for item {item_id}."
        )

    legacy_stem = _build_legacy_media_filename(item_id, title, "").strip()
    legacy_file_matches = [
        candidate
        for candidate in entries
        if candidate.is_file() and candidate.stem == legacy_stem
    ]
    if len(legacy_file_matches) == 1:
        return legacy_file_matches[0]
    if len(legacy_file_matches) > 1:
        raise ValueError(
            f"Multiple legacy media file paths found in {media_library} for item {item_id}."
        )

    legacy_dirname = _build_legacy_media_dirname(item_id, title)
    legacy_dir_matches = [
        candidate
        for candidate in entries
        if candidate.is_dir() and candidate.name == legacy_dirname
    ]
    if len(legacy_dir_matches) == 1:
        return legacy_dir_matches[0]
    if len(legacy_dir_matches) > 1:
        raise ValueError(
            f"Multiple legacy media directory paths found in {media_library} for item {item_id}."
        )
    return None


def _find_unsourced_standardized_media_path(
    media_library: Path,
    item_id: str,
    media_entries: list[Path] | None = None,
) -> Path | None:
    entries = media_entries if media_entries is not None else _list_media_library_entries(media_library)
    prefix = f"{_build_media_item_prefix(item_id)} "
    matches = [
        candidate
        for candidate in entries
        if candidate.name.startswith(prefix) and not _extract_media_source_marker(candidate.name)
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"Multiple unsourced standardized media paths found in {media_library} for item {item_id}."
        )
    return None


def _find_legacy_media_path(
    media_library: Path,
    title: str,
    media_entries: list[Path] | None = None,
) -> Path | None:
    entries = media_entries if media_entries is not None else _list_media_library_entries(media_library)
    legacy_dir = media_library / sanitize_filename_component(title)
    if legacy_dir.exists():
        return legacy_dir

    legacy_file_matches = [
        candidate
        for candidate in entries
        if candidate.is_file() and candidate.stem == sanitize_filename_component(title)
    ]
    if len(legacy_file_matches) == 1:
        return legacy_file_matches[0]
    if len(legacy_file_matches) > 1:
        raise ValueError(
            f"Multiple legacy media file paths found in {media_library} for title {title}."
        )
    return None


def _list_media_library_entries(media_library: Path) -> list[Path]:
    return list(media_library.iterdir())


def _find_existing_archive_source(archive_library: Path, item_id: str) -> Path | None:
    # Prefer exact matches without gid prefix (server-provided archive names)
    title = sanitize_filename_component(item_id.split(' ', 1)[-1]) if ' ' in item_id else ''
    candidates: list[Path] = []
    for candidate in archive_library.iterdir():
        if not candidate.is_file():
            continue
        name = candidate.name
        if name.startswith(f"{sanitize_filename_component(item_id)} "):
            candidates.append(candidate)
        elif title and title in sanitize_filename_component(name):
            candidates.append(candidate)
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda candidate: candidate.stat().st_size)
    except OSError:
        return candidates[0]


def _hardlink_back_or_copy(source_path: Path, destination_path: Path) -> str:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source_path, destination_path)
        return "hardlink"
    except OSError:
        shutil.copy2(source_path, destination_path)
        return "copy"


def _safe_text(value: Any) -> str:
    text = str(value)
    try:
        text.encode("cp1252")
        return text
    except UnicodeEncodeError:
        return text.encode("cp1252", errors="replace").decode("cp1252")


def _resolve_torrent_terminal_status(
    download_adapter: ImportAdapter,
    torrent_hash: str,
) -> str:
    try:
        status = download_adapter.get_status(torrent_hash) or {}
    except Exception:
        return "completed"
    if bool(status.get("found")) and bool(status.get("is_seeding")):
        return "seeding"
    return "completed"
