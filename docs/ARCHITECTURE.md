# Architecture

> A map of the codebase for whoever maintains it next — and the rules that keep
> it maintainable.

## 1. Layering

The dependency direction is strictly one-way. Nothing below imports anything
above it.

```
                 ┌──────────────────────────────┐
   presentation  │  app/bot  (aiogram)          │
                 │  app/web  (FastAPI + Jinja)  │
                 └──────────────┬───────────────┘
                                │
                 ┌──────────────▼───────────────┐
   domain        │  app/services                │
                 │  orders, receipts, payments, │
                 │  provisioning, reports, …    │
                 └──────────────┬───────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        │                       │                       │
┌───────▼────────┐   ┌──────────▼─────────┐   ┌─────────▼────────┐
│ app/panels     │   │ app/db             │   │ app/workers      │
│ panel *port*   │   │ SQLAlchemy models  │   │ APScheduler jobs │
└───────┬────────┘   └────────────────────┘   └──────────────────┘
        │
┌───────▼────────┐
│ app/core       │  config · security · money · jalali · cache · errors
└────────────────┘
```

**Why it matters.** `app/services` is the only place business rules live, so the
bot and the web panel can never disagree about what a price or a refund means.
And because `app/panels` is a *port* rather than a WG-Guard client, the backend
is swappable (§13).

### A note on `app.services.notifications`

The notifier is the one deliberate inversion: it lives in `services` but has to
know about the bot's keyboards (for the styled-keyboard fallback). It handles
this by duck-typing (`hasattr(keyboard, "markup")`) instead of importing
`app.bot`, so the layering rule holds and there is no import cycle.

## 2. Money

**Every amount in the database, every service signature and every API payload is
an integer number of Rial.** Toman exists only in presentation:

| Layer | Unit |
|---|---|
| `app/db/models.py`, `app/services/*` | Rial (integer) |
| `app/panels/client.py` | Rial (integer) — not money at all, just bytes/seconds |
| `app/bot/*`, `app/web/templates/*` | display unit via `format_amount` / the `|money` filter |
| Operator input in the web panel | Toman → converted by `form_money` |

`app/core/money.py` owns all conversion. Digit tables are written with explicit
`\u06f0`-style escapes: Persian (U+06F0–U+06F9) and Arabic-Indic (U+0660–U+0669)
digits look identical in most editors, and a silent swap would make every price
wrong.

## 3. Secrets

| Secret | Storage |
|---|---|
| Panel API token | `panels.api_token_encrypted` — Fernet, purpose `panel-token` |
| WG-Guard webhook secret | `panels.webhook_secret_encrypted` — purpose `webhook-secret` |
| WireGuard config (contains the private key) | `service_devices.config_encrypted` — purpose `config` |
| Customer subscription link (a bearer capability) | `services.subscription_encrypted` — purpose `subscription` |
| Admin password | `staff.password_hash` — `scrypt`, per-password salt |
| Panel session | signed cookie (`itsdangerous`) + CSRF token bound to the session id |

All Fernet keys derive from `SECRET_KEY` through HKDF-SHA256 with a distinct
`purpose`, so a ciphertext from one purpose can never be decrypted as another.
Rotating `SECRET_KEY` invalidates those values — `decrypt_secret` returns
`None` rather than raising, and the affected screens ask the operator to
re-enter the token.

## 4. Exactly-once provisioning

The most safety-critical path. `POST /api/v1/purchases` commits the user, the
first device and the customer link atomically, and is idempotent for 90 days
under an `Idempotency-Key`.

```
order.paid
   │
   ├─ pick panel           plan.panel_id → healthy node with free capacity
   ├─ ensure node plan     POST /plans once, remember wg_plan_id
   ├─ username             render plan.username_template
   ├─ POST /purchases      key = order.idempotency_key   ← stable forever
   │     ├─ 409 USERNAME_EXISTS → new derived key (…-r1) + new username
   │     └─ timeout / 5xx       → GET /operations/result with the SAME key
   │                              ├─ found  → that purchase is ours, continue
   │                              └─ 404    → not committed, retry safely
   ├─ GET /devices/{id}/config      → stored encrypted
   ├─ GET /users/{id}/subscription  → stored encrypted
   └─ service + device rows, then deliver to the customer
```

The key is **never regenerated** for the same payload. A retry after an
ambiguous failure reuses it, so a purchase that committed before the connection
dropped is replayed instead of duplicated. `provisioning.provision_order()` also
short-circuits when the order is already `completed`, which makes the
scheduler's crash-recovery pass safe to run at any time.

## 5. The multi-admin receipt workflow

The requirement was: a receipt must reach every admin/support, and when one of
them decides, everybody else must see it.

```
customer sends a photo
        │
        ├─ Receipt row (status=pending), order → awaiting_review
        ├─ broadcast: one message per reviewer
        │     └─ ReceiptMessage(chat_id, message_id) recorded per copy
        │
   reviewer A presses «تأیید»
        ├─ approve(): status=approved, order marked paid
        ├─ refresh_copies(): edit_message_caption on EVERY ReceiptMessage
        │     → later reviewers see "✅ تأیید شد توسط A" and the buttons are gone
        └─ provisioning + delivery to the customer
```

`ReceiptMessage` is the piece that makes the "everyone sees the same status"
requirement work; a reviewer pressing a stale button gets
`ReviewOutcome(accepted=False)` naming whoever decided first instead of a second
provisioning run.

## 6. Appearance: colours and premium emoji

Three Bot API 9.4+ features are surfaced without any handler knowing an emoji id:

```
app/services/appearance.py
  CATALOG      key → Visual(label, style, emoji_fallback, emoji_key)   (defaults)
  overrides    DB button_styles rows keyed by the same keys            (operator)
  resolve(key) → ResolvedVisual(label, style, icon_custom_emoji_id, emoji_fallback)

app/bot/keyboards.py
  KeyboardBuilder.add("menu.buy", callback=…)
      → label / style / icon_custom_emoji_id applied
      → plus a "plain twin" markup with no styling at all

app/services/notifications.py
  send() tries the styled markup; if Telegram rejects it the plain twin is
  sent instead and styling is switched off process-wide (logged once).
```

The fallback matters: colours and custom emoji need a recent client, a
self-hosted Bot API server may be old, and custom emoji on buttons require the
bot to own a Fragment username. None of those must ever break a sale.

Text placeholders `{e:fire}` are resolved through the same catalog, so the same
emoji id is used inline in messages and as a button icon.

## 7. Request lifecycle (one bot update)

```
Update
 └─ DatabaseMiddleware      one AsyncSession, committed on success, rolled back on error
     └─ ContextMiddleware   User + Staff records, maintenance/ban gate, cache warm-up
         └─ MembershipMiddleware  forced-join gate (staff and /start are exempt)
             └─ ThrottleMiddleware  per-user rate limit (staff exempt, receipts never dropped)
                 └─ handler
```

Handlers therefore always receive `session`, `user` and `staff` without
boilerplate. A handler that talks to the WG-Guard API calls
`await session.commit()` first so no database transaction is held open across a
network call.

## 8. Request lifecycle (one panel request)

```
GET /panel/orders
 └─ layout middleware   badge counters cached for 8 s
     └─ require_manager()  signed cookie → Staff, sets request.state.staff
         └─ route handler   services → render(request, "orders.html", ctx)
```

Mutations are POST-only, verify the CSRF token first, and end with
`redirect(url, message=…)`. The flash travels in a latin-1-safe cookie
(`json.dumps` without `ensure_ascii=False`) and is turned into a toast by
`panel.js`. Full details in [`PANEL-CONTRACT.md`](PANEL-CONTRACT.md).

## 9. Background jobs

| Job | Interval | Purpose |
|---|---|---|
| `expire_payments` | 5 min | close orders/receipts past their deadline, refund reserved wallet funds |
| `panel_health` | `PANEL_HEALTH_INTERVAL` | probe every node, persist the verdict |
| `sync_services` | 10 min | refresh usage/expiry from the nodes |
| `retry_provisioning` | 3 min | recover orders that were paid but never reached a node |
| `scheduled_broadcasts` | 1 min | fire time-scheduled campaigns |
| `reminders` | 30 min | expiry reminders (3 d / 1 d / expired) |
| `traffic_alerts` | 20 min | 80 % and 100 % quota warnings |
| `cleanup` | 12 h | trim `system_events` |
| `backup` | `BACKUP_INTERVAL_HOURS` | `pg_dump` into `backups/`, prune old files |

Every job opens its own session and swallows its exceptions (logged + recorded
as a `SystemEvent`), so one failing job can never take the scheduler down.

## 10. Extension points

| To add… | Do this |
|---|---|
| a bot screen | `app/bot/handlers/<name>.py` with a `router`, append the module to `ROUTE_ORDER` |
| a panel page | `app/web/routes/<name>.py` with a `router`, append the name to `ROUTE_MODULES`, add a template |
| a shop setting | one `SettingSpec` entry in `app/services/settings_store.py` — the panel renders it automatically |
| a bot string | one key in `app/locales/fa.json`; operators can override it from the panel |
| a button colour/emoji | one entry in `app/services/appearance.py::_BUTTON_RAW` |
| a database table | a model in `app/db/models.py` + `alembic revision --autogenerate` |
| a scheduled job | a method on `Jobs` + one `scheduler.add_job(...)` line |

## 11. Testing strategy

| Layer | How it is tested |
|---|---|
| money, security, calendar | pure unit tests, no I/O (`tests/test_core.py`) |
| WG-Guard client | against the in-repo mock node via `httpx.ASGITransport` — retries, idempotency, pagination, error envelopes (`tests/test_wg_client.py`) |
| services | real PostgreSQL; fixtures commit like production and each test starts from a `TRUNCATE` (`tests/test_purchase_flow.py`) |
| panel | every registered page is requested through the real cookie login and must render 200 (`tests/test_panel.py`) |

The mock node (`tools/mock_wg_panel`) implements 61 of the 63 documented
method/path pairs of the upstream OpenAPI document, including fault injection
(`POST /__mock__/fail`) and a request journal (`GET /__mock__/requests`).

## 12. Panel providers — supporting more than one VPN backend

WG-Guard is the first backend, not the only one. The integration is written as a
**port and adapter** boundary so adding PasarGuard, Marzban, x-ui or a plain
WireGuard server is a new file rather than a refactor.

```
app/panels/
├── models.py            canonical DTOs the domain speaks
│                        RemoteUser · RemoteDevice · PlanSpec · PurchaseResult …
├── base.py              the port: PanelProvider (ABC) + CAP_* capability flags
├── registry.py          kind -> adapter class; drives the "panel type" dropdown
├── manager.py           picks a node, caches one adapter per panel, probes health
├── client.py            WG-Guard HTTP transport      ┐ implementation details
├── schemas.py           WG-Guard payload models      ┘ of the adapter below
└── providers/
    ├── wgguard.py       WGGuardProvider  — maps vendor payloads to canonical ones
    └── example.py       a documented skeleton to copy when adding a backend
```

Three things make this work in practice:

**1. A canonical vocabulary.** Vendor statuses, field names and error shapes stop
at the adapter. `active | waiting_first_connection | disabled | expired |
traffic_exceeded` is the only status set the domain knows, so reporting,
reminders and the panel need no per-backend branch.

**2. Capabilities, not assumptions.** A backend that cannot queue a successor
plan simply omits `CAP_NEXT_PLAN`; `provider.require(...)` turns an attempt into
a clear Persian message instead of an `AttributeError`. A partial backend is a
supported configuration.

**3. The hard guarantee stays in one place.** `purchase()` must be exactly-once
per `idempotency_key`, and `recover_purchase()` answering `None` means "not
committed, safe to retry". Those two sentences are the whole duplicate-account
defence; everything else about a vendor's API is the adapter's problem.

`Panel.kind` selects the adapter at runtime, so a WG-Guard node and a PasarGuard
node can serve different plans in the same shop, and
`tests/test_panel_providers.py` pins the contract every new adapter must meet.

## 13. Operational notes

- **One process** runs the bot, the scheduler and the panel. That keeps
  in-process caches (`settings`, `texts`, `appearance`) trivially consistent —
  a write in the panel is visible to the bot immediately, no pub/sub needed.
- **Redis is optional.** It backs FSM state and the rate limiter; if it is
  unreachable the bot falls back to in-memory storage and keeps working.
- **The panel keeps working without the bot.** If `BOT_TOKEN` is missing or
  Telegram is unreachable, outbound messages are dropped with a single warning
  and the operator can still review receipts and build services.
- **Backups** need `pg_dump` (present in the runtime image) and write to
  `backups/`, which is a declared volume.
- **TLS is optional and automatic.** With a domain configured the stack runs a
  Caddy profile that issues and renews Let's Encrypt certificates on its own;
  without one the app is served directly on `PANEL_PORT`.
