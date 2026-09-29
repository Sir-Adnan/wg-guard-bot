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
| Dependencies | **Dev-only** additions (`requirements-dev.txt`) for tooling. |
| Migrations | Create new revisions. |
| Docs | Rewrite any document, including this one. |
| Git | Work on a branch; commit locally. |
| Local Docker | Build images, start/stop the stack, run the test database. |

## 3. What needs an explicit instruction first

Do not do these because they seem convenient. Ask, or leave it.

- **Runtime dependencies** — `requirements.txt` is deliberately minimal and the
  panel ships zero frontend/build tooling. Adding a runtime dep changes the
  deployment contract.
- **Deleting or weakening a test, guardrail, or check** to make something pass.
  Adding `# noqa`, `xfail`, `skip`, or an `except: pass` for that purpose is the
  same act.
- **Editing a released migration.** Add a new revision instead.
- **Pushing to `main`**, force-pushing, rewriting history, or touching tags.
- **Publishing images** or changing what CI does on release.
- **Anything against a live deployment**: the owner's server, a real database, a
  real WG-Guard node, a real bot token. Read-only inspection is fine.
- **Destructive Docker**: `system prune`, `volume rm`, `down -v`.
- **Bulk rewrites** of Persian copy across the product — wording is product.

## 4. Verification: match evidence to risk

A docs typo and a change to the money path do not deserve the same afternoon.
Full policy, commands and rationale: **[`docs/VERIFICATION.md`](docs/VERIFICATION.md)**.

Start at the lowest tier that plausibly covers your change and climb only when
the evidence says you must.

| Tier | Change | Smallest sufficient evidence |
|---|---|---|
| **T0** | Docs, comments, docstrings | Read the diff. If you edited a command in a doc, run that one command. |
| **T1** | Persian strings, templates, CSS, icons, button labels | Render the affected page or parse the locale file. No database, no full suite. |
| **T2** | One service, helper, or adapter mapping | `ruff check .` + the one focused test module. |
| **T3** | Money, auth, provisioning, models, migrations, the provider port, router order | Focused tests first, then the full suite. Migrations add `alembic check`. |
| **T4** | Release, deploy, migration on real data | Full suite + mock suite + migration round trip + image build. |

Three rules make this work:

- **Smallest sufficient evidence first.** Run the narrow check; widen only if it
  fails or you are genuinely unsure.
- **Never repeat a check that is still valid.** Evidence belongs to a tree
  state. If nothing executable changed since it passed, cite that result instead
  of re-running it. State the tree state you tested.
- **Never run two `pytest` processes against one `TEST_DATABASE_URL`.** The
  fixtures recreate the schema per session; concurrent runs destroy each other.
  A lock now serialises them, so a parallel run *waits* — do not "fix" that by
  deleting the lock.

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
| Changing deployment, TLS, domains, backups | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |
| Handling secrets, auth, or a vulnerability | [`docs/SECURITY.md`](docs/SECURITY.md) |
| Writing customer-facing Persian | §6 below |
| Checking what shipped when | [`docs/CHANGELOG.md`](docs/CHANGELOG.md) |

The upstream contract is [`docs/upstream-api/openapi-wg-guard.json`](docs/upstream-api/openapi-wg-guard.json);
it is additive-only — tolerate unknown fields, never assume one is absent.

## 6. Persian copy rules

The wording is part of the product, not a translation layer.

- Natural, polite Persian. No «کاربر گرامی», no «لطفاً منتظر بمانید».
- **ZWNJ** written as a literal `\u200c`: میشود، سرویسها، نمیتوانید. A
  missing ZWNJ is a visible typo to a Persian speaker.
- Persian digits in user-facing text (۱، ۲، ۳) via `fa_digits` or the `|fa`
  filter — never hard-coded.
- Telegram messages are **HTML**, not Markdown. Only
  `<b> <i> <u> <s> <code> <pre> <a> <blockquote> <tg-emoji>`. No `*`, `_`, `#`
  or `[text](url)`.
- Escape user input with `html_escape` (bot) or `|e` (templates).
- Button labels and emoji come from `app/services/appearance.py`, never from a
  handler, so the owner can rename them without a deploy.

## 7. Handoff

When you stop, report in this shape — it is what a reviewer needs and nothing
more:

```
Changed:   what, and why (one short paragraph)
Files:     the paths that matter
Evidence:  the exact commands you ran and their result, plus the tree state
Not run:   checks you deliberately skipped, and why that was safe
Risk:      migrations, env vars, new deps, or behaviour that needs a human look
```

Do not paste a full suite log. One line per check is enough. If you skipped a
tier, say so — a silent gap is worse than a named one.

## 8. Automatic rejection

- Money as a float, or Toman in the database.
- A plaintext secret, or a secret in a log/error message.
- An import that crosses the layering in §1.3.
- A model change with no migration, or an edited released migration.
- Markdown in bot text, or unescaped user input in an HTML message.
- A hard-coded button label or emoji id in a handler.
- A deleted or weakened test, or a `# noqa` used to silence a real finding.
- A card payment that debits a wallet.
- A retry of a mutation that has no idempotency key and is not idempotent.
