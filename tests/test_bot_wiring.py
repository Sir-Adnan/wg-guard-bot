"""Wiring guarantees that are easy to break and painful to debug.

aiogram routers are single-parent singletons, so the router tree can only be
attached to one dispatcher.  These tests pin the behaviour that makes startup
safe no matter how many times the factories are called — which is exactly what
a test suite, a hot reload or an embedded restart does.
"""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import ROUTER_ORDER, build_root_router, detach_router
from app.bot.setup import create_dispatcher, reset_dispatcher


def _count_handlers(router) -> int:
    return (
        len(router.message.handlers)
        + len(router.callback_query.handlers)
        + len(router.errors.handlers)
        + sum(_count_handlers(child) for child in router.sub_routers)
    )


@pytest.fixture(autouse=True)
def _clean_dispatcher():
    """Each test starts from an empty process-wide dispatcher."""
    reset_dispatcher()
    yield
    reset_dispatcher()


def test_create_dispatcher_is_idempotent() -> None:
    """Calling it twice must not raise, and must not gut the first dispatcher."""
    first = create_dispatcher(MemoryStorage())
    second = create_dispatcher(MemoryStorage())

    assert first is second, "a second dispatcher would strip the first of its routers"
    assert _count_handlers(first) > 100


def test_reset_rebuilds_a_working_tree() -> None:
    """After a reset the *new* tree is complete.

    The old dispatcher is deliberately left empty: its routers now belong to the
    new tree.  That is why production code calls ``create_dispatcher()`` — which
    never rebuilds — rather than composing routers itself.
    """
    first = create_dispatcher(MemoryStorage())
    complete = _count_handlers(first)

    reset_dispatcher()
    second = create_dispatcher(MemoryStorage())

    assert second is not first
    assert _count_handlers(second) == complete > 100
    assert _count_handlers(first) == 0, "the superseded tree gives up its routers"


def test_build_root_router_can_be_called_repeatedly() -> None:
    """The composition helper must survive being called more than once."""
    first = build_root_router()
    second = build_root_router()

    assert first is not second
    assert _count_handlers(second) > 100
    assert len(second.sub_routers) == len(ROUTER_ORDER)
    assert len(first.sub_routers) == 0, "routers moved to the newer root"


def test_detach_is_safe_on_an_unattached_router() -> None:
    from aiogram import Router

    orphan = Router(name="orphan")
    detach_router(orphan)  # must not raise
    assert orphan.parent_router is None


def test_detach_keeps_the_subtree_intact() -> None:
    """Detaching a router must not orphan its own children."""
    from aiogram import Router

    parent = Router(name="parent")
    child = Router(name="child")
    parent.include_router(child)

    detach_router(parent)

    assert parent.parent_router is None
    assert child.parent_router is parent, "the router's own subtree must survive"


def test_every_route_module_is_composed_in_order() -> None:
    """The admin queue must stay ahead of the customer routers."""
    from app.bot.handlers import admin

    assert ROUTER_ORDER[0] is admin, "staff callbacks must be matched before customer menus"
    root = build_root_router()
    assert len(root.sub_routers) == len(ROUTER_ORDER)


def _command_names(router) -> set[str]:
    """Every ``Command(...)`` filter reachable from ``router``, recursively."""
    from aiogram.filters import Command

    names: set[str] = set()
    for handler in router.message.handlers:
        for filter_object in handler.filters:
            callback = getattr(filter_object, "callback", None)
            if isinstance(callback, Command):
                names.update(str(command) for command in callback.commands)
    for child in router.sub_routers:
        names |= _command_names(child)
    return names


def test_every_advertised_command_has_a_handler() -> None:
    """A command in the Telegram menu must not fall through to the catch-all.

    ``/rules`` was advertised in ``USER_COMMANDS`` with no handler behind it, so
    tapping it answered «متوجه نشدم».  Anything the menu offers has to exist.
    """
    from app.bot.setup import STAFF_EXTRA_COMMANDS, USER_COMMANDS

    implemented = _command_names(build_root_router())
    advertised = {name for name, _ in (*USER_COMMANDS, *STAFF_EXTRA_COMMANDS)}

    assert advertised - implemented == set(), "advertised but not implemented"


def test_the_gift_toggle_reads_the_key_the_panel_writes(monkeypatch) -> None:
    """An operator's switch only works if the bot reads the key the panel saves.

    The gift screen gated on ``gift.enabled``, which is not a ``SettingSpec``, so
    the panel could never write it and the default silently won.
    """
    from app.bot.handlers import gift
    from app.services.settings_store import SPECS, app_settings

    assert "shop.gift_enabled" in SPECS, "the panel cannot save a key that has no spec"

    asked: list[str] = []

    def fake_get_bool(key: str, default: bool = False) -> bool:
        asked.append(key)
        return True

    monkeypatch.setattr(app_settings, "get_bool", fake_get_bool)

    assert gift.enabled() is True
    assert asked == ["shop.gift_enabled"]
