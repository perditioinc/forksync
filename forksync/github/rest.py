"""
GitHub REST API client for fork sync operations.

Used for:
- Comparing branches (ahead/behind counts)
- Merging upstream (syncing forks)
- Creating conflict issues
"""

import logging
from typing import Dict, List, Optional, Tuple

import httpx

from forksync.github.rate_limit import RateLimitTracker

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


class GitHubRestClient:
    """
    REST API client for GitHub fork sync operations.
    Uses httpx async HTTP.
    """

    def __init__(self, token: str, rate_tracker: Optional[RateLimitTracker] = None):
        self.token = token
        self.rate_tracker = rate_tracker or RateLimitTracker()
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get(self, path: str, **kwargs) -> dict:
        """Execute a GET request, record rate limit usage, return parsed JSON."""
        url = f"{GITHUB_API_BASE}{path}"
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url, headers=self._headers, timeout=30.0, **kwargs
            )
            self.rate_tracker.record_call()
            response.raise_for_status()
            return response.json()

    async def _post(self, path: str, json: Optional[dict] = None, **kwargs) -> dict:
        """Execute a POST request, record rate limit usage, return parsed JSON."""
        url = f"{GITHUB_API_BASE}{path}"
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url, headers=self._headers, json=json or {}, timeout=30.0, **kwargs
            )
            self.rate_tracker.record_call()
            response.raise_for_status()
            return response.json()

    async def compare_branches(
        self,
        owner: str,
        repo: str,
        base: str,
        head: str,
    ) -> dict:
        """
        Compare two refs using GET /repos/{owner}/{repo}/compare/{base}...{head}.

        Returns the raw GitHub API compare response dict containing
        'ahead_by', 'behind_by', 'status', etc.
        """
        path = f"/repos/{owner}/{repo}/compare/{base}...{head}"
        logger.debug("Comparing branches: %s/%s %s...%s", owner, repo, base, head)
        return await self._get(path)

    async def merge_upstream(
        self,
        owner: str,
        repo: str,
        branch: str,
    ) -> dict:
        """
        Sync a fork branch with upstream using POST /repos/{owner}/{repo}/merge-upstream.

        This is the official GitHub API endpoint for syncing forks.
        GitHub handles the merge server-side — no git clone needed.

        Returns dict with 'merge_type' and 'message' keys on success.
        """
        path = f"/repos/{owner}/{repo}/merge-upstream"
        payload = {"branch": branch}
        logger.info("Merging upstream for %s/%s branch=%s", owner, repo, branch)
        return await self._post(path, json=payload)

    async def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        labels: Optional[List[str]] = None,
    ) -> str:
        """
        Create a GitHub Issue and return the issue URL.

        POST /repos/{owner}/{repo}/issues
        """
        path = f"/repos/{owner}/{repo}/issues"
        payload: Dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels

        logger.info("Creating issue in %s/%s: %s", owner, repo, title)
        response = await self._post(path, json=payload)
        issue_url: str = response.get("html_url", "")
        logger.info("Issue created: %s", issue_url)
        return issue_url

    async def get_fork_commit_comparison(
        self,
        fork_owner: str,
        fork_repo: str,
        upstream_owner: str,
        upstream_repo: str,
        branch: str,
    ) -> Tuple[int, int]:
        """
        Get (ahead_by, behind_by) for a fork vs its upstream.

        Uses compare endpoint with cross-repo refs:
        GET /repos/{upstream_owner}/{upstream_repo}/compare/{upstream_branch}...{fork_owner}:{branch}

        Returns (ahead_by, behind_by) tuple.
        """
        base = branch
        head = f"{fork_owner}:{branch}"
        path = f"/repos/{upstream_owner}/{upstream_repo}/compare/{base}...{head}"

        logger.debug(
            "Comparing fork %s/%s to upstream %s/%s branch=%s",
            fork_owner,
            fork_repo,
            upstream_owner,
            upstream_repo,
            branch,
        )

        try:
            data = await self._get(path)
            ahead_by = int(data.get("ahead_by", 0))
            behind_by = int(data.get("behind_by", 0))
            return ahead_by, behind_by
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning(
                    "Compare endpoint returned 404 for %s/%s — marking as UNKNOWN",
                    fork_owner,
                    fork_repo,
                )
                raise
            raise

    async def list_issues(
        self,
        owner: str,
        repo: str,
        labels: Optional[List[str]] = None,
        state: str = "open",
    ) -> list:
        """
        List issues for a repo, optionally filtered by labels and state.
        GET /repos/{owner}/{repo}/issues
        """
        path = f"/repos/{owner}/{repo}/issues"
        params: Dict = {"state": state, "per_page": 100}
        if labels:
            params["labels"] = ",".join(labels)

        url = f"{GITHUB_API_BASE}{path}"
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url, headers=self._headers, params=params, timeout=30.0
            )
            self.rate_tracker.record_call()
            response.raise_for_status()
            return response.json()
