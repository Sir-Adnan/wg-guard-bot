# Development

Setting up, running, and debugging this project locally.

For *what to run as evidence* see [`VERIFICATION.md`](VERIFICATION.md). This file
is about getting a working environment and finding your way around it.

---

## 1. Setup

```bash
git clone https://github.com/Sir-Adnan/wg-guard-bot.git
cd wg-guard-bot
python -m venv .venv
.venv/Scripts/activate          # Windows
source .venv/bin/activate       # Linux/macOS
pip install -r requirements-dev.txt
```

`requirements-dev.txt` includes the runtime requirements plus `pytest`,
`pytest-asyncio` and `ruff`. The production image installs **only**
`requirements.txt`, so it has neither pytest nor ruff — see §5.

## 2. Local configuration

```bash
cp .env.example .env
```

For local work the minimum is:

```bash
ENV=development
SECRET_KEY=dev-only-secret-key-that-is-long-enough-0000000000
BOT_TOKEN=                      # leave empty: the panel still runs
REDIS_URL=                      # empty = in-memory FSM
```

`ENV=development` relaxes the `SECRET_KEY` check and generates one if missing;
`ENV=production` refuses to boot without a real key. The full variable list is in
[`CONFIGURATION.md`](CONFIGURATION.md).

## 3. A database for local work

`docker-compose.yml` deliberately does **not** publish the `db` port — a
production host should not expose PostgreSQL. For host-side work, run a
throwaway instance on a non-standard port (`make test-db` does exactly this):

```bash
docker run -d --name wgguard-pg -p 55432:5432 \
  -e POSTGRES_USER=wgguard -e POSTGRES_PASSWORD=wgguard -e POSTGRES_DB=wgguard \
  postgres:16-alpine
docker exec wgguard-pg psql -U wgguard -d postgres -c "CREATE DATABASE wgguard_test"
```

Then point the test-suite at it (see [`VERIFICATION.md`](VERIFICATION.md) §4 for
the full environment block):

```bash
TEST_DATABASE_URL="postgresql+asyncpg://wgguard:wgguard@127.0.0.1:55432/wgguard_test"
```

Remove the container when you are done with it. Leaving throwaway containers and
images behind is a handoff defect, not a convenience.

## 4. Run

```bash
alembic upgrade head
uvicorn app.main:app --reload --port 8080
```

The panel is at <http://127.0.0.1:8080/panel>. On first start the owner account
is seeded from `OWNER_USERNAME` / `OWNER_PASSWORD`. Give the bot a token and
message it to exercise the Telegram side.

## 5. Commands

**Run these from the host venv.** `make dev-venv` creates it, `make test-db`
starts the database from §3, and `make test` / `make lint` / `make format` use the
same venv — the runtime image deliberately ships no development tooling, so those
targets never touch Docker.

```bash
ruff check .                     # lint (the CI gate); path arguments work too
ruff format .                    # format
pytest -q -m "not db"            # 73 tests, no database
pytest -q                        # 172 tests: app suite + mock panel suite
pytest tests/test_core.py -q     # one module
alembic upgrade head             # apply migrations
```

Sample data and a fake node, when you need them:

```bash
cd tools && python -m mock_wg_panel --host 127.0.0.1 --port 8787
```

## 6. Test map

Pick the file that already covers your area before writing a new one.

| File | Needs DB | Covers |
|---|---|---|
| `tests/test_core.py` | no | money conversion/formatting, digit translation, password hashing, secret encryption, session signing, CSRF binding, Jalali dates |
| `tests/test_wg_client.py` | no | the WG-Guard transport against the in-process mock: auth, idempotent purchases, pagination, error envelopes, retry on 503 |
| `tests/test_bot_wiring.py` | no | dispatcher/router composition, idempotent startup, handler order |
| `tests/test_purchase_flow.py` | yes | pricing, wallet reservation and refund, provisioning, idempotent retries, receipts, renewals, ledger invariant |
| `tests/test_catalog_features.py` | yes | category tree (depth, cycles, cascade), gift codes, guides, catalog filters |
| `tests/test_panel_providers.py` | yes | the provider port: registry, capability flags, canonical mapping, exactly-once purchase |
| `tests/test_panel.py` | yes | login, CSRF, every page renders, card round trip, receipt approval through the UI |
| `tools/mock_wg_panel/` | no | the mock node's own smoke suite; `testpaths` collects it with everything else |

The database marker is `@pytest.mark.db` (module-level `pytestmark`). Without a
reachable PostgreSQL those tests are **skipped**, not failed — `pytest -q` reports
73 passed / 99 skipped in about four seconds, and the session header names the
database it probed. Set `REQUIRE_DB=1` (CI does it for you) when a skip must be an
error instead.

### Writing a test

```python
import pytest
from app.services.orders import order_service

pytestmark = pytest.mark.db          # only if it needs PostgreSQL


async def test_something(session, customer, plan) -> None:
    order = await order_service.create(session, customer, plan)
    assert order.payable_rial == plan.price_rial
```

Fixtures (`session`, `customer`, `plan`, `panel_row`, `admin`, `wg_client`,
`mock_panel`) come from `tests/conftest.py`.

Two things about that fixture model are load-bearing:

- Database fixtures **commit** like production, and each test starts from a
  `TRUNCATE`. Services such as provisioning open their own session and would not
  see uncommitted rows, so a rollback-per-test wrapper would silently break them.
- The session fixture takes a PostgreSQL advisory lock. Two `pytest` runs against
  one `TEST_DATABASE_URL` would otherwise recreate the schema underneath each
  other. A second run now waits instead of corrupting the first — do not remove
  the lock to "speed things up".

## 7. Migrations

```bash
alembic revision --autogenerate -m "add plan categories"
alembic upgrade head
alembic check                    # must print "No new upgrade operations detected."
alembic downgrade -1             # then upgrade again: the downgrade must work
```

Two tables reference each other (`orders.service_id` ↔ `services.origin_order_id`).
The second FK uses `use_alter=True`, so autogenerate cannot emit it and the
initial migration creates it by hand. If you regenerate migrations, keep that
comment and that call.

Never edit a migration that has been released; add a new revision.

## 8. Adding a feature

The mechanics live next to the thing you are changing — follow the pointer
rather than duplicating the contract here:

| Adding | Start at |
|---|---|
| A panel page | [`PANEL-CONTRACT.md`](PANEL-CONTRACT.md) — it ends with the "done" checklist |
| A VPN backend or node call | [`PROVIDERS.md`](PROVIDERS.md) |
| A bot screen | `app/bot/handlers/`, then append to `ROUTE_ORDER` in `handlers/__init__.py` |
| A shop setting | one `SettingSpec` in `app/services/settings_store.py`; the panel renders it |
| A bot string | one key in `app/locales/fa.json` — copy rules in `AGENTS.md` §6, the full guide in [`UX-WRITING.md`](UX-WRITING.md) |
| A button colour or emoji | one entry in `_BUTTON_RAW` in `app/services/appearance.py` |
| A table | a model in `app/db/models.py`, then a migration (§7) |
| A scheduled job | a method on `Jobs` plus one `scheduler.add_job(...)` line |

Services stay free of Telegram and FastAPI imports so they remain testable with
nothing but a session — that is the layering rule in `AGENTS.md` §1.3, and it is
what keeps the focused tests in §6 cheap.

## 9. Debugging

- `LOG_LEVEL=DEBUG` and `DB_ECHO=true` make everything visible.
- `POST /__mock__/fail` on the mock node is the fastest way to exercise retry and
  recovery paths without real infrastructure.
- The panel keeps working without a bot token. That is deliberate and covered by
  `tests/test_panel.py`.
- If a test hangs it is almost always an unclosed `AsyncEngine`; the `engine`
  fixture uses `NullPool` for this reason.
- `POST /panel/...` returning 403 in a test is usually a missing CSRF token, not
  an auth failure.

## 10. Repository conventions

```
app/core/       config, logging, security, money, jalali, cache, errors
app/db/         models, session, base
app/panels/     the VPN-backend port (see PROVIDERS.md)
app/services/   business logic — one module per concept
app/bot/        handlers/, middlewares/, keyboards, callbacks, states, setup
app/web/        app, routes/, templates/, static/
app/workers/    jobs, scheduler
app/locales/    fa.json
tools/          mock WG-Guard node and developer utilities (not shipped)
```

`ruff check .` and `ruff format .` must both pass; CI gates on `check` and treats
formatting as informational, so run `ruff format .` before you push.
