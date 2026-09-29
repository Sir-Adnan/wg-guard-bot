# Documentation

Guide to the **WG-Guard Bot** project. Documents are in English; the product's
own user-facing copy — bot strings, panel labels, installer prompts — is Persian.

**You do not need to read all of this.** Find your situation below and read one
document.

## Read on trigger

| If you are… | Read |
|---|---|
| Changing code, and wondering what is allowed | [`../AGENTS.md`](../AGENTS.md) — invariants, authority boundaries, rejection list |
| Deciding what to run before handing work over | [`VERIFICATION.md`](VERIFICATION.md) — risk tiers and the smallest sufficient evidence |
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

Run these from the host virtualenv. The `make test`, `make lint` and
`make format` targets exec into the runtime image, which has neither pytest nor
ruff, so they fail — see [`VERIFICATION.md`](VERIFICATION.md) §7.

```bash
ruff check .                 # lint (the CI gate)
ruff format .                # format
pytest -m "not db" -q        # no database needed
pytest -q                    # everything (needs PostgreSQL)
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
