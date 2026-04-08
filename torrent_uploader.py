from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import database
from qbittorrent_adapter import QbittorrentAdapter


class TorrentBuildError(Exception):
    pass


class TorrentUploadError(Exception):
    pass


class TorrentAddError(Exception):
    pass


class TorrentUploadFormError(Exception):
    pass


INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]+')
MAX_FILENAME_BYTES = 240


def _ensure_torf():
    try:
        import torf  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover - runtime guard
        raise TorrentBuildError(
            "Python package 'torf' is required to build .torrent files. "
            "Add it to requirements.txt and install it in this environment."
        ) from exc


def _fetch_torrent_page(session: requests.Session, gid: str, token: str) -> requests.Response:
    response = session.get(
        f"https://exhentai.org/gallerytorrents.php?gid={gid}&t={token}",
        timeout=30,
    )
    response.raise_for_status()
    return response


def _discover_upload_form(session: requests.Session, gid: str, token: str) -> tuple[str, str, dict[str, str]]:
    response = _fetch_torrent_page(session, gid, token)
    html = response.text
    soup = BeautifulSoup(html, "html.parser")

    announce_match = re.search(r"https?://[\w\.-]+/\d+/announce", html, re.IGNORECASE)
    if not announce_match:
        raise TorrentUploadFormError("Torrent upload page did not expose an announce URL.")

    upload_form = soup.find("form", attrs={"enctype": "multipart/form-data"})
    if upload_form is None:
        raise TorrentUploadFormError("Torrent upload form was not found on the gallery torrent page.")

    action = str(upload_form.get("action") or "").strip()
    if not action:
        raise TorrentUploadFormError("Torrent upload form did not include an action URL.")

    payload: dict[str, str] = {}
    for input_node in upload_form.find_all("input"):
        name = str(input_node.get("name") or "").strip()
        if not name or name == "torrentfile":
            continue
        payload[name] = str(input_node.get("value") or "")

    return announce_match.group(0), urljoin(response.url, action), payload


def _build_torrent(
    archive_path: Path,
    trackers: list[str],
    piece_size_mb: int,
    comment: str,
    out_path: Path,
) -> str:
    _ensure_torf()
    import torf

    torrent = torf.Torrent(path=archive_path)
    torrent.announce_urls = trackers
    if piece_size_mb and piece_size_mb > 0:
        torrent.piece_size = piece_size_mb * 1024 * 1024
    if comment:
        torrent.comment = comment
    torrent.generate()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    torrent.write(out_path)
    return str(torrent.infohash)


def _safe_torrent_filename(title: str, fallback: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub(" ", str(title or "")).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    stem = cleaned or fallback
    return _truncate_filename(stem, ".torrent")


def _safe_archive_filename(title: str, suffix: str, fallback: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub(" ", str(title or "")).strip().rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    normalized_suffix = suffix if str(suffix).startswith(".") else f".{suffix}" if suffix else ".zip"
    stem = cleaned or fallback
    return _truncate_filename(stem, normalized_suffix)


def _truncate_filename(stem: str, suffix: str) -> str:
    normalized_suffix = str(suffix or "")
    suffix_bytes = len(normalized_suffix.encode("utf-8"))
    if suffix_bytes >= MAX_FILENAME_BYTES:
        normalized_suffix = ""
        suffix_bytes = 0
    budget = max(1, MAX_FILENAME_BYTES - suffix_bytes)
    trimmed_stem = str(stem or "archive").encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    trimmed_stem = trimmed_stem.rstrip(" .") or "archive"
    return f"{trimmed_stem}{normalized_suffix}"


def _parse_gid_token(item: Mapping[str, Any]) -> tuple[str, str]:
    url = str(item.get("item_url", "") or "")
    match = re.search(r"/g/(\d+)/([a-f0-9]+)", url, re.IGNORECASE)
    if not match:
        raise TorrentUploadError("Item URL missing gid/token; cannot upload torrent.")
    return match.group(1), match.group(2)


def has_gid_and_token(item: Mapping[str, Any]) -> bool:
    url = str(item.get("item_url", "") or "")
    return bool(re.search(r"/g/(\d+)/([a-f0-9]+)", url, re.IGNORECASE))


def _download_torrent_by_href(
    *,
    session: requests.Session,
    base_url: str,
    href: str,
    output_dir: Path,
    filename_stem: str,
) -> Path:
    response = session.get(urljoin(base_url, href), timeout=60)
    response.raise_for_status()
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{filename_stem}.torrent"
    target.write_bytes(response.content)
    return target


def download_personalized_torrent_file(
    *,
    item: Mapping[str, Any],
    config: Mapping[str, Any],
    info_hash: str,
    output_dir: Path,
    prefer_redistributable: bool = True,
) -> Path:
    gid, token = _parse_gid_token(item)

    session = requests.Session()
    session.trust_env = False
    cookie_str = str(config.get("exhentai", {}).get("cookies", ""))
    for k, v in (cookie.split("=", 1) for cookie in cookie_str.split(";") if "=" in cookie):
        session.cookies.set(k.strip(), v.strip())

    response = _fetch_torrent_page(session, gid, token)
    info_hash_value = str(info_hash or "").strip().lower()
    if not info_hash_value:
        raise TorrentUploadError("Missing torrent hash; cannot fetch personalized torrent file.")

    # Prefer redistributable torrent when present (stable .torrent fetch),
    # then fall back to the personalized hash-specific torrent URL.
    if prefer_redistributable:
        soup = BeautifulSoup(response.text, "html.parser")
        redistributable_link = None
        for link in soup.find_all("a", href=True):
            label = str(link.get_text(" ", strip=True) or "").strip().lower()
            href = str(link.get("href", "") or "").strip()
            if not href:
                continue
            if "redistributable" in label and "/torrent/" in href:
                redistributable_link = href
                break
        if redistributable_link:
            return _download_torrent_by_href(
                session=session,
                base_url=response.url,
                href=redistributable_link,
                output_dir=output_dir,
                filename_stem=f"{str(item.get('id') or 'item')}-{info_hash_value}-redistributable",
            )

    match = re.search(
        rf"https://exhentai\.org/torrent/\d+/[^\"'>]+/{re.escape(info_hash_value)}\.torrent",
        response.text,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise TorrentUploadError(f"No torrent download link found for hash {info_hash_value}.")
    return _download_torrent_by_href(
        session=session,
        base_url=response.url,
        href=match.group(0),
        output_dir=output_dir,
        filename_stem=f"{str(item.get('id') or 'item')}-{info_hash_value}",
    )


def _upload_torrent_file(
    session: requests.Session,
    upload_url: str,
    form_payload: Mapping[str, str],
    torrent_path: Path,
) -> requests.Response:
    with torrent_path.open("rb") as f:
        files = {"torrentfile": (torrent_path.name, f, "application/x-bittorrent")}
        resp = session.post(upload_url, data=dict(form_payload), files=files, timeout=60)
    if resp.status_code >= 400:
        raise TorrentUploadError(f"Upload failed with HTTP {resp.status_code}")
    return resp


def _download_posted_torrent(
    session: requests.Session,
    upload_response_html: str,
    gid: str,
    token: str,
    fallback_path: Path,
) -> Path:
    response = None
    success_soup = BeautifulSoup(upload_response_html, "html.parser")
    link = success_soup.find("a", href=re.compile(r"/torrent/\d+/", re.IGNORECASE))
    href = str(link.get("href") or "").strip() if link else ""
    if not href:
        response = _fetch_torrent_page(session, gid, token)
        soup = BeautifulSoup(response.text, "html.parser")
        link = soup.find("a", href=re.compile(r"torrentdownload|/torrent/\d+/", re.IGNORECASE))
        if not link:
            raise TorrentUploadError("Upload completed, but no downloadable personalized torrent link was found.")
        href = str(link.get("href") or "").strip()
        if not href:
            raise TorrentUploadError("Torrent download link was empty.")

    base_url = response.url if response is not None else f"https://exhentai.org/gallerytorrents.php?gid={gid}&t={token}"
    resp = session.get(urljoin(base_url, href), timeout=60)
    resp.raise_for_status()
    target = fallback_path.with_name(f"posted-{fallback_path.name}")
    target.write_bytes(resp.content)
    return target


def process_item(
    item: dict[str, Any],
    config: dict[str, Any],
    adapter: QbittorrentAdapter,
    db_path: str | Path,
) -> dict[str, Any]:
    now = int(time.time())
    archive_path = Path(item.get("archive_path") or "")
    if not archive_path.exists():
        raise TorrentBuildError(f"Archive file missing: {archive_path}")

    settings = config.get("torrent_upload", {}) if isinstance(config, dict) else {}
    staging_dir = Path(settings.get("staging_dir") or (Path(config["paths"]["archive_library"]).parent / "Torrent_Staging"))
    generated_dir = Path(settings.get("generated_torrent_dir") or (Path(config["paths"]["archive_library"]).parent / "Generated_Torrents"))
    piece_size_mb = int(settings.get("piece_size_mb") or 4)
    trackers_extra: list[str] = list(settings.get("trackers_extra") or [])

    staging_dir.mkdir(parents=True, exist_ok=True)
    generated_dir.mkdir(parents=True, exist_ok=True)

    gid, token = _parse_gid_token(item)

    session = requests.Session()
    session.trust_env = False
    cookie_str = str(config.get("exhentai", {}).get("cookies", ""))
    for k, v in (cookie.split("=", 1) for cookie in cookie_str.split(";") if "=" in cookie):
        session.cookies.set(k.strip(), v.strip())

    announce, upload_url, upload_payload = _discover_upload_form(session, gid, token)
    trackers = [announce] + [t for t in trackers_extra if t]

    staging_path = staging_dir / _safe_archive_filename(
        str(item.get("title", "")),
        archive_path.suffix or ".zip",
        str(item["id"]),
    )
    shutil.move(archive_path, staging_path)

    final_archive_path = None
    torrent_added = False
    try:
        torrent_path = generated_dir / _safe_torrent_filename(
            str(item.get("title", "")),
            str(item["id"]),
        )
        info_hash = _build_torrent(
            archive_path=staging_path,
            trackers=trackers,
            piece_size_mb=piece_size_mb,
            comment=str(item.get("title", "")),
            out_path=torrent_path,
        )

        upload_resp = _upload_torrent_file(session, upload_url, upload_payload, torrent_path)
        if "Upload Torrent" in upload_resp.text and "torrentdownload" not in upload_resp.text:
            raise TorrentUploadError("ExH returned the upload form again; the torrent was likely rejected.")
        posted_torrent_path = _download_posted_torrent(session, upload_resp.text, gid, token, torrent_path)

        qb_save_path = Path(config.get("qbittorrent", {}).get("download_path") or staging_dir)
        qb_save_path.mkdir(parents=True, exist_ok=True)
        final_archive_path = qb_save_path / staging_path.name
        shutil.move(staging_path, final_archive_path)

        adapter.client.torrents_add(
            torrent_files=[posted_torrent_path],
            save_path=str(qb_save_path),
            is_paused=True,
        )
        torrent_added = True

        adapter.client.torrents_recheck(hashes=info_hash)
        for _ in range(600):  # wait up to 10 minutes for hash check
            status = adapter.get_status(info_hash)
            if status.get("progress", 0) >= 0.999:
                break
            time.sleep(1)
        else:
            raise TorrentAddError("qB recheck did not reach 100% in time.")
        adapter.client.torrents_resume(hashes=info_hash)
    except Exception:
        # attempt to restore archive if we moved it
        if not torrent_added and final_archive_path and final_archive_path.exists():
            try:
                shutil.move(final_archive_path, archive_path)
            except Exception:
                pass
        elif staging_path.exists():
            try:
                shutil.move(staging_path, archive_path)
            except Exception:
                pass
        raise

    # Persist metadata
    database.upsert_torrent(
        {
            "hash_string": info_hash,
            "parent_item_id": item["id"],
            "name": staging_path.name,
            "size": final_archive_path.stat().st_size,
            "added_epoch": now,
            "is_best_candidate": True,
        },
        db_path=db_path,
    )
    database.update_item_download_flags(
        item_id=item["id"],
        torrent_downloaded=True,
        archive_downloaded=False,
        db_path=db_path,
    )
    database.update_item_status(
        item_id=item["id"],
        local_status="completed",
        db_path=db_path,
    )
    database.update_item_progress(
        item_id=item["id"],
        progress_pct=100.0,
        db_path=db_path,
    )
    with database.get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE items
            SET archive_path = ?,
                torrent_from_archive = 1,
                torrent_created_epoch = ?,
                torrent_uploaded_epoch = ?,
                torrent_upload_status = 'uploaded',
                torrent_staging_path = '',
                torrent_hash_uploaded = ?,
                external_method = '',
                external_label = '',
                external_size_text = '',
                external_cost_gp = NULL
            WHERE id = ?
            """,
            ("", now, now, info_hash, item["id"]),
        )
    database.resolve_error(item_id=item["id"], error_type="missing_archive", db_path=db_path)
    database.resolve_error(item_id=item["id"], error_type="missing_torrent", db_path=db_path)

    return {"info_hash": info_hash, "announce": announce, "torrent_path": str(posted_torrent_path)}
