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

The mechanics of adding anything — a page, a handler, a setting, a table — are
kept next to the contract they must satisfy, so there is one place to read and one
place to update:

- [`DEVELOPMENT.md`](DEVELOPMENT.md) §8 — the "adding a feature" index
- [`PANEL-CONTRACT.md`](PANEL-CONTRACT.md) — admin panel pages, ending in a checklist
- [`PROVIDERS.md`](PROVIDERS.md) — VPN backends and node calls

The architectural constraint behind all of them is the layering in §1: a new
feature is a new module in the right layer, never a new dependency direction.

## 11. Testing strategy

The *shape* of the suite is an architectural decision, not a testing detail:

| Layer | How it is tested | Why that way |
|---|---|---|
| money, security, calendar | pure unit tests, no I/O | the logic is pure; a database would add cost and hide nothing |
| WG-Guard transport | against the in-repo mock via `httpx.ASGITransport` | the vendor contract is external and must be exercised offline and deterministically |
| services | real PostgreSQL, fixtures that commit | services open their own sessions; a rollback-based wrapper would not see the rows they write |
| panel | every registered page fetched through a real cookie login | the failure mode is a template that renders for the author and 500s for everyone else |
| bot wiring | dispatcher construction and handler order | routers are single-parent singletons; a second construction used to gut the first |

The mock node (`tools/mock_wg_panel`) implements 61 of the 63 documented
method/path pairs of the upstream OpenAPI document, including fault injection
(`POST /__mock__/fail`) and a request journal (`GET /__mock__/requests`).

Which of these to run, and when, is [`VERIFICATION.md`](VERIFICATION.md).

## 12. Panel providers

WG-Guard is the first backend, not the only one: `app/panels` is a port and
adapter boundary, and `Panel.kind` selects the adapter at runtime.

The interface, the canonical models, the capability flags, the steps for adding a
backend and the WG-Guard vendor traps are documented in
[`PROVIDERS.md`](PROVIDERS.md). The design note that matters here is why the
boundary sits where it does: vendor statuses and payloads stop at the adapter so
that reporting, reminders and the admin panel never grow a per-backend branch,
and the exactly-once guarantee lives in the port rather than in each adapter.

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
