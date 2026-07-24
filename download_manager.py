from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Protocol

from database import (
    get_active_rule_sets,
    get_category_rule_map,
    get_connection,
    item_matches_rule_set,
    update_item_download_flags,
    update_item_progress,
    update_item_status,
)
import torrent_uploader


DEFAULT_TAGS = ["Sadpanda_Auto"]
DEFAULT_METADATA_RECOVERY_MINUTES = 60
DEFAULT_METADATA_RECOVERY_COOLDOWN_MINUTES = 30
MAX_SKIPPED_CANDIDATE_LOG_SAMPLES = 8
_METADATA_STUCK_SINCE: dict[str, float] = {}
_METADATA_RECOVERY_LAST_ATTEMPT: dict[str, float] = {}


class DownloadAdapter(Protocol):
    def enqueue(self, torrent_hash: str, save_path: str, tags: list[str]) -> Any:
        ...

    def get_status(self, torrent_hash: str) -> dict[str, Any]:
        ...

    def replace_with_torrent_file(
        self,
        torrent_hash: str,
        torrent_file_path: str,
        save_path: str,
        tags: list[str],
    ) -> Any:
        ...


def get_queued_items(db_path: str | Path) -> list[dict[str, Any]]:
    active_rule_sets = get_active_rule_sets(db_path)
    legacy_category_rule_map = get_category_rule_map(db_path) if not active_rule_sets else {}

    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                title_jpn,
                uploader,
                site_category,
                favorite_category,
                custom_folder_slot,
                gallery_filesize,
                filecount,
                rating,
                expunged,
                tags,
                last_updated_epoch,
                local_status,
                thumbnail_local_path
            FROM items
            WHERE local_status IN ('force_queued', 'queued', 'new')
              AND torrent_downloaded = 0
              AND COALESCE(is_latest_revision, 1) = 1
            ORDER BY
                CASE
                    WHEN local_status = 'force_queued' THEN 0
                    ELSE 1
                END ASC,
                last_updated_epoch ASC,
                title COLLATE NOCASE ASC
            """
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item["local_status"] == "force_queued":
            items.append(item)
            continue
        if _matches_auto_download_rule(item, active_rule_sets, legacy_category_rule_map):
            items.append(item)
    return items


def select_best_candidate(db_path: str | Path, item_id: str) -> dict[str, Any] | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
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
            LIMIT 1
            """,
            (item_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def process_queue(
    db_path: str | Path,
    config: dict[str, Any],
    dry_run: bool = True,
    download_adapter: DownloadAdapter | None = None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    queued_items = get_queued_items(db_path)
    save_path = _resolve_save_path(config)
    tags = _resolve_tags(config)
    skipped_without_candidate: list[tuple[str, str]] = []

    for item in queued_items:
        candidate = select_best_candidate(db_path, item["id"])
        if candidate is None:
            skipped_without_candidate.append((str(item["id"]), _safe_text(item["title"])))
            continue

        action = {
            "item_id": item["id"],
            "title": item["title"],
            "torrent_hash": candidate["hash_string"],
            "save_path": save_path,
            "tags": tags,
            "dry_run": dry_run,
        }
        actions.append(action)

        if dry_run:
            print(
                f"DRY RUN: enqueue {candidate['hash_string']} for "
                f"{_safe_text(item['title'])} -> {save_path} tags={tags}"
            )
            continue

        if download_adapter is None:
            raise ValueError("download_adapter is required when dry_run is False")

        enqueued = bool(download_adapter.enqueue(candidate["hash_string"], save_path, tags))
        if not enqueued:
            print(
                f"qB add failed for {candidate['hash_string']} "
                f"({_safe_text(item['title'])}); leaving status as {item['local_status']}."
            )
            continue
        update_item_status(item["id"], "downloading", db_path=db_path)
        print(
            f"Enqueued {candidate['hash_string']} for {_safe_text(item['title'])} -> {save_path}. "
            f"Status transition: {item['id']} {item['local_status']} -> downloading"
        )

    if skipped_without_candidate:
        sample = "; ".join(
            f"{item_id} ({title})"
            for item_id, title in skipped_without_candidate[:MAX_SKIPPED_CANDIDATE_LOG_SAMPLES]
        )
        extra = len(skipped_without_candidate) - MAX_SKIPPED_CANDIDATE_LOG_SAMPLES
        suffix = f"; +{extra} more" if extra > 0 else ""
        print(
            "Skipped queued items with no candidate torrent: "
            f"{len(skipped_without_candidate)} total. Sample: {sample}{suffix}."
        )

    return actions


def sync_active_downloads(
    db_path: str | Path,
    download_adapter: DownloadAdapter,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    downloading_items = _get_items_for_status_sync(db_path)
    metadata_recovery_minutes, metadata_recovery_cooldown_minutes = _resolve_metadata_recovery_windows(config)
    metadata_recovery_enabled = metadata_recovery_minutes > 0
    now = time.time()

    for item in downloading_items:
        candidate = select_best_candidate(db_path, item["id"])
        if candidate is None:
            continue

        status = download_adapter.get_status(candidate["hash_string"]) or {}
        found = bool(status.get("found"))
        errored = bool(status.get("error"))
        progress_pct = _normalize_progress_pct(status.get("progress", 0.0))

        if found:
            update_item_progress(item["id"], progress_pct, db_path)

        item_update = {
            "item_id": item["id"],
            "title": item["title"],
            "torrent_hash": candidate["hash_string"],
            "found": found,
            "error": errored,
            "progress_pct": progress_pct,
            "is_completed": bool(status.get("is_completed")),
        }
        metadata_state = str(status.get("state", "") or "").strip().lower()
        metadata_stuck = found and not bool(status.get("is_completed")) and "meta" in metadata_state

        if not found:
            _clear_metadata_recovery_tracking(item["id"])
            if not errored and item["local_status"] == "downloading":
                update_item_progress(item["id"], 0.0, db_path)
                update_item_status(item["id"], "new", db_path=db_path)
                print(
                    f"Reset stale torrent state for {candidate['hash_string']} "
                    f"({_safe_text(item['title'])}); qB no longer reports the torrent. "
                    f"Status transition: {item['id']} downloading -> new"
                )
                item_update["local_status"] = "new"
            updates.append(item_update)
            continue

        if item["local_status"] in {"queued", "force_queued"}:
            update_item_status(item["id"], "downloading", db_path=db_path)
            print(
                f"Recovered existing torrent state for {candidate['hash_string']} "
                f"({_safe_text(item['title'])}). "
                f"Status transition: {item['id']} {item['local_status']} -> downloading"
            )
            item_update["local_status"] = "downloading"

        if metadata_stuck:
            started = _METADATA_STUCK_SINCE.setdefault(item["id"], now)
            elapsed_minutes = (now - started) / 60.0
            added_epoch = int(status.get("added_epoch", 0) or 0)
            if added_epoch > 0:
                elapsed_from_added = max(0.0, (now - float(added_epoch)) / 60.0)
                # Respect the torrent's actual age in qB so recovery works across worker restarts.
                elapsed_minutes = max(elapsed_minutes, elapsed_from_added)
            if metadata_recovery_enabled and elapsed_minutes >= metadata_recovery_minutes:
                last_attempt = _METADATA_RECOVERY_LAST_ATTEMPT.get(item["id"], 0.0)
                if (now - last_attempt) / 60.0 >= metadata_recovery_cooldown_minutes:
                    _METADATA_RECOVERY_LAST_ATTEMPT[item["id"]] = now
                    print(
                        f"Metadata stuck recovery trigger for {item['id']} "
                        f"({_safe_text(item['title'])}) state={metadata_state} "
                        f"elapsed={elapsed_minutes:.1f}m threshold={metadata_recovery_minutes}m"
                    )
                    recovered = _attempt_metadata_file_recovery(
                        item=item,
                        candidate=candidate,
                        config=config or {},
                        download_adapter=download_adapter,
                    )
                    if recovered:
                        _METADATA_STUCK_SINCE[item["id"]] = time.time()
        else:
            _clear_metadata_recovery_tracking(item["id"])

        if bool(status.get("is_completed")):
            _clear_metadata_recovery_tracking(item["id"])
            update_item_progress(item["id"], 100.0, db_path)
            update_item_download_flags(item["id"], torrent_downloaded=True, db_path=db_path)
            update_item_status(item["id"], "complete", db_path=db_path)
            print(
                f"Completed {candidate['hash_string']} for {_safe_text(item['title'])}. "
                f"Status transition: {item['id']} downloading -> complete"
            )
            item_update["progress_pct"] = 100.0

        updates.append(item_update)

    return updates


def restore_missing_torrents(
    db_path: str | Path,
    config: dict[str, Any],
    download_adapter: DownloadAdapter,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    save_path = _resolve_save_path(config)
    tags = _resolve_tags(config)

    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, local_status
            FROM items
            WHERE COALESCE(is_latest_revision, 1) = 1
              AND (
                  torrent_downloaded = 1
                  OR local_status IN ('complete', 'completed', 'seeding')
              )
            ORDER BY last_updated_epoch ASC, title COLLATE NOCASE ASC
            """
        ).fetchall()

    for row in rows:
        item_id = str(row["id"])
        candidate = select_best_candidate(db_path, item_id)
        if candidate is None:
            continue

        status = download_adapter.get_status(candidate["hash_string"]) or {}
        if bool(status.get("found")):
            continue

        try:
            download_adapter.enqueue(candidate["hash_string"], save_path, tags)
            actions.append(
                {
                    "item_id": item_id,
                    "torrent_hash": candidate["hash_string"],
                    "save_path": save_path,
                    "tags": tags,
                }
            )
            print(
                f"Restored missing torrent {candidate['hash_string']} for {_safe_text(item_id)} -> {save_path} tags={tags}"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to restore torrent {candidate['hash_string']} for {item_id}: {exc}")

    return actions


def _resolve_save_path(config: dict[str, Any]) -> str:
    qbittorrent_config = config.get("qbittorrent", {})
    if isinstance(qbittorrent_config, dict):
        qbittorrent_path = qbittorrent_config.get("download_path")
        if isinstance(qbittorrent_path, str) and qbittorrent_path.strip():
            return qbittorrent_path.strip()

    download_config = config.get("downloads", {})
    if not isinstance(download_config, dict):
        download_config = {}
    save_path = download_config.get("save_path") or config.get("download_path") or "."
    return str(save_path)


def _resolve_tags(config: dict[str, Any]) -> list[str]:
    download_config = config.get("downloads", {})
    if not isinstance(download_config, dict):
        return DEFAULT_TAGS.copy()

    configured_tags = download_config.get("tags")
    if isinstance(configured_tags, str) and configured_tags.strip():
        return [configured_tags.strip()]
    if isinstance(configured_tags, list):
        tags = [str(tag).strip() for tag in configured_tags if str(tag).strip()]
        if tags:
            return tags
    return DEFAULT_TAGS.copy()


def _safe_text(value: Any) -> str:
    text = str(value)
    try:
        text.encode("cp1252")
        return text
    except UnicodeEncodeError:
        return text.encode("cp1252", errors="replace").decode("cp1252")


def _get_downloading_items(db_path: str | Path) -> list[dict[str, Any]]:
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
                thumbnail_local_path
            FROM items
            WHERE local_status = 'downloading'
            ORDER BY last_updated_epoch ASC, title COLLATE NOCASE ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _get_items_for_status_sync(db_path: str | Path) -> list[dict[str, Any]]:
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
                item_url
            FROM items
            WHERE local_status IN ('downloading', 'queued', 'force_queued')
            ORDER BY
                CASE
                    WHEN local_status = 'downloading' THEN 0
                    WHEN local_status = 'force_queued' THEN 1
                    ELSE 2
                END ASC,
                last_updated_epoch ASC,
                title COLLATE NOCASE ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _normalize_progress_pct(progress: Any) -> float:
    try:
        numeric_progress = float(progress)
    except (TypeError, ValueError):
        return 0.0

    if numeric_progress <= 1.0:
        numeric_progress *= 100.0
    return max(0.0, min(numeric_progress, 100.0))


def _resolve_metadata_recovery_windows(config: dict[str, Any] | None) -> tuple[int, int]:
    if not isinstance(config, dict):
        return (DEFAULT_METADATA_RECOVERY_MINUTES, DEFAULT_METADATA_RECOVERY_COOLDOWN_MINUTES)

    qb_cfg = config.get("qbittorrent", {})
    if not isinstance(qb_cfg, dict):
        return (DEFAULT_METADATA_RECOVERY_MINUTES, DEFAULT_METADATA_RECOVERY_COOLDOWN_MINUTES)

    try:
        recovery_minutes = int(qb_cfg.get("metadata_stuck_recovery_minutes", DEFAULT_METADATA_RECOVERY_MINUTES) or 0)
    except (TypeError, ValueError):
        recovery_minutes = DEFAULT_METADATA_RECOVERY_MINUTES

    try:
        cooldown_minutes = int(qb_cfg.get("metadata_recovery_cooldown_minutes", DEFAULT_METADATA_RECOVERY_COOLDOWN_MINUTES) or 0)
    except (TypeError, ValueError):
        cooldown_minutes = DEFAULT_METADATA_RECOVERY_COOLDOWN_MINUTES

    if recovery_minutes < 0:
        recovery_minutes = 0
    if cooldown_minutes < 1:
        cooldown_minutes = 1
    return (recovery_minutes, cooldown_minutes)


def _clear_metadata_recovery_tracking(item_id: str) -> None:
    _METADATA_STUCK_SINCE.pop(item_id, None)
    _METADATA_RECOVERY_LAST_ATTEMPT.pop(item_id, None)


def _attempt_metadata_file_recovery(
    *,
    item: dict[str, Any],
    candidate: dict[str, Any],
    config: dict[str, Any],
    download_adapter: DownloadAdapter,
) -> bool:
    if not hasattr(download_adapter, "replace_with_torrent_file"):
        return False

    item_url = str(item.get("item_url", "") or "").strip()
    if not item_url:
        print(f"Metadata recovery skipped for {item['id']}: missing item_url.")
        return False

    info_hash = str(candidate.get("hash_string", "") or "").strip().lower()
    if not info_hash:
        return False

    try:
        save_path = _resolve_save_path(config)
        tags = _resolve_tags(config)
        with tempfile.TemporaryDirectory(prefix="sp-meta-recovery-") as tmp_dir:
            torrent_file = torrent_uploader.download_personalized_torrent_file(
                item=item,
                config=config,
                info_hash=info_hash,
                output_dir=Path(tmp_dir),
            )
            torrent_source = (
                "redistributable"
                if "redistributable" in str(getattr(torrent_file, "name", "")).lower()
                else "personalized"
            )
            replaced = bool(
                download_adapter.replace_with_torrent_file(
                    info_hash,
                    str(torrent_file),
                    save_path,
                    tags,
                )
            )
        if replaced:
            print(
                f"Recovered metadata-stuck torrent for {item['id']} ({_safe_text(item['title'])}) "
                f"by injecting {torrent_source} .torrent file."
            )
        return replaced
    except Exception as exc:  # noqa: BLE001
        print(
            f"Metadata recovery failed for {item['id']} ({_safe_text(item['title'])}): {exc}"
        )
        return False


def _matches_auto_download_rule(
    item: dict[str, Any],
    active_rule_sets: list[dict[str, Any]],
    legacy_category_rule_map: dict[str, bool],
) -> bool:
    if active_rule_sets:
        for rule_set in active_rule_sets:
            if not bool(rule_set.get("auto_download", True)):
                continue
            if item_matches_rule_set(item, rule_set):
                return True
        return False

    category = str(item.get("site_category", "") or "")
    return bool(legacy_category_rule_map.get(category, False))
