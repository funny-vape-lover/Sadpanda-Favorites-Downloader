from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import lanraragi_sync


class LanraragiRevisionSyncTests(unittest.TestCase):
    def test_revision_candidates_prioritize_parent_and_include_chain(self) -> None:
        item = {
            "id": "g-4030899",
            "parent_gid": "4030769",
            "first_gid": "4030000",
        }
        records = {
            "g-4030769": {"id": "g-4030769", "title": "Parent"},
            "g-4030000": {"id": "g-4030000", "title": "First"},
            "g-4029999": {"id": "g-4029999", "title": "Earlier"},
        }
        graph = {
            "g-4030899": {"g-4030769", "g-4030000"},
            "g-4030769": {"g-4030899", "g-4029999"},
            "g-4030000": {"g-4030899"},
            "g-4029999": {"g-4030769"},
        }

        candidates = lanraragi_sync._get_revision_candidates(item, records, graph)

        self.assertEqual(
            [candidate["gid"] for candidate in candidates],
            ["4030769", "4030000", "4029999"],
        )

    def test_local_arcid_matches_lanraragi_first_512000_byte_hash(self) -> None:
        payload = (b"revision-payload" * 40000) + b"ignored-tail"
        with tempfile.TemporaryDirectory() as temp_dir:
            media_path = Path(temp_dir) / "g-[4030899] gallery.zip"
            media_path.write_bytes(payload)
            expected = hashlib.sha1(payload[:512000]).hexdigest()

            with (
                patch.object(
                    lanraragi_sync.media_importer,
                    "_list_media_library_entries",
                    return_value=[media_path],
                ),
                patch.object(
                    lanraragi_sync.media_importer,
                    "_find_media_library_file",
                    return_value=media_path,
                ),
            ):
                arcid, resolved_path = lanraragi_sync._compute_local_lanraragi_arcid(
                    item={"id": "g-4030899", "title": "Latest"},
                    revision_candidates=[
                        {"id": "g-4030769", "gid": "4030769", "title": "Parent"}
                    ],
                    media_library=temp_dir,
                )

        self.assertEqual(arcid, expected)
        self.assertEqual(resolved_path, str(media_path))

    @patch.object(lanraragi_sync, "_get_items_for_sync")
    @patch.object(lanraragi_sync, "_build_session")
    @patch.object(lanraragi_sync, "_compute_local_lanraragi_arcid")
    @patch.object(lanraragi_sync, "_find_archive_candidate")
    @patch.object(lanraragi_sync, "_fetch_metadata")
    def test_revision_fallback_updates_same_payload_with_latest_metadata(
        self,
        fetch_metadata: Mock,
        find_candidate: Mock,
        compute_arcid: Mock,
        build_session: Mock,
        get_items: Mock,
    ) -> None:
        arcid = "a" * 40
        item = self._latest_item()
        get_items.return_value = [item]
        session = Mock()
        response = Mock()
        session.put.return_value = response
        build_session.return_value = session
        compute_arcid.return_value = (arcid, "/media/latest.zip")
        find_candidate.side_effect = lambda _session, _base, gid: (
            None if gid == "4030899" else {"arcid": arcid, "title": "Old"}
        )
        fetch_metadata.return_value = {
            "arcid": arcid,
            "title": "Old",
            "tags": "gid:4030769",
            "summary": "",
        }

        result = lanraragi_sync.sync_favorites_metadata(
            "unused.db",
            {
                "api_key": "key",
                "base_url": "http://lanraragi",
                "media_library": "/media",
            },
        )

        self.assertEqual(result, {"matched": 1, "updated": 1, "skipped": 0, "failed": 0})
        payload = session.put.call_args.kwargs["data"]
        self.assertEqual(payload["title"], "Latest title")
        self.assertIn("gid:4030899", payload["tags"])
        self.assertIn("previous_gid:4030769", payload["tags"])
        response.raise_for_status.assert_called_once()

    @patch.object(lanraragi_sync, "_get_items_for_sync")
    @patch.object(lanraragi_sync, "_build_session")
    @patch.object(lanraragi_sync, "_compute_local_lanraragi_arcid")
    @patch.object(lanraragi_sync, "_find_archive_candidate")
    def test_revision_fallback_rejects_different_payload(
        self,
        find_candidate: Mock,
        compute_arcid: Mock,
        build_session: Mock,
        get_items: Mock,
    ) -> None:
        get_items.return_value = [self._latest_item()]
        session = Mock()
        build_session.return_value = session
        compute_arcid.return_value = ("a" * 40, "/media/latest.zip")
        find_candidate.side_effect = lambda _session, _base, gid: (
            None if gid == "4030899" else {"arcid": "b" * 40, "title": "Old"}
        )

        result = lanraragi_sync.sync_favorites_metadata(
            "unused.db",
            {
                "api_key": "key",
                "base_url": "http://lanraragi",
                "media_library": "/media",
            },
        )

        self.assertEqual(result, {"matched": 0, "updated": 0, "skipped": 1, "failed": 0})
        session.put.assert_not_called()

    @staticmethod
    def _latest_item() -> dict[str, object]:
        return {
            "id": "g-4030899",
            "title": "Latest title",
            "title_jpn": "",
            "item_url": "https://exhentai.org/g/4030899/token/",
            "uploader": "Uploader",
            "site_category": "doujinshi",
            "favorite_category": "Favorites 0",
            "last_updated_epoch": 123,
            "rating": 4.5,
            "expunged": 0,
            "tags": "artist:test",
            "favorite_note": "",
            "favorited_epoch": 456,
            "revision_candidates": [
                {"id": "g-4030769", "gid": "4030769", "title": "Old title"}
            ],
        }


if __name__ == "__main__":
    unittest.main()
