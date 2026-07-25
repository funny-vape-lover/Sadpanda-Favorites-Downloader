from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


REQUEST_TIMEOUT = 30
ARCHIVE_STREAM_READ_TIMEOUT = 180
SAFE_GET_MAX_ATTEMPTS = 3
SAFE_GET_BACKOFF_SECONDS = (2, 5)
POLL_DELAY_SECONDS = 5
MAX_POLL_ATTEMPTS = 10
INITIAL_STREAM_CHUNK_SIZE = 64 * 1024
STREAM_CHUNK_SIZE = 1024 * 1024
MAX_FILENAME_BYTES = 240
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/133.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


class ArchiveDownloadCancelled(Exception):
    pass


class ExternalAdapter:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(DEFAULT_HEADERS)

        raw_cookies = config.get("exhentai", {}).get("cookies", "")
        cookies_dict = {
            key.strip(): value.strip()
            for key, value in (
                item.split("=", 1) for item in raw_cookies.split(";") if "=" in item
            )
        }
        self.session.cookies.update(cookies_dict)

    def inspect_item(self, item: dict[str, Any]) -> dict[str, Any]:
        item_id = str(item.get("id") or item.get("item_id") or "")
        gallery_info = self._parse_gallery_reference(item)
        if gallery_info is None:
            return {
                "success": False,
                "item_id": item_id,
                "reason": "invalid_gallery_url",
                "message": "Could not parse gid/token from item_url.",
                "options": [],
            }

        archiver_url = gallery_info["archiver_url"]
        try:
            response = self._get_with_retry(
                archiver_url,
                headers={"Referer": str(item.get("item_url", "")).strip() or archiver_url},
            )
        except requests.RequestException as exc:
            return {
                "success": False,
                "item_id": item_id,
                "reason": "request_failed",
                "message": str(exc),
                "archiver_url": archiver_url,
                "options": [],
            }

        options = self._parse_options(response.text, archiver_url)
        return {
            "success": True,
            "item_id": item_id,
            "archiver_url": archiver_url,
            "options": options,
        }

    def handle_item(
        self,
        item: dict[str, Any],
        save_dir: str,
        progress_callback=None,
        cancel_check=None,
    ) -> dict[str, Any]:
        item_id = str(item.get("id") or item.get("item_id") or "")
        title = str(item.get("title") or item_id)

        cancel_result = self._cancelled_result(item_id)
        if self._is_cancel_requested(cancel_check):
            return cancel_result

        inspection = self.inspect_item(item)
        if not inspection.get("success"):
            message = inspection.get("message") or inspection.get("reason") or "Inspection failed."
            print(f"[{item_id}] FAILED: {message}")
            return inspection

        choice = self._choose_download_option(item, inspection)
        if choice is None:
            print(f"[{item_id}] Skipping external download: no eligible archive option.")
            return {
                "success": False,
                "item_id": item_id,
                "reason": "no_eligible_method",
                "skipped": True,
                "options": inspection.get("options", []),
            }

        cost_text = choice.get("cost_text", "").strip() or "Unknown cost"
        print(
            f"[{item_id}] Requesting {choice['label']} "
            f"({choice['kind_label']}, {cost_text})..."
        )
        if self._is_cancel_requested(cancel_check):
            return cancel_result

        try:
            response = self.session.post(
                inspection["archiver_url"],
                data=choice["payload"],
                timeout=REQUEST_TIMEOUT,
                headers={"Referer": str(item.get("item_url", "")).strip() or inspection["archiver_url"]},
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"[{item_id}] FAILED: Could not submit archive request: {exc}")
            return {
                "success": False,
                "item_id": item_id,
                "reason": "submit_failed",
                "message": str(exc),
                "method": choice["method"],
                "label": choice["label"],
                "cost_gp": choice.get("cost_gp"),
                "request_submitted": True,
            }

        if choice.get("kind") == "hath":
            queue_result = self._resolve_hath_queue_response(response.text)
            if queue_result.get("success"):
                print(f"[{item_id}] H@H download successfully queued.")
                return {
                    "success": True,
                    "item_id": item_id,
                    "method": choice["method"],
                    "label": choice["label"],
                    "size_text": choice.get("size_text", ""),
                    "cost_gp": choice.get("cost_gp"),
                    "request_submitted": True,
                    "queued_hath": True,
                    "message": queue_result.get("message", ""),
                }

            reason = str(queue_result.get("reason") or "hath_queue_failed")
            message = str(queue_result.get("message") or "")
            print(f"[{item_id}] FAILED: {reason} | {message}")
            return {
                "success": False,
                "item_id": item_id,
                "reason": reason,
                "message": message,
                "method": choice["method"],
                "label": choice["label"],
                "cost_gp": choice.get("cost_gp"),
                "request_submitted": True,
            }

        resolution = self._resolve_download_page(response.text, response.url)
        download_url = resolution.get("download_url")
        filename_hint = resolution.get("filename", "")

        attempt = 0
        while not download_url and resolution.get("status") == "wait" and attempt < MAX_POLL_ATTEMPTS:
            if self._is_cancel_requested(cancel_check):
                return cancel_result
            attempt += 1
            print(
                f"[{item_id}] Locating archive server... waiting {POLL_DELAY_SECONDS} seconds "
                f"(Attempt {attempt}/{MAX_POLL_ATTEMPTS})"
            )
            time.sleep(POLL_DELAY_SECONDS)
            try:
                response = self._get_with_retry(
                    inspection["archiver_url"],
                    headers={"Referer": inspection["archiver_url"]},
                )
            except requests.RequestException as exc:
                print(f"[{item_id}] FAILED during polling: {exc}")
                return {
                    "success": False,
                    "item_id": item_id,
                    "reason": "poll_failed",
                    "message": str(exc),
                    "method": choice["method"],
                    "label": choice["label"],
                    "cost_gp": choice.get("cost_gp"),
                    "request_submitted": True,
                }
            resolution = self._resolve_download_page(response.text, response.url)
            download_url = resolution.get("download_url")
            if not filename_hint:
                filename_hint = resolution.get("filename", "")

        if resolution.get("status") == "error":
            reason = resolution.get("reason") or "unexpected_response"
            print(f"[{item_id}] FAILED: {reason}")
            return {
                "success": False,
                "item_id": item_id,
                "reason": reason,
                "message": str(resolution.get("message", "") or ""),
                "method": choice["method"],
                "label": choice["label"],
                "cost_gp": choice.get("cost_gp"),
                "request_submitted": True,
            }

        if not download_url:
            print(f"[{item_id}] FAILED: Could not grab download link after polling.")
            return {
                "success": False,
                "item_id": item_id,
                "reason": "download_url_missing",
                "method": choice["method"],
                "label": choice["label"],
                "cost_gp": choice.get("cost_gp"),
                "request_submitted": True,
            }

        os.makedirs(save_dir, exist_ok=True)
        if self._is_cancel_requested(cancel_check):
            return cancel_result

        try:
            print(f"[{item_id}] Archive server ready. Opening download stream...")
            with self._get_with_retry(
                download_url,
                stream=True,
                read_timeout=ARCHIVE_STREAM_READ_TIMEOUT,
                headers={"Referer": inspection["archiver_url"]},
            ) as response:
                filename = self._derive_filename(
                    item_id=item_id,
                    title=title,
                    filename_hint=filename_hint,
                    content_disposition=response.headers.get("content-disposition", ""),
                )
                save_path = os.path.join(save_dir, filename)
                total_length_header = response.headers.get("content-length")
                size_label = (
                    f"{int(total_length_header):,} bytes"
                    if total_length_header and total_length_header.isdigit()
                    else "unknown size"
                )
                print(f"[{item_id}] Download stream opened from {response.url} ({size_label}).")
                stream_result = self._stream_download_to_path(
                    response=response,
                    save_path=save_path,
                    item_id=item_id,
                    expected_kind=choice["kind"],
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                )
                if not stream_result["success"]:
                    return {
                        "success": False,
                        "item_id": item_id,
                        "reason": stream_result["reason"],
                        "message": stream_result.get("message", ""),
                        "method": choice["method"],
                        "label": choice["label"],
                        "cost_gp": choice.get("cost_gp"),
                        "request_submitted": True,
                    }
        except requests.RequestException as exc:
            print(f"[{item_id}] FAILED during file download: {exc}")
            return {
                "success": False,
                "item_id": item_id,
                "reason": "file_download_failed",
                "message": str(exc),
                "method": choice["method"],
                "label": choice["label"],
                "cost_gp": choice.get("cost_gp"),
                "request_submitted": True,
            }

        print(f"[{item_id}] Successfully saved to {save_path}")
        return {
            "success": True,
            "item_id": item_id,
            "method": choice["method"],
            "label": choice["label"],
            "path": save_path,
            "size_text": size_label,
            "cost_gp": choice.get("cost_gp"),
            "request_submitted": True,
        }

    def _stream_download_to_path(
        self,
        response: requests.Response,
        save_path: str,
        item_id: str,
        expected_kind: str,
        progress_callback=None,
        cancel_check=None,
    ) -> dict[str, Any]:
        active_response = response
        referer = str(response.request.headers.get("Referer", ""))

        for _ in range(5):
            if self._is_cancel_requested(cancel_check):
                active_response.close()
                return self._cancelled_result(item_id)
            content_type = str(active_response.headers.get("content-type", "")).lower()
            total_length_header = active_response.headers.get("content-length")
            total_bytes = (
                int(total_length_header)
                if total_length_header and total_length_header.isdigit()
                else None
            )

            stream = active_response.iter_content(chunk_size=INITIAL_STREAM_CHUNK_SIZE)
            try:
                first_chunk = next(stream, b"")
            except requests.RequestException as exc:
                active_response.close()
                return {
                    "success": False,
                    "reason": "stream_read_failed",
                    "message": str(exc),
                }

            if not first_chunk:
                active_response.close()
                return {
                    "success": False,
                    "reason": "empty_download",
                    "message": "Server returned no file content.",
                }

            analysis = self._analyze_download_prefix(
                first_bytes=first_chunk[:1024],
                content_type=content_type,
                expected_kind=expected_kind,
                response_url=active_response.url,
            )
            if analysis["success"]:
                result = self._write_stream_to_path(
                    save_path=save_path,
                    item_id=item_id,
                    first_chunk=first_chunk,
                    remaining_response=active_response,
                    total_bytes=total_bytes,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                )
                active_response.close()
                return result

            html_body = self._collect_html_body(first_chunk, stream)
            active_response.close()

            resolution = self._resolve_download_page(
                html_body.decode("utf-8", errors="ignore"),
                active_response.url,
            )
            follow_url = resolution.get("download_url")
            if not follow_url or follow_url == active_response.url:
                return {
                    "success": False,
                    "reason": analysis["reason"],
                    "message": analysis.get("message", ""),
                }

            try:
                active_response = self._get_with_retry(
                    follow_url,
                    stream=True,
                    read_timeout=ARCHIVE_STREAM_READ_TIMEOUT,
                    headers={"Referer": referer or active_response.url},
                )
            except requests.RequestException as exc:
                return {
                    "success": False,
                    "reason": "followup_download_failed",
                    "message": str(exc),
                }
            referer = active_response.url

        active_response.close()
        return {
            "success": False,
            "reason": "download_redirect_loop",
            "message": "Archive host kept returning HTML instead of file bytes.",
        }

    def _get_with_retry(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        stream: bool = False,
        read_timeout: int = REQUEST_TIMEOUT,
    ) -> requests.Response:
        last_error: requests.RequestException | None = None
        for attempt in range(1, SAFE_GET_MAX_ATTEMPTS + 1):
            try:
                response = self.session.get(
                    url,
                    stream=stream,
                    timeout=(REQUEST_TIMEOUT, read_timeout),
                    headers=headers,
                )
                response.raise_for_status()
                return response
            except requests.HTTPError as exc:
                last_error = exc
                status_code = exc.response.status_code if exc.response is not None else 0
                if status_code not in {408, 429} and status_code < 500:
                    raise
                if exc.response is not None:
                    exc.response.close()
            except requests.RequestException as exc:
                last_error = exc

            if attempt >= SAFE_GET_MAX_ATTEMPTS:
                break
            delay = SAFE_GET_BACKOFF_SECONDS[min(attempt - 1, len(SAFE_GET_BACKOFF_SECONDS) - 1)]
            print(
                f"Safe GET failed for {url}; retrying in {delay}s "
                f"(attempt {attempt}/{SAFE_GET_MAX_ATTEMPTS}): {last_error}"
            )
            time.sleep(delay)

        if last_error is not None:
            raise last_error
        raise requests.RequestException(f"Safe GET failed without a response: {url}")

    def _write_stream_to_path(
        self,
        save_path: str,
        item_id: str,
        first_chunk: bytes,
        remaining_response: requests.Response,
        total_bytes: int | None,
        progress_callback=None,
        cancel_check=None,
    ) -> dict[str, Any]:
        downloaded_bytes = 0
        temp_path = f"{save_path}.part"
        last_reported_pct = -1
        try:
            with open(temp_path, "wb") as handle:
                if self._is_cancel_requested(cancel_check):
                    raise ArchiveDownloadCancelled()
                handle.write(first_chunk)
                downloaded_bytes += len(first_chunk)
                handle.flush()

                last_reported_pct = self._report_download_progress(
                    item_id=item_id,
                    downloaded_bytes=downloaded_bytes,
                    total_bytes=total_bytes,
                    progress_callback=progress_callback,
                    last_reported_pct=last_reported_pct,
                )
                if total_bytes and downloaded_bytes >= total_bytes:
                    handle.flush()
                    os.fsync(handle.fileno())
                    return {"success": True, "bytes_written": downloaded_bytes}

                for chunk in remaining_response.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                    if not chunk:
                        continue
                    if self._is_cancel_requested(cancel_check):
                        raise ArchiveDownloadCancelled()
                    handle.write(chunk)
                    downloaded_bytes += len(chunk)
                    handle.flush()
                    last_reported_pct = self._report_download_progress(
                        item_id=item_id,
                        downloaded_bytes=downloaded_bytes,
                        total_bytes=total_bytes,
                        progress_callback=progress_callback,
                        last_reported_pct=last_reported_pct,
                    )
                    if total_bytes and downloaded_bytes >= total_bytes:
                        break
                handle.flush()
                os.fsync(handle.fileno())
        except ArchiveDownloadCancelled:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return self._cancelled_result(item_id)
        except requests.RequestException as exc:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return {
                "success": False,
                "reason": "stream_read_failed",
                "message": str(exc),
            }
        except OSError as exc:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return {
                "success": False,
                "reason": "file_write_failed",
                "message": str(exc),
            }

        try:
            os.replace(temp_path, save_path)
        except OSError as exc:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return {
                "success": False,
                "reason": "file_finalize_failed",
                "message": str(exc),
            }

        return {"success": True, "bytes_written": downloaded_bytes}

    def _report_download_progress(
        self,
        item_id: str,
        downloaded_bytes: int,
        total_bytes: int | None,
        progress_callback=None,
        last_reported_pct: int = -1,
    ) -> int:
        if not total_bytes or total_bytes <= 0:
            return last_reported_pct
        pct = int((downloaded_bytes / total_bytes) * 100)
        pct = max(0, min(pct, 100))
        if pct != last_reported_pct:
            print(f"[{item_id}] Downloading... {pct}%")
        if callable(progress_callback):
            progress_callback(float(pct))
        return pct

    def _collect_html_body(self, first_chunk: bytes, remaining_stream) -> bytes:
        html_parts = [first_chunk]
        total_bytes = len(first_chunk)
        for chunk in remaining_stream:
            if not chunk:
                continue
            html_parts.append(chunk)
            total_bytes += len(chunk)
            if total_bytes >= 256 * 1024:
                break
        return b"".join(html_parts)

    def _analyze_download_prefix(
        self,
        first_bytes: bytes,
        content_type: str,
        expected_kind: str,
        response_url: str,
    ) -> dict[str, Any]:
        stripped = first_bytes.lstrip()
        lowercase_prefix = stripped[:512].decode("utf-8", errors="ignore").lower()
        looks_like_html = (
            "<!doctype html" in lowercase_prefix
            or "<html" in lowercase_prefix
            or "<body" in lowercase_prefix
            or "text/html" in content_type
        )

        if expected_kind in {"archive", "hath"}:
            if stripped.startswith(b"PK\x03\x04") or stripped.startswith(b"PK\x05\x06"):
                return {"success": True}

            if looks_like_html:
                snippet = " ".join(lowercase_prefix.split())[:240]
                return {
                    "success": False,
                    "reason": "html_instead_of_archive",
                    "message": (
                        f"Server returned HTML instead of a zip file from {response_url}. "
                        f"Response snippet: {snippet}"
                    ),
                }

        return {"success": True}

    def _parse_gallery_reference(self, item: dict[str, Any]) -> dict[str, str] | None:
        url = str(item.get("item_url", "")).strip()
        match = re.search(r"/g/(\d+)/([a-f0-9]+)", url, re.IGNORECASE)
        if not match:
            return None

        gid, token = match.groups()
        return {
            "gid": gid,
            "token": token,
            "archiver_url": f"https://exhentai.org/archiver.php?gid={gid}&token={token}",
        }

    def _parse_options(self, html: str, archiver_url: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        options = self._parse_archive_options(soup, archiver_url)
        options.extend(self._parse_hath_options(soup, archiver_url))
        options.sort(key=lambda option: int(option.get("sort_order", 999)))
        return options

    def _parse_archive_options(
        self,
        soup: BeautifulSoup,
        archiver_url: str,
    ) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []
        sort_order_map = {"org": 10, "res": 20}
        label_map = {"org": "Original Archive", "res": "Resample Archive"}

        for form in soup.find_all("form"):
            dltype_input = form.find("input", attrs={"name": "dltype"})
            if dltype_input is None:
                continue

            dltype = str(dltype_input.get("value", "")).strip().lower()
            if dltype not in label_map:
                continue

            submit_input = form.find("input", attrs={"name": "dlcheck"}) or form.find(
                "input",
                attrs={"type": "submit"},
            )
            if submit_input is None:
                continue

            block = form.parent if form.parent is not None else form
            strong_nodes = block.find_all("strong")
            cost_text = strong_nodes[0].get_text(" ", strip=True) if len(strong_nodes) >= 1 else ""
            size_text = strong_nodes[1].get_text(" ", strip=True) if len(strong_nodes) >= 2 else ""
            available = not submit_input.has_attr("disabled") and cost_text.upper() != "N/A"

            options.append(
                {
                    "method": f"archive_{dltype}",
                    "kind": "archive",
                    "kind_label": "Archive",
                    "label": label_map[dltype],
                    "action_url": str(form.get("action") or archiver_url),
                    "payload": {
                        "dltype": dltype,
                        "dlcheck": str(submit_input.get("value", "")).strip() or label_map[dltype],
                    },
                    "cost_text": cost_text,
                    "cost_gp": self._parse_cost_gp(cost_text),
                    "is_free": self._is_free_cost(cost_text),
                    "size_text": size_text,
                    "available": available,
                    "sort_order": sort_order_map[dltype],
                }
            )

        return options

    def _parse_hath_options(
        self,
        soup: BeautifulSoup,
        archiver_url: str,
    ) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = []
        hath_form = soup.find("form", id="hathdl_form")
        if hath_form is None:
            return options

        table = hath_form.find_next("table")
        if table is None:
            return options

        for index, cell in enumerate(table.find_all("td")):
            paragraphs = [paragraph.get_text(" ", strip=True) for paragraph in cell.find_all("p")]
            if len(paragraphs) < 3:
                continue

            resolution_label = paragraphs[0]
            size_text = paragraphs[1]
            cost_text = paragraphs[2]
            anchor = cell.find("a")
            xres = None
            if anchor is not None and anchor.get("onclick"):
                match = re.search(r"do_hathdl\('([^']+)'\)", str(anchor.get("onclick")))
                if match:
                    xres = match.group(1)

            available = bool(xres) and size_text.upper() != "N/A" and cost_text.upper() != "N/A"
            method = f"hath_{xres}" if xres else f"hath_unavailable_{index}"
            options.append(
                {
                    "method": method,
                    "kind": "hath",
                    "kind_label": "H@H",
                    "label": f"H@H {resolution_label}",
                    "action_url": str(hath_form.get("action") or archiver_url),
                    "payload": {"hathdl_xres": xres} if xres else {},
                    "cost_text": cost_text,
                    "cost_gp": self._parse_cost_gp(cost_text),
                    "is_free": self._is_free_cost(cost_text),
                    "size_text": size_text,
                    "available": available,
                    "sort_order": 100 + index,
                }
            )

        return options

    def _choose_download_option(
        self,
        item: dict[str, Any],
        inspection: dict[str, Any],
    ) -> dict[str, Any] | None:
        available_options = {
            option["method"]: option
            for option in inspection.get("options", [])
            if option.get("available")
        }

        selected_method = str(item.get("external_method", "")).strip()
        if selected_method:
            return available_options.get(selected_method)

        local_status = str(item.get("local_status", "")).strip().lower()
        if local_status in {"new", "queued"}:
            return self._auto_select_download_option(
                [
                    option
                    for option in available_options.values()
                    if option.get("kind") == "archive"
                ],
                max_external_gp=item.get("max_external_gp"),
            )

        return (
            available_options.get("archive_org")
            or available_options.get("archive_res")
            or next(iter(available_options.values()), None)
        )

    def _auto_select_download_option(
        self,
        available_options: list[dict[str, Any]],
        max_external_gp: Any,
    ) -> dict[str, Any] | None:
        gp_cap = self._safe_int(max_external_gp)
        eligible_options: list[dict[str, Any]] = []
        for option in available_options:
            cost_gp = option.get("cost_gp")
            if gp_cap is None:
                if option.get("is_free"):
                    eligible_options.append(option)
                continue
            if cost_gp is None:
                continue
            if int(cost_gp) <= gp_cap:
                eligible_options.append(option)

        if not eligible_options:
            return None

        return sorted(
            eligible_options,
            key=lambda option: (
                int(option.get("cost_gp") or 0),
                0 if str(option.get("method", "")).strip() == "archive_org" else 1,
                int(option.get("sort_order", 999)),
            ),
        )[0]

    def _resolve_hath_queue_response(self, html: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        page_text = " ".join(soup.get_text(" ", strip=True).split())

        if re.search(
            r"download\s+has\s+been\s+queued\s+for\s+client",
            page_text,
            re.IGNORECASE,
        ):
            return {"success": True, "message": page_text[:500]}

        lowered = page_text.casefold()
        if "h@h client" in lowered and (
            "do not have" in lowered
            or "no " in lowered
            or "need " in lowered
            or "required" in lowered
        ):
            reason = "hath_client_missing"
        elif "offline" in lowered:
            reason = "hath_client_offline"
        elif "resolution" in lowered and (
            "cannot" in lowered or "unavailable" in lowered or "invalid" in lowered
        ):
            reason = "hath_resolution_unavailable"
        elif "insufficient" in lowered or "you lack the" in lowered:
            reason = "insufficient_gp"
        else:
            reason = "hath_queue_failed"

        error_node = soup.select_one(".stuffbox") or soup.select_one("p.br") or soup.find("p")
        message = (
            " ".join(error_node.get_text(" ", strip=True).split())
            if error_node is not None
            else page_text[:500]
        )
        return {
            "success": False,
            "reason": reason,
            "message": message or "H@H queue response was not recognized.",
        }

    def _resolve_download_page(self, html: str, archiver_url: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        filename = ""
        db_container = soup.find("div", id="db")
        if db_container is not None:
            strong_nodes = db_container.find_all("strong")
            if strong_nodes:
                filename = strong_nodes[-1].get_text(" ", strip=True)

        if "Insufficient" in html or "You lack the" in html:
            return {"status": "error", "reason": "insufficient_gp", "filename": filename}

        download_link = soup.find(
            "a",
            string=re.compile(r"Click here to start downloading", re.IGNORECASE),
        )
        if download_link is not None and download_link.get("href"):
            return {
                "status": "ready",
                "download_url": urljoin(archiver_url, str(download_link["href"])),
                "filename": filename,
            }

        redirect_match = re.search(
            r'document\.location\s*=\s*"([^"]+)"',
            html,
            re.IGNORECASE,
        )
        if redirect_match:
            return {
                "status": "ready",
                "download_url": urljoin(archiver_url, redirect_match.group(1)),
                "filename": filename,
            }

        if (
            "Please wait" in html
            or "Locating archive server" in html
            or "preparing file for download" in html
        ):
            return {"status": "wait", "filename": filename}

        page_text = " ".join(soup.get_text(" ", strip=True).split())
        return {
            "status": "error",
            "reason": "unexpected_response",
            "message": f"Archive request returned an unrecognized page: {page_text[:300]}",
            "filename": filename,
        }

    def _derive_filename(
        self,
        item_id: str,
        title: str,
        filename_hint: str,
        content_disposition: str,
    ) -> str:
        content_name = self._filename_from_content_disposition(content_disposition)
        if content_name:
            return content_name

        if filename_hint:
            return self._sanitize_filename(filename_hint)

        clean_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()
        # Do NOT prefix gid; keep original-style archive names.
        return self._sanitize_filename(f"{clean_title or 'archive'}.zip")

    def _filename_from_content_disposition(self, header_value: str) -> str:
        if not header_value:
            return ""
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^\";]+)"?', header_value, re.IGNORECASE)
        if not match:
            return ""
        return self._sanitize_filename(match.group(1))

    def _sanitize_filename(self, value: str) -> str:
        cleaned = re.sub(r'[\\/*?:"<>|]', "", str(value)).strip().rstrip(".")
        if not cleaned:
            cleaned = "archive.zip"

        suffix = ""
        stem = cleaned
        if "." in cleaned and not cleaned.startswith("."):
            stem, suffix = cleaned.rsplit(".", 1)
            suffix = f".{suffix}"

        suffix_bytes = len(suffix.encode("utf-8"))
        if suffix_bytes >= MAX_FILENAME_BYTES:
            suffix = ""
            suffix_bytes = 0
        budget = max(1, MAX_FILENAME_BYTES - suffix_bytes)
        stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip(" .")
        stem = stem or "archive"
        return f"{stem}{suffix}"

    def _parse_cost_gp(self, cost_text: str) -> int | None:
        if self._is_free_cost(cost_text):
            return 0
        match = re.search(r"(\d[\d,]*)\s*GP", str(cost_text), re.IGNORECASE)
        if not match:
            return None
        return int(match.group(1).replace(",", ""))

    def _is_free_cost(self, cost_text: str) -> bool:
        return "free" in str(cost_text).strip().lower()

    def _safe_int(self, value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _is_cancel_requested(self, cancel_check) -> bool:
        try:
            return bool(callable(cancel_check) and cancel_check())
        except Exception:
            return False

    def _cancelled_result(self, item_id: str) -> dict[str, Any]:
        print(f"[{item_id}] Archive download cancelled.")
        return {
            "success": False,
            "item_id": item_id,
            "reason": "cancelled",
            "message": "Archive download cancelled.",
            "cancelled": True,
        }


def get_adapter(config: dict[str, Any]) -> ExternalAdapter:
    return ExternalAdapter(config)
