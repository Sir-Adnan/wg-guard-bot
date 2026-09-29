# Deployment

## Contents

- [Prerequisites](#prerequisites)
- [Quick install](#quick-install)
- [Manual install](#manual-install)
- [Domain and HTTPS](#domain-and-https)
- [Backup and restore](#backup-and-restore)
- [Updating](#updating)
- [Monitoring and logs](#monitoring-and-logs)
- [Uninstall](#uninstall)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Item | Minimum | Recommended |
|---|---|---|
| Operating system | Ubuntu 22.04 / Debian 12 | Ubuntu 24.04 |
| RAM | 1 GB | 2 GB |
| Disk | 5 GB | 20 GB |
| CPU | 1 core | 2 cores |
| Software | Docker 24+ and Compose v2 | Latest version |

Required ports:

| Port | Purpose |
|---|---|
| `8080` (or `PANEL_PORT`) | Admin panel |
| `443` / `80` | Only if you use a domain and HTTPS |

> In `polling` mode the bot needs no other inbound port.

---

## Quick install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Sir-Adnan/wg-guard-bot/main/install.sh)
```

The script does the following:

1. Checks for Docker and installs it if needed
2. Asks for the bot token and the admin IDs (in Persian)
3. Generates `SECRET_KEY`, the database password and the panel password at random
4. Writes `.env` and brings the services up
5. Runs the database migrations
6. Waits for the panel to become ready and prints the login details

### Unattended install

```bash
bash install.sh --yes \
  --bot-token "123456789:AA..." \
  --admin-ids "123456789" \
  --port 8080 \
  --panel-url "http://1.2.3.4:8080" \
  --owner-password "a-strong-password"
```

### Full list of options

```bash
bash install.sh --help
```

---

## Manual install

```bash
git clone https://github.com/Sir-Adnan/wg-guard-bot.git
cd wg-guard-bot
cp .env.example .env
```

At minimum, fill in these four values:

```bash
SECRET_KEY=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')
BOT_TOKEN=123456789:AA...
ADMIN_IDS=123456789
```

Then:

```bash
docker compose up -d --build
docker compose exec bot alembic upgrade head
docker compose logs -f bot
```

---

## Domain and HTTPS

There are two ways to put the bot on a domain or subdomain. **Method A requires
no configuration and renews certificates forever on its own** — use it unless
you already run a reverse proxy.

### Method A — built-in Caddy (recommended, automatic SSL)

Answer the installer's domain question, or pass the flag:

```bash
bash install.sh --domain bot.example.com --acme-email you@example.com
```

With a domain configured the installer:

1. validates the hostname (no scheme, no path, no bare IP, not `localhost`);
2. resolves it and compares the result with the server's public IP, warning you
   if they differ (a DNS record must point here **before** the certificate can
   be issued);
3. writes `DOMAIN`, `ACME_EMAIL`, `PANEL_BASE_URL`, `WEBHOOK_BASE_URL`,
   `BOT_MODE=webhook`, `PANEL_BEHIND_PROXY=true` and `PANEL_BIND=127.0.0.1`
   into `.env`;
4. starts the stack **with the `tls` profile**, which adds a
   [Caddy 2](https://caddyserver.com/) container;
5. waits for `https://<domain>/healthz` and reports the result.

Caddy obtains a Let's Encrypt certificate on the first request and **renews it
automatically about 30 days before expiry**. There is no cron job, no
`certbot renew` timer and nothing to remember. Certificates live in the
`caddy_data` volume — do not delete it unless you want new ones.

Ports **80 and 443 must be reachable from the internet** for issuance and
renewal to work, and `PANEL_BIND=127.0.0.1` keeps the raw app port off the
public interface.

### Changing or removing the domain later

```bash
bash scripts/set-domain.sh bot.example.com   # set or change
bash scripts/set-domain.sh --status          # domain, Caddy state, cert expiry
bash scripts/set-domain.sh --renew           # reload config + print expiry
bash scripts/set-domain.sh --disable         # back to polling, no TLS
```

`set-domain.sh` edits `.env` in place (backing it up to `.env.bak.<timestamp>`),
restarts the stack and waits for the new URL. Changing the domain triggers a new
certificate automatically; the old one stays in the volume unused, and the
Telegram webhook is re-registered on the next bot start.

From the project Makefile: `make domain d=bot.example.com`, `make cert`,
`make tls`.

### Method B — your own reverse proxy

Already running Nginx or Traefik? Set `PANEL_BEHIND_PROXY=true` and point it at
the published panel port. An Nginx example:

```nginx
server {
    listen 443 ssl http2;
    server_name panel.example.com;

    ssl_certificate     /etc/letsencrypt/live/panel.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/panel.example.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8080;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

Then in `.env`:

```bash
PANEL_BASE_URL=https://panel.example.com
PANEL_BEHIND_PROXY=true
BOT_MODE=webhook
WEBHOOK_BASE_URL=https://panel.example.com
```

Certificate issuance and renewal are then your proxy's responsibility (for
Nginx: `certbot --nginx` plus the packaged `certbot.timer`).

> **Important:** without `PANEL_BEHIND_PROXY=true` the session cookie is not
> marked `Secure` and login fails in some browsers.
>
> Everything the application serves — `/panel`, `/healthz`, `/readyz`,
> `/tg/webhook/<secret>`, `/wg/webhook/<panel_id>` — is one ASGI app on one
> port, so a single `proxy_pass` (or one Caddy site block) is all you need.

### Running without a domain

Leave `DOMAIN` empty. The bot uses long polling, needs no certificate and no
open port besides the panel's; there is no Caddy container at all.

---

## Backup and restore

### Automatic

By default a `backups/wgguard-YYYYMMDD-HHMMSS.sql` file is written every 24
hours, and copies older than `BACKUP_KEEP` are deleted.

You can also run a backup by hand from the panel:
**System events → Run backup**

### Manual

```bash
docker compose exec -T db pg_dump -U wgguard wgguard > backup-$(date +%F).sql
```

### Restore

> ⚠️ Restoring replaces the current data. Back up the current state first.

```bash
docker compose stop bot
docker compose exec -T db psql -U wgguard -d wgguard < backup-2026-09-29.sql
docker compose start bot
```

### Moving to another server

```bash
# old server
docker compose exec -T db pg_dump -U wgguard wgguard > dump.sql
tar czf wggb-backup.tgz dump.sql .env backups/

# new server
tar xzf wggb-backup.tgz
docker compose up -d
docker compose exec -T db psql -U wgguard -d wgguard < dump.sql
```

Move `.env` as well; without the old `SECRET_KEY` the panel tokens cannot be read.

---

## Updating

```bash
bash update.sh                 # from the main branch
bash update.sh --branch develop
```

The script pulls the code, rebuilds the image, runs the migrations and brings
the service up.

### Manual update

```bash
git pull
docker compose build
docker compose up -d
docker compose exec bot alembic upgrade head
```

---

## Monitoring and logs

```bash
docker compose ps                       # service status
docker compose logs -f bot              # bot logs
docker compose logs --tail=100 db       # database logs
curl -s localhost:8080/healthz          # application health
curl -s localhost:8080/readyz           # readiness + database connectivity
```

Both endpoints answer without authentication and are suitable for external monitoring.

Important events (panel errors, provisioning failures, system events) are also
visible in **Panel → System events**.

---

## Uninstall

```bash
bash uninstall.sh            # stop and remove the containers — data is kept
bash uninstall.sh --purge    # full removal including the database and backups
```

---

## Troubleshooting

<details>
<summary><b>The service does not come up</b></summary>

```bash
docker compose logs --tail=60 bot
docker compose ps
```

If you see "SECRET_KEY is missing or too short", fill in `SECRET_KEY` in
`.env` (at least 32 characters; 64 recommended).

</details>

<details>
<summary><b>The bot is online but does not answer</b></summary>

- Make sure you have not blocked the bot in Telegram.
- Check `BOT_TOKEN` again.
- If the server cannot reach Telegram: set `BOT_PROXY`.
- In webhook mode, check `getWebhookInfo`.

</details>

<details>
<summary><b>"Database unavailable"</b></summary>

```bash
docker compose ps db
docker compose exec bot alembic upgrade head
```

If you changed the database password, you must also update `POSTGRES_PASSWORD`
and probably `DATABASE_URL`.

</details>

<details>
<summary><b>The subscription link does not work</b></summary>

If `SUBSCRIPTION_BASE_URL` is empty, the WG-Guard panel URL is used.
If the panel is on an internal domain, set the public domain in
**Panel → WG-Guard panels → Public subscription URL**.

</details>

<details>
<summary><b>The disk fills up quickly</b></summary>

```bash
docker system prune -f          # clean up unused images
ls -lh backups/                 # number of backup copies
```

Lower `BACKUP_KEEP` and press **System events → Clean up old events** in the panel.

</details>
