# Development

## Setup

```bash
git clone https://github.com/Sir-Adnan/wg-guard-bot.git
cd wg-guard-bot
python -m venv .venv
.venv/Scripts/activate          # Windows
source .venv/bin/activate       # Linux/macOS
pip install -r requirements-dev.txt
```

### Configuration

```bash
cp .env.example .env
```

For local work the minimum is:

```bash
ENV=development
SECRET_KEY=dev-only-secret-key-that-is-long-enough-0000000000
BOT_TOKEN=                      # leave empty: the panel still runs
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=55432             # if you use the compose database
REDIS_URL=                      # empty = in-memory FSM
```

`ENV=development` relaxes the `SECRET_KEY` check and generates one if missing;
`ENV=production` refuses to boot without a real key.

### Database for local work

The compose file already ships a PostgreSQL:

```bash
docker compose up -d db redis
docker compose exec db psql -U wgguard -d postgres -c "CREATE DATABASE wgguard_test"
```

Or start a throwaway one:

```bash
docker run -d --name wgguard-dev \
  -e POSTGRES_PASSWORD=wgguard -e POSTGRES_USER=wgguard -e POSTGRES_DB=wgguard \
  -p 55432:5432 postgres:16-alpine
```

### Run

```bash
alembic upgrade head
uvicorn app.main:app --reload --port 8080
```

The panel is at <http://127.0.0.1:8080/panel>. On the first start the owner
account is seeded from `OWNER_USERNAME` / `OWNER_PASSWORD`. Give the bot a
token and message it to exercise the bot side.

## Tests

```bash
# unit tests only — no database, no network
pytest tests/test_core.py -q

# everything (needs a database)
export TEST_DATABASE_URL="postgresql+asyncpg://wgguard:wgguard@127.0.0.1:55432/wgguard_test"
pytest -q

# with coverage
pytest --cov=app --cov-report=term-missing
```

Without a reachable database the `db`-marked tests **skip** instead of failing,
so `pytest` is useful on any machine.

### What each file covers

| File | Needs DB | Covers |
|---|---|---|
| `tests/test_core.py` | no | money conversion/formatting, password hashing, secret encryption, session signing, Jalali dates |
| `tests/test_wg_client.py` | no | the WG-Guard client against the in-repo mock: auth, idempotent purchases, pagination, error envelopes, retry on 503 |
| `tests/test_purchase_flow.py` | yes | pricing, wallet reservation/refund, provisioning, idempotent retries, receipts, renewals, ledger invariant |
| `tests/test_catalog_features.py` | yes | category tree (depth, cycles, cascade), gift codes, guides |
| `tests/test_panel.py` | yes | login, CSRF, every panel page renders 200, card creation round trip, receipt approval through the panel |

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
`mock_panel`) come from `tests/conftest.py`. Database fixtures **commit** like
production and every test starts from a `TRUNCATE` — services such as
provisioning open their own session and would not see uncommitted rows.

## Mock WG-Guard panel

`tools/mock_wg_panel` is an in-memory implementation of the upstream REST
contract (61 of 63 documented method/path pairs).

```bash
cd tools && python -m mock_wg_panel --host 127.0.0.1 --port 8787
```

It authenticates `wg_test_token` (all scopes) and `wg_readonly_token` (read
scopes) and offers test affordances:

| Endpoint | Effect |
|---|---|
| `POST /__mock__/reset` | wipe all state |
| `GET /__mock__/requests` | journal of every request received |
| `POST /__mock__/fail` | make the next N matching requests fail |
| `POST /__mock__/slow` | delay matching requests |
| `POST /__mock__/seed/plan` | add a plan |
| `POST /__mock__/expire` | force-expire a user |

In tests, inject it without a network via `httpx.ASGITransport`; the
`wg_client` fixture already does this.

## Lint and format

```bash
ruff check .
ruff format .
```

## Database migrations

```bash
alembic revision --autogenerate -m "add plan categories"
alembic upgrade head
alembic check                    # must report no pending operations
alembic downgrade -1             # verify the downgrade works too
```

Two tables reference each other (`orders.service_id` ↔
`services.origin_order_id`); the second FK uses `use_alter=True`, so
autogenerate cannot emit it and the initial migration creates it explicitly.
Keep that comment if you regenerate.

## Adding a feature

1. **Model** — `app/db/models.py`, then a migration.
2. **Service** — `app/services/<concept>.py`. Keep it free of Telegram and
   FastAPI imports; it should be testable with just a session.
3. **Panel page** — `app/web/routes/<name>.py` + `templates/<name>.html`, then
   register in `app/web/routes/__init__.py`. Follow
   [`PANEL-CONTRACT.md`](PANEL-CONTRACT.md).
4. **Bot screen** — `app/bot/handlers/<name>.py`, then register in
   `ROUTE_ORDER`. Add any new strings to `app/locales/fa.json` and any new
   buttons to `appearance._BUTTON_RAW`.
5. **Tests** — extend the closest existing file (see the table above).
6. **Docs** — update `CHANGELOG.md` and, if behaviour changed, the README.

## Debugging tips

- `LOG_LEVEL=DEBUG` and `DB_ECHO=true` make everything visible.
- `POST /__mock__/fail` is the fastest way to exercise the retry and
  recovery paths without touching real infrastructure.
- The panel keeps working without a bot token; that is deliberate and is
  covered by `tests/test_panel.py`.
- If a test hangs, it is almost always an un-closed `AsyncEngine`. Use
  `NullPool` in tests (the `engine` fixture does).

## Definition of done

- [ ] `ruff check .` passes.
- [ ] `pytest -q` passes (including the database tests).
- [ ] `alembic check` reports no drift.
- [ ] New panel pages appear in `tests/test_panel.py::PAGES`.
- [ ] `CHANGELOG.md` has an entry.
- [ ] No secret is logged, no money is a float, no layering rule is broken.
