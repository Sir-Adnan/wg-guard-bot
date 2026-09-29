# AGENTS.md

Rules for anyone — human or AI agent — changing this repository. Read this
before opening a pull request. It is deliberately short and specific; when a
rule here conflicts with your instinct, the rule wins.

---

## 1. The five non-negotiables

1. **Money is Rial, everywhere, as an integer.**
   Database columns, service signatures and API payloads all use Rial. Toman is
   a *display* concern handled by `app/core/money.py`. A new `*_rial` column that
   is not `BigInteger`, or a service that returns Toman, is a bug.
   Never use floats for money.

2. **No secret is ever stored in plaintext.**
   Panel API tokens, WireGuard configs and subscription links go through
   `app.core.security.encrypt_secret(..., purpose=...)`. Each secret has its own
   `purpose`; never reuse one. Never log a secret, never render it in a
   template without `mask_secret`, never put it in an error message.

3. **The layering is one-way.**
   `core → db / panels → services → bot / web`. Nothing lower may import
   anything higher. If you feel you need to, the logic belongs one layer down.

4. **Only `app/panels/providers/*` speaks HTTP to a VPN node.**
   Business rules never build URLs and never import a vendor client. If you
   need a new node call, add it to the `PanelProvider` port
   (`app/panels/base.py`) and implement it in every adapter.

5. **Every behaviour change ships with a test.**
   See §6. A PR that changes money, provisioning or auth without a test will be
   sent back.

## 2. Environment

| | |
|---|---|
| Python | 3.12 in Docker, 3.11+ supported |
| Package manager | `pip` with pinned `requirements.txt` |
| Database | PostgreSQL 16 (`asyncpg` + SQLAlchemy 2.x async) |
| Bot | aiogram 3.x, **Bot API 9.5** |
| Web | FastAPI + Jinja2, no build step, no CDN, no npm |

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
source .venv/bin/activate       # Linux/macOS
pip install -r requirements-dev.txt
```

## 3. Code style

- `ruff check .` and `ruff format .` must pass (config in `pyproject.toml`).
- `from __future__ import annotations` at the top of every module.
- Full type hints on public functions. No bare `except:`; catch the specific
  exception, or `AppError` for domain failures.
- Docstrings explain **why**, not what. A docstring that restates the function
  name is noise.
- Prefer small, named helpers over long functions. If a function needs a
  paragraph to explain, split it.
- Comments are for non-obvious decisions. Keep the existing bilingual comments
  where they explain a trap (they are there for a reason).

### Module layout

```
app/core/       config, logging, security, money, jalali, cache, errors
app/db/         models, session, base
app/panels/     the VPN-backend port: base (interface), models (canonical DTOs),
                registry, manager, providers/<vendor>.py adapters
app/services/   business logic — one module per concept
app/bot/        handlers/, middlewares/, keyboards, callbacks, states, setup
app/web/        app, routes/, templates/, static/
app/workers/    jobs, scheduler
app/locales/    fa.json
```

## 4. Adding things

| To add… | Do this |
|---|---|
| a bot screen | `app/bot/handlers/<name>.py` exposing `router`; append the module to `ROUTE_ORDER` in `handlers/__init__.py` |
| a panel page | `app/web/routes/<name>.py` exposing `router`; append the name to `ROUTE_MODULES`; add `templates/<name>.html` extending `base.html` |
| a shop setting | one `SettingSpec` in `app/services/settings_store.py` — the panel renders it automatically |
| a bot string | one key in `app/locales/fa.json`; never hard-code user-facing Persian in a handler when a key would do |
| a button colour/emoji | one entry in `_BUTTON_RAW` in `app/services/appearance.py` |
| a table | a model in `app/db/models.py`, then `alembic revision --autogenerate -m "…"` |
| a scheduled job | a method on `Jobs` + one `scheduler.add_job(...)` line |
| a WG-Guard call | a typed method on `WGGuardClient` + a schema in `panels/schemas.py` |
| **a new VPN backend** | `app/panels/providers/<name>.py` implementing `PanelProvider`, decorated with `@register`; import it in `providers/__init__.py`. Copy `providers/example.py`. **No business logic changes.** |

### Adding a panel provider

`app/panels` is a port-and-adapter boundary. The domain talks to `PanelProvider`
and the canonical models in `app/panels/models.py` — never to a vendor payload.

1. Copy `app/panels/providers/example.py` to `providers/<vendor>.py`.
2. Implement the abstract methods against that vendor's HTTP API, mapping every
   response into the canonical models. Keep vendor-only fields in `.raw`.
3. Declare only the capabilities you really support (`CAP_*` in
   `app/panels/base.py`). A partial backend is fine; the domain degrades.
4. Import the module in `app/panels/providers/__init__.py` so `@register` runs.
5. Run `pytest tests/test_panel_providers.py` — it pins the contract.

The admin panel's "panel type" dropdown is generated from the registry and
`Panel.kind` selects the adapter at runtime, so no UI change is needed either.

Follow [`docs/PANEL-CONTRACT.md`](docs/PANEL-CONTRACT.md) for anything under
`app/web`.

## 5. Persian copy rules

The product is for Persian speakers; the wording is part of the product.

- Write natural, polite Persian — not machine translation. No
  «کاربر گرامی», no «لطفاً منتظر بمانید».
- Use **ZWNJ** (`\u200c`) correctly: میشود، سرویسها، نمیتوانید.
  Write it as a literal character (this repo's tooling preserves it); if your
  editor strips it, inject it explicitly and verify.
- Persian digits in user-facing text (۱، ۲، ۳). Use `fa_digits` or the `|fa`
  filter — never hard-code them.
- Messages that contain HTML must use only Telegram's subset:
  `<b> <i> <u> <s> <code> <pre> <a> <blockquote> <tg-emoji>`. **Never** Markdown
  (`*`, `_`, `#`, `[text](url)`) — `parse_mode` is HTML.
- Escape user-provided values with `html_escape` (bot) or `|e` (templates)
  before embedding them in a message.
- Button labels come from the appearance catalog, not from handlers, so the
  owner can rename them from the panel.

## 6. Testing

```bash
# unit + panel tests (no database needed)
pytest tests/test_core.py

# everything (needs a database)
export TEST_DATABASE_URL="postgresql+asyncpg://user:pass@127.0.0.1:5432/wgguard_test"
pytest -q
```

- New service logic → extend `tests/test_purchase_flow.py` or add
  `tests/test_<area>.py`.
- New panel page → it must be reachable from `PAGES` in `tests/test_panel.py`.
- New WG-Guard call → test it against `tools/mock_wg_panel`, never the network.
- Fixtures commit like production; each database test starts from a `TRUNCATE`.
  Do not reintroduce a rollback-per-test wrapper — services open their own
  sessions and will not see uncommitted rows.

## 7. Database changes

```bash
alembic revision --autogenerate -m "add plan categories"
alembic upgrade head
alembic check          # must print "No new upgrade operations detected."
```

- Never edit a migration that has been released; add a new one.
- Never drop a column in the same release that stops writing to it.
- Two tables reference each other (`orders.service_id` ↔ `services.origin_order_id`).
  The second uses `use_alter=True`, so autogenerate cannot emit it — the
  initial migration creates it by hand. Keep that comment.

## 8. Working with WG-Guard's API

The contract is `docs/upstream-api/openapi-wg-guard.json`. It is
**additive-only**: never assume a field is absent, and tolerate unknown fields
(`extra="allow"` in every schema).

- `POST /api/v1/purchases` is idempotent for 90 days. **Reuse the same
  `Idempotency-Key` when retrying the same payload.** Only derive a new key
  (`<base>-r1`) after the node has *definitively* refused the body.
- After an ambiguous failure (timeout, 5xx) ask `GET /api/v1/operations/result`
  before retrying.
- Never retry a mutation that has no idempotency key and is not naturally
  idempotent (`/devices/{id}/regenerate`, for example).
- `config` and `subscription` responses are secrets: `Cache-Control: no-store`,
  never logged, always encrypted at rest.

## 9. Git & pull requests

- Branch names: `feat/<topic>`, `fix/<topic>`, `docs/<topic>`, `chore/<topic>`.
- Commit messages: [Conventional Commits](https://www.conventionalcommits.org/),
  imperative mood, ≤ 72 characters in the subject.

  ```
  feat(shop): category tree with nested sub-categories
  fix(provisioning): reuse the idempotency key on transport retry
  docs: document the panel page contract
  ```

- One logical change per commit. Do not mix formatting with behaviour.
- A PR must state: what changed, why, how it was verified, and any migration or
  environment change. Green CI is a precondition, not a result.
- Never commit `.env`, `backups/`, `logs/`, or anything in `.gitignore`.

## 10. Things that will get a PR rejected

- Money as a float, or Toman in the database.
- A plaintext secret, a secret in a log line, or a secret in an error message.
- A new import from a higher layer (e.g. `services` importing `app.bot`).
- A migration that was edited after release, or a model change with no migration.
- Markdown formatting in bot text, or unescaped user input in an HTML message.
- A hard-coded button label or emoji id in a handler instead of going through
  `app/services/appearance.py`.
- Comparing money with `==` after a division instead of using the helpers.
- Removing or weakening the styled-keyboard fallback in `notifier`.
- `except Exception: pass`.

## 11. Where to look

| Question | File |
|---|---|
| How is the code organised? | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| How do I write a panel page? | [`docs/PANEL-CONTRACT.md`](docs/PANEL-CONTRACT.md) |
| What does each setting do? | [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) |
| How do I deploy it? | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |
| What changed between versions? | [`docs/CHANGELOG.md`](docs/CHANGELOG.md) |
| How do I report a vulnerability? | [`docs/SECURITY.md`](docs/SECURITY.md) |
| What does the upstream panel offer? | [`docs/upstream-api/openapi-wg-guard.json`](docs/upstream-api/openapi-wg-guard.json) |
