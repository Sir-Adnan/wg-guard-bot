"""Domain exceptions shared by the services and the panels client."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for expected, user-presentable failures."""

    code = "INTERNAL_ERROR"
    default_message = "خطای غیرمنتظره رخ داد."

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None) -> None:
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class ValidationError(AppError):
    code = "INVALID_REQUEST"
    default_message = "اطلاعات وارد شده معتبر نیست."


class NotFoundError(AppError):
    code = "NOT_FOUND"
    default_message = "موردی یافت نشد."


class ConflictError(AppError):
    code = "CONFLICT"
    default_message = "این عملیات با وضعیت فعلی سازگار نیست."


class PermissionDenied(AppError):
    code = "FORBIDDEN"
    default_message = "شما به این بخش دسترسی ندارید."


class InsufficientFunds(AppError):
    code = "INSUFFICIENT_FUNDS"
    default_message = "موجودی کیف پول شما کافی نیست."


class MaintenanceMode(AppError):
    code = "MAINTENANCE"
    default_message = "ربات موقتاً در حال تعمیر و به‌روزرسانی است. کمی بعد دوباره تلاش کنید."


class RateLimited(AppError):
    code = "RATE_LIMITED"
    default_message = "تعداد درخواست‌های شما زیاد بود. کمی صبر کنید."


class MembershipRequired(AppError):
    code = "MEMBERSHIP_REQUIRED"
    default_message = "برای استفاده از ربات ابتدا در کانال‌های ما عضو شوید."


class PanelError(AppError):
    """Raised for any failure talking to a WG-Guard node."""

    code = "PANEL_ERROR"
    default_message = "ارتباط با پنل برقرار نشد."

    def __init__(
        self,
        message: str | None = None,
        *,
        status: int | None = None,
        panel_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.status = status
        self.panel_code = panel_code


class PanelUnavailable(PanelError):
    code = "NODE_UNAVAILABLE"
    default_message = "پنل در حال حاضر در دسترس نیست."


class PanelAuthError(PanelError):
    code = "UNAUTHORIZED"
    default_message = "توکن دسترسی پنل نامعتبر است."


class PanelConflict(PanelError):
    code = "CONFLICT"
    default_message = "این عملیات در پنل پذیرفته نشد."


class PanelNotFound(PanelError):
    code = "NOT_FOUND"
    default_message = "این مورد در پنل پیدا نشد."


class PanelValidation(PanelError):
    code = "INVALID_REQUEST"
    default_message = "پنل درخواست را نامعتبر دانست."


class ProvisioningFailed(AppError):
    code = "PROVISIONING_FAILED"
    default_message = "ساخت سرویس با خطا مواجه شد."
