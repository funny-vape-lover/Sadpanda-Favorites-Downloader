from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import database
import external_manager
import media_importer
import requests
from external_adapter import ExternalAdapter


class ArchiveOptionSafetyTests(unittest.TestCase):
    def test_hath_options_remain_available_as_queue_methods(self) -> None:
        adapter = ExternalAdapter({"exhentai": {"cookies": ""}})
        html = """
        <form id="hathdl_form">
          <table><tr><td>
            <p><a onclick="return do_hathdl('org')">Original</a></p>
            <p>10 MiB</p><p>Free</p>
          </td></tr></table>
        </form>
        """
        options = adapter._parse_options(html, "https://example.test/archiver.php")
        self.assertEqual(options[0]["method"], "hath_org")

    def test_hath_queue_acknowledgement_is_recognized(self) -> None:
        adapter = ExternalAdapter({"exhentai": {"cookies": ""}})
        result = adapter._resolve_hath_queue_response(
            "<p>An original resolution download has been queued for client 12345.</p>"
        )
        self.assertTrue(result["success"])

    @patch("external_adapter.time.sleep")
    def test_safe_initial_get_retries_transient_disconnects(self, sleep_mock: Mock) -> None:
        adapter = ExternalAdapter({"exhentai": {"cookies": ""}})
        response = Mock()
        response.text = "<html></html>"
        response.raise_for_status.return_value = None
        adapter.session.get = Mock(
            side_effect=[
                requests.ConnectionError("remote disconnected"),
                requests.ConnectionError("remote disconnected"),
                response,
            ]
        )

        result = adapter.inspect_item(
            {"id": "g-1", "item_url": "https://exhentai.org/g/1/abc/"}
        )

        self.assertTrue(result["success"])
        self.assertEqual(adapter.session.get.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_unrecognized_response_retains_page_reason(self) -> None:
        adapter = ExternalAdapter({"exhentai": {"cookies": ""}})
        result = adapter._resolve_download_page(
            "<html><body>Custom archive failure explanation</body></html>",
            "https://example.test",
        )

        self.assertEqual(result["reason"], "unexpected_response")
        self.assertIn("Custom archive failure explanation", result["message"])


class ExternalQueueSafetyTests(unittest.TestCase):
    def test_retry_backoff_is_capped(self) -> None:
        self.assertEqual(
            [external_manager._archive_retry_delay(attempt) for attempt in range(1, 5)],
            [30, 120, 600, 600],
        )

    def test_transient_manual_failure_stays_queued_with_backoff(self) -> None:
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
            self.assertEqual(item["local_status"], "force_external")
            self.assertEqual(item["archive_retry_count"], 1)
            self.assertGreater(item["archive_retry_after_epoch"], 0)
            self.assertIn("unexpected_response", item["archive_last_auto_error"])
            self.assertEqual(external_manager.get_external_queue(db_path, config), [])

    def test_nonretryable_failure_becomes_terminal_error(self) -> None:
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
                    ("g-2", "Unavailable Gallery", "https://exhentai.org/g/2/abc/"),
                )

            adapter = Mock()
            adapter.handle_item.return_value = {
                "success": False,
                "reason": "no_eligible_method",
                "message": "No direct archive option is available.",
            }
            config = {
                "paths": {
                    "media_library": str(root / "media"),
                    "archive_library": str(root / "archives"),
                }
            }

            external_manager.process_external_queue(db_path, config, adapter)

            item = database.get_item("g-2", db_path)
            self.assertEqual(item["local_status"], "error")
            self.assertEqual(item["archive_retry_count"], 0)

    def test_retry_delay_is_enforced_when_archive_fallback_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            db_path = root / "dashboard.db"
            database.init_db(db_path)
            database.set_setting(
                external_manager.FALLBACK_SETTING_KEY,
                "true",
                db_path,
            )
            with database.get_connection(db_path) as connection:
                connection.execute(
                    """
                    INSERT INTO items (
                        id,
                        title,
                        item_url,
                        local_status,
                        external_method,
                        archive_retry_after_epoch
                    )
                    VALUES (?, ?, ?, 'force_external', 'archive_org', ?)
                    """,
                    (
                        "g-3",
                        "Delayed Gallery",
                        "https://exhentai.org/g/3/abc/",
                        4_102_444_800,
                    ),
                )

            config = {
                "paths": {
                    "media_library": str(root / "media"),
                    "archive_library": str(root / "archives"),
                }
            }

            self.assertEqual(external_manager.get_external_queue(db_path, config), [])

            with database.get_connection(db_path) as connection:
                connection.execute(
                    "UPDATE items SET archive_retry_after_epoch = 0 WHERE id = ?",
                    ("g-3",),
                )
            queue = external_manager.get_external_queue(db_path, config)
            self.assertEqual([item["id"] for item in queue], ["g-3"])

    def test_hath_queue_waits_for_client_then_imports_completed_directory(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            db_path = root / "dashboard.db"
            media = root / "media"
            archives = root / "archives"
            hath = root / "hath"
            database.init_db(db_path)
            with database.get_connection(db_path) as connection:
                connection.execute(
                    """
                    INSERT INTO items (id, title, item_url, local_status, external_method)
                    VALUES (?, ?, ?, 'force_external', 'hath_org')
                    """,
                    ("g-911853", "H@H Gallery", "https://exhentai.org/g/911853/abc/"),
                )

            adapter = Mock()
            adapter.handle_item.return_value = {
                "success": True,
                "queued_hath": True,
                "method": "hath_org",
                "label": "H@H Original",
                "size_text": "10 MiB",
                "cost_gp": 0,
                "request_submitted": True,
            }
            config = {
                "paths": {
                    "media_library": str(media),
                    "archive_library": str(archives),
                    "hath_downloads": str(hath),
                }
            }

            external_manager.process_external_queue(db_path, config, adapter)
            queued_item = database.get_item("g-911853", db_path)
            self.assertEqual(queued_item["local_status"], "queued_hath")
            self.assertFalse(queued_item["archive_downloaded"])
            self.assertEqual(external_manager.get_external_queue(db_path, config), [])

            completed = hath / "H@H Gallery [911853]"
            completed.mkdir(parents=True)
            (completed / "001.jpg").write_bytes(b"image")
            (completed / "galleryinfo.txt").write_text("GID 911853", encoding="utf-8")

            reconciled = external_manager.reconcile_hath_downloads(db_path, config)
            self.assertEqual(len(reconciled), 1)
            media_importer.import_completed_archives(db_path, config)

            completed_item = database.get_item("g-911853", db_path)
            self.assertTrue(completed_item["archive_downloaded"])
            self.assertEqual(completed_item["local_status"], "completed")
            imported_dirs = [
                path
                for path in media.iterdir()
                if path.is_dir() and path.name.startswith("g-[911853] [archive] ")
            ]
            self.assertEqual(len(imported_dirs), 1)
            self.assertTrue((imported_dirs[0] / "galleryinfo.txt").exists())


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
