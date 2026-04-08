from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class GalleryItem:
    item_id: str
    title: str
    category: str = ""
    item_url: str = ""
    secondary_url: str = ""
    size_bytes: int = 0
    has_torrent: bool = False
    torrent_label: str = ""
    torrent_info: str = ""
    progress: float = 0.0
    status: str = "new"
    attachments: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["size_mb"] = round(self.size_bytes / (1024 * 1024), 2)
        record["progress_pct"] = round(self.progress * 100, 1)
        return record
