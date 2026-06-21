import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from state_machine import transition_item  # noqa: E402


class StateMachineTests(unittest.TestCase):
    def test_valid_transition_records_metadata(self):
        item = {"status": "todo", "retryCount": "0", "error": "old"}
        transition_item(item, "active", now="2026-01-01T00:00:00+00:00")
        self.assertEqual(item["status"], "active")
        self.assertEqual(item["error"], "")
        self.assertEqual(item["stateChangedAt"], "2026-01-01T00:00:00+00:00")

    def test_invalid_transition_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "completed -> active"):
            transition_item({"status": "completed"}, "active")

    def test_failure_increments_retry_counter(self):
        item = {"status": "todo", "retryCount": "2"}
        transition_item(item, "failed", "boom")
        self.assertEqual(item["retryCount"], "3")


if __name__ == "__main__":
    unittest.main()
