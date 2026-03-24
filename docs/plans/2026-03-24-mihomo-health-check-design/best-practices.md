# Best Practices

## Subprocess Lifecycle (monitor.py)

### Starting Mihomo

Use `start_new_session=True` to put Mihomo in its own process group. This prevents the OS from forwarding the parent's signals (e.g., Ctrl+C) to Mihomo automatically — the monitor owns the lifecycle explicitly.

```python
proc = subprocess.Popen(
    ["mihomo", "-d", "/opt/mihomo-monitor"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
    start_new_session=True,  # new process group; prevents signal forwarding
)
```

For Python 3.11+, `process_group=0` is the equivalent cleaner form.

### Cleanup Pattern

Always terminate-then-kill with a grace period. Never call `wait()` on a process with piped streams — it can deadlock. Use `communicate()` after a timeout to drain pipes safely.

```python
def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return  # already exited; no action needed
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()  # drain stderr pipe to avoid deadlock
```

### Dual Registration: atexit + Signal Handlers

`atexit` handlers do not run on unhandled signals (SIGTERM, SIGKILL). Register both:

```python
import atexit, signal, sys

_proc: subprocess.Popen | None = None

def _cleanup() -> None:
    if _proc is not None:
        stop(_proc)

atexit.register(_cleanup)

def _signal_handler(signum: int, frame: object) -> None:
    _cleanup()
    sys.exit(0)

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)
```

Set `_proc` immediately after `Popen` succeeds so cleanup never misses a live process.

---

## Readiness Polling

Use `time.monotonic()` (not `time.time()`) for the deadline — immune to clock adjustments.

```python
import time, urllib.request, urllib.error

def wait_for_ready(url: str, token: str, timeout: float = 30.0, interval: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(interval)
    raise TimeoutError(f"Mihomo API not ready after {timeout}s")
```

Recommended values:
- `connect_timeout`: 1.0s (fail fast if port not yet open)
- `poll_interval`: 0.5s (responsive without hammering)
- `total_timeout`: 30s (ample for Mihomo startup)

---

## Health Check Timing

The `/providers/proxies/{name}/healthcheck` endpoint is **fire-and-return** — it does not block until checks complete. After triggering, wait before fetching results:

```python
# Trigger
urllib.request.urlopen(healthcheck_url, ...).read()

# Wait for async checks to complete
time.sleep(5)

# Fetch results
with urllib.request.urlopen(results_url, ...) as resp:
    data = json.load(resp)
```

Alternatively, poll the results endpoint until all `history` arrays are non-empty, with a deadline.

---

## Webhook Authentication (Server Side)

### Timing-Safe Comparison

Never compare tokens with `==`. Use `hmac.compare_digest()` to prevent timing attacks:

```python
import hmac, os

def _authenticate(self) -> bool:
    auth = self.headers.get("Authorization", "")
    # Accept case-insensitive "bearer" scheme prefix
    if not auth.lower().startswith("bearer "):
        return False
    presented = auth[7:]  # strip "Bearer "
    expected = os.environ.get("WEBHOOK_TOKEN", "")
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())
```

### Request Validation Order

Check in this order to avoid leaking information:
1. Method == POST (→ 405 otherwise)
2. Path == /restart (→ 404 otherwise)
3. Authorization header present and valid (→ 401 otherwise)
4. Execute `systemctl restart trojan`

### subprocess.run in Handler

```python
result = subprocess.run(
    ["systemctl", "restart", "trojan"],  # never shell=True
    capture_output=True,
    timeout=30,   # prevent handler from blocking indefinitely
    check=False,  # handle non-zero exit manually; don't raise
)
if result.returncode != 0:
    # log result.stderr.decode() and return 500
```

---

## Secret Management

### Never hardcode

Secrets are read at runtime from environment variables or `0600` files:

```python
import os

def load_secret(env_var: str, path: str | None = None) -> str:
    value = os.environ.get(env_var)
    if value:
        return value
    if path and os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    raise RuntimeError(f"Secret {env_var} not configured")
```

### File permissions

```bash
chmod 600 /opt/mihomo-monitor/secrets.env
chmod 600 /etc/clash-health-check/webhook.env
chown root:root /etc/clash-health-check/webhook.env
```

### Never log tokens

Log the webhook URL but strip the token from any log output. Use `****` placeholders in debug logs if needed.

---

## Logging

### Use WatchedFileHandler

`WatchedFileHandler` automatically detects inode changes (caused by logrotate's `create` directive) and reopens the file. This gives zero-loss rotation without signal coordination.

```python
import logging, logging.handlers

def setup_logging(log_path: str) -> None:
    handler = logging.handlers.WatchedFileHandler(log_path)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
```

### Log structure

Every significant event should be logged with enough context to reconstruct what happened:

```
2026-03-24T10:00:01 INFO     Spawned Mihomo pid=12345
2026-03-24T10:00:03 INFO     Mihomo ready after 1.8s
2026-03-24T10:00:03 INFO     Triggered health check for provider=trojan-nodes
2026-03-24T10:00:08 INFO     node=node-sg-01 alive=True delay=142ms → HEALTHY
2026-03-24T10:00:08 WARNING  node=node-hk-01 alive=False delay=0ms → UNHEALTHY reason=unreachable
2026-03-24T10:00:08 INFO     Dispatching webhook to http://10.0.0.1:8765/restart
2026-03-24T10:00:09 INFO     Webhook response: 200 OK
2026-03-24T10:00:09 INFO     Terminated Mihomo pid=12345
```

---

## Error Handling

### Infrastructure errors (exit non-zero)

- Binary not found → log + exit 1
- Mihomo fails to start (non-zero exit) → log stderr + exit 1
- Mihomo never becomes ready (timeout) → log + kill + exit 1

### Transient errors (log, continue, exit 0)

- Health check API returns 5xx → treat node as unhealthy, proceed
- Webhook returns non-200 → log the status, do not retry, do not crash
- Webhook connection error → log the exception, do not crash

### Pattern

```python
try:
    result = fetch_health_check(...)
except Exception as exc:
    logging.error("Health check failed: %s", exc)
    result = HealthResult(alive=False, delay=0, error=str(exc))
# evaluation always runs, even on failed collection
```

---

## Security Hardening Summary

| Concern | Mitigation |
|---------|-----------|
| Unauthorized webhook calls | `hmac.compare_digest()` token validation; 401 on mismatch |
| Token timing leak | `hmac.compare_digest()` is constant-time |
| Token in logs | Never log the raw token value; use `****` if needed |
| Port exposure | Mihomo binds `127.0.0.1` only; webhook server can bind `0.0.0.0` but should be firewalled |
| Root privilege escalation | `NoNewPrivileges=yes` in systemd unit; `shell=False` in subprocess calls |
| Trojan config in VCS | Gitignore `secrets.env`, `webhook.env`, and `trojan_subscription.yaml` |
| Method abuse | Webhook server returns 405 for non-POST, 404 for unknown paths |
| Slow-loris | Set `timeout=` on all urllib calls; `close_connection=True` in handler |
| Stray Mihomo processes | Dual cleanup registration (atexit + signal handlers) |

---

## Mihomo-Specific Gotchas

1. **`health-check.timeout` is in milliseconds** — not seconds. `5000` means 5 seconds.
2. **`/healthcheck` is async** — always sleep or poll after triggering before reading results.
3. **`lazy: false`** — set this explicitly; default `true` skips checks when no proxy group is actively routing through the provider.
4. **`mode: direct`** — essential. Without this, Mihomo tries to route its own health-check traffic via proxy groups (which don't exist in our config).
5. **Omit all inbound ports** — even setting `port: 0` may cause Mihomo to attempt binding. Simply omit `port`, `socks-port`, `mixed-port`, and related keys entirely.
6. **Provider `path` is relative to the `-d` working directory** — use `./trojan_subscription.yaml`, not an absolute path, to keep the config portable.
