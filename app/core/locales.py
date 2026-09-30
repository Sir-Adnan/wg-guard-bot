"""Shipped copy available to all layers, without database or UI imports."""

import json
from functools import lru_cache
from pathlib import Path

from app.core.logging import get_logger

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
log = get_logger(__name__)
_runtime_overrides: dict[str, str] = {}


@lru_cache(maxsize=4)
def load_catalog(locale: str = "fa") -> dict[str, str]:
    path = LOCALES_DIR / f"{locale}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        log.error("Locale catalog could not be loaded: %s", locale)
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def default_text(key: str) -> str:
    return _runtime_overrides.get(key, load_catalog()[key])


def set_runtime_text_overrides(values: dict[str, str]) -> None:
    """The service layer injects operator copy; core never imports that layer."""
    global _runtime_overrides
    _runtime_overrides = dict(values)
