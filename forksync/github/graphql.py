"""
GitHub GraphQL client for batch fork metadata fetch.

One query fetches all forks and their upstream metadata.
Dramatically reduces API calls vs REST approach — typically 7-10 calls
for 700 forks instead of 700 REST calls.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

import httpx

from forksync.models import ForkDocument
from forksync.scheduler import get_tier

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
    Fetches fork metadata for all forks in minimal API calls.
    Handles pagination automatically.
    Returns ForkDocument objects for all forks.
    """

    def __init__(self, token: str):
        self.token = token
        self._headers = {
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v4+json",
        }
        self._api_calls: int = 0

    @property
    def api_calls(self) -> int:
        return self._api_calls

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
                self._api_calls += 1

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

    def _parse_node(self, node: dict, username: str) -> Optional[ForkDocument]:
        """Parse a single repository node into a ForkDocument."""
        try:
            repo_name = node.get("name", "")

            parent = node.get("parent")
            if not parent:
                logger.debug("Skipping %s — no parent repo", repo_name)
                return None

            nameWithOwner = parent.get("nameWithOwner", "")
            upstream_owner, _, upstream_repo_name = nameWithOwner.partition("/")
            archived = bool(parent.get("isArchived", False))

            pushed_at_raw = parent.get("pushedAt")
            upstream_pushed_at: Optional[datetime] = None
            if pushed_at_raw:
                try:
                    upstream_pushed_at = datetime.fromisoformat(
                        pushed_at_raw.replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

            # Branch names
            fork_branch_ref = node.get("defaultBranchRef") or {}
            fork_branch = fork_branch_ref.get("name", "main")

            parent_branch_ref = parent.get("defaultBranchRef") or {}
            upstream_branch = parent_branch_ref.get("name", "main")

            # Schedule tier from upstream activity
            tier = get_tier(upstream_pushed_at)

            return ForkDocument(
                fork_owner=username,
                fork_repo=repo_name,
                fork_branch=fork_branch,
                upstream_owner=upstream_owner,
                upstream_repo=upstream_repo_name,
                upstream_branch=upstream_branch,
                upstream_pushed_at=upstream_pushed_at,
                archived=archived,
                status="unknown",
                schedule_tier=tier,
            )

        except Exception as exc:
            logger.warning(
                "Failed to parse fork node %s: %s",
                node.get("name", "<unknown>"),
                exc,
            )
            return None

    async def get_all_forks(
        self,
        username: str,
    ) -> List[ForkDocument]:
        """
        Fetches all fork metadata using GraphQL pagination.
        Typically 7-10 API calls for 700 forks.

        Returns a list of ForkDocument with status='unknown' —
        compare.py fills in the actual status.
        """
        forks: List[ForkDocument] = []
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
                    fork = self._parse_node(node, username)
                    if fork:
                        forks.append(fork)

                has_next = page_info.get("hasNextPage", False)
                if not has_next:
                    break
                after = page_info.get("endCursor")
                if not after:
                    logger.warning("hasNextPage=True but no endCursor — stopping pagination")
                    break

        logger.info(
            "GraphQL fetch complete: %d forks found in %d page(s)", len(forks), page
        )
        return forks
