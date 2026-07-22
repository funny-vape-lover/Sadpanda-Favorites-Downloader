from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

import database
import media_importer


LANRARAGI_FORCE_SYNC_KEY = "lanraragi_sync_requested"
LANRARAGI_LAST_SYNC_EPOCH_KEY = "lanraragi_last_sync_epoch"
LANRARAGI_LAST_STATUS_KEY = "lanraragi_last_sync_status_json"
DEFAULT_SYNC_INTERVAL_MINUTES = 60
ITEM_ID_PATTERN = re.compile(r"^g-(\d+)$", re.IGNORECASE)
NS_CATEGORY = "category"
NS_UPLOADER = "uploader"
NS_TIMESTAMP = "timestamp"
NS_SOURCE = "source"
NS_DATE_ADDED = "date_added"
NS_RATING = "rating"
NS_FAVORITE_CATEGORY = "favorite_category"
NS_FAVORITE_NOTE = "favorite_note"
NS_JAPANESE_TITLE = "japanese_title"
NS_EXPUNGED = "expunged"
NS_PREVIOUS_GID = "previous_gid"
NS_GID = "gid"
LANRARAGI_MANAGED_NAMESPACES = {
    NS_CATEGORY.casefold(),
    NS_UPLOADER.casefold(),
    NS_TIMESTAMP.casefold(),
    NS_SOURCE.casefold(),
    NS_DATE_ADDED.casefold(),
    NS_RATING.casefold(),
    NS_FAVORITE_CATEGORY.casefold(),
    NS_FAVORITE_NOTE.casefold(),
    NS_JAPANESE_TITLE.casefold(),
    NS_EXPUNGED.casefold(),
    NS_PREVIOUS_GID.casefold(),
    NS_GID.casefold(),
    # Legacy aliases to clean old sync/plugin output.
    "category",
    "Category",
    "gallery_category",
    "uploader",
    "Uploader",
    "timestamp",
    "Timestamp",
    "source",
    "Source",
    "date_added",
    "Date Added",
    "favorite_category",
    "Favorite Category",
    "favorite_note",
    "Favorite Note",
    "rating",
    "Rating",
    "expunged",
    "Expunged",
    "previous_gid",
    "Previous GID",
    "gid",
    "GID",
    "exh_gid",
    "title_jpn",
    "Japanese Title",
}


def run_scheduled_sync(db_path: str | Path, config: dict[str, Any]) -> dict[str, Any]:
    lan_cfg = _normalize_lanraragi_config(config)
    if not lan_cfg["enabled"]:
        return {"skipped": True, "reason": "disabled"}
    if not lan_cfg["base_url"] or not lan_cfg["api_key"]:
        _set_status(
            db_path,
            state="failed",
            message="LANraragi sync is enabled but base_url/api_key is missing.",
            matched=0,
            updated=0,
            skipped=0,
            failed=0,
        )
        return {"skipped": True, "reason": "missing_credentials"}

    force_requested = database.get_bool_setting(LANRARAGI_FORCE_SYNC_KEY, False, db_path)
    interval_seconds = max(1, int(lan_cfg["sync_interval_minutes"])) * 60
    now_epoch = int(time.time())
    last_sync_raw = database.get_setting(LANRARAGI_LAST_SYNC_EPOCH_KEY, "0", db_path)
    try:
        last_sync_epoch = int(last_sync_raw)
    except (TypeError, ValueError):
        last_sync_epoch = 0

    if not force_requested and last_sync_epoch > 0 and (now_epoch - last_sync_epoch) < interval_seconds:
        return {"skipped": True, "reason": "interval_not_elapsed"}

    _set_status(
        db_path,
        state="running",
        message="Syncing favorited metadata to LANraragi.",
        matched=0,
        updated=0,
        skipped=0,
        failed=0,
    )
    try:
        result = sync_favorites_metadata(db_path, lan_cfg)
    except Exception as exc:  # noqa: BLE001
        _set_status(
            db_path,
            state="failed",
            message=str(exc),
            matched=0,
            updated=0,
            skipped=0,
            failed=0,
        )
        raise

    database.set_bool_setting(LANRARAGI_FORCE_SYNC_KEY, False, db_path)
    database.set_setting(LANRARAGI_LAST_SYNC_EPOCH_KEY, str(now_epoch), db_path)
    _set_status(
        db_path,
        state="completed",
        message="LANraragi metadata sync completed.",
        matched=result["matched"],
        updated=result["updated"],
        skipped=result["skipped"],
        failed=result["failed"],
    )
    return result


def sync_favorites_metadata(db_path: str | Path, lan_cfg: dict[str, Any]) -> dict[str, int]:
    session = _build_session(lan_cfg["api_key"])
    base_url = str(lan_cfg["base_url"]).rstrip("/")
    items = _get_items_for_sync(db_path)

    matched = 0
    updated = 0
    skipped = 0
    failed = 0

    for item in items:
        gid = _extract_gid(item.get("id"))
        if not gid:
            skipped += 1
            continue
        try:
            candidate = _find_archive_candidate(session, base_url, gid)
            matched_gid = gid
            revision_candidates = list(item.get("revision_candidates", []))
            if candidate is None and revision_candidates:
                local_arcid, local_path = _compute_local_lanraragi_arcid(
                    item=item,
                    revision_candidates=revision_candidates,
                    media_library=str(lan_cfg.get("media_library", "") or ""),
                )
                if local_arcid:
                    for revision in revision_candidates:
                        previous_gid = str(revision.get("gid", "") or "")
                        if not previous_gid:
                            continue
                        previous_candidate = _find_archive_candidate(
                            session,
                            base_url,
                            previous_gid,
                        )
                        if previous_candidate is None:
                            continue
                        candidate_arcid = str(previous_candidate.get("arcid", "") or "").lower()
                        if candidate_arcid != local_arcid:
                            print(
                                f"Rejected LANraragi revision fallback for g-{gid} via "
                                f"g-{previous_gid}: payload arcid mismatch."
                            )
                            continue
                        candidate = previous_candidate
                        matched_gid = previous_gid
                        print(
                            f"LANraragi revision fallback matched g-{gid} to g-{previous_gid} "
                            f"using identical payload {local_arcid} ({local_path})."
                        )
                        break
            if candidate is None:
                skipped += 1
                continue
            matched += 1
            archive_id = str(candidate.get("arcid") or candidate.get("id") or "")
            if not archive_id:
                skipped += 1
                continue

            current = _fetch_metadata(session, base_url, archive_id, candidate)
            next_tags = _build_synced_tags(
                raw_tags=str(current.get("tags", "") or ""),
                item=item,
                gid=gid,
                previous_gids=_revision_gid_values(revision_candidates, matched_gid),
            )
            next_title = (
                unescape(str(item.get("title", "") or "")).strip()
                or unescape(str(item.get("title_jpn", "") or "")).strip()
                or str(current.get("title", "") or candidate.get("title", "") or "").strip()
            )
            current_tags = str(current.get("tags", "") or "").strip()
            current_title = str(current.get("title", "") or "").strip()
            if (
                _canonicalize_tags(current_tags) == _canonicalize_tags(next_tags)
                and current_title == next_title
            ):
                continue

            payload = {
                "title": next_title,
                "tags": next_tags,
                "summary": str(current.get("summary", "") or candidate.get("summary", "") or ""),
            }
            response = session.put(
                f"{base_url}/api/archives/{archive_id}/metadata",
                data=payload,
                timeout=30,
            )
            response.raise_for_status()
            updated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"LANraragi sync failed for {item.get('id')}: {exc}")

    return {
        "matched": matched,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
    }


def _normalize_lanraragi_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("lanraragi", {}) if isinstance(config, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        interval = int(raw.get("sync_interval_minutes", DEFAULT_SYNC_INTERVAL_MINUTES))
    except (TypeError, ValueError):
        interval = DEFAULT_SYNC_INTERVAL_MINUTES
    interval = max(1, interval)
    paths_cfg = config.get("paths", {}) if isinstance(config, dict) else {}
    if not isinstance(paths_cfg, dict):
        paths_cfg = {}

    return {
        "enabled": bool(raw.get("enabled", False)),
        "base_url": str(raw.get("base_url", "") or "").strip(),
        "api_key": str(raw.get("api_key", "") or "").strip(),
        "sync_interval_minutes": interval,
        "media_library": str(paths_cfg.get("media_library", "") or "").strip(),
    }


def _set_status(
    db_path: str | Path,
    *,
    state: str,
    message: str,
    matched: int,
    updated: int,
    skipped: int,
    failed: int,
) -> None:
    payload = {
        "state": state,
        "message": message,
        "matched": int(matched),
        "updated": int(updated),
        "skipped": int(skipped),
        "failed": int(failed),
        "updated_epoch": int(time.time()),
    }
    database.set_setting(LANRARAGI_LAST_STATUS_KEY, json.dumps(payload, ensure_ascii=True), db_path)


def _build_session(api_key: str) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    encoded = base64.b64encode(api_key.encode("utf-8")).decode("ascii")
    session.headers.update({"Authorization": f"Bearer {encoded}"})
    return session


def _extract_gid(item_id: Any) -> str:
    match = ITEM_ID_PATTERN.match(str(item_id or "").strip())
    if not match:
        return ""
    return match.group(1)


def _get_items_for_sync(db_path: str | Path) -> list[dict[str, Any]]:
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                title_jpn,
                item_url,
                uploader,
                site_category,
                favorite_category,
                last_updated_epoch,
                rating,
                expunged,
                tags,
                favorite_note,
                favorited_epoch,
                parent_gid,
                first_gid
            FROM items
            WHERE COALESCE(is_latest_revision, 1) = 1
              AND (torrent_downloaded = 1 OR archive_downloaded = 1)
            ORDER BY favorited_epoch DESC, last_updated_epoch DESC
            """
        ).fetchall()
    items = [dict(row) for row in rows]
    records, graph = _load_revision_graph(db_path)
    for item in items:
        item["revision_candidates"] = _get_revision_candidates(item, records, graph)
    return items


def _normalize_item_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = ITEM_ID_PATTERN.match(text)
    if match:
        return f"g-{match.group(1)}"
    if text.isdigit():
        return f"g-{text}"
    return ""


def _load_revision_graph(
    db_path: str | Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, title, parent_gid, first_gid, superseded_by_id
            FROM items
            """
        ).fetchall()

    records: dict[str, dict[str, Any]] = {}
    graph: dict[str, set[str]] = {}
    for row in rows:
        record = dict(row)
        item_id = _normalize_item_id(record.get("id"))
        if not item_id:
            continue
        records[item_id] = record
        graph.setdefault(item_id, set())
        related_ids = (
            _normalize_item_id(record.get("parent_gid")),
            _normalize_item_id(record.get("first_gid")),
            _normalize_item_id(record.get("superseded_by_id")),
        )
        for related_id in related_ids:
            if not related_id or related_id == item_id:
                continue
            graph.setdefault(related_id, set())
            graph[item_id].add(related_id)
            graph[related_id].add(item_id)
    return records, graph


def _get_revision_candidates(
    item: dict[str, Any],
    records: dict[str, dict[str, Any]],
    graph: dict[str, set[str]],
) -> list[dict[str, str]]:
    item_id = _normalize_item_id(item.get("id"))
    if not item_id:
        return []

    ordered_ids: list[str] = []
    seen: set[str] = {item_id}

    def add_candidate(candidate_id: str) -> None:
        if candidate_id and candidate_id not in seen:
            seen.add(candidate_id)
            ordered_ids.append(candidate_id)

    add_candidate(_normalize_item_id(item.get("parent_gid")))
    add_candidate(_normalize_item_id(item.get("first_gid")))

    queue = [item_id]
    traversed = {item_id}
    while queue:
        current = queue.pop(0)
        for related_id in sorted(graph.get(current, set())):
            if related_id not in traversed:
                traversed.add(related_id)
                queue.append(related_id)
            add_candidate(related_id)

    candidates: list[dict[str, str]] = []
    for candidate_id in ordered_ids:
        gid = _extract_gid(candidate_id)
        if not gid:
            continue
        record = records.get(candidate_id, {})
        candidates.append(
            {
                "id": candidate_id,
                "gid": gid,
                "title": str(record.get("title", "") or ""),
            }
        )
    return candidates


def _compute_local_lanraragi_arcid(
    *,
    item: dict[str, Any],
    revision_candidates: list[dict[str, str]],
    media_library: str,
) -> tuple[str, str]:
    if not media_library:
        return "", ""
    library_path = Path(media_library).expanduser()
    if not library_path.is_dir():
        return "", ""

    entries = media_importer._list_media_library_entries(library_path)  # type: ignore[attr-defined]
    media_items = [
        {"id": str(item.get("id", "") or ""), "title": str(item.get("title", "") or "")},
        *revision_candidates,
    ]
    for media_item in media_items:
        try:
            path = media_importer._find_media_library_file(  # type: ignore[attr-defined]
                library_path,
                str(media_item.get("id", "") or ""),
                str(media_item.get("title", "") or ""),
                media_entries=entries,
            )
        except (FileNotFoundError, OSError, ValueError):
            continue
        if not path.is_file():
            continue
        digest = hashlib.sha1()
        with path.open("rb") as handle:
            digest.update(handle.read(512000))
        return digest.hexdigest(), str(path)
    return "", ""


def _revision_gid_values(
    revision_candidates: list[dict[str, str]],
    matched_gid: str,
) -> list[str]:
    values = [str(candidate.get("gid", "") or "") for candidate in revision_candidates]
    if matched_gid and matched_gid not in values:
        values.insert(0, matched_gid)
    return list(dict.fromkeys(value for value in values if value))


def _find_archive_candidate(
    session: requests.Session,
    base_url: str,
    gid: str,
) -> dict[str, Any] | None:
    searches = [
        f"gid:{gid}",
        f"GID:{gid}",
        f"previous_gid:{gid}",
        f"exh_gid:{gid}",
        f"g-[{gid}]",
        f"g-{gid}",
    ]
    for search in searches:
        results = _search_archives(session, base_url, search)
        if not results:
            continue
        exact = [
            row
            for row in results
            if _has_gid_match(row, gid)
        ]
        if len(exact) == 1:
            return exact[0]
        if exact:
            return exact[0]
        if len(results) == 1:
            return results[0]
    return None


def _search_archives(session: requests.Session, base_url: str, search_text: str) -> list[dict[str, Any]]:
    response = session.get(
        f"{base_url}/api/search",
        params={
            "filter": search_text,
            "sortby": "title",
            "order": "asc",
            "start": -1,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data", []) if isinstance(payload, dict) else []
    return [row for row in data if isinstance(row, dict)]


def _has_gid_match(row: dict[str, Any], gid: str) -> bool:
    tags = str(row.get("tags", "") or "").casefold()
    title = str(row.get("title", "") or "")
    return (
        f"gid:{gid}" in tags
        or f"previous_gid:{gid}" in tags
        or f"exh_gid:{gid}" in tags
        or f"g-[{gid}]" in title
        or f"g-{gid}" in title
    )


def _fetch_metadata(
    session: requests.Session,
    base_url: str,
    archive_id: str,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    response = session.get(f"{base_url}/api/archives/{archive_id}/metadata", timeout=30)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and "arcid" in payload:
        return payload
    return {
        "title": fallback.get("title", ""),
        "tags": fallback.get("tags", ""),
        "summary": fallback.get("summary", ""),
    }


def _build_synced_tags(
    *,
    raw_tags: str,
    item: dict[str, Any],
    gid: str,
    previous_gids: list[str] | None = None,
) -> str:
    # For now we do a full managed rewrite to keep output stable and avoid duplicates.
    gallery_tags = _drop_managed_tags(_parse_item_tags(item.get("tags", "")), LANRARAGI_MANAGED_NAMESPACES)
    managed_tags = _build_managed_metadata_tags(
        item=item,
        gid=gid,
        previous_gids=previous_gids or [],
    )
    return ", ".join(_dedupe_tags([*gallery_tags, *managed_tags]))


def _build_managed_metadata_tags(
    *,
    item: dict[str, Any],
    gid: str,
    previous_gids: list[str],
) -> list[str]:
    tags: list[str] = []

    category_value = _sanitize_tag_value(str(item.get("site_category", "") or "").lower())
    if category_value:
        tags.append(f"{NS_CATEGORY}:{category_value}")

    uploader = _sanitize_tag_value(unescape(str(item.get("uploader", "") or "")))
    if uploader:
        tags.append(f"{NS_UPLOADER}:{uploader}")

    posted_epoch = int(item.get("last_updated_epoch", 0) or 0)
    if posted_epoch > 0:
        tags.append(f"{NS_TIMESTAMP}:{posted_epoch}")

    source_tag = _build_source_tag(str(item.get("item_url", "") or ""))
    if source_tag:
        tags.append(source_tag)

    favorited_epoch = int(item.get("favorited_epoch", 0) or 0)
    if favorited_epoch > 0:
        tags.append(f"{NS_DATE_ADDED}:{favorited_epoch}")

    rating_value = _sanitize_tag_value(str(item.get("rating", "") or ""))
    if rating_value:
        tags.append(f"{NS_RATING}:{rating_value}")

    favorite_category = _sanitize_tag_value(str(item.get("favorite_category", "") or ""))
    if favorite_category:
        tags.append(f"{NS_FAVORITE_CATEGORY}:{favorite_category}")

    cleaned_note = _sanitize_tag_value(str(item.get("favorite_note", "") or ""))
    if cleaned_note:
        tags.append(f"{NS_FAVORITE_NOTE}:{cleaned_note}")

    japanese_title = _sanitize_tag_value(unescape(str(item.get("title_jpn", "") or "")))
    if japanese_title:
        tags.append(f"{NS_JAPANESE_TITLE}:{japanese_title}")

    expunged = bool(item.get("expunged", False))
    tags.append(f"{NS_EXPUNGED}:{'Yes' if expunged else 'No'}")
    for previous_gid in previous_gids:
        if previous_gid and previous_gid != gid:
            tags.append(f"{NS_PREVIOUS_GID}:{previous_gid}")
    tags.append(f"{NS_GID}:{gid}")

    return tags


def _build_source_tag(item_url: str) -> str:
    parsed = urlparse(item_url.strip())
    if not parsed.netloc or not parsed.path:
        return ""
    path = parsed.path.strip()
    if path.endswith("/"):
        path = path[:-1]
    if not path:
        return ""
    return f"{NS_SOURCE}:{parsed.netloc}{path}"


def _parse_item_tags(raw_tags: Any) -> list[str]:
    if isinstance(raw_tags, list):
        return [str(tag).strip() for tag in raw_tags if str(tag).strip()]
    text = str(raw_tags or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(tag).strip() for tag in parsed if str(tag).strip()]
    except (TypeError, ValueError):
        pass
    return _split_tags(text)


def _drop_managed_tags(tags: list[str], managed_namespaces: set[str]) -> list[str]:
    kept: list[str] = []
    for tag in tags:
        namespace, _ = _split_namespace(tag)
        if namespace and namespace.casefold() in managed_namespaces:
            continue
        kept.append(tag)
    return kept


def _split_namespace(tag: str) -> tuple[str, str]:
    text = str(tag or "").strip()
    if ":" not in text:
        return "", text
    namespace, value = text.split(":", 1)
    return namespace.strip(), value.strip()


def _dedupe_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for tag in tags:
        cleaned = str(tag or "").strip()
        if not cleaned:
            continue
        marker = cleaned.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(cleaned)
    return deduped


def _split_tags(raw_tags: str) -> list[str]:
    return [part.strip() for part in str(raw_tags or "").split(",") if part.strip()]


def _sanitize_tag_value(value: str) -> str:
    cleaned = str(value or "").replace(",", ";").replace("\n", " ").replace("\r", " ").strip()
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > 180:
        cleaned = cleaned[:180].rstrip()
    return cleaned


def _canonicalize_tags(raw_tags: str) -> str:
    return ", ".join(_split_tags(raw_tags))
