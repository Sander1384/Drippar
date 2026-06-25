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


if __name__ == "__main__":
    unittest.main()
