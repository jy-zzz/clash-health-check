# Architecture

## System Overview

Two hosts, two components:

```
┌──────────────────────────────────────┐     HTTP POST /restart
│  Monitor Host (Ubuntu)               │ ─────────────────────────► ┌────────────────────────────┐
│                                      │     Authorization: Bearer   │  Trojan Proxy Server       │
│  cron (*/10 * * * *)                 │                             │                            │
│    └─ monitor.py                     │                             │  clash-webhook.service     │
│         └─ mihomo (ephemeral)        │                             │    └─ webhook_server.py    │
│              └─ 127.0.0.1:9090       │                             │         └─ systemctl       │
│                                      │                             │              restart trojan│
└──────────────────────────────────────┘                             └────────────────────────────┘
```

---

## Monitor Host File Layout

```
/usr/local/bin/
└── mihomo                            # Mihomo binary (from GitHub releases)

/opt/mihomo-monitor/
├── config.yaml                       # Mihomo runtime config
├── trojan_subscription.yaml          # Trojan node definitions
├── monitor.py                        # Python monitoring script
├── monitor.log                       # Rolling log (managed by WatchedFileHandler)
└── secrets.env                       # API secret + webhook secret (chmod 600)

/etc/cron.d/
└── mihomo-monitor                    # Cron schedule

/etc/logrotate.d/
└── mihomo-monitor                    # Log rotation config
```

---

## Trojan Server File Layout

```
/opt/clash-health-check/
└── webhook_server.py                 # Python stdlib HTTP server

/etc/systemd/system/
└── clash-webhook.service             # systemd unit

/etc/clash-health-check/
└── webhook.env                       # WEBHOOK_TOKEN, WEBHOOK_PORT (chmod 600)

/var/log/clash-health-check/
└── webhook.log                       # Webhook server log
```

---

## Mihomo Configuration

### config.yaml

```yaml
mode: direct          # No routing via proxy groups; direct outbound connections only
ipv6: true
allow-lan: false      # API only accessible from localhost
external-controller: 127.0.0.1:9090
secret: ""            # Set via monitor.py at startup by writing a fresh config

proxy-providers:
  trojan-nodes:
    type: file
    path: ./trojan_subscription.yaml
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204   # Google generate_204; always returns 204
      interval: 300   # Background check interval in seconds (within a single run)
      timeout: 5000   # In MILLISECONDS (not seconds — Mihomo quirk)
      lazy: false     # Always test even when provider is not selected in a group

proxies: []
proxy-groups: []
rules: []
```

**Critical**: No `port`, `socks-port`, `mixed-port`, `tun`, or `redir-port` — these are intentionally absent so Mihomo binds zero proxy listener ports.

### trojan_subscription.yaml

```yaml
proxies:
  - name: "node-sg-01"
    type: trojan
    server: sg-01.example.com
    port: 443
    password: "your-trojan-password"
    udp: true
    skip-cert-verify: false
    sni: sg-01.example.com

  - name: "node-hk-01"
    type: trojan
    server: hk-01.example.com
    port: 443
    password: "your-trojan-password"
    udp: true
    skip-cert-verify: false
    sni: hk-01.example.com
```

Each node must have a unique `name`. The name is used as the path parameter in API calls.

---

## Mihomo REST API Endpoints Used

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/version` | Readiness check — returns 200 when API is up |
| GET | `/providers/proxies/{name}/healthcheck` | Trigger async health check for all nodes in provider |
| GET | `/providers/proxies/{name}` | Fetch per-node results: `alive`, `delay`, `history` |

All requests require the header: `Authorization: Bearer <MIHOMO_API_SECRET>`

**Important timing note**: `/providers/proxies/{name}/healthcheck` returns immediately (fire-and-return). You must wait ~5 seconds before fetching results via `/providers/proxies/{name}`, or poll until the `history` array is non-empty.

---

## Python Module Structure

```
monitor.py
├── Config dataclass (paths, thresholds, secrets)
├── MihomoProcess
│   ├── start()              → Popen with start_new_session=True
│   ├── wait_for_ready()     → deadline loop polling /version
│   └── stop()               → terminate → wait(5s) → kill
├── HealthChecker
│   ├── trigger()            → GET /providers/proxies/{name}/healthcheck
│   └── fetch_results()      → GET /providers/proxies/{name}
├── NodeEvaluator
│   └── evaluate(result)     → healthy | unhealthy(reason)
├── WebhookClient
│   └── notify()             → POST /restart with Bearer token
└── main()                   → orchestrates all the above
```

---

## webhook_server.py Structure

```
webhook_server.py
├── Config (port, token from env)
├── RestartHandler(BaseHTTPRequestHandler)
│   ├── do_POST()
│   │   ├── _authenticate()  → hmac.compare_digest()
│   │   ├── _restart_trojan() → subprocess.run(["systemctl", "restart", "trojan"])
│   │   └── _send_response()
│   ├── do_GET/do_PUT/etc.   → 405 Method Not Allowed
│   └── log_message()        → structured log with client IP
└── main()                   → HTTPServer(("0.0.0.0", port), RestartHandler).serve_forever()
```

---

## Cron Configuration

`/etc/cron.d/mihomo-monitor`:

```
*/10 * * * * root /usr/bin/python3 /opt/mihomo-monitor/monitor.py >> /opt/mihomo-monitor/monitor.log 2>&1
```

Or, to use a dedicated user:

```
*/10 * * * * mihomo-monitor /opt/mihomo-monitor/venv/bin/python /opt/mihomo-monitor/monitor.py >> /opt/mihomo-monitor/monitor.log 2>&1
```

---

## systemd Unit — Webhook Server

`/etc/systemd/system/clash-webhook.service`:

```ini
[Unit]
Description=Clash Health Check Webhook Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/clash-health-check/webhook_server.py
Restart=on-failure
RestartSec=5s
StartLimitBurst=3
StartLimitInterval=60s

EnvironmentFile=-/etc/clash-health-check/webhook.env

StandardOutput=journal
StandardError=journal
SyslogIdentifier=clash-webhook

# Hardening (root required for systemctl)
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ReadWritePaths=/var/log/clash-health-check
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6
RestrictRealtime=yes
# NOTE: Do NOT add MemoryDenyWriteExecute=yes — Python requires W+X memory mappings

[Install]
WantedBy=multi-user.target
```

Validate before enabling:
```bash
systemd-analyze verify /etc/systemd/system/clash-webhook.service
systemctl enable --now clash-webhook.service
```

---

## logrotate Configuration

`/etc/logrotate.d/mihomo-monitor`:

```
/opt/mihomo-monitor/monitor.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    dateext
    dateformat -%Y%m%d
    create 640 root root
}
```

Use `logging.handlers.WatchedFileHandler` in `monitor.py` (not a plain `FileHandler`) so Python automatically detects inode changes after rotation and reopens the file. No `postrotate` signal required.

---

## Deployment Steps

### Monitor Host

```bash
# 1. Download and install Mihomo
curl -L https://github.com/MetaCubeX/mihomo/releases/latest/download/mihomo-linux-amd64.gz | gunzip > /usr/local/bin/mihomo
chmod +x /usr/local/bin/mihomo

# 2. Create working directory
mkdir -p /opt/mihomo-monitor
cp config.yaml trojan_subscription.yaml /opt/mihomo-monitor/

# 3. Create secrets file
cat > /opt/mihomo-monitor/secrets.env <<EOF
MIHOMO_API_SECRET=<random-32-char-string>
WEBHOOK_SECRET=<shared-secret-matching-trojan-server>
TROJAN_SERVER_HOST=<trojan-server-ip>
WEBHOOK_PORT=8765
EOF
chmod 600 /opt/mihomo-monitor/secrets.env

# 4. Deploy monitor script
cp monitor.py /opt/mihomo-monitor/
chmod +x /opt/mihomo-monitor/monitor.py

# 5. Set up cron
cp etc/cron.d/mihomo-monitor /etc/cron.d/
chmod 644 /etc/cron.d/mihomo-monitor

# 6. Set up logrotate
cp etc/logrotate.d/mihomo-monitor /etc/logrotate.d/
```

### Trojan Server

```bash
# 1. Create directory and deploy webhook server
mkdir -p /opt/clash-health-check
cp webhook_server.py /opt/clash-health-check/
chmod +x /opt/clash-health-check/webhook_server.py

# 2. Create secrets file
mkdir -p /etc/clash-health-check
cat > /etc/clash-health-check/webhook.env <<EOF
WEBHOOK_TOKEN=<shared-secret-matching-monitor-host>
WEBHOOK_PORT=8765
EOF
chmod 600 /etc/clash-health-check/webhook.env
chown root:root /etc/clash-health-check/webhook.env

# 3. Create log directory
mkdir -p /var/log/clash-health-check
chmod 750 /var/log/clash-health-check

# 4. Install and enable systemd service
cp clash-webhook.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now clash-webhook.service
```
