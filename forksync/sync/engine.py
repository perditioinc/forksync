"""
Core sync orchestration engine for forksync.

Decision tree for each fork:
1. Is upstream archived? → Skip (log: archived)
2. Is fork ahead? → Skip (log: has local changes)
3. Is fork diverged? → Create conflict issue, skip
4. Is fork up to date? → Skip (log: already current)
5. Is fork behind and can fast-forward? → Sync via GitHub API
6. Unknown? → Skip (log: unknown status)

Never uses git directly — uses GitHub API merge endpoint.
This means no local clone needed, works entirely through API.
"""

import asyncio
import logging
import time
from typing import List, Optional

from forksync import ForkStatus, SyncResult, SyncState

logger = logging.getLogger(__name__)


class SyncEngine:
    """
    Core sync orchestration.

    Uses GitHub's native merge-upstream API:
    POST /repos/{owner}/{repo}/merge-upstream

    This means:
    - No git clone, no git merge, no local operations
    - GitHub handles the merge server-side
    - Works entirely through API calls
    - No possibility of leaving local filesystem in a bad state
    """

    def __init__(
        self,
        rest_client,
        conflict_reporter,
        history=None,
        dry_run: bool = False,
        username: str = "",
    ):
        self.rest_client = rest_client
        self.conflict_reporter = conflict_reporter
        self.history = history
        self.dry_run = dry_run
        self.username = username

    async def sync_fork(
        self,
        status: ForkStatus,
        dry_run: Optional[bool] = None,
    ) -> SyncResult:
        """
        Syncs a single fork using GitHub's merge-upstream API endpoint.
        Uses: POST /repos/{owner}/{repo}/merge-upstream

        This is the official GitHub API for syncing forks.
        No git operations needed.
        """
        effective_dry_run = dry_run if dry_run is not None else self.dry_run
        start_time = time.monotonic()

        logger.info(
            "Processing fork: %s (state=%s, ahead=%d, behind=%d, archived=%s)",
            status.repo_name,
            status.state.value,
            status.ahead_by,
            status.behind_by,
            status.is_archived,
        )

        # Decision tree
        try:
            # 1. Archived upstream
            if status.is_archived or status.state == SyncState.ARCHIVED:
                logger.info("[%s] Skipping — upstream is archived", status.repo_name)
                return SyncResult(
                    repo_name=status.repo_name,
                    state=status.state,
                    action_taken="skipped",
                    commits_merged=0,
                    error="archived upstream",
                    duration_seconds=time.monotonic() - start_time,
                    issue_url=None,
                )

            # 2. Fork is ahead — has local commits not in upstream
            if status.state == SyncState.AHEAD or (
                status.ahead_by > 0 and status.behind_by == 0
            ):
                logger.info(
                    "[%s] Skipping — fork is %d commit(s) ahead of upstream",
                    status.repo_name,
                    status.ahead_by,
                )
                return SyncResult(
                    repo_name=status.repo_name,
                    state=status.state,
                    action_taken="skipped",
                    commits_merged=0,
                    error=None,
                    duration_seconds=time.monotonic() - start_time,
                    issue_url=None,
                )

            # 3. Diverged — both have unique commits
            if status.state == SyncState.DIVERGED or (
                status.ahead_by > 0 and status.behind_by > 0
            ):
                logger.info(
                    "[%s] Diverged — %d ahead, %d behind — creating conflict issue",
                    status.repo_name,
                    status.ahead_by,
                    status.behind_by,
                )
                if effective_dry_run:
                    return SyncResult(
                        repo_name=status.repo_name,
                        state=status.state,
                        action_taken="dry_run",
                        commits_merged=0,
                        error=None,
                        duration_seconds=time.monotonic() - start_time,
                        issue_url=None,
                    )
                issue_url = await self.conflict_reporter.create_conflict_issue(
                    status, self.rest_client
                )
                return SyncResult(
                    repo_name=status.repo_name,
                    state=status.state,
                    action_taken="conflict_reported",
                    commits_merged=0,
                    error=None,
                    duration_seconds=time.monotonic() - start_time,
                    issue_url=issue_url,
                )

            # 4. Up to date
            if status.state == SyncState.UP_TO_DATE:
                logger.info("[%s] Already up to date", status.repo_name)
                return SyncResult(
                    repo_name=status.repo_name,
                    state=status.state,
                    action_taken="skipped",
                    commits_merged=0,
                    error=None,
                    duration_seconds=time.monotonic() - start_time,
                    issue_url=None,
                )

            # 5. Behind and can fast-forward
            if status.state == SyncState.BEHIND and status.can_fast_forward:
                if effective_dry_run:
                    logger.info(
                        "[%s] DRY RUN — would sync %d commit(s)",
                        status.repo_name,
                        status.behind_by,
                    )
                    return SyncResult(
                        repo_name=status.repo_name,
                        state=status.state,
                        action_taken="dry_run",
                        commits_merged=0,
                        error=None,
                        duration_seconds=time.monotonic() - start_time,
                        issue_url=None,
                    )

                logger.info(
                    "[%s] Syncing — %d commit(s) behind upstream",
                    status.repo_name,
                    status.behind_by,
                )
                result = await self.rest_client.merge_upstream(
                    owner=self.username,
                    repo=status.repo_name,
                    branch=status.fork_default_branch,
                )
                merge_type = result.get("merge_type", "")
                logger.debug(
                    "[%s] merge-upstream response: merge_type=%r message=%r",
                    status.repo_name,
                    merge_type,
                    result.get("message", ""),
                )

                if merge_type == "none":
                    # 200 but GitHub says nothing changed — already up to date
                    logger.info(
                        "[%s] Already up to date (merge_type=none)", status.repo_name
                    )
                    return SyncResult(
                        repo_name=status.repo_name,
                        state=SyncState.UP_TO_DATE,
                        action_taken="skipped",
                        commits_merged=0,
                        error=None,
                        duration_seconds=time.monotonic() - start_time,
                        issue_url=None,
                    )

                if merge_type not in ("fast-forward", "merge"):
                    logger.warning(
                        "[%s] Unexpected merge_type=%r — treating as synced",
                        status.repo_name,
                        merge_type,
                    )

                commits_merged = status.behind_by if status.behind_by > 0 else 1
                logger.info(
                    "[%s] Synced successfully — %d commit(s) via %s",
                    status.repo_name,
                    commits_merged,
                    merge_type or "unknown",
                )
                return SyncResult(
                    repo_name=status.repo_name,
                    state=status.state,
                    action_taken="synced",
                    commits_merged=commits_merged,
                    error=None,
                    duration_seconds=time.monotonic() - start_time,
                    issue_url=None,
                )

            # 6. Unknown or anything else
            logger.info(
                "[%s] Skipping — status is %s", status.repo_name, status.state.value
            )
            return SyncResult(
                repo_name=status.repo_name,
                state=status.state,
                action_taken="skipped",
                commits_merged=0,
                error=f"unknown state: {status.state.value}",
                duration_seconds=time.monotonic() - start_time,
                issue_url=None,
            )

        except Exception as exc:
            logger.error("[%s] Sync failed: %s", status.repo_name, exc, exc_info=True)
            return SyncResult(
                repo_name=status.repo_name,
                state=status.state,
                action_taken="error",
                commits_merged=0,
                error=str(exc),
                duration_seconds=time.monotonic() - start_time,
                issue_url=None,
            )

    async def sync_all(
        self,
        statuses: List[ForkStatus],
        concurrency: int = 5,
    ) -> List[SyncResult]:
        """
        Sync all forks, respecting concurrency limits to avoid rate limits.

        Processes forks in batches to be a good API citizen.
        """
        results: List[SyncResult] = []
        semaphore = asyncio.Semaphore(concurrency)

        async def sync_with_semaphore(status: ForkStatus) -> SyncResult:
            async with semaphore:
                return await self.sync_fork(status)

        tasks = [sync_with_semaphore(s) for s in statuses]
        results = list(await asyncio.gather(*tasks, return_exceptions=False))

        synced = sum(1 for r in results if r.action_taken == "synced")
        skipped = sum(1 for r in results if r.action_taken == "skipped")
        conflicts = sum(1 for r in results if r.action_taken == "conflict_reported")
        dry_runs = sum(1 for r in results if r.action_taken == "dry_run")
        errors = sum(1 for r in results if r.action_taken == "error")

        logger.info(
            "Sync complete: %d synced, %d skipped, %d conflicts, %d dry-run, %d errors",
            synced,
            skipped,
            conflicts,
            dry_runs,
            errors,
        )
        return results
