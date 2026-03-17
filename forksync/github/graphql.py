"""
GitHub GraphQL client for batch fork sync status.

One query fetches all forks and their upstream comparison status.
Dramatically reduces API calls vs REST approach — typically 7-10 calls
for 700 forks instead of 700 REST calls.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

import httpx

from forksync import ForkStatus, SyncState
from forksync.github.rate_limit import RateLimitTracker

logger = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

FORK_STATUS_QUERY = """
query GetForkStatus($login: String!, $after: String) {
  user(login: $login) {
    repositories(
      first: 100
      after: $after
      isFork: true
      ownerAffiliations: OWNER
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        name
        url
        defaultBranchRef {
          name
        }
        parent {
          nameWithOwner
          url
          defaultBranchRef {
            name
            target {
              oid
            }
          }
          pushedAt
          isArchived
        }
        ref(qualifiedName: "HEAD") {
          target {
            oid
          }
        }
      }
    }
  }
}
"""


class GitHubGraphQLClient:
    """
    Fetches fork sync status for all forks in minimal API calls.
    Handles pagination automatically.
    Returns ForkStatus objects for all forks.
    """

    def __init__(self, token: str, rate_tracker: Optional[RateLimitTracker] = None):
        self.token = token
        self.rate_tracker = rate_tracker or RateLimitTracker()
        self._headers = {
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v4+json",
        }

    async def _execute_query(
        self,
        client: httpx.AsyncClient,
        login: str,
        after: Optional[str] = None,
    ) -> dict:
        """Execute a single GraphQL query page with exponential backoff retry on 5xx."""
        variables: dict = {"login": login}
        if after:
            variables["after"] = after

        payload = {
            "query": FORK_STATUS_QUERY,
            "variables": variables,
        }

        delays = [5, 10, 20]
        last_exc: Exception = RuntimeError("No attempts made")

        for attempt, delay in enumerate(delays + [None], start=1):
            try:
                response = await client.post(
                    GITHUB_GRAPHQL_URL,
                    json=payload,
                    headers=self._headers,
                    timeout=30.0,
                )

                if response.status_code >= 500:
                    if delay is not None:
                        logger.warning(
                            "GraphQL returned %d on attempt %d/%d — retrying in %ds",
                            response.status_code, attempt, len(delays) + 1, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    # Final attempt also failed
                    response.raise_for_status()

                response.raise_for_status()
                self.rate_tracker.record_call()

                data = response.json()
                if "errors" in data:
                    errors = data["errors"]
                    logger.error("GraphQL errors: %s", errors)
                    raise RuntimeError(f"GraphQL errors: {errors}")

                return data

            except httpx.HTTPStatusError:
                raise
            except httpx.RequestError as exc:
                last_exc = exc
                if delay is not None:
                    logger.warning(
                        "GraphQL request error on attempt %d/%d: %s — retrying in %ds",
                        attempt, len(delays) + 1, exc, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise last_exc

        raise last_exc  # unreachable, but satisfies type checker

    def _parse_node(self, node: dict, username: str) -> Optional[ForkStatus]:
        """Parse a single repository node into a ForkStatus."""
        try:
            repo_name = node.get("name", "")
            fork_url = node.get("url", "")

            parent = node.get("parent")
            if not parent:
                # Should not happen (isFork: true), but guard anyway
                logger.debug("Skipping %s — no parent repo", repo_name)
                return None

            upstream_repo = parent.get("nameWithOwner", "")
            upstream_url = parent.get("url", "")
            is_archived = bool(parent.get("isArchived", False))

            pushed_at_raw = parent.get("pushedAt")
            upstream_last_pushed: Optional[datetime] = None
            if pushed_at_raw:
                try:
                    upstream_last_pushed = datetime.fromisoformat(
                        pushed_at_raw.replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

            # Branch names
            fork_branch_ref = node.get("defaultBranchRef") or {}
            fork_branch = fork_branch_ref.get("name", "main")

            parent_branch_ref = parent.get("defaultBranchRef") or {}
            upstream_branch = parent_branch_ref.get("name", "main")

            # OIDs for quick equality check
            fork_ref = node.get("ref") or {}
            fork_target = fork_ref.get("target") or {}
            fork_oid = fork_target.get("oid", "")

            parent_target = parent_branch_ref.get("target") or {}
            upstream_oid = parent_target.get("oid", "")

            # Determine initial state from OID comparison
            if is_archived:
                state = SyncState.ARCHIVED
                behind_by = 0
                ahead_by = 0
                can_fast_forward = False
            elif not fork_oid or not upstream_oid:
                state = SyncState.UNKNOWN
                behind_by = 0
                ahead_by = 0
                can_fast_forward = False
            elif fork_oid == upstream_oid:
                state = SyncState.UP_TO_DATE
                behind_by = 0
                ahead_by = 0
                can_fast_forward = False
            else:
                # OIDs differ — needs REST comparison to determine ahead/behind
                # We set BEHIND as optimistic default; engine will refine via REST
                state = SyncState.BEHIND
                behind_by = -1   # sentinel: needs REST comparison
                ahead_by = 0
                can_fast_forward = True  # tentative

            return ForkStatus(
                repo_name=repo_name,
                fork_url=fork_url,
                upstream_repo=upstream_repo,
                upstream_url=upstream_url,
                fork_default_branch=fork_branch,
                upstream_default_branch=upstream_branch,
                state=state,
                behind_by=behind_by,
                ahead_by=ahead_by,
                upstream_last_pushed=upstream_last_pushed,
                is_archived=is_archived,
                can_fast_forward=can_fast_forward,
            )

        except Exception as exc:
            logger.warning(
                "Failed to parse fork node %s: %s",
                node.get("name", "<unknown>"),
                exc,
            )
            return None

    async def get_all_fork_statuses(
        self,
        username: str,
        rest_client=None,
    ) -> List[ForkStatus]:
        """
        Fetches sync status for all forks using GraphQL pagination.
        Typically 7-10 API calls for 700 forks vs 700 REST calls.

        If a rest_client is provided, it will be used to refine
        BEHIND status entries to detect AHEAD / DIVERGED accurately.
        """
        statuses: List[ForkStatus] = []
        after: Optional[str] = None
        page = 0

        async with httpx.AsyncClient() as client:
            while True:
                page += 1
                logger.info(
                    "Fetching fork page %d for %s (cursor=%s)", page, username, after
                )

                data = await self._execute_query(client, username, after)
                user_data = (data.get("data") or {}).get("user") or {}
                repositories = user_data.get("repositories") or {}
                nodes = repositories.get("nodes") or []
                page_info = repositories.get("pageInfo") or {}

                for node in nodes:
                    status = self._parse_node(node, username)
                    if status:
                        statuses.append(status)

                has_next = page_info.get("hasNextPage", False)
                if not has_next:
                    break
                after = page_info.get("endCursor")
                if not after:
                    logger.warning("hasNextPage=True but no endCursor — stopping pagination")
                    break

        logger.info(
            "GraphQL fetch complete: %d forks found in %d page(s)", len(statuses), page
        )

        # Refine BEHIND statuses that need REST comparison
        if rest_client:
            statuses = await self._refine_statuses(statuses, username, rest_client)

        return statuses

    async def _refine_statuses(
        self,
        statuses: List[ForkStatus],
        username: str,
        rest_client,
    ) -> List[ForkStatus]:
        """
        For forks where OIDs differ, use REST compare endpoint to get
        exact ahead_by / behind_by counts and determine true state.
        """
        async def refine_one(status: ForkStatus) -> ForkStatus:
            if status.state not in (SyncState.BEHIND,):
                return status
            if status.behind_by != -1:
                # Already has known counts
                return status

            try:
                upstream_owner, upstream_repo_name = status.upstream_repo.split("/", 1)
                ahead_by, behind_by = await rest_client.get_fork_commit_comparison(
                    fork_owner=username,
                    fork_repo=status.repo_name,
                    upstream_owner=upstream_owner,
                    upstream_repo=upstream_repo_name,
                    branch=status.upstream_default_branch,
                )
                status.ahead_by = ahead_by
                status.behind_by = behind_by

                if ahead_by > 0 and behind_by > 0:
                    status.state = SyncState.DIVERGED
                    status.can_fast_forward = False
                elif ahead_by > 0:
                    status.state = SyncState.AHEAD
                    status.can_fast_forward = False
                elif behind_by > 0:
                    status.state = SyncState.BEHIND
                    status.can_fast_forward = True
                else:
                    status.state = SyncState.UP_TO_DATE
                    status.can_fast_forward = False

            except Exception as exc:
                logger.warning(
                    "Failed to refine status for %s: %s — marking UNKNOWN",
                    status.repo_name,
                    exc,
                )
                status.state = SyncState.UNKNOWN
                status.behind_by = 0
                status.ahead_by = 0
                status.can_fast_forward = False

            return status

        tasks = [refine_one(s) for s in statuses]
        return list(await asyncio.gather(*tasks))
