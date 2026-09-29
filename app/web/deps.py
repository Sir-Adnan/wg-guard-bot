"""Shared FastAPI dependencies and helpers for the panel routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from fastapi import Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import parse_user_amount

T = TypeVar("T")

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 200


@dataclass(slots=True)
class Page:
    """Pagination state shared by every list page."""

    number: int = 1
    size: int = PAGE_SIZE_DEFAULT
    total: int = 0
    search: str = ""

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size

    @property
    def pages(self) -> int:
        return max((self.total + self.size - 1) // self.size, 1)

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.pages

    @property
    def first_index(self) -> int:
        return self.offset + 1 if self.total else 0

    @property
    def last_index(self) -> int:
        return min(self.offset + self.size, self.total)

    def window(self, span: int = 2) -> list[int | None]:
        """Page numbers around the current one, with ``None`` as an ellipsis."""
        pages: list[int | None] = []
        for number in range(1, self.pages + 1):
            if number == 1 or number == self.pages or abs(number - self.number) <= span:
                pages.append(number)
            elif pages and pages[-1] is not None:
                pages.append(None)
        return pages


def pagination(
    page: int = Query(1, ge=1),
    size: int = Query(PAGE_SIZE_DEFAULT, ge=5, le=PAGE_SIZE_MAX),
    q: str = Query("", alias="q"),
) -> Page:
    """FastAPI dependency producing a :class:`Page` from the query string."""
    return Page(number=page, size=size, search=(q or "").strip())


async def form_dict(request: Request) -> dict[str, Any]:
    """All submitted form fields as a plain dict (last value wins)."""
    form = await request.form()
    return {key: form.get(key) for key in form}


def form_str(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key)
    return str(value).strip() if value is not None else default


def form_int(data: dict[str, Any], key: str, default: int = 0) -> int:
    raw = data.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        from app.core.money import en_digits

        return int(float(en_digits(str(raw)).replace(",", "")))
    except (TypeError, ValueError):
        return default


def form_money(data: dict[str, Any], key: str, default: int = 0) -> int:
    """Read a money field typed by an operator (Toman by default) into Rial."""
    raw = data.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return parse_user_amount(str(raw))
    except Exception:
        return default


def form_float(data: dict[str, Any], key: str, default: float = 0.0) -> float:
    raw = data.get(key)
    try:
        from app.core.money import en_digits

        return float(en_digits(str(raw)))
    except (TypeError, ValueError):
        return default


def form_bool(data: dict[str, Any], key: str) -> bool:
    """Checkbox semantics: present and truthy."""
    raw = data.get(key)
    if raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "on", "yes", "بله")


def form_int_list(data: dict[str, Any], key: str) -> list[int]:
    raw = data.get(key)
    if not raw:
        return []
    out: list[int] = []
    for chunk in str(raw).replace(",", " ").split():
        try:
            out.append(int(chunk))
        except ValueError:
            continue
    return out


async def save_upload(upload: UploadFile | None, *, max_mb: int = 8) -> tuple[bytes, str] | None:
    """Read an uploaded file, enforcing a size cap.  Returns ``(bytes, filename)``."""
    if upload is None or not upload.filename:
        return None
    content = await upload.read()
    if not content:
        return None
    if len(content) > max_mb * 1024 * 1024:
        return None
    return content, upload.filename


async def db_session_dep() -> AsyncSession:
    """Alias kept import-friendly for route modules."""
    from app.db.session import get_db

    async for session in get_db():
        yield session


__all__ = [
    "PAGE_SIZE_DEFAULT",
    "PAGE_SIZE_MAX",
    "Page",
    "db_session_dep",
    "form_bool",
    "form_dict",
    "form_float",
    "form_int",
    "form_int_list",
    "form_money",
    "form_str",
    "pagination",
    "save_upload",
]
