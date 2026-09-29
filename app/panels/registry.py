"""Provider registry.

Adding a backend is two steps:

1. subclass :class:`~app.panels.base.PanelProvider` and decorate it with
   :func:`register`;
2. import it from :mod:`app.panels.providers` so the decorator runs.

Nothing else in the codebase changes — the admin panel's "panel type" dropdown
is generated from this registry, and ``Panel.kind`` selects the adapter at
runtime.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.core.logging import get_logger
from app.panels.base import PanelProvider

log = get_logger(__name__)

_REGISTRY: dict[str, type[PanelProvider]] = {}

#: Used for panels created before the ``kind`` column existed.
DEFAULT_KIND = "wg_guard"


def register(cls: type[PanelProvider]) -> type[PanelProvider]:
    """Class decorator adding a provider to the registry."""
    kind = cls.kind
    if not kind:
        raise ValueError(f"{cls.__name__} must declare a non-empty `kind`")
    if kind in _REGISTRY and _REGISTRY[kind] is not cls:
        raise ValueError(f"provider kind {kind!r} is already registered by {_REGISTRY[kind].__name__}")
    _REGISTRY[kind] = cls
    log.debug("Registered panel provider %s (%s)", kind, cls.__name__)
    return cls


def get(kind: str | None) -> type[PanelProvider]:
    """Resolve a provider class by kind, falling back to the default."""
    resolved = (kind or DEFAULT_KIND).strip() or DEFAULT_KIND
    provider = _REGISTRY.get(resolved)
    if provider is None:
        raise LookupError(f"Unknown panel provider {resolved!r}. Registered: {', '.join(sorted(_REGISTRY)) or '—'}")
    return provider


def has(kind: str) -> bool:
    return kind in _REGISTRY


def kinds() -> list[str]:
    return sorted(_REGISTRY)


def choices() -> list[tuple[str, str, str]]:
    """``[(kind, label, description)]`` for the admin panel's select box."""
    return sorted(
        ((kind, cls.label or kind, cls.description) for kind, cls in _REGISTRY.items()),
        key=lambda row: row[1],
    )


def all_providers() -> Iterable[type[PanelProvider]]:
    return _REGISTRY.values()


def describe(kind: str) -> dict[str, object]:
    """Machine-readable description of a provider (used by the UI and tests)."""
    cls = get(kind)
    return {
        "kind": cls.kind,
        "label": cls.label,
        "description": cls.description,
        "capabilities": sorted(cls.capabilities),
    }


__all__ = [
    "DEFAULT_KIND",
    "all_providers",
    "choices",
    "describe",
    "get",
    "has",
    "kinds",
    "register",
]
