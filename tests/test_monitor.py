"""Unit tests for monitor/monitor.py."""
from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Allow importing monitor from parent directory's monitor/ subfolder
sys.path.insert(0, str(Path(__file__).parent.parent / "monitor"))
import monitor  # noqa: E402


class TestLoadConfig(unittest.TestCase):
    """Tests for config loading from env and secrets file."""

    def setUp(self):
        # Clear any relevant env vars before each test
        for key in ["MIHOMO_API_SECRET", "WEBHOOK_SECRET", "TROJAN_SERVER_HOST",
                    "WEBHOOK_PORT", "DELAY_THRESHOLD_MS", "WORKDIR",
                    "MIHOMO_BINARY", "PROVIDER_NAME", "MIHOMO_API_PORT"]:
            os.environ.pop(key, None)

    def test_loads_from_environment_variables(self):
        os.environ["MIHOMO_API_SECRET"] = "api-secret"
        os.environ["WEBHOOK_SECRET"] = "wh-secret"
        os.environ["TROJAN_SERVER_HOST"] = "10.0.0.1"
        os.environ["WORKDIR"] = "/tmp"
        cfg = monitor.load_config()
        self.assertEqual(cfg.mihomo_api_secret, "api-secret")
        self.assertEqual(cfg.webhook_secret, "wh-secret")
        self.assertEqual(cfg.trojan_server_host, "10.0.0.1")

    def test_raises_when_required_key_missing(self):
        # No env vars set, no secrets file at default path
        os.environ["WORKDIR"] = "/tmp/nonexistent_workdir_abc123"
        with self.assertRaises(RuntimeError):
            monitor.load_config()

    def test_loads_from_secrets_file(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            secrets = Path(tmpdir) / "secrets.env"
            secrets.write_text(
                "MIHOMO_API_SECRET=file-secret\n"
                "WEBHOOK_SECRET=file-wh\n"
                "TROJAN_SERVER_HOST=192.168.1.1\n"
            )
            os.environ["WORKDIR"] = tmpdir
            cfg = monitor.load_config()
            self.assertEqual(cfg.mihomo_api_secret, "file-secret")
            self.assertEqual(cfg.webhook_secret, "file-wh")

    def test_default_values_applied(self):
        os.environ["MIHOMO_API_SECRET"] = "s"
        os.environ["WEBHOOK_SECRET"] = "s"
        os.environ["TROJAN_SERVER_HOST"] = "h"
        os.environ["WORKDIR"] = "/tmp"
        cfg = monitor.load_config()
        self.assertEqual(cfg.mihomo_api_port, 19090)
        self.assertEqual(cfg.webhook_port, 8765)
        self.assertEqual(cfg.delay_threshold_ms, 2000)
        self.assertEqual(cfg.mihomo_binary, "/usr/local/bin/mihomo")
        self.assertEqual(cfg.provider_name, "trojan-nodes")


class TestMihomoProcess(unittest.TestCase):
    """Tests for MihomoProcess start/stop/ready lifecycle."""

    def _make_config(self):
        import tempfile
        tmpdir = tempfile.mkdtemp()
        return monitor.Config(
            mihomo_binary="/usr/local/bin/mihomo",
            workdir=Path(tmpdir),
            provider_name="trojan-nodes",
            mihomo_api_port=19090,
            mihomo_api_secret="test-secret",
            webhook_secret="wh-secret",
            trojan_server_host="10.0.0.1",
            webhook_port=8765,
            delay_threshold_ms=2000,
        )

    def setUp(self):
        # Reset module-level process reference before each test
        monitor._mihomo_proc = None

    def test_start_uses_new_session(self):
        cfg = self._make_config()
        # Create a fake binary
        binary = cfg.workdir / "mihomo"
        binary.write_text("#!/bin/sh\nsleep 60\n")
        binary.chmod(0o755)
        cfg = monitor.Config(
            mihomo_binary=str(binary),
            workdir=cfg.workdir,
            provider_name=cfg.provider_name,
            mihomo_api_port=cfg.mihomo_api_port,
            mihomo_api_secret=cfg.mihomo_api_secret,
            webhook_secret=cfg.webhook_secret,
            trojan_server_host=cfg.trojan_server_host,
            webhook_port=cfg.webhook_port,
            delay_threshold_ms=cfg.delay_threshold_ms,
        )
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_popen.return_value = mock_proc
            monitor.start_mihomo(cfg)
            call_kwargs = mock_popen.call_args[1]
            self.assertTrue(call_kwargs.get("start_new_session"))

    def test_start_raises_if_binary_missing(self):
        cfg = self._make_config()
        cfg2 = monitor.Config(
            mihomo_binary="/nonexistent/mihomo",
            workdir=cfg.workdir,
            provider_name=cfg.provider_name,
            mihomo_api_port=cfg.mihomo_api_port,
            mihomo_api_secret=cfg.mihomo_api_secret,
            webhook_secret=cfg.webhook_secret,
            trojan_server_host=cfg.trojan_server_host,
            webhook_port=cfg.webhook_port,
            delay_threshold_ms=cfg.delay_threshold_ms,
        )
        with self.assertRaises(FileNotFoundError):
            monitor.start_mihomo(cfg2)

    def test_stop_is_idempotent_when_process_already_exited(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # already exited
        monitor._mihomo_proc = mock_proc
        monitor._stop_mihomo()  # must not raise
        mock_proc.terminate.assert_not_called()

    def test_stop_terminates_then_kills_on_timeout(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="mihomo", timeout=5)
        monitor._mihomo_proc = mock_proc
        monitor._stop_mihomo()
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()
        mock_proc.communicate.assert_called_once()

    def test_stop_does_not_kill_if_terminate_succeeds(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0  # terminates cleanly
        monitor._mihomo_proc = mock_proc
        monitor._stop_mihomo()
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_not_called()

    def test_wait_for_ready_succeeds_on_200(self):
        cfg = self._make_config()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200

        with patch("urllib.request.urlopen", return_value=mock_resp):
            # Should not raise
            monitor.wait_for_ready(cfg, mock_proc, timeout=5, interval=0.01)

    def test_wait_for_ready_retries_on_connection_refused(self):
        cfg = self._make_config()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        import urllib.error
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200

        call_count = {"n": 0}
        def urlopen_side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise urllib.error.URLError("connection refused")
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=urlopen_side_effect):
            monitor.wait_for_ready(cfg, mock_proc, timeout=5, interval=0.01)
        self.assertGreaterEqual(call_count["n"], 3)

    def test_wait_for_ready_raises_timeout(self):
        cfg = self._make_config()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(TimeoutError):
                monitor.wait_for_ready(cfg, mock_proc, timeout=0.1, interval=0.01)

    def test_wait_for_ready_raises_if_process_crashes(self):
        cfg = self._make_config()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # crashed
        mock_proc.returncode = 1
        mock_proc.stderr = io.BytesIO(b"fatal error")

        with self.assertRaises(RuntimeError):
            monitor.wait_for_ready(cfg, mock_proc, timeout=5, interval=0.01)


class TestHealthChecker(unittest.TestCase):
    """Tests for trigger_health_check and fetch_results."""

    def _make_config(self):
        import tempfile
        return monitor.Config(
            mihomo_binary="/usr/local/bin/mihomo",
            workdir=Path(tempfile.mkdtemp()),
            provider_name="trojan-nodes",
            mihomo_api_port=19090,
            mihomo_api_secret="test-secret",
            webhook_secret="wh-secret",
            trojan_server_host="10.0.0.1",
            webhook_port=8765,
            delay_threshold_ms=2000,
        )

    def test_fetch_results_parses_alive_true(self):
        cfg = self._make_config()
        payload = {"proxies": [{"name": "sg-01", "alive": True, "delay": 150}]}
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps(payload).encode()

        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = mock_resp
            # Patch json.load to work with our mock
            with patch("json.load", return_value=payload):
                results = monitor.fetch_results(cfg)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "sg-01")
        self.assertTrue(results[0].alive)
        self.assertEqual(results[0].delay, 150)

    def test_fetch_results_parses_alive_false(self):
        cfg = self._make_config()
        payload = {"proxies": [{"name": "hk-01", "alive": False, "delay": 0}]}
        with patch("urllib.request.urlopen"), patch("json.load", return_value=payload):
            results = monitor.fetch_results(cfg)
        self.assertFalse(results[0].alive)
        self.assertEqual(results[0].delay, 0)

    def test_fetch_results_treats_http_error_as_unhealthy(self):
        import urllib.error
        cfg = self._make_config()
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("err")):
            results = monitor.fetch_results(cfg)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].alive)
        self.assertNotEqual(results[0].error, "")

    def test_fetch_results_treats_json_error_as_unhealthy(self):
        cfg = self._make_config()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch("json.load", side_effect=json.JSONDecodeError("err", "", 0)):
                results = monitor.fetch_results(cfg)
        self.assertFalse(results[0].alive)
        self.assertNotEqual(results[0].error, "")

    def test_trigger_sends_get_to_healthcheck_endpoint(self):
        cfg = self._make_config()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b""
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            with patch("time.sleep"):  # Skip the 5s wait
                monitor.trigger_health_check(cfg)
        url_used = mock_open.call_args[0][0].full_url
        self.assertIn("/providers/proxies/trojan-nodes/healthcheck", url_used)


class TestNodeEvaluator(unittest.TestCase):
    """Tests for evaluate_node — healthy/unhealthy classification.

    A node is healthy if alive=True, regardless of delay.
    Delay is logged for observability but never used as a failure criterion.
    """

    def _result(self, alive, delay, error=""):
        return monitor.NodeResult(name="test-node", alive=alive, delay=delay, error=error)

    def test_healthy_when_alive_regardless_of_delay(self):
        ev = monitor.evaluate_node(self._result(True, 150))
        self.assertTrue(ev.healthy)

    def test_healthy_when_alive_with_very_high_delay(self):
        # High delay is NOT a failure — only alive=False matters
        ev = monitor.evaluate_node(self._result(True, 9999))
        self.assertTrue(ev.healthy)

    def test_unhealthy_when_alive_false(self):
        ev = monitor.evaluate_node(self._result(False, 0))
        self.assertFalse(ev.healthy)
        self.assertIn("unreachable", ev.reason)

    def test_unhealthy_when_alive_false_despite_low_delay(self):
        ev = monitor.evaluate_node(self._result(False, 100))
        self.assertFalse(ev.healthy)
        self.assertIn("unreachable", ev.reason)

    def test_unhealthy_when_collection_failed(self):
        ev = monitor.evaluate_node(self._result(False, 0, error="connection refused"))
        self.assertFalse(ev.healthy)
        self.assertIn("collection failed", ev.reason)


class TestWebhookClient(unittest.TestCase):
    """Tests for notify_webhook."""

    def _make_config(self):
        import tempfile
        return monitor.Config(
            mihomo_binary="/usr/local/bin/mihomo",
            workdir=Path(tempfile.mkdtemp()),
            provider_name="trojan-nodes",
            mihomo_api_port=19090,
            mihomo_api_secret="api-secret",
            webhook_secret="wh-secret-value",
            trojan_server_host="10.0.0.1",
            webhook_port=8765,
            delay_threshold_ms=2000,
        )

    def test_sends_post_with_bearer_token(self):
        cfg = self._make_config()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            monitor.notify_webhook(cfg)

        req = mock_open.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        auth_header = req.get_header("Authorization")
        self.assertEqual(auth_header, "Bearer wh-secret-value")

    def test_logs_success_on_200(self):
        cfg = self._make_config()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with self.assertLogs(level="INFO") as log_ctx:
                monitor.notify_webhook(cfg)
        self.assertTrue(any("200" in line for line in log_ctx.output))

    def test_logs_error_on_401(self):
        import urllib.error
        cfg = self._make_config()
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError(url="", code=401, msg="", hdrs=None, fp=None)):
            with self.assertLogs(level="WARNING") as log_ctx:
                monitor.notify_webhook(cfg)
        self.assertTrue(any("401" in line for line in log_ctx.output))

    def test_handles_connection_error_without_crash(self):
        import urllib.error
        cfg = self._make_config()
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            # Must not raise
            monitor.notify_webhook(cfg)

    def test_token_never_logged(self):
        cfg = self._make_config()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with self.assertLogs(level="DEBUG") as log_ctx:
                monitor.notify_webhook(cfg)

        for line in log_ctx.output:
            self.assertNotIn("wh-secret-value", line,
                             f"Token found in log line: {line}")


if __name__ == "__main__":
    unittest.main()
