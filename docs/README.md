# Documentation

Guide to the **WG-Guard Bot** project. Documents are in English. The product's
own user-facing copy — bot strings and panel labels — is Persian, because that
is what customers and the owner read; everything an operator sees in a terminal
or an editor (installer, updater, Makefile, `.env.example`) is English, since
Linux terminals render Persian badly.

**You do not need to read all of this.** Find your situation below and read one
document.

## Read on trigger

| If you are… | Read |
|---|---|
| Changing code, and wondering what is allowed | [`../AGENTS.md`](../AGENTS.md) — invariants, authority boundaries, rejection list |
| Deciding what to run before handing work over | [`VERIFICATION.md`](VERIFICATION.md) — risk tiers and the smallest sufficient evidence |
| Writing or rewording bot copy, buttons, errors | [`UX-WRITING.md`](UX-WRITING.md) |
| Setting up, running, or debugging locally | [`DEVELOPMENT.md`](DEVELOPMENT.md) |
| Adding or changing an admin panel page | [`PANEL-CONTRACT.md`](PANEL-CONTRACT.md) |
| Touching `app/panels/**`, or adding a VPN backend | [`PROVIDERS.md`](PROVIDERS.md) |
| Wanting to know why a boundary exists | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| Adding or changing a setting or env var | [`CONFIGURATION.md`](CONFIGURATION.md) |
| Deploying: server, domain, TLS, backups, updates | [`DEPLOYMENT.md`](DEPLOYMENT.md) |
| Handling secrets, auth, or reporting a vulnerability | [`SECURITY.md`](SECURITY.md) |
| Installing the product as an operator | [`../README.md`](../README.md) |
| Checking what shipped when | [`CHANGELOG.md`](CHANGELOG.md) |

The upstream WG-Guard contract lives in [`upstream-api/`](upstream-api/) and is
an external, additive-only source.

## Code map

```
app/core/       config, logging, security, money (Rial/Toman), Jalali, cache, errors
app/db/         models (28 tables) and session management
app/panels/     the VPN-backend port: interface, canonical DTOs, registry, adapters
app/services/   business logic — one module per concept
app/bot/        Telegram layer: handlers, middlewares, keyboards, callbacks
app/web/        admin panel: FastAPI + Jinja2 + hand-written RTL CSS
app/workers/    scheduled jobs (reminders, sync, backups)
app/locales/    default Persian strings
tools/          in-repo mock WG-Guard node (developer tooling, not shipped)
tests/          test-suite
```

## Commands

The runtime image deliberately ships no development tooling, so `make test`,
`make lint` and `make format` run against a local virtualenv: create it once with
`make dev-venv`, and start the throwaway test database with `make test-db` (see
[`VERIFICATION.md`](VERIFICATION.md) §1).

```bash
ruff check .                 # lint (the CI gate)
ruff format .                # format
pytest -q -m "not db"        # 73 tests, no database needed
pytest -q                    # 172 tests, needs PostgreSQL (app + mock suites)
alembic upgrade head         # migrations
```

Deployment and operations targets (inside the project directory):

```bash
make up            # start
make tls           # start with the TLS (Caddy) profile
make logs          # follow the logs
make migrate       # database migrations
make backup        # back up
```
