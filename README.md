# 🤖 WG-Guard Bot — VPN Sales Bot

**Telegram VPN service sales bot + web admin panel** for [WG-Guard](https://github.com/Sir-Adnan/wg-guard) panels (AmneziaWG).

Sales, card-to-card payment with multi-admin approval, wallet, trial service, support, sales reports and a complete admin panel — all in one project, ready to run on Docker.

![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)
[![CI](https://github.com/Sir-Adnan/wg-guard-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/Sir-Adnan/wg-guard-bot/actions/workflows/ci.yml)

> **Repository:** <https://github.com/Sir-Adnan/wg-guard-bot>

## Table of contents

- [Quick install](#quick-install)
- [Features](#features)
- [Manual install](#manual-install)
- [Configuration](#configuration)
- [Admin panel guide](#admin-panel-guide)
- [Bot guide](#bot-guide)
- [Connecting to a WG-Guard panel](#connecting-to-a-wg-guard-panel)
- [Button and premium-emoji customisation](#button-and-premium-emoji-customisation)
- [Project architecture](#project-architecture)
- [Development and testing](#development-and-testing)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)

---

## Quick install

On a fresh Ubuntu or Debian server (Docker is installed automatically):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Sir-Adnan/wg-guard-bot/main/install.sh)
```

The installer asks you these questions on the terminal, in English (bot token, numeric admin IDs, panel port), and does the rest itself:
generating the secrets, bringing up the database, running the migrations and starting the bot.

At the end it prints the panel URL, the username and the generated password. **Change the password on first login.**

### Unattended install (script-friendly)

```bash
bash install.sh --yes \
  --bot-token "123456789:AA...your-token" \
  --admin-ids "123456789,987654321" \
  --port 8080 \
  --panel-url "http://1.2.3.4:8080"
```

| Option | Description |
|---|---|
| `--bot-token` | Bot token from [@BotFather](https://t.me/BotFather) |
| `--admin-ids` | Numeric admin IDs, comma-separated (required) |
| `--support-ids` | Numeric support-staff IDs, comma-separated |
| `--domain` | Domain or subdomain (e.g. `bot.example.com`) — **enables automatic HTTPS** |
| `--acme-email` | Contact address for Let's Encrypt (optional but recommended) |
| `--port` | Panel port (default `8080`) |
| `--panel-url` | Public panel URL |
| `--app-name` | Shop name shown to customers |
| `--owner-username` | Panel username (default `admin`) |
| `--owner-password` | Panel password (random by default) |
| `--ip` | Override the detected public IP (used for the DNS pre-check) |
| `--branch` | Git branch to install (default `main`) |
| `--dir` | Install directory |
| `--yes`, `-y` | Unattended: take every value from the flags or the defaults |
| `--no-docker-install` | Skip the automatic Docker install |
| `--no-domain` | Force no-domain mode even if `DOMAIN` is already set |
| `--force` | Overwrite an existing `.env` (backs it up first) |

> Run `bash install.sh --help` for the authoritative list.

### Domain and automatic HTTPS

Give the installer a domain and it configures TLS for you:

```bash
bash install.sh --domain bot.example.com --acme-email you@example.com
```

A Caddy container is added to the stack; it obtains a Let's Encrypt certificate
on the first request and **renews it automatically before expiry** — no cron
job, no `certbot renew`, nothing to maintain. The installer validates the
hostname, checks that DNS already points at your server, and waits for
`https://<domain>/healthz` before reporting success.

Change or drop the domain at any time:

```bash
bash scripts/set-domain.sh new.example.com   # change the domain
bash scripts/set-domain.sh --status          # domain, Caddy state, cert expiry
bash scripts/set-domain.sh --renew           # reload config and show expiry
bash scripts/set-domain.sh --disable         # back to polling, no TLS
```

Prefer your own reverse proxy? Leave the domain empty, set
`PANEL_BEHIND_PROXY=true` and follow
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#method-b--your-own-reverse-proxy).

### Follow-up commands

```bash
bash update.sh              # update to the latest version
bash uninstall.sh           # stop and remove the containers (data is kept)
bash uninstall.sh --purge   # full removal including the data
```

If you are inside the project directory:

```bash
make up        # start
make tls       # start with the TLS (Caddy) profile
make domain    # change the domain: make domain d=bot.example.com
make cert      # certificate status and expiry
make logs      # follow the logs
make migrate   # run migrations
make backup    # back up
make psql      # connect to the database
```

---

## Features

### Shop and sales

- 🗂 **Multi-level category tree** — category → sub-category → sub-sub-category (up to 5 levels), each with its own icon and custom order
- 📦 **Volume and time-based plans** — volume (gigabytes), duration (days/months), device count, speed limit, "special offer" badge, feature list
- 🏷 **Pricing in Toman** — always stored in **Rial**, displayed in Toman (configurable)
- 🎟 **Discount codes** — percentage or fixed amount, with a discount cap, a minimum purchase and per-code/per-user limits
- 🎁 **Gift codes** — wallet top-up, a free service or a percentage bonus, with bulk generation of up to 200 codes
- 👛 **Wallet** — top-up, payment, transaction history and an accounting ledger
- 💳 **Card-to-card** — several bank cards, receipt submission, approval/rejection by admins
- ✅ **Multi-admin approval** — the receipt goes to every admin and support agent; once one of them decides, **everyone else's copy is updated too**
- 🎁 **Trial service** — once per user, with a configurable cooldown
- 💸 **Cashback and referral rewards** — a percentage returned to the wallet plus a reward for inviting friends
- 🛒 **Limited sales** — stock limits for individual plans

### Services

- 🔑 Fetch the config, the QR code and the subscription link again at any time
- 🔄 Renew a service, automatic renewal (queued successor)
- 📱 Add and remove devices, rotate keys
- 📊 Usage, remaining days and live status
- 🔔 Expiry reminders (3 days, 1 day, at expiry) and 80% / 100% usage warnings
- 🧪 Automatic synchronisation of service status with the panel

### Support and communication

- 🎫 Two-way ticket system, answered from either the bot or the panel
- 📣 Broadcasts with audience filtering and **scheduling**
- 📚 Connection guides (Android, iOS, Windows, macOS, Linux)
- 📢 Mandatory channel membership with automatic approval of join requests
- 🚫 User blocking and maintenance mode

### Admin panel

- 📊 Dashboard with a sales chart, live statistics and follow-up alerts
- 🖥 **Support for multiple WG-Guard panels** with automatic selection of the least-loaded node
- 👥 User management: balance, notes and direct messages
- 🧾 Receipt review queue with approve/reject and a rejection reason
- 📈 Sales reports with charts, a breakdown by plan / payment method / panel and **CSV export**
- 🎨 Full customisation of buttons (text, colour, emoji) and bot texts
- 📋 Event log and a complete audit trail of admin actions
- 👮 Three access levels: owner, admin, support
- 💾 Automatic database backups

---

## Manual install

<details>
<summary>If you want to control everything yourself</summary>

```bash
git clone https://github.com/Sir-Adnan/wg-guard-bot.git wg-guard-bot
cd wg-guard-bot
cp .env.example .env
```

Fill in these values in `.env`:

```bash
SECRET_KEY=$(openssl rand -hex 32)          # security key (64 characters)
POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')
BOT_TOKEN=123456789:AA...                   # bot token
ADMIN_IDS=123456789                         # numeric admin IDs
OWNER_PASSWORD=a-strong-password            # panel login password
```

Then:

```bash
docker compose up -d --build
docker compose logs -f bot
```

The panel comes up at `http://SERVER_IP:8080/panel`.

</details>

---

## Configuration

Everything is configured through the `.env` file (infrastructure) and the admin panel (shop).

### Key `.env` variables

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | — | **Required.** Signs the panel cookie and encrypts panel tokens and configs |
| `BOT_TOKEN` | — | Bot token from BotFather |
| `BOT_MODE` | `polling` | `polling` (no domain needed) or `webhook` |
| `ADMIN_IDS` | — | Numeric owner IDs, comma-separated |
| `SUPPORT_IDS` | — | Numeric support-agent IDs (limited access) |
| `PANEL_PORT` | `8080` | Web panel port |
| `PANEL_BASE_URL` | `http://localhost:8080` | Public panel URL |
| `CURRENCY_DISPLAY` | `toman` | Display unit: `toman` or `rial` |
| `TEST_SERVICE_ENABLED` | `true` | Enable the trial service |
| `RECEIPT_EXPIRE_MINUTES` | `90` | How long a receipt stays valid |
| `BACKUP_ENABLED` | `true` | Daily automatic backups |
| `BOT_PROXY` | — | Proxy for talking to Telegram (e.g. `socks5://127.0.0.1:1080`) |
| `BOT_API_SERVER` | — | Local Bot API server (optional) |

> **Important:** never change `SECRET_KEY`, because the stored panel tokens and configs are encrypted with it.

### Shop settings (from the panel)

Items such as the shop name, support hours, the receipt deadline, the minimum top-up, the cashback percentage, the trial service, mandatory membership and button colours are changed in
**Admin panel → Shop settings** and need no restart.

---

## Admin panel guide

URL: `http://SERVER_IP:8080/panel`

| Section | Purpose |
|---|---|
| **Dashboard** | 30-day sales, orders in progress, receipts awaiting review, panel health |
| **Plans** | Create and edit plans: price, volume, duration, category; duplicate and enable/disable |
| **Categories** | Multi-level category tree with icons and ordering |
| **Orders** | View, approve a manual payment, retry provisioning, cancel and refund |
| **Services** | Synchronise, enable/disable, reset traffic, renew manually, resend the config |
| **Payment receipts** | Card-to-card approve/reject queue with the receipt image and a message to the customer |
| **Users** | Search, adjust the balance, notes, messages, blocking, CSV export |
| **Tickets** | Read the conversation and reply to the customer |
| **Discount / gift codes** | Create, edit and report on usage |
| **Buttons and emoji** | Text, colour and premium emoji for every button, with a live preview |
| **Bot texts** | Edit every Persian bot string without writing code |
| **Guides** | Educational content for customers |
| **Channels** | Manage mandatory membership |
| **Bank cards** | Destination cards for card-to-card payments |
| **WG-Guard / VPN panels** | Add and manage several nodes, of any provider |
| **Broadcast** | Bulk messages with audience filtering and scheduling |
| **System events** | Error log and a record of admin actions |
| **Sales reports** | Revenue over a chosen period, breakdowns and CSV export |
| **Staff** | Manage admins, support agents and access levels |
| **Profile** | Your own account: change the panel password |

---

## Bot guide

### User menu

| Button | What it does |
|---|---|
| 🛒 Buy a service | Browse the categories and buy a plan |
| 📦 My services | View, config, QR code, subscription link, renewal |
| 👛 Wallet | Balance, top-up, history |
| 🎁 Trial service | Get a free trial service |
| 🎧 Support | Open a ticket and follow it up |
| 📚 Connection guide | Setup instructions for every device |
| 👤 Account | Account details and purchase statistics |
| 👥 Refer friends | Invite link and reward statistics |
| 🎁 Gift code | Redeem a gift code |
| 📢 Our channels | Channel links |

### Purchase flow

```
Buy service → category → plan → payment method
                                 ├── Wallet        → instant approval → service created
                                 └── Card-to-card  → receipt sent → admin approval → service created
```

After approval, the config (`.conf`), the QR image and the subscription link are sent automatically.

### Text commands

`/start` start · `/menu` menu · `/help` help · `/cancel` cancel the current action · `/id` show your ID
and for admins: `/admin` the admin panel inside the bot

---

## Connecting to a WG-Guard panel

1. In the WG-Guard panel, create an **API Token** with these scopes:

```
node.read, plans.read, plans.write, users.read, users.create, users.update, users.delete
devices.read, devices.write, configs.read, subscriptions.read, subscriptions.rotate
traffic.read, traffic.update, stats.read, purchases.create, operations.read
```

2. In **Admin panel → WG-Guard panels**, add a node:

| Field | Description |
|---|---|
| Name | Any label you like, for identification |
| URL | Base URL of the panel, for example `https://panel.example.com` |
| API token | The token you created (stored encrypted) |
| Public subscription URL | If subscription links use a separate domain |
| Max services | Node capacity (optional) |

3. Use the **Test connection** button to check the node's health.

### How it works

- On the first sale of a plan, that plan is created on the node automatically (`POST /api/v1/plans`) and its ID is stored.
- Services are provisioned through the **atomic** `POST /api/v1/purchases` endpoint, which registers the user, the device and the subscription link in a single call.
- Every order gets a stable `Idempotency-Key`; if the connection drops, the bot retries with the same key and
  even checks with `GET /api/v1/operations/result` whether the purchase went through. **So two services are never created for one payment.**

---

## Button and premium-emoji customisation

From **Admin panel → Buttons and emoji** you can set, for every button:

| Option | Values |
|---|---|
| **Text** | Any Persian wording you like |
| **Colour** | Default · blue (`primary`) · green (`success`) · red (`danger`) · link |
| **Premium emoji** | Numeric ID of a custom emoji (`icon_custom_emoji_id`) |
| **Fallback** | The Unicode emoji shown when no premium emoji is available |

A live preview is rendered on the same page, plus a preview of the whole main menu at the bottom.

**Notes:**

- Button colours and custom emoji require **Bot API 9.4+** and an up-to-date Telegram client.
- Premium emoji on buttons require the bot to own a username purchased on [Fragment](https://fragment.com).
- If Telegram rejects these features, the bot **falls back automatically** to plain buttons and service is not interrupted.
- You can use `{e:fire}` in message texts; it is replaced with the configured premium emoji.

---

## Project architecture

```
app/
├── core/          config, security, money (Rial/Toman), Jalali dates, cache, errors
├── db/            SQLAlchemy models and session management
├── panels/        the VPN-backend *port*: canonical models, provider registry,
│                  and one adapter per backend under providers/   ← only this talks to a node
├── services/      business logic (orders, payments, receipts, provisioning, reports, …)
├── bot/           Telegram layer: handlers, keyboards, middlewares, texts
├── web/           admin panel: FastAPI + Jinja2 + custom CSS
├── workers/       scheduled jobs (reminders, synchronisation, backups)
└── locales/       default Persian strings
```

**Design principles:**

- **Strict layering** — `panels` only talks to the node, `services` holds the business rules, and `bot` and `web` only present.
- **Money is always Rial** — an integer everywhere; Toman only at display time.
- **Secrets are encrypted** — panel tokens, WireGuard configs and subscription links are encrypted with `SECRET_KEY`.
- **No external dependencies in the panel** — no CDN, no npm, no chart library; everything lives in the repository.
- **Easy to extend** — a new page is one file in `app/web/routes/` plus one template. A new bot command is one file in `app/bot/handlers/`.

### Adding another VPN backend (PasarGuard, Marzban, x-ui, …)

WG-Guard is the first backend, not the only one. `app/panels` is a port-and-adapter
boundary: the domain talks to `PanelProvider` and the canonical models in
`app/panels/models.py`, never to a vendor payload.

```python
# app/panels/providers/pasarguard.py
@register
class PasarGuardProvider(PanelProvider):
    kind = "pasarguard"
    label = "PasarGuard"
    capabilities = frozenset({CAP_ATOMIC_PURCHASE, CAP_PURCHASE_RECOVERY})

    async def purchase(self, *, plan_ref, username, device_name, idempotency_key):
        ...   # return a canonical PurchaseResult
```

Then import the module in `app/panels/providers/__init__.py`. That is the whole
change — no business logic, no admin-panel UI work:

- the **"panel type" dropdown** in the admin panel is generated from the registry;
- `Panel.kind` selects the adapter at runtime, so a WG-Guard node and a
  PasarGuard node can serve different plans in the same shop;
- **capabilities** (`CAP_NEXT_PLAN`, `CAP_PLAN_SYNC`, …) let a partial backend
  degrade gracefully with a clear Persian message instead of crashing;
- statuses are normalised to one canonical set, so reporting, reminders and the
  panel need no per-backend branch.

`app/panels/providers/example.py` is a fully documented skeleton to copy, and
`tests/test_panel_providers.py` pins the contract every adapter must meet.

Further documentation: [`docs/README.md`](docs/README.md) (index) · [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · [`docs/PANEL-CONTRACT.md`](docs/PANEL-CONTRACT.md) · [`docs/PROVIDERS.md`](docs/PROVIDERS.md) · [`docs/UX-WRITING.md`](docs/UX-WRITING.md) · [`docs/VERIFICATION.md`](docs/VERIFICATION.md) · [`AGENTS.md`](AGENTS.md)

---

## Development and testing

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows
# source .venv/bin/activate       # Linux/macOS

pip install -r requirements-dev.txt
```

### Running the tests

```bash
# no database needed — unit tests, the WG-Guard mock, bot wiring
pytest -m "not db" -q

# the full suite (needs a test database)
export TEST_DATABASE_URL="postgresql+asyncpg://user:pass@127.0.0.1:5432/wgguard_test"
pytest -q
```

The database-backed tests are marked `db`; without a reachable database they are
skipped rather than failed (73 pass, 99 skip), and every run prints which database
it probed. `make test-db` starts a throwaway PostgreSQL and `make test` runs the
whole suite against it. Ports, containers and the exact environment block are in
[`docs/VERIFICATION.md`](docs/VERIFICATION.md#4-the-environment-the-commands-need).

To test without a real node, use the **mock panel** shipped in the repository:

```bash
cd tools && python -m mock_wg_panel --port 8787
```

### Running locally

```bash
cp .env.example .env      # fill in the values
uvicorn app.main:app --reload
```

### Test layout

| File | Coverage |
|---|---|
| `tests/test_core.py` | money, security, Jalali dates |
| `tests/test_wg_client.py` | the REST client against the mock panel (idempotency, pagination, errors) |
| `tests/test_bot_wiring.py` | dispatcher and router composition, handler order |
| `tests/test_purchase_flow.py` | the full purchase cycle, provisioning, receipt approval, renewal |
| `tests/test_catalog_features.py` | category tree, gift codes, guides |
| `tests/test_panel_providers.py` | the provider port: registry, capability flags, canonical mapping |
| `tests/test_panel.py` | rendering of every panel page + login + CSRF |
| `tools/mock_wg_panel/` | the mock node's own smoke suite, collected by the same `pytest` |

---

## Troubleshooting

<details>
<summary><b>The bot does not respond</b></summary>

```bash
docker compose logs -f bot
```

- Check `BOT_TOKEN`.
- If your server cannot reach Telegram, set `BOT_PROXY`.
- If the panel comes up but the bot does not, the log shows the error.

</details>

<details>
<summary><b>The panel does not open</b></summary>

```bash
docker compose ps                 # service status
curl http://127.0.0.1:8080/healthz
```

- Open the port in the server firewall.
- If you are behind a reverse proxy, set `PANEL_BEHIND_PROXY=true`.

</details>

<details>
<summary><b>"Panel token cannot be read" error</b></summary>

This error appears when `SECRET_KEY` has changed. Enter the panel token again in
**Admin panel → WG-Guard panels**.

</details>

<details>
<summary><b>"Database unavailable" error</b></summary>

```bash
docker compose logs db
docker compose exec bot alembic upgrade head
```

</details>

<details>
<summary><b>I forgot the panel password</b></summary>

```bash
docker compose exec bot python - <<'PY'
import asyncio
from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import Staff
from app.db.session import session_scope


async def main():
    async with session_scope() as session:
        row = (await session.execute(select(Staff).where(Staff.role == "owner"))).scalars().first()
        row.password_hash = hash_password("NewStrongPass123")
        print("Password changed for:", row.login)


asyncio.run(main())
PY
```

</details>

---

## FAQ

<details>
<summary><b>How many WG-Guard panels are supported?</b></summary>

Yes, any number. You can pin each plan to a specific panel or let the bot pick the
least-loaded node. The capacity of each node is configurable.

</details>

<details>
<summary><b>Is the currency Rial or Toman?</b></summary>

Storage and all calculations always use **Rial**, but the user always sees **Toman**
(configurable with `CURRENCY_DISPLAY`). When you enter a price in the panel, write the amount in Toman.

</details>

<details>
<summary><b>What if an admin approves a receipt by mistake?</b></summary>

From **Orders** you can cancel the order and return the money to the user's wallet (refund).
Every decision is also recorded in **System events → Actions**.

</details>

<details>
<summary><b>How do I make sure data is not lost?</b></summary>

A daily backup runs automatically into the `backups/` directory (configurable with `BACKUP_INTERVAL_HOURS` and `BACKUP_KEEP`).
You can also press the "Run backup" button in **System events** in the panel.

</details>

<details>
<summary><b>Can I run the bot on a server without a domain?</b></summary>

Yes. The default mode is `polling` and it needs no domain or SSL. You only need a domain and an SSL
certificate if you want to use `webhook` mode.

</details>

---

## Licence

[MIT](LICENSE)

## Credits

- [WG-Guard](https://github.com/Sir-Adnan/wg-guard) panel by Sir-Adnan
- Built with [aiogram](https://github.com/aiogram/aiogram) and [FastAPI](https://fastapi.tiangolo.com/)
