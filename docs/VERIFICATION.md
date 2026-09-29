# Verification

What evidence a change needs, and what it does not. The goal is a fast inner
loop that still refuses to ship a broken money path.

This is the authoritative document for "what do I run?". `AGENTS.md` §4 is the
one-screen summary.

---

## 1. Why this document exists

The project's own history shows where verification time actually goes. Each
item below is observable in the repository, not a guess:

| Observation | Cost |
|---|---|
| The full suite needs a live PostgreSQL, and the autouse `database_schema` fixture drops and recreates ~28 tables on **every** `pytest` invocation — including runs that select no database test. | A one-line helper change pays full DB setup. |
| `tests/conftest.py` defines `requires_db`, but **no test uses it**. The `db` marker is only a label, so database tests fail rather than skip when PostgreSQL is absent. | A machine without a database produces failures that look like regressions. |
| `make test`, `make lint` and `make format` run `pytest`/`ruff` **inside the runtime container**, but the image installs only `requirements.txt` — `pytest` and `ruff` live in `requirements-dev.txt`. | Three documented commands fail; the agent then improvises and re-derives environment variables by hand. |
| `tools/mock_wg_panel` sits outside `testpaths`, is excluded from ruff, and needs `PYTHONPATH=tools`. | A second suite with its own incantation that is easy to forget. |
| Two `pytest` runs against one `TEST_DATABASE_URL` recreate the schema concurrently and destroy each other's state. | Very confusing intermittent failures (now serialised by an advisory lock in `conftest.py`). |
| An earlier revision of `docs/DEVELOPMENT.md` claimed database tests skip without a database, and documented `pytest --cov`, which is not an installed dependency. | Followers lose a cycle on a command that cannot work. |

None of this is fixed by reading more carefully. It is fixed by **choosing the
narrowest check that can actually falsify your change**, and by not repeating
work whose validity has not expired.

## 2. Principles

1. **Evidence is proportional to risk, and to the blast radius.** A CSS tweak and
   a change to `order_service.mark_paid` do not get the same treatment.
2. **Smallest sufficient evidence first.** Run the narrow check. Widen only if it
   fails, or if you cannot construct a narrow check that would catch the mistake
   you are worried about.
3. **Evidence belongs to a tree state.** A green run is valid for the exact
   working tree it ran against. If nothing executable changed since, cite it —
   do not re-run it. "I ran the full suite at `abc1234` and have only touched
   `docs/` since" is complete evidence.
4. **Never skip a check because the change looks small — skip it because the
   change cannot affect what it tests.** That distinction is the whole policy.
5. **A named gap beats a silent one.** If you skipped a tier, say so in the
   handoff and say why it was safe.
6. **Do not remove a guardrail to go faster.** Tiers select *when* a check runs,
   never *whether* it exists. Deleting coverage is not optimisation.

## 3. Tiers

Pick the lowest tier that plausibly covers the change. Climb only on evidence:
a failure, or a specific doubt the lower tier cannot answer.

### T0 — Documentation, comments, docstrings

**Evidence:** read the diff.

- If you edited a **command** in a document, run that one command. Documentation
  that teaches a broken command is a defect, and this repository has shipped
  three (see §1).
- If you edited a **link or anchor**, check the target exists.
- Nothing else. Do not run the suite to validate prose.

### T1 — Presentation: Persian strings, templates, CSS, icons, button labels

**Evidence:** render exactly what you touched.

```bash
# A template — parse it without needing a server or a database:
python -c "from app.web.templating import templates; templates.env.get_template('plans.html'); print('ok')"

# The locale file — it must stay valid JSON with the same key set:
python -c "import json,pathlib; d=json.loads(pathlib.Path('app/locales/fa.json').read_text(encoding='utf-8')); print(len(d),'keys')"
```

Run `ruff check .` **only if you also touched Python** — ruff does not read
`.html`, `.css` or `.json`.

Widen to `pytest tests/test_panel.py` only when the change alters page
*behaviour* (a new form field, a renamed route, a new data attribute the
JavaScript depends on). Rendering every panel page is a T3-sized check; do not
use it to confirm a label edit.

### T2 — Isolated logic: one service, one helper, one adapter mapping

**Evidence:** lint plus the focused module.

```bash
ruff check .
pytest tests/test_core.py -q          # or whichever single module covers the change
```

If the module you need is database-backed, this is T3 — see §4 for the database
you need.

### T3 — Cross-cutting or sensitive

Anything touching money, authentication and sessions, provisioning and
idempotency, the database models or a migration, the `PanelProvider` port and
its contracts, or bot router order.

**Evidence:** focused tests first (they localise a failure), then the full suite.

```bash
ruff check .
ruff format --check .
pytest tests/test_purchase_flow.py -q     # focused: fail here first
pytest -q                                 # full, needs PostgreSQL
```

Add `alembic check` when you touched a model or a migration — it must print
"No new upgrade operations detected."

### T4 — Release, deployment, or a migration against real data

**Evidence:** everything above, plus the integration surfaces that only break in
production.

```bash
pytest -q                                              # application suite
PYTHONPATH=tools pytest -q tools/mock_wg_panel/test_mock_smoke.py
alembic upgrade head && alembic downgrade -1 && alembic upgrade head
docker compose config --quiet                          # base stack
docker compose --profile tls config --quiet            # with Caddy
docker build -t wgguard-bot:check .                    # the image must build
```

T4 is for handoff of a release, not for every pull request. Running it on a
docs-only change is the waste this document exists to prevent.

## 4. The environment the commands need

These are the values the test-suite actually reads. Set them once per shell.

```bash
export ENV=test
export SECRET_KEY=0000000000000000000000000000000000000000000000000000000000000000
export REDIS_URL=            # empty = in-memory FSM
export BOT_TOKEN=            # empty = the bot side stays dormant, the panel runs
export TEST_DATABASE_URL="postgresql+asyncpg://wgguard:wgguard@127.0.0.1:55432/wgguard_test"
```

On Windows PowerShell:

```powershell
$env:ENV="test"
$env:SECRET_KEY="0"*64
$env:REDIS_URL=""; $env:BOT_TOKEN=""
$env:TEST_DATABASE_URL="postgresql+asyncpg://wgguard:wgguard@127.0.0.1:55432/wgguard_test"
```

**The database.** `docker-compose.yml` does *not* publish the `db` port — its
`ports:` block is commented out, deliberately, so a production host does not
expose PostgreSQL. For a host-side test run, start a throwaway one instead:

```bash
docker run -d --name wgguard-pg -p 55432:5432 \
  -e POSTGRES_USER=wgguard -e POSTGRES_PASSWORD=wgguard -e POSTGRES_DB=wgguard \
  postgres:16-alpine
docker exec wgguard-pg psql -U wgguard -d postgres -c "CREATE DATABASE wgguard_test"
```

Clean up after yourself (`docker rm -f wgguard-pg`); leaving containers and test
images behind is reported in the handoff, not silently ignored.

**Selecting tiers without a database.** `pytest -m "not db"` deselects the
database tests, so it is usable on a machine with no PostgreSQL. As of this
writing that selects **49 of 148** tests — a third of the suite for none of the
setup cost. Be aware of the gap noted in §1: without a database the `db` tests
*fail* rather than skip, so the marker is a selection tool, not a safety net.

## 5. The three loops

Keep these separate. Blurring them is what makes a small change expensive.

| Loop | Scope | Expected cost |
|---|---|---|
| **Inner** | Edit → narrowest check → edit | Seconds. T0–T2. No full suite. |
| **Handoff** | The tier your change earned, plus a written report | Once per unit of work. |
| **Release** | T4, on the exact commit being released | Once per release. |

A reviewer asking for more evidence than the tier requires should be told which
tier applies and why. A reviewer asking for **less** than the tier requires is a
correctness risk — the tiers already encode the minimum.

## 6. What is never skipped

Some guards are cheap and their failures are catastrophic. These run at every
tier that touches them, with no exceptions:

- **Secret handling.** A new log line, error message or template that could
  render a panel token, WireGuard config or subscription link.
- **Money arithmetic.** Any change under `app/core/money.py` or to an order
  total, in either direction of the Rial/Toman conversion.
- **Idempotency.** Any change to `provisioning` purchase, retry, or recovery
  logic — the duplicate-account path.
- **Authorisation.** Any change to `require_roles`, session handling, or CSRF.
- **Migrations.** `alembic check` clean, and a downgrade that works.

## 7. Known gaps (recorded, not yet fixed)

Out of scope for the documentation pass that wrote this file. Each one costs an
agent real time today; each is a small code change.

1. **`make test` / `make lint` / `make format` fail** — they exec into a runtime
   image without `pytest` or `ruff`. Either add a dev stage to the image or point
   the targets at the host venv.
2. **`requires_db` is unused** — database tests fail instead of skipping when no
   PostgreSQL is reachable. Wiring it up as a module-level `pytestmark` on the
   four database modules would make the boundary honest.
3. **The `database_schema` fixture is `autouse` and session-scoped** — it creates
   and drops the entire schema even for `pytest tests/test_core.py`. Making it
   depend on the `db` marker would remove most of the cost of the inner loop.
4. **`pytest-cov` is not a dependency** but coverage commands appear in the
   older docs (removed from `DEVELOPMENT.md`).
5. **The mock suite lives outside `testpaths`** and needs `PYTHONPATH=tools`; a
   second `testpaths` entry would fold it into one command.

Fixing these is a T3–T4 change to test infrastructure and earns its own commit.
