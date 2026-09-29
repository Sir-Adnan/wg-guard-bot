"""ASGI entry point.

Runs the admin panel, the Telegram bot and the background scheduler in one
process.  ``uvicorn app.main:app`` is all a deployment needs.
"""

from __future__ import annotations

from app.web.app import create_app

app = create_app()

__all__ = ["app"]
