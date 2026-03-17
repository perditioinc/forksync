"""
GitHub compare API client with ETag caching.

Uses the /compare endpoint to determine if a fork is behind, ahead,
or diverged from its upstream. ETags allow 304 responses when nothing
has changed, dramatically reducing API usage.
"""

import logging
from typing import Optional

import httpx

from forksync.models import ForkDocument, ForkStatus, SyncState
from forksync.storage.cache import CacheClient

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


async def compare_fork(
    fork: ForkDocument,
    cache: CacheClient,
    http: httpx.AsyncClient,
    token: str,
) -> ForkStatus:
    """
    Compare fork to upstream with ETag caching.

    - Checks Redis fork_status cache first
    - Uses If-None-Match ETag header to get 304 when nothing changed
    - Caches result with TTL based on schedule tier
    """
    # 1. Check fork_status cache
    cache_key = f"fork_status:{fork.fork_owner}/{fork.fork_repo}"
    cached = await cache.get(cache_key)
    if cached:
        logger.debug("Cache hit for %s/%s", fork.fork_owner, fork.fork_repo)
        return ForkStatus.from_cache(cached)

    # 2. Build headers with ETag if available
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    etag_key = f"upstream_etag:{fork.upstream_owner}/{fork.upstream_repo}"
    etag = await cache.get_raw(etag_key)
    if etag:
        headers["If-None-Match"] = etag

    url = (
        f"{GITHUB_API}/repos/{fork.fork_owner}/{fork.fork_repo}"
        f"/compare/{fork.upstream_branch}...{fork.fork_owner}:{fork.fork_branch}"
    )

    response = await http.get(url, headers=headers)

    # 3. 304 = nothing changed
    if response.status_code == 304:
        logger.debug("304 Not Modified for %s/%s", fork.fork_owner, fork.fork_repo)
        status = ForkStatus(state=SyncState.UP_TO_DATE, behind=0, ahead=0)
        ttl = _tier_ttl(fork.schedule_tier)
        await cache.set(cache_key, status.to_cache(), ttl=ttl)
        return status

    response.raise_for_status()

    # 4. Store new ETag
    if new_etag := response.headers.get("ETag"):
        await cache.set_raw(etag_key, new_etag, ttl=86400)

    data = response.json()
    behind = int(data.get("behind_by", 0))
    ahead = int(data.get("ahead_by", 0))

    if behind > 0 and ahead > 0:
        state = SyncState.DIVERGED
    elif behind > 0:
        state = SyncState.BEHIND
    elif ahead > 0:
        state = SyncState.AHEAD
    else:
        state = SyncState.UP_TO_DATE

    status = ForkStatus(
        state=state,
        behind=behind,
        ahead=ahead,
        etag=response.headers.get("ETag"),
    )

    ttl = _tier_ttl(fork.schedule_tier)
    await cache.set(cache_key, status.to_cache(), ttl=ttl)

    logger.debug(
        "%s/%s: behind=%d ahead=%d → %s",
        fork.fork_owner, fork.fork_repo, behind, ahead, state.value,
    )
    return status


def _tier_ttl(tier: str) -> int:
    return {"nightly": 3600, "weekly": 86400, "monthly": 604800}.get(tier, 3600)
