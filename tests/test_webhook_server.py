"""Unit tests for webhook/webhook_server.py."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Allow importing webhook_server from parent directory's webhook/ subfolder
sys.path.insert(0, str(Path(__file__).parent.parent / "webhook"))
import webhook_server  # noqa: E402


def _make_handler(method: str = "POST", path: str = "/restart",
                  auth: str | None = "Bearer test-token") -> webhook_server.RestartHandler:
    """
    Create a RestartHandler instance with a mocked socket/request.
    Bypasses the real HTTP server setup.
    """
    # Minimal fake request and client address
    mock_request = MagicMock()
    mock_request.makefile.return_value = io.BytesIO(b"")
    client_address = ("127.0.0.1", 12345)
    mock_server = MagicMock()

    # Build raw HTTP request bytes so BaseHTTPRequestHandler can parse them
    raw = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n"
    if auth is not None:
        raw += f"Authorization: {auth}\r\n"
    raw += "\r\n"

    mock_request.makefile.return_value = io.BytesIO(raw.encode())

    handler = webhook_server.RestartHandler.__new__(webhook_server.RestartHandler)
    handler.rfile = io.BytesIO(b"")
    handler.wfile = io.BytesIO()
    handler.server = mock_server
    handler.client_address = client_address
    handler.command = method
    handler.path = path
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.close_connection = True

    # Parse headers from raw
    import http.client
    raw_msg = raw.encode()
    handler.headers = http.client.parse_headers(io.BytesIO(raw_msg.split(b"\r\n", 1)[1]))

    return handler


class TestAuthentication(unittest.TestCase):
    """Feature 7: Webhook server authentication via Bearer token."""

    def setUp(self):
        webhook_server.RestartHandler._token = "test-token"

    def test_valid_token_accepted(self):
        handler = _make_handler(auth="Bearer test-token")
        self.assertTrue(handler._authenticate())

    def test_missing_authorization_header_rejected(self):
        handler = _make_handler(auth=None)
        self.assertFalse(handler._authenticate())

    def test_no_bearer_prefix_rejected(self):
        handler = _make_handler(auth="test-token")
        self.assertFalse(handler._authenticate())

    def test_wrong_token_rejected(self):
        handler = _make_handler(auth="Bearer wrong-token")
        self.assertFalse(handler._authenticate())

    def test_empty_token_rejected(self):
        handler = _make_handler(auth="Bearer ")
        self.assertFalse(handler._authenticate())

    def test_token_comparison_is_case_sensitive(self):
        handler = _make_handler(auth="Bearer TEST-TOKEN")
        self.assertFalse(handler._authenticate())

    def test_bearer_prefix_is_case_insensitive(self):
        handler = _make_handler(auth="bearer test-token")
        self.assertTrue(handler._authenticate())

    def test_bearer_prefix_uppercase_accepted(self):
        handler = _make_handler(auth="BEARER test-token")
        self.assertTrue(handler._authenticate())

    def test_token_never_logged(self):
        handler = _make_handler(auth="Bearer wrong-token")
        with self.assertLogs(level="WARNING") as log_ctx:
            handler._authenticate()
        for line in log_ctx.output:
            self.assertNotIn("wrong-token", line,
                             f"Token value found in log line: {line}")


class TestRestartTrojan(unittest.TestCase):
    """Feature 8 & 9: systemctl restart trojan execution."""

    def setUp(self):
        webhook_server.RestartHandler._token = "test-token"

    def test_successful_restart_returns_true(self):
        handler = _make_handler()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = b""
        with patch("subprocess.run", return_value=mock_result):
            success, msg = handler._restart_trojan()
        self.assertTrue(success)
        self.assertEqual(msg, "ok")

    def test_systemctl_uses_list_not_shell(self):
        handler = _make_handler()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = b""
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            handler._restart_trojan()
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        self.assertIsInstance(cmd, list)
        self.assertNotIn("shell", str(call_args[1]))
        # shell=True must not be passed
        self.assertFalse(call_args[1].get("shell", False))

    def test_nonzero_exit_code_returns_false(self):
        handler = _make_handler()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = b"Unit trojan.service not found."
        with patch("subprocess.run", return_value=mock_result):
            success, msg = handler._restart_trojan()
        self.assertFalse(success)
        self.assertIn("1", msg)

    def test_systemctl_not_found_returns_false(self):
        handler = _make_handler()
        with patch("subprocess.run", side_effect=FileNotFoundError("systemctl")):
            success, msg = handler._restart_trojan()
        self.assertFalse(success)
        self.assertIn("not found", msg)

    def test_timeout_returns_false(self):
        handler = _make_handler()
        mock_proc = MagicMock()
        exc = subprocess.TimeoutExpired(cmd="systemctl", timeout=30)
        exc.process = mock_proc
        with patch("subprocess.run", side_effect=exc):
            success, msg = handler._restart_trojan()
        self.assertFalse(success)
        self.assertIn("timed out", msg.lower())


class TestRequestRouting(unittest.TestCase):
    """Feature 9: HTTP method and path routing."""

    def setUp(self):
        webhook_server.RestartHandler._token = "test-token"

    def _get_response_status(self, handler: webhook_server.RestartHandler) -> int:
        """Extract the HTTP status code written to wfile."""
        response_bytes = handler.wfile.getvalue()
        first_line = response_bytes.decode(errors="replace").split("\r\n")[0]
        # e.g. "HTTP/1.0 200 OK"
        parts = first_line.split()
        return int(parts[1]) if len(parts) >= 2 else 0

    def test_get_request_returns_405(self):
        handler = _make_handler(method="GET")
        handler.do_GET()
        status = self._get_response_status(handler)
        self.assertEqual(status, 405)

    def test_put_request_returns_405(self):
        handler = _make_handler(method="PUT")
        handler.do_PUT()
        status = self._get_response_status(handler)
        self.assertEqual(status, 405)

    def test_delete_request_returns_405(self):
        handler = _make_handler(method="DELETE")
        handler.do_DELETE()
        status = self._get_response_status(handler)
        self.assertEqual(status, 405)

    def test_unknown_path_returns_404(self):
        handler = _make_handler(path="/unknown")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = b""
        with patch("subprocess.run", return_value=mock_result):
            handler.do_POST()
        status = self._get_response_status(handler)
        self.assertEqual(status, 404)

    def test_valid_post_to_restart_returns_200(self):
        handler = _make_handler(method="POST", path="/restart", auth="Bearer test-token")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = b""
        with patch("subprocess.run", return_value=mock_result):
            handler.do_POST()
        status = self._get_response_status(handler)
        self.assertEqual(status, 200)

    def test_invalid_token_returns_401(self):
        handler = _make_handler(method="POST", path="/restart", auth="Bearer bad-token")
        handler.do_POST()
        status = self._get_response_status(handler)
        self.assertEqual(status, 401)

    def test_systemctl_failure_returns_500(self):
        handler = _make_handler(method="POST", path="/restart", auth="Bearer test-token")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = b"error"
        with patch("subprocess.run", return_value=mock_result):
            handler.do_POST()
        status = self._get_response_status(handler)
        self.assertEqual(status, 500)

    def test_server_continues_after_failure(self):
        """Server should not crash after a systemctl failure."""
        handler = _make_handler(method="POST", path="/restart", auth="Bearer test-token")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = b"error"
        with patch("subprocess.run", return_value=mock_result):
            handler.do_POST()  # first request fails

        # Second request should work fine
        handler2 = _make_handler(method="POST", path="/restart", auth="Bearer test-token")
        mock_result2 = MagicMock()
        mock_result2.returncode = 0
        mock_result2.stderr = b""
        with patch("subprocess.run", return_value=mock_result2):
            handler2.do_POST()
        status = self._get_response_status(handler2)
        self.assertEqual(status, 200)


class TestLoadToken(unittest.TestCase):
    """Tests for load_token config loading."""

    def setUp(self):
        os.environ.pop("WEBHOOK_TOKEN", None)
        os.environ.pop("WEBHOOK_PORT", None)

    def test_raises_when_token_not_set(self):
        with self.assertRaises(RuntimeError):
            webhook_server.load_token()

    def test_loads_token_from_env(self):
        os.environ["WEBHOOK_TOKEN"] = "my-token"
        self.assertEqual(webhook_server.load_token(), "my-token")

    def test_default_port_is_8765(self):
        self.assertEqual(webhook_server.load_port(), 8765)

    def test_port_from_env(self):
        os.environ["WEBHOOK_PORT"] = "9999"
        self.assertEqual(webhook_server.load_port(), 9999)

    def tearDown(self):
        os.environ.pop("WEBHOOK_TOKEN", None)
        os.environ.pop("WEBHOOK_PORT", None)


if __name__ == "__main__":
    unittest.main()
