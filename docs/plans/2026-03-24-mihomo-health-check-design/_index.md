# Design: Trojan Proxy Health Monitoring with Mihomo

**Date:** 2026-03-24
**Status:** Approved — ready for implementation

---

## Context

You operate multiple trojan proxy servers to bypass China's firewall. The monitoring host is a separate Ubuntu machine. The goal is a simple, self-contained system that:

- Periodically checks whether each trojan node is reachable and responsive
- Automatically triggers a trojan service restart on the proxy server if the node fails
- Does not interfere with the monitoring host's network or firewall

Mihomo (formerly Clash.Meta) is used as the health-check engine because it natively speaks the trojan protocol, removing the need to build a trojan client from scratch.

---

## Requirements

### Infrastructure

1. The monitor runs on an Ubuntu host separate from the trojan proxy server.
2. The Mihomo binary lives at `/usr/local/bin/mihomo`.
3. The working directory `/opt/mihomo-monitor` contains all config and log files.
4. A cron job triggers the monitoring script every 10 minutes.

### Mihomo Instance

5. Each cron invocation spawns a fresh Mihomo process; no instance persists between runs.
6. The Mihomo config sets `mode: direct`, exposes no inbound proxy ports, and enables the external controller on `127.0.0.1:19090`. Port `19090` is used instead of the default `9090` to avoid conflicts with any other Mihomo/Clash instance on any host.
7. The Mihomo config references a `proxy-providers` entry pointing at `trojan_subscription.yaml`.
8. The Mihomo process is unconditionally terminated (SIGTERM then SIGKILL) after results are collected, regardless of success or failure.

### Monitoring Script

9. Written in Python using only the standard library (no third-party dependencies at runtime).
10. Waits for the Mihomo external controller to become available before issuing requests.
11. Queries the Mihomo API (authenticated with a shared secret) to retrieve health-check results.
12. A node is **healthy** if `alive=true`, regardless of delay. A node is **unhealthy** only if `alive=false` or result collection failed. Delay is logged for observability only.
13. Logs every run result (node name, status, delay) with ISO 8601 timestamps.
14. Sends a webhook POST to the trojan server when the node is unhealthy.
15. Exits non-zero only on infrastructure failures (binary not found, Mihomo never starts, config missing).

### Webhook — Monitor Side

16. POST to `http://<trojan-server>:<port>/restart`.
17. Sends `Authorization: Bearer <shared-secret>` header.
18. The shared secret is never hardcoded; it is read from an environment variable or a `0600` secrets file.
19. The webhook call has a configured timeout and never blocks indefinitely.

### Webhook Server — Trojan Server Side

20. Written in Python using only `http.server` from the standard library.
21. Listens on a configurable port; responds to `POST /restart`.
22. Requests without a valid `Authorization: Bearer <token>` header receive HTTP 401; no action taken.
23. Uses `hmac.compare_digest()` (not `==`) for token comparison to prevent timing attacks.
24. On a valid authenticated request, runs `subprocess.run(["systemctl", "restart", "trojan"])`.
25. Runs as a systemd service; auto-restarts on failure.
26. Runs as root (required to invoke `systemctl restart trojan`).

### Security

27. Mihomo external controller binds only to `127.0.0.1`.
28. The webhook shared secret is never stored in version control.
29. The webhook server rejects all HTTP methods except POST on `/restart`.
30. Bearer tokens are never written to any log output.

---

## Rationale

**Why Mihomo?**
Building a trojan health-checker from scratch requires implementing the trojan TLS handshake, authentication, and CONNECT tunneling. Mihomo already does this. Reusing it reduces implementation scope to configuration and a few API calls.

**Why ephemeral (not persistent)?**
An ephemeral instance guarantees port 9090 is only open for ~15 seconds per run. No persistent service means no orphaned ports, no systemd unit to maintain on the monitor host, and simpler reasoning about state.

**Why Python stdlib only?**
The tool runs as a cron job on an Ubuntu host. Requiring no pip install keeps deployment to a simple `scp` of a single script. The webhook server has equally minimal deployment needs on the trojan server.

**Why webhook instead of SSH?**
A webhook is a narrow, auditable interface. The trojan server exposes a single HTTP endpoint with token auth. SSH would grant much broader access from the monitoring host.

---

## Detailed Design

### Component Layout

```
Monitor host (/opt/mihomo-monitor/)
├── config.yaml                # Mihomo config: mode direct, provider, API on 127.0.0.1:9090
├── trojan_subscription.yaml   # Trojan node definitions (type: trojan entries)
├── monitor.py                 # Main Python monitoring script
├── monitor.log                # Rolling timestamped log
└── secrets.env                # MIHOMO_API_SECRET and WEBHOOK_SECRET (chmod 600)

Monitor host (system)
├── /usr/local/bin/mihomo      # Mihomo binary
└── /etc/cron.d/mihomo-monitor # Cron schedule: */10 * * * *

Trojan proxy server
├── webhook_server.py          # Python stdlib HTTP server
├── /etc/systemd/system/clash-webhook.service
└── /etc/clash-health-check/webhook.env  # WEBHOOK_TOKEN, WEBHOOK_PORT (chmod 600)
```

### Data Flow

```
1.  CRON FIRES (every 10 minutes)
    └─ executes monitor.py

2.  SPAWN MIHOMO
    └─ Popen(["mihomo", "-d", "/opt/mihomo-monitor"], start_new_session=True)
    └─ atexit + SIGTERM/SIGINT handlers registered immediately

3.  WAIT FOR API READINESS
    └─ Poll GET http://127.0.0.1:9090/version with Authorization header
    └─ deadline = time.monotonic() + 30s, poll every 0.5s
    └─ On timeout: log error, kill Mihomo, exit 1

4.  TRIGGER HEALTH CHECK
    └─ GET /providers/proxies/{provider-name}/healthcheck
    └─ Wait 5s for async checks to complete
    └─ GET /providers/proxies/{provider-name} → per-node {alive, delay, history}

5.  EVALUATE NODES
    └─ healthy: alive=true (delay logged but not used as failure criterion)
    └─ unhealthy: alive=false OR result collection failed

6.  LOG RESULTS
    └─ ISO 8601 timestamp + node name + alive + delay + classification

7.  IF ANY NODE UNHEALTHY → DISPATCH WEBHOOK
    └─ POST http://<trojan-server>:<port>/restart
    └─ Authorization: Bearer <WEBHOOK_SECRET>
    └─ Log HTTP response code

8.  KILL MIHOMO (via atexit / cleanup function)
    └─ proc.terminate() → wait(5s) → proc.kill() if still alive
    └─ Port 19090 is now closed

--- on trojan server ---

9.  WEBHOOK RECEIVED
    └─ Validate method == POST, path == /restart
    └─ Validate Authorization header via hmac.compare_digest()
    └─ On mismatch: return 401, log rejection, no action

10. RESTART TROJAN
    └─ subprocess.run(["systemctl", "restart", "trojan"], timeout=30)
    └─ Return 200 on success, 500 on failure
    └─ Log action with timestamp and client IP
```

### Mihomo config.yaml (minimal)

```yaml
mode: direct
ipv6: true
allow-lan: false
external-controller: 127.0.0.1:19090  # non-default port; avoids conflict with any existing Clash/Mihomo instance
secret: "${MIHOMO_API_SECRET}"  # injected by monitor.py at runtime

proxy-providers:
  trojan-nodes:
    type: file
    path: ./trojan_subscription.yaml
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
      timeout: 5000        # milliseconds
      lazy: false

proxies: []
proxy-groups: []
rules: []
```

### Key Configuration Values

| Value | Location | Notes |
|---|---|---|
| `MIHOMO_API_SECRET` | `secrets.env` on monitor host | Bearer token for Mihomo API |
| `WEBHOOK_SECRET` | `secrets.env` (monitor) + `webhook.env` (trojan server) | Must match on both sides |
| `TROJAN_SERVER_HOST` | `monitor.py` config constant or env var | IP/hostname of trojan server |
| `WEBHOOK_PORT` | Both hosts | Port the webhook server listens on |
| Provider name in `config.yaml` | `config.yaml` | Must match the name used in API calls |
| Cron schedule | `/etc/cron.d/mihomo-monitor` | `*/10 * * * *` |

---

## Out of Scope (v1)

- Prometheus metrics or any time-series telemetry
- Web UI or history dashboard
- Per-node granular alerting (webhook fires only when the monitored node fails, not partial failures)
- Email, Slack, or any alerting channel beyond the single webhook
- TLS/HTTPS for the webhook endpoint
- Multi-server monitoring (single trojan server target)
- Dynamic subscription updates from a remote URL
- Rate limiting on the webhook server

---

## Design Documents

- [BDD Specifications](./bdd-specifications.feature) — 62 Gherkin scenarios across 10 features
- [Architecture](./architecture.md) — Component details, file structure, systemd units, Mihomo config
- [Best Practices](./best-practices.md) — Security, subprocess lifecycle, error handling, logging
