from __future__ import annotations

import importlib.util
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from models import GalleryItem


@dataclass
class PluginContext:
    source_url: str = ""
    cookies_text: str = ""
    plugin_path: str = ""
    plugin_function: str = "fetch_items"


class SourcePlugin(ABC):
    name = "base"

    @abstractmethod
    def fetch_items(self, context: PluginContext) -> Iterable[GalleryItem]:
        raise NotImplementedError


class UnimplementedRemotePlugin(SourcePlugin):
    name = "custom_remote"

    def fetch_items(self, context: PluginContext) -> Iterable[GalleryItem]:
        raise NotImplementedError(
            "Add your own source-specific fetch logic in "
            "generic_plugin_app/plugin_api.py or a separate plugin module."
        )


class LocalModulePlugin(SourcePlugin):
    name = "local_module"

    def fetch_items(self, context: PluginContext) -> Iterable[GalleryItem]:
        module_path = Path(context.plugin_path).expanduser()
        if not module_path.is_file():
            raise FileNotFoundError(f"Plugin file not found: {module_path}")

        spec = importlib.util.spec_from_file_location("custom_plugin", module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load plugin module from: {module_path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        function_name = context.plugin_function or "fetch_items"
        fetcher = getattr(module, function_name, None)
        if fetcher is None or not callable(fetcher):
            raise AttributeError(
                f"Plugin function '{function_name}' not found in {module_path.name}"
            )

        cookies_dict = parse_cookies_text(context.cookies_text)
        result = fetcher(context.source_url, cookies_dict)
        return normalize_items(result)


def parse_cookies_text(cookies_text: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookies_text.split(";"):
        chunk = part.strip()
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def normalize_items(raw_items: Iterable[GalleryItem | dict]) -> list[GalleryItem]:
    items: list[GalleryItem] = []
    for raw in raw_items:
        if isinstance(raw, GalleryItem):
            items.append(raw)
            continue

        if not isinstance(raw, dict):
            raise TypeError("Plugin results must be GalleryItem objects or dictionaries.")

        size_bytes = raw.get("size_bytes")
        if size_bytes in (None, "", 0) and raw.get("size_mb") not in (None, ""):
            size_bytes = int(float(raw["size_mb"]) * 1024 * 1024)
        else:
            size_bytes = int(float(size_bytes or 0))

        progress = raw.get("progress")
        if progress in (None, "") and raw.get("progress_pct") not in (None, ""):
            progress = float(raw["progress_pct"]) / 100.0
        else:
            progress = float(progress or 0)

        items.append(
            GalleryItem(
                item_id=str(raw.get("item_id", "")),
                title=str(raw.get("title", "")),
                category=str(raw.get("category", "")),
                item_url=str(raw.get("item_url", "")),
                secondary_url=str(raw.get("secondary_url", "")),
                size_bytes=size_bytes,
                has_torrent=bool(raw.get("has_torrent", False)),
                torrent_label=str(raw.get("torrent_label", "")),
                torrent_info=str(raw.get("torrent_info", "")),
                progress=progress,
                status=str(raw.get("status", "new")),
                attachments=list(raw.get("attachments", [])),
                notes=str(raw.get("notes", "")),
            )
        )
    return items
