import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from update_compose_runtime import update_compose  # noqa: E402


class ComposeUpdateTests(unittest.TestCase):
    def test_update_preserves_yaml_lines_and_pins_runtime(self):
        source = """version: \"3.9\"
services:
  driparr:
    image: ghcr.io/sander1384/driparr:latest
    environment:
      TZ: Europe/Amsterdam
      DRIPARR_SESSION_SECRET:
    volumes:
      - /data:/app/data
"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "docker-compose.yml"
            path.write_text(source, encoding="utf-8")
            update_compose(path, "ghcr.io/sander1384/driparr:v0.2.0", "a" * 64)
            updated = path.read_text(encoding="utf-8")
        self.assertIn("image: ghcr.io/sander1384/driparr:v0.2.0\n", updated)
        self.assertIn('DRIPARR_SESSION_SECRET: "' + ("a" * 64) + '"\n', updated)
        self.assertIn("    environment:\n", updated)
        self.assertIn("    volumes:\n", updated)
        self.assertEqual(len(updated.splitlines()), len(source.splitlines()))


if __name__ == "__main__":
    unittest.main()
