"""
Upstash Redis cache client with graceful degradation.

Uses Upstash's HTTP REST API (https://upstash.com) instead of a TCP Redis
connection — no VPC connector or Cloud Memorystore needed.

Falls back gracefully when Upstash is unavailable:
cache misses just mean more API calls, not failures.

Environment variables:
  UPSTASH_REDIS_REST_URL   — e.g. https://xxx.upstash.io
  UPSTASH_REDIS_REST_TOKEN — Bearer token from Upstash console
"""

import json
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0  # seconds — fail fast, cache misses are fine


class CacheClient:
    """
    Upstash Redis REST client with typed get/set helpers.

    Falls back gracefully when Upstash is unavailable —
    cache misses just mean more GitHub API calls, not failures.
    """

    def __init__(self, upstash_url: str = "", upstash_token: str = ""):
        self._url = upstash_url.rstrip("/")
        self._token = upstash_token
        self._available = bool(upstash_url and upstash_token)
        self._client: Optional[httpx.AsyncClient] = None

    async def connect(self) -> None:
        if not self._available:
            logger.info("Upstash not configured — running without cache")
            return
        self._client = httpx.AsyncClient(
            base_url=self._url,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=_TIMEOUT,
        )
        try:
            r = await self._client.post("", json=["PING"])
            r.raise_for_status()
            logger.info("Upstash Redis connected at %s", self._url)
        except Exception as exc:
            logger.warning("Upstash unavailable (%s) — running without cache", exc)
            await self._client.aclose()
            self._client = None
            self._available = False

    async def close(self) -> None:
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass

    async def _cmd(self, *args) -> Any:
        """Execute a Redis command via Upstash REST API. Returns result or None."""
        if not self._client:
            return None
        try:
            r = await self._client.post("", json=list(args))
            r.raise_for_status()
            return r.json().get("result")
        except Exception:
            return None

    async def get(self, key: str) -> Optional[dict]:
        """Get a JSON-decoded value or None."""
        val = await self._cmd("GET", key)
        if val is None:
            return None
        try:
            return json.loads(val)
        except Exception:
            return None

    async def set(self, key: str, value: dict, ttl: int = 3600) -> None:
        """Set a JSON-encoded value with TTL."""
        await self._cmd("SET", key, json.dumps(value), "EX", ttl)

    async def get_raw(self, key: str) -> Optional[str]:
        """Get a raw string value or None."""
        result = await self._cmd("GET", key)
        return str(result) if result is not None else None

    async def set_raw(self, key: str, value: str, ttl: int = 3600) -> None:
        """Set a raw string value with TTL."""
        await self._cmd("SET", key, value, "EX", ttl)

    async def delete(self, key: str) -> None:
        """Delete a key."""
        await self._cmd("DEL", key)
