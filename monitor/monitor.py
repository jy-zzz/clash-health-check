#!/usr/bin/env python3
"""
Trojan proxy health monitor.

Spawns an ephemeral Mihomo instance, triggers health checks for all configured
trojan nodes, evaluates results, and POSTs to a webhook to restart the trojan
service when a node is unhealthy.

Usage:
    python3 monitor.py

Configuration via environment variables (or /opt/mihomo-monitor/secrets.env):
    MIHOMO_API_SECRET     Bearer token for Mihomo external controller API
    WEBHOOK_SECRET        Shared secret for the restart webhook
    TROJAN_SERVER_HOST    Hostname/IP of the trojan proxy server
    WEBHOOK_PORT          Port the webhook server listens on (default: 8765)
    DELAY_THRESHOLD_MS    Max acceptable delay in ms (default: 2000)
    MIHOMO_BINARY         Path to mihomo binary (default: /usr/local/bin/mihomo)
    WORKDIR               Working directory (default: /opt/mihomo-monitor)
    PROVIDER_NAME         Proxy provider name in config.yaml (default: trojan-nodes)
    MIHOMO_API_PORT       Mihomo external controller port (default: 19090)
"""

from __future__ import annotations

import atexit
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    mihomo_binary: str
    workdir: Path
    provider_name: str
    mihomo_api_port: int
    mihomo_api_secret: str
    webhook_secret: str
    trojan_server_host: str
    webhook_port: int
    delay_threshold_ms: int

    @property
    def mihomo_api_base(self) -> str:
        return f"http://127.0.0.1:{self.mihomo_api_port}"

    @property
    def webhook_url(self) -> str:
        return f"http://{self.trojan_server_host}:{self.webhook_port}/restart"

    @property
    def log_path(self) -> Path:
        return self.workdir / "monitor.log"

    @property
    def runtime_config_path(self) -> Path:
        return self.workdir / "config.yaml"

    @property
    def subscription_path(self) -> Path:
        return self.workdir / "trojan_subscription.yaml"


def _load_secrets_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE secrets file, ignoring comments and blank lines."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def load_config() -> Config:
    """Load configuration from environment variables, falling back to secrets.env."""
    workdir = Path(os.environ.get("WORKDIR", "/opt/mihomo-monitor"))
    secrets_file = workdir / "secrets.env"
    secrets = _load_secrets_file(secrets_file)

    def get(key: str, default: str | None = None) -> str:
        value = os.environ.get(key) or secrets.get(key) or default
        if value is None:
            raise RuntimeError(f"Required configuration '{key}' is not set. "
                               f"Set it in environment or {secrets_file}")
        return value

    return Config(
        mihomo_binary=get("MIHOMO_BINARY", "/usr/local/bin/mihomo"),
        workdir=workdir,
        provider_name=get("PROVIDER_NAME", "trojan-nodes"),
        mihomo_api_port=int(get("MIHOMO_API_PORT", "19090")),
        mihomo_api_secret=get("MIHOMO_API_SECRET"),
        webhook_secret=get("WEBHOOK_SECRET"),
        trojan_server_host=get("TROJAN_SERVER_HOST"),
        webhook_port=int(get("WEBHOOK_PORT", "8765")),
        delay_threshold_ms=int(get("DELAY_THRESHOLD_MS", "2000")),
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_path: Path) -> None:
    """Configure logging with WatchedFileHandler for logrotate compatibility."""
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # File handler — WatchedFileHandler detects inode changes after logrotate
    try:
        file_handler = logging.handlers.WatchedFileHandler(str(log_path))
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass  # Log path may not exist in test environments

    # stderr handler — captured by cron into the log file as well
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)


# ---------------------------------------------------------------------------
# Mihomo config generation
# ---------------------------------------------------------------------------

def write_runtime_config(config: Config) -> None:
    """Write a Mihomo config.yaml with the actual API secret substituted."""
    content = f"""\
# Runtime config generated by monitor.py — do not edit manually.
mode: direct
ipv6: true
allow-lan: false
external-controller: 127.0.0.1:{config.mihomo_api_port}
secret: "{config.mihomo_api_secret}"

proxy-providers:
  {config.provider_name}:
    type: file
    path: ./trojan_subscription.yaml
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
      timeout: 5000
      lazy: false

proxies: []
proxy-groups: []
rules: []
"""
    config.runtime_config_path.write_text(content)


# ---------------------------------------------------------------------------
# Mihomo process management
# ---------------------------------------------------------------------------

_mihomo_proc: Optional[subprocess.Popen] = None  # type: ignore[type-arg]


def _stop_mihomo() -> None:
    """Terminate the Mihomo subprocess. Safe to call multiple times."""
    global _mihomo_proc
    proc = _mihomo_proc
    if proc is None:
        return
    if proc.poll() is not None:
        logging.info("Mihomo pid=%d already exited (code=%d)", proc.pid, proc.poll())
        return
    logging.info("Terminating Mihomo pid=%d", proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logging.warning("Mihomo did not exit after SIGTERM; sending SIGKILL")
        proc.kill()
        proc.communicate()  # drain stderr pipe to avoid deadlock
    logging.info("Mihomo pid=%d terminated", proc.pid)


def _signal_handler(signum: int, frame: object) -> None:  # noqa: ARG001
    _stop_mihomo()
    sys.exit(0)


def start_mihomo(config: Config) -> subprocess.Popen:  # type: ignore[type-arg]
    """Spawn Mihomo and register cleanup handlers. Returns the Popen object."""
    global _mihomo_proc

    binary = config.mihomo_binary
    if not Path(binary).is_file():
        raise FileNotFoundError(f"Mihomo binary not found: {binary}")

    proc = subprocess.Popen(
        [binary, "-d", str(config.workdir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,  # new process group; prevents signal forwarding
    )
    _mihomo_proc = proc
    logging.info("Spawned Mihomo pid=%d", proc.pid)

    atexit.register(_stop_mihomo)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    return proc


def wait_for_ready(config: Config, proc: subprocess.Popen,  # type: ignore[type-arg]
                   timeout: float = 30.0, interval: float = 0.5) -> None:
    """Poll /version until Mihomo API is ready. Raises TimeoutError on deadline."""
    url = f"{config.mihomo_api_base}/version"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {config.mihomo_api_secret}"},
    )
    deadline = time.monotonic() + timeout
    start = time.monotonic()

    while time.monotonic() < deadline:
        # Check if process has already crashed
        if proc.poll() is not None:
            stderr_output = ""
            try:
                stderr_output = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            except Exception:
                pass
            raise RuntimeError(
                f"Mihomo exited unexpectedly (code={proc.returncode}): {stderr_output[:200]}"
            )
        try:
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status == 200:
                    elapsed = time.monotonic() - start
                    logging.info("Mihomo ready after %.1fs", elapsed)
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(interval)

    raise TimeoutError(f"Mihomo API not ready after {timeout}s")


# ---------------------------------------------------------------------------
# Health checker
# ---------------------------------------------------------------------------

@dataclass
class NodeResult:
    name: str
    alive: bool
    delay: int
    error: str = ""


def trigger_health_check(config: Config) -> None:
    """Tell Mihomo to run health checks for all nodes in the provider."""
    url = f"{config.mihomo_api_base}/providers/proxies/{config.provider_name}/healthcheck"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {config.mihomo_api_secret}"},
    )
    logging.info("Triggering health check for provider=%s", config.provider_name)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()  # drain body
    except (urllib.error.URLError, OSError) as exc:
        logging.warning("Health check trigger failed: %s", exc)
    # The endpoint returns immediately; checks run asynchronously
    logging.info("Waiting 5s for health checks to complete")
    time.sleep(5)


def fetch_results(config: Config) -> list[NodeResult]:
    """Fetch per-node health results from Mihomo."""
    url = f"{config.mihomo_api_base}/providers/proxies/{config.provider_name}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {config.mihomo_api_secret}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError) as exc:
        logging.error("Failed to fetch health results: %s", exc)
        return [NodeResult(name="unknown", alive=False, delay=0, error=str(exc))]
    except json.JSONDecodeError as exc:
        logging.error("Malformed JSON from Mihomo: %s", exc)
        return [NodeResult(name="unknown", alive=False, delay=0, error=str(exc))]

    results = []
    proxies = data.get("proxies", [])
    for proxy in proxies:
        name = proxy.get("name", "unknown")
        alive = bool(proxy.get("alive", False))
        # delay may be in top-level field or history list
        delay = int(proxy.get("delay", 0))
        if delay == 0 and proxy.get("history"):
            last = proxy["history"][-1]
            delay = int(last.get("delay", 0))
        results.append(NodeResult(name=name, alive=alive, delay=delay))

    logging.info("Fetched results for %d node(s)", len(results))
    return results


# ---------------------------------------------------------------------------
# Node evaluator
# ---------------------------------------------------------------------------

@dataclass
class EvalResult:
    node: NodeResult
    healthy: bool
    reason: str = ""


def evaluate_node(result: NodeResult, threshold_ms: int) -> EvalResult:
    """Classify a node as healthy or unhealthy."""
    if result.error:
        return EvalResult(node=result, healthy=False,
                          reason=f"collection failed: {result.error}")
    if not result.alive:
        return EvalResult(node=result, healthy=False, reason="node unreachable")
    if result.delay >= threshold_ms:
        return EvalResult(
            node=result, healthy=False,
            reason=f"delay {result.delay}ms >= threshold {threshold_ms}ms",
        )
    return EvalResult(node=result, healthy=True)


# ---------------------------------------------------------------------------
# Webhook client
# ---------------------------------------------------------------------------

def notify_webhook(config: Config) -> None:
    """POST to the trojan server webhook to trigger a service restart."""
    url = config.webhook_url
    logging.info("Dispatching webhook to %s", url)
    req = urllib.request.Request(
        url,
        method="POST",
        data=b"",
        headers={
            "Authorization": f"Bearer {config.webhook_secret}",
            "Content-Length": "0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            logging.info("Webhook response: %d", status)
    except urllib.error.HTTPError as exc:
        logging.error("Webhook returned HTTP %d", exc.code)
    except (urllib.error.URLError, OSError) as exc:
        logging.error("Webhook request failed: %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """Orchestrate the full health-check cycle. Returns exit code."""
    # Config and logging setup
    try:
        config = load_config()
    except RuntimeError as exc:
        # Logging not yet set up; print to stderr
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    setup_logging(config.log_path)
    logging.info("=== Monitor run started ===")

    # Write runtime Mihomo config with actual secret
    try:
        write_runtime_config(config)
    except OSError as exc:
        logging.error("Failed to write runtime config: %s", exc)
        return 1

    # Start Mihomo
    try:
        proc = start_mihomo(config)
    except FileNotFoundError as exc:
        logging.error("%s", exc)
        return 1

    # Wait for API readiness
    try:
        wait_for_ready(config, proc)
    except (TimeoutError, RuntimeError) as exc:
        logging.error("Mihomo startup failed: %s", exc)
        return 1  # atexit will clean up the process

    # Trigger health checks and fetch results
    trigger_health_check(config)
    results = fetch_results(config)

    # Evaluate nodes
    unhealthy: list[EvalResult] = []
    for result in results:
        ev = evaluate_node(result, config.delay_threshold_ms)
        if ev.healthy:
            logging.info("node=%s alive=%s delay=%dms → HEALTHY",
                         result.name, result.alive, result.delay)
        else:
            logging.warning("node=%s alive=%s delay=%dms → UNHEALTHY reason=%s",
                            result.name, result.alive, result.delay, ev.reason)
            unhealthy.append(ev)

    # Notify webhook if any nodes are unhealthy
    if unhealthy:
        logging.warning("%d unhealthy node(s); dispatching restart webhook", len(unhealthy))
        notify_webhook(config)
    else:
        logging.info("All %d node(s) healthy", len(results))

    logging.info("=== Monitor run complete ===")
    return 0  # atexit will terminate Mihomo


if __name__ == "__main__":
    sys.exit(main())
