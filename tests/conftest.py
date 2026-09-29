"""Shared pytest fixtures.

Two kinds of test live here:

* **pure** tests (money, security, calendar, client) that run anywhere;
* **db** tests marked with ``@pytest.mark.db`` that need PostgreSQL.  Without a
  reachable database they are **skipped**, not failed, so ``pytest`` stays
  useful on a laptop with no Docker — the session header always says which
  database was probed, so a skip is never silent.  Set ``REQUIRE_DB=1`` (CI does
  it for you) to turn that skip into a hard error.

The WG-Guard node is replaced by the in-repo mock panel
(``tools/mock_wg_panel``) driven through ``httpx.ASGITransport`` — no network.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

# The application reads configuration at import time, so establish a test
# environment *before* anything imports app.core.config.
os.environ.setdefault("ENV", "test")
os.environ.setdefault("SECRET_KEY", "a" * 64)
os.environ.setdefault("BOT_TOKEN", "")
os.environ.setdefault("REDIS_URL", "")

#: Every test runs against this database.  ``DATABASE_URL`` is set too so that
#: code opening its *own* session (``session_scope`` inside the provisioning
#: service, the scheduler, …) hits the test database rather than the ``db``
#: container hostname from ``.env``.
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL") or "postgresql+asyncpg://wgguard:wgguard@127.0.0.1:55432/wgguard_test",
)
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["TEST_DATABASE_URL"] = TEST_DB_URL

import httpx  # noqa: E402

from app.db.base import Base  # noqa: E402

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def _db_available() -> bool:
    """Cheap synchronous reachability probe for a TCP endpoint."""
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(TEST_DB_URL.replace("+asyncpg", ""))
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


DB_AVAILABLE = _db_available()
requires_db = pytest.mark.skipif(not DB_AVAILABLE, reason="PostgreSQL is not reachable")

#: A missing database must never look like a passing run in CI.  GitHub Actions
#: exports ``CI``; ``REQUIRE_DB=1`` does the same locally.
REQUIRE_DB = os.environ.get("REQUIRE_DB", "").strip().lower() in {"1", "true", "yes", "on"} or bool(
    os.environ.get("CI")
)


def _safe_db_url() -> str:
    """``TEST_DATABASE_URL`` without the password, for terminal output."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(TEST_DB_URL)
    if parsed.password is None:
        return TEST_DB_URL
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    netloc = f"{parsed.username}@{host}" if parsed.username else host
    return urlunparse(parsed._replace(netloc=netloc))


# ---------------------------------------------------------------------------
# pytest hooks
# ---------------------------------------------------------------------------
def pytest_configure(config) -> None:
    if REQUIRE_DB and not DB_AVAILABLE:
        raise pytest.UsageError(
            f"REQUIRE_DB/CI is set but no database answered at {_safe_db_url()} — "
            "refusing to run a suite in which every database test would be skipped."
        )


def pytest_report_header(config) -> str:
    """Always state which database was probed: a skip must be visible."""
    if DB_AVAILABLE:
        return f"test database: reachable ({_safe_db_url()})"
    return f"test database: UNREACHABLE ({_safe_db_url()}) — db-marked tests will be skipped"


def pytest_collection_modifyitems(config, items) -> None:
    """Apply ``requires_db`` to everything marked ``db``.

    Marking at collection time means the database fixtures are never even
    requested for a skipped module, and one database-free run reports the same
    outcome whether or not the ``engine`` fixture happens to be used.
    """
    if DB_AVAILABLE:
        return
    for item in items:
        if item.get_closest_marker("db") is not None:
            item.add_marker(requires_db)


#: Advisory-lock key.  Two pytest runs sharing one database would otherwise drop
#: each other's schema mid-test — the fixtures recreate it per session — so the
#: run takes a session-level lock and concurrent runs serialise instead of
#: producing baffling "relation does not exist" failures.
SCHEMA_LOCK_KEY = 0x77676762  # "wggb"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def database_schema():
    """Create the schema once per session, drop it at the end.

    Deliberately **not** ``autouse``: only the fixtures that actually touch the
    database request it, so a pure-logic run (``pytest tests/test_core.py``)
    never pays for a schema rebuild.

    Deliberately synchronous: mixing a session-scoped *async* fixture with
    function-scoped async tests makes event loops cross, which asyncpg hates.
    """
    if not DB_AVAILABLE:
        yield
        return

    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    async def recreate(drop_first: bool) -> None:
        engine = create_async_engine(TEST_DB_URL)
        async with engine.begin() as conn:
            if drop_first:
                await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    async def acquire_lock():
        """Take a session-level advisory lock so parallel runs queue up."""
        from urllib.parse import urlparse

        import asyncpg

        parsed = urlparse(TEST_DB_URL.replace("+asyncpg", ""))
        connection = await asyncpg.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 5432,
            user=parsed.username,
            password=parsed.password,
            database=(parsed.path or "/").lstrip("/"),
        )
        await connection.execute("SELECT pg_advisory_lock($1)", SCHEMA_LOCK_KEY)
        return connection

    lock = asyncio.run(acquire_lock())
    asyncio.run(recreate(drop_first=True))
    try:
        yield
    finally:

        async def drop() -> None:
            engine = create_async_engine(TEST_DB_URL)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
            await engine.dispose()

        asyncio.run(drop())
        with suppress(Exception):  # closing a lock connection is best effort
            asyncio.run(lock.close())


@pytest.fixture
async def engine(database_schema):
    """Function-scoped engine (connection failures surface immediately)."""
    if not DB_AVAILABLE:
        pytest.skip("PostgreSQL is not reachable")

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _truncate_all(engine) -> None:
    """Empty every table so each database test starts from a known state.

    Truncation (rather than a rolled-back transaction) is deliberate: services
    such as the provisioning flow open their **own** session and commit, so the
    tests must behave like production — real commits, real visibility — and pay
    for it with a fast ``TRUNCATE`` between tests.
    """
    import sqlalchemy as sa

    names = ", ".join(f'"{name}"' for name in Base.metadata.tables)
    if not names:
        return
    async with engine.begin() as conn:
        await conn.execute(sa.text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def session(engine) -> AsyncIterator:
    """A committing session that starts from an empty database."""
    from sqlalchemy.ext.asyncio import AsyncSession

    await _truncate_all(engine)
    async with AsyncSession(bind=engine, expire_on_commit=False, autoflush=False) as session:
        yield session


@pytest.fixture
async def committed_session(engine) -> AsyncIterator:
    """Alias kept for readability in tests that only read after a commit."""
    from sqlalchemy.ext.asyncio import AsyncSession

    await _truncate_all(engine)
    async with AsyncSession(bind=engine, expire_on_commit=False, autoflush=False) as session:
        yield session


# ---------------------------------------------------------------------------
# Mock WG-Guard panel
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_panel():
    """A fresh in-memory WG-Guard node."""
    from mock_wg_panel import MockConfig, create_app

    config = MockConfig()
    return create_app(config)


@pytest.fixture
def panel_transport(mock_panel) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=mock_panel)


@pytest.fixture
async def panel_client(panel_transport) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=panel_transport, base_url="http://panel.test") as client:
        yield client


@pytest.fixture
async def wg_client(panel_transport) -> AsyncIterator:
    """A real :class:`WGGuardClient` wired to the mock panel."""
    from app.panels.client import WGGuardClient

    client = WGGuardClient(
        base_url="http://panel.test",
        token="wg_test_token",
        transport=panel_transport,
        max_retries=1,
        backoff_base=0.01,
    )
    try:
        yield client
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Application helpers
# ---------------------------------------------------------------------------
@pytest.fixture
async def admin(session):
    """An owner staff row."""
    from app.db.models import Staff, StaffRole

    row = Staff(name="مالک تست", role=StaffRole.OWNER, login="owner", receive_receipts=True)
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
async def customer(session):
    """A customer with a healthy wallet."""
    import secrets

    from app.db.models import User

    row = User(
        telegram_id=111_222_333,
        first_name="کاربر",
        last_name="تست",
        balance_rial=0,
        referral_code=f"wg{secrets.token_hex(3)}",
    )
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
async def panel_row(session):
    """A Panel row pointing at the mock node."""
    from app.core.security import encrypt_secret
    from app.db.models import Panel, PanelHealth

    row = Panel(
        name="نود تست",
        base_url="http://panel.test",
        api_token_encrypted=encrypt_secret("wg_test_token", purpose="panel-token") or "",
        is_active=True,
        is_default=True,
        health=PanelHealth.ONLINE,
        service_count=0,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
async def plan(session):
    """A sellable plan (2,500,000 Rial = 250,000 Toman) with no panel pinned."""
    from app.db.models import Plan

    row = Plan(
        panel_id=None,
        name="یک ماهه ۳۰ گیگ",
        traffic_gb=30,
        duration_days=30,
        device_limit=2,
        price_rial=2_500_000,
        is_active=True,
        is_unlimited_stock=True,
        username_template="wg{tg}",
    )
    session.add(row)
    await session.flush()
    return row


@pytest.fixture(autouse=True)
def wire_panel_transport(panel_transport, monkeypatch):
    """Point every panel client at the mock node instead of the network.

    ``PanelManager`` builds its clients lazily; this seam lets the test-suite
    swap in an ``ASGITransport`` so no HTTP request ever leaves the process.
    """
    from app.panels.manager import panel_manager

    panel_manager.transport = panel_transport
    panel_manager.forget_all()
    yield
    panel_manager.forget_all()
    panel_manager.transport = None


@pytest.fixture
def settings_env():
    """Base URL + token of the mock panel as the client expects them."""
    return {"base_url": "http://panel.test", "token": "wg_test_token"}
