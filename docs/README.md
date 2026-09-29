# Documentation

The complete guide to the **WG-Guard Bot** project. These documents are written in English; the product's
own user-facing copy — bot strings, panel labels and the installer prompts — stays in Persian.

## Contents

| Document | Language | Topic |
|---|---|---|
| [`../README.md`](../README.md) | English | Introduction, quick install, features, troubleshooting |
| [`CONFIGURATION.md`](CONFIGURATION.md) | English | Every `.env` variable and the panel settings |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | English | Server install, domain, backups, updates |
| [`../AGENTS.md`](../AGENTS.md) | English | Contribution and development rules (required reading) |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | English | Layering, data flow, extension points |
| [`PANEL-CONTRACT.md`](PANEL-CONTRACT.md) | English | The contract for building admin panel pages |
| [`DEVELOPMENT.md`](DEVELOPMENT.md) | English | Setting up a development and test environment |
| [`SECURITY.md`](SECURITY.md) | English | Security model and vulnerability reporting |
| [`CHANGELOG.md`](CHANGELOG.md) | English | Release history |
| [`upstream-api/`](upstream-api/) | — | WG-Guard panel OpenAPI contract (external source) |

## Quick code map

```
app/core/       config, security, money, Jalali dates, cache
app/db/         models (28 tables) and session management
app/panels/     WG-Guard REST client + multi-node management
app/services/   business logic (18 services)
app/bot/        Telegram bot (115 handlers, middlewares, keyboards)
app/web/        admin panel (21 pages)
app/workers/    scheduled jobs
app/locales/    Persian strings
tools/          WG-Guard mock panel for tests
tests/          120 tests
```

## Quick command reference

```bash
make up            # start
make logs          # logs
make migrate       # database migrations
make backup        # back up
make test          # tests
make lint          # lint the code
bash update.sh     # update
```
