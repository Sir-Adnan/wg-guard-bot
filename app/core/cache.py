"""Caching primitives.

Two layers:

* :class:`TTLCache` — an in-process, ``asyncio``-safe TTL map used for the hot
  read paths (settings, texts, button styles, plan lists).  Because the web
  panel and the bot share one process, a write in the panel can invalidate it
  synchronously, which keeps the UI feeling instant.
* :class:`RedisStore` / :class:`MemoryStore` — a tiny async key/value interface
  used for rate limiting and short-lived locks.  Redis is optional: when it is
  unreachable the bot degrades to the in-memory implementation instead of
  failing to boot.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, Protocol

from app.core.logging import get_logger

log = get_logger(__name__)


class TTLCache:
    """Small in-process cache with per-key expiry."""

    def __init__(self, default_ttl: float = 30.0) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._default_ttl = default_ttl
        self._lock = asyncio.Lock()

    async def get(self, key: str, default: Any = None) -> Any:
        entry = self._data.get(key)
        if entry is None:
            return default
        expires, value = entry
        if expires and expires < time.monotonic():
            self._data.pop(key, None)
            return default
        return value

    async def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        ttl = self._default_ttl if ttl is None else ttl
        expires = time.monotonic() + ttl if ttl > 0 else 0.0
        async with self._lock:
            self._data[key] = (expires, value)

    async def get_or_set(self, key: str, factory, ttl: float | None = None) -> Any:
        cached = await self.get(key, _MISSING)
        if cached is not _MISSING:
            return cached
        value = await factory() if asyncio.iscoroutinefunction(factory) else factory()
        await self.set(key, value, ttl)
        return value

    def invalidate(self, *keys: str) -> None:
        if not keys:
            self._data.clear()
            return
        for key in keys:
            self._data.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        for key in [k for k in self._data if k.startswith(prefix)]:
            self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)


_MISSING = object()


class Store(Protocol):
    """Minimal async key/value contract."""

    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int | None = None) -> None: ...
    async def delete(self, *keys: str) -> None: ...
    async def incr(self, key: str, ttl: int) -> int: ...
    async def close(self) -> None: ...


class MemoryStore:
    """Fallback store used when Redis is unavailable."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    def _purge(self) -> None:
        now = time.monotonic()
        for key in [k for k, (exp, _) in self._data.items() if exp and exp < now]:
            self._data.pop(key, None)

    async def get(self, key: str) -> str | None:
        async with self._lock:
            self._purge()
            entry = self._data.get(key)
            return entry[1] if entry else None

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        async with self._lock:
            expires = time.monotonic() + ttl if ttl else 0.0
            self._data[key] = (expires, value)

    async def delete(self, *keys: str) -> None:
        async with self._lock:
            for key in keys:
                self._data.pop(key, None)

    async def incr(self, key: str, ttl: int) -> int:
        async with self._lock:
            self._purge()
            entry = self._data.get(key)
            count = int(entry[1]) + 1 if entry else 1
            expires = entry[0] if entry else time.monotonic() + ttl
            self._data[key] = (expires, str(count))
            return count

    async def close(self) -> None:
        self._data.clear()


class RedisStore:
    """Redis-backed store (preferred when a server is configured)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def get(self, key: str) -> str | None:
        value = await self._client.get(key)
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        if ttl:
            await self._client.set(key, value, ex=ttl)
        else:
            await self._client.set(key, value)

    async def delete(self, *keys: str) -> None:
        if keys:
            await self._client.delete(*keys)

    async def incr(self, key: str, ttl: int) -> int:
        pipe = self._client.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl, nx=True)
        result = await pipe.execute()
        return int(result[0])

    async def close(self) -> None:
        with suppress(Exception):  # pragma: no cover - best effort
            await self._client.aclose()


class CacheHub:
    """Holds the process-wide caches and the key/value store."""

    def __init__(self) -> None:
        self.settings_cache = TTLCache(default_ttl=60)
        self.texts_cache = TTLCache(default_ttl=60)
        self.buttons_cache = TTLCache(default_ttl=60)
        self.misc_cache = TTLCache(default_ttl=30)
        self.store: Store = MemoryStore()
        self.redis_client: Any = None

    async def connect_redis(self, url: str) -> bool:
        """Try to connect; return ``True`` on success."""
        if not url:
            return False
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(url, encoding="utf-8", decode_responses=False)
            await client.ping()
        except Exception as exc:
            log.warning("Redis unavailable (%s) — falling back to in-memory storage", exc)
            self.store = MemoryStore()
            return False
        self.redis_client = client
        self.store = RedisStore(client)
        log.info("Redis connected: %s", url.split("@")[-1])
        return True

    def invalidate_all(self) -> None:
        self.settings_cache.invalidate()
        self.texts_cache.invalidate()
        self.buttons_cache.invalidate()
        self.misc_cache.invalidate()

    async def close(self) -> None:
        await self.store.close()

    @asynccontextmanager
    async def lock(self, name: str, *, ttl: int = 30, wait: float = 0.0) -> AsyncIterator[bool]:
        """Best-effort distributed lock.

        Yields ``True`` when the lock was acquired.  Callers should treat a
        ``False`` as "someone else is doing this work" rather than an error.
        """
        key = f"lock:{name}"
        token = str(time.time())
        acquired = False
        deadline = time.monotonic() + wait
        while True:
            try:
                if self.redis_client is not None:
                    acquired = bool(await self.redis_client.set(key, token, nx=True, ex=ttl))
                else:
                    current = await self.store.get(key)
                    if current is None:
                        await self.store.set(key, token, ttl)
                        acquired = True
                if acquired or time.monotonic() >= deadline:
                    break
            except Exception as exc:  # pragma: no cover - redis hiccup
                log.warning("lock(%s) failed: %s", name, exc)
                acquired = True  # fail-open: never deadlock the bot
                break
            await asyncio.sleep(0.2)
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    if self.redis_client is not None:
                        # Only release our own lock.
                        current = await self.redis_client.get(key)
                        if current and current.decode() == token:
                            await self.redis_client.delete(key)
                    else:
                        await self.store.delete(key)
                except Exception:  # pragma: no cover
                    pass


cache = CacheHub()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default
