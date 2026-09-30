# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

- Redesign the entire admin surface with the ivory/navy Atelier experience,
  local Persian typography, role-aware navigation, responsive drawer/icon rail,
  section search, shared page headings and consistent cards/forms/tables/states.
  Preserve ordering and menu-builder contracts, improve modal keyboard behavior
  and confirmation feedback, and protect logout with CSRF. See [PANEL-DESIGN.md](PANEL-DESIGN.md).
- Follow the October WG-Guard contract: honor 64-byte UTF-8 template names,
  recreate a template when duration must become unlimited, verify patch results,
  and preserve all device IDs/configs during atomic purchase recovery. Initial
  allocation stays separate from the device cap. Align the mock's duration,
  metadata and statistics behavior with the official API.

- Align WG-Guard integration with technical templates and current units. Freeze
  paid-order terms and request identity; preserve uncertainty during recovery;
  serialize provisioning across durable checkpoints and node capacity checks.
- Queue paid successors for renewal; route automatic-renew callbacks through
  checkout. Use recoverable atomic quota top-ups and an explicit volume checkout.
  Block unrecoverable paid device orders and retries beyond result retention.
- Keep wallet/ledger mutations atomic, reject underpaid/expired payments and
  invalid refunds, reserve catalog stock transactionally, and refund expired
  reservations once. Preserve orders awaiting receipt review.
- Apply exact, idempotent order rewards across payment flows. Correct decimal
  speed/volume display and tri-state limits. Reconcile unordered signed webhooks,
  deduplicate transactionally and avoid persisting raw credential-bearing errors.
- Add contract, concurrency, recovery, payment, stock and webhook regression
  coverage. See [the integration audit](WG-GUARD-AUDIT.md) for operating notes.

The whole operator surface inside the bot was unreachable, and the physical
keyboard crashed on some buttons. Both had one root cause each, and both now have
a test that runs a real update through the assembled dispatcher. The control menu
had the same shape of problem — started the way the README suggests, it could not
find the project it was managing — and the test suite on Linux found a third: the
call that records a failed bot start-up raised on the way in.

- **Staff buttons answer again: receipts, users, orders, broadcast.**  aiogram
  evaluates a handler's filters *before* it runs the observer's **inner**
  middlewares, and `ContextMiddleware` — which resolves who is talking —
  was registered as an inner middleware.  So `data["staff"]` did not exist yet
  when `IsStaff()` asked for it, every staff-only filter answered "no", and the
  catch-all handed the operator «این دکمه دیگر معتبر نیست» on a receipt they were
  supposed to approve.  Identity is now resolved by **outer** middlewares, which
  run before filter evaluation. Nothing else about the middleware order changed.

- **Alerts read like sentences, not like source code.**  Telegram does not parse
  HTML in ``answerCallbackQuery``, so a popup built from a screen's text showed
  the tags themselves — `<b>این دکمه دیگر معتبر نیست</b>`, and with premium emoji
  configured, a raw `<tg-emoji>` element. Every alert now goes through
  `app.bot.utils.alert_text`, which strips the markup, keeps the Unicode emoji,
  collapses the line breaks and trims to Telegram's limit.

- **A physical-keyboard press no longer crashes a screen.**  Seven buttons were
  built with `kb.add(..., event=…)`, a keyword the keyboard builder has never
  accepted — `TypeError: KeyboardBuilder.add() got an unexpected keyword argument
  'event'` for the empty-services screen and for the profile and channels
  screens. Every reply button is now rendered by the test suite.

- **Typing a question gets an answer again.**  The reply-keyboard router matched
  *every* stateless text message and then ignored the ones that were not a menu
  label — and a matched handler ends the dispatch, so the fallback never ran and
  the customer was met with silence. The label check is now a filter
  (`ReplyButton`), which claims the message only when it really is a button.

- **The physical keyboard has colours and premium emoji.**  `KeyboardButton`
  carries `style` and `icon_custom_emoji_id` in the same Bot API version that
  added them for inline buttons, so the reply keyboard can look like the rest of
  the bot instead of plain text. Both variants are accepted as a route, because a
  keyboard already on a phone may have been drawn either way — and the notifier
  falls back to the unstyled twin when a Bot API server rejects the fields.

- **An unfinished order is no longer a dead end.**  «یک سفارش ناتمام برای این پلن
  دارید» offered a receipt button and a cancel button, which are the wrong two
  answers for an order that has not reached the receipt step. It now offers
  «ادامه سفارش» — which lands on the *right* step for that order (payment method,
  awaiting review, in progress, or retry) — and «سفارش تازه», which closes the
  unpaid order and starts a clean one. An order that was already paid for is never
  thrown away by that button: card money is not in the wallet, so cancelling it
  needs a human.

- **A sub-category shows its own services.**  «پیشنهاد ویژه» and «همه سرویس‌ها»
  are doors out of the shop; they belong to the front page and no longer follow
  the customer down into every category, where they buried the sub-categories.

- **`appearance.plan_columns` now does something, and category rows are
  configurable.**  The setting was declared in the panel and read by nothing.
  Plan lists honour it, and the new «تعداد دسته‌بندی در هر ردیف»
  (`appearance.category_columns`, 1–4, default 2) lays out category and
  sub-category buttons.

- **The control menu is no longer lost when it is started from outside the
  project.**  `bash <(curl …/menu.sh)` gives the script no path of its own
  (`/dev/fd/63`), and the menu used that path as the project directory: on an
  installed server item 2 answered «This server is not installed yet — no .env in
  /dev/fd», `sudo` was handed an `/dev/fd` path it cannot read, and item 1 said
  `install.sh was not found in /dev/fd`.  The menu now resolves the project from
  `--dir`, the current directory, its own directory, then `~/wg-guard-bot`; it
  copies itself somewhere readable before re-running under `sudo`; and when a
  sibling script is missing entirely (`install.sh` on a fresh server) it fetches
  the released copy and runs it — pointed at the directory it manages.

- **A failed operator action comes back to the menu.**  A child script that exits
  non-zero ended the whole session silently, because `set -e` was applied to the
  call.  The child's status is now returned: the menu says what did not finish and
  redraws, while `bash menu.sh update` still exits non-zero for a script that
  calls it.

- **Recording a failure can no longer fail.**  `notifier.record_event` demands an
  `EventLevel` but asked whatever it was given for `.value`, and four call sites
  passed the string an error path has at hand — `"critical"` from a failed bot
  start-up, `"warning"` from the bot's error handler, the receipt notifier and the
  membership check.  The sink now accepts either form (and never raises over an
  unreadable one), and the call sites pass the enum.  Found by the test suite on
  the Linux CI runner, where a rejected token actually reaches that branch.

- **`tools/menu_harness.sh` — the control menu is tested, not just parsed.**
  Seventeen assertions across the ways an operator really starts it: inside a
  checkout, from another directory, through the README's `curl` pipe, as a normal
  user, with a failing child, and on a server where nothing is installed.  Docker,
  `curl` and root are stubbed, so it runs in CI with no daemon and no network —
  and it fails 12 of those assertions against the release before this change.

Operator-facing text is English everywhere: the installer, updater and uninstaller
no longer print Persian, which Linux terminals render reversed (the Makefile help,
`.env.example` and `docker-compose.override.example.yml` comments follow). Bot copy,
panel labels and customer messages stay Persian — that is what customers read.

Also on `main` since 1.0.0:

- **`bash menu.sh` — one control menu for the whole server.**  Install, update,
  status, live logs per service (`bot` / `db` / `redis` / `caddy` / all, streaming
  or a snapshot), database + `.env` backups, restore with a safety dump, domain and
  certificate status, panel-password reset, maintenance (migrate, restart, rebuild,
  prune, shell, psql) and uninstall — every item is also a command
  (`bash menu.sh logs bot`), so nothing is menu-only.

- **An old `.env` can be repaired in place.**  Installations from before 1.0.0
  copied a Persian `.env.example`, and a Linux terminal renders those comments
  backwards.  Menu item 9 rewrites the file from the current English template:
  values are carried across byte for byte, missing settings are filled in, extra
  keys are appended, the old file is kept as `.env.bak.<timestamp>`, and the action
  is a no-op the second time.  `APP_NAME` keeps its Persian value — that is the
  shop name customers read.

- **An update can no longer be blocked by its own backup file.**  `update.sh` wrote
  its pre-update dump into the project root, where the next run saw an untracked
  file as a local code change, stopped to ask about stashing it, and — if the
  answer was no — left the checkout on the old revision.  Dumps now go to
  `./backups/`, and only *tracked* edits count as local changes.

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
  The test suite now runs the real startup lifespan, which it never did before.

- **An image reports the revision it was built from.**  `install.sh` and
  `update.sh` pass the checkout's commit as the `GIT_COMMIT` build arg, `/healthz`
  answers with it as `"commit"`, and the startup log prints it.  `update.sh` also
  compares the two and **fails** the update when the container is still on the
  previous revision — an update that pulled code but never rebuilt the image used
  to look exactly like a fix that did not work.  `"unknown"` means the image was
  built without the stamp.

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

- **«پیام همگانی» works — from the bot and from the panel.**  The audience button
  packed `hash(key) & 0xFFFF`, which Python reads as
  `hash(key) & (0xFFFF % len(keys))`; `0xFFFF % 5` is `0`, so *every* broadcast
  went to every user whatever group was chosen.  The bot now packs the audience
  **index** and says so when a stale button carries one it does not know.  A
  message composed inside the bot is stored on the campaign (`source_chat_id` /
  `source_message_id`, migration `c4b7d1e90a35`) and delivered with
  `copyMessage`, so premium emoji, formatting, media and the forwarded post's
  inline keyboard arrive exactly as composed; the panel's form renders
  `{e:key}` into the configured premium emoji and fills `{name}`/`{shop}` per
  recipient instead of sending the placeholders literally, and grew a
  `label | url` keyboard editor (http/https only, six buttons, two per row).
  The bot flow is compose → audience (with live counts) → preview (test send,
  send, cancel) → progress with a cancel button, every screen carries a next
  step, and starting a campaign with no bot bound explains itself in a Persian
  flash instead of answering a 500.  The panel copy no longer promises that a
  canceled campaign continues «از همانجا که مانده»: there is no per-recipient
  table, so the button says «ارسال دوباره», warns that the message goes to the
  whole audience again, and the bot's status screen says the same.

- **A receipt can only be decided once.**  `approve()` and `reject()` read the
  status, decided in Python, and wrote it back, so two reviewers pressing at the
  same moment both saw `pending` — and a wallet top-up was credited **twice**.
  The decision is now a single conditional `UPDATE … WHERE status = 'pending'`,
  so exactly one caller wins the row and the other is told who decided.  The
  reviewer's copies also keep their promise: an approved text receipt used to
  hold on to its approve/reject buttons (`reply_markup=None` leaves the old
  keyboard in place), and the «رسید جدید» header was printed twice.  Captions are
  fitted to Telegram's 1024-character limit — an over-long one made the whole
  send fail, which left the reviewer with no receipt at all.

- **The main menu is the owner's, and it can be a physical keyboard too.**  The
  layout used to be a hard-coded tuple in `app/bot/menus.py`: the order was fixed,
  a button could not be hidden, and the shipped buttons were the only ones
  available.  It now lives in the database as *overrides* of that default
  (`menu_layout`, migration `a1d0c7f4b2e6`) — moved, hidden and added buttons are
  stored, everything the owner did not touch keeps following the code, and a later
  release can therefore introduce a menu entry without a migration or an owner
  action.  The panel's buttons page grew a drag-and-drop editor for it (rows,
  order, hide, add from a palette of unused buttons, reset).  Because the layout
  is one source of truth, the same buttons can also be drawn as a Telegram
  **reply keyboard** («کیبورد فیزیکی», off by default): it sits under the input
  field, survives scrolling, and a press is routed by its exact label back to the
  same screen the inline button opens.  Turning the feature off never strands a
  keyboard that is already on a customer's phone, and a test fails if any button
  on it has no screen behind it.

- **The main-menu editor saves the whole menu at once.**  The buttons page renders
  the layout as rows of chips — server-side, so it reads and saves without
  JavaScript — next to a palette of the buttons that are outside the menu: a
  removed button is only *hidden*, and waits there to be dragged (or clicked) back
  into a row.  «افزودن ردیف» adds a row, «بازگردانی چیدمان پیش‌فرض» goes back to the
  shipped menu.  Dragging and the ✕ edit the page; «ذخیره چیدمان» posts the
  arrangement to `POST {panel_prefix}/buttons/layout`, and an answer that is not
  `ok` — a refused key, an expired session — puts back the menu the server still
  has, so an unsaved arrangement is never left on screen looking saved.
  `Alt`+arrows move a focused chip and save through that same path, and a plain
  form post (repeated `keys` fields split by a `row_break` marker, plus a
  `remove_key` submit) keeps the editor usable with no scripts at all.

- **Volume reads the same in the bot and on the node.**  A plan for "50 GB" was
  created on WG-Guard as 50 × 1024³ bytes, which the vendor panel prints as
  **53.7 GB** — the same traffic in a different unit.  `shop.traffic_unit` now
  chooses the basis (1024³, the historical default, or 1000³ to match the node's
  own display), the plan form says which one is in force, and a change applies to
  everything sent to a node afterwards.  The plans list now labels the node-side
  plan (`پلن نود: 01a0f170…`, or «هنوز روی نود ساخته نشده») so the two records read
  as one product, and every row gained **«بهروزرسانی نود»**: it pushes the current
  terms — volume, duration, device and speed limits, interface — to the node
  immediately instead of waiting for the next sale, updating the same node plan
  rather than creating a second one.

- **A node that answers a shape this build does not know can no longer fail a
  sale.**  WG-Guard serialises "not set" as JSON `null`, so a strict `list[str]`
  field rejected the entire read-back; `_safe_get_user` only caught `PanelError`,
  so the exception escaped, marked an already-committed purchase as failed, and
  showed the operator a raw pydantic dump — while a real VPN account existed on
  the node.  `null` now means "use the default" for every response model, an
  unknown vendor status name is mapped instead of rejected, and every best-effort
  read-back tolerates any exception.  While in there: retrying after an ambiguous
  transport failure rotated the idempotency key, which is how one order becomes
  two VPN accounts; the key now follows the payload and only rotates after the
  node definitively refuses it.

- **Receipt photos reach the reviewers.**  `send_photo` has no
  `link_preview_options` parameter, so every media receipt raised `TypeError`
  inside the notifier before a single byte left the process: the customer's
  receipt was stored, and no admin ever saw it.

- **«بازگشت» goes where it says.**  Two back buttons shipped pointing at callback
  actions no handler filtered on — the one on every plan card (`pl:page`) and the
  one on «همه سرویسها» (`ct:root`) — so the button only spun.  Every parent
  payload is now built in one place (`app/bot/nav.py`), the plan card returns to
  the category (and page) it came from, the services list keeps its page, guides
  return to their section and tickets to the ticket list, and a payload nothing
  answers is caught by a new catch-all that says «این دکمه دیگر معتبر نیست» and
  hands back the main menu instead of leaving the customer with a spinner.
  A sub-category level that held a single plan used to hide its children
  completely; a node now shows its plans *and* its sub-categories.  `tests/test_bot_nav.py`
  runs every navigation payload through the real dispatcher, so the next dead
  button fails the suite.

- **Ordering is drag-and-drop.**  Every list that already had a `sort_order`
  column (plans, categories, channels, cards, guides, panels) is reordered by
  dragging a row — or with `Alt`+`↑`/`↓` on the focused handle — through one
  generic endpoint (`POST /panel/reorder`) and one service
  (`app/services/ordering.py`, dense gap-10 renumbering, scope-aware, one audit
  entry per change).  The free-typed "ترتیب نمایش" number boxes are gone from the
  forms and new rows append to the end; the numeric fields that remain
  (`panels.priority`) explain what they do.  `tests/test_panel_drag_contract.py`
  parses the rendered HTML and asserts the drag container really owns its rows —
  the first version put `data-drag-list` on the `<table>`, where the script found
  no items and dragging silently did nothing.

- The test suite builds its schema from a complete model registry.  A db test
  module that did not happen to import `app.db.models` made the session-scoped
  `create_all` run against empty metadata, and every later `TRUNCATE` failed with
  "relation users does not exist".

- **Plans are chosen for a category from the category page.**  Every row of the
  tree carries «انتخاب پلن‌ها», which opens the full plan list with the plans of
  that category already ticked — price, volume and the category each plan sits in
  today — and saving posts the whole selection: ticked plans are attached,
  unticked ones are detached to «بدون دسته», and a plan of another category is
  never touched.  One audit entry records the change, and the flash message says
  what happened in the operator's words («۳ پلن به دستهٔ «آلمان» وصل شد و ۱ پلن
  جدا شد.»).  A branch that only holds sub-categories no longer reads «۰ پلن»:
  the tree shows the whole subtree and names the part it does not hold itself
  («۵ پلن (۲ در زیردسته‌ها)»).  The plans list also gained a category filter —
  «همه دسته‌ها», every category (descendants included) and «بدون دسته» — which
  survives pagination and reordering.

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
  `docs/upstream-api/wg-guard-openapi.json` including the error envelope and
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
