"""Unified drag-and-drop endpoint for every reorderable panel list.

One route serves all six lists (see :mod:`app.services.ordering`), which keeps
the CSRF check, the auth check and the two response shapes in a single place
instead of six near-copies.

Two callers, one contract
-------------------------
* **Background call** — ``panel.js`` posts the ids of the dragged list with
  ``X-Requested-With: fetch`` and gets JSON: ``{"ok", "changed", "message"}``
  with 200 on success, 400 on a refused order, 403 on a bad CSRF token.  The
  page stays where it is and the message becomes a toast.
* **Form post** — the no-JavaScript path (and the tests) posts the same fields
  as an ordinary form and gets the panel's usual 303 + flash cookie, so a
  browser without scripts still reorders, just with a page reload.

Failures are always an operator-readable Persian sentence; a stack trace, an
HTTP status or a driver message never reaches the page (``AGENTS.md`` §6).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, ValidationError
from app.core.logging import get_logger
from app.db.models import Staff
from app.services.ordering import ORDERABLE, apply_order, spec_for
from app.web.deps import form_dict, form_int_list, form_str
from app.web.security import get_db_session, require_manager, verify_csrf
from app.web.templating import redirect

log = get_logger(__name__)
router = APIRouter(tags=["ordering"])

#: Marker ``panel.js`` sends with every background call.
FETCH_HEADER = "x-requested-with"

#: Where a caller lands when it sent no usable ``next`` or ``Referer``.
DEFAULT_BACK = f"{settings.panel_prefix}/"

#: Said whenever the order could not be applied because the request itself was
#: no longer trustworthy — never the raw CSRF reason, which names internals.
STALE_SESSION_MESSAGE = "نشست شما منقضی شده است؛ صفحه را دوباره باز کنید و ترتیب را تغییر دهید."


# ---------------------------------------------------------------------------
# Reading the request
# ---------------------------------------------------------------------------
def _form_value(form: Any, key: str) -> Any:
    """One field, keeping repeats when the parser offers them.

    ``ids`` may arrive either repeated (``ids=3&ids=1``) or as one string
    (``ids=3 1`` / ``ids=3,1``); ``getlist`` covers the first, the per-key
    mapping the second.
    """
    getlist = getattr(form, "getlist", None)
    if getlist is not None:
        values = getlist(key)
        if len(values) > 1:
            return list(values)
        if values:
            return values[0]
    return form.get(key)


def _parse_ids(raw: Any) -> list[int]:
    """Turn every accepted ``ids`` shape into a plain list of integers.

    Accepts a repeated form field, a comma/space separated string, and a JSON
    array of numbers — an unknown token is simply not an id, which the service
    then reports as a missing row rather than as a crash.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        chunks: list[Any] = []
        for item in raw:
            chunks.extend(item if isinstance(item, (list, tuple)) else [item])
    else:
        chunks = [raw]

    out: list[int] = []
    for chunk in chunks:
        text = str(chunk).strip()
        if not text:
            continue
        out.extend(form_int_list({"ids": text}, "ids"))
    return out


def _parse_scope(payload: dict[str, Any]) -> int | None:
    """The optional list partition (a category's ``parent_id``)."""
    raw = form_str(payload, "scope")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValidationError("بخش انتخابی معتبر نیست.") from exc


def _wants_json(request: Request) -> bool:
    """True for the background caller (``panel.js`` / an API client)."""
    if request.headers.get(FETCH_HEADER, "").strip().lower() == "fetch":
        return True
    return "application/json" in request.headers.get("accept", "").lower()


# ---------------------------------------------------------------------------
# Choosing where a form post goes back to
# ---------------------------------------------------------------------------
def _safe_next(candidate: str | None) -> str | None:
    """Only a same-origin relative path may be used as a redirect target.

    A redirect is the one place where an attacker-supplied string can send an
    operator somewhere else, so ``//evil.example``, ``https://…``, a
    backslash-smuggled host and any control character are all refused, and the
    caller falls back to the ``Referer``.
    """
    value = (candidate or "").strip()
    if not value or len(value) > 512:
        return None
    if not value.startswith("/") or value.startswith("//") or value.startswith("/\\"):
        return None
    if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


def _referer_path(request: Request) -> str | None:
    """The ``Referer``'s path, but only when it belongs to this panel host."""
    raw = request.headers.get("referer", "").strip()
    if not raw or "://" not in raw:
        return None
    _scheme, _, rest = raw.partition("://")
    host, _, path = rest.partition("/")
    if host.split("@")[-1] != request.headers.get("host", ""):
        return None
    return _safe_next(f"/{path}")


def _back_url(spec_key: str, next_url: str | None, request: Request) -> str:
    """Where a form post returns to after the reorder."""
    resolved = _safe_next(next_url) or _referer_path(request)
    if resolved:
        return resolved
    # Every registered entity key is also its panel path, which is the sane
    # landing spot for a post that carried neither ``next`` nor a ``Referer``.
    return f"{settings.panel_prefix}/{spec_key}" if spec_key in ORDERABLE else DEFAULT_BACK


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------
def _json(*, ok: bool, changed: int, message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"ok": ok, "changed": changed, "message": message}, status_code=status_code)


def _refuse(exc: AppError, *, wants_json: bool, back: str):
    """One calm sentence back to the caller, in whichever shape it asked for."""
    if wants_json:
        return _json(ok=False, changed=0, message=exc.message, status_code=400)
    return redirect(back, message=exc.message, level="danger")


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------
@router.post("/reorder")
async def reorder(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    """Apply a new order for one list (see the module docstring)."""
    form = await request.form()
    payload = await form_dict(request)
    payload["ids"] = _form_value(form, "ids")
    wants_json = _wants_json(request)

    requested = form_str(payload, "entity")
    next_url = form_str(payload, "next")

    try:
        verify_csrf(request, form_str(payload, "csrf_token"))
    except HTTPException:
        log.warning("Reorder refused: CSRF validation failed for %s", request.url.path)
        if wants_json:
            return _json(ok=False, changed=0, message=STALE_SESSION_MESSAGE, status_code=403)
        return redirect(DEFAULT_BACK, message=STALE_SESSION_MESSAGE, level="danger")

    try:
        ids = _parse_ids(payload.get("ids"))
        changed = await apply_order(session, requested, ids, scope=_parse_scope(payload))
        label = spec_for(requested).label
        await session.commit()
    except AppError as exc:
        await session.rollback()
        return _refuse(exc, wants_json=wants_json, back=_back_url(requested, next_url, request))

    message = f"ترتیب «{label}» ذخیره شد." if changed else f"ترتیب «{label}» از قبل همین بود؛ تغییری لازم نشد."
    if wants_json:
        return _json(ok=True, changed=changed, message=message, status_code=200)
    return redirect(_back_url(requested, next_url, request), message=message)


__all__ = ["router"]
