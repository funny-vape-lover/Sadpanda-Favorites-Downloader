from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Protocol

from database import (
    get_active_rule_sets,
    get_bool_setting,
    get_category_rule_map,
    get_connection,
    get_item,
    item_matches_rule_set,
    record_error,
    resolve_error,
    update_archive_auto_queue,
    update_item_archive_path,
    update_item_external_selection,
    set_archive_cancel_requested,
    update_item_download_flags,
    update_media_primary_source,
    update_item_progress,
    update_item_status,
)


FALLBACK_SETTING_KEY = "external_archive_fallback_enabled"
EXTERNAL_ACTIVE_STATUS = "downloading_external"
AUTO_ARCHIVE_RETRY_COOLDOWN_SECONDS = 12 * 60 * 60
EXTERNAL_FAILURE_ERROR_TYPE = "external_archive_failed"


class ExternalAdapter(Protocol):
    def handle_item(
        self,
        item: dict[str, Any],
        save_dir: str,
        progress_callback=None,
        cancel_check=None,
    ) -> Any:
        ...


def get_external_queue(
    db_path: str | Path,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    now_epoch = int(time.time())
    fallback_enabled = get_bool_setting(FALLBACK_SETTING_KEY, False, db_path)
    active_rule_sets = get_active_rule_sets(db_path)
    legacy_category_rule_map = get_category_rule_map(db_path) if not active_rule_sets else {}

    with get_connection(db_path) as connection:
        if fallback_enabled:
            rows = connection.execute(
                """
                SELECT
                    items.id,
                    items.title,
                    items.title_jpn,
                    items.uploader,
                    items.item_url,
                    items.site_category,
                    items.favorite_category,
                    items.custom_folder_slot,
                    items.gallery_filesize,
                    items.filecount,
                    items.rating,
                    items.expunged,
                    items.tags,
                    items.progress_pct,
                    items.last_updated_epoch,
                    items.local_status,
                    items.thumbnail_local_path,
                    items.external_method,
                    items.external_label,
                    items.external_size_text,
                    items.external_cost_gp,
                    items.archive_path,
                    items.archive_cancel_requested,
                    items.archive_auto_queue,
                    items.archive_retry_after_epoch,
                    items.archive_last_auto_error,
                    items.torrent_downloaded,
                    items.media_primary_source,
                    COUNT(torrents.hash_string) AS torrent_count
                FROM items
                LEFT JOIN torrents ON torrents.parent_item_id = items.id
                WHERE (
                    (items.local_status IN ('force_external', 'downloading_external') AND items.archive_downloaded = 0)
                    OR (
                        items.archive_auto_queue = 1
                        AND items.archive_downloaded = 0
                        AND COALESCE(items.archive_retry_after_epoch, 0) <= ?
                    )
                    OR (
                        items.local_status IN ('new', 'queued')
                        AND items.archive_downloaded = 0
                        AND items.torrent_downloaded = 0
                    )
                )
                  AND COALESCE(items.is_latest_revision, 1) = 1
                GROUP BY
                    items.id,
                    items.title,
                    items.title_jpn,
                    items.uploader,
                    items.item_url,
                    items.site_category,
                    items.favorite_category,
                    items.custom_folder_slot,
                    items.gallery_filesize,
                    items.filecount,
                    items.rating,
                    items.expunged,
                    items.tags,
                    items.progress_pct,
                    items.last_updated_epoch,
                    items.local_status,
                    items.thumbnail_local_path
                    ,
                    items.external_method,
                    items.external_label,
                    items.external_size_text,
                    items.external_cost_gp,
                    items.archive_path,
                    items.archive_cancel_requested,
                    items.archive_auto_queue,
                    items.archive_retry_after_epoch,
                    items.archive_last_auto_error,
                    items.torrent_downloaded,
                    items.media_primary_source
                HAVING
                    items.local_status IN ('force_external', 'downloading_external')
                    OR items.archive_auto_queue = 1
                    OR COUNT(torrents.hash_string) = 0
                ORDER BY
                    CASE
                        WHEN items.local_status IN ('force_external', 'downloading_external') THEN 0
                        WHEN items.archive_auto_queue = 1 THEN 1
                        ELSE 1
                    END ASC,
                    items.last_updated_epoch ASC,
                    items.title COLLATE NOCASE ASC
                """
                ,
                (now_epoch,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT
                    items.id,
                    items.title,
                    items.title_jpn,
                    items.uploader,
                    items.item_url,
                    items.site_category,
                    items.favorite_category,
                    items.custom_folder_slot,
                    items.gallery_filesize,
                    items.filecount,
                    items.rating,
                    items.expunged,
                    items.tags,
                    items.progress_pct,
                    items.last_updated_epoch,
                    items.local_status,
                    items.thumbnail_local_path,
                    items.external_method,
                    items.external_label,
                    items.external_size_text,
                    items.external_cost_gp,
                    items.archive_path,
                    items.archive_cancel_requested,
                    items.archive_auto_queue,
                    items.archive_retry_after_epoch,
                    items.archive_last_auto_error,
                    items.torrent_downloaded,
                    items.media_primary_source,
                    COUNT(torrents.hash_string) AS torrent_count
                FROM items
                LEFT JOIN torrents ON torrents.parent_item_id = items.id
                WHERE (
                    items.local_status IN ('force_external', 'downloading_external')
                    OR (
                        items.archive_auto_queue = 1
                        AND items.archive_downloaded = 0
                        AND COALESCE(items.archive_retry_after_epoch, 0) <= ?
                    )
                )
                  AND items.archive_downloaded = 0
                  AND COALESCE(items.is_latest_revision, 1) = 1
                GROUP BY
                    items.id,
                    items.title,
                    items.item_url,
                    items.site_category,
                    items.custom_folder_slot,
                    items.gallery_filesize,
                    items.progress_pct,
                    items.last_updated_epoch,
                    items.local_status,
                    items.thumbnail_local_path,
                    items.external_method,
                    items.external_label,
                    items.external_size_text,
                    items.external_cost_gp,
                    items.archive_path,
                    items.archive_cancel_requested,
                    items.archive_auto_queue,
                    items.archive_retry_after_epoch,
                    items.archive_last_auto_error,
                    items.torrent_downloaded,
                    items.media_primary_source
                ORDER BY
                    CASE
                        WHEN items.local_status IN ('force_external', 'downloading_external') THEN 0
                        WHEN items.archive_auto_queue = 1 THEN 1
                        ELSE 2
                    END ASC,
                    items.last_updated_epoch ASC,
                    items.title COLLATE NOCASE ASC
                """
                ,
                (now_epoch,),
            ).fetchall()

    queue: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["is_auto_archive_queue"] = bool(item.get("archive_auto_queue", 0))
        if bool(item.get("archive_cancel_requested")):
            continue
        if item["local_status"] in {"force_external", EXTERNAL_ACTIVE_STATUS}:
            queue.append(item)
            continue
        if bool(item.get("archive_auto_queue", 0)):
            queue.append(item)
            continue
        if int(item.get("torrent_count", 0) or 0) != 0:
            continue
        matched_rule_sets = _matching_active_auto_rule_sets(item, active_rule_sets)
        if matched_rule_sets:
            numeric_caps = [
                int(rule_set["max_external_gp"])
                for rule_set in matched_rule_sets
                if rule_set.get("max_external_gp") is not None
            ]
            item["max_external_gp"] = max(numeric_caps) if numeric_caps else None
            queue.append(item)
            continue
        if not active_rule_sets and _matches_auto_download_rule(item, active_rule_sets, legacy_category_rule_map):
            item["max_external_gp"] = None
            queue.append(item)

    return queue


def process_external_queue(
    db_path: str | Path,
    config: dict[str, Any],
    external_adapter: ExternalAdapter,
) -> list[dict[str, Any]]:
    save_dir = _resolve_external_save_dir(config)
    actions: list[dict[str, Any]] = []
    now_epoch = int(time.time())

    for item in get_external_queue(db_path, config):
        is_auto_queue = bool(item.get("is_auto_archive_queue", False)) and bool(item.get("torrent_downloaded", False))
        original_status = str(item.get("local_status", "") or "").strip().lower()
        if not is_auto_queue:
            update_item_progress(item["id"], 0.0, db_path)
            update_item_status(item["id"], EXTERNAL_ACTIVE_STATUS, db_path=db_path)
        set_archive_cancel_requested(item["id"], False, db_path)
        result = external_adapter.handle_item(
            item,
            save_dir,
            progress_callback=lambda progress_pct, item_id=item["id"]: update_item_progress(
                item_id,
                progress_pct,
                db_path,
            ),
            cancel_check=lambda item_id=item["id"]: _is_archive_cancel_requested(item_id, db_path),
        )
        action = {
            "item_id": item["id"],
            "title": item["title"],
            "save_dir": save_dir,
            "result": result,
            "local_status": item.get("local_status", ""),
            "is_auto_archive_queue": is_auto_queue,
        }
        actions.append(action)

        if _is_success(result):
            if isinstance(result, dict):
                update_item_external_selection(
                    item["id"],
                    external_method=str(result.get("method", "")),
                    external_label=str(result.get("label", "")),
                    external_size_text=str(result.get("size_text", "")),
                    external_cost_gp=result.get("cost_gp"),
                    db_path=db_path,
                )
                if result.get("path"):
                    update_item_archive_path(item["id"], result["path"], db_path=db_path)
            update_item_download_flags(item["id"], archive_downloaded=True, db_path=db_path)
            if not is_auto_queue:
                update_item_progress(item["id"], 100.0, db_path)
            set_archive_cancel_requested(item["id"], False, db_path)
            if is_auto_queue and bool(item.get("torrent_downloaded", False)):
                if original_status in {"seeding", "completed", "complete"}:
                    update_item_status(
                        item["id"],
                        original_status if original_status != "complete" else "completed",
                        db_path=db_path,
                    )
                else:
                    update_item_status(item["id"], "completed", db_path=db_path)
                update_media_primary_source(item["id"], "torrent", db_path=db_path)
            else:
                update_item_status(item["id"], "completed", db_path=db_path)
                if not bool(item.get("torrent_downloaded", False)):
                    update_media_primary_source(item["id"], "archive", db_path=db_path)
            update_archive_auto_queue(
                item["id"],
                enabled=False,
                retry_after_epoch=0,
                last_error="",
                db_path=db_path,
            )
            resolve_error(
                item_id=item["id"],
                error_type=EXTERNAL_FAILURE_ERROR_TYPE,
                db_path=db_path,
            )
            print(
                f"External import completed for {item['id']} ({_safe_text(item['title'])}). "
                f"Status transition: {item['local_status']} -> completed"
            )
        else:
            failure_reason = _format_external_failure_reason(result)
            was_cancelled = (
                isinstance(result, dict)
                and str(result.get("reason", "")).strip().lower() == "cancelled"
            )
            if not is_auto_queue:
                update_item_progress(item["id"], 0.0, db_path)
            set_archive_cancel_requested(item["id"], False, db_path)
            update_item_archive_path(item["id"], "", db_path=db_path)
            if is_auto_queue:
                request_was_paid = _result_is_paid_submitted_request(result)
                update_archive_auto_queue(
                    item["id"],
                    enabled=not request_was_paid and not was_cancelled,
                    retry_after_epoch=now_epoch + AUTO_ARCHIVE_RETRY_COOLDOWN_SECONDS,
                    last_error="" if was_cancelled else failure_reason,
                    db_path=db_path,
                )
                if original_status in {"seeding", "completed", "complete"}:
                    update_item_status(
                        item["id"],
                        original_status if original_status != "complete" else "completed",
                        db_path=db_path,
                    )
            else:
                next_status = _resolve_external_failure_status(item["id"], db_path, result)
                if next_status:
                    update_item_status(
                        item["id"],
                        next_status,
                        error_message=failure_reason if next_status == "error" else None,
                        db_path=db_path,
                    )
                update_archive_auto_queue(
                    item["id"],
                    enabled=False,
                    retry_after_epoch=now_epoch + AUTO_ARCHIVE_RETRY_COOLDOWN_SECONDS,
                    last_error="" if was_cancelled else failure_reason,
                    db_path=db_path,
                )
            if not was_cancelled:
                record_error(
                    item_id=item["id"],
                    error_type=EXTERNAL_FAILURE_ERROR_TYPE,
                    message=failure_reason,
                    fix_hint=(
                        "Review the response and manually queue Original Archive or "
                        "Resample Archive when it is safe to retry."
                    ),
                    db_path=db_path,
                )
            detail = ""
            if isinstance(result, dict):
                reason = str(result.get("reason", "") or "").strip()
                message = str(result.get("message", "") or "").strip()
                parts = [part for part in [reason, message] if part]
                if parts:
                    detail = f" Details: {' | '.join(parts)}"
            print(
                f"External import failed or was skipped for {item['id']} "
                f"({_safe_text(item['title'])}).{detail}"
            )

    return actions


def reconcile_external_imports(
    db_path: str | Path,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    archive_library = Path(_resolve_external_save_dir(config)).expanduser()
    archive_library.mkdir(parents=True, exist_ok=True)

    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                gallery_filesize,
                local_status,
                archive_downloaded,
                torrent_downloaded,
                external_method,
                archive_path
            FROM items
            WHERE archive_downloaded = 0
              AND (
                  TRIM(COALESCE(external_method, '')) != ''
                  OR local_status IN ('force_external', 'downloading_external')
              )
            ORDER BY last_updated_epoch ASC, title COLLATE NOCASE ASC
            """
        ).fetchall()

    updates: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        match_path = _find_existing_archive(archive_library, item)
        if match_path is None:
            continue

        size_bytes = _measure_path_size(match_path)
        if match_path.is_file() and _looks_like_html_file(match_path):
            continue
        if size_bytes <= 1024:
            continue

        if str(item.get("archive_path") or "") != str(match_path):
            update_item_archive_path(item["id"], str(match_path), db_path=db_path)

        update_item_download_flags(item["id"], archive_downloaded=True, db_path=db_path)
        if not bool(item.get("torrent_downloaded", False)):
            update_media_primary_source(item["id"], "archive", db_path=db_path)
        update_archive_auto_queue(
            item["id"],
            enabled=False,
            retry_after_epoch=0,
            last_error="",
            db_path=db_path,
        )
        update_item_progress(item["id"], 100.0, db_path=db_path)
        update_item_status(item["id"], "completed", db_path=db_path)
        updates.append(
            {
                "item_id": item["id"],
                "title": item["title"],
                "path": str(match_path),
                "size_bytes": size_bytes,
            }
        )
        print(
            f"Recovered external archive source for {item['id']} "
            f"({_safe_text(item['title'])}) from {_safe_text(match_path)}."
        )

    return updates


def reconcile_missing_external_imports(
    db_path: str | Path,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    archive_library = Path(_resolve_external_save_dir(config)).expanduser()
    archive_library.mkdir(parents=True, exist_ok=True)

    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                gallery_filesize,
                local_status,
                archive_downloaded,
                external_method,
                archive_path,
                torrent_downloaded,
                archive_cancel_requested
            FROM items
            WHERE archive_downloaded = 1
              AND TRIM(COALESCE(external_method, '')) != ''
            ORDER BY last_updated_epoch ASC, title COLLATE NOCASE ASC
            """
        ).fetchall()

    updates: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        match_path = _find_existing_archive(archive_library, item)

        is_valid = False
        if match_path is not None and match_path.is_file():
            size_bytes = _measure_path_size(match_path)
            looks_like_html = _looks_like_html_file(match_path)
            if not looks_like_html and size_bytes > 1024:
                is_valid = True

        if is_valid:
            continue

        update_item_download_flags(item["id"], archive_downloaded=False, db_path=db_path)
        if bool(item.get("archive_cancel_requested")):
            set_archive_cancel_requested(item["id"], False, db_path)
            update_item_progress(item["id"], 0.0, db_path)
            update_item_status(
                item["id"],
                _resolve_cancel_target_status(item),
                db_path=db_path,
            )
        elif str(item.get("external_method", "")).strip() and not bool(item.get("torrent_downloaded", False)):
            update_item_status(item["id"], "force_external", db_path=db_path)
        else:
            update_item_progress(item["id"], 0.0, db_path)
            update_item_status(
                item["id"],
                _resolve_cancel_target_status(item),
                db_path=db_path,
            )

        updates.append(
            {
                "item_id": item["id"],
                "title": item["title"],
                "path": str(match_path) if match_path is not None else "",
                "action": "reset_missing_or_invalid_archive",
            }
        )
        print(
            f"Reset invalid external archive state for {item['id']} "
            f"({_safe_text(item['title'])})."
        )

    return updates


def clear_misattributed_archive_flags(
    db_path: str | Path,
) -> list[dict[str, Any]]:
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
            WHERE archive_downloaded = 1
              AND torrent_downloaded = 1
              AND TRIM(COALESCE(external_method, '')) = ''
            ORDER BY last_updated_epoch ASC, title COLLATE NOCASE ASC
            """
        ).fetchall()

    updates: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        update_item_download_flags(item["id"], archive_downloaded=False, db_path=db_path)
        updates.append(
            {
                "item_id": item["id"],
                "title": item["title"],
                "action": "cleared_misattributed_archive_flag",
            }
        )
        print(
            f"Cleared misattributed archive flag for {item['id']} "
            f"({_safe_text(item['title'])})."
        )

    return updates


def _resolve_external_save_dir(config: dict[str, Any]) -> str:
    paths_config = config.get("paths", {})
    if isinstance(paths_config, dict):
        archive_library = paths_config.get("archive_library") or paths_config.get("archive_downloads")
        if isinstance(archive_library, str) and archive_library.strip():
            return archive_library.strip()

        external_archive = paths_config.get("external_archive")
        if isinstance(external_archive, str) and external_archive.strip():
            return external_archive.strip()

        media_library = paths_config.get("media_library")
        if isinstance(media_library, str) and media_library.strip():
            media_path = Path(media_library).expanduser()
            return str(media_path.parent / f"{media_path.name}_Archives")

    return str(Path(".").resolve() / "archive_downloads")


def _find_existing_archive(media_library: Path, item: dict[str, Any]) -> Path | None:
    stored_path = str(item.get("archive_path", "") or "").strip()
    if stored_path:
        path = Path(stored_path)
        if not path.is_absolute():
            path = media_library / path
        if path.exists():
            return path

    item_id = str(item.get("id", "") or "")
    title = str(item.get("title", "") or "")
    normalized_title = _normalize_match_key(title)
    normalized_item_id = _normalize_match_key(item_id)

    candidates: list[Path] = []
    for candidate in media_library.iterdir():
        if not candidate.is_file():
            continue
        candidate_name = candidate.name
        candidate_stem = candidate.stem
        normalized_name = _normalize_match_key(candidate_name)
        normalized_stem = _normalize_match_key(candidate_stem)

        if normalized_item_id and normalized_name.startswith(normalized_item_id):
            candidates.append(candidate)
            continue
        if normalized_title and (
            normalized_stem == normalized_title
            or normalized_stem.startswith(normalized_title)
            or normalized_title in normalized_stem
        ):
            candidates.append(candidate)
            continue

    if not candidates:
        return None

    try:
        return max(candidates, key=lambda candidate: candidate.stat().st_size)
    except OSError:
        return candidates[0]


def _measure_path_size(path: Path) -> int:
    if path.is_file():
        return int(path.stat().st_size)
    return sum(int(candidate.stat().st_size) for candidate in path.rglob("*") if candidate.is_file())


def _sanitize_component(value: Any) -> str:
    invalid_chars = '<>:"/\\|?*'
    cleaned = str(value).replace("\0", " ")
    for char in invalid_chars:
        cleaned = cleaned.replace(char, " ")
    return " ".join(cleaned.split()).strip().rstrip(".") or "untitled"


def _normalize_match_key(value: Any) -> str:
    sanitized = _sanitize_component(value)
    return "".join(ch for ch in sanitized.casefold() if ch.isalnum())


def _is_success(result: Any) -> bool:
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        if "success" in result:
            return bool(result.get("success"))
        if "ok" in result:
            return bool(result.get("ok"))
    return bool(result)


def _is_archive_cancel_requested(
    item_id: str,
    db_path: str | Path,
) -> bool:
    item = get_item(item_id, db_path)
    return bool(item and item.get("archive_cancel_requested"))


def _resolve_cancel_target_status(item: dict[str, Any]) -> str:
    current_status = str(item.get("local_status", "") or "").strip().lower()
    if current_status in {"force_queued", "downloading", "complete", "seeding", "completed", "archived", "error"}:
        return current_status
    if bool(item.get("torrent_downloaded", False)):
        return "completed"
    return "new"


def _resolve_external_failure_status(
    item_id: str,
    db_path: str | Path,
    result: Any,
) -> str | None:
    current_item = get_item(item_id, db_path) or {}
    if isinstance(result, dict) and str(result.get("reason", "")).strip().lower() == "cancelled":
        return _resolve_cancel_target_status(current_item)

    current_status = str(current_item.get("local_status", "") or "").strip().lower()
    if current_status not in {EXTERNAL_ACTIVE_STATUS, "force_external"}:
        return None
    return "error"


def _result_is_paid_submitted_request(result: Any) -> bool:
    if not isinstance(result, dict) or not bool(result.get("request_submitted")):
        return False
    try:
        return int(result.get("cost_gp") or 0) > 0
    except (TypeError, ValueError):
        return False


def _format_external_failure_reason(result: Any) -> str:
    if isinstance(result, dict):
        reason = str(result.get("reason", "") or "").strip()
        message = str(result.get("message", "") or "").strip()
        combined = " | ".join(part for part in [reason, message] if part)
        return combined or "external download failed"
    return str(result or "external download failed")


def _looks_like_html_file(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            prefix = handle.read(512).lstrip().lower()
    except OSError:
        return False
    return (
        prefix.startswith(b"<!doctype html")
        or prefix.startswith(b"<html")
        or b"<body" in prefix
    )


def _safe_text(value: Any) -> str:
    text = str(value)
    try:
        text.encode("cp1252")
        return text
    except UnicodeEncodeError:
        return text.encode("cp1252", errors="replace").decode("cp1252")


def _matches_auto_download_rule(
    item: dict[str, Any],
    active_rule_sets: list[dict[str, Any]],
    legacy_category_rule_map: dict[str, bool],
) -> bool:
    if active_rule_sets:
        return bool(_matching_active_auto_rule_sets(item, active_rule_sets))

    category = str(item.get("site_category", "") or "")
    return bool(legacy_category_rule_map.get(category, False))


def _matching_active_auto_rule_sets(
    item: dict[str, Any],
    active_rule_sets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for rule_set in active_rule_sets:
        if not bool(rule_set.get("auto_download", True)):
            continue
        if item_matches_rule_set(item, rule_set):
            matches.append(rule_set)
    return matches
