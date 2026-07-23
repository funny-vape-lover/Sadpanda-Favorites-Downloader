from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import database
from search_utils import exclude_items_by_tags, extract_negative_tag_terms


class TagSearchExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.items = [
            self._item("g-1", "Clean", '["female:office", "artist:test"]'),
            self._item("g-2", "Teacher", '["female:office", "teacher"]'),
            self._item("g-3", "Human", '["male:human", "artist:test"]'),
            self._item("g-4", "Big", '["female:big breasts", "artist:test"]'),
        ]

    def test_multiple_negative_tags_are_combined(self) -> None:
        excluded, remaining = extract_negative_tag_terms("-tag:teacher -tag:male:human")
        filtered = exclude_items_by_tags(self.items, excluded)
        self.assertEqual(remaining, "")
        self.assertEqual([item["id"] for item in filtered], ["g-1", "g-4"])

    def test_positive_and_negative_tag_search_can_be_combined(self) -> None:
        excluded, remaining = extract_negative_tag_terms("tag:female:office -tag:teacher")
        filtered = exclude_items_by_tags(self.items, excluded)
        self.assertEqual(remaining, "tag:female:office")
        self.assertEqual([item["id"] for item in filtered], ["g-1", "g-3", "g-4"])

    def test_quoted_negative_tag_supports_spaces(self) -> None:
        excluded, remaining = extract_negative_tag_terms('-tag:"female:big breasts"')
        filtered = exclude_items_by_tags(self.items, excluded)
        self.assertEqual(remaining, "")
        self.assertEqual([item["id"] for item in filtered], ["g-1", "g-2", "g-3"])

    @staticmethod
    def _item(item_id: str, title: str, tags: str) -> dict[str, object]:
        return {
            "id": item_id,
            "title": title,
            "title_jpn": "",
            "tags": tags,
            "is_latest_revision": True,
            "torrent_from_archive": False,
            "site_category": "doujinshi",
        }


class TagRuleExclusionTests(unittest.TestCase):
    def test_exclusion_is_persisted_and_applied_on_create_and_update(self) -> None:
        # SQLite WAL handles can linger briefly on Windows after connection teardown.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "rules.db"
            database.init_db(db_path)
            rule_id = database.save_rule_set(
                {
                    "name": "Exclude teacher",
                    "is_active": True,
                    "auto_download": True,
                    "tags_contains": "female:office",
                    "tags_not_contains": "teacher",
                    "notes_contains": "keep",
                    "notes_not_contains": "skip",
                },
                db_path,
            )

            active = database.get_active_rule_sets(db_path)
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["tags_not_contains"], "teacher")
            self.assertEqual(active[0]["notes_contains"], "keep")
            self.assertEqual(active[0]["notes_not_contains"], "skip")
            self.assertTrue(
                database.item_matches_rule_set(
                    {"tags": '["female:office"]', "favorite_note": "keep this"},
                    active[0],
                )
            )
            self.assertFalse(
                database.item_matches_rule_set(
                    {"tags": '["female:office", "teacher"]', "favorite_note": "keep this"},
                    active[0],
                )
            )

            database.save_rule_set(
                {
                    **active[0],
                    "rule_set_id": rule_id,
                    "tags_not_contains": "male:human",
                    "notes_contains": "updated",
                },
                db_path,
            )
            updated = database.get_all_rule_sets(db_path)[0]
            self.assertEqual(updated["tags_not_contains"], "male:human")
            self.assertEqual(updated["notes_contains"], "updated")


if __name__ == "__main__":
    unittest.main()
