import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "web_app.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("driparr_web_app", MODULE_PATH)
web_app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(web_app)


class WorkerResilienceTests(unittest.TestCase):
    def setUp(self):
        web_app.WORKER_STATE.update(
            {
                "alive": False,
                "lastHeartbeat": None,
                "lastError": "",
                "lastErrorAt": None,
                "consecutiveFailures": 0,
                "recoveredAt": None,
            }
        )

    def test_service_request_converts_socket_timeout_to_runtime_error(self):
        service = {"url": "http://radarr:7878", "apiKey": "test"}
        with (
            patch.object(web_app, "urlopen", side_effect=TimeoutError("timed out")) as mocked_urlopen,
            patch.object(web_app.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Not reachable after 3 attempts: timed out"):
                web_app.service_request(service, "GET", "/system/status")
        self.assertEqual(mocked_urlopen.call_count, 3)

    def test_worker_retries_after_unexpected_error(self):
        config = {"app": {"workerEnabled": True, "dripMode": "sync"}}
        attempts = []

        def process_once():
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise TimeoutError("temporary Radarr timeout")

        with (
            patch.object(web_app, "ensure_data"),
            patch.object(web_app, "read_config", return_value=config),
            patch.object(web_app, "get_store", return_value=Mock(backup_if_due=Mock())),
            patch.object(web_app, "process_once", side_effect=process_once),
            patch.object(web_app, "push_event"),
            patch.object(web_app, "push_liveblog"),
            patch.object(web_app.traceback, "print_exc"),
        ):
            web_app.worker_loop(max_cycles=2, sleep_fn=lambda _seconds: None)

        self.assertEqual(attempts, [1, 2])
        self.assertEqual(web_app.WORKER_STATE["consecutiveFailures"], 0)
        self.assertIsNotNone(web_app.WORKER_STATE["recoveredAt"])


if __name__ == "__main__":
    unittest.main()
