# clash-health-check

Monitors trojan proxy nodes using an ephemeral [Mihomo](https://github.com/MetaCubeX/mihomo) instance and automatically restarts the trojan service when a node is unhealthy.

## How it works

```
Monitor host (Ubuntu, cron)          Trojan proxy server
┌──────────────────────────┐         ┌───────────────────────────┐
│  cron → monitor.py       │         │  clash-webhook.service    │
│    └─ mihomo (ephemeral) │──POST──▶│    └─ webhook_server.py   │
│         health checks    │         │         systemctl restart  │
└──────────────────────────┘         └───────────────────────────┘
```

Every 10 minutes:
1. `monitor.py` spawns a fresh Mihomo process (port 19090, never persists)
2. Triggers health checks for all trojan nodes via Mihomo's REST API
3. Evaluates each node: **healthy** if `alive=true` and `delay < threshold`
4. If any node is unhealthy → POSTs to the trojan server webhook to restart the service
5. Kills Mihomo and exits cleanly

No inbound proxy ports are opened. The monitor host's network is never affected.

## Requirements

**Monitor host:**
- Ubuntu (or any Linux with cron)
- Python 3.8+
- [Mihomo binary](https://github.com/MetaCubeX/mihomo/releases) at `/usr/local/bin/mihomo`

**Trojan proxy server:**
- Python 3.8+
- `systemctl` available (standard on systemd systems)
- Root access (to restart the trojan service)

## Setup: Monitor host

### 1. Install Mihomo

```bash
curl -L https://github.com/MetaCubeX/mihomo/releases/latest/download/mihomo-linux-amd64.gz \
  | gunzip > /usr/local/bin/mihomo
chmod +x /usr/local/bin/mihomo
```

### 2. Create working directory

```bash
mkdir -p /opt/mihomo-monitor
```

### 3. Configure Mihomo

```bash
cp config/config.yaml.example /opt/mihomo-monitor/config.yaml
# Edit config.yaml: set secret: to match MIHOMO_API_SECRET below
```

### 4. Define your trojan nodes

```bash
cp config/trojan_subscription.yaml.example /opt/mihomo-monitor/trojan_subscription.yaml
# Edit trojan_subscription.yaml: fill in your real server/port/password/sni
```

### 5. Configure secrets

```bash
cp config/secrets.env.example /opt/mihomo-monitor/secrets.env
# Edit secrets.env: fill in all CHANGE_ME values
chmod 600 /opt/mihomo-monitor/secrets.env
```

### 6. Deploy monitor script

```bash
cp monitor/monitor.py /opt/mihomo-monitor/
chmod +x /opt/mihomo-monitor/monitor.py
```

### 7. Install cron job

```bash
cp deploy/cron.d/mihomo-monitor /etc/cron.d/
chmod 644 /etc/cron.d/mihomo-monitor
```

### 8. Install log rotation

```bash
cp deploy/logrotate.d/mihomo-monitor /etc/logrotate.d/
```

### 9. Test manually

```bash
python3 /opt/mihomo-monitor/monitor.py
tail -f /opt/mihomo-monitor/monitor.log
```

## Setup: Trojan server

### 1. Deploy webhook server

```bash
mkdir -p /opt/clash-health-check
cp webhook/webhook_server.py /opt/clash-health-check/
```

### 2. Configure secrets

```bash
mkdir -p /etc/clash-health-check
cp config/webhook.env.example /etc/clash-health-check/webhook.env
# Edit webhook.env: set WEBHOOK_TOKEN to same value as WEBHOOK_SECRET on monitor host
chmod 600 /etc/clash-health-check/webhook.env
chown root:root /etc/clash-health-check/webhook.env
```

### 3. Create log directory

```bash
mkdir -p /var/log/clash-health-check
chmod 750 /var/log/clash-health-check
```

### 4. Install and enable systemd service

```bash
cp deploy/systemd/clash-webhook.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now clash-webhook.service
```

### 5. Test the webhook

```bash
# Replace <token> with your WEBHOOK_TOKEN value
curl -X POST -H "Authorization: Bearer <token>" http://localhost:8765/restart
# Expected: 200 OK
```

## Configuration reference

| Variable | Host | Required | Default | Description |
|---|---|---|---|---|
| `MIHOMO_API_SECRET` | Monitor | Yes | — | Bearer token for Mihomo API |
| `WEBHOOK_SECRET` | Monitor | Yes | — | Shared secret for webhook (must match `WEBHOOK_TOKEN`) |
| `TROJAN_SERVER_HOST` | Monitor | Yes | — | IP/hostname of trojan server |
| `WEBHOOK_PORT` | Both | No | `8765` | Port the webhook server listens on |
| `DELAY_THRESHOLD_MS` | Monitor | No | `2000` | Max acceptable delay in ms |
| `MIHOMO_BINARY` | Monitor | No | `/usr/local/bin/mihomo` | Path to Mihomo binary |
| `WORKDIR` | Monitor | No | `/opt/mihomo-monitor` | Working directory |
| `PROVIDER_NAME` | Monitor | No | `trojan-nodes` | Proxy provider name in config.yaml |
| `MIHOMO_API_PORT` | Monitor | No | `19090` | Mihomo external controller port |
| `WEBHOOK_TOKEN` | Trojan server | Yes | — | Bearer token validated by webhook server |

## Security

- `WEBHOOK_TOKEN` / `WEBHOOK_SECRET` are never stored in version control (see `.gitignore`)
- Mihomo API binds to `127.0.0.1` only — not exposed to the network
- No inbound proxy ports (`port`, `socks-port`, `mixed-port`, `tun`) are configured
- Token comparison uses `hmac.compare_digest()` to prevent timing attacks
- Bearer tokens are never written to any log file

## Logs

**Monitor host:** `/opt/mihomo-monitor/monitor.log`

```
2026-03-24T10:00:01 INFO     === Monitor run started ===
2026-03-24T10:00:01 INFO     Spawned Mihomo pid=12345
2026-03-24T10:00:03 INFO     Mihomo ready after 1.8s
2026-03-24T10:00:03 INFO     Triggering health check for provider=trojan-nodes
2026-03-24T10:00:08 INFO     Fetched results for 2 node(s)
2026-03-24T10:00:08 INFO     node=node-sg-01 alive=True delay=142ms → HEALTHY
2026-03-24T10:00:08 WARNING  node=node-hk-01 alive=False delay=0ms → UNHEALTHY reason=node unreachable
2026-03-24T10:00:08 WARNING  1 unhealthy node(s); dispatching restart webhook
2026-03-24T10:00:08 INFO     Dispatching webhook to http://10.0.0.1:8765/restart
2026-03-24T10:00:09 INFO     Webhook response: 200
2026-03-24T10:00:09 INFO     === Monitor run complete ===
```

**Trojan server:** `/var/log/clash-health-check/webhook.log` (also via `journalctl -u clash-webhook`)

```
2026-03-24T10:00:09 INFO     HTTP 10.0.0.2 "POST /restart HTTP/1.1" 200 -
2026-03-24T10:00:09 INFO     Trojan restarted successfully (client=10.0.0.2)
```

## Running tests

```bash
cd tests
python3 -m unittest discover -v
```
