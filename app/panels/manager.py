"""Panel orchestration: provider cache, node selection and health.

This module is the **only** place that knows which concrete adapter serves a
given panel.  Everything above it works with :class:`~app.panels.base.PanelProvider`
and the canonical models, so a new backend is a registry entry rather than a
refactor.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache
from app.core.config import settings
from app.core.errors import PanelError, PanelNotFound, PanelUnavailable
from app.core.jalali import now_utc
from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.db.models import Panel, PanelHealth, Plan, Service, ServiceStatus
from app.panels import registry
from app.panels.base import PanelProvider, UnsupportedCapability
from app.panels.models import PlanSpec

log = get_logger(__name__)

#: How long a panel's health verdict is trusted before re-probing.
HEALTH_TTL_SECONDS = 60


@dataclass(slots=True)
class PanelSnapshot:
    """Lightweight, non-ORM view of a panel used by the UI and pickers."""

    id: int
    name: str
    base_url: str
    health: PanelHealth
    is_active: bool
    is_default: bool
    priority: int
    service_count: int
    max_services: int | None
    kind: str = registry.DEFAULT_KIND
    kind_label: str = ""
    node_version: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def has_capacity(self) -> bool:
        return self.max_services is None or self.service_count < self.max_services


class PanelManager:
    """Owns one provider instance per panel for the lifetime of the process."""

    def __init__(self) -> None:
        self._providers: dict[int, PanelProvider] = {}
        self._token_cache: dict[int, str] = {}
        #: Optional httpx transport override, used by the test-suite to route
        #: panel traffic into an in-process ASGI app instead of the network.
        self.transport: object | None = None

    # -- providers ---------------------------------------------------------
    async def provider_for(self, panel: Panel) -> PanelProvider:
        """Return (and lazily build) the adapter for ``panel``."""
        provider = self._providers.get(panel.id)
        if provider is not None:
            return provider

        token = self._token_cache.get(panel.id)
        if token is None:
            token = decrypt_secret(panel.api_token_encrypted, purpose="panel-token") or ""
            if not token:
                raise PanelError(f"توکن پنل «{panel.name}» قابل خواندن نیست. آن را در پنل مدیریت دوباره وارد کنید.")
            self._token_cache[panel.id] = token

        try:
            provider_class = registry.get(getattr(panel, "kind", None))
        except LookupError as exc:
            raise PanelError(
                f"نوع پنل «{getattr(panel, 'kind', '?')}» پشتیبانی نمی‌شود. آن را در پنل مدیریت اصلاح کنید."
            ) from exc

        provider = provider_class(
            name=panel.name,
            base_url=panel.base_url,
            token=token,
            subscription_base_url=panel.subscription_base_url,
            options=dict(getattr(panel, "options", None) or {}),
            transport=self.transport,
            timeout=settings.wg_request_timeout,
            connect_timeout=settings.wg_connect_timeout,
            max_retries=settings.wg_max_retries,
        )
        self._providers[panel.id] = provider
        return provider

    async def provider_by_id(self, session: AsyncSession, panel_id: int) -> PanelProvider:
        panel = await session.get(Panel, panel_id)
        if panel is None:
            raise PanelNotFound("پنل مورد نظر پیدا نشد.")
        return await self.provider_for(panel)

    #: Backwards-compatible alias kept for call sites written before the
    #: provider abstraction landed.
    client_for = provider_for

    def forget(self, panel_id: int) -> None:
        """Drop cached credentials — call after editing a panel."""
        self._providers.pop(panel_id, None)
        self._token_cache.pop(panel_id, None)

    def forget_all(self) -> None:
        """Drop every cached provider and credential (panel edits, tests)."""
        self._providers.clear()
        self._token_cache.clear()

    async def close(self) -> None:
        for provider in self._providers.values():
            try:
                await provider.aclose()
            except Exception as exc:  # pragma: no cover - shutdown best effort
                log.debug("Closing provider failed: %s", exc)
        self._providers.clear()
        self._token_cache.clear()

    # -- selection ---------------------------------------------------------
    async def pick_panel(self, session: AsyncSession, plan: Plan | None = None) -> Panel:
        """Choose the node that should serve a new service.

        Order of preference:

        1. the panel pinned on the plan,
        2. a healthy panel with free capacity, lowest ``service_count`` first,
        3. any active panel (a later provisioning attempt will surface the error).
        """
        if plan is not None and plan.panel_id:
            panel = await session.get(Panel, plan.panel_id)
            if panel is None or not panel.is_active:
                raise PanelUnavailable("پنل انتخاب‌شده برای این پلن فعال نیست.")
            return panel

        stmt = (
            select(Panel)
            .where(Panel.is_active.is_(True))
            .order_by(Panel.priority.desc(), Panel.sort_order.asc(), Panel.service_count.asc(), Panel.id.asc())
        )
        panels = list((await session.execute(stmt)).scalars())
        if not panels:
            raise PanelUnavailable("هیچ پنل فعالی برای ساخت سرویس وجود ندارد.")

        def has_capacity(panel: Panel) -> bool:
            return panel.max_services is None or panel.service_count < panel.max_services

        for panel in panels:
            if panel.health == PanelHealth.ONLINE and has_capacity(panel):
                return panel
        for panel in panels:
            if has_capacity(panel) and panel.health != PanelHealth.OFFLINE:
                return panel
        for panel in panels:
            if has_capacity(panel):
                return panel
        raise PanelUnavailable("ظرفیت همه پنل‌ها تکمیل است.")

    async def list_snapshots(self, session: AsyncSession) -> list[PanelSnapshot]:
        panels = list((await session.execute(select(Panel).order_by(Panel.sort_order.asc(), Panel.id.asc()))).scalars())
        counts = dict(
            (
                await session.execute(
                    select(Service.panel_id, func.count(Service.id))
                    .where(Service.status != ServiceStatus.DELETED)
                    .group_by(Service.panel_id)
                )
            ).all()
        )
        labels = {kind: label for kind, label, _description in registry.choices()}
        return [
            PanelSnapshot(
                id=panel.id,
                name=panel.name,
                base_url=panel.base_url,
                health=panel.health,
                is_active=panel.is_active,
                is_default=panel.is_default,
                priority=panel.priority,
                service_count=int(counts.get(panel.id, panel.service_count or 0)),
                max_services=panel.max_services,
                kind=getattr(panel, "kind", registry.DEFAULT_KIND),
                kind_label=labels.get(getattr(panel, "kind", ""), getattr(panel, "kind", "")),
                node_version=panel.node_version,
            )
            for panel in panels
        ]

    # -- health ------------------------------------------------------------
    async def check_health(self, session: AsyncSession, panel: Panel, *, force: bool = False) -> PanelHealth:
        """Probe one node and persist the verdict."""
        cache_key = f"panel-health:{panel.id}"
        if not force:
            cached = await cache.misc_cache.get(cache_key)
            if cached is not None:
                return PanelHealth(cached)

        health = PanelHealth.OFFLINE
        message: str | None = None
        version: str | None = None
        node_id: str | None = None
        uptime: int | None = None

        try:
            provider = await self.provider_for(panel)
            report = await provider.health()
            health = PanelHealth(report.state)
            message = report.message
            version = report.version
            node_id = report.node_id
            uptime = report.uptime_seconds
        except PanelError as exc:
            message = exc.message[:255]
            log.warning("Panel %s health check failed: %s", panel.name, exc.message)
        except Exception as exc:  # pragma: no cover - defensive
            message = str(exc)[:255]
            log.exception("Panel %s health check crashed", panel.name)

        panel.health = health
        panel.health_message = message
        panel.last_checked_at = now_utc()
        if version:
            panel.node_version = version
        if node_id:
            panel.node_id = node_id
        if uptime is not None:
            panel.uptime_seconds = uptime

        await cache.misc_cache.set(cache_key, health.value, ttl=HEALTH_TTL_SECONDS)
        await session.flush()
        return health

    async def check_all(self, session: AsyncSession) -> dict[int, PanelHealth]:
        panels = list((await session.execute(select(Panel).where(Panel.is_active.is_(True)))).scalars())
        return {panel.id: await self.check_health(session, panel, force=True) for panel in panels}

    # -- plan synchronisation ---------------------------------------------
    def build_plan_spec(self, plan: Plan, panel: Panel) -> PlanSpec:
        """Translate a local :class:`Plan` into the canonical :class:`PlanSpec`."""
        from app.core.money import days_to_seconds, gb_to_bytes

        return PlanSpec(
            name=f"{plan.name} #{plan.id}"[:120],
            traffic_limit_bytes=gb_to_bytes(plan.traffic_gb),
            duration_seconds=days_to_seconds(plan.duration_days),
            start_policy=plan.start_policy if plan.start_policy in ("immediate", "first_connection") else "immediate",
            device_limit=plan.device_limit,
            speed_limit_down_kbps=plan.speed_limit_down_kbps,
            speed_limit_up_kbps=plan.speed_limit_up_kbps,
            interface_ref=plan.interface_id or panel.default_interface_id,
        )

    async def ensure_node_plan(self, session: AsyncSession, plan: Plan, panel: Panel) -> str:
        """Return a valid plan reference on ``panel``, creating it when needed.

        The local catalog is the source of truth for pricing and terms; the node
        only needs the technical terms so that an atomic purchase can commit the
        customer, the device and the subscription link together.
        """
        provider = await self.provider_for(panel)
        spec = self.build_plan_spec(plan, panel)

        try:
            reference = await provider.ensure_plan(spec, existing_ref=plan.wg_plan_id)
        except UnsupportedCapability as exc:
            raise PanelError("این پنل امکان ساخت خودکار پلن را ندارد. شناسه پلن را در تنظیمات پلن وارد کنید.") from exc

        if reference != plan.wg_plan_id:
            plan.wg_plan_id = reference
            await session.flush()
            log.info("Synced plan %s to panel %s as %s", plan.id, panel.name, reference)
        return reference

    # -- misc --------------------------------------------------------------
    async def all_providers(self, session: AsyncSession) -> list[tuple[Panel, PanelProvider]]:
        panels = list((await session.execute(select(Panel).where(Panel.is_active.is_(True)))).scalars())
        out: list[tuple[Panel, PanelProvider]] = []
        for panel in panels:
            try:
                out.append((panel, await self.provider_for(panel)))
            except PanelError as exc:
                log.warning("Skipping panel %s: %s", panel.name, exc.message)
        return out

    #: Backwards-compatible alias.
    all_clients = all_providers

    async def find_service(self, session: AsyncSession, service_id: int) -> tuple[Service, Panel, PanelProvider]:
        service = await session.get(Service, service_id)
        if service is None:
            from app.core.errors import NotFoundError

            raise NotFoundError("سرویس مورد نظر پیدا نشد.")
        panel = await session.get(Panel, service.panel_id)
        if panel is None:
            raise PanelUnavailable("پنل این سرویس حذف شده است.")
        return service, panel, await self.provider_for(panel)

    @staticmethod
    async def uptime_text(seconds: int | None) -> str:
        if not seconds:
            return "—"
        days, rem = divmod(int(seconds), 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        parts = []
        if days:
            parts.append(f"{days} روز")
        if hours:
            parts.append(f"{hours} ساعت")
        if minutes and not days:
            parts.append(f"{minutes} دقیقه")
        from app.core.money import fa_digits

        return fa_digits(" و ".join(parts) or "—")


#: Process-wide singleton — services import this rather than building providers.
panel_manager = PanelManager()


__all__ = ["PanelManager", "PanelSnapshot", "panel_manager"]
