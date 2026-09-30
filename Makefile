# ===========================================================================
#  WG-Guard Bot — Makefile
#
#  A thin wrapper around docker compose — the jobs you would run by hand,
#  only shorter.  Run `make` or `make help` to list the commands.
#
#  Thin wrapper around `docker compose` — run `make` (or `make help`) to list
#  every target; the `##` comment on each target line is its help text.
# ===========================================================================

SHELL := /bin/sh
COMPOSE := docker compose
COMPOSE_TLS := docker compose --profile tls
# Revision stamp baked into the image and reported by /healthz, so `make up`
# produces an image that can say which code it is (see the Dockerfile).
GIT_COMMIT ?= $(shell git rev-parse --short HEAD 2>/dev/null)
export GIT_COMMIT
SERVICE ?= bot
BACKUP_DIR ?= backups
PY ?= .venv/bin/python
TEST_DB_PORT ?= 55432
TEST_DATABASE_URL ?= postgresql+asyncpg://wgguard:wgguard@127.0.0.1:$(TEST_DB_PORT)/wgguard_test

.DEFAULT_GOAL := help
.PHONY: help install up tls domain cert down logs restart migrate test test-db lint format dev-venv venv-check backup shell psql version health clean menu

help: ## show this help
	@printf '\n\033[1mWG-Guard Bot\033[0m — available commands (make <target>):\n\n'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf '\n  examples: make logs    |    make migrate    |    make backup    |    make cert\n\n'

menu: ## open the control menu (install, update, status, logs, backup, restore, uninstall)
	@bash menu.sh

install: ## first-time setup from scratch (runs install.sh)
	@bash install.sh

up: ## start the services without a domain (use `make tls` when you have one)
	$(COMPOSE) up -d --build

tls: ## start with the tls profile (automatic HTTPS through Caddy)
	$(COMPOSE_TLS) up -d --build

domain: ## set or change the domain, e.g. make domain d=bot.example.com
	@bash scripts/set-domain.sh $(d)

cert: ## domain and SSL certificate status (renewal is automatic)
	@bash scripts/set-domain.sh --status

down: ## stop and remove the containers (data is kept)
	$(COMPOSE_TLS) down --remove-orphans

logs: ## follow the bot logs
	$(COMPOSE_TLS) logs -f --tail=100 $(SERVICE)

restart: ## restart the bot container
	$(COMPOSE) restart $(SERVICE)

migrate: ## apply the Alembic database migrations
	$(COMPOSE) exec -T $(SERVICE) alembic upgrade head

# ---------------------------------------------------------------------------
#  Development tooling
#
#  The runtime image deliberately ships no development tooling (it installs
#  requirements.txt only), so test / lint / format run against the local venv.
#  Once: make dev-venv     For database tests: make test-db
#  Windows: make test PY=.venv/Scripts/python.exe
# ---------------------------------------------------------------------------
venv-check:
	@test -x "$(PY)" || { \
	  printf '\n\033[31mDevelopment tooling is not installed:\033[0m %s\n' "$(PY)"; \
	  printf '  Run: make dev-venv\n\n'; \
	  exit 1; }

dev-venv: ## create .venv and install the development tooling
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -r requirements-dev.txt

test-db: ## start the throwaway test database on port 55432
	@docker start wgguard-pg >/dev/null 2>&1 || docker run -d --name wgguard-pg \
	  -p $(TEST_DB_PORT):5432 -e POSTGRES_USER=wgguard -e POSTGRES_PASSWORD=wgguard \
	  -e POSTGRES_DB=wgguard_test postgres:16-alpine >/dev/null
	@printf 'Test database ready: %s\n' "$(TEST_DATABASE_URL)"

test: venv-check ## run the test-suite — database tests need `make test-db`
TEST_DATABASE_URL="$(TEST_DATABASE_URL)" $(PY) -m pytest -q

lint: venv-check ## lint the codebase with ruff
	$(PY) -m ruff check .

format: venv-check ## auto-format the codebase with ruff
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

backup: ## manual pg_dump into ./backups
	@mkdir -p $(BACKUP_DIR)
	@stamp=$$(date +%Y%m%d-%H%M%S); \
	 $(COMPOSE) exec -T db sh -c 'pg_dump -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" --clean --if-exists' \
	   > $(BACKUP_DIR)/manual-$$stamp.sql; \
	 printf 'Backup written to %s/manual-%s.sql\n' '$(BACKUP_DIR)' "$$stamp"

shell: ## open a shell inside the bot container
	$(COMPOSE) exec $(SERVICE) bash

psql: ## open psql on the database
	$(COMPOSE) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

version: ## print the installed version
	$(COMPOSE) exec -T $(SERVICE) python -c \
	  'import tomllib; print(tomllib.load(open("/app/pyproject.toml","rb"))["project"]["version"])'

health: ## check the panel health endpoint
	@$(COMPOSE) exec -T $(SERVICE) sh -c \
	  'curl -fsS "http://127.0.0.1:$${PANEL_PORT:-8080}/healthz" && echo " ← healthy"'

clean: ## clean caches and temporary files (data and .env are kept)
	@find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name '*.py[co]' -delete 2>/dev/null || true
	@rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage 2>/dev/null || true
	@printf 'Temporary files cleaned. (data, .env and volumes are untouched)\n'
