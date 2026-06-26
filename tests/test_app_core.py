import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import web_app  # noqa: E402


class AppCoreTests(unittest.TestCase):
    def test_movie_tmdb_id_uses_cache_without_network_lookup(self):
        item = {"externalId": "imdb:tt1234567", "tmdbId": "42"}
        with patch.object(web_app, "resolve_tmdb_from_imdb") as lookup:
            self.assertEqual(web_app.movie_tmdb_id({}, item), "42")
        lookup.assert_not_called()

    def test_movie_tmdb_id_populates_cache_once(self):
        item = {"externalId": "imdb:tt1234567", "tmdbId": ""}
        with patch.object(web_app, "resolve_tmdb_from_imdb", return_value="99") as lookup:
            self.assertEqual(web_app.movie_tmdb_id({}, item), "99")
            self.assertEqual(web_app.movie_tmdb_id({}, item), "99")
        self.assertEqual(lookup.call_count, 1)

    def test_transient_error_schedules_exponential_retry(self):
        item = {"status": "todo", "retryCount": "1"}
        config = {"app": {"maxWorkerRetries": 5, "retryBaseSeconds": 30, "retryMaxSeconds": 900}}
        self.assertTrue(web_app.schedule_item_retry(item, config, "timed out"))
        self.assertEqual(item["retryCount"], "2")
        self.assertIn("retry 2/5 in 60s", item["error"])
        self.assertFalse(web_app.retry_is_due(item))

    def test_retry_due_handles_past_timestamp(self):
        item = {"nextRetryAt": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
        self.assertTrue(web_app.retry_is_due(item))

    def test_post_requests_are_not_retried(self):
        service = {"url": "http://radarr:7878", "apiKey": "test"}
        with patch.object(web_app, "urlopen", side_effect=TimeoutError("timed out")) as request:
            with self.assertRaisesRegex(RuntimeError, "after 1 attempts"):
                web_app.service_request(service, "POST", "/movie", {"tmdbId": 1})
        self.assertEqual(request.call_count, 1)

    def test_security_rejects_default_secrets(self):
        config = {"app": {"adminUsername": "", "adminPasswordHash": "", "adminPasswordSalt": ""}}
        with patch.dict(web_app.os.environ, {"DRIPARR_SESSION_SECRET": "replace-me", "DRIPARR_ALLOW_INSECURE_DEFAULTS": ""}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "SESSION_SECRET"):
                web_app.validate_startup_security(config)

    def test_csrf_token_is_session_bound(self):
        with patch.dict(web_app.os.environ, {"DRIPARR_SESSION_SECRET": "strong-test-secret"}, clear=False):
            self.assertEqual(web_app.csrf_token_for_session("a"), web_app.csrf_token_for_session("a"))
            self.assertNotEqual(web_app.csrf_token_for_session("a"), web_app.csrf_token_for_session("b"))

    def test_csv_upload_xhr_sends_csrf_header(self):
        app_html = web_app.page(web_app.default_config(), [], csrf_token="test-csrf-token")
        self.assertIn("xhr.open('POST', '/api/import-csv');", app_html)
        self.assertIn("xhr.setRequestHeader('X-Driparr-CSRF', DRIPARR_CSRF_TOKEN);", app_html)

    def test_movie_root_folder_is_stored_per_queue_item(self):
        config = web_app.default_config()
        with patch.object(web_app, "build_radarr_movie_index", return_value={"imdb": set(), "tmdb": set(), "title_year": set()}):
            with patch.object(web_app, "write_queue") as write_queue:
                with patch.object(web_app, "read_queue", return_value=[]):
                    web_app.enqueue_ids(config, "movie", "imdb", ["tt1234567"], "Animation", "/Disney")
        self.assertEqual(write_queue.call_args.args[0][0]["rootFolderPath"], "/Disney")

    def test_radarr_payload_uses_item_root_folder(self):
        config = web_app.default_config()
        item = {"externalId": "imdb:tt1234567", "tmdbId": "1", "rootFolderPath": "/Disney"}
        self.assertEqual(web_app.radarr_payload(config, item)["rootFolderPath"], "/Disney")

    def test_selected_movie_root_must_be_known_to_radarr(self):
        config = web_app.default_config()
        with patch.object(web_app, "radarr_root_folders", return_value=["/movies", "/Disney"]):
            self.assertEqual(web_app.selected_movie_root_folder(config, "/Disney"), "/Disney")
            with self.assertRaisesRegex(RuntimeError, "not an accessible"):
                web_app.selected_movie_root_folder(config, "/unknown")

    def test_stale_grab_without_live_queue_is_released(self):
        item = {
            "externalId": "imdb:tt1234567",
            "tmdbId": "42",
            "radarrMovieId": "7",
            "addedAt": (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),
        }
        movie = {"id": 7, "tmdbId": 42, "hasFile": False, "monitored": True, "status": "released"}
        with patch.dict(web_app.os.environ, {"DRIPARR_STALE_GRAB_SKIP_SECONDS": "600"}, clear=False):
            with patch.object(web_app, "service_request", return_value=[movie]):
                with patch.object(web_app, "radarr_queue_records", return_value=[]):
                    with patch.object(web_app, "radarr_history_records", return_value=[{"eventType": "grabbed", "movieId": 7}]):
                        result = web_app.radarr_movie_status({"radarr": {}}, item)
        self.assertEqual(result["state"], "skipped_no_indexer")
        self.assertIn("automatisch vrijgegeven", result["reason"])

    def test_recent_grab_without_live_queue_stays_active_briefly(self):
        item = {
            "externalId": "imdb:tt1234567",
            "tmdbId": "42",
            "radarrMovieId": "7",
            "addedAt": datetime.now(timezone.utc).isoformat(),
        }
        movie = {"id": 7, "tmdbId": 42, "hasFile": False, "monitored": True, "status": "released"}
        with patch.dict(web_app.os.environ, {"DRIPARR_STALE_GRAB_SKIP_SECONDS": "600"}, clear=False):
            with patch.object(web_app, "service_request", return_value=[movie]):
                with patch.object(web_app, "radarr_queue_records", return_value=[]):
                    with patch.object(web_app, "radarr_history_records", return_value=[{"eventType": "grabbed", "movieId": 7}]):
                        result = web_app.radarr_movie_status({"radarr": {}}, item)
        self.assertEqual(result["state"], "active")


if __name__ == "__main__":
    unittest.main()
