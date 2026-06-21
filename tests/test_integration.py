import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mock_radarr  # noqa: E402
import web_app  # noqa: E402
from storage import SQLiteStore  # noqa: E402


class MockRadarrIntegrationTests(unittest.TestCase):
    def setUp(self):
        mock_radarr.MOVIES.clear()
        mock_radarr.DOWNLOADS.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), mock_radarr.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.previous_store = web_app.STORE
        web_app.STORE = SQLiteStore(self.temp.name, web_app.default_config)
        config = web_app.default_config()
        config["app"].update({"setupComplete": True, "workerEnabled": True, "dripMode": "sync"})
        config["radarr"].update(
            {
                "enabled": True,
                "url": f"http://127.0.0.1:{self.server.server_port}",
                "apiKey": "mock",
                "qualityProfileId": 1,
                "rootFolderPath": "/movies",
                "searchOnAdd": True,
            }
        )
        web_app.save_config(config)
        web_app.write_queue(
            [
                {
                    "type": "movie",
                    "externalId": "imdb:tt0137523",
                    "title": "Fight Club",
                    "status": "todo",
                    "source": "Integration",
                    "addedAt": "",
                    "error": "",
                }
            ]
        )

    def tearDown(self):
        web_app.STORE = self.previous_store
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def test_full_queue_item_lifecycle_uses_cached_ids(self):
        self.assertTrue(web_app.process_once())
        active = web_app.read_queue()[0]
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["tmdbId"], "550")
        self.assertEqual(active["radarrMovieId"], "1")

        mock_radarr.MOVIES[0]["hasFile"] = True
        mock_radarr.MOVIES[0]["movieFile"] = {"id": 1}
        mock_radarr.DOWNLOADS.clear()

        self.assertTrue(web_app.process_once())
        completed = web_app.read_queue()[0]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["tmdbId"], "550")
        self.assertEqual(completed["radarrMovieId"], "1")


if __name__ == "__main__":
    unittest.main()
