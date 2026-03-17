"""
Redis cache client with graceful degradation.

Falls back gracefully when Redis is unavailable —
cache misses just mean more API calls, not failures.
"""

import json
import logging
from typing import Any, Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class CacheClient:
    """
    Redis client with typed get/set helpers.

    Falls back gracefully when Redis is unavailable —
    cache misses just mean more API calls, not failures.
    """

    def __init__(self, host: str = "localhost", port: int = 6379):
        self._redis: Optional[aioredis.Redis] = None
        self._host = host
        self._port = port

    async def connect(self) -> None:
        try:
            self._redis = aioredis.Redis(
                host=self._host,
                port=self._port,
                decode_responses=True,
                socket_connect_timeout=5,  # fail fast if host is unreachable
                socket_timeout=5,          # fail fast on individual operations
            )
            await self._redis.ping()
            logger.info("Redis connected at %s:%d", self._host, self._port)
        except Exception as exc:
            logger.warning("Redis unavailable (%s) — running without cache", exc)
            self._redis = None

    async def close(self) -> None:
        if self._redis:
            try:
                await self._redis.aclose()
            except Exception:
                pass

    async def get(self, key: str) -> Optional[dict]:
        """Get a JSON-decoded value or None."""
        if not self._redis:
            return None
        try:
            val = await self._redis.get(key)
            return json.loads(val) if val else None
        except Exception:
            return None

    async def set(self, key: str, value: dict, ttl: int = 3600) -> None:
        """Set a JSON-encoded value with TTL."""
        if not self._redis:
            return
        try:
            await self._redis.set(key, json.dumps(value), ex=ttl)
        except Exception:
            pass

    async def get_raw(self, key: str) -> Optional[str]:
        if not self._redis:
            return None
        try:
            return await self._redis.get(key)
        except Exception:
            return None

    async def set_raw(self, key: str, value: str, ttl: int = 3600) -> None:
        if not self._redis:
            return
        try:
            await self._redis.set(key, value, ex=ttl)
        except Exception:
            pass

    async def delete(self, key: str) -> None:
        if not self._redis:
            return
        try:
            await self._redis.delete(key)
        except Exception:
            pass
