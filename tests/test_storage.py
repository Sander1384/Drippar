import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from storage import LEGACY_QUEUE_FIELDS, SQLiteStore  # noqa: E402


class StorageTests(unittest.TestCase):
    def default_config(self):
        return {"schemaVersion": 2, "app": {"workerEnabled": False}, "radarr": {}, "lists": []}

    def test_legacy_files_migrate_without_losing_queue_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = {"app": {"workerEnabled": True}, "radarr": {"url": "http://radarr"}}
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            with (root / "queue.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["type", "externalId", "title", "status", "source", "addedAt", "error"],
                )
                writer.writeheader()
                writer.writerow({"type": "movie", "externalId": "imdb:tt1", "title": "One", "status": "active", "source": "List", "addedAt": "now", "error": ""})
                writer.writerow({"type": "movie", "externalId": "imdb:tt2", "title": "Two", "status": "todo", "source": "List", "addedAt": "", "error": ""})

            store = SQLiteStore(root, self.default_config)
            store.initialize()

            self.assertTrue((root / "driparr.db").exists())
            self.assertEqual(store.read_config(), config)
            rows = store.read_queue()
            self.assertEqual([row["title"] for row in rows], ["One", "Two"])
            self.assertEqual(rows[0]["tmdbId"], "")
            self.assertTrue(any((root / "backups").glob("pre-sqlite-*/queue.csv")))
            with (root / "queue.csv").open("r", encoding="utf-8") as handle:
                self.assertEqual(next(csv.reader(handle)), LEGACY_QUEUE_FIELDS)

    def test_transactional_round_trip_preserves_extended_state(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteStore(folder, self.default_config)
            row = {
                "type": "movie",
                "externalId": "imdb:tt1234567",
                "title": "Cached",
                "status": "active",
                "source": "Test",
                "addedAt": "2026-01-01T00:00:00+00:00",
                "error": "",
                "tmdbId": "42",
                "radarrMovieId": "7",
                "retryCount": "2",
                "nextRetryAt": "later",
                "stateChangedAt": "now",
                "lastCheckedAt": "now",
            }
            store.write_queue([row])
            loaded = store.read_queue()[0]
            self.assertEqual(loaded, row)
            self.assertEqual(store.health()["queueCount"], 1)

    def test_duplicate_legacy_rows_are_preserved_for_safe_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteStore(folder, self.default_config)
            row = {"type": "movie", "externalId": "imdb:tt1", "title": "Same", "status": "todo", "source": "List"}
            store.write_queue([row, row])
            self.assertEqual(len(store.read_queue()), 2)

    def test_database_backup_is_created_and_rate_limited(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteStore(folder, self.default_config)
            store.write_queue([])
            first = store.backup_if_due(interval_hours=24, retention=2)
            second = store.backup_if_due(interval_hours=24, retention=2)
            self.assertIsNotNone(first)
            self.assertTrue(first.exists())
            self.assertIsNone(second)


if __name__ == "__main__":
    unittest.main()
