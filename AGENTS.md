# AGENTS.md

Working agreement for anyone — human or AI — changing this repository.

Two ideas carry the whole file: **keep the invariants**, and **spend evidence in
proportion to risk**. Read the section that matches what you are about to touch;
you do not need the rest.

---

## 1. Non-negotiables

Break one of these and the change is wrong, no matter how well it tests.

1. **Money is an integer in Rial.** Everywhere: columns, function signatures,
   API payloads. Toman is a display concern owned by `app/core/money.py`. A
   `*_rial` column that is not `BigInteger`, or a service that returns Toman, is
   a bug. Never a float.

2. **No secret is ever plaintext.** Panel tokens, WireGuard configs and
   subscription links go through `app.core.security.encrypt_secret(...,
   purpose=...)`; each secret gets its own purpose. Never log one, never render
   one without `mask_secret`, never put one in an error message.

3. **Layering is one-way:** `core → db / panels → services → bot / web`.
   Nothing lower imports anything higher. If you need to, the logic belongs one
   layer down.

4. **Card payments never touch the wallet.** Only `WALLET`, `ADMIN` and `GIFT`
   orders debit a balance. Card revenue is tracked through orders.

5. **Provisioning is exactly-once.** Reuse the same `Idempotency-Key` when
   retrying the same payload; after an ambiguous failure ask
   `recover_purchase()` first. `None` means "not committed, safe to retry" —
   answering `None` for a purchase that did commit creates a duplicate account.

## 2. What you may do without asking

| Area | Allowed |
|---|---|
| Code | Add and refactor freely inside the layering rules. |
| Tests | Add tests; extend existing ones; strengthen assertions. |
| Dependencies | **Dev-only** additions in `requirements-dev.txt`, pinned. CI installs that file, so keep it small. |
| Migrations | A new revision for a model change you made (see §3 for destructive ones). |
| Docs | Rewrite any document, including this one. |
| Git | Branch or `main`; commit locally; push your own finished work. |
| Local Docker | Build images; start/stop the stack **without** the `tls` profile; run the throwaway test database. |

Never commit `.env`, a real token, or a dump taken from a live database — not
even on a branch you intend to delete.

## 3. What needs an explicit instruction first

Pushing finished work is this project's workflow, so that is allowed. What needs
a yes is anything that publishes, rewrites history, or touches real data.

- **Runtime dependencies** — `requirements.txt` is deliberately minimal and the
  panel ships zero frontend/build tooling. Adding a runtime dep changes the
  deployment contract.
- **Deleting or weakening a test, guardrail, or check** to make something pass.
  Adding `# noqa`, `xfail`, `skip`, or an `except: pass` for that purpose is the
  same act.
- **Editing a released migration.** Add a new revision instead.
- **A destructive migration** — dropping or renaming a column, or rewriting rows.
  Deployments migrate automatically on start, so this changes live data.
- **Rewriting published history** — force-pushing, amending or rebasing commits
  that are already on `main`, or moving and deleting tags.
- **Rotating `SECRET_KEY` on a live deployment.** Panel tokens, WireGuard configs
  and subscription links are encrypted with a key derived from it; a new value
  makes the existing rows unreadable.
- **Publishing images**, or changing what CI does on release.
- **Anything against a live deployment**: the owner's server, a real database, a
  real WG-Guard node, a real bot token. Read-only inspection is fine.
- **The `tls` profile against a real domain** — starting Caddy requests real
  Let's Encrypt certificates for it, and repeated bad starts hit the rate limit.
- **Destructive Docker**: `system prune`, `volume rm`, `down -v`.
- **Bulk rewrites** of Persian copy across the product — wording is product.

## 4. Verification: match evidence to risk

A docs typo and a change to the money path do not deserve the same afternoon.
Full policy, commands and rationale: **[`docs/VERIFICATION.md`](docs/VERIFICATION.md)**.

Pick the lowest tier that plausibly covers the change.

| Tier | Change | Smallest sufficient evidence |
|---|---|---|
| **T0** | Docs, comments, docstrings | Read the diff. If you edited a command in a doc, run that one command. |
| **T1** | Persian strings, templates, CSS, icons, button labels | Render that page or parse the locale file. No database, no full suite. |
| **T2** | One service, helper, or adapter mapping | `ruff check <the paths you touched>` + the one focused test module. |
| **T3** | Money, auth, provisioning, models, migrations, the provider port, router order | The focused tests that cover the blast radius, plus `alembic check` if a model or migration moved. The full suite only when §4.2 says so. |
| **T4** | Release, deploy, migration on real data | Full suite + mock suite + migration round trip + image build. |

**4.1 The rules that keep this cheap**

- **Smallest sufficient evidence first.** Run the narrow check. Widen only when
  it fails, or when you cannot name a check that would catch the mistake you are
  worried about.
- **Never repeat a check that is still valid.** Evidence belongs to a tree state,
  not to a moment. Cite the earlier run together with that state — "full suite
  green at `f30fa23`, docs-only edits since" — instead of running it again.
  Re-run only when something the check actually reads has changed: tracked code,
  configuration, dependencies, tooling, the database, or the env vars the suite
  consumes. Comment, docstring and document edits invalidate nothing.
- **Widen on blast radius, not on directory.** A file *looking* sensitive is not a
  reason to run everything; who else depends on the behaviour you changed is.
  `ruff check .` and `pytest -q` are for a change that can reach past the module
  you edited, and for the handoff below.
- **One full-suite run per handoff, not per change.** The expensive checks are a
  boundary, not a loop: they run when you finish a unit of work, push, or
  release, and they cover everything that unit touched. Inside the loop, stay at
  T0–T2 evidence.
- **Never run two `pytest` processes against one `TEST_DATABASE_URL`.** The
  fixtures recreate the schema per session; the lock serialises concurrent runs,
  so a parallel run *waits* — do not "fix" that by deleting the lock.

**4.2 The full suite is genuinely required when…** — any one is enough:

- the change touches shared foundations: `app/db/models.py`, `app/core/config.py`,
  `app/core/security.py`, `app/db/session.py`, `app/bot/keyboards.py` (every
  screen's markup), router order in `app/bot/handlers/__init__.py`, or the
  `PanelProvider` port itself;
- you changed a contract other modules call — a service signature, a callback
  payload, a DTO field, a template variable;
- the focused tests pass, but you cannot say which other modules consume what
  you touched;
- you are handing the work over, pushing, or releasing, and no full-suite run
  covers the current tree state.

Not reasons on their own: "the area is sensitive", "it lives under
`app/services/`", "the suite is quick enough".

## 5. Where to read more

Read on trigger, not by default.

| Trigger | Read |
|---|---|
| Touching `app/panels/**`, or adding a VPN backend | [`docs/PROVIDERS.md`](docs/PROVIDERS.md) |
| Adding or changing a panel page | [`docs/PANEL-CONTRACT.md`](docs/PANEL-CONTRACT.md) |
| Deciding what to test / how much evidence to gather | [`docs/VERIFICATION.md`](docs/VERIFICATION.md) |
| Setting up, running, or debugging locally | [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) |
| Needing the design rationale behind a boundary | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| Changing a setting, env var, or default | [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) |
| Changing deployment, TLS, domains, backups, or panel access | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |
| Handling secrets, auth, or a vulnerability | [`docs/SECURITY.md`](docs/SECURITY.md) |
| Writing or rewording anything the customer reads | §6 below, then [`docs/UX-WRITING.md`](docs/UX-WRITING.md) |
| Checking what shipped when | [`docs/CHANGELOG.md`](docs/CHANGELOG.md) |

The upstream contract is [`docs/upstream-api/openapi-wg-guard.json`](docs/upstream-api/openapi-wg-guard.json);
it is additive-only — tolerate unknown fields, never assume one is absent.

## 6. Text the customer reads

Wording is product, not a translation layer. Every message, button, error and
notification follows the rules below; the full guide, glossary and worked
examples are in **[`docs/UX-WRITING.md`](docs/UX-WRITING.md)**.

- **Voice:** warm, plain and confident, second person. No «کاربر گرامی», no «لطفاً منتظر بمانید».
- **Shape:** what happened → what it means → the next step. The button carries
  the next step, so a screen without one is usually missing something.
- **Errors explain.** Never just "it failed": say what went wrong in the
  customer's terms and what to do now — and never an exception, HTTP status,
  panel error or stack trace.
- **Short and calm.** One screen, one idea; page long lists instead of a wall of
  text. At most one leading emoji, never an emoji mid-sentence.
- **ZWNJ** written as a literal `\u200c`: می‌شود، سرویس‌ها، نمی‌توانید. A
  missing ZWNJ is a visible typo to a Persian speaker.
- Persian digits in user-facing text (۱، ۲، ۳) via `fa_digits` or the `|fa`
  filter — never hard-coded.
- Amounts render through `format_amount` (Toman) and volumes through
  `format_gb`; never format money by hand, and never show Rial to a customer.
- Telegram messages are **HTML**, not Markdown. Only
  `<b> <i> <u> <s> <code> <pre> <a> <blockquote> <tg-emoji>`. No `*`, `_`, `#`
  or `[text](url)`.
- Escape user input with `html_escape` (bot) or `|e` (templates).
- **New copy lives in `app/locales/fa.json`**, and every button label, colour or
  emoji id in `app/services/appearance.py` — never inline in a handler, so the
  owner can reword it from the panel without a deploy. (Older handlers still
  carry inline strings; `docs/UX-WRITING.md` §9 measures the gap.)
- One term per concept. The glossary in `docs/UX-WRITING.md` is the reference —
  it is not a place to invent synonyms.
- **Terminals and editors get English.** The installer, updater, uninstaller,
  control menu (`menu.sh`), Makefile help and `.env.example` are English on
  purpose: a Linux terminal has no bidi support and renders Persian reversed, so an
  operator cannot act on the line. Persian belongs to the bot copy and the panel
  UI. The only exceptions are product values (the default shop name) and input
  matchers that still accept «بله». A `.env` written by an older release keeps its
  Persian comments until `bash menu.sh env-english` rewrites it in place — values
  included byte for byte.

## 7. Handoff

When you stop, report in this shape — it is what a reviewer needs and nothing
more:

```
Changed:   what, and why (one short paragraph)
Files:     the paths that matter
Evidence:  the exact commands you ran and their result, plus the tree state
           (a re-used run is cited with the state it ran against)
Not run:   checks you deliberately skipped, and why that was safe
Risk:      migrations, env vars, new deps, or behaviour that needs a human look
```

Do not paste a full suite log. One line per check is enough. If you skipped a
tier, say so — a silent gap is worse than a named one.

## 8. Automatic rejection

- Money as a float, or Toman in the database.
- Rial shown to a customer, or an amount formatted by hand instead of through
  `format_amount`.
- A plaintext secret, or a secret in a log/error message.
- An import that crosses the layering in §1.3.
- A model change with no migration, or an edited released migration.
- Markdown in bot text, or unescaped user input in an HTML message.
- A customer-facing message that only says something failed, or that exposes an
  exception, HTTP status, panel error or secret.
- A button label, emoji id or customer-visible string hard-coded in a handler
  instead of coming from `appearance.py` / `fa.json`.
- A deleted or weakened test, or a `# noqa` used to silence a real finding.
- A card payment that debits a wallet.
- A retry of a mutation that has no idempotency key and is not idempotent.
