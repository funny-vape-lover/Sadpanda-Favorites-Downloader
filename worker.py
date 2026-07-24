from __future__ import annotations

import media_importer
import importlib
import json
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import database
import download_manager
import external_adapter
import external_manager
import health_checker
import lanraragi_sync
import torrent_uploader


APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / "config.json"
THUMBNAILS_DIR = APP_DIR / "thumbnails"
DB_PATH = APP_DIR / "dashboard.db"
FAVORITES_URL = "https://exhentai.org/favorites.php"
REQUEST_TIMEOUT = 30
METADATA_REFRESH_SECONDS = 60
OPERATIONAL_REFRESH_SECONDS = 5
TORRENT_MAINTENANCE_SECONDS = 60
ARCHIVE_MAINTENANCE_SECONDS = 300
HEALTH_CHECK_SECONDS = 1800
MEDIA_NORMALIZATION_SECONDS = 21600
LANRARAGI_SYNC_POLL_SECONDS = 60
FAVORITES_SORT_FAVORITED = "f"
FAVORITES_SORT_PUBLISHED = "p"
FAVORITES_DISPLAY_MODE = "extended"
FAVORITES_DISPLAY_INLINE_EXTENDED = "e"
_FAVORITES_DISPLAY_NORMALIZATION = {
    "m": "m",
    "minimal": "m",
    "p": "p",
    "minimal+": "p",
    "l": "l",
    "compact": "l",
    "e": "e",
    "extended": "e",
    "t": "t",
    "thumbnail": "t",
}
FULL_RESCAN_INTERVAL_KEY = "favorites_full_rescan_interval_hours"
FAVORITES_RESCAN_FLAG_KEY = "favorites_rescan_requested"
FAVORITES_LAST_REFRESH_KEY = "favorites_last_refresh_epoch"
FAVORITES_RESCAN_STATUS_KEY = "favorites_rescan_status_json"
CONFIG_READY_FLAG_KEY = "runtime_config_ready"
DEFAULT_FULL_RESCAN_INTERVAL_HOURS = 24


def _ensure_display_mode(url: str) -> str:
    if "display=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}display={FAVORITES_DISPLAY_MODE}"


def _normalize_display_value(value: str) -> str:
    if not value:
        return ""
    return _FAVORITES_DISPLAY_NORMALIZATION.get(value.strip().lower(), "")


def _extract_display_mode(soup: BeautifulSoup, response_url: str) -> str:
    for select in soup.select("div.searchnav select"):
        onchange = str(select.get("onchange", "") or "")
        if "inline_set=dm_" not in onchange:
            continue
        option = select.find("option", selected=True) or select.find("option")
        if option:
            normalized = _normalize_display_value(str(option.get("value", "") or ""))
            if normalized:
                return normalized

    if response_url:
        parsed = urlparse(response_url)
        query = parse_qs(parsed.query)
        for raw in query.get("inline_set", []):
            if raw.startswith("dm_"):
                normalized = _normalize_display_value(raw.split("dm_", 1)[1])
                if normalized:
                    return normalized
        for raw in query.get("display", []):
            normalized = _normalize_display_value(raw)
            if normalized:
                return normalized
    return ""


def _full_rescan_interval_seconds() -> int:
    raw_value = database.get_setting(
        FULL_RESCAN_INTERVAL_KEY, str(DEFAULT_FULL_RESCAN_INTERVAL_HOURS), DB_PATH
    )
    try:
        hours = int(raw_value)
    except (TypeError, ValueError):
        hours = DEFAULT_FULL_RESCAN_INTERVAL_HOURS
    hours = max(1, hours)
    return hours * 3600


def _has_required_runtime_config(config: dict[str, object] | None) -> bool:
    if not isinstance(config, dict):
        return False
    exhentai_cfg = config.get("exhentai")
    paths_cfg = config.get("paths")
    qb_cfg = config.get("qbittorrent")
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


def _set_favorites_rescan_status(
    *,
    state: str,
    phase: str,
    message: str,
    pages_scanned: int = 0,
    new_items_found: int = 0,
    favorite_metadata_updates: int = 0,
    items_changed: int = 0,
    torrents_changed: int = 0,
    started_epoch: int | None = None,
) -> None:
    now_epoch = int(time.time())
    payload: dict[str, int | str] = {
        "state": str(state),
        "phase": str(phase),
        "message": str(message),
        "pages_scanned": int(pages_scanned),
        "new_items_found": int(new_items_found),
        "favorite_metadata_updates": int(favorite_metadata_updates),
        "items_changed": int(items_changed),
        "torrents_changed": int(torrents_changed),
        "updated_epoch": now_epoch,
    }
    payload["started_epoch"] = int(started_epoch or now_epoch)
    try:
        database.set_setting(
            FAVORITES_RESCAN_STATUS_KEY,
            json.dumps(payload, ensure_ascii=True),
            DB_PATH,
        )
    except Exception:
        pass


def build_favorite_category_map(soup: BeautifulSoup) -> dict[str, int]:
    category_map: dict[str, int] = {}
    for block in soup.select("div.fp"):
        onclick = block.get("onclick", "")
        match = re.search(r"favcat=(\d+)", onclick)
        if not match:
            continue

        label_nodes = block.find_all("div")
        if not label_nodes:
            continue
        label = label_nodes[-1].get_text(" ", strip=True)
        if label:
            category_map[label] = int(match.group(1))
    return category_map


def parse_favorites_page(soup: BeautifulSoup) -> list[dict[str, int | str | None]]:
    category_map = build_favorite_category_map(soup)
    parsed_items: list[dict[str, int | str | None]] = []
    seen_ids: set[str] = set()
    gallery_href_pattern = re.compile(
        r"(?:https?://(?:exhentai|e-hentai)\.org)?/g/(\d+)/([a-f0-9]+)/?"
    )

    for row in soup.select("table.itg tr"):
        match = None
        for link in row.find_all("a", href=True):
            href = str(link.get("href", "") or "")
            candidate = gallery_href_pattern.search(href)
            if candidate:
                match = candidate
                break
        if match is None:
            continue

        gid = match.group(1)
        if gid in seen_ids:
            continue
        seen_ids.add(gid)

        posted_div = row.find(id=f"posted_{gid}")
        favorite_category = posted_div.get("title", "").strip() if posted_div else ""
        custom_folder_slot = category_map.get(favorite_category)
        favorited_epoch = _parse_favorited_epoch(row)
        favorite_note = _extract_favorite_note_from_row(row, gid)

        parsed_items.append(
            {
                "gid": int(gid),
                "token": match.group(2),
                "favorite_category": favorite_category,
                "custom_folder_slot": custom_folder_slot,
                "favorited_epoch": favorited_epoch,
                "favorite_note": favorite_note,
            }
        )

    return parsed_items


def _extract_favorite_note_from_row(row, gid: str) -> str:
    note_selectors = ["div.glfnote", "td.glfnote", "td.glfn", ".glfnote", ".glfn"]
    for selector in note_selectors:
        node = row.select_one(selector)
        if node:
            text = _clean_favorite_note_text(node.get_text(" ", strip=True))
            if text:
                return text
    # Fallback: when row segmentation is odd, look up the well-known note container id.
    note_node = row.find(id=f"favnote_{gid}")
    if note_node:
        text = _clean_favorite_note_text(note_node.get_text(" ", strip=True))
        if text:
            return text
    return ""


def _clean_favorite_note_text(text: str) -> str:
    cleaned = str(text or "").strip()
    if cleaned.lower().startswith("note:"):
        cleaned = cleaned[len("note:") :].strip()
    return cleaned


def _parse_favorited_epoch(row) -> int:
    for block in row.select("div.gl3e > div"):
        paragraphs = [node.get_text(" ", strip=True) for node in block.find_all("p")]
        if len(paragraphs) < 2:
            continue
        if paragraphs[0].strip().casefold() != "favorited:":
            continue
        return _parse_utc_timestamp(paragraphs[1])
    return 0


def _parse_utc_timestamp(raw_value: str) -> int:
    value = str(raw_value or "").strip()
    if not value:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return 0


def get_favorites_sort_order(soup: BeautifulSoup) -> str:
    for select in soup.select("div.searchnav select"):
        onchange = str(select.get("onchange", "") or "")
        if "inline_set=fs_" not in onchange:
            continue
        selected_option = select.find("option", selected=True) or select.find("option")
        if selected_option is None:
            return ""
        value = str(selected_option.get("value", "") or "").strip().lower()
        if value in {FAVORITES_SORT_FAVORITED, FAVORITES_SORT_PUBLISHED}:
            return value
    return ""


def fetch_favorites_page(
    session: requests.Session,
    url: str,
    ensure_display: bool = True,
) -> tuple[requests.Response, BeautifulSoup]:
    target_url = _ensure_display_mode(url) if ensure_display else url
    response = session.get(target_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response, BeautifulSoup(response.text, "html.parser")


def set_favorites_sort_order(
    session: requests.Session,
    sort_order: str,
) -> tuple[requests.Response, BeautifulSoup]:
    return fetch_favorites_page(session, f"{FAVORITES_URL}?inline_set=fs_{sort_order}")


def set_favorites_display_mode(
    session: requests.Session,
    display_mode: str,
) -> tuple[requests.Response, BeautifulSoup]:
    if not display_mode:
        display_mode = FAVORITES_DISPLAY_INLINE_EXTENDED
    return fetch_favorites_page(
        session, f"{FAVORITES_URL}?inline_set=dm_{display_mode}", ensure_display=False
    )


def load_config():
    if not CONFIG_PATH.exists():
        print("ERROR: config.json not found! Please create it.")
        return None
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def parse_cookies(cookie_string):
    return {
        k.strip(): v.strip()
        for k, v in (item.split("=", 1) for item in cookie_string.split(";") if "=" in item)
    }


def build_exh_session(raw_cookies: str) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.cookies.update(parse_cookies(raw_cookies))
    return session


def _parse_gallery_url(item_url: str) -> tuple[str, str] | None:
    value = str(item_url or "").strip()
    match = re.search(r"https://exhentai\.org/g/(\d+)/([a-f0-9]+)/?", value, re.IGNORECASE)
    if not match:
        return None
    return match.group(1), match.group(2)


def _download_thumbnail_if_needed(
    session: requests.Session,
    item_id: str,
    thumb_url: str,
) -> str:
    thumb_local_path = THUMBNAILS_DIR / f"{item_id}.jpg"
    if not thumb_url or thumb_local_path.exists():
        return str(thumb_local_path.relative_to(APP_DIR)) if thumb_local_path.exists() else ""
    print(f"Downloading thumbnail for {item_id}...")
    try:
        thumb_response = session.get(thumb_url, timeout=REQUEST_TIMEOUT)
        thumb_response.raise_for_status()
        thumb_local_path.write_bytes(thumb_response.content)
        time.sleep(1)
    except requests.RequestException as exc:
        print(f"Thumbnail download failed for {item_id}: {exc}")
    return str(thumb_local_path.relative_to(APP_DIR)) if thumb_local_path.exists() else ""


def _apply_gallery_metadata(
    session: requests.Session,
    gallery: dict[str, object],
    gallery_context: dict[str, object],
    existing_status: str,
) -> tuple[bool, int]:
    gid = str(gallery["gid"])
    item_id = f"g-{gid}"
    category = gallery.get("category", "Unknown")
    thumb_path = _download_thumbnail_if_needed(
        session,
        item_id,
        str(gallery.get("thumb", "") or ""),
    )

    item_changed = database.upsert_item(
        {
            "id": item_id,
            "title": gallery.get("title", "Unknown"),
            "title_jpn": gallery.get("title_jpn", ""),
            "uploader": gallery.get("uploader", "Unknown"),
            "site_category": category,
            "favorite_category": gallery_context.get("favorite_category", ""),
            "favorite_note": str(
                gallery_context.get("favorite_note", "")
                or gallery.get("notes", "")
                or gallery.get("note", "")
            ),
            "favorited_epoch": int(gallery_context.get("favorited_epoch", 0) or 0),
            "item_url": f"https://exhentai.org/g/{gid}/{gallery.get('token') or gallery_context.get('token', '')}/",
            "custom_folder_slot": gallery_context.get("custom_folder_slot"),
            "gallery_filesize": gallery.get("filesize", 0),
            "filecount": gallery.get("filecount", 0),
            "rating": str(gallery.get("rating", "0.0")),
            "expunged": bool(gallery.get("expunged", False)),
            "tags": json.dumps(gallery.get("tags", []), ensure_ascii=False),
            "parent_gid": str(gallery.get("parent_gid", "")),
            "first_gid": str(gallery.get("first_gid", "")),
            "last_updated_epoch": int(gallery.get("posted", 0)),
            "local_status": existing_status,
            "thumbnail_local_path": thumb_path,
        },
        DB_PATH,
    )

    torrents = gallery.get("torrents", [])
    sorted_torrents = sorted(torrents, key=lambda x: int(x.get("fsize", 0)), reverse=True)
    best_hash = sorted_torrents[0].get("hash") if sorted_torrents else None
    torrent_changes = 0
    for torrent in torrents:
        torrent_hash = torrent.get("hash")
        if not torrent_hash:
            continue
        torrent_changed = database.upsert_torrent(
            {
                "hash_string": torrent_hash,
                "parent_item_id": item_id,
                "name": torrent.get("name", ""),
                "size": int(torrent.get("fsize", 0)),
                "added_epoch": int(torrent.get("added", 0)),
                "is_best_candidate": torrent_hash == best_hash,
            },
            DB_PATH,
        )
        torrent_changes += 1 if torrent_changed else 0
    return item_changed, torrent_changes


def _refresh_torrents_from_gallery_page(
    session: requests.Session,
    item: dict[str, object],
    gid: str,
    token: str,
) -> dict[str, object]:
    try:
        response = session.get(
            f"https://exhentai.org/gallerytorrents.php?gid={gid}&t={token}",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        return {"success": False, "reason": "torrent_page_failed", "message": str(exc)}

    matches = re.findall(
        r"https://exhentai\.org/torrent/\d+/[^\"'>]+/([a-f0-9]{40})\.torrent",
        response.text,
        flags=re.IGNORECASE,
    )
    if not matches:
        return {"success": True, "item_changed": False, "torrents_changed": 0, "torrent_count": 0}

    try:
        from torf import Torrent
    except ModuleNotFoundError as exc:
        return {"success": False, "reason": "missing_torf", "message": str(exc)}

    seen_hashes: set[str] = set()
    torrent_changes = 0
    for info_hash in matches:
        if info_hash in seen_hashes:
            continue
        seen_hashes.add(info_hash)
        torrent_url = re.search(
            rf"https://exhentai\.org/torrent/\d+/[^\"'>]+/{info_hash}\.torrent",
            response.text,
            flags=re.IGNORECASE,
        )
        if torrent_url is None:
            continue
        try:
            torrent_response = session.get(torrent_url.group(0), timeout=REQUEST_TIMEOUT)
            torrent_response.raise_for_status()
        except requests.RequestException as exc:
            print(f"Failed to download personalized torrent for {item['id']}: {exc}")
            continue

        with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as handle:
            tmp_path = Path(handle.name)
            handle.write(torrent_response.content)
        try:
            torrent_file = Torrent.read(tmp_path)
            torrent_changed = database.upsert_torrent(
                {
                    "hash_string": str(torrent_file.infohash),
                    "parent_item_id": str(item["id"]),
                    "name": str(getattr(torrent_file, "name", "") or torrent_file.metainfo["info"].get("name", "")),
                    "size": int(getattr(torrent_file, "size", 0) or 0),
                    "added_epoch": int(time.time()),
                    "is_best_candidate": torrent_changes == 0,
                },
                DB_PATH,
            )
            torrent_changes += 1 if torrent_changed else 0
        finally:
            tmp_path.unlink(missing_ok=True)

    return {
        "success": True,
        "item_changed": False,
        "torrents_changed": torrent_changes,
        "torrent_count": len(seen_hashes),
    }


def get_existing_item_statuses() -> dict[str, str]:
    try:
        with database.get_connection(DB_PATH) as connection:
            rows = connection.execute("SELECT id, local_status FROM items").fetchall()
        return {str(row["id"]): str(row["local_status"]) for row in rows}
    except Exception as exc:
        print(f"Database read error: {exc}")
        return {}


def build_runtime_signature(config: dict[str, object] | None) -> str:
    if not isinstance(config, dict):
        return ""
    return json.dumps(config, sort_keys=True, default=str)


def _maintenance_is_due(
    last_run_epoch: float | None,
    interval_seconds: int,
    now_epoch: float,
) -> bool:
    if last_run_epoch is None:
        return True
    return (now_epoch - last_run_epoch) >= max(1, interval_seconds)


def run_operational_cycle(
    config: dict[str, object],
    adapter,
    *,
    run_torrent_maintenance: bool,
    run_archive_maintenance: bool,
    run_health_checks: bool,
    run_media_normalization: bool,
    run_lanraragi_sync: bool,
) -> None:
    def _safe(label: str, fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
            database.resolve_error(
                item_id="worker",
                error_type=f"{label}_failed",
                db_path=DB_PATH,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"{label} failed: {exc}")
            try:
                database.record_error(
                    item_id="worker",
                    error_type=f"{label}_failed",
                    message=str(exc),
                    fix_hint="Check config paths and client connectivity.",
                    db_path=DB_PATH,
                )
            except Exception:
                pass

    print("Checking database for queued items to download...")
    _safe(
        "process_queue",
        download_manager.process_queue,
        db_path=DB_PATH,
        config=config,
        dry_run=False,
        download_adapter=adapter,
    )

    print("Syncing active downloads with qBittorrent...")
    _safe(
        "sync_active_downloads",
        download_manager.sync_active_downloads,
        db_path=DB_PATH,
        download_adapter=adapter,
        config=config,
    )

    if run_media_normalization:
        print("Normalizing Media Library gid filename format...")
        _safe(
            "normalize_media_gid_bracket_format",
            media_importer.normalize_media_gid_bracket_format,
            db_path=DB_PATH,
            config=config,
        )

    if run_torrent_maintenance:
        print("Reconciling torrent imports in Media Library...")
        _safe(
            "reconcile_torrent_imports",
            media_importer.reconcile_torrent_imports,
            db_path=DB_PATH,
            config=config,
            download_adapter=adapter,
        )

        print("Recovering misclassified torrent imports...")
        _safe(
            "recover_misclassified_torrent_imports",
            media_importer.recover_misclassified_torrent_imports,
            db_path=DB_PATH,
            config=config,
            download_adapter=adapter,
        )

        print("Importing finished torrents to Media Library...")
        _safe(
            "import_completed_torrents",
            media_importer.import_completed_torrents,
            db_path=DB_PATH,
            config=config,
            download_adapter=adapter,
        )

        print("Refreshing torrent terminal statuses...")
        _safe(
            "sync_torrent_terminal_statuses",
            media_importer.sync_torrent_terminal_statuses,
            db_path=DB_PATH,
            download_adapter=adapter,
        )

    if run_archive_maintenance:
        print("Checking database for External Archive (DDL) requests...")
        print("Clearing misattributed archive flags...")
        _safe(
            "clear_misattributed_archive_flags",
            external_manager.clear_misattributed_archive_flags,
            db_path=DB_PATH,
        )
        print("Migrating legacy archive storage...")
        _safe(
            "migrate_legacy_archive_storage",
            media_importer.migrate_legacy_archive_storage,
            db_path=DB_PATH,
            config=config,
        )
        print("Reconciling missing External Archive imports...")
        _safe(
            "reconcile_missing_external_imports",
            external_manager.reconcile_missing_external_imports,
            db_path=DB_PATH,
            config=config,
        )
        print("Reconciling H@H downloads...")
        _safe(
            "reconcile_hath_downloads",
            external_manager.reconcile_hath_downloads,
            db_path=DB_PATH,
            config=config,
        )
        print("Reconciling existing External Archive downloads...")
        _safe(
            "reconcile_external_imports",
            external_manager.reconcile_external_imports,
            db_path=DB_PATH,
            config=config,
        )
        print("Linking downloaded archives into Media Library...")
        _safe(
            "import_completed_archives",
            media_importer.import_completed_archives,
            db_path=DB_PATH,
            config=config,
        )

    if run_health_checks:
        print("Running health checks for missing resources...")
        _safe(
            "health_checks",
            health_checker.run_health_checks,
            db_path=DB_PATH,
            config=config,
            adapter=adapter,
        )

    if run_lanraragi_sync:
        print("Syncing LANraragi metadata...")
        _safe(
            "sync_lanraragi_metadata",
            lanraragi_sync.run_scheduled_sync,
            db_path=DB_PATH,
            config=config,
        )


def process_archive_to_torrent_queue(
    db_path: str | Path,
    config: dict[str, object],
    adapter,
) -> None:
    with database.get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                items.id,
                items.title,
                items.item_url,
                items.archive_path,
                items.archive_downloaded,
                COALESCE(items.torrent_from_archive, 0) AS torrent_from_archive,
                COUNT(t.hash_string) AS torrent_count
            FROM items
            LEFT JOIN torrents t ON t.parent_item_id = items.id
            WHERE items.archive_downloaded = 1
              AND COALESCE(items.torrent_from_archive, 0) = 0
              AND COALESCE(items.archive_path, '') != ''
              AND COALESCE(items.is_latest_revision, 1) = 1
            GROUP BY items.id
            HAVING torrent_count = 0
            ORDER BY items.last_updated_epoch ASC
            """
        ).fetchall()
    if not rows:
        return

    for row in rows:
        item = dict(row)
        # Skip items without a valid gid/token in the URL; they need manual fixing.
        if not torrent_uploader.has_gid_and_token(item):
            print(f"Skipping archive->torrent for {item['id']}: missing gid/token in item_url.")
            continue
        try:
            database.update_item_status(
                item_id=item["id"],
                local_status="creating_torrent",
                db_path=db_path,
            )
            torrent_uploader.process_item(item=item, config=config, adapter=adapter, db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            print(f"archive_to_torrent failed for {item['id']}: {exc}")
            database.record_error(
                item_id=item["id"],
                error_type="torrent_upload_failed",
                message=str(exc),
                fix_hint="Check announce URL/cookies and qbittorrent connectivity.",
                db_path=db_path,
            )
            database.update_item_status(
                item_id=item["id"],
                local_status="error",
                error_message=str(exc),
                db_path=db_path,
            )

def fetch_and_store_metadata(*, full_rescan: bool = False):
    status_started_epoch = int(time.time())
    if full_rescan:
        _set_favorites_rescan_status(
            state="running",
            phase="starting",
            message="Preparing full favorites rescan.",
            started_epoch=status_started_epoch,
        )
    if full_rescan:
        print("Waking up worker: Running full favorites rescan...")
    else:
        print("Waking up worker: Checking for live Sadpanda updates...")
    config = load_config()
    if not config:
        if full_rescan:
            _set_favorites_rescan_status(
                state="failed",
                phase="starting",
                message="Missing config.json.",
                started_epoch=status_started_epoch,
            )
        return {"skipped": True, "reason": "missing_config", "items_changed": 0, "torrents_changed": 0}

    raw_cookies = config.get("exhentai", {}).get("cookies", "")
    if not raw_cookies.strip():
        print("Missing exhentai cookies in config.json.")
        if full_rescan:
            _set_favorites_rescan_status(
                state="failed",
                phase="starting",
                message="Missing exhentai cookies in config.",
                started_epoch=status_started_epoch,
            )
        return {"skipped": True, "reason": "missing_cookies", "items_changed": 0, "torrents_changed": 0}

    THUMBNAILS_DIR.mkdir(exist_ok=True)

    session = build_exh_session(raw_cookies)

    existing_status_by_id = get_existing_item_statuses()
    current_url = FAVORITES_URL
    new_galleries_to_fetch: list[dict[str, int | str | None]] = []
    gallery_context_by_gid: dict[int, dict[str, int | str | None]] = {}
    seen_ids: set[str] = set()
    page_num = 1
    pages_scanned = 0
    favorite_metadata_changes = 0
    restore_sort_order = ""
    restore_display_mode = ""

    try:
        fav_response, soup = fetch_favorites_page(session, current_url, ensure_display=False)
        current_display_mode = _extract_display_mode(soup, fav_response.url)
        if current_display_mode != FAVORITES_DISPLAY_INLINE_EXTENDED:
            if current_display_mode:
                restore_display_mode = current_display_mode
                print(
                    f"Favorites display mode is {current_display_mode}. "
                    "Switching scrape to Extended mode temporarily."
                )
            else:
                print(
                    "Could not determine favorites display mode. "
                    "Forcing Extended mode for the scrape."
                )
            fav_response, soup = set_favorites_display_mode(
                session, FAVORITES_DISPLAY_INLINE_EXTENDED
            )
            if full_rescan:
                _set_favorites_rescan_status(
                    state="running",
                    phase="preparing",
                    message="Switched favorites display mode to Extended for scraping.",
                    started_epoch=status_started_epoch,
                )
        current_url = fav_response.url
        current_sort_order = get_favorites_sort_order(soup)
        if current_sort_order == FAVORITES_SORT_PUBLISHED:
            print("Favorites page is sorted by Published Time. Temporarily switching scrape to Favorited Time.")
            fav_response, soup = set_favorites_sort_order(session, FAVORITES_SORT_FAVORITED)
            restore_sort_order = FAVORITES_SORT_PUBLISHED
            current_url = fav_response.url
        elif current_sort_order != FAVORITES_SORT_FAVORITED:
            print("Could not determine favorites sort order. Forcing Favorited Time for the scrape.")
            fav_response, soup = set_favorites_sort_order(session, FAVORITES_SORT_FAVORITED)
            current_url = fav_response.url

        while current_url:
            print(f"Checking favorites page {page_num}...")
            if page_num > 1:
                try:
                    fav_response, soup = fetch_favorites_page(session, current_url)
                except requests.RequestException as exc:
                    print(f"Failed to load page {page_num}: {exc}")
                    return {
                        "skipped": True,
                        "reason": "favorites_request_failed",
                        "items_changed": 0,
                        "torrents_changed": 0,
                    }

            page_items = parse_favorites_page(soup)
            pages_scanned += 1
            if full_rescan:
                _set_favorites_rescan_status(
                    state="running",
                    phase="scanning_pages",
                    message=f"Scanned page {page_num}.",
                    pages_scanned=pages_scanned,
                    new_items_found=len(new_galleries_to_fetch),
                    favorite_metadata_updates=favorite_metadata_changes,
                    started_epoch=status_started_epoch,
                )

            if not page_items:
                print("Favorites page returned no gallery links. Cookies may be expired or page format changed.")
                if full_rescan:
                    _set_favorites_rescan_status(
                        state="failed",
                        phase="scanning_pages",
                        message="No gallery links found; cookies or page format may be invalid.",
                        pages_scanned=pages_scanned,
                        new_items_found=len(new_galleries_to_fetch),
                        favorite_metadata_updates=favorite_metadata_changes,
                        started_epoch=status_started_epoch,
                    )
                return {"skipped": True, "reason": "no_gallery_links", "items_changed": 0, "torrents_changed": 0}

            page_new_count = 0
            for page_item in page_items:
                gid = str(page_item["gid"])
                item_id = f"g-{gid}"
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                if item_id in existing_status_by_id:
                    metadata_changed = database.update_item_favorite_metadata(
                        item_id=item_id,
                        favorite_category=str(page_item.get("favorite_category", "") or ""),
                        custom_folder_slot=page_item.get("custom_folder_slot"),
                        item_url=f"https://exhentai.org/g/{gid}/{page_item.get('token', '')}/",
                        favorited_epoch=int(page_item.get("favorited_epoch", 0) or 0),
                        favorite_note=str(page_item.get("favorite_note", "") or ""),
                        db_path=DB_PATH,
                    )
                    favorite_metadata_changes += 1 if metadata_changed else 0
                else:
                    new_galleries_to_fetch.append(page_item)
                    gallery_context_by_gid[int(gid)] = page_item
                    page_new_count += 1

            if page_new_count == 0 and not full_rescan:
                print("Reached a page where every item is already known. Stopping page scrape.")
                break

            next_button = soup.find("a", id="pnext")
            if not next_button:
                next_button = soup.find("a", href=re.compile(r"\?next="))

            if next_button and next_button.get("href"):
                next_url = urljoin(current_url, next_button["href"])
                if next_url == current_url:
                    break
                current_url = _ensure_display_mode(next_url)
                page_num += 1
                time.sleep(3)
            else:
                break
    except requests.RequestException as exc:
        print(f"Failed to load favorites page 1: {exc}")
        if full_rescan:
            _set_favorites_rescan_status(
                state="failed",
                phase="scanning_pages",
                message=f"Request failed: {exc}",
                pages_scanned=pages_scanned,
                started_epoch=status_started_epoch,
            )
        return {"skipped": True, "reason": "favorites_request_failed", "items_changed": 0, "torrents_changed": 0}
    finally:
        if restore_display_mode:
            try:
                set_favorites_display_mode(session, restore_display_mode)
                print(
                    f"Restored favorites display mode to '{restore_display_mode}'."
                )
            except requests.RequestException as exc:
                print(f"Failed to restore favorites display mode: {exc}")
        if restore_sort_order:
            try:
                set_favorites_sort_order(session, restore_sort_order)
                print("Restored favorites page sort order to Published Time.")
            except requests.RequestException as exc:
                print(f"Failed to restore favorites sort order: {exc}")

    if not new_galleries_to_fetch:
        if favorite_metadata_changes:
            print(
                f"No brand-new galleries found after scanning {pages_scanned} page(s), "
                f"but updated favorite metadata for {favorite_metadata_changes} known item(s)."
            )
            if full_rescan:
                _set_favorites_rescan_status(
                    state="completed",
                    phase="done",
                    message="Completed full rescan; updated favorite metadata for existing items.",
                    pages_scanned=pages_scanned,
                    new_items_found=0,
                    favorite_metadata_updates=favorite_metadata_changes,
                    items_changed=favorite_metadata_changes,
                    started_epoch=status_started_epoch,
                )
            return {
                "skipped": False,
                "reason": "updated_existing_favorite_metadata",
                "items_changed": favorite_metadata_changes,
                "torrents_changed": 0,
            }
        print(f"No new galleries found after scanning {pages_scanned} page(s).")
        if full_rescan:
            _set_favorites_rescan_status(
                state="completed",
                phase="done",
                message="Completed full rescan; no new galleries found.",
                pages_scanned=pages_scanned,
                started_epoch=status_started_epoch,
            )
        return {"skipped": True, "reason": "no_new_items", "items_changed": 0, "torrents_changed": 0}

    print(f"Found {len(new_galleries_to_fetch)} new galleries. Fetching API metadata...")
    if full_rescan:
        _set_favorites_rescan_status(
            state="running",
            phase="fetching_metadata",
            message=f"Found {len(new_galleries_to_fetch)} new galleries; fetching metadata.",
            pages_scanned=pages_scanned,
            new_items_found=len(new_galleries_to_fetch),
            favorite_metadata_updates=favorite_metadata_changes,
            started_epoch=status_started_epoch,
        )
    chunks = [new_galleries_to_fetch[i : i + 25] for i in range(0, len(new_galleries_to_fetch), 25)]
    total_item_changes = 0
    total_torrent_changes = 0

    for chunk in chunks:
        api_payload = {
            "method": "gdata",
            "gidlist": [[int(entry["gid"]), str(entry["token"])] for entry in chunk],
            "namespace": 1,
        }
        try:
            api_response = session.post(
                "https://api.e-hentai.org/api.php",
                json=api_payload,
                timeout=REQUEST_TIMEOUT,
            )
            api_response.raise_for_status()
            api_resp = api_response.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"API error for batch of {len(chunk)} items. Retrying next loop. {exc}")
            continue

        gmetadata = api_resp.get("gmetadata")
        if not isinstance(gmetadata, list):
            print(f"API response missing gmetadata for batch of {len(chunk)} items.")
            continue

        batch_item_changes = 0
        batch_torrent_changes = 0

        for gallery in gmetadata:
            if "error" in gallery:
                continue

            gid = str(gallery["gid"])
            item_id = f"g-{gid}"
            gallery_context = gallery_context_by_gid.get(int(gid), {})
            category = gallery.get("category", "Unknown")
            existing_status = existing_status_by_id.get(item_id, "new")

            item_changed, torrent_changes = _apply_gallery_metadata(
                session=session,
                gallery=gallery,
                gallery_context=gallery_context,
                existing_status=existing_status,
            )
            batch_item_changes += 1 if item_changed else 0
            batch_torrent_changes += torrent_changes

        total_item_changes += batch_item_changes
        total_torrent_changes += batch_torrent_changes

        if batch_item_changes or batch_torrent_changes:
            print(
                f"Applied SQLite changes from batch of {len(chunk)} items: "
                f"{batch_item_changes} item changes, {batch_torrent_changes} torrent changes."
            )
        else:
            print(f"Checked batch of {len(chunk)} items, no SQLite changes detected.")
        if full_rescan:
            _set_favorites_rescan_status(
                state="running",
                phase="fetching_metadata",
                message=f"Processed metadata batch of {len(chunk)} galleries.",
                pages_scanned=pages_scanned,
                new_items_found=len(new_galleries_to_fetch),
                favorite_metadata_updates=favorite_metadata_changes,
                items_changed=total_item_changes + favorite_metadata_changes,
                torrents_changed=total_torrent_changes,
                started_epoch=status_started_epoch,
            )

        time.sleep(2)

    print(
        "Background sync complete. "
        f"New galleries seen: {len(new_galleries_to_fetch)}. "
        f"Item changes: {total_item_changes + favorite_metadata_changes}. "
        f"Torrent changes: {total_torrent_changes}. "
        f"Favorite metadata updates: {favorite_metadata_changes}."
    )
    if full_rescan:
        _set_favorites_rescan_status(
            state="completed",
            phase="done",
            message="Completed full favorites rescan.",
            pages_scanned=pages_scanned,
            new_items_found=len(new_galleries_to_fetch),
            favorite_metadata_updates=favorite_metadata_changes,
            items_changed=total_item_changes + favorite_metadata_changes,
            torrents_changed=total_torrent_changes,
            started_epoch=status_started_epoch,
        )
    return {
        "skipped": False,
        "reason": "processed",
        "items_changed": total_item_changes + favorite_metadata_changes,
        "torrents_changed": total_torrent_changes,
    }


def refresh_gallery_metadata(item_id: str) -> dict[str, object]:
    config = load_config()
    if not config:
        return {"success": False, "reason": "missing_config"}

    item = database.get_item(item_id, DB_PATH)
    if item is None:
        return {"success": False, "reason": "missing_item"}

    parsed = _parse_gallery_url(str(item.get("item_url", "") or ""))
    if parsed is None:
        return {"success": False, "reason": "missing_item_url"}
    gid, token = parsed

    raw_cookies = str(config.get("exhentai", {}).get("cookies", "") or "")
    if not raw_cookies.strip():
        return {"success": False, "reason": "missing_cookies"}

    THUMBNAILS_DIR.mkdir(exist_ok=True)
    session = build_exh_session(raw_cookies)

    api_payload = {
        "method": "gdata",
        "gidlist": [[int(gid), token]],
        "namespace": 1,
    }
    try:
        api_response = session.post(
            "https://api.e-hentai.org/api.php",
            json=api_payload,
            timeout=REQUEST_TIMEOUT,
        )
        api_response.raise_for_status()
        api_resp = api_response.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"Gallery API refresh failed for {item_id}; falling back to torrent page scrape. {exc}")
        return _refresh_torrents_from_gallery_page(session, item, gid, token)

    gmetadata = api_resp.get("gmetadata")
    if not isinstance(gmetadata, list) or not gmetadata:
        return {"success": False, "reason": "missing_gmetadata"}

    gallery = gmetadata[0]
    if "error" in gallery:
        return {"success": False, "reason": "gallery_error", "message": str(gallery.get("error") or "")}

    item_changed, torrent_changes = _apply_gallery_metadata(
        session=session,
        gallery=gallery,
        gallery_context={
            "favorite_category": item.get("favorite_category", ""),
            "favorited_epoch": item.get("favorited_epoch", 0),
            "custom_folder_slot": item.get("custom_folder_slot"),
            "token": token,
        },
        existing_status=str(item.get("local_status", "new") or "new"),
    )
    return {
        "success": True,
        "item_changed": item_changed,
        "torrents_changed": torrent_changes,
        "torrent_count": len(gallery.get("torrents", [])),
    }


def start_external_download_thread(
    config: dict[str, object],
    ext_client,
) -> threading.Thread:
    def _runner() -> None:
        try:
            external_manager.process_external_queue(
                db_path=DB_PATH,
                config=config,
                external_adapter=ext_client,
            )
        except Exception as exc:
            print(f"External download worker crashed: {exc}")

    thread = threading.Thread(
        target=_runner,
        name="sadpanda-external-downloads",
        daemon=True,
    )
    thread.start()
    return thread


if __name__ == "__main__":
    print("Starting Sadpanda Downloader background worker...")
    database.init_db(DB_PATH)
    normalized_count = database.normalize_legacy_auto_queue_statuses(DB_PATH)
    if normalized_count:
        print(f"Normalized {normalized_count} legacy queued item(s) back to new.")
    normalized_terminal_count = database.normalize_legacy_terminal_statuses(DB_PATH)
    if normalized_terminal_count:
        print(f"Normalized {normalized_terminal_count} legacy imported item(s) to completed.")

    adapter = None
    ext_client = None
    runtime_signature = ""
    last_metadata_refresh = 0.0
    last_full_rescan = 0.0
    waiting_for_config_logged = False
    last_torrent_maintenance = None
    last_archive_maintenance = None
    last_health_check = None
    last_media_normalization = None
    last_lanraragi_sync_poll = None
    last_full_rescan_raw = database.get_setting(FAVORITES_LAST_REFRESH_KEY, "0", DB_PATH)
    try:
        last_full_rescan = float(int(last_full_rescan_raw or "0"))
    except (TypeError, ValueError):
        last_full_rescan = 0.0
    external_download_thread: threading.Thread | None = None

    while True:
        cycle_started = time.time()
        try:
            config = load_config()
            config_ready = database.get_bool_setting(CONFIG_READY_FLAG_KEY, False, DB_PATH)
            if not config_ready and _has_required_runtime_config(config):
                database.set_bool_setting(CONFIG_READY_FLAG_KEY, True, DB_PATH)
                config_ready = True

            if not config_ready:
                adapter = None
                ext_client = None
                runtime_signature = ""
                if not waiting_for_config_logged:
                    print(
                        "Worker is waiting for initial required config fields. "
                        "Save config once in Streamlit sidebar to start syncing."
                    )
                    waiting_for_config_logged = True
            else:
                waiting_for_config_logged = False

            if config and config_ready:
                current_signature = build_runtime_signature(config)
                adapter_name = config.get("downloader", {}).get("active_adapter")
                if adapter_name and current_signature != runtime_signature:
                    try:
                        adapter_module = importlib.import_module(adapter_name)
                        adapter = adapter_module.get_adapter(config)
                        ext_client = external_adapter.get_adapter(config)
                        runtime_signature = current_signature
                    except ModuleNotFoundError as exc:
                        missing = exc.name or adapter_name
                        print(
                            f"ERROR: Missing module '{missing}'. "
                            f"Ensure '{adapter_name}.py' exists and required dependencies are installed."
                        )
                        adapter = None
                        ext_client = None
                        runtime_signature = ""
                    except Exception as exc:
                        print(f"ERROR initializing adapter: {exc}")
                        adapter = None
                        ext_client = None
                        runtime_signature = ""
                elif not adapter_name:
                    adapter = None
                    ext_client = None
                    runtime_signature = current_signature

            now = time.time()
            if config_ready:
                rescan_requested = database.get_bool_setting(FAVORITES_RESCAN_FLAG_KEY, False, DB_PATH)
                full_rescan_interval_seconds = _full_rescan_interval_seconds()
                full_rescan_due = now - last_full_rescan >= full_rescan_interval_seconds
                if rescan_requested or full_rescan_due:
                    fetch_and_store_metadata(full_rescan=True)
                    last_metadata_refresh = time.time()
                    last_full_rescan = last_metadata_refresh
                    if rescan_requested:
                        database.set_bool_setting(FAVORITES_RESCAN_FLAG_KEY, False, DB_PATH)
                    database.set_setting(FAVORITES_LAST_REFRESH_KEY, str(int(last_full_rescan)), DB_PATH)
                elif now - last_metadata_refresh >= METADATA_REFRESH_SECONDS:
                    fetch_and_store_metadata(full_rescan=False)
                    last_metadata_refresh = time.time()

            if config_ready and adapter and config and ext_client:
                run_torrent_maintenance = _maintenance_is_due(
                    last_torrent_maintenance,
                    TORRENT_MAINTENANCE_SECONDS,
                    now,
                )
                run_archive_maintenance = _maintenance_is_due(
                    last_archive_maintenance,
                    ARCHIVE_MAINTENANCE_SECONDS,
                    now,
                )
                run_health_checks = _maintenance_is_due(
                    last_health_check,
                    HEALTH_CHECK_SECONDS,
                    now,
                )
                run_media_normalization = _maintenance_is_due(
                    last_media_normalization,
                    MEDIA_NORMALIZATION_SECONDS,
                    now,
                )
                run_lanraragi_sync = _maintenance_is_due(
                    last_lanraragi_sync_poll,
                    LANRARAGI_SYNC_POLL_SECONDS,
                    now,
                )

                due_labels: list[str] = []
                if run_torrent_maintenance:
                    due_labels.append("torrent")
                if run_archive_maintenance:
                    due_labels.append("archive")
                if run_health_checks:
                    due_labels.append("health")
                if run_media_normalization:
                    due_labels.append("media-normalize")
                if run_lanraragi_sync:
                    due_labels.append("lanraragi")
                if due_labels:
                    print(f"Maintenance buckets due this cycle: {', '.join(due_labels)}.")

                run_operational_cycle(
                    config=config,
                    adapter=adapter,
                    run_torrent_maintenance=run_torrent_maintenance,
                    run_archive_maintenance=run_archive_maintenance,
                    run_health_checks=run_health_checks,
                    run_media_normalization=run_media_normalization,
                    run_lanraragi_sync=run_lanraragi_sync,
                )
                if run_torrent_maintenance:
                    last_torrent_maintenance = now
                if run_archive_maintenance:
                    last_archive_maintenance = now
                if run_health_checks:
                    last_health_check = now
                if run_media_normalization:
                    last_media_normalization = now
                if run_lanraragi_sync:
                    last_lanraragi_sync_poll = now
                if external_download_thread is None or not external_download_thread.is_alive():
                    if external_manager.get_external_queue(DB_PATH, config):
                        external_download_thread = start_external_download_thread(
                            config=config,
                            ext_client=ext_client,
                        )
        except Exception as exc:
            print(f"An unexpected error crashed the loop: {exc}")
            _set_favorites_rescan_status(
                state="failed",
                phase="worker_loop",
                message=f"Worker loop exception: {exc}",
            )
            try:
                database.record_error(
                    item_id="worker",
                    error_type="runtime_exception",
                    message=str(exc),
                    fix_hint="Check logs and configuration.",
                    db_path=DB_PATH,
                )
            except Exception:
                pass
        else:
            try:
                database.resolve_error(
                    item_id="worker",
                    error_type="runtime_exception",
                    db_path=DB_PATH,
                )
            except Exception:
                pass

        elapsed = time.time() - cycle_started
        sleep_seconds = max(1.0, OPERATIONAL_REFRESH_SECONDS - elapsed)
        print(
            f"Sleep cycle started. Waiting {sleep_seconds:.1f} seconds before the next live sync "
            f"(live check every {METADATA_REFRESH_SECONDS}s; full rescan every "
            f"{_full_rescan_interval_seconds() // 3600}h)..."
        )
        time.sleep(sleep_seconds)
