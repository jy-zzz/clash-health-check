#!/usr/bin/env python3
"""
Webhook server for the Clash health check system.

Runs on the trojan proxy server. Accepts POST /restart requests with a
Bearer token and executes `systemctl restart trojan`.

Deploy to: /opt/clash-health-check/webhook_server.py
Run as: systemd service (see deploy/systemd/clash-webhook.service)

Configuration via environment variables (loaded by systemd from EnvironmentFile):
    WEBHOOK_TOKEN    Required. Shared bearer token (must match monitor's WEBHOOK_SECRET).
    WEBHOOK_PORT     Port to listen on (default: 8765).
"""

from __future__ import annotations

import hmac
import http.server
import logging
import logging.handlers
import os
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOG_PATH = Path("/var/log/clash-health-check/webhook.log")


def setup_logging() -> None:
    """Configure structured logging to file and stderr."""
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.WatchedFileHandler(str(_LOG_PATH))
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass  # Log directory may not exist; stderr fallback is sufficient

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_token() -> str:
    """Load the webhook bearer token from environment. Fails if not set."""
    token = os.environ.get("WEBHOOK_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "WEBHOOK_TOKEN environment variable is not set. "
            "Set it in /etc/clash-health-check/webhook.env"
        )
    return token


def load_port() -> int:
    """Load the webhook server port from environment."""
    return int(os.environ.get("WEBHOOK_PORT", "8765"))


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class RestartHandler(http.server.BaseHTTPRequestHandler):
    """Handle POST /restart — authenticate, restart trojan, respond."""

    # Injected by main() before server starts
    _token: str = ""

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/restart":
            self._send(404, "Not Found")
            return
        if not self._authenticate():
            self._send(401, "Unauthorized")
            return
        success, message = self._restart_trojan()
        if success:
            logging.info("Trojan restarted successfully (client=%s)", self.client_address[0])
            self._send(200, "OK")
        else:
            logging.error("Trojan restart failed: %s (client=%s)", message, self.client_address[0])
            self._send(500, f"Internal Server Error: {message}")

    def do_GET(self) -> None:  # noqa: N802
        self._send(405, "Method Not Allowed")

    def do_PUT(self) -> None:  # noqa: N802
        self._send(405, "Method Not Allowed")

    def do_DELETE(self) -> None:  # noqa: N802
        self._send(405, "Method Not Allowed")

    def do_PATCH(self) -> None:  # noqa: N802
        self._send(405, "Method Not Allowed")

    def do_HEAD(self) -> None:  # noqa: N802
        self._send(405, "Method Not Allowed")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _authenticate(self) -> bool:
        """Validate Authorization: Bearer <token> header. Timing-safe."""
        auth = self.headers.get("Authorization", "")
        # Case-insensitive "bearer " prefix check
        if not auth.lower().startswith("bearer "):
            logging.warning("Auth rejected: missing or malformed Authorization header (client=%s)",
                            self.client_address[0])
            return False
        presented = auth[len("bearer "):].strip()
        expected = self.__class__._token
        if not presented or not expected:
            logging.warning("Auth rejected: empty token (client=%s)", self.client_address[0])
            return False
        # hmac.compare_digest is constant-time — prevents timing attacks
        result = hmac.compare_digest(presented.encode(), expected.encode())
        if not result:
            logging.warning("Auth rejected: wrong token (client=%s)", self.client_address[0])
        return result

    # ------------------------------------------------------------------
    # Trojan restart
    # ------------------------------------------------------------------

    def _restart_trojan(self) -> tuple[bool, str]:
        """Run systemctl restart trojan. Returns (success, message)."""
        try:
            result = subprocess.run(
                ["systemctl", "restart", "trojan"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError:
            msg = "systemctl binary not found"
            logging.error(msg)
            return False, msg
        except subprocess.TimeoutExpired as exc:
            msg = "systemctl restart timed out after 30s"
            logging.error(msg)
            if exc.process:
                try:
                    exc.process.kill()
                except Exception:
                    pass
            return False, msg

        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            msg = f"systemctl exited with code {result.returncode}"
            logging.error("%s: %s", msg, stderr[:200])
            return False, msg

        return True, "ok"

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _send(self, status: int, body: str) -> None:
        """Send a plain-text HTTP response and close the connection."""
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    # ------------------------------------------------------------------
    # Logging override
    # ------------------------------------------------------------------

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Override default BaseHTTPRequestHandler logging to use Python logging."""
        logging.info("HTTP %s %s", self.client_address[0], format % args)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()

    try:
        token = load_token()
    except RuntimeError as exc:
        logging.error("%s", exc)
        sys.exit(1)

    port = load_port()

    # Inject token into handler class before server starts
    RestartHandler._token = token

    server = http.server.HTTPServer(("0.0.0.0", port), RestartHandler)
    logging.info("Webhook server listening on port %d", port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Webhook server stopped")
        server.server_close()


if __name__ == "__main__":
    main()
