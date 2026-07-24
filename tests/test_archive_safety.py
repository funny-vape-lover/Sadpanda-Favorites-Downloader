from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import database
import external_manager
import media_importer
from external_adapter import ExternalAdapter


class ArchiveOptionSafetyTests(unittest.TestCase):
    def test_hath_options_are_not_exposed_as_direct_downloads(self) -> None:
        adapter = ExternalAdapter({"exhentai": {"cookies": ""}})
        adapter._parse_archive_options = Mock(return_value=[])  # type: ignore[method-assign]
        adapter._parse_hath_options = Mock(  # type: ignore[method-assign]
            return_value=[{"method": "hath_org", "available": True}]
        )

        self.assertEqual(adapter._parse_options("<html></html>", "https://example.test"), [])
        adapter._parse_hath_options.assert_not_called()

    def test_unrecognized_response_retains_page_reason(self) -> None:
        adapter = ExternalAdapter({"exhentai": {"cookies": ""}})
        result = adapter._resolve_download_page(
            "<html><body>Custom archive failure explanation</body></html>",
            "https://example.test",
        )

        self.assertEqual(result["reason"], "unexpected_response")
        self.assertIn("Custom archive failure explanation", result["message"])


class ExternalQueueSafetyTests(unittest.TestCase):
    def test_manual_failure_is_not_automatically_resubmitted(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            db_path = root / "dashboard.db"
            database.init_db(db_path)
            with database.get_connection(db_path) as connection:
                connection.execute(
                    """
                    INSERT INTO items (id, title, item_url, local_status, external_method)
                    VALUES (?, ?, ?, 'force_external', 'archive_org')
                    """,
                    ("g-1", "Test Gallery", "https://exhentai.org/g/1/abc/",),
                )

            adapter = Mock()
            adapter.handle_item.return_value = {
                "success": False,
                "reason": "unexpected_response",
                "message": "Paid request response was ambiguous.",
                "method": "archive_org",
                "cost_gp": 20,
                "request_submitted": True,
            }
            config = {
                "paths": {
                    "media_library": str(root / "media"),
                    "archive_library": str(root / "archives"),
                }
            }

            external_manager.process_external_queue(db_path, config, adapter)

            item = database.get_item("g-1", db_path)
            self.assertIsNotNone(item)
            self.assertEqual(item["local_status"], "error")
            self.assertIn("unexpected_response", item["archive_last_auto_error"])
            self.assertEqual(external_manager.get_external_queue(db_path, config), [])


class PartialAliasCleanupTests(unittest.TestCase):
    def test_only_partial_hardlinks_with_completed_aliases_are_removed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            media = root / "media"
            archives = root / "archives"
            media.mkdir()
            archives.mkdir()

            completed = archives / "gallery.zip"
            completed.write_bytes(b"archive payload")
            archive_partial = archives / "gallery.part"
            media_partial = media / "g-[1] [archive] gallery.part"
            os.link(completed, archive_partial)
            os.link(completed, media_partial)
            incomplete = archives / "unfinished.part"
            incomplete.write_bytes(b"incomplete")

            actions = media_importer.cleanup_completed_partial_aliases(
                {
                    "paths": {
                        "media_library": str(media),
                        "archive_library": str(archives),
                    }
                }
            )

            self.assertEqual(len(actions), 2)
            self.assertTrue(completed.exists())
            self.assertFalse(archive_partial.exists())
            self.assertFalse(media_partial.exists())
            self.assertTrue(incomplete.exists())


if __name__ == "__main__":
    unittest.main()
