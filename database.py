from __future__ import annotations

import json
import sqlite3
import time
from html import unescape
from pathlib import Path, PurePath
from typing import Any, Mapping


APP_DIR = Path(__file__).parent
DEFAULT_DB_PATH = APP_DIR / "dashboard.db"

# Allow accidental pathlib values in SQL parameter bindings.
sqlite3.register_adapter(PurePath, lambda value: str(value))
sqlite3.register_adapter(Path, lambda value: str(value))
try:  # Linux containers
    from pathlib import PosixPath

    sqlite3.register_adapter(PosixPath, lambda value: str(value))
except Exception:  # noqa: BLE001
    pass
try:  # Windows hosts
    from pathlib import WindowsPath

    sqlite3.register_adapter(WindowsPath, lambda value: str(value))
except Exception:  # noqa: BLE001
    pass


def _normalize_epoch_seconds(*values: Any) -> int:
    for value in values:
        if value in (None, "", 0, "0"):
            continue
        try:
            numeric = int(float(value))
        except (TypeError, ValueError):
            continue
        if numeric <= 0:
            continue
        if numeric >= 1_000_000_000_000:
            numeric //= 1000
        return numeric
    return 0


def _resolve_db_path(db_path: str | Path | None = None) -> Path:
    return Path(db_path) if db_path is not None else DEFAULT_DB_PATH


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = _resolve_db_path(db_path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def init_db(db_path: str | Path | None = None) -> None:
    with get_connection(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                title_jpn TEXT NOT NULL DEFAULT '',
                uploader TEXT NOT NULL DEFAULT '',
                site_category TEXT NOT NULL DEFAULT '',
                favorite_category TEXT NOT NULL DEFAULT '',
                favorite_note TEXT NOT NULL DEFAULT '',
                favorited_epoch INTEGER NOT NULL DEFAULT 0,
                item_url TEXT NOT NULL DEFAULT '',
                custom_folder_slot INTEGER,
                gallery_filesize INTEGER NOT NULL DEFAULT 0,
                filecount INTEGER NOT NULL DEFAULT 0,
                rating TEXT NOT NULL DEFAULT '0.0',
                expunged INTEGER NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT '',
                parent_gid TEXT NOT NULL DEFAULT '',
                first_gid TEXT NOT NULL DEFAULT '',
                torrent_downloaded INTEGER NOT NULL DEFAULT 0,
                archive_downloaded INTEGER NOT NULL DEFAULT 0,
                progress_pct REAL NOT NULL DEFAULT 0,
                last_updated_epoch INTEGER NOT NULL DEFAULT 0,
                state_changed_epoch INTEGER NOT NULL DEFAULT 0,
                local_status TEXT NOT NULL DEFAULT 'new',
                thumbnail_local_path TEXT NOT NULL DEFAULT '',
                is_latest_revision INTEGER NOT NULL DEFAULT 1,
                superseded_by_id TEXT NOT NULL DEFAULT '',
                external_method TEXT NOT NULL DEFAULT '',
                external_label TEXT NOT NULL DEFAULT '',
                external_size_text TEXT NOT NULL DEFAULT '',
                external_cost_gp INTEGER,
                archive_path TEXT NOT NULL DEFAULT '',
                archive_cancel_requested INTEGER NOT NULL DEFAULT 0,
                media_primary_source TEXT NOT NULL DEFAULT 'torrent',
                archive_auto_queue INTEGER NOT NULL DEFAULT 0,
                archive_retry_after_epoch INTEGER NOT NULL DEFAULT 0,
                archive_retry_count INTEGER NOT NULL DEFAULT 0,
                archive_last_auto_error TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS torrents (
                hash_string TEXT PRIMARY KEY,
                parent_item_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0,
                added_epoch INTEGER NOT NULL DEFAULT 0,
                is_best_candidate INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(parent_item_id) REFERENCES items(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS rules (
                rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_category TEXT NOT NULL DEFAULT '',
                match_folder_slot INTEGER,
                auto_download INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS rule_sets (
                rule_set_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0,
                auto_download INTEGER NOT NULL DEFAULT 1,
                match_category TEXT NOT NULL DEFAULT '',
                match_favorite_category TEXT NOT NULL DEFAULT '',
                match_folder_slot INTEGER,
                uploader_contains TEXT NOT NULL DEFAULT '',
                title_contains TEXT NOT NULL DEFAULT '',
                tags_contains TEXT NOT NULL DEFAULT '',
                tags_not_contains TEXT NOT NULL DEFAULT '',
                notes_contains TEXT NOT NULL DEFAULT '',
                notes_not_contains TEXT NOT NULL DEFAULT '',
                rating_min REAL,
                rating_max REAL,
                filesize_min_bytes INTEGER,
                filesize_max_bytes INTEGER,
                filecount_min INTEGER,
                filecount_max INTEGER,
                posted_from_epoch INTEGER,
                posted_to_epoch INTEGER,
                expunged_mode TEXT NOT NULL DEFAULT 'any',
                max_external_gp INTEGER,
                created_epoch INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS item_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL,
                error_type TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                fix_hint TEXT NOT NULL DEFAULT '',
                detected_epoch INTEGER NOT NULL DEFAULT 0,
                fixed_epoch INTEGER NOT NULL DEFAULT 0,
                UNIQUE(item_id, error_type, message)
            );

            CREATE INDEX IF NOT EXISTS idx_items_site_category
                ON items(site_category);

            CREATE INDEX IF NOT EXISTS idx_items_uploader
                ON items(uploader);

            CREATE INDEX IF NOT EXISTS idx_torrents_parent_item
                ON torrents(parent_item_id);

            CREATE INDEX IF NOT EXISTS idx_rules_category_slot
                ON rules(match_category, match_folder_slot);

            CREATE INDEX IF NOT EXISTS idx_rule_sets_active
                ON rule_sets(is_active);

            CREATE INDEX IF NOT EXISTS idx_rule_sets_name
                ON rule_sets(name COLLATE NOCASE);
            """
        )

        item_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(items)").fetchall()
        }
        revision_columns_added = False
        _ensure_item_column(connection, item_columns, "title_jpn", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "uploader", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "favorite_category", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "favorite_note", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "favorited_epoch", "INTEGER NOT NULL DEFAULT 0")
        _ensure_item_column(connection, item_columns, "item_url", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "filecount", "INTEGER NOT NULL DEFAULT 0")
        _ensure_item_column(connection, item_columns, "rating", "TEXT NOT NULL DEFAULT '0.0'")
        _ensure_item_column(connection, item_columns, "expunged", "INTEGER NOT NULL DEFAULT 0")
        _ensure_item_column(connection, item_columns, "tags", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "parent_gid", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "first_gid", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "torrent_downloaded", "INTEGER NOT NULL DEFAULT 0")
        _ensure_item_column(connection, item_columns, "archive_downloaded", "INTEGER NOT NULL DEFAULT 0")
        _ensure_item_column(connection, item_columns, "progress_pct", "REAL NOT NULL DEFAULT 0")
        _ensure_item_column(connection, item_columns, "state_changed_epoch", "INTEGER NOT NULL DEFAULT 0")
        revision_columns_added = _ensure_item_column(
            connection,
            item_columns,
            "is_latest_revision",
            "INTEGER NOT NULL DEFAULT 1",
        ) or revision_columns_added
        revision_columns_added = _ensure_item_column(
            connection,
            item_columns,
            "superseded_by_id",
            "TEXT NOT NULL DEFAULT ''",
        ) or revision_columns_added
        _ensure_item_column(connection, item_columns, "external_method", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "external_label", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "external_size_text", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "external_cost_gp", "INTEGER")
        _ensure_item_column(connection, item_columns, "archive_path", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "archive_cancel_requested", "INTEGER NOT NULL DEFAULT 0")
        _ensure_item_column(connection, item_columns, "media_primary_source", "TEXT NOT NULL DEFAULT 'torrent'")
        _ensure_item_column(connection, item_columns, "archive_auto_queue", "INTEGER NOT NULL DEFAULT 0")
        _ensure_item_column(connection, item_columns, "archive_retry_after_epoch", "INTEGER NOT NULL DEFAULT 0")
        _ensure_item_column(connection, item_columns, "archive_retry_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_item_column(connection, item_columns, "archive_last_auto_error", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "last_error_message", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "last_error_epoch", "INTEGER NOT NULL DEFAULT 0")
        _ensure_item_column(connection, item_columns, "torrent_from_archive", "INTEGER NOT NULL DEFAULT 0")
        _ensure_item_column(connection, item_columns, "torrent_created_epoch", "INTEGER NOT NULL DEFAULT 0")
        _ensure_item_column(connection, item_columns, "torrent_uploaded_epoch", "INTEGER NOT NULL DEFAULT 0")
        _ensure_item_column(connection, item_columns, "torrent_upload_status", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "torrent_staging_path", "TEXT NOT NULL DEFAULT ''")
        _ensure_item_column(connection, item_columns, "torrent_hash_uploaded", "TEXT NOT NULL DEFAULT ''")
        rule_set_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(rule_sets)").fetchall()
        }
        _ensure_rule_set_column(connection, rule_set_columns, "max_external_gp", "INTEGER")
        _ensure_rule_set_column(connection, rule_set_columns, "tags_not_contains", "TEXT NOT NULL DEFAULT ''")
        _ensure_rule_set_column(connection, rule_set_columns, "notes_contains", "TEXT NOT NULL DEFAULT ''")
        _ensure_rule_set_column(connection, rule_set_columns, "notes_not_contains", "TEXT NOT NULL DEFAULT ''")
        if revision_columns_added:
            _rebuild_revision_flags_in_connection(connection)
    repair_favorited_metadata_from_exports(db_path)


def _ensure_item_column(
    connection: sqlite3.Connection,
    item_columns: set[str],
    column_name: str,
    column_sql: str,
) -> bool:
    if column_name in item_columns:
        return False
    connection.execute(f"ALTER TABLE items ADD COLUMN {column_name} {column_sql}")
    item_columns.add(column_name)
    return True


def _ensure_rule_set_column(
    connection: sqlite3.Connection,
    rule_set_columns: set[str],
    column_name: str,
    column_sql: str,
) -> bool:
    if column_name in rule_set_columns:
        return False
    connection.execute(f"ALTER TABLE rule_sets ADD COLUMN {column_name} {column_sql}")
    rule_set_columns.add(column_name)
    return True


def upsert_item(item: Mapping[str, Any], db_path: str | Path | None = None) -> bool:
    item_id = str(item["id"])

    with get_connection(db_path) as connection:
        existing_row = connection.execute(
            """
            SELECT
                id,
                title,
                title_jpn,
                uploader,
                site_category,
                favorite_category,
                favorite_note,
                favorite_note,
                favorited_epoch,
                item_url,
                custom_folder_slot,
                gallery_filesize,
                filecount,
                rating,
                expunged,
                tags,
                parent_gid,
                first_gid,
                torrent_downloaded,
                archive_downloaded,
                last_updated_epoch,
                local_status,
                thumbnail_local_path,
                archive_path
            FROM items
            WHERE id = ?
            """,
            (item_id,),
        ).fetchone()

        normalized_item = {
            "id": item_id,
            "title": unescape(str(item.get("title", ""))),
            "title_jpn": unescape(str(item.get("title_jpn", ""))),
            "uploader": unescape(str(item.get("uploader", ""))),
            "site_category": str(item.get("site_category", "")),
            "favorite_category": str(item.get("favorite_category", "")),
            "favorite_note": str(item.get("favorite_note", "")),
            "favorited_epoch": _normalize_epoch_seconds(
                item.get("favorited_epoch"),
                item.get("date_favorited"),
                item.get("favorited_time"),
                item.get("favorited_timestamp"),
            ),
            "item_url": str(item.get("item_url", "")),
            "custom_folder_slot": item.get("custom_folder_slot"),
            "gallery_filesize": int(item.get("gallery_filesize", 0) or 0),
            "filecount": int(item.get("filecount", item.get("file_count", 0)) or 0),
            "rating": str(item.get("rating", "0.0")),
            "expunged": 1 if bool(item.get("expunged", False)) else 0,
            "tags": _normalize_tags(item.get("tags", "")),
            "parent_gid": str(item.get("parent_gid", "")),
            "first_gid": str(item.get("first_gid", "")),
            "torrent_downloaded": 1 if bool(item.get("torrent_downloaded", False)) else 0,
            "archive_downloaded": 1 if bool(item.get("archive_downloaded", False)) else 0,
            "last_updated_epoch": int(item.get("last_updated_epoch", 0) or 0),
            "local_status": str(item.get("local_status", "new")),
            "thumbnail_local_path": str(item.get("thumbnail_local_path", "")),
            "archive_path": str(item.get("archive_path", "")),
        }

        # Metadata refreshes should not wipe runtime download state unless the caller
        # explicitly supplies new flag values.
        if existing_row is not None:
            if (
                "favorited_epoch" not in item
                and "date_favorited" not in item
                and "favorited_time" not in item
                and "favorited_timestamp" not in item
            ):
                normalized_item["favorited_epoch"] = int(existing_row["favorited_epoch"] or 0)
            if "torrent_downloaded" not in item:
                normalized_item["torrent_downloaded"] = int(existing_row["torrent_downloaded"])
            if "archive_downloaded" not in item:
                normalized_item["archive_downloaded"] = int(existing_row["archive_downloaded"])
            if "local_status" not in item:
                normalized_item["local_status"] = str(existing_row["local_status"])
            if "thumbnail_local_path" not in item and str(existing_row["thumbnail_local_path"]):
                normalized_item["thumbnail_local_path"] = str(existing_row["thumbnail_local_path"])
            if "archive_path" not in item and "archive_downloaded" not in item:
                normalized_item["archive_path"] = str(existing_row["archive_path"] or "")
            if "favorite_note" not in item:
                normalized_item["favorite_note"] = str(existing_row["favorite_note"] or "")

        if existing_row is not None and dict(existing_row) == normalized_item:
            return False

        connection.execute(
            """
            INSERT INTO items (
                id,
                title,
                title_jpn,
                uploader,
                site_category,
                favorite_category,
                favorite_note,
                favorited_epoch,
                item_url,
                custom_folder_slot,
                gallery_filesize,
                filecount,
                rating,
                expunged,
                tags,
                parent_gid,
                first_gid,
                torrent_downloaded,
                archive_downloaded,
                last_updated_epoch,
                local_status,
                thumbnail_local_path,
                archive_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                title_jpn = excluded.title_jpn,
                uploader = excluded.uploader,
                site_category = excluded.site_category,
                favorite_category = excluded.favorite_category,
                favorite_note = excluded.favorite_note,
                favorited_epoch = excluded.favorited_epoch,
                item_url = excluded.item_url,
                custom_folder_slot = excluded.custom_folder_slot,
                gallery_filesize = excluded.gallery_filesize,
                filecount = excluded.filecount,
                rating = excluded.rating,
                expunged = excluded.expunged,
                tags = excluded.tags,
                parent_gid = excluded.parent_gid,
                first_gid = excluded.first_gid,
                torrent_downloaded = excluded.torrent_downloaded,
                archive_downloaded = excluded.archive_downloaded,
                last_updated_epoch = excluded.last_updated_epoch,
                local_status = excluded.local_status,
                thumbnail_local_path = excluded.thumbnail_local_path,
                archive_path = excluded.archive_path
            """,
            (
                normalized_item["id"],
                normalized_item["title"],
                normalized_item["title_jpn"],
                normalized_item["uploader"],
                normalized_item["site_category"],
                normalized_item["favorite_category"],
                normalized_item["favorite_note"],
                normalized_item["favorited_epoch"],
                normalized_item["item_url"],
                normalized_item["custom_folder_slot"],
                normalized_item["gallery_filesize"],
                normalized_item["filecount"],
                normalized_item["rating"],
                normalized_item["expunged"],
                normalized_item["tags"],
                normalized_item["parent_gid"],
                normalized_item["first_gid"],
                normalized_item["torrent_downloaded"],
                normalized_item["archive_downloaded"],
                normalized_item["last_updated_epoch"],
                normalized_item["local_status"],
                normalized_item["thumbnail_local_path"],
                normalized_item["archive_path"],
            ),
        )
        _apply_revision_chain_updates(
            connection,
            item_id=normalized_item["id"],
            parent_gid=normalized_item["parent_gid"],
            first_gid=normalized_item["first_gid"],
        )
    return True


def _normalize_tags(tags: Any) -> str:
    if isinstance(tags, str):
        return tags
    if isinstance(tags, (list, dict)):
        return json.dumps(tags, ensure_ascii=False)
    return str(tags)


def _apply_revision_chain_updates(
    connection: sqlite3.Connection,
    item_id: str,
    parent_gid: str,
    first_gid: str,
) -> None:
    connection.execute(
        """
        UPDATE items
        SET is_latest_revision = 1,
            superseded_by_id = ''
        WHERE id = ?
        """,
        (item_id,),
    )

    conditions: list[str] = []
    params: list[Any] = []
    normalized_parent_gid = str(parent_gid or "").strip()
    normalized_first_gid = str(first_gid or "").strip()

    if normalized_first_gid:
        conditions.append("first_gid = ?")
        params.append(normalized_first_gid)
        conditions.append("id = ?")
        params.append(f"g-{normalized_first_gid}")

    if normalized_parent_gid:
        conditions.append("id = ?")
        params.append(f"g-{normalized_parent_gid}")

    if not conditions:
        return

    connection.execute(
        f"""
        UPDATE items
        SET is_latest_revision = 0,
            superseded_by_id = ?
        WHERE id <> ?
          AND ({' OR '.join(conditions)})
        """,
        (item_id, item_id, *params),
    )


def _rebuild_revision_flags_in_connection(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE items
        SET is_latest_revision = 1,
            superseded_by_id = ''
        """
    )
    rows = connection.execute(
        """
        SELECT
            id,
            parent_gid,
            first_gid
        FROM items
        ORDER BY last_updated_epoch ASC, id ASC
        """
    ).fetchall()
    for row in rows:
        _apply_revision_chain_updates(
            connection,
            item_id=str(row["id"]),
            parent_gid=str(row["parent_gid"] or ""),
            first_gid=str(row["first_gid"] or ""),
        )


def rebuild_revision_flags(db_path: str | Path | None = None) -> None:
    with get_connection(db_path) as connection:
        _rebuild_revision_flags_in_connection(connection)


def update_item_status(
    item_id: str,
    local_status: str,
    error_message: str | None = None,
    db_path: str | Path | None = None,
) -> None:
    now_epoch = int(time.time())
    set_error_fields = local_status.strip().lower() == "error"
    with get_connection(db_path) as connection:
        if set_error_fields:
            connection.execute(
                """
                UPDATE items
                SET local_status = ?,
                    state_changed_epoch = ?,
                    last_error_message = ?,
                    last_error_epoch = ?
                WHERE id = ?
                """,
                (local_status, now_epoch, error_message or "", now_epoch, item_id),
            )
        else:
            connection.execute(
                "UPDATE items SET local_status = ?, state_changed_epoch = ? WHERE id = ?",
                (local_status, now_epoch, item_id),
            )


def update_item_favorite_metadata(
    item_id: str,
    favorite_category: str,
    custom_folder_slot: int | None,
    item_url: str,
    favorited_epoch: int | None = None,
    favorite_note: str = "",
    db_path: str | Path | None = None,
) -> bool:
    normalized_category = str(favorite_category or "")
    normalized_item_url = str(item_url or "")
    normalized_epoch = _normalize_epoch_seconds(favorited_epoch)
    normalized_note = str(favorite_note or "")

    with get_connection(db_path) as connection:
        existing_row = connection.execute(
            """
            SELECT
                favorite_category,
                favorited_epoch,
                custom_folder_slot,
                item_url,
                favorite_note
            FROM items
            WHERE id = ?
            """,
            (item_id,),
        ).fetchone()
        if existing_row is None:
            return False

        existing_slot = (
            int(existing_row["custom_folder_slot"])
            if existing_row["custom_folder_slot"] is not None
            else None
        )
        if (
            str(existing_row["favorite_category"] or "") == normalized_category
            and int(existing_row["favorited_epoch"] or 0) == normalized_epoch
            and existing_slot == custom_folder_slot
            and str(existing_row["item_url"] or "") == normalized_item_url
            and str(existing_row["favorite_note"] or "") == normalized_note
        ):
            return False

        connection.execute(
            """
            UPDATE items
            SET
                favorite_category = ?,
                favorited_epoch = ?,
                custom_folder_slot = ?,
                favorite_note = ?,
                item_url = ?
            WHERE id = ?
            """,
            (
                normalized_category,
                normalized_epoch,
                custom_folder_slot,
                normalized_note,
                normalized_item_url,
                item_id,
            ),
        )
    return True


def repair_favorited_metadata_from_exports(db_path: str | Path | None = None) -> int:
    export_dir = APP_DIR / "plugins"
    if not export_dir.exists():
        return 0

    export_files = sorted(
        export_dir.glob("Sadpanda-Favorites-*.json"),
        key=lambda candidate: candidate.stat().st_mtime,
        reverse=True,
    )
    if not export_files:
        return 0

    metadata_by_item_id: dict[str, dict[str, Any]] = {}
    for export_file in export_files:
        try:
            payload = json.loads(export_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list):
            continue

        for entry in payload:
            if not isinstance(entry, dict):
                continue
            full = entry.get("gallery_info_full")
            if not isinstance(full, dict):
                continue
            gallery = full.get("gallery")
            if not isinstance(gallery, dict):
                continue

            gid = str(gallery.get("gid", "") or "").strip()
            if not gid:
                continue

            favorited_epoch = _normalize_epoch_seconds(full.get("date_favorited"))
            if not favorited_epoch:
                continue

            item_id = f"g-{gid}"
            if item_id in metadata_by_item_id:
                continue

            favorite_category = ""
            custom_folder_slot = None
            favorite_data = full.get("favorites")
            if isinstance(favorite_data, dict):
                favorite_category = str(favorite_data.get("category_title", "") or "")
                raw_slot = favorite_data.get("category")
                if raw_slot not in (None, ""):
                    try:
                        custom_folder_slot = int(raw_slot)
                    except (TypeError, ValueError):
                        custom_folder_slot = None

            token = str(gallery.get("token", "") or "").strip()
            item_url = f"https://exhentai.org/g/{gid}/{token}/" if token else ""

            metadata_by_item_id[item_id] = {
                "favorited_epoch": favorited_epoch,
                "favorite_category": favorite_category,
                "custom_folder_slot": custom_folder_slot,
                "item_url": item_url,
            }

    if not metadata_by_item_id:
        return 0

    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                favorite_category,
                custom_folder_slot,
                item_url
            FROM items
            WHERE favorited_epoch = 0
            """
        ).fetchall()

        updates: list[tuple[Any, ...]] = []
        for row in rows:
            item_id = str(row["id"])
            mapped = metadata_by_item_id.get(item_id)
            if mapped is None:
                continue

            favorite_category = str(row["favorite_category"] or "") or str(mapped["favorite_category"] or "")
            custom_folder_slot = (
                int(row["custom_folder_slot"])
                if row["custom_folder_slot"] is not None
                else mapped["custom_folder_slot"]
            )
            item_url = str(row["item_url"] or "") or str(mapped["item_url"] or "")

            updates.append(
                (
                    int(mapped["favorited_epoch"]),
                    favorite_category,
                    custom_folder_slot,
                    item_url,
                    item_id,
                )
            )

        if not updates:
            return 0

        connection.executemany(
            """
            UPDATE items
            SET
                favorited_epoch = ?,
                favorite_category = ?,
                custom_folder_slot = ?,
                item_url = ?
            WHERE id = ?
            """,
            updates,
        )
    return len(updates)


def normalize_legacy_auto_queue_statuses(db_path: str | Path | None = None) -> int:
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            """
            UPDATE items
            SET local_status = 'new'
            WHERE local_status = 'queued'
            """
        )
    return int(cursor.rowcount or 0)


def normalize_legacy_terminal_statuses(db_path: str | Path | None = None) -> int:
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            """
            UPDATE items
            SET local_status = 'completed'
            WHERE local_status = 'imported'
            """
        )
    return int(cursor.rowcount or 0)


def update_item_progress(
    item_id: str,
    progress_pct: float,
    db_path: str | Path | None = None,
) -> None:
    clamped_progress = max(0.0, min(float(progress_pct), 100.0))
    with get_connection(db_path) as connection:
        connection.execute(
            "UPDATE items SET progress_pct = ? WHERE id = ?",
            (clamped_progress, item_id),
        )


def update_item_download_flags(
    item_id: str,
    torrent_downloaded: bool | None = None,
    archive_downloaded: bool | None = None,
    db_path: str | Path | None = None,
) -> None:
    updates: list[str] = []
    params: list[Any] = []

    if torrent_downloaded is not None:
        updates.append("torrent_downloaded = ?")
        params.append(1 if torrent_downloaded else 0)

    if archive_downloaded is not None:
        updates.append("archive_downloaded = ?")
        params.append(1 if archive_downloaded else 0)

    if not updates:
        return

    updates.append("state_changed_epoch = ?")
    params.append(int(time.time()))
    params.append(item_id)
    with get_connection(db_path) as connection:
        connection.execute(
            f"UPDATE items SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )


def update_media_primary_source(
    item_id: str,
    primary_source: str,
    db_path: str | Path | None = None,
) -> None:
    normalized = str(primary_source or "").strip().lower()
    if normalized not in {"torrent", "archive"}:
        return
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE items
            SET media_primary_source = ?, state_changed_epoch = ?
            WHERE id = ?
            """,
            (normalized, int(time.time()), item_id),
        )


def update_archive_auto_queue(
    item_id: str,
    *,
    enabled: bool,
    retry_after_epoch: int = 0,
    retry_count: int = 0,
    last_error: str = "",
    db_path: str | Path | None = None,
) -> None:
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE items
            SET
                archive_auto_queue = ?,
                archive_retry_after_epoch = ?,
                archive_retry_count = ?,
                archive_last_auto_error = ?,
                state_changed_epoch = ?
            WHERE id = ?
            """,
            (
                1 if enabled else 0,
                max(0, int(retry_after_epoch or 0)),
                max(0, int(retry_count or 0)),
                str(last_error or ""),
                int(time.time()),
                item_id,
            ),
        )


def update_item_archive_path(
    item_id: str,
    archive_path: str,
    db_path: str | Path | None = None,
) -> None:
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE items
            SET archive_path = ?, state_changed_epoch = ?
            WHERE id = ?
            """,
            (str(archive_path or ""), int(time.time()), item_id),
        )


def update_item_external_selection(
    item_id: str,
    external_method: str = "",
    external_label: str = "",
    external_size_text: str = "",
    external_cost_gp: int | None = None,
    db_path: str | Path | None = None,
) -> None:
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE items
            SET
                external_method = ?,
                external_label = ?,
                external_size_text = ?,
                external_cost_gp = ?
            WHERE id = ?
            """,
            (
                str(external_method or ""),
                str(external_label or ""),
                str(external_size_text or ""),
                None if external_cost_gp is None else int(external_cost_gp),
                item_id,
            ),
        )


def set_archive_cancel_requested(
    item_id: str,
    requested: bool,
    db_path: str | Path | None = None,
) -> None:
    with get_connection(db_path) as connection:
        connection.execute(
            "UPDATE items SET archive_cancel_requested = ? WHERE id = ?",
            (1 if requested else 0, item_id),
        )


def get_item(item_id: str, db_path: str | Path | None = None) -> dict[str, Any] | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT
                items.*,
                COUNT(torrents.hash_string) AS torrent_count,
                MAX(CASE WHEN torrents.is_best_candidate THEN torrents.name ELSE '' END)
                    AS best_torrent_name
            FROM items
            LEFT JOIN torrents ON torrents.parent_item_id = items.id
            WHERE items.id = ?
            GROUP BY items.id
            """,
            (item_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def get_all_items_for_ui(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                items.id,
                items.title,
                items.title_jpn,
                items.uploader,
                items.site_category,
                items.favorite_category,
                items.favorite_note,
                items.favorited_epoch,
                items.item_url,
                items.custom_folder_slot,
                items.gallery_filesize,
                items.filecount,
                items.rating,
                items.expunged,
                items.tags,
                items.parent_gid,
                items.first_gid,
                items.torrent_downloaded,
                COALESCE(items.torrent_from_archive, 0) AS torrent_from_archive,
                items.archive_downloaded,
                items.progress_pct,
                items.last_updated_epoch,
                items.state_changed_epoch,
                items.local_status,
                items.thumbnail_local_path,
                items.is_latest_revision,
                items.superseded_by_id,
                items.external_method,
                items.external_label,
                items.external_size_text,
                items.external_cost_gp,
                items.archive_path,
                items.archive_cancel_requested,
                items.media_primary_source,
                items.archive_auto_queue,
                items.archive_retry_after_epoch,
                items.archive_retry_count,
                items.archive_last_auto_error,
                COUNT(torrents.hash_string) AS torrent_count,
                MAX(CASE WHEN torrents.is_best_candidate THEN torrents.name ELSE '' END)
                    AS best_torrent_name
            FROM items
            LEFT JOIN torrents ON torrents.parent_item_id = items.id
            GROUP BY
                items.id,
                items.title,
                items.title_jpn,
                items.uploader,
                items.site_category,
                items.favorite_category,
                items.favorite_note,
                items.favorited_epoch,
                items.item_url,
                items.custom_folder_slot,
                items.gallery_filesize,
                items.filecount,
                items.rating,
                items.expunged,
                items.tags,
                items.parent_gid,
                items.first_gid,
                items.torrent_downloaded,
                items.torrent_from_archive,
                items.archive_downloaded,
                items.progress_pct,
                items.last_updated_epoch,
                items.state_changed_epoch,
                items.local_status,
                items.thumbnail_local_path,
                items.is_latest_revision,
                items.superseded_by_id,
                items.external_method,
                items.external_label,
                items.external_size_text,
                items.external_cost_gp,
                items.archive_path,
                items.archive_cancel_requested,
                items.media_primary_source,
                items.archive_auto_queue,
                items.archive_retry_after_epoch,
                items.archive_retry_count,
                items.archive_last_auto_error
            ORDER BY items.last_updated_epoch DESC, items.title COLLATE NOCASE ASC
            """
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        record["has_torrent"] = bool(record["torrent_count"])
        record["expunged"] = bool(record["expunged"])
        record["torrent_downloaded"] = bool(record["torrent_downloaded"])
        record["torrent_from_archive"] = bool(record["torrent_from_archive"])
        record["archive_downloaded"] = bool(record["archive_downloaded"])
        record["is_latest_revision"] = bool(record["is_latest_revision"])
        record["archive_path"] = record.get("archive_path", "") or ""
        record["archive_cancel_requested"] = bool(record["archive_cancel_requested"])
        record["media_primary_source"] = str(record.get("media_primary_source", "torrent") or "torrent")
        record["archive_auto_queue"] = bool(record.get("archive_auto_queue", 0))
        record["archive_retry_after_epoch"] = int(record.get("archive_retry_after_epoch", 0) or 0)
        record["archive_retry_count"] = int(record.get("archive_retry_count", 0) or 0)
        record["archive_last_auto_error"] = str(record.get("archive_last_auto_error", "") or "")
        record["favorite_note"] = str(record.get("favorite_note", "") or "")
        items.append(record)
    return items


def upsert_torrent(torrent: Mapping[str, Any], db_path: str | Path | None = None) -> bool:
    normalized_torrent = {
        "hash_string": str(torrent["hash_string"]),
        "parent_item_id": str(torrent["parent_item_id"]),
        "name": unescape(str(torrent.get("name", ""))),
        "size": int(torrent.get("size", 0) or 0),
        "added_epoch": int(torrent.get("added_epoch", 0) or 0),
        "is_best_candidate": 1 if bool(torrent.get("is_best_candidate", False)) else 0,
    }

    with get_connection(db_path) as connection:
        existing_row = connection.execute(
            """
            SELECT
                hash_string,
                parent_item_id,
                name,
                size,
                added_epoch,
                is_best_candidate
            FROM torrents
            WHERE hash_string = ?
            """,
            (normalized_torrent["hash_string"],),
        ).fetchone()
        if existing_row is not None and dict(existing_row) == normalized_torrent:
            return False

        connection.execute(
            """
            INSERT INTO torrents (
                hash_string,
                parent_item_id,
                name,
                size,
                added_epoch,
                is_best_candidate
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(hash_string) DO UPDATE SET
                parent_item_id = excluded.parent_item_id,
                name = excluded.name,
                size = excluded.size,
                added_epoch = excluded.added_epoch,
                is_best_candidate = excluded.is_best_candidate
            """,
            (
                normalized_torrent["hash_string"],
                normalized_torrent["parent_item_id"],
                normalized_torrent["name"],
                normalized_torrent["size"],
                normalized_torrent["added_epoch"],
                normalized_torrent["is_best_candidate"],
            ),
        )
    return True


# --- Error tracking helpers ---


def record_error(
    item_id: str,
    error_type: str,
    message: str,
    fix_hint: str = "",
    db_path: str | Path | None = None,
) -> None:
    normalized_item_id = str(item_id)
    normalized_error_type = str(error_type)
    normalized_message = str(message)
    normalized_fix_hint = str(fix_hint or "")
    now_epoch = int(time.time())
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE item_errors
            SET fixed_epoch = ?
            WHERE item_id = ?
              AND error_type = ?
              AND fixed_epoch = 0
            """,
            (now_epoch, normalized_item_id, normalized_error_type),
        )
        connection.execute(
            """
            INSERT INTO item_errors (item_id, error_type, message, fix_hint, detected_epoch)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(item_id, error_type, message) DO UPDATE SET
                detected_epoch = excluded.detected_epoch,
                fixed_epoch = 0,
                fix_hint = excluded.fix_hint
            """,
            (
                normalized_item_id,
                normalized_error_type,
                normalized_message,
                normalized_fix_hint,
                now_epoch,
            ),
        )
        connection.execute(
            """
            UPDATE items
            SET last_error_message = ?, last_error_epoch = ?
            WHERE id = ? AND (last_error_epoch IS NULL OR last_error_epoch < ?)
            """,
            (normalized_message, now_epoch, normalized_item_id, now_epoch),
        )


def resolve_error(
    error_id: int | None = None,
    item_id: str | None = None,
    error_type: str | None = None,
    message: str | None = None,
    db_path: str | Path | None = None,
) -> int:
    now_epoch = int(time.time())
    conditions: list[str] = []
    params: list[Any] = []
    if error_id is not None:
        conditions.append("id = ?")
        params.append(int(error_id))
    if item_id is not None:
        conditions.append("item_id = ?")
        params.append(item_id)
    if error_type is not None:
        conditions.append("error_type = ?")
        params.append(error_type)
    if message is not None:
        conditions.append("message = ?")
        params.append(message)
    if not conditions:
        return 0

    with get_connection(db_path) as connection:
        cursor = connection.execute(
            f"UPDATE item_errors SET fixed_epoch = ? WHERE {' AND '.join(conditions)}",
            (now_epoch, *params),
        )
        return cursor.rowcount


def clear_errors_for_item(item_id: str, db_path: str | Path | None = None) -> None:
    with get_connection(db_path) as connection:
        connection.execute("DELETE FROM item_errors WHERE item_id = ?", (item_id,))


def get_open_errors(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                item_id,
                error_type,
                message,
                fix_hint,
                detected_epoch
            FROM (
                SELECT
                    id,
                    item_id,
                    error_type,
                    message,
                    fix_hint,
                    detected_epoch,
                    ROW_NUMBER() OVER (
                        PARTITION BY item_id, error_type
                        ORDER BY detected_epoch DESC, id DESC
                    ) AS row_num
                FROM item_errors
                WHERE fixed_epoch = 0
            )
            WHERE row_num = 1
            ORDER BY detected_epoch DESC, id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_torrents_for_item(
    parent_item_id: str,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                hash_string,
                parent_item_id,
                name,
                size,
                added_epoch,
                is_best_candidate
            FROM torrents
            WHERE parent_item_id = ?
            ORDER BY is_best_candidate DESC, added_epoch DESC, name COLLATE NOCASE ASC
            """,
            (parent_item_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_rule(rule: Mapping[str, Any], db_path: str | Path | None = None) -> int:
    with get_connection(db_path) as connection:
        rule_id = rule.get("rule_id")
        if rule_id in (None, ""):
            cursor = connection.execute(
                """
                INSERT INTO rules (
                    match_category,
                    match_folder_slot,
                    auto_download
                )
                VALUES (?, ?, ?)
                """,
                (
                    str(rule.get("match_category", "")),
                    rule.get("match_folder_slot"),
                    1 if bool(rule.get("auto_download", False)) else 0,
                ),
            )
            return int(cursor.lastrowid)

        connection.execute(
            """
            INSERT INTO rules (
                rule_id,
                match_category,
                match_folder_slot,
                auto_download
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(rule_id) DO UPDATE SET
                match_category = excluded.match_category,
                match_folder_slot = excluded.match_folder_slot,
                auto_download = excluded.auto_download
            """,
            (
                int(rule_id),
                str(rule.get("match_category", "")),
                rule.get("match_folder_slot"),
                1 if bool(rule.get("auto_download", False)) else 0,
            ),
        )
        return int(rule_id)


def get_all_rules(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT rule_id, match_category, match_folder_slot, auto_download
            FROM rules
            ORDER BY rule_id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def save_rule_set(rule_set: Mapping[str, Any], db_path: str | Path | None = None) -> int:
    normalized = _normalize_rule_set_input(rule_set)
    now_epoch = int(time.time())

    with get_connection(db_path) as connection:
        rule_set_id = rule_set.get("rule_set_id")
        if rule_set_id in (None, ""):
            cursor = connection.execute(
                """
                INSERT INTO rule_sets (
                    name,
                    is_active,
                    auto_download,
                    match_category,
                    match_favorite_category,
                    match_folder_slot,
                    uploader_contains,
                    title_contains,
                    tags_contains,
                    tags_not_contains,
                    notes_contains,
                    notes_not_contains,
                    rating_min,
                    rating_max,
                    filesize_min_bytes,
                    filesize_max_bytes,
                    filecount_min,
                    filecount_max,
                    posted_from_epoch,
                    posted_to_epoch,
                    expunged_mode,
                    max_external_gp,
                    created_epoch
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["name"],
                    normalized["is_active"],
                    normalized["auto_download"],
                    normalized["match_category"],
                    normalized["match_favorite_category"],
                    normalized["match_folder_slot"],
                    normalized["uploader_contains"],
                    normalized["title_contains"],
                    normalized["tags_contains"],
                    normalized["tags_not_contains"],
                    normalized["notes_contains"],
                    normalized["notes_not_contains"],
                    normalized["rating_min"],
                    normalized["rating_max"],
                    normalized["filesize_min_bytes"],
                    normalized["filesize_max_bytes"],
                    normalized["filecount_min"],
                    normalized["filecount_max"],
                    normalized["posted_from_epoch"],
                    normalized["posted_to_epoch"],
                    normalized["expunged_mode"],
                    normalized["max_external_gp"],
                    now_epoch,
                ),
            )
            return int(cursor.lastrowid)

        connection.execute(
            """
            UPDATE rule_sets
            SET
                name = ?,
                is_active = ?,
                auto_download = ?,
                match_category = ?,
                match_favorite_category = ?,
                match_folder_slot = ?,
                uploader_contains = ?,
                title_contains = ?,
                tags_contains = ?,
                tags_not_contains = ?,
                notes_contains = ?,
                notes_not_contains = ?,
                rating_min = ?,
                rating_max = ?,
                filesize_min_bytes = ?,
                filesize_max_bytes = ?,
                filecount_min = ?,
                filecount_max = ?,
                posted_from_epoch = ?,
                posted_to_epoch = ?,
                expunged_mode = ?,
                max_external_gp = ?
            WHERE rule_set_id = ?
            """,
            (
                normalized["name"],
                normalized["is_active"],
                normalized["auto_download"],
                normalized["match_category"],
                normalized["match_favorite_category"],
                normalized["match_folder_slot"],
                normalized["uploader_contains"],
                normalized["title_contains"],
                normalized["tags_contains"],
                normalized["tags_not_contains"],
                normalized["notes_contains"],
                normalized["notes_not_contains"],
                normalized["rating_min"],
                normalized["rating_max"],
                normalized["filesize_min_bytes"],
                normalized["filesize_max_bytes"],
                normalized["filecount_min"],
                normalized["filecount_max"],
                normalized["posted_from_epoch"],
                normalized["posted_to_epoch"],
                normalized["expunged_mode"],
                normalized["max_external_gp"],
                int(rule_set_id),
            ),
        )
        return int(rule_set_id)


def get_all_rule_sets(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                rule_set_id,
                name,
                is_active,
                auto_download,
                match_category,
                match_favorite_category,
                match_folder_slot,
                uploader_contains,
                title_contains,
                tags_contains,
                tags_not_contains,
                notes_contains,
                notes_not_contains,
                rating_min,
                rating_max,
                filesize_min_bytes,
                filesize_max_bytes,
                filecount_min,
                filecount_max,
                posted_from_epoch,
                posted_to_epoch,
                expunged_mode,
                max_external_gp,
                created_epoch
            FROM rule_sets
            ORDER BY is_active DESC, name COLLATE NOCASE ASC, rule_set_id ASC
            """
        ).fetchall()
    return [_normalize_rule_set_row(dict(row)) for row in rows]


def get_active_rule_set(db_path: str | Path | None = None) -> dict[str, Any] | None:
    active_rule_sets = get_active_rule_sets(db_path)
    return active_rule_sets[0] if active_rule_sets else None


def get_active_rule_sets(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                rule_set_id,
                name,
                is_active,
                auto_download,
                match_category,
                match_favorite_category,
                match_folder_slot,
                uploader_contains,
                title_contains,
                tags_contains,
                tags_not_contains,
                notes_contains,
                notes_not_contains,
                rating_min,
                rating_max,
                filesize_min_bytes,
                filesize_max_bytes,
                filecount_min,
                filecount_max,
                posted_from_epoch,
                posted_to_epoch,
                expunged_mode,
                max_external_gp,
                created_epoch
            FROM rule_sets
            WHERE is_active = 1
            ORDER BY name COLLATE NOCASE ASC, rule_set_id ASC
            """
        ).fetchall()
    return [_normalize_rule_set_row(dict(row)) for row in rows]


def set_active_rule_set(
    rule_set_id: int | None,
    db_path: str | Path | None = None,
) -> None:
    with get_connection(db_path) as connection:
        if rule_set_id in (None, ""):
            connection.execute("UPDATE rule_sets SET is_active = 0")
        else:
            connection.execute(
                "UPDATE rule_sets SET is_active = 1 WHERE rule_set_id = ?",
                (int(rule_set_id),),
            )


def set_rule_set_active(
    rule_set_id: int,
    is_active: bool,
    db_path: str | Path | None = None,
) -> None:
    with get_connection(db_path) as connection:
        connection.execute(
            "UPDATE rule_sets SET is_active = ? WHERE rule_set_id = ?",
            (1 if is_active else 0, int(rule_set_id)),
        )


def delete_rule_set(
    rule_set_id: int,
    db_path: str | Path | None = None,
) -> bool:
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            "DELETE FROM rule_sets WHERE rule_set_id = ?",
            (int(rule_set_id),),
        )
    return cursor.rowcount > 0


def item_matches_rule_set(item: Mapping[str, Any], rule_set: Mapping[str, Any]) -> bool:
    category = str(rule_set.get("match_category", "") or "").strip().casefold()
    if category and str(item.get("site_category", "") or "").strip().casefold() != category:
        return False

    favorite_category = str(rule_set.get("match_favorite_category", "") or "").strip().casefold()
    if favorite_category and str(item.get("favorite_category", "") or "").strip().casefold() != favorite_category:
        return False

    folder_slot = rule_set.get("match_folder_slot")
    if folder_slot not in (None, ""):
        try:
            item_slot = int(item.get("custom_folder_slot")) if item.get("custom_folder_slot") is not None else None
        except (TypeError, ValueError):
            item_slot = None
        if item_slot != int(folder_slot):
            return False

    uploader_contains = str(rule_set.get("uploader_contains", "") or "").strip().casefold()
    if uploader_contains and uploader_contains not in str(item.get("uploader", "") or "").casefold():
        return False

    title_contains = str(rule_set.get("title_contains", "") or "").strip().casefold()
    if title_contains:
        title_haystack = " ".join(
            [
                str(item.get("title", "") or ""),
                str(item.get("title_jpn", "") or ""),
            ]
        ).casefold()
        if title_contains not in title_haystack:
            return False

    tags_contains = str(rule_set.get("tags_contains", "") or "").strip().casefold()
    tag_value = _stringify_tags(item.get("tags", "")).casefold()
    if tags_contains and tags_contains not in tag_value:
        return False
    tags_not_contains = str(rule_set.get("tags_not_contains", "") or "").strip().casefold()
    if tags_not_contains and tags_not_contains in tag_value:
        return False

    rating_value = _safe_float(item.get("rating"))
    rating_min = _safe_float(rule_set.get("rating_min"))
    if rating_min is not None and (rating_value is None or rating_value < rating_min):
        return False
    rating_max = _safe_float(rule_set.get("rating_max"))
    if rating_max is not None and (rating_value is None or rating_value > rating_max):
        return False

    gallery_filesize = _safe_int(item.get("gallery_filesize"))
    filesize_min = _safe_int(rule_set.get("filesize_min_bytes"))
    if filesize_min is not None and (gallery_filesize is None or gallery_filesize < filesize_min):
        return False
    filesize_max = _safe_int(rule_set.get("filesize_max_bytes"))
    if filesize_max is not None and (gallery_filesize is None or gallery_filesize > filesize_max):
        return False

    filecount = _safe_int(item.get("filecount"))
    filecount_min = _safe_int(rule_set.get("filecount_min"))
    if filecount_min is not None and (filecount is None or filecount < filecount_min):
        return False
    filecount_max = _safe_int(rule_set.get("filecount_max"))
    if filecount_max is not None and (filecount is None or filecount > filecount_max):
        return False

    posted_epoch = _safe_int(item.get("last_updated_epoch"))
    posted_from = _safe_int(rule_set.get("posted_from_epoch"))
    if posted_from is not None and (posted_epoch is None or posted_epoch < posted_from):
        return False
    posted_to = _safe_int(rule_set.get("posted_to_epoch"))
    if posted_to is not None and (posted_epoch is None or posted_epoch > posted_to):
        return False

    expunged_mode = str(rule_set.get("expunged_mode", "any") or "any").strip().lower()
    item_expunged = bool(item.get("expunged", False))
    if expunged_mode == "yes" and not item_expunged:
        return False
    if expunged_mode == "no" and item_expunged:
        return False

    note_value = str(item.get("favorite_note", "") or "").casefold()
    notes_contains = str(rule_set.get("notes_contains", "") or "").strip().casefold()
    if notes_contains and notes_contains not in note_value:
        return False
    notes_not_contains = str(rule_set.get("notes_not_contains", "") or "").strip().casefold()
    if notes_not_contains and notes_not_contains in note_value:
        return False

    return True


def get_category_rule_map(db_path: str | Path | None = None) -> dict[str, bool]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT match_category, MAX(auto_download) AS auto_download
            FROM rules
            WHERE match_category != '' AND match_folder_slot IS NULL
            GROUP BY match_category
            """
        ).fetchall()
    return {
        str(row["match_category"]): bool(row["auto_download"])
        for row in rows
    }


def set_category_auto_download_rule(
    match_category: str,
    auto_download: bool,
    db_path: str | Path | None = None,
) -> int:
    normalized_category = str(match_category).strip()
    with get_connection(db_path) as connection:
        existing_rule = connection.execute(
            """
            SELECT rule_id
            FROM rules
            WHERE match_category = ? AND match_folder_slot IS NULL
            ORDER BY rule_id ASC
            LIMIT 1
            """,
            (normalized_category,),
        ).fetchone()

        if existing_rule is None:
            cursor = connection.execute(
                """
                INSERT INTO rules (
                    match_category,
                    match_folder_slot,
                    auto_download
                )
                VALUES (?, NULL, ?)
                """,
                (normalized_category, 1 if auto_download else 0),
            )
            return int(cursor.lastrowid)

        rule_id = int(existing_rule["rule_id"])
        connection.execute(
            """
            UPDATE rules
            SET auto_download = ?
            WHERE rule_id = ?
            """,
            (1 if auto_download else 0, rule_id),
        )
        return rule_id


def delete_item(item_id: str, db_path: str | Path | None = None) -> bool:
    with get_connection(db_path) as connection:
        cursor = connection.execute("DELETE FROM items WHERE id = ?", (item_id,))
    return cursor.rowcount > 0


def get_setting(
    setting_key: str,
    default: str | None = None,
    db_path: str | Path | None = None,
) -> str | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT setting_value
            FROM settings
            WHERE setting_key = ?
            """,
            (str(setting_key),),
        ).fetchone()
    if row is None:
        return default
    return str(row["setting_value"])


def set_setting(
    setting_key: str,
    setting_value: str,
    db_path: str | Path | None = None,
) -> None:
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO settings (setting_key, setting_value)
            VALUES (?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value = excluded.setting_value
            """,
            (str(setting_key), str(setting_value)),
        )


def get_bool_setting(
    setting_key: str,
    default: bool = False,
    db_path: str | Path | None = None,
) -> bool:
    raw_value = get_setting(setting_key, None, db_path)
    if raw_value is None:
        return default
    return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}


def set_bool_setting(
    setting_key: str,
    setting_value: bool,
    db_path: str | Path | None = None,
) -> None:
    set_setting(setting_key, "1" if setting_value else "0", db_path)


def _normalize_rule_set_input(rule_set: Mapping[str, Any]) -> dict[str, Any]:
    expunged_mode = str(rule_set.get("expunged_mode", "any") or "any").strip().lower()
    if expunged_mode not in {"any", "yes", "no"}:
        expunged_mode = "any"

    return {
        "name": str(rule_set.get("name", "") or "").strip() or "Untitled Rule Set",
        "is_active": 1 if bool(rule_set.get("is_active", False)) else 0,
        "auto_download": 1 if bool(rule_set.get("auto_download", True)) else 0,
        "match_category": str(rule_set.get("match_category", "") or "").strip(),
        "match_favorite_category": str(rule_set.get("match_favorite_category", "") or "").strip(),
        "match_folder_slot": _safe_int(rule_set.get("match_folder_slot")),
        "uploader_contains": str(rule_set.get("uploader_contains", "") or "").strip(),
        "title_contains": str(rule_set.get("title_contains", "") or "").strip(),
        "tags_contains": str(rule_set.get("tags_contains", "") or "").strip(),
        "tags_not_contains": str(rule_set.get("tags_not_contains", "") or "").strip(),
        "notes_contains": str(rule_set.get("notes_contains", "") or "").strip(),
        "notes_not_contains": str(rule_set.get("notes_not_contains", "") or "").strip(),
        "rating_min": _safe_float(rule_set.get("rating_min")),
        "rating_max": _safe_float(rule_set.get("rating_max")),
        "filesize_min_bytes": _safe_int(rule_set.get("filesize_min_bytes")),
        "filesize_max_bytes": _safe_int(rule_set.get("filesize_max_bytes")),
        "filecount_min": _safe_int(rule_set.get("filecount_min")),
        "filecount_max": _safe_int(rule_set.get("filecount_max")),
        "posted_from_epoch": _safe_int(rule_set.get("posted_from_epoch")),
        "posted_to_epoch": _safe_int(rule_set.get("posted_to_epoch")),
        "expunged_mode": expunged_mode,
        "max_external_gp": _safe_int(rule_set.get("max_external_gp")),
    }


def _normalize_rule_set_row(row: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(row)
    record["is_active"] = bool(record.get("is_active", 0))
    record["auto_download"] = bool(record.get("auto_download", 0))
    record["match_folder_slot"] = _safe_int(record.get("match_folder_slot"))
    record["rating_min"] = _safe_float(record.get("rating_min"))
    record["rating_max"] = _safe_float(record.get("rating_max"))
    record["filesize_min_bytes"] = _safe_int(record.get("filesize_min_bytes"))
    record["filesize_max_bytes"] = _safe_int(record.get("filesize_max_bytes"))
    record["filecount_min"] = _safe_int(record.get("filecount_min"))
    record["filecount_max"] = _safe_int(record.get("filecount_max"))
    record["posted_from_epoch"] = _safe_int(record.get("posted_from_epoch"))
    record["posted_to_epoch"] = _safe_int(record.get("posted_to_epoch"))
    record["max_external_gp"] = _safe_int(record.get("max_external_gp"))
    record["tags_not_contains"] = str(record.get("tags_not_contains", "") or "")
    record["notes_contains"] = str(record.get("notes_contains", "") or "")
    record["notes_not_contains"] = str(record.get("notes_not_contains", "") or "")
    record["created_epoch"] = _safe_int(record.get("created_epoch")) or 0
    record["expunged_mode"] = str(record.get("expunged_mode", "any") or "any").strip().lower()
    return record


def _stringify_tags(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value
        if isinstance(parsed, list):
            return " | ".join(str(tag) for tag in parsed)
        return value
    if isinstance(value, list):
        return " | ".join(str(tag) for tag in value)
    return str(value or "")


def _safe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_item_titles(db_path: str | Path | None = None) -> int:
    updated_rows = 0
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT id, title, title_jpn, uploader FROM items"
        ).fetchall()
        for row in rows:
            decoded_title = unescape(str(row["title"]))
            decoded_title_jpn = unescape(str(row["title_jpn"]))
            decoded_uploader = unescape(str(row["uploader"]))
            if (
                decoded_title != row["title"]
                or decoded_title_jpn != row["title_jpn"]
                or decoded_uploader != row["uploader"]
            ):
                connection.execute(
                    """
                    UPDATE items
                    SET title = ?, title_jpn = ?, uploader = ?
                    WHERE id = ?
                    """,
                    (
                        decoded_title,
                        decoded_title_jpn,
                        decoded_uploader,
                        row["id"],
                    ),
                )
                updated_rows += 1
    return updated_rows


def normalize_torrent_names(db_path: str | Path | None = None) -> int:
    updated_rows = 0
    with get_connection(db_path) as connection:
        rows = connection.execute("SELECT hash_string, name FROM torrents").fetchall()
        for row in rows:
            decoded_name = unescape(str(row["name"]))
            if decoded_name != row["name"]:
                connection.execute(
                    "UPDATE torrents SET name = ? WHERE hash_string = ?",
                    (decoded_name, row["hash_string"]),
                )
                updated_rows += 1
    return updated_rows
