from __future__ import annotations

import base64
import json
from datetime import date, datetime, time as dt_time, timezone
from html import unescape
from mimetypes import guess_type
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import download_manager
import external_manager
import external_adapter
import media_importer
import qbittorrent_adapter
import torrent_uploader
import worker
from lanraragi_sync import (
    LANRARAGI_FORCE_SYNC_KEY,
    LANRARAGI_LAST_STATUS_KEY,
    LANRARAGI_LAST_SYNC_EPOCH_KEY,
)
from database import (
    DEFAULT_DB_PATH,
    delete_item,
    delete_rule_set,
    get_active_rule_sets,
    get_all_items_for_ui,
    get_all_rule_sets,
    get_bool_setting,
    get_item,
    get_open_errors,
    get_setting,
    get_torrents_for_item,
    init_db,
    item_matches_rule_set,
    record_error,
    resolve_error,
    save_rule_set,
    set_bool_setting,
    set_active_rule_set,
    set_rule_set_active,
    set_archive_cancel_requested,
    set_setting,
    update_item_download_flags,
    update_item_external_selection,
    update_item_progress,
    update_item_status,
)
from external_manager import FALLBACK_SETTING_KEY


APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = DEFAULT_DB_PATH
VISIBLE_COLUMNS_SETTING_KEY = "items_table_visible_columns"
ARCHIVE_DIALOG_ITEM_KEY = "archive_dialog_item_id"
ARCHIVE_OPTIONS_CACHE_KEY = "archive_options_cache"
STATUS_OPTIONS = [
    "new",
    "force_queued",
    "force_external",
    "downloading_external",
    "downloading",
    "seeding",
    "completed",
    "archived",
    "error",
]
FULL_RESCAN_INTERVAL_KEY = "favorites_full_rescan_interval_hours"
FAVORITES_RESCAN_FLAG_KEY = "favorites_rescan_requested"
FAVORITES_LAST_REFRESH_KEY = "favorites_last_refresh_epoch"
FAVORITES_RESCAN_STATUS_KEY = "favorites_rescan_status_json"
CONFIG_READY_FLAG_KEY = "runtime_config_ready"
FULL_RESCAN_DEFAULT_HOURS = 24
LANRARAGI_SYNC_DEFAULT_MINUTES = 60
SEARCH_TEXT_KEY = "search_text"
SEARCH_TEXT_PENDING_KEY = "search_text_pending"
TABLE_SORT_FIELD_KEY = "items_table_default_sort_field"
TABLE_SORT_DESC_KEY = "items_table_default_sort_desc"
TABLE_SORT_DEFAULT_FIELD = "favorited_epoch"
TABLE_SORT_DEFAULT_DESC = True
LIVE_REFRESH_SECONDS = 8
DEFAULT_VISIBLE_COLUMNS = [
    "title",
    "title_jpn",
    "uploader",
    "favorite_category",
    "favorited_time",
    "custom_folder_slot",
    "site_category",
    "gallery_filesize",
    "filecount",
    "torrent_downloaded",
    "archive_downloaded",
    "torrent_count",
    "best_torrent_name",
    "local_status",
    "last_updated",
    "id",
]
TABLE_COLUMN_LABELS = {
    "title": "Title",
    "title_jpn": "Japanese Title",
    "uploader": "Uploader",
    "favorite_category": "Favorite Category",
    "favorite_note": "Note",
    "favorited_time": "Favorited Time",
    "custom_folder_slot": "Fav Slot",
    "site_category": "Gallery Category",
    "gallery_filesize": "Gallery Size",
    "filecount": "Files",
    "rating": "Rating",
    "expunged": "Expunged",
    "torrent_downloaded": "Torrent Downloaded",
    "torrent_from_archive": "User Created Torrent",
    "archive_downloaded": "Archive Downloaded",
    "torrent_count": "Torrent Count",
    "best_torrent_name": "Best Torrent",
    "local_status": "Status",
    "matches_active_rule": "Matched By Active Rule",
    "last_updated": "Last Updated",
    "parent_gid": "Parent GID",
    "first_gid": "First GID",
    "is_latest_revision": "Latest Revision",
    "superseded_by_id": "Superseded By",
    "id": "ID",
    "tags": "Tags",
}
TABLE_SORT_LABEL_TO_FIELD = {
    "Favorited Time": "favorited_epoch",
    "Last Updated": "last_updated_epoch",
    "Title": "title",
    "Current GID": "id",
}

STATUS_DISPLAY_LABELS = {
    "new": "new",
    "queued": "queued",
    "force_queued": "queued for torrent",
    "force_external": "queued for archive",
    "downloading_external": "downloading archive",
    "downloading": "downloading",
    "complete": "processing",
    "completed": "completed",
    "imported": "completed",
    "seeding": "seeding",
    "archived": "archived",
    "error": "error",
}


def display_status(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return STATUS_DISPLAY_LABELS.get(raw, raw or "n/a")


def default_status_after_archive_cancel(item: dict[str, Any]) -> str:
    current_status = str(item.get("local_status", "") or "").strip().lower()
    if current_status in {"force_queued", "downloading", "complete", "seeding", "completed", "archived", "error"}:
        return current_status
    if bool(item.get("torrent_downloaded", False)):
        return "completed"
    return "new"


def bytes_label(num_bytes: int | float) -> str:
    value = float(num_bytes or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{num_bytes} B"


def normalize_epoch_seconds(epoch_value: Any) -> int | None:
    if epoch_value in (None, "", 0, "0"):
        return None
    try:
        numeric = int(float(epoch_value))
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    if numeric >= 1_000_000_000_000:
        numeric //= 1000
    return numeric


def epoch_label(epoch_value: int | float | None) -> str:
    normalized_epoch = normalize_epoch_seconds(epoch_value)
    if normalized_epoch is None:
        return "n/a"
    return (
        datetime.fromtimestamp(float(normalized_epoch), tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def epoch_date_value(epoch_value: int | float | None) -> date | None:
    normalized_epoch = normalize_epoch_seconds(epoch_value)
    if normalized_epoch is None:
        return None
    return datetime.fromtimestamp(float(normalized_epoch), tz=timezone.utc).date()


def epoch_datetime_value(epoch_value: int | float | None) -> datetime | None:
    normalized_epoch = normalize_epoch_seconds(epoch_value)
    if normalized_epoch is None:
        return None
    return (
        datetime.fromtimestamp(float(normalized_epoch), tz=timezone.utc)
        .astimezone()
        .replace(tzinfo=None)
    )


def optional_text(value: Any) -> str:
    return str(value or "")


def optional_int_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(int(value))


def optional_float_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return f"{float(value):g}"


def megabytes_to_bytes(value: str) -> int | None:
    raw = value.strip()
    if not raw:
        return None
    return int(float(raw) * 1024 * 1024)


def parse_optional_int(value: str, label: str) -> int | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{label} must be a whole number.") from exc


def parse_optional_float(value: str, label: str) -> float | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number.") from exc


def parse_optional_megabytes(value: str, label: str) -> int | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        return megabytes_to_bytes(raw)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number in MB.") from exc


def date_to_start_epoch(value: date | None) -> int | None:
    if value is None:
        return None
    return int(datetime.combine(value, dt_time.min, tzinfo=timezone.utc).timestamp())


def date_to_end_epoch(value: date | None) -> int | None:
    if value is None:
        return None
    return int(datetime.combine(value, dt_time.max, tzinfo=timezone.utc).timestamp())


def describe_rule_set(rule_set: dict[str, Any]) -> str:
    parts: list[str] = []
    if rule_set.get("match_category"):
        parts.append(f"Category: {rule_set['match_category']}")
    if rule_set.get("match_favorite_category"):
        parts.append(f"Favorite: {rule_set['match_favorite_category']}")
    if rule_set.get("match_folder_slot") is not None:
        parts.append(f"Fav Slot: {rule_set['match_folder_slot']}")
    if rule_set.get("uploader_contains"):
        parts.append(f"Uploader: {rule_set['uploader_contains']}")
    if rule_set.get("title_contains"):
        parts.append(f"Title: {rule_set['title_contains']}")
    if rule_set.get("tags_contains"):
        parts.append(f"Tag: {rule_set['tags_contains']}")
    if rule_set.get("rating_min") is not None or rule_set.get("rating_max") is not None:
        parts.append(
            f"Rating: {optional_float_text(rule_set.get('rating_min')) or '-inf'}"
            f" to {optional_float_text(rule_set.get('rating_max')) or '+inf'}"
        )
    if rule_set.get("filesize_min_bytes") is not None or rule_set.get("filesize_max_bytes") is not None:
        min_mb = (
            f"{rule_set['filesize_min_bytes'] / (1024 * 1024):g}"
            if rule_set.get("filesize_min_bytes") is not None
            else "0"
        )
        max_mb = (
            f"{rule_set['filesize_max_bytes'] / (1024 * 1024):g}"
            if rule_set.get("filesize_max_bytes") is not None
            else "inf"
        )
        parts.append(f"Filesize MB: {min_mb} to {max_mb}")
    if rule_set.get("filecount_min") is not None or rule_set.get("filecount_max") is not None:
        parts.append(
            f"Files: {optional_int_text(rule_set.get('filecount_min')) or '0'}"
            f" to {optional_int_text(rule_set.get('filecount_max')) or 'inf'}"
        )
    if rule_set.get("posted_from_epoch") or rule_set.get("posted_to_epoch"):
        parts.append(
            f"Posted: {epoch_date_value(rule_set.get('posted_from_epoch')) or 'start'}"
            f" to {epoch_date_value(rule_set.get('posted_to_epoch')) or 'now'}"
        )
    if rule_set.get("expunged_mode") and rule_set.get("expunged_mode") != "any":
        parts.append(f"Expunged: {rule_set['expunged_mode']}")
    if rule_set.get("max_external_gp") is not None:
        parts.append(f"Max External GP: {int(rule_set['max_external_gp'])}")
    return " | ".join(parts) if parts else "No filters"


def summarize(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "count": len(items),
        "total_size": sum(int(item.get("gallery_filesize", 0) or 0) for item in items),
        "torrent_items": sum(1 for item in items if item.get("has_torrent")),
    }


def normalize_progress_pct(value: int | float | None) -> float:
    try:
        progress = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(progress, 100.0))


def resolve_thumbnail_path(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        path = APP_DIR / path
    return path


def build_table(items: list[dict[str, Any]]) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for item in items:
        thumbnail_data = ""
        thumb_path = resolve_thumbnail_path(item.get("thumbnail_local_path"))
        if thumb_path is not None and thumb_path.exists():
            mime_type = guess_type(thumb_path.name)[0] or "image/jpeg"
            encoded = base64.b64encode(thumb_path.read_bytes()).decode("ascii")
            thumbnail_data = f"data:{mime_type};base64,{encoded}"

        records.append(
            {
                "thumbnail": thumbnail_data,
                "title": unescape(item.get("title", "")),
                "title_jpn": unescape(item.get("title_jpn", "")),
                "uploader": unescape(item.get("uploader", "")),
                "favorite_category": item.get("favorite_category", ""),
                "favorite_note": item.get("favorite_note", ""),
                "favorite_note": item.get("favorite_note", ""),
                "favorited_time": epoch_datetime_value(item.get("favorited_epoch")),
                "item_url": item.get("item_url", ""),
                "custom_folder_slot": item.get("custom_folder_slot"),
                "site_category": item.get("site_category", ""),
                "gallery_filesize": int(item.get("gallery_filesize", 0) or 0),
                "filecount": int(item.get("filecount", 0) or 0),
                "rating": item.get("rating", ""),
                "expunged": bool(item.get("expunged", False)),
                "torrent_downloaded": bool(item.get("torrent_downloaded", False)),
                "torrent_from_archive": bool(item.get("torrent_from_archive", False)),
                "archive_downloaded": bool(item.get("archive_downloaded", False)),
                "torrent_count": int(item.get("torrent_count", 0) or 0),
                "best_torrent_name": item.get("best_torrent_name", ""),
                "local_status": display_status(item.get("local_status", "")),
                "matches_active_rule": bool(item.get("matches_active_rule", False)),
                "last_updated": epoch_datetime_value(item.get("last_updated_epoch")),
                "parent_gid": item.get("parent_gid", ""),
                "first_gid": item.get("first_gid", ""),
                "is_latest_revision": bool(item.get("is_latest_revision", True)),
                "superseded_by_id": item.get("superseded_by_id", ""),
                "tags": item.get("tags", ""),
                "id": item["id"],
            }
        )
    return pd.DataFrame(records)


def build_torrent_table(torrents: list[dict[str, Any]]) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for torrent in torrents:
        records.append(
            {
                "hash_string": torrent["hash_string"],
                "name": torrent.get("name", ""),
                "size": bytes_label(torrent.get("size", 0)),
                "added": epoch_label(torrent.get("added_epoch")),
                "is_best_candidate": bool(torrent.get("is_best_candidate", 0)),
            }
        )
    return pd.DataFrame(records)


def exportable_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exported: list[dict[str, Any]] = []
    for item in items:
        exported.append(
            {
                "id": item["id"],
                "title": unescape(item.get("title", "")),
                "title_jpn": unescape(item.get("title_jpn", "")),
                "uploader": unescape(item.get("uploader", "")),
                "site_category": item.get("site_category", ""),
                "favorite_category": item.get("favorite_category", ""),
                "favorited_epoch": int(item.get("favorited_epoch", 0) or 0),
                "item_url": item.get("item_url", ""),
                "custom_folder_slot": item.get("custom_folder_slot"),
                "gallery_filesize": int(item.get("gallery_filesize", 0) or 0),
                "filecount": int(item.get("filecount", 0) or 0),
                "rating": item.get("rating", ""),
                "expunged": bool(item.get("expunged", False)),
                "torrent_downloaded": bool(item.get("torrent_downloaded", False)),
                "torrent_from_archive": bool(item.get("torrent_from_archive", False)),
                "archive_downloaded": bool(item.get("archive_downloaded", False)),
                "tags": item.get("tags", ""),
                "parent_gid": item.get("parent_gid", ""),
                "first_gid": item.get("first_gid", ""),
                "is_latest_revision": bool(item.get("is_latest_revision", True)),
                "superseded_by_id": item.get("superseded_by_id", ""),
                "last_updated_epoch": int(item.get("last_updated_epoch", 0) or 0),
                "local_status": display_status(item.get("local_status", "")),
                "matches_active_rule": bool(item.get("matches_active_rule", False)),
                "thumbnail_local_path": item.get("thumbnail_local_path", ""),
                "external_method": item.get("external_method", ""),
                "external_label": item.get("external_label", ""),
                "external_size_text": item.get("external_size_text", ""),
                "external_cost_gp": item.get("external_cost_gp"),
                "torrent_count": int(item.get("torrent_count", 0) or 0),
                "best_torrent_name": item.get("best_torrent_name", ""),
            }
        )
    return exported


def render_thumbnail(raw_path: str | None) -> None:
    path = resolve_thumbnail_path(raw_path)
    if path is None:
        st.caption("No thumbnail path stored.")
        return
    if not path.exists():
        st.caption("Thumbnail file not found.")
        return
    st.image(str(path), width=220)


def sort_items_for_table(
    items: list[dict[str, Any]],
    sort_field: str,
    descending: bool,
) -> list[dict[str, Any]]:
    present: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for item in items:
        value = item.get(sort_field)
        if value is None or value == "":
            missing.append(item)
        else:
            present.append(item)

    def _normalize(value: Any) -> Any:
        if isinstance(value, str):
            return value.lower()
        return value

    present.sort(key=lambda row: _normalize(row.get(sort_field)), reverse=descending)
    return [*present, *missing]


def sync_selected_item(filtered_items: list[dict[str, Any]]) -> str | None:
    if not filtered_items:
        st.session_state.pop("selected_item_id", None)
        return None

    selected_id = st.session_state.get("selected_item_id")
    valid_ids = {item["id"] for item in filtered_items}
    if selected_id not in valid_ids:
        selected_id = filtered_items[0]["id"]
        st.session_state["selected_item_id"] = selected_id
    return selected_id


def get_selected_row_indices(table_event) -> list[int]:
    if hasattr(table_event, "selection"):
        rows = list(getattr(table_event.selection, "rows", []))
    elif isinstance(table_event, dict):
        rows = list(table_event.get("selection", {}).get("rows", []))
    else:
        rows = []
    return [int(row) for row in rows]


def sync_selected_item_from_table_event(
    filtered_items: list[dict[str, Any]],
    table_event,
) -> str | None:
    if not filtered_items:
        st.session_state.pop("selected_item_id", None)
        return None

    selected_row_index: int | None = None
    if hasattr(table_event, "selection"):
        cells = list(getattr(table_event.selection, "cells", []))
    elif isinstance(table_event, dict):
        cells = list(table_event.get("selection", {}).get("cells", []))
    else:
        cells = []

    if cells:
        first_cell = cells[0]
        if isinstance(first_cell, (list, tuple)) and first_cell:
            selected_row_index = int(first_cell[0])
        elif isinstance(first_cell, dict) and "row" in first_cell:
            selected_row_index = int(first_cell["row"])

    if selected_row_index is None:
        selected_rows = get_selected_row_indices(table_event)
        selected_row_index = selected_rows[0] if selected_rows else None

    if selected_row_index is not None and 0 <= selected_row_index < len(filtered_items):
        st.session_state["selected_item_id"] = filtered_items[selected_row_index]["id"]

    return sync_selected_item(filtered_items)


def filter_items(
    items: list[dict[str, Any]],
    selected_categories: list[str],
    search_text: str,
    show_superseded: bool,
    only_user_created_torrents: bool,
) -> list[dict[str, Any]]:
    filtered = [
        item
        for item in items
        if (show_superseded or bool(item.get("is_latest_revision", True)))
        and (not selected_categories or item.get("site_category") in selected_categories)
        and (not only_user_created_torrents or bool(item.get("torrent_from_archive", False)))
    ]

    query = search_text.strip()
    if not query:
        return filtered

    lowered_query = query.lower()
    if lowered_query.startswith("uploader:"):
        needle = lowered_query.split(":", 1)[1].strip()
        if not needle:
            return filtered
        return [
            item
            for item in filtered
            if needle in unescape(str(item.get("uploader", ""))).lower()
        ]

    if lowered_query.startswith("tag:"):
        needle = lowered_query.split(":", 1)[1].strip()
        if not needle:
            return filtered
        return [
            item
            for item in filtered
            if needle in str(item.get("tags", "")).lower()
        ]

    if lowered_query.startswith("note:"):
        needle = lowered_query.split(":", 1)[1].strip()
        if not needle:
            return filtered
        return [
            item
            for item in filtered
            if needle in str(item.get("favorite_note", "") or "").lower()
        ]

    if lowered_query in {
        "flag:user_created_torrent",
        "flag:user-created-torrent",
        "source:user_created_torrent",
        "source:user-created-torrent",
    }:
        return [item for item in filtered if bool(item.get("torrent_from_archive", False))]

    if lowered_query in {
        "flag:not_user_created_torrent",
        "flag:not-user-created-torrent",
        "source:not_user_created_torrent",
        "source:not-user-created-torrent",
    }:
        return [item for item in filtered if not bool(item.get("torrent_from_archive", False))]

    return [
        item
        for item in filtered
        if lowered_query in unescape(str(item.get("title", ""))).lower()
        or lowered_query in unescape(str(item.get("title_jpn", ""))).lower()
    ]


def annotate_active_rule_matches(
    items: list[dict[str, Any]],
    active_rule_sets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for item in items:
        record = dict(item)
        matched_rule_names = [
            str(rule_set.get("name", "") or "")
            for rule_set in active_rule_sets
            if bool(rule_set.get("auto_download", True)) and item_matches_rule_set(record, rule_set)
        ]
        record["matches_active_rule"] = bool(matched_rule_names)
        record["matched_rule_names"] = matched_rule_names
        annotated.append(record)
    return annotated


def parse_item_tags(raw_tags: Any) -> list[dict[str, str]]:
    if isinstance(raw_tags, str):
        try:
            parsed = json.loads(raw_tags)
        except (TypeError, ValueError):
            parsed = [raw_tags]
    elif isinstance(raw_tags, list):
        parsed = raw_tags
    else:
        parsed = [str(raw_tags or "")]

    tags: list[dict[str, str]] = []
    for entry in parsed:
        text = str(entry or "").strip()
        if not text:
            continue
        if ":" in text:
            namespace, value = text.split(":", 1)
        else:
            namespace, value = "misc", text
        namespace = namespace.strip() or "misc"
        value = value.strip()
        if not value:
            continue
        tags.append(
            {
                "raw": f"{namespace}:{value}",
                "namespace": namespace,
                "value": value,
            }
        )
    return tags


def group_tags_by_namespace(raw_tags: Any) -> list[tuple[str, list[dict[str, str]]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for tag in parse_item_tags(raw_tags):
        grouped.setdefault(tag["namespace"], []).append(tag)
    return [(namespace, grouped[namespace]) for namespace in sorted(grouped.keys())]


def queue_search_text(value: str) -> None:
    st.session_state[SEARCH_TEXT_PENDING_KEY] = value


def render_tag_groups(raw_tags: Any) -> None:
    tag_groups = group_tags_by_namespace(raw_tags)
    if not tag_groups:
        st.caption("No tags stored.")
        return

    for namespace, tags in tag_groups:
        namespace_col, tags_col = st.columns([1, 5])
        with namespace_col:
            st.markdown(f"**{namespace}:**")
        with tags_col:
            cols_per_row = 4
            for start in range(0, len(tags), cols_per_row):
                row_tags = tags[start : start + cols_per_row]
                button_cols = st.columns(cols_per_row)
                for index, tag in enumerate(row_tags):
                    with button_cols[index]:
                        if st.button(
                            tag["value"],
                            key=f"tag_button_{namespace}_{start}_{index}_{tag['value']}",
                            width="stretch",
                        ):
                            queue_search_text(f"tag:{tag['raw']}")
                            st.rerun()


def load_visible_columns() -> list[str]:
    raw_value = get_setting(VISIBLE_COLUMNS_SETTING_KEY, "", DB_PATH)
    if not raw_value:
        return DEFAULT_VISIBLE_COLUMNS.copy()

    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return DEFAULT_VISIBLE_COLUMNS.copy()

    if not isinstance(parsed, list):
        return DEFAULT_VISIBLE_COLUMNS.copy()

    normalized = [str(column) for column in parsed if str(column) in TABLE_COLUMN_LABELS]
    return normalized or DEFAULT_VISIBLE_COLUMNS.copy()


def save_visible_columns(columns: list[str]) -> None:
    set_setting(VISIBLE_COLUMNS_SETTING_KEY, json.dumps(columns), DB_PATH)


def render_summary(metric_cols, items: list[dict[str, Any]]) -> None:
    summary = summarize(items)
    metric_cols[0].metric("Items", summary["count"])
    metric_cols[1].metric("Total Size", bytes_label(summary["total_size"]))
    metric_cols[2].metric("With Torrent Flag", summary["torrent_items"])


def _validate_archive_for_conversion(item: dict[str, Any]) -> str | None:
    archive_path = Path(str(item.get("archive_path", "") or ""))
    if not archive_path:
        return "Archive path missing."
    if not archive_path.exists():
        return "Archive file missing."
    expected_size = int(item.get("gallery_filesize", 0) or 0)
    if expected_size:
        actual_size = archive_path.stat().st_size
        if actual_size != expected_size:
            return f"Size mismatch (expected {expected_size}, got {actual_size})."
    return None


def _run_mass_archive_conversion(
    selected_items: list[dict[str, Any]], config: dict[str, Any]
) -> tuple[list[str], int, int]:
    if not isinstance(config, dict):
        return ["Missing downloader configuration."], 0, 0
    upload_settings = config.get("torrent_upload", {}) or {}
    try:
        max_batch = int(upload_settings.get("max_batch_per_run") or 3)
    except (TypeError, ValueError):
        max_batch = 3
    max_batch = max(1, max_batch)
    try:
        adapter = get_cached_qbittorrent_adapter(json.dumps(config, sort_keys=True))
    except Exception as exc:
        return [f"Unable to initialize qBittorrent adapter: {exc}"], 0, max_batch

    messages: list[str] = []
    processed = 0
    for item in selected_items:
        if processed >= max_batch:
            break
        if not bool(item.get("archive_downloaded")):
            continue
        validation_error = _validate_archive_for_conversion(item)
        if validation_error:
            messages.append(f"{item['id']}: {validation_error}")
            continue
        current_item = get_item(item["id"], DB_PATH)
        if current_item is None:
            messages.append(f"{item['id']}: item no longer exists.")
            continue
        try:
            update_item_status(item["id"], "creating_torrent", DB_PATH)
            torrent_uploader.process_item(
                item=current_item,
                config=config,
                adapter=adapter,
                db_path=DB_PATH,
            )
        except Exception as exc:
            messages.append(f"{item['id']}: {exc}")
            try:
                record_error(
                    item_id=item["id"],
                    error_type="torrent_upload_failed",
                    message=str(exc),
                    fix_hint="Check archive file and qBittorrent connectivity.",
                    db_path=DB_PATH,
                )
                update_item_status(item["id"], "error", str(exc), DB_PATH)
            except Exception:
                pass
        else:
            processed += 1
            messages.append(f"{item['id']}: conversion queued.")

    return messages, processed, max_batch


def queue_preview_rows(
    queue_items: list[dict[str, Any]],
    *,
    include_timestamp: bool = False,
    include_progress: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in queue_items[:8]:
        row = {
            "id": str(item.get("id", "")),
            "title": unescape(str(item.get("title", ""))),
            "status": display_status(item.get("local_status", "")),
            "cost": external_cost_label(item.get("external_cost_gp")),
        }
        if include_progress:
            row["progress"] = f"{normalize_progress_pct(item.get('progress_pct', 0)):.1f}%"
        if include_timestamp:
            row["timestamp"] = epoch_label(item.get("state_changed_epoch"))
        rows.append(row)
    return rows


@st.cache_resource(show_spinner=False)
def get_cached_qbittorrent_adapter(config_signature: str):
    import qbittorrent_adapter as qbittorrent_adapter_module

    return qbittorrent_adapter_module.get_adapter(json.loads(config_signature))


def get_live_torrent_progress_pct(item: dict[str, Any], config: dict[str, Any]) -> float | None:
    if not config:
        return None
    if str(item.get("local_status", "")).strip().lower() != "downloading":
        return None

    candidate = download_manager.select_best_candidate(DB_PATH, str(item.get("id", "")))
    if candidate is None:
        return None

    try:
        adapter = get_cached_qbittorrent_adapter(json.dumps(config, sort_keys=True))
        status = adapter.get_status(candidate["hash_string"]) or {}
    except Exception:
        return None

    if not bool(status.get("found")):
        return None

    try:
        progress_pct = float(status.get("progress", 0.0))
    except (TypeError, ValueError):
        return None

    if progress_pct <= 1.0:
        progress_pct *= 100.0
    if bool(status.get("is_completed")):
        progress_pct = 100.0
    return normalize_progress_pct(progress_pct)


def get_display_progress(item: dict[str, Any], config: dict[str, Any]) -> tuple[float, str | None]:
    live_progress_pct = get_live_torrent_progress_pct(item, config)
    if live_progress_pct is not None:
        return live_progress_pct, "Live qBittorrent progress"

    progress_pct = normalize_progress_pct(item.get("progress_pct", 0))
    status_key = str(item.get("local_status", "")).strip().lower()
    if bool(item.get("torrent_downloaded", False)) or bool(item.get("archive_downloaded", False)):
        progress_pct = max(progress_pct, 100.0)
    elif status_key in {"completed", "imported", "seeding"}:
        progress_pct = max(progress_pct, 100.0)

    if status_key in {"force_external", "downloading_external"}:
        return progress_pct, "Archive download progress"
    if status_key == "downloading":
        return progress_pct, "Stored torrent progress"
    return progress_pct, None


def _fix_errors(selected_error_ids: list[int], config: dict[str, Any]) -> list[str]:
    """Attempt to remediate selected errors; return log messages."""
    if not selected_error_ids:
        return []
    messages: list[str] = []
    open_errors = {err["id"]: err for err in get_open_errors(DB_PATH)}
    adapter = None
    if config:
        try:
            adapter = get_cached_qbittorrent_adapter(json.dumps(config, sort_keys=True))
        except Exception as exc:  # noqa: BLE001
            messages.append(f"Failed to initialize qBittorrent adapter: {exc}")
    for error_id in selected_error_ids:
        err = open_errors.get(error_id)
        if not err:
            continue
        err_type = err.get("error_type")
        item_id = str(err.get("item_id", ""))
        try:
            if err_type == "missing_torrent" and adapter and config:
                download_manager.restore_missing_torrents(DB_PATH, config, adapter)
                resolve_error(item_id=item_id, error_type=str(err_type), db_path=DB_PATH)
                messages.append(f"Requeued torrent for {item_id}")
            elif err_type == "missing_archive" and config:
                ext_client = external_adapter.get_adapter(config)
                external_manager.reconcile_missing_external_imports(DB_PATH, config)
                external_manager.process_external_queue(DB_PATH, config, ext_client)
                resolve_error(item_id=item_id, error_type=str(err_type), db_path=DB_PATH)
                messages.append(f"Re-ran archive reconciliation for {item_id}")
            elif err_type in {"missing_media", "ambiguous_media"} and config and adapter:
                media_importer.import_completed_archives(DB_PATH, config)
                media_importer.import_completed_torrents(DB_PATH, config, adapter)
                resolve_error(item_id=item_id, error_type=str(err_type), db_path=DB_PATH)
                messages.append(f"Re-import attempted for {item_id}")
            elif err_type == "missing_thumbnail":
                worker.fetch_and_store_metadata()
                resolve_error(item_id=item_id, error_type=str(err_type), db_path=DB_PATH)
                messages.append(f"Refetched metadata for {item_id}")
            else:
                resolve_error(item_id=item_id, error_type=str(err_type), db_path=DB_PATH)
                messages.append(f"Marked error resolved for {item_id}")
        except Exception as exc:  # noqa: BLE001
            record_error(
                item_id=item_id or "unknown",
                error_type="error_fix_failed",
                message=str(exc),
                fix_hint="Check logs and retry fix.",
                db_path=DB_PATH,
            )
            messages.append(f"Fix failed for {item_id}: {exc}")
    return messages


def _reset_items_to_new(error_records: list[dict[str, Any]]) -> list[str]:
    logs: list[str] = []
    seen: set[tuple[str, str]] = set()
    for err in error_records:
        item_id = str(err.get("item_id", ""))
        err_type = str(err.get("error_type", ""))
        if not item_id or (item_id, err_type) in seen:
            continue
        seen.add((item_id, err_type))
        try:
            update_item_download_flags(item_id, archive_downloaded=False, db_path=DB_PATH)
            update_item_archive_path(item_id, "", db_path=DB_PATH)
            update_item_progress(item_id, 0.0, db_path=DB_PATH)
            update_item_status(item_id, "new", db_path=DB_PATH)
            resolve_error(item_id=item_id, error_type=err_type, db_path=DB_PATH)
            resolve_error(item_id=item_id, error_type="error", db_path=DB_PATH)
            logs.append(f"Reset {item_id} to new")
        except Exception as exc:  # noqa: BLE001
            logs.append(f"Reset failed for {item_id}: {exc}")
    return logs


def _set_error_selection(error_ids: list[int], state_key: str) -> None:
    desired = bool(st.session_state.get(state_key, False))
    for error_id in error_ids:
        st.session_state[f"err_sel_{error_id}"] = desired


def _cleanup_error_selection_state(open_errors: list[dict[str, Any]]) -> None:
    active_row_keys = {f"err_sel_{int(err['id'])}" for err in open_errors}
    active_group_keys = {"err_select_all"}
    for err in open_errors:
        error_type = str(err.get("error_type", ""))
        if error_type in {"missing_archive"}:
            category = "archive"
        elif error_type in {"missing_torrent", "torrent_status_failed"}:
            category = "torrent"
        elif error_type in {"missing_media", "ambiguous_media"}:
            category = "media"
        else:
            category = "misc"
        active_group_keys.add(f"err_select_category_{category}")
        active_group_keys.add(f"err_select_group_{category}_{error_type}")
    stale_keys = [
        key
        for key in st.session_state.keys()
        if (
            key.startswith("err_sel_")
            and key not in active_row_keys
        ) or (
            key.startswith("err_select_")
            and key not in active_group_keys
        )
    ]
    for key in stale_keys:
        del st.session_state[key]


def render_error_sidebar(config: dict[str, Any]) -> None:
    open_errors = get_open_errors(DB_PATH)
    _cleanup_error_selection_state(open_errors)
    open_errors_by_id = {int(err["id"]): err for err in open_errors}
    count = len(open_errors)
    with st.expander(f"Errors ({count})", expanded=count > 0):
        if not open_errors:
            st.caption("No active errors detected.")
            return

        # Group errors by category and type
        def categorize(err_type: str) -> str:
            if err_type in {"missing_archive"}:
                return "Archive"
            if err_type in {"missing_torrent", "torrent_status_failed"}:
                return "Torrent"
            if err_type in {"missing_media", "ambiguous_media"}:
                return "Media"
            return "Misc"

        grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for err in open_errors:
            cat = categorize(str(err.get("error_type", "")))
            grouped.setdefault(cat, {}).setdefault(err.get("error_type", "unknown"), []).append(err)

        all_error_ids = [int(err["id"]) for err in open_errors]
        master_key = "err_select_all"
        if master_key not in st.session_state:
            st.session_state[master_key] = False
        st.checkbox(
            "Select all",
            key=master_key,
            on_change=_set_error_selection,
            args=(all_error_ids, master_key),
        )
        for category, type_map in grouped.items():
            cat_count = sum(len(v) for v in type_map.values())
            with st.expander(f"{category} ({cat_count})", expanded=cat_count > 0):
                category_error_ids = [int(err["id"]) for err_list in type_map.values() for err in err_list]
                category_key = f"err_select_category_{category.lower()}"
                if category_key not in st.session_state:
                    st.session_state[category_key] = False
                st.checkbox(
                    f"Select all {category.lower()} errors",
                    key=category_key,
                    on_change=_set_error_selection,
                    args=(category_error_ids, category_key),
                )
                for err_type, err_list in type_map.items():
                    type_count = len(err_list)
                    with st.expander(f"{err_type.replace('_', ' ').title()} ({type_count})", expanded=False):
                        type_error_ids = [int(err["id"]) for err in err_list]
                        group_key = f"err_select_group_{category.lower()}_{err_type}"
                        if group_key not in st.session_state:
                            st.session_state[group_key] = False
                        st.checkbox(
                            "Select all in group",
                            key=group_key,
                            on_change=_set_error_selection,
                            args=(type_error_ids, group_key),
                        )
                        for err in err_list:
                            item_id = str(err.get("item_id", ""))
                            item = get_item(item_id, DB_PATH) if item_id else None
                            title = unescape(item["title"]) if item and item.get("title") else (item_id or "Unknown")

                            help_text_parts = [f"Detected: {epoch_label(err.get('detected_epoch'))}"]
                            if err.get("message"):
                                help_text_parts.append(str(err["message"]))
                            if err.get("fix_hint"):
                                help_text_parts.append(f"Hint: {err['fix_hint']}")
                            help_text = " | ".join(help_text_parts)

                            key = f"err_sel_{err['id']}"
                            if key not in st.session_state:
                                st.session_state[key] = False

                            col1, col2 = st.columns([0.12, 0.88])
                            with col1:
                                checked = st.checkbox(
                                    f"Select {title}",
                                    key=key,
                                    label_visibility="collapsed",
                                    help=help_text,
                                )
                            with col2:
                                if st.button(
                                    title,
                                    key=f"err_jump_{err['id']}",
                                    width="stretch",
                                    help=help_text,
                                ):
                                    if item_id and item:
                                        st.session_state["selected_item_id"] = item_id
                                        st.session_state[SEARCH_TEXT_KEY] = ""
                                        st.session_state[SEARCH_TEXT_PENDING_KEY] = ""
                                        st.rerun()
                                # keep line compact; hover contains details

        selected_ids: list[int] = [
            err_id for err_id in open_errors_by_id.keys() if st.session_state.get(f"err_sel_{err_id}", False)
        ]

        fix_col, dismiss_col = st.columns(2)
        action = st.selectbox(
            "Selected action",
            options=["Fix (attempt remediation)", "Dismiss", "Reset status to New"],
            index=0,
            key="error_action_select",
        )
        if fix_col.button("Apply action", width="stretch"):
            logs: list[str] = []
            if not selected_ids:
                st.warning("Select at least one error before applying an action.")
                return
            if action.startswith("Fix"):
                logs = _fix_errors(selected_ids, config)
            elif action.startswith("Dismiss"):
                for err_id in selected_ids:
                    err = open_errors_by_id.get(err_id)
                    if err is None:
                        continue
                    resolve_error(
                        item_id=str(err.get("item_id", "")),
                        error_type=str(err.get("error_type", "")),
                        db_path=DB_PATH,
                    )
                logs = [f"Dismissed {len(selected_ids)} error(s)"]
            else:  # Reset status
                selected_records = [open_errors_by_id[err_id] for err_id in selected_ids if err_id in open_errors_by_id]
                logs = _reset_items_to_new(selected_records)
            if logs:
                st.success("; ".join(logs))
            st.rerun()
        if dismiss_col.button("Refresh errors", width="stretch"):
            st.rerun()


def render_download_queue_sidebar(
    config: dict[str, Any],
) -> None:
    fragment_root = st.container()
    with fragment_root:
        items = get_all_items_for_ui(DB_PATH)
        torrent_queue = download_manager.get_queued_items(DB_PATH)
        external_queue = external_manager.get_external_queue(DB_PATH, config) if config else []
        active_downloads = [
            item
            for item in items
            if str(item.get("local_status", "")).strip().lower() == "downloading"
            or (
                str(item.get("local_status", "")).strip().lower() == "force_external"
                and not bool(item.get("archive_downloaded", False))
                and normalize_progress_pct(item.get("progress_pct", 0)) > 0.0
            )
        ]
        recent_downloads = sorted(
            [
                item
                for item in items
                if bool(item.get("torrent_downloaded", False)) or bool(item.get("archive_downloaded", False))
            ],
            key=lambda item: int(item.get("state_changed_epoch", 0) or 0),
            reverse=True,
        )[:6]

        st.divider()
        st.subheader("Download Queue")
        st.caption("Derived from manual actions and the active auto-download rule set.")

        queue_metric_cols = st.columns(3)
        queue_metric_cols[0].metric("Torrent", len(torrent_queue))
        queue_metric_cols[1].metric("External", len(external_queue))
        queue_metric_cols[2].metric("Active", len(active_downloads))

        next_item = torrent_queue[0] if torrent_queue else (external_queue[0] if external_queue else None)
        if next_item is not None:
            st.caption(f"Next up: {next_item['id']} | {unescape(str(next_item.get('title', '')))}")
        else:
            st.caption("Nothing is waiting to download.")

        with st.expander("Queue Details", expanded=False):
            if torrent_queue:
                st.write("**Torrent Queue**")
                st.dataframe(pd.DataFrame(queue_preview_rows(torrent_queue)), width="stretch", hide_index=True)
            else:
                st.caption("No torrent items waiting.")

            if external_queue:
                st.write("**External Queue**")
                st.dataframe(pd.DataFrame(queue_preview_rows(external_queue)), width="stretch", hide_index=True)
            else:
                st.caption("No external items waiting.")

            if active_downloads:
                st.write("**Active Downloads**")
                st.dataframe(
                    pd.DataFrame(queue_preview_rows(active_downloads, include_progress=True)),
                    width="stretch",
                    hide_index=True,
                )
            else:
                st.caption("No downloads currently running.")

            if recent_downloads:
                st.write("**Recent Downloaded**")
                st.dataframe(
                    pd.DataFrame(queue_preview_rows(recent_downloads, include_timestamp=True)),
                    width="stretch",
                    hide_index=True,
                )


def render_item_status_panel(
    item_id: str,
    config: dict[str, Any],
) -> None:
    fragment_root = st.container()
    with fragment_root:
        current_item = get_item(item_id, DB_PATH)
        if current_item is None:
            st.info("Selected item no longer exists.")
            return

        progress_pct, progress_source = get_display_progress(current_item, config)

        st.write("**Progress**")
        st.progress(int(round(progress_pct)))
        progress_caption_parts = [f"{progress_pct:.1f}%"]
        if progress_source:
            progress_caption_parts.append(progress_source)
        st.caption(" | ".join(progress_caption_parts))

        current_status = current_item.get("local_status", "new")
        current_status_key = str(current_status or "").strip().lower()
        torrent_downloaded = bool(current_item.get("torrent_downloaded", False))
        torrent_from_archive = bool(current_item.get("torrent_from_archive", False))
        archive_downloaded = bool(current_item.get("archive_downloaded", False))
        archive_cancel_requested = bool(current_item.get("archive_cancel_requested", False))
        has_torrent_candidate = bool(current_item.get("torrent_count", 0))
        external_label = str(current_item.get("external_label", "") or "")
        external_size_text = str(current_item.get("external_size_text", "") or "")
        external_cost_gp = current_item.get("external_cost_gp")

        if current_status not in STATUS_OPTIONS:
            status_options = [current_status, *STATUS_OPTIONS]
            status_index = 0
        else:
            status_options = STATUS_OPTIONS
            status_index = status_options.index(current_status)

        new_status = st.selectbox(
            "Local Status",
            options=status_options,
            index=status_index,
            format_func=display_status,
        )
        if st.button("Save Status", key=f"save_status_{item_id}", width="stretch"):
            update_item_status(item_id, new_status, DB_PATH)
            st.rerun()

        st.write("**Download State**")
        torrent_downloaded_label = "Torrent Downloaded | User Created" if torrent_from_archive else "Torrent Downloaded"
        st.checkbox(torrent_downloaded_label, value=torrent_downloaded, disabled=True)
        st.checkbox("Archive Downloaded", value=archive_downloaded, disabled=True)
        st.caption(f"Last state change: {epoch_label(current_item.get('state_changed_epoch'))}")
        if external_label:
            selection_bits = [external_label, external_cost_label(external_cost_gp)]
            if external_size_text:
                selection_bits.append(external_size_text)
            st.caption(f"Selected external method: {' | '.join(selection_bits)}")
        if archive_cancel_requested:
            st.caption("Archive cancellation requested. Waiting for the worker to stop the current stream.")

        if torrent_downloaded:
            st.success(torrent_downloaded_label)
            if st.button("Reimport Torrent", key=f"reimport_torrent_{item_id}", type="secondary", width="stretch"):
                update_item_download_flags(item_id, torrent_downloaded=False, db_path=DB_PATH)
                update_item_status(item_id, "complete", DB_PATH)
                st.rerun()
        else:
            torrent_button_type = "secondary" if archive_downloaded else "primary"
            torrent_button_label = (
                "Queue Torrent Instead"
                if current_status_key in {"force_external", "downloading_external"}
                else "Download Torrent"
            )
            if st.button(
                torrent_button_label,
                key=f"download_torrent_{item_id}",
                type=torrent_button_type,
                width="stretch",
                disabled=not has_torrent_candidate,
            ):
                set_archive_cancel_requested(item_id, current_status_key == "downloading_external", DB_PATH)
                if current_status_key in {"force_external", "downloading_external"} or archive_cancel_requested:
                    update_item_download_flags(item_id, archive_downloaded=False, db_path=DB_PATH)
                    update_item_progress(item_id, 0.0, DB_PATH)
                update_item_status(item_id, "force_queued", DB_PATH)
                st.rerun()
            if not has_torrent_candidate:
                st.caption("No torrent is available for this gallery.")

        if archive_downloaded:
            st.success("Archive Downloaded")
            archive_retry_cols = st.columns(2)
            if archive_retry_cols[0].button("Retry Archive", key=f"retry_archive_{item_id}", type="secondary", width="stretch"):
                update_item_download_flags(item_id, archive_downloaded=False, db_path=DB_PATH)
                st.session_state[ARCHIVE_DIALOG_ITEM_KEY] = item_id
                st.rerun()
            if archive_retry_cols[1].button(
                "Keep Method / Queue",
                key=f"keep_archive_method_{item_id}",
                type="secondary",
                width="stretch",
            ):
                update_item_download_flags(item_id, archive_downloaded=False, db_path=DB_PATH)
                update_item_status(item_id, "force_external", DB_PATH)
                st.rerun()
            if not torrent_downloaded:
                if st.button(
                    "Convert Archive to Torrent",
                    key=f"convert_archive_{item_id}",
                    type="primary",
                    width="stretch",
                ):
                    try:
                        adapter = qbittorrent_adapter.get_adapter(config)
                        with st.spinner("Building and uploading torrent..."):
                            torrent_uploader.process_item(
                                item=current_item,
                                config=config,
                                adapter=adapter,
                                db_path=DB_PATH,
                            )
                        st.success("Torrent created and added to qBittorrent.")
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Torrent conversion failed: {exc}")
        else:
            archive_button_type = "secondary" if torrent_downloaded else "primary"
            archive_button_label = "Change Archive Method" if external_label else "Download Archive"
            archive_action_cols = st.columns(2)
            archive_button_disabled = current_status_key == "downloading_external"
            if archive_action_cols[0].button(
                archive_button_label,
                key=f"download_archive_{item_id}",
                type=archive_button_type,
                width="stretch",
                disabled=archive_button_disabled,
            ):
                set_archive_cancel_requested(item_id, False, DB_PATH)
                st.session_state[ARCHIVE_DIALOG_ITEM_KEY] = item_id
                st.rerun()
            cancel_archive_label = (
                "Cancel Archive Download"
                if current_status_key == "downloading_external"
                else "Cancel Queued Archive"
            )
            cancel_archive_disabled = current_status_key not in {"force_external", "downloading_external"}
            if archive_action_cols[1].button(
                cancel_archive_label,
                key=f"cancel_archive_{item_id}",
                type="secondary",
                width="stretch",
                disabled=cancel_archive_disabled,
            ):
                if current_status_key == "downloading_external":
                    set_archive_cancel_requested(item_id, True, DB_PATH)
                else:
                    set_archive_cancel_requested(item_id, False, DB_PATH)
                    update_item_progress(item_id, 0.0, DB_PATH)
                    update_item_status(item_id, default_status_after_archive_cancel(current_item), DB_PATH)
                update_item_download_flags(item_id, archive_downloaded=False, db_path=DB_PATH)
                st.rerun()

        if st.button("Delete Selected Entry", key=f"delete_item_{item_id}", type="primary", width="stretch"):
            deleted = delete_item(item_id, DB_PATH)
            if deleted:
                st.session_state.pop("selected_item_id", None)
            st.rerun()


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=4, ensure_ascii=False), encoding="utf-8")


def has_required_runtime_config(config: dict[str, Any]) -> bool:
    if not isinstance(config, dict):
        return False
    exhentai_cfg = config.get("exhentai", {})
    paths_cfg = config.get("paths", {})
    qb_cfg = config.get("qbittorrent", {})
    if not isinstance(exhentai_cfg, dict) or not isinstance(paths_cfg, dict) or not isinstance(qb_cfg, dict):
        return False

    raw_cookies = str(exhentai_cfg.get("cookies", "") or "").strip()
    media_path = str(paths_cfg.get("media_library", "") or "").strip()
    archive_path = str(
        paths_cfg.get("archive_library", "") or paths_cfg.get("archive_downloads", "") or ""
    ).strip()
    qb_host = str(qb_cfg.get("host", "") or "").strip()
    qb_download_path = str(qb_cfg.get("download_path", "") or "").strip()

    try:
        qb_port = int(qb_cfg.get("port", 0))
    except (TypeError, ValueError):
        qb_port = 0

    return bool(
        raw_cookies
        and media_path
        and archive_path
        and qb_host
        and qb_download_path
        and qb_port > 0
    )


def render_config_sidebar_editor(config: dict[str, Any]) -> dict[str, Any]:
    def _int_or_default(raw_value: Any, fallback: int) -> int:
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            return fallback

    config_dict = dict(config) if isinstance(config, dict) else {}
    exhentai_cfg = config_dict.get("exhentai", {}) if isinstance(config_dict.get("exhentai"), dict) else {}
    downloader_cfg = (
        config_dict.get("downloader", {}) if isinstance(config_dict.get("downloader"), dict) else {}
    )
    paths_cfg = config_dict.get("paths", {}) if isinstance(config_dict.get("paths"), dict) else {}
    qb_cfg = config_dict.get("qbittorrent", {}) if isinstance(config_dict.get("qbittorrent"), dict) else {}
    upload_cfg = (
        config_dict.get("torrent_upload", {})
        if isinstance(config_dict.get("torrent_upload"), dict)
        else {}
    )
    lanraragi_cfg = (
        config_dict.get("lanraragi", {})
        if isinstance(config_dict.get("lanraragi"), dict)
        else {}
    )

    with st.expander("Config", expanded=False):
        st.caption("Edit runtime config here (biglybt fields intentionally excluded).")
        with st.form("sidebar_config_editor_form"):
            cookies_value = st.text_area(
                "ExHentai Cookies",
                value=str(exhentai_cfg.get("cookies", "") or ""),
                height=110,
                help="Full cookie string used by both UI and worker.",
            )
            st.markdown("**Paths**")
            media_library = st.text_input(
                "Media Library Path",
                value=str(paths_cfg.get("media_library", "") or ""),
            )
            archive_library = st.text_input(
                "Archive Library Path",
                value=str(paths_cfg.get("archive_library", "") or paths_cfg.get("archive_downloads", "") or ""),
            )

            st.markdown("**qBittorrent**")
            qb_host = st.text_input("Host", value=str(qb_cfg.get("host", "localhost") or "localhost"))
            qb_port = st.number_input(
                "Port",
                min_value=1,
                max_value=65535,
                value=_int_or_default(qb_cfg.get("port"), 8080),
                step=1,
            )
            qb_username = st.text_input("Username", value=str(qb_cfg.get("username", "") or ""))
            qb_password = st.text_input(
                "Password",
                value=str(qb_cfg.get("password", "") or ""),
                type="password",
            )
            qb_download_path = st.text_input(
                "Download Path",
                value=str(qb_cfg.get("download_path", "") or ""),
            )
            qb_metadata_stuck_minutes = st.number_input(
                "Metadata-Stuck Recovery (minutes)",
                min_value=0,
                max_value=1440,
                value=_int_or_default(qb_cfg.get("metadata_stuck_recovery_minutes"), 60),
                step=5,
                help="If a torrent stays in metadata download state this long, auto-fetch its .torrent file and re-add it.",
            )
            qb_metadata_retry_minutes = st.number_input(
                "Metadata Recovery Retry Cooldown (minutes)",
                min_value=1,
                max_value=1440,
                value=_int_or_default(qb_cfg.get("metadata_recovery_cooldown_minutes"), 30),
                step=5,
            )

            st.markdown("**Torrent Upload (optional)**")
            staging_dir = st.text_input(
                "Staging Directory",
                value=str(upload_cfg.get("staging_dir", "") or ""),
            )
            generated_torrent_dir = st.text_input(
                "Generated Torrent Directory",
                value=str(upload_cfg.get("generated_torrent_dir", "") or ""),
            )
            piece_size_mb = st.number_input(
                "Piece Size (MB)",
                min_value=1,
                max_value=64,
                value=_int_or_default(upload_cfg.get("piece_size_mb"), 4),
                step=1,
            )
            max_batch_per_run = st.number_input(
                "Max Batch Per Run",
                min_value=1,
                max_value=100,
                value=_int_or_default(upload_cfg.get("max_batch_per_run"), 3),
                step=1,
            )
            trackers_extra = st.text_area(
                "Extra Trackers (one per line)",
                value="\n".join(str(t) for t in list(upload_cfg.get("trackers_extra") or []) if str(t).strip()),
                height=90,
            )

            st.markdown("**LANraragi Metadata Sync (optional)**")
            lrr_enabled = st.checkbox(
                "Enable LANraragi sync",
                value=bool(lanraragi_cfg.get("enabled", False)),
                help="Push favorited timestamp and favorite note into LANraragi tags.",
            )
            lrr_base_url = st.text_input(
                "LANraragi Base URL",
                value=str(lanraragi_cfg.get("base_url", "") or ""),
                help="Example: http://192.168.1.50:3000",
            )
            lrr_api_key = st.text_input(
                "LANraragi API Key",
                value=str(lanraragi_cfg.get("api_key", "") or ""),
                type="password",
            )
            lrr_sync_interval_minutes = st.number_input(
                "LANraragi Sync Interval (minutes)",
                min_value=1,
                value=_int_or_default(
                    lanraragi_cfg.get("sync_interval_minutes"),
                    LANRARAGI_SYNC_DEFAULT_MINUTES,
                ),
                step=1,
            )
            st.caption(
                "Metadata tag format is fixed to plugin-style + Sadpanda fields "
                "(Category/Uploader/Timestamp/Source/Date Added/.../GID)."
            )

            submitted = st.form_submit_button("Save Config", width="stretch")

        if submitted:
            next_config = dict(config_dict)
            next_config["exhentai"] = {"cookies": cookies_value.strip()}
            next_config["downloader"] = {"active_adapter": "qbittorrent_adapter"}
            next_config["paths"] = {
                "media_library": media_library.strip(),
                "archive_library": archive_library.strip(),
                "archive_downloads": archive_library.strip(),
            }
            next_config["qbittorrent"] = {
                "host": qb_host.strip(),
                "port": int(qb_port),
                "username": qb_username.strip(),
                "password": qb_password,
                "download_path": qb_download_path.strip(),
                "metadata_stuck_recovery_minutes": int(qb_metadata_stuck_minutes),
                "metadata_recovery_cooldown_minutes": int(qb_metadata_retry_minutes),
            }

            trackers = [line.strip() for line in trackers_extra.splitlines() if line.strip()]
            next_config["torrent_upload"] = {
                "staging_dir": staging_dir.strip(),
                "generated_torrent_dir": generated_torrent_dir.strip(),
                "piece_size_mb": int(piece_size_mb),
                "max_batch_per_run": int(max_batch_per_run),
                "trackers_extra": trackers,
            }
            next_config["lanraragi"] = {
                "enabled": bool(lrr_enabled),
                "base_url": lrr_base_url.strip(),
                "api_key": lrr_api_key.strip(),
                "sync_interval_minutes": int(lrr_sync_interval_minutes),
            }

            try:
                save_config(next_config)
                set_bool_setting(CONFIG_READY_FLAG_KEY, has_required_runtime_config(next_config), DB_PATH)
                st.success(f"Config saved to {CONFIG_PATH}.")
                st.rerun()
            except OSError as exc:
                st.error(f"Failed to save config: {exc}")

    return config_dict


def clear_archive_dialog(item_id: str | None = None) -> None:
    st.session_state.pop(ARCHIVE_DIALOG_ITEM_KEY, None)
    if item_id:
        cache = dict(st.session_state.get(ARCHIVE_OPTIONS_CACHE_KEY, {}))
        cache.pop(item_id, None)
        st.session_state[ARCHIVE_OPTIONS_CACHE_KEY] = cache


def fetch_archive_options(item: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    if not config:
        return {
            "success": False,
            "reason": "missing_config",
            "message": f"Config file not found or invalid: {CONFIG_PATH}",
            "options": [],
        }

    try:
        import external_adapter as external_adapter_module

        adapter = external_adapter_module.get_adapter(config)
        return adapter.inspect_item(item)
    except Exception as exc:
        return {
            "success": False,
            "reason": "adapter_error",
            "message": str(exc),
            "options": [],
        }


def archive_choice_label(option: dict[str, Any]) -> str:
    parts = [str(option.get("label", "")).strip()]
    kind_label = str(option.get("kind_label", "")).strip()
    if kind_label:
        parts.append(kind_label)
    cost_text = str(option.get("cost_text", "")).strip()
    if cost_text:
        parts.append(cost_text)
    size_text = str(option.get("size_text", "")).strip()
    if size_text and size_text.upper() != "N/A":
        parts.append(size_text)
    return " | ".join(part for part in parts if part)


def external_cost_label(cost_gp: Any) -> str:
    if cost_gp in (None, ""):
        return "Unknown"
    try:
        value = int(cost_gp)
    except (TypeError, ValueError):
        return str(cost_gp)
    return "Free!" if value <= 0 else f"{value} GP"


@st.dialog("Archive Download Options")
def render_archive_dialog(item: dict[str, Any]) -> None:
    item_id = str(item["id"])
    st.write(f"**{unescape(str(item.get('title', '')))}**")
    st.caption("Choose the external download method before the worker spends any GP.")

    cache = dict(st.session_state.get(ARCHIVE_OPTIONS_CACHE_KEY, {}))
    inspection = cache.get(item_id)
    if inspection is None:
        inspection = fetch_archive_options(item)
        cache[item_id] = inspection
        st.session_state[ARCHIVE_OPTIONS_CACHE_KEY] = cache

    if not inspection.get("success"):
        st.error(str(inspection.get("message") or inspection.get("reason") or "Failed to inspect archiver page."))
        retry_col, close_col = st.columns(2)
        if retry_col.button("Retry", width="stretch"):
            clear_archive_dialog(item_id)
            st.rerun()
        if close_col.button("Close", width="stretch"):
            clear_archive_dialog(item_id)
            st.rerun()
        return

    options = list(inspection.get("options", []))
    if not options:
        st.warning("No archive or H@H options were detected on the archiver page.")
        if st.button("Close", width="stretch"):
            clear_archive_dialog(item_id)
            st.rerun()
        return

    display_rows = [
        {
            "Method": option.get("label", ""),
            "Type": option.get("kind_label", ""),
            "Cost": option.get("cost_text", ""),
            "Size": option.get("size_text", ""),
            "Available": "Yes" if option.get("available") else "No",
        }
        for option in options
    ]
    st.dataframe(pd.DataFrame(display_rows), width="stretch", hide_index=True)

    available_options = [option for option in options if option.get("available")]
    if not available_options:
        st.warning("Every detected option is unavailable for this gallery.")
        if st.button("Close", width="stretch"):
            clear_archive_dialog(item_id)
            st.rerun()
        return

    option_map = {str(option["method"]): option for option in available_options}
    selected_method = str(item.get("external_method", "") or "")
    available_methods = list(option_map.keys())
    if selected_method not in option_map:
        selected_method = available_methods[0]

    selected_method = st.radio(
        "Download method",
        options=available_methods,
        index=available_methods.index(selected_method),
        format_func=lambda method: archive_choice_label(option_map[method]),
    )
    selected_option = option_map[selected_method]

    if selected_option.get("cost_gp") not in (None, 0):
        st.warning(
            f"This choice will spend GP: {external_cost_label(selected_option.get('cost_gp'))}."
        )
    elif selected_option.get("is_free"):
        st.info("This choice is currently free.")

    confirm_col, cancel_col = st.columns(2)
    if confirm_col.button("Confirm Download", type="primary", width="stretch"):
        update_item_external_selection(
            item_id,
            external_method=str(selected_option.get("method", "")),
            external_label=str(selected_option.get("label", "")),
            external_size_text=str(selected_option.get("size_text", "")),
            external_cost_gp=selected_option.get("cost_gp"),
            db_path=DB_PATH,
        )
        set_archive_cancel_requested(item_id, False, DB_PATH)
        update_item_status(item_id, "force_external", DB_PATH)
        clear_archive_dialog(item_id)
        st.rerun()

    if cancel_col.button("Cancel", width="stretch"):
        clear_archive_dialog(item_id)
        st.rerun()


def main() -> None:
    init_db(DB_PATH)

    st.set_page_config(page_title="ExHentai Favorites Downloader", layout="wide")
    st.title("ExHentai Favorites Downloader")
    st.caption("ExHentai favorites browser and download manager.")

    config = load_config()

    raw_items = get_all_items_for_ui(DB_PATH)
    active_rule_sets = get_active_rule_sets(DB_PATH)
    items = annotate_active_rule_matches(raw_items, active_rule_sets)
    category_options = sorted({item["site_category"] for item in items if item.get("site_category")})
    favorite_category_options = sorted(
        {item["favorite_category"] for item in items if item.get("favorite_category")}
    )
    visible_columns = load_visible_columns()
    sort_field = str(get_setting(TABLE_SORT_FIELD_KEY, TABLE_SORT_DEFAULT_FIELD, DB_PATH) or "").strip()
    if sort_field not in TABLE_SORT_LABEL_TO_FIELD.values():
        sort_field = TABLE_SORT_DEFAULT_FIELD
    sort_desc = get_bool_setting(TABLE_SORT_DESC_KEY, TABLE_SORT_DEFAULT_DESC, DB_PATH)

    with st.sidebar:
        config = render_config_sidebar_editor(config)
        st.divider()
        st.header("Favorites")
        if st.button("Rescan All Favorites", width="stretch"):
            set_bool_setting(FAVORITES_RESCAN_FLAG_KEY, True, DB_PATH)
            st.success("Full favorites rescan requested; the worker will run it shortly.")

        interval_value = get_setting(
            FULL_RESCAN_INTERVAL_KEY, str(FULL_RESCAN_DEFAULT_HOURS)
        )
        try:
            interval_hours = max(1, int(interval_value))
        except (TypeError, ValueError):
            interval_hours = FULL_RESCAN_DEFAULT_HOURS
        new_interval = st.number_input(
            "Full favorites rescan interval (hours)",
            min_value=1,
            value=interval_hours,
            step=1,
            key="favorites_full_rescan_interval_input",
        )
        st.caption("Examples: 12 = twice daily, 24 = daily, 168 = weekly.")
        if st.button("Set Rescan Interval", key="set_favorites_full_rescan_interval"):
            set_setting(FULL_RESCAN_INTERVAL_KEY, str(int(new_interval)), DB_PATH)
            st.success(f"Full rescan interval set to {int(new_interval)} hour(s).")

        last_refresh_value = get_setting(FAVORITES_LAST_REFRESH_KEY)
        if last_refresh_value:
            try:
                last_refresh_epoch = int(last_refresh_value)
            except (TypeError, ValueError):
                last_refresh_epoch = 0
        else:
            last_refresh_epoch = 0
        if last_refresh_epoch:
            st.caption(f"Last full favorites rescan: {epoch_label(last_refresh_epoch)}")
        else:
            st.caption("Full favorites rescan pending.")
        status_raw = get_setting(FAVORITES_RESCAN_STATUS_KEY, "", DB_PATH)
        status_data: dict[str, Any] = {}
        if status_raw:
            try:
                status_data = json.loads(status_raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                status_data = {}
        if status_data:
            state = str(status_data.get("state", "unknown") or "unknown").strip().lower()
            phase = str(status_data.get("phase", "") or "").strip()
            message = str(status_data.get("message", "") or "").strip()
            pages_scanned = int(status_data.get("pages_scanned", 0) or 0)
            new_items_found = int(status_data.get("new_items_found", 0) or 0)
            favorite_metadata_updates = int(
                status_data.get("favorite_metadata_updates", 0) or 0
            )
            items_changed = int(status_data.get("items_changed", 0) or 0)
            torrents_changed = int(status_data.get("torrents_changed", 0) or 0)
            updated_epoch = int(status_data.get("updated_epoch", 0) or 0)

            if state == "running":
                st.info(
                    "Rescan status: Running"
                    + (f" ({phase})" if phase else "")
                    + (f" | {message}" if message else "")
                )
            elif state == "completed":
                st.success(
                    "Rescan status: Completed"
                    + (f" | {message}" if message else "")
                )
            elif state == "failed":
                st.error("Rescan status: Failed" + (f" | {message}" if message else ""))
            else:
                st.caption(
                    "Rescan status: "
                    + state.title()
                    + (f" | {message}" if message else "")
                )

            st.caption(
                f"Pages scanned: {pages_scanned} | New galleries: {new_items_found} | "
                f"Favorite metadata updates: {favorite_metadata_updates} | "
                f"Item changes: {items_changed} | Torrent changes: {torrents_changed}"
            )
        if updated_epoch:
            st.caption(f"Status updated: {epoch_label(updated_epoch)}")

        st.divider()
        st.subheader("LANraragi")
        if st.button("Sync LANraragi Metadata Now", width="stretch"):
            set_bool_setting(LANRARAGI_FORCE_SYNC_KEY, True, DB_PATH)
            st.success("LANraragi sync requested; worker will process it on the next cycle.")

        lrr_last_sync_value = get_setting(LANRARAGI_LAST_SYNC_EPOCH_KEY, "0", DB_PATH)
        try:
            lrr_last_sync_epoch = int(lrr_last_sync_value or 0)
        except (TypeError, ValueError):
            lrr_last_sync_epoch = 0
        if lrr_last_sync_epoch > 0:
            st.caption(f"Last LANraragi sync: {epoch_label(lrr_last_sync_epoch)}")
        else:
            st.caption("LANraragi sync has not run yet.")

        lrr_status_raw = get_setting(LANRARAGI_LAST_STATUS_KEY, "", DB_PATH)
        if lrr_status_raw:
            try:
                lrr_status = json.loads(lrr_status_raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                lrr_status = {}
            if lrr_status:
                state = str(lrr_status.get("state", "") or "").strip().lower()
                message = str(lrr_status.get("message", "") or "").strip()
                matched = int(lrr_status.get("matched", 0) or 0)
                updated = int(lrr_status.get("updated", 0) or 0)
                skipped = int(lrr_status.get("skipped", 0) or 0)
                failed = int(lrr_status.get("failed", 0) or 0)
                if state == "completed":
                    st.success(f"Status: Completed | {message}")
                elif state == "failed":
                    st.error(f"Status: Failed | {message}")
                elif state == "running":
                    st.info(f"Status: Running | {message}")
                else:
                    st.caption(f"Status: {state or 'unknown'} | {message}")
                st.caption(
                    f"Matched: {matched} | Updated: {updated} | Skipped: {skipped} | Failed: {failed}"
                )

        st.divider()
        render_error_sidebar(config or {})
        if config:
            if st.button(
                "Re-add missing torrents to qBittorrent",
                type="secondary",
                width="stretch",
            ):
                try:
                    adapter = get_cached_qbittorrent_adapter(json.dumps(config, sort_keys=True))
                    actions = download_manager.restore_missing_torrents(DB_PATH, config, adapter)
                    st.success(f"Re-enqueued {len(actions)} torrent(s) for recheck/reseed.")
                except Exception as exc:
                    st.error(f"Restore failed: {exc}")

        st.divider()
        st.subheader("Table Columns")
        selected_visible_columns = st.multiselect(
            "Visible Columns",
            options=list(TABLE_COLUMN_LABELS.keys()),
            default=visible_columns,
            format_func=lambda column: TABLE_COLUMN_LABELS[column],
        )
        normalized_visible_columns = [
            column for column in selected_visible_columns if column in TABLE_COLUMN_LABELS
        ] or DEFAULT_VISIBLE_COLUMNS.copy()
        if normalized_visible_columns != visible_columns:
            save_visible_columns(normalized_visible_columns)
            st.rerun()
        thumbnail_display_mode = st.radio(
            "Thumbnail Display",
            options=["Off", "Table column"],
            index=0,
        )
        sort_field_to_label = {value: label for label, value in TABLE_SORT_LABEL_TO_FIELD.items()}
        selected_sort_label = st.selectbox(
            "Default Sort Field",
            options=list(TABLE_SORT_LABEL_TO_FIELD.keys()),
            index=list(TABLE_SORT_LABEL_TO_FIELD.keys()).index(
                sort_field_to_label.get(sort_field, "Favorited Time")
            ),
        )
        selected_sort_desc = st.toggle("Default Sort Descending", value=sort_desc)
        selected_sort_field = TABLE_SORT_LABEL_TO_FIELD[selected_sort_label]
        if selected_sort_field != sort_field:
            set_setting(TABLE_SORT_FIELD_KEY, selected_sort_field, DB_PATH)
            st.rerun()
        if selected_sort_desc != sort_desc:
            set_bool_setting(TABLE_SORT_DESC_KEY, selected_sort_desc, DB_PATH)
            st.rerun()

        saved_rule_sets = get_all_rule_sets(DB_PATH)
        external_fallback_enabled = get_bool_setting(FALLBACK_SETTING_KEY, False, DB_PATH)
        st.divider()
        st.subheader("Auto-Download Rules")
        active_names = [str(rule_set["name"]) for rule_set in active_rule_sets]
        active_label = ", ".join(active_names) if active_names else "None"
        st.caption(f"Active rule sets: {active_label}")
        fallback_to_external = st.toggle(
            "Auto-download externally when no torrent exists",
            value=external_fallback_enabled,
        )
        if fallback_to_external != external_fallback_enabled:
            set_bool_setting(FALLBACK_SETTING_KEY, fallback_to_external, DB_PATH)
            st.rerun()
        st.caption("When enabled, matching items without torrents can use archive/H@H options within the active GP cap.")

        render_download_queue_sidebar(config)

        rule_set_options: dict[str, int | None] = {"Create New Rule Set": None}
        for rule_set in saved_rule_sets:
            label = rule_set["name"]
            if rule_set.get("is_active"):
                label = f"{label} (Active)"
            rule_set_options[label] = int(rule_set["rule_set_id"])

        selected_rule_set_label = st.selectbox(
            "Edit Rule Set",
            options=list(rule_set_options.keys()),
        )
        selected_rule_set_id = rule_set_options[selected_rule_set_label]
        selected_rule_set = next(
            (rule_set for rule_set in saved_rule_sets if rule_set["rule_set_id"] == selected_rule_set_id),
            None,
        )

        with st.expander("Manage Rule Sets", expanded=False):
            st.caption(
                "Leave any field blank to ignore it. Text fields use case-insensitive contains matching."
            )

            with st.form("rule_set_editor_form"):
                rule_set_name = st.text_input(
                    "Rule Set Name",
                    value=optional_text(selected_rule_set.get("name") if selected_rule_set else ""),
                )
                make_active = st.checkbox(
                "Set this rule set active after save",
                value=bool(selected_rule_set.get("is_active")) if selected_rule_set else False,
                )
                auto_download_enabled = st.checkbox(
                    "Auto-download when this rule set matches",
                    value=bool(selected_rule_set.get("auto_download", True)) if selected_rule_set else True,
                )
                max_external_gp_text = st.text_input(
                    "Max External GP Spend",
                    value=optional_int_text(selected_rule_set.get("max_external_gp") if selected_rule_set else None),
                    placeholder="Blank = free only",
                )

                category_choice = st.selectbox(
                    "Gallery Category",
                    options=["Any", *category_options],
                    index=(
                        ["Any", *category_options].index(selected_rule_set["match_category"])
                        if selected_rule_set and selected_rule_set.get("match_category") in category_options
                        else 0
                    ),
                )
                favorite_choice = st.selectbox(
                    "Favorite Category",
                    options=["Any", *favorite_category_options],
                    index=(
                        ["Any", *favorite_category_options].index(selected_rule_set["match_favorite_category"])
                        if selected_rule_set and selected_rule_set.get("match_favorite_category") in favorite_category_options
                        else 0
                    ),
                )
                folder_slot_text = st.text_input(
                    "Favorite Slot",
                    value=optional_int_text(selected_rule_set.get("match_folder_slot") if selected_rule_set else None),
                    placeholder="Example: 1",
                )
                uploader_contains = st.text_input(
                    "Uploader Contains",
                    value=optional_text(selected_rule_set.get("uploader_contains") if selected_rule_set else ""),
                )
                title_contains = st.text_input(
                    "Title Contains",
                    value=optional_text(selected_rule_set.get("title_contains") if selected_rule_set else ""),
                )
                tag_contains = st.text_input(
                    "Tag Contains",
                    value=optional_text(selected_rule_set.get("tags_contains") if selected_rule_set else ""),
                    placeholder="Example: female:office",
                )
                note_contains = st.text_input(
                    "Note Contains",
                    value=optional_text(selected_rule_set.get("notes_contains") if selected_rule_set else ""),
                    placeholder="Example: note:favorite",
                )
                note_not_contains = st.text_input(
                    "Note Does Not Contain",
                    value=optional_text(selected_rule_set.get("notes_not_contains") if selected_rule_set else ""),
                    placeholder="Example: note:skip",
                )

                rating_cols = st.columns(2)
                rating_min_text = rating_cols[0].text_input(
                    "Rating Min",
                    value=optional_float_text(selected_rule_set.get("rating_min") if selected_rule_set else None),
                )
                rating_max_text = rating_cols[1].text_input(
                    "Rating Max",
                    value=optional_float_text(selected_rule_set.get("rating_max") if selected_rule_set else None),
                )

                size_cols = st.columns(2)
                filesize_min_text = size_cols[0].text_input(
                    "Filesize Min (MB)",
                    value=(
                        optional_float_text(
                            (selected_rule_set.get("filesize_min_bytes") or 0) / (1024 * 1024)
                        )
                        if selected_rule_set and selected_rule_set.get("filesize_min_bytes") is not None
                        else ""
                    ),
                )
                filesize_max_text = size_cols[1].text_input(
                    "Filesize Max (MB)",
                    value=(
                        optional_float_text(
                            (selected_rule_set.get("filesize_max_bytes") or 0) / (1024 * 1024)
                        )
                        if selected_rule_set and selected_rule_set.get("filesize_max_bytes") is not None
                        else ""
                    ),
                )

                filecount_cols = st.columns(2)
                filecount_min_text = filecount_cols[0].text_input(
                    "Filecount Min",
                    value=optional_int_text(selected_rule_set.get("filecount_min") if selected_rule_set else None),
                )
                filecount_max_text = filecount_cols[1].text_input(
                    "Filecount Max",
                    value=optional_int_text(selected_rule_set.get("filecount_max") if selected_rule_set else None),
                )

                posted_cols = st.columns(2)
                posted_from_date = posted_cols[0].date_input(
                    "Upload Date From",
                    value=epoch_date_value(selected_rule_set.get("posted_from_epoch") if selected_rule_set else None),
                )
                posted_to_date = posted_cols[1].date_input(
                    "Upload Date To",
                    value=epoch_date_value(selected_rule_set.get("posted_to_epoch") if selected_rule_set else None),
                )

                expunged_mode = st.selectbox(
                    "Expunged",
                    options=["any", "no", "yes"],
                    index=(
                        ["any", "no", "yes"].index(selected_rule_set.get("expunged_mode", "any"))
                        if selected_rule_set
                        else 0
                    ),
                    format_func=lambda value: {"any": "Any", "no": "No", "yes": "Yes"}[value],
                )

                create_new = st.form_submit_button("Save As New", width="stretch")
                save_changes = st.form_submit_button(
                    "Save Changes",
                    width="stretch",
                    disabled=selected_rule_set is None,
                )

                if create_new or save_changes:
                    try:
                        rule_payload = {
                            "rule_set_id": selected_rule_set["rule_set_id"] if save_changes and selected_rule_set else None,
                            "name": rule_set_name,
                            "is_active": make_active,
                            "auto_download": auto_download_enabled,
                            "match_category": "" if category_choice == "Any" else category_choice,
                            "match_favorite_category": "" if favorite_choice == "Any" else favorite_choice,
                            "match_folder_slot": parse_optional_int(folder_slot_text, "Favorite Slot"),
                            "uploader_contains": uploader_contains.strip(),
                            "title_contains": title_contains.strip(),
                            "tags_contains": tag_contains.strip(),
                            "notes_contains": note_contains.strip(),
                            "notes_not_contains": note_not_contains.strip(),
                            "rating_min": parse_optional_float(rating_min_text, "Rating Min"),
                            "rating_max": parse_optional_float(rating_max_text, "Rating Max"),
                            "filesize_min_bytes": parse_optional_megabytes(filesize_min_text, "Filesize Min"),
                            "filesize_max_bytes": parse_optional_megabytes(filesize_max_text, "Filesize Max"),
                            "filecount_min": parse_optional_int(filecount_min_text, "Filecount Min"),
                            "filecount_max": parse_optional_int(filecount_max_text, "Filecount Max"),
                            "posted_from_epoch": date_to_start_epoch(posted_from_date),
                            "posted_to_epoch": date_to_end_epoch(posted_to_date),
                            "expunged_mode": expunged_mode,
                            "max_external_gp": parse_optional_int(max_external_gp_text, "Max External GP Spend"),
                        }
                        if (
                            rule_payload["rating_min"] is not None
                            and rule_payload["rating_max"] is not None
                            and rule_payload["rating_min"] > rule_payload["rating_max"]
                        ):
                            raise ValueError("Rating Min cannot be greater than Rating Max.")
                        if (
                            rule_payload["filesize_min_bytes"] is not None
                            and rule_payload["filesize_max_bytes"] is not None
                            and rule_payload["filesize_min_bytes"] > rule_payload["filesize_max_bytes"]
                        ):
                            raise ValueError("Filesize Min cannot be greater than Filesize Max.")
                        if (
                            rule_payload["filecount_min"] is not None
                            and rule_payload["filecount_max"] is not None
                            and rule_payload["filecount_min"] > rule_payload["filecount_max"]
                        ):
                            raise ValueError("Filecount Min cannot be greater than Filecount Max.")
                        if (
                            rule_payload["posted_from_epoch"] is not None
                            and rule_payload["posted_to_epoch"] is not None
                            and rule_payload["posted_from_epoch"] > rule_payload["posted_to_epoch"]
                        ):
                            raise ValueError("Upload Date From cannot be later than Upload Date To.")
                        if (
                            rule_payload["max_external_gp"] is not None
                            and rule_payload["max_external_gp"] < 0
                        ):
                            raise ValueError("Max External GP Spend cannot be negative.")
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        save_rule_set(rule_payload, DB_PATH)
                        st.rerun()

            action_cols = st.columns(2)
            if action_cols[0].button(
                "Activate Selected",
                width="stretch",
                disabled=selected_rule_set is None,
            ) and selected_rule_set is not None:
                set_rule_set_active(int(selected_rule_set["rule_set_id"]), True, DB_PATH)
                st.rerun()

            if action_cols[1].button(
                "Deactivate All",
                width="stretch",
                disabled=not active_rule_sets,
            ):
                set_active_rule_set(None, DB_PATH)
                st.rerun()

            if st.button(
                "Delete Selected",
                width="stretch",
                disabled=selected_rule_set is None,
            ) and selected_rule_set is not None:
                delete_rule_set(int(selected_rule_set["rule_set_id"]), DB_PATH)
                st.rerun()

        st.divider()
        st.subheader("Saved Rule Sets")
        st.metric("Configured Rule Sets", len(saved_rule_sets))
        if saved_rule_sets:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "rule_set_id": rule_set["rule_set_id"],
                            "name": rule_set["name"],
                            "active": bool(rule_set["is_active"]),
                            "auto_download": bool(rule_set["auto_download"]),
                            "filters": describe_rule_set(rule_set),
                        }
                        for rule_set in saved_rule_sets
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("No rule sets saved yet.")

    pending_search_text = st.session_state.pop(SEARCH_TEXT_PENDING_KEY, None)
    if pending_search_text is not None:
        st.session_state[SEARCH_TEXT_KEY] = pending_search_text

    search_text = st.text_input(
        "Search",
        placeholder="Search title/title_jpn, or use uploader:name, tag:teacher, note:phrase",
        key=SEARCH_TEXT_KEY,
    )
    selected_categories = st.multiselect("Categories", options=category_options)
    show_superseded = st.checkbox("Show superseded revisions", value=False)
    only_user_created_torrents = st.checkbox("Only user-created torrents", value=False)
    filtered_items = filter_items(
        items,
        selected_categories,
        search_text,
        show_superseded,
        only_user_created_torrents,
    )
    displayed_items = sort_items_for_table(filtered_items, sort_field, sort_desc)

    selected_items: list[dict[str, Any]] = []
    metric_cols = st.columns(3)
    render_summary(metric_cols, filtered_items)

    st.subheader("Items")
    table = build_table(displayed_items)
    if table.empty:
        st.info("The database is initialized, but there are no matching items yet.")
    else:
        column_order = normalized_visible_columns.copy()
        if thumbnail_display_mode == "Table column":
            column_order = ["thumbnail", *column_order]

        table_event = st.dataframe(
            table,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode=["multi-row", "single-cell"],
            key="items_table",
            column_config={
                "thumbnail": st.column_config.ImageColumn(
                    "Thumbnail",
                    width=140,
                    pinned=True,
                ),
                "gallery_filesize": st.column_config.NumberColumn(
                    TABLE_COLUMN_LABELS["gallery_filesize"],
                    format="bytes",
                    width="small",
                ),
                "filecount": st.column_config.NumberColumn(
                    TABLE_COLUMN_LABELS["filecount"],
                    width="small",
                ),
                "expunged": st.column_config.CheckboxColumn(
                    TABLE_COLUMN_LABELS["expunged"],
                    width="small",
                ),
                "torrent_downloaded": st.column_config.CheckboxColumn(
                    TABLE_COLUMN_LABELS["torrent_downloaded"],
                    width="small",
                ),
                "torrent_from_archive": st.column_config.CheckboxColumn(
                    TABLE_COLUMN_LABELS["torrent_from_archive"],
                    width="small",
                ),
                "archive_downloaded": st.column_config.CheckboxColumn(
                    TABLE_COLUMN_LABELS["archive_downloaded"],
                    width="small",
                ),
                "matches_active_rule": st.column_config.CheckboxColumn(
                    TABLE_COLUMN_LABELS["matches_active_rule"],
                    width="small",
                ),
                "title": st.column_config.TextColumn(TABLE_COLUMN_LABELS["title"], width="large"),
                "title_jpn": st.column_config.TextColumn(
                    TABLE_COLUMN_LABELS["title_jpn"],
                    width="large",
                ),
                "tags": st.column_config.TextColumn(TABLE_COLUMN_LABELS["tags"], width="large"),
                "uploader": st.column_config.TextColumn(TABLE_COLUMN_LABELS["uploader"], width="medium"),
                "favorite_category": st.column_config.TextColumn(
                    TABLE_COLUMN_LABELS["favorite_category"],
                    width="medium",
                ),
                "favorited_time": st.column_config.DatetimeColumn(
                    TABLE_COLUMN_LABELS["favorited_time"],
                    format="YYYY-MM-DD HH:mm:ss",
                    width="medium",
                ),
                "custom_folder_slot": st.column_config.NumberColumn(
                    TABLE_COLUMN_LABELS["custom_folder_slot"],
                    width="small",
                ),
                "site_category": st.column_config.TextColumn(
                    TABLE_COLUMN_LABELS["site_category"],
                    width="medium",
                ),
                "rating": st.column_config.TextColumn(TABLE_COLUMN_LABELS["rating"], width="small"),
                "torrent_count": st.column_config.NumberColumn(
                    TABLE_COLUMN_LABELS["torrent_count"],
                    width="small",
                ),
                "best_torrent_name": st.column_config.TextColumn(
                    TABLE_COLUMN_LABELS["best_torrent_name"],
                    width="large",
                ),
                "local_status": st.column_config.TextColumn(
                    TABLE_COLUMN_LABELS["local_status"],
                    width="medium",
                ),
                "last_updated": st.column_config.DatetimeColumn(
                    TABLE_COLUMN_LABELS["last_updated"],
                    format="YYYY-MM-DD HH:mm:ss",
                    width="medium",
                ),
                "parent_gid": st.column_config.TextColumn(
                    TABLE_COLUMN_LABELS["parent_gid"],
                    width="small",
                ),
                "first_gid": st.column_config.TextColumn(
                    TABLE_COLUMN_LABELS["first_gid"],
                    width="small",
                ),
                "id": st.column_config.TextColumn(TABLE_COLUMN_LABELS["id"], width="small"),
            },
            column_order=column_order,
            row_height=110 if thumbnail_display_mode == "Table column" else None,
        )
        selected_row_indices = get_selected_row_indices(table_event)
        selected_items = [
            displayed_items[row_index]
            for row_index in selected_row_indices
            if 0 <= row_index < len(displayed_items)
        ]

    archived_selected = [
        item for item in selected_items if bool(item.get("archive_downloaded", False))
    ]
    feedback = st.session_state.pop("mass_conversion_feedback", None)
    if feedback:
        st.caption(
            f"Mass conversion: processed {feedback.get('processed', 0)} / "
            f"{feedback.get('total', 0)} items (batch limit {feedback.get('max_batch', 0)})."
        )
        for message in feedback.get("messages", []):
            st.caption(message)

    if archived_selected:
        if st.button(
            "Convert Selected Archives to Torrents",
            key="convert_selected_archives",
            type="primary",
            width="stretch",
        ):
            with st.spinner("Converting archives to torrents..."):
                messages, processed, limit = _run_mass_archive_conversion(
                    archived_selected, config or {}
                )
            st.session_state["mass_conversion_feedback"] = {
                "messages": messages,
                "processed": processed,
                "max_batch": limit,
                "total": len(archived_selected),
            }
            st.experimental_rerun()

    st.subheader("Item Detail")
    if not items:
        st.info("No items in the database.")
        return

    if not filtered_items:
        st.info("No items match the current filters.")
        return

    selected_id = sync_selected_item_from_table_event(displayed_items, table_event if not table.empty else {})
    if selected_id is None:
        st.info("Select an item from the table.")
        return

    selected_item = next(item for item in displayed_items if item["id"] == selected_id)
    item_torrents = get_torrents_for_item(selected_id, DB_PATH)

    if st.session_state.get(ARCHIVE_DIALOG_ITEM_KEY) == selected_id:
        render_archive_dialog(selected_item)

    detail_left, detail_right = st.columns([2, 1])
    with detail_left:
        if not bool(selected_item.get("is_latest_revision", True)):
            replacement_id = str(selected_item.get("superseded_by_id", "") or "a newer revision")
            st.warning(f"This gallery revision has been superseded by {replacement_id}.")
        elif selected_item.get("parent_gid") or selected_item.get("first_gid"):
            st.caption("Latest known revision in a replacement chain.")

        if not active_rule_sets:
            st.caption("No active auto-download rule sets.")
        elif bool(selected_item.get("matches_active_rule", False)):
            matched_names = ", ".join(selected_item.get("matched_rule_names", []))
            st.success(f"Matches active rule set(s): {matched_names}")
        else:
            st.caption(
                "Does not match active rule set(s): "
                + ", ".join(str(rule_set["name"]) for rule_set in active_rule_sets)
            )

        meta_left, meta_right = st.columns([3, 2])
        with meta_left:
            st.write(f"**Title:** {unescape(selected_item.get('title', ''))}")
            st.write(f"**Japanese Title:** {unescape(selected_item.get('title_jpn', '')) or 'n/a'}")
            render_thumbnail(selected_item.get("thumbnail_local_path"))
        with meta_right:
            item_url = str(selected_item.get("item_url", "") or "")
            if item_url:
                st.markdown(f"<{item_url}>")
            else:
                st.write("n/a")
            favorite_note = str(selected_item.get("favorite_note", "") or "").strip()
            if favorite_note:
                st.write(f"**Note:** {favorite_note}")
            st.write(f"**Uploader:** {unescape(selected_item.get('uploader', '')) or 'n/a'}")
            st.write(f"**Rating:** {selected_item.get('rating') or 'n/a'}")
            st.write(f"**Last Updated:** {epoch_label(selected_item.get('last_updated_epoch'))}")
            st.write(f"**Gallery Category:** {selected_item.get('site_category') or 'n/a'}")
            st.write(f"**Favorite Category:** {selected_item.get('favorite_category') or 'n/a'}")
            st.write(f"**Favorited Time:** {epoch_label(selected_item.get('favorited_epoch'))}")
            st.write(f"**Gallery Size:** {bytes_label(selected_item.get('gallery_filesize', 0))}")
            st.write(f"**File Count:** {int(selected_item.get('filecount', 0) or 0)}")

        extra_meta_col, tags_col = st.columns([1, 2])
        with extra_meta_col:
            if st.button(
                "Force Refresh Gallery Data",
                key=f"refresh_gallery_{selected_id}",
                width="stretch",
            ):
                with st.spinner("Refreshing gallery metadata..."):
                    result = worker.refresh_gallery_metadata(selected_id)
                if bool(result.get("success")):
                    st.success(
                        "Gallery refreshed. "
                        f"Torrents now known: {int(result.get('torrent_count', 0) or 0)}."
                    )
                    st.rerun()
                st.error(
                    "Gallery refresh failed: "
                    f"{result.get('message') or result.get('reason') or 'unknown error'}"
                )
            st.write(f"**Expunged:** {'Yes' if selected_item.get('expunged') else 'No'}")
            st.write(f"**Current GID:** {selected_item.get('id') or 'n/a'}")
            st.write(f"**Parent GID:** {selected_item.get('parent_gid') or 'n/a'}")
            st.write(f"**First GID:** {selected_item.get('first_gid') or 'n/a'}")
            st.write(f"**Torrent Count:** {int(selected_item.get('torrent_count', 0) or 0)}")
            st.write(f"**Best Torrent:** {selected_item.get('best_torrent_name') or 'n/a'}")
        with tags_col:
            st.write("**Tags**")
            render_tag_groups(selected_item.get("tags", ""))
            with st.expander("Raw Tags", expanded=False):
                st.code(str(selected_item.get("tags", "")), language="json")

        st.write("**Torrents**")
        if item_torrents:
            st.dataframe(build_torrent_table(item_torrents), width="stretch", hide_index=True)
        else:
            st.caption("No torrents stored for this item.")

    with detail_right:
        render_item_status_panel(selected_id, config)

    st.subheader("Export")
    export_rows = exportable_items(displayed_items)
    export_frame = pd.DataFrame(export_rows)
    st.download_button(
        "Download visible items as JSON",
        data=json.dumps(export_rows, indent=2, ensure_ascii=False),
        file_name="items_export.json",
        mime="application/json",
        width="stretch",
    )
    st.download_button(
        "Download visible items as CSV",
        data=export_frame.to_csv(index=False),
        file_name="items_export.csv",
        mime="text/csv",
        width="stretch",
    )


if __name__ == "__main__":
    main()

