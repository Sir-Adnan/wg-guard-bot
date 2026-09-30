"""The revision stamp: which code is actually running.

An image built without provenance reports ``"commit": "unknown"`` at ``/healthz``
— the state in which "I updated and the bug is still there" happens, because the
container is still running the previous image.  These tests keep the stamp wired
from the build arg through to the health endpoint.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


async def test_healthz_reports_a_revision() -> None:
    from app.web.app import create_app

    app = create_app(start_background=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://panel.test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # Never an empty string: a build without the stamp says so out loud.
    assert body["commit"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("c0ffee1", "c0ffee1"), ("  c0ffee1\n", "c0ffee1"), ("", "")],
)
def test_the_stamp_comes_from_the_build_environment(raw: str, expected: str) -> None:
    """``WG_GUARD_COMMIT`` is set by the Dockerfile from the ``GIT_COMMIT`` arg."""
    result = subprocess.run(
        [sys.executable, "-c", "import app; print(app.__commit__)"],
        cwd=REPO_ROOT,
        env={**os.environ, "WG_GUARD_COMMIT": raw},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == expected
