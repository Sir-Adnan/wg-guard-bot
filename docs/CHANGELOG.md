# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

Operator-facing text is English everywhere: the installer, updater and uninstaller
no longer print Persian, which Linux terminals render reversed (the Makefile help
and `.env.example` comments follow). Bot copy, panel labels and customer messages
stay Persian — that is what customers read.

Also on `main` since 1.0.0:

- **The main menu works.**  `KeyboardBuilder(columns=…)` stored the value and never
  read it, and `build()` called `aiogram`'s `adjust()` once per row — where each
  call replaces the whole layout, not just the row it follows.  Every screen
  therefore collected its buttons into a single row, and any screen with more than
  eight of them died while the markup was built: `/start` answered
  «خطای غیر‌منتظره‌ای رخ داد» on a fresh installation.  Rows now honour `columns`
  and are capped at Telegram's limit of eight.

- **The panel owner account is seeded at startup, not inside an update.**  It used
  to be created by the bot middleware, in the session the handler shares, so the
  rollback that follows a failing handler took the row with it — a fresh
  installation answered «نام کاربری یا رمز عبور نادرست است» for the password the
  installer had just printed.  A password changed inside the panel is never
  overwritten by `OWNER_PASSWORD`, and a lost password can be reset with
  `python -m app.cli set-password` ([deployment](DEPLOYMENT.md#reset-owner-password)).

- The interactive installer works.  `ask()` printed its question to stdout while
  every caller captured it (`answer="$(ask ...)"`), so the question never reached
  the terminal **and** its text was prepended to the answer: no typed bot token
  could ever match its validation pattern, and the operator saw nothing but
  repeated "invalid token" errors.  Questions now go to stderr and the answer is
  the only thing on stdout; end of input aborts with a clear message instead of
  asking forever.

- The gift-code switch in the panel now actually gates the gift screen (the bot
  read a key the panel could never write), and `/rules` answers instead of
  falling through to the catch-all.
- The development provider template is registered outside production only, so it
  no longer appears as a selectable panel type for an operator.
- `make test` / `lint` / `format` work again: they run against the project venv
  rather than the runtime image, which ships no development tooling.
- Database tests skip instead of failing when PostgreSQL is unreachable, the
  session header names the database it probed, and `REQUIRE_DB=1` (CI sets `CI`)
  turns that skip into an error.
- The mock WG-Guard panel is collected by the same `pytest`, and `tools/` is
  linted and formatted like the rest of the repository.
- Documentation for agents was restructured around invariants, authority
  boundaries and risk-proportional evidence (`AGENTS.md`, `docs/VERIFICATION.md`,
  `docs/UX-WRITING.md`, `docs/PROVIDERS.md`).

---

## [1.0.0] — 2026-09-29

First public release. A complete Telegram VPN shop for WG-Guard
(AmneziaWG) nodes: storefront, payments, provisioning, support and a full admin
panel — self-hosted, Docker-only, Persian-first.

### Added — Storefront

- Nested category tree (`PlanCategory`, up to 5 levels) with icons, ordering,
  visibility flags and cycle/depth validation.
- Plan catalog: volume (GB), duration (days/months), device limit, per-direction
  speed limits, start policy, badge, feature bullets, featured flag, limited
  stock, and a per-plan WG-Guard node binding.
- Pricing in **Rial** stored as integers, rendered in **Toman** with Persian
  digits and thousands separators.
- Discount codes: percentage or fixed amount, with a maximum discount, minimum
  basket, total-use cap, per-user cap and an expiry window.
- Gift / redeem codes: wallet credit, a free plan, or a percentage bonus on a
  top-up, plus bulk generation of up to 200 codes.
- Wallet: top-up by card, balance payment, refunds, referral rewards, cashback,
  and a full transaction history.
- Card-to-card payments with multiple destination cards and receipt upload
  (photo, screenshot/PDF document, or plain text).
- Free test service, created on demand from the shop settings, with a
  configurable cooldown per customer.
- Referral program with a personal invite link and a configurable bonus.
- Deep links: `?start=ref<code>` and `?start=plan<id>`.

### Added — Provisioning

- Typed async REST client for the WG-Guard `/api/v1` surface, mirroring
  `docs/upstream-api/openapi-wg-guard.json` including the error envelope and
  cursor pagination.
- Multi-node support with automatic node selection (least loaded, priority
  ordered, capacity aware) and per-plan pinning.
- Automatic node-side plan creation and drift detection: local terms are pushed
  to the node before the first sale.
- **Exactly-once provisioning**: a stable `Idempotency-Key` per order, reuse on
  transport retries, and recovery through `GET /operations/result` after an
  ambiguous failure. A dropped connection can never create a second account.
- Automatic node-side plan sync, service status sync, usage/expiry refresh,
  access rotation (`/subscription/rotate`) and per-device management.
- Auto-renew via the node's queued successor plan.
- Delivery of the `.conf` file, a locally generated QR code (no third-party
  QR service) and the subscription link.
- Config generation for the customer at any time, from the encrypted local copy,
  so a node outage never blocks a paying customer.

### Added — Admin panel (web)

- 21 pages: dashboard, plans, categories, orders, services, receipts, users,
  tickets, discounts, gifts, buttons & emoji, texts, guides, channels, cards,
  panels, broadcast, logs, reports, settings, staff.
- Dashboard with a 30-day revenue chart (server-rendered inline SVG), live
  counters, expiring services and items needing attention.
- Multi-admin receipt review: a decision edits **every** reviewer's copy in the
  bot so nobody acts twice.
- Order lifecycle controls: manual payment confirmation, retry provisioning,
  cancel with automatic wallet refund, and refund.
- User management: balance adjustment with a ledger entry, notes, direct
  message, block/unblock, test-service reset, CSV export.
- Reports: revenue over time, breakdowns by plan / payment method / panel, a
  payment ledger, and CSV exports (UTF-8 BOM so Excel renders Persian correctly).
- Broadcast with audience filters, per-audience recipient counts, scheduling and
  progress tracking.
- System event log and a full audit trail of operator actions.
- Staff management with three roles (owner/admin/support) and last-owner
  protection.
- Premium dark-first RTL design system with zero external dependencies — no CDN,
  no npm, no chart library.

### Added — Customisation

- **Button customisation**: label, colour (`primary`/`success`/`danger`/`link`)
  and premium custom emoji per button, with a live preview of each button and of
  the whole main menu.
- Premium emoji in message text via `{e:key}` placeholders resolved from the same
  catalog.
- Automatic graceful degradation: if Telegram rejects styling and custom emoji,
  the message is resent with a plain keyboard and styling is switched off instead
  of failing the sale.
- Every bot string is overridable from the panel without a redeploy.
- Connection guides and FAQ articles, grouped by section and platform, with a
  Telegram-accurate preview.

### Added — Operations

- **Domain or subdomain with automatic HTTPS.** Passing `--domain` to the
  installer adds a Caddy 2 container that issues a Let's Encrypt certificate on
  the first request and renews it automatically before expiry — no cron job and
  no `certbot renew` timer. `scripts/set-domain.sh` changes or removes the
  domain later, reports certificate status and forces a config reload.
- Docker Compose stack: PostgreSQL 16, Redis 7 (optional), the app and an
  optional TLS profile, with health checks and log rotation.
- One-command installer (Persian, interactive or fully scripted), plus
  `update.sh` and `uninstall.sh`.
- Alembic migrations with a verified clean `alembic check`.
- Automatic daily `pg_dump` backups with retention.
- APScheduler jobs: payment expiry, node health, service sync, provisioning
  crash recovery, scheduled broadcasts, expiry reminders, quota alerts, cleanup
  and backups.
- `/healthz` and `/readyz` endpoints.
- Optional webhook mode for Telegram and a signed inbound webhook for WG-Guard
  lifecycle events (HMAC-SHA256 with a replay window).
- GitHub Actions CI: lint, test against a real PostgreSQL service, and a Docker
  build.

### Added — Developer experience

- **Pluggable VPN backends.** `app/panels` is now a port-and-adapter boundary:
  a `PanelProvider` interface, canonical DTOs, a capability registry and a
  documented template (`providers/example.py`). Supporting PasarGuard, Marzban
  or x-ui means adding one adapter file — no business-logic or UI change.
  `Panel.kind` selects the adapter at runtime, so different plans can be served
  by different backends in the same shop.
- An admin page for managing panels (add, test connection, edit, toggle,
  delete) with the provider list and its capabilities.
- unit, integration against a real PostgreSQL, a panel smoke suite
  that renders every page, and a suite pinning the provider contract.
- In-repo mock WG-Guard node (61 of 63 documented endpoints) with fault
  injection and a request journal, so the whole flow is testable offline.
- `AGENTS.md` contribution rules, an architecture guide and a panel page
  contract.

### Security

- Fernet encryption of every stored secret, keyed by purpose
  (panel token, webhook secret, WireGuard config, subscription link).
- `scrypt` password hashing, signed session cookies, per-route CSRF checks and
  three-role authorisation with last-owner protection.
- Login rate limiting with an IP lockout.
- Money can only move through the ledger-backed wallet service; the balance and
  the ledger cannot disagree.
- Card payments never debit the wallet, preventing a double charge for customers
  who both top up and pay by card.

[1.0.0]: https://github.com/Sir-Adnan/wg-guard-bot/releases/tag/v1.0.0
