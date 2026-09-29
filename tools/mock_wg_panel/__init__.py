"""In-memory mock of the WG-Guard panel REST API for automated tests.

Public surface::

    from mock_wg_panel import create_app, MockConfig

    app = create_app()                       # default seed + wg_test_token
    app = create_app(MockConfig(...))        # custom tokens / plans / clock
"""

from __future__ import annotations

from .app import MockMiddleware, create_app
from .store import ALL_SCOPES, DEFAULT_TOKEN, READONLY_TOKEN, MockConfig, MockStore

__all__ = [
    "ALL_SCOPES",
    "DEFAULT_TOKEN",
    "READONLY_TOKEN",
    "MockConfig",
    "MockMiddleware",
    "MockStore",
    "create_app",
]

__version__ = "1.0.0"
