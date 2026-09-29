"""Delivering a service to its owner.

A "service" reaches the customer as three artefacts:

1. the ``.conf`` file (private key inside — never logged),
2. a QR code image of the same config,
3. the subscription link, which keeps updating on its own.

All three are produced from the locally cached copy so a temporary node outage
never stops a paying customer from retrieving what they already bought.
"""

from __future__ import annotations

import io

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.jalali import humanize_delta, jalali_date
from app.core.logging import get_logger
from app.core.money import format_bytes
from app.core.security import decrypt_secret
from app.db.models import Service, ServiceDevice, User
from app.services.notifications import notifier
from app.services.settings_store import app_settings
from app.services.texts import texts

log = get_logger(__name__)


class DeliveryService:
    """Sends config files, QR codes and subscription links."""

    # -- config ------------------------------------------------------------
    async def config_text(self, session: AsyncSession, device: ServiceDevice) -> str | None:
        """Decrypted config, refreshing from the node when the cache is unusable."""
        config = decrypt_secret(device.config_encrypted, purpose="config")
        if config:
            return config
        from app.services.provisioning import provisioning

        try:
            return await provisioning.refresh_device_config(session, device)
        except AppError as exc:
            log.warning("Could not refresh config for device %s: %s", device.wg_device_id, exc.message)
            return None

    @staticmethod
    def qr_png(config_text: str) -> bytes | None:
        """Render a scalable QR code locally (no dependency on the node)."""
        try:
            import segno
        except ImportError:  # pragma: no cover - optional dependency
            return None
        buffer = io.BytesIO()
        segno.make(config_text, error="m").save(buffer, kind="png", scale=6, border=3)
        return buffer.getvalue()

    async def send_config(
        self,
        session: AsyncSession,
        service: Service,
        device: ServiceDevice,
        *,
        user: User | None = None,
        caption: str | None = None,
    ) -> bool:
        owner = user or service.user
        text = caption or await texts.get("service.config_caption", session, name=service.wg_username)
        config = await self.config_text(session, device)
        if not config:
            await notifier.to_user(
                owner,
                await texts.get("error.panel_down", session),
            )
            return False

        filename = self._filename(service, device)
        message = await notifier.send_upload(
            owner.telegram_id,
            config.encode("utf-8"),
            filename,
            caption=text,
        )
        return message is not None

    async def send_qr(
        self,
        session: AsyncSession,
        service: Service,
        device: ServiceDevice,
        *,
        user: User | None = None,
    ) -> bool:
        owner = user or service.user
        config = await self.config_text(session, device)
        if not config:
            return False
        png = self.qr_png(config)
        if png is None:
            return False
        caption = await texts.get("service.qr_caption", session, name=service.wg_username)
        message = await notifier.send_upload(
            owner.telegram_id,
            png,
            f"{service.wg_username}-qr.png",
            caption=caption,
            as_photo=True,
        )
        return message is not None

    # -- subscription link -------------------------------------------------
    async def subscription_url(self, service: Service) -> str | None:
        path = decrypt_secret(service.subscription_encrypted, purpose="subscription")
        if not path:
            return None
        base = app_settings.get_str("advanced.subscription_base_url", "").strip()
        if not base and service.panel is not None:
            base = service.panel.public_origin
        if not base:
            return None
        return f"{base.rstrip('/')}/{path.lstrip('/')}"

    async def send_subscription(self, session: AsyncSession, service: Service, *, user: User | None = None) -> bool:
        owner = user or service.user
        url = await self.subscription_url(service)
        if not url:
            return False
        body = await texts.get("service.subscription_link", session, link=url)
        return await notifier.to_user(owner, body) is not None

    # -- bundled delivery --------------------------------------------------
    async def deliver_service(
        self,
        session: AsyncSession,
        service: Service,
        *,
        user: User | None = None,
        intro: str | None = None,
        include_summary: bool = True,
    ) -> bool:
        """Send the welcome message, the config, the QR and the subscription link."""
        owner = user or service.user
        delivered = False

        if include_summary:
            summary = intro or await self.service_summary(session, service)
            delivered = await notifier.to_user(owner, summary) is not None

        device = self.primary_device(service)
        if device is not None:
            if await self.send_config(session, service, device, user=owner):
                delivered = True
            await self.send_qr(session, service, device, user=owner)

        if await self.send_subscription(session, service, user=owner):
            delivered = True
        return delivered

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def primary_device(service: Service) -> ServiceDevice | None:
        devices = sorted(service.devices, key=lambda d: d.id)
        return devices[0] if devices else None

    @staticmethod
    def _filename(service: Service, device: ServiceDevice) -> str:
        """Short ASCII label + stable suffix, mirroring the node's convention."""
        label = "".join(ch for ch in service.wg_username if ch.isalnum())[:6] or "wg"
        return f"{label}-{device.wg_device_id[:8]}.conf"

    async def service_summary(self, session: AsyncSession, service: Service) -> str:
        """Human-readable service card used after a purchase and on demand."""
        status_key = {
            "active": "service.status_active",
            "disabled": "service.status_disabled",
            "expired": "service.status_expired",
            "traffic_exceeded": "service.status_exceeded",
            "deleted": "service.status_disabled",
        }.get(service.status.value, "service.status_active")
        status = await texts.get(status_key, session)

        limit = format_bytes(service.traffic_limit_bytes) if service.traffic_limit_bytes else None
        unlimited = await texts.get("common.unlimited", session)
        expires = jalali_date(service.expires_at) if service.expires_at else unlimited

        return await texts.get(
            "service.detail",
            session,
            name=service.wg_username,
            status=status,
            volume=limit or unlimited,
            used=format_bytes(service.traffic_used_bytes),
            remaining=format_bytes(service.remaining_bytes) if service.remaining_bytes is not None else unlimited,
            percent=f"{service.usage_percent:.0f}",
            expires=expires,
            days=humanize_delta(service.expires_at),
            devices=str(service.device_limit),
            panel=service.panel.name if service.panel else "—",
        )

    async def purchase_success_text(self, session: AsyncSession, service: Service, order_code: str) -> str:
        return await texts.get(
            "buy.success",
            session,
            order=order_code,
            service=service.wg_username,
            volume=format_bytes(service.traffic_limit_bytes)
            if service.traffic_limit_bytes
            else await texts.get("common.unlimited", session),
            expires=jalali_date(service.expires_at) if service.expires_at else "—",
            devices=str(service.device_limit),
        )


delivery = DeliveryService()


__all__ = ["DeliveryService", "delivery"]
