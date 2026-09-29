# ===========================================================================
#  WG-Guard Bot — Makefile
#
#  پوشش نازکی روی docker compose است؛ همان کارهایی که دستی انجام می‌دهید،
#  فقط کوتاه‌تر.  اجرای «make» یا «make help» فهرست دستورها را نشان می‌دهد.
#
#  Thin wrapper around `docker compose` — run `make` (or `make help`) to list
#  every target; the `##` comment on each target line is its help text.
# ===========================================================================

SHELL := /bin/sh
COMPOSE := docker compose
COMPOSE_TLS := docker compose --profile tls
SERVICE ?= bot
BACKUP_DIR ?= backups
PY ?= .venv/bin/python
TEST_DB_PORT ?= 55432
TEST_DATABASE_URL ?= postgresql+asyncpg://wgguard:wgguard@127.0.0.1:$(TEST_DB_PORT)/wgguard_test

.DEFAULT_GOAL := help
.PHONY: help install up tls domain cert down logs restart migrate test test-db lint format dev-venv venv-check backup shell psql version health clean

help: ## نمایش همین راهنما / show this help
	@printf '\n\033[1mWG-Guard Bot\033[0m — دستورهای موجود (make <target>):\n\n'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf '\n  نمونه: make logs    |    make migrate    |    make backup    |    make cert\n\n'

install: ## نصب/راه‌اندازی اولیه از صفر / first-time setup (install.sh)
	@bash install.sh

up: ## اجرای سرویس‌ها بدون دامنه؛ برای حالت دامنه از make tls استفاده کنید / start without a domain (see `make tls`)
	$(COMPOSE) up -d --build

tls: ## اجرای سرویس‌ها با دامنه و SSL خودکار Caddy / start with the tls profile (automatic HTTPS)
	$(COMPOSE_TLS) up -d --build

domain: ## تعیین یا تغییر دامنه، مثال: make domain d=bot.example.com / set or change the domain
	@bash scripts/set-domain.sh $(d)

cert: ## وضعیت دامنه و گواهی SSL (تمدید خودکار) / domain + certificate status
	@bash scripts/set-domain.sh --status

down: ## توقف و حذف کانتینرها (داده‌ها می‌مانند) / stop and remove containers
	$(COMPOSE_TLS) down --remove-orphans

logs: ## دیدن لاگ زنده‌ی ربات / follow the bot logs
	$(COMPOSE_TLS) logs -f --tail=100 $(SERVICE)

restart: ## راه‌اندازی دوباره‌ی ربات / restart the bot container
	$(COMPOSE) restart $(SERVICE)

migrate: ## اجرای مایگریشن‌های دیتابیس / apply Alembic migrations
	$(COMPOSE) exec -T $(SERVICE) alembic upgrade head

# ---------------------------------------------------------------------------
#  ابزارهای توسعه / development tooling
#
#  ایمیج تولیدی عمداً هیچ ابزار توسعه‌ای ندارد (فقط requirements.txt
#  نصب می‌شود)، پس test / lint / format روی venv محلی اجرا می‌شوند.
#  یک‌بار: make dev-venv     و برای تست‌های دیتابیسی: make test-db
#  ویندوز: make test PY=.venv/Scripts/python.exe
# ---------------------------------------------------------------------------
venv-check:
	@test -x "$(PY)" || { \
	  printf '\n\033[31mابزارهای توسعه نصب نیستند:\033[0m %s\n' "$(PY)"; \
	  printf '  اجرا کنید: make dev-venv\n\n'; \
	  exit 1; }

dev-venv: ## ساخت venv توسعه + نصب ابزارها / create .venv with the dev tooling
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -r requirements-dev.txt

test-db: ## دیتابیس موقت تست روی پورت ۵۵۴۳۲ / start the throwaway test database
	@docker start wgguard-pg >/dev/null 2>&1 || docker run -d --name wgguard-pg \
	  -p $(TEST_DB_PORT):5432 -e POSTGRES_USER=wgguard -e POSTGRES_PASSWORD=wgguard \
	  -e POSTGRES_DB=wgguard_test postgres:16-alpine >/dev/null
	@printf 'دیتابیس تست آماده است: %s\n' "$(TEST_DATABASE_URL)"

test: venv-check ## اجرای تست‌ها / run the test-suite — db tests need `make test-db`
TEST_DATABASE_URL="$(TEST_DATABASE_URL)" $(PY) -m pytest -q

lint: venv-check ## بررسی کد با ruff / lint the codebase with ruff
	$(PY) -m ruff check .

format: venv-check ## مرتب‌سازی و قالب‌بندی کد با ruff / auto-format with ruff
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

backup: ## گرفتن پشتیبان دستی از دیتابیس در ./backups / manual pg_dump into ./backups
	@mkdir -p $(BACKUP_DIR)
	@stamp=$$(date +%Y%m%d-%H%M%S); \
	 $(COMPOSE) exec -T db sh -c 'pg_dump -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" --clean --if-exists' \
	   > $(BACKUP_DIR)/manual-$$stamp.sql; \
	 printf 'پشتیبان ذخیره شد: %s/manual-%s.sql\n' '$(BACKUP_DIR)' "$$stamp"

shell: ## ورود به پوسته‌ی کانتینر ربات / open a shell inside the bot container
	$(COMPOSE) exec $(SERVICE) bash

psql: ## اتصال به دیتابیس با psql / open psql on the database
	$(COMPOSE) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

version: ## نمایش نسخه‌ی نصب‌شده / print the installed version
	$(COMPOSE) exec -T $(SERVICE) python -c \
	  'import tomllib; print(tomllib.load(open("/app/pyproject.toml","rb"))["project"]["version"])'

health: ## بررسی سلامت پنل / check the panel health endpoint
	@$(COMPOSE) exec -T $(SERVICE) sh -c \
	  'curl -fsS "http://127.0.0.1:$${PANEL_PORT:-8080}/healthz" && echo " ← سالم/healthy"'

clean: ## پاک‌سازی کش‌ها و فایل‌های موقت (داده‌ها و .env دست‌نخورده) / clean caches only
	@find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name '*.py[co]' -delete 2>/dev/null || true
	@rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage 2>/dev/null || true
	@printf 'فایل‌های موقت پاک شدند. (داده‌ها، .env و volume ها دست‌نخورده‌اند)\n'
