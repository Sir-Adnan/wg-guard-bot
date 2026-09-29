# Verification

What evidence a change needs, and what it does not. The goal is a fast inner
loop that still refuses to ship a broken money path.

This is the authoritative document for "what do I run?". `AGENTS.md` §4 is the
one-screen summary.

---

## 1. What this policy is calibrated against

Measured on this repository, not guessed. `pytest -q` means the whole suite,
including the mock WG-Guard panel, which `testpaths` now collects.

| Check | Cost | Notes |
|---|---|---|
| `pytest -q` | ~42 s, 191 tests | needs PostgreSQL; the only check that exercises everything |
| `pytest -q -m "not db"` | ~4 s, 83 tests | no database; deselects the 108 `db` tests |
| `pytest -q` with no database reachable | ~4 s | 83 pass, 108 skip, **0 fail**; the header names the database it probed |
| `ruff check .` | <0.1 s | cheap enough to run over the whole tree |
| `alembic check` | seconds | needs a database; only for models and migrations |

The gap between the first row and the second is the whole argument for tiers:
the expensive check is expensive because of the database, not because of the
code, so it belongs at a boundary instead of in the edit loop.

**Why the rules look the way they do.** Earlier revisions of this repository made
the inner loop pay for the outer loop. Each item below is fixed now; the policy
exists to stop them coming back:

- `database_schema` was `autouse`, so a one-helper test run recreated the whole
  schema. It is now requested only by the fixtures that touch the database.
- `requires_db` existed and was never applied, so nobody could say what a
  database-free run does. It is applied at collection time for every `db`-marked
  test, and `REQUIRE_DB=1` (CI sets `CI`) turns the skip into a hard error, so a
  green CI run cannot mean "nothing ran".
- `make test` / `lint` / `format` exec'd into the runtime image, which ships
  neither pytest nor ruff. They now use the project venv (`make dev-venv`).
- The mock panel sat outside `testpaths`, outside ruff, and needed
  `PYTHONPATH=tools`.
- Two concurrent runs against one `TEST_DATABASE_URL` destroyed each other's
  schema; an advisory lock in `conftest.py` serialises them.
- The docs claimed database tests *fail* without a database. They skip — that is
  what the third row above measures.

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
   `docs/` since" is complete evidence. Comment, docstring and document edits are
   not executable changes; code, configuration, dependencies, tooling and the
   environment the suite reads are.
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
ruff check app/services/orders.py     # the paths you touched
pytest tests/test_core.py -q          # or whichever single module covers the change
```

`ruff check .` costs under a tenth of a second, so run the whole gate whenever
you prefer it to a path list — it is simply not *required* for a one-module
change. If the module you need is database-backed, add the database from §4 and
still run only that module.

### T3 — Sensitive or wide-reaching

Money, authentication and sessions, provisioning and idempotency, the database
models or a migration, the `PanelProvider` port and its contracts, bot router
order.

**Evidence:** the focused tests that cover the blast radius, plus `alembic check`
when a model or migration moved.

```bash
ruff check .
pytest tests/test_purchase_flow.py -q     # focused: fail here first
alembic check                             # models/migrations only
```

The full suite is **not** part of T3 by default. A change inside a sensitive area
is not by itself a reason to run everything — run it when `AGENTS.md` §4.2
applies (shared foundations, a changed contract, a blast radius you cannot name),
or at the handoff, where one run covers the whole unit of work:

```bash
pytest -q                                 # needs PostgreSQL
```

### T4 — Release, deployment, or a migration against real data

**Evidence:** everything above, plus the integration surfaces that only break in
production.

```bash
pytest -q                                              # application + mock suites
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

**Selecting tiers without a database.** A database-free run is a supported state,
not a degraded one: 83 tests pass, the 108 `db` tests skip, nothing fails, and
`pytest -q -m "not db"` selects the same 83 without even collecting the rest. The
session header prints which database was probed, so a skip is never silent, and
`REQUIRE_DB=1` turns a missing database into a hard error — that is what CI uses,
where a silent skip would look like a green build.

With Docker available, `make test-db` starts the throwaway PostgreSQL from the
recipe above and `make test` runs the suite against it; `make dev-venv` creates
the virtualenv those targets need. See `DEVELOPMENT.md` §5.

## 5. The three loops

Keep these separate. Blurring them is what makes a small change expensive.

| Loop | Scope | Expected cost |
|---|---|---|
| **Inner** | Edit → narrowest check → edit | Seconds. T0–T2. No full suite. |
| **Handoff** | One full-suite run covering the whole unit, plus the written report | Once per unit of work. |
| **Release** | T4, on the exact commit being released | Once per release. |

Two habits stop the loops from bleeding into each other:

- **Batch the edits, run once.** Several small changes to the same area are one
  unit of work: finish them, then run the tier that unit earned. A suite run
  after every edit buys nothing that the next run does not already cover.
- **Cite, do not repeat.** A run whose tree state still holds *is* the evidence —
  quote it with that state (`f30fa23 + docs only`) rather than paying for it
  again.

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

1. **No test renders every screen's keyboard.** `tests/test_keyboards.py` pins the
   builder's rules — `columns` is honoured, no row exceeds eight buttons — but a
   screen that only breaks with an unusual amount of data (nine plans on one page,
   nine categories in one level) is still found in production.
2. **The operator CLI is exercised as a function, not through the image.**
   `docker compose exec bot python -m app.cli …` is the documented way back into a
   panel whose password is lost, and no check runs it inside a container.
3. **`make test-db` assumes port 55432 is free.** It starts or reuses a container
   named `wgguard-pg`; a machine already running PostgreSQL on that port under
   another name gets a port conflict instead of a database.
4. **258 Persian strings still live inside `app/bot/handlers/**`** rather than in
   `app/locales/fa.json`, so most screens cannot be reworded from the panel.
   Measured, and written up in [`UX-WRITING.md`](UX-WRITING.md) §9.
5. **The `/panels` admin page has only a render test** — no browser-level check of
   the provider dropdown, the connection test or the delete guard.

Items 1 and 2 are small test additions, 3 is test infrastructure; each earns its
own commit.
