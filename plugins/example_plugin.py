from __future__ import annotations


def fetch_items(source_url: str, cookies_dict: dict[str, str]) -> list[dict]:
    return [
        {
            "item_id": "plugin-001",
            "title": "Plugin Example Item",
            "category": "Plugin Demo",
            "item_url": source_url or "https://example.invalid/item",
            "secondary_url": "",
            "size_bytes": 52428800,
            "has_torrent": True,
            "torrent_label": "Example attachment",
            "torrent_info": f"Cookies provided: {len(cookies_dict)}",
            "progress": 0.2,
            "status": "queued",
            "attachments": [
                {
                    "label": "Attachment A",
                    "url": "https://example.invalid/files/a.bin",
                    "kind": "archive",
                }
            ],
            "notes": "Replace this file with your own local plugin implementation.",
        }
    ]
