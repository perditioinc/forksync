"""
Sync engine — the core orchestrator.

Pipeline:
  1. Fetch all fork metadata from GitHub GraphQL
  2. Load all existing fork documents from Firestore in one batch read
  3. Upsert any new forks not yet in Firestore
  4. Determine which forks are due for checking based on schedule tier
  5. Run compare API concurrently (semaphore=50) for all due forks
  6. Filter to only BEHIND forks
  7. Run gh repo sync concurrently (semaphore=20) for BEHIND forks
  8. Verify each sync result with compare API (re-run compare, check behind==0)
  9. Batch write all results back to Firestore
 10. Generate SYNC_REPORT.md and write to disk
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from forksync.config import Config
from forksync.github.compare import compare_fork
from forksync.github.graphql import GitHubGraphQLClient
from forksync.github.sync import sync_fork
from forksync.models import ForkDocument, ForkStatus, SyncResult, SyncRun, SyncState
from forksync.scheduler import get_tier, is_due
from forksync.storage.cache import CacheClient

logger = logging.getLogger(__name__)


async def run_sync(
    config: Config,
    dry_run: bool = False,
    http: Optional[httpx.AsyncClient] = None,
    cache: Optional[CacheClient] = None,
) -> SyncRun:
    """
    Run the full fork sync pipeline.

    Returns a SyncRun with all counters populated.
    Works in memory-only mode if gcp_project_id is not configured.
    """
    started_at = datetime.now(timezone.utc)
    run = SyncRun(started_at=started_at, dry_run=dry_run)
    logger.info("run_sync started at %s (dry_run=%s)", started_at.isoformat(), dry_run)

    # Decide if we have Firestore available
    use_firestore = bool(config.gcp_project_id)
    db = None
    if use_firestore:
        try:
            from forksync.storage.firestore import FirestoreClient
            db = FirestoreClient(config.gcp_project_id, config.firestore_collection)
        except Exception as exc:
            logger.warning("Firestore unavailable (%s) — running in memory-only mode", exc)
            use_firestore = False

    # Set up cache
    owns_cache = cache is None
    if cache is None:
        cache = CacheClient(host=config.redis_host, port=config.redis_port)
        await cache.connect()

    # Set up HTTP client.
    # max_keepalive_connections must be >= concurrency_compare or httpx will
    # repeatedly tear down and re-establish TLS connections, serialising the pool.
    owns_http = http is None
    if http is None:
        http = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(
                max_connections=config.concurrency_compare * 2,
                max_keepalive_connections=config.concurrency_compare,
            ),
        )

    try:
        # ── STEP 1: Fetch all fork metadata from GitHub GraphQL ──────────────
        logger.info("Step 1: Fetching fork metadata from GitHub GraphQL...")
        graphql = GitHubGraphQLClient(token=config.github_token)
        fetched_forks: List[ForkDocument] = await graphql.get_all_forks(
            username=config.fork_owner
        )
        run.total_forks = len(fetched_forks)
        run.api_calls_used += graphql.api_calls
        logger.info("Fetched %d forks", run.total_forks)

        # ── STEP 2: Load existing fork documents from Firestore ──────────────
        existing: Dict[str, dict] = {}
        if use_firestore and db:
            logger.info("Step 2: Loading existing fork documents from Firestore...")
            existing = await db.batch_get_forks(config.fork_owner)
            logger.info("Loaded %d existing fork documents", len(existing))

        # ── STEP 3: Upsert new forks not yet in Firestore ────────────────────
        if use_firestore and db:
            new_forks = [f for f in fetched_forks if f.fork_repo not in existing]
            if new_forks:
                logger.info("Step 3: Upserting %d new forks to Firestore...", len(new_forks))
                await db.batch_upsert(new_forks)

        # Merge existing Firestore data into fetched fork documents
        for fork in fetched_forks:
            if fork.fork_repo in existing:
                stored = existing[fork.fork_repo]
                # Preserve stored timestamps and state
                fork.last_synced_at = _parse_dt(stored.get("last_synced_at"))
                fork.last_checked_at = _parse_dt(stored.get("last_checked_at"))
                fork.last_error = stored.get("last_error")
                fork.status = stored.get("status", "unknown")
                fork.behind_by = int(stored.get("behind_by", 0))
                fork.ahead_by = int(stored.get("ahead_by", 0))
                fork.compare_etag = stored.get("compare_etag")
                fork.last_upstream_sha = stored.get("last_upstream_sha", "")
                # Re-compute tier in case upstream activity has changed
                fork.schedule_tier = get_tier(fork.upstream_pushed_at)

        # ── STEP 4: Determine which forks are due for checking ───────────────
        logger.info("Step 4: Determining which forks are due for checking...")
        due_forks: List[ForkDocument] = []
        not_due_forks: List[ForkDocument] = []

        for fork in fetched_forks:
            if fork.archived:
                run.skipped_archived += 1
                fork.status = "archived"
                continue

            last_checked_date = fork.last_checked_at.date() if fork.last_checked_at else None
            if is_due(fork.schedule_tier, last_checked_date):
                due_forks.append(fork)
            else:
                not_due_forks.append(fork)
                run.skipped_schedule += 1

        logger.info(
            "%d forks due for checking, %d skipped by schedule, %d archived",
            len(due_forks), run.skipped_schedule, run.skipped_archived,
        )

        # ── STEP 5: Compare due forks concurrently ───────────────────────────
        _compare_start = time.monotonic()
        logger.info(
            "Step 5: Comparing %d forks against upstream (concurrency=%d) — start %.3f",
            len(due_forks), config.concurrency_compare, _compare_start,
        )
        compare_semaphore = asyncio.Semaphore(config.concurrency_compare)
        compare_results: List[Tuple[ForkDocument, Optional[ForkStatus]]] = []

        _in_flight = 0
        _completed = 0
        _peak_in_flight = 0
        _total = len(due_forks)

        async def compare_one(fork: ForkDocument) -> Tuple[ForkDocument, Optional[ForkStatus]]:
            nonlocal _in_flight, _completed, _peak_in_flight
            async with compare_semaphore:
                _in_flight += 1
                if _in_flight > _peak_in_flight:
                    _peak_in_flight = _in_flight
                try:
                    status = await compare_fork(fork, cache, http, config.github_token)
                    run.api_calls_used += 1
                    return fork, status
                except Exception as exc:
                    logger.warning(
                        "Compare failed for %s/%s: %s",
                        fork.fork_owner, fork.fork_repo, exc,
                        exc_info=True,
                    )
                    return fork, None
                finally:
                    _in_flight -= 1
                    _completed += 1
                    if _completed % 50 == 0 or _completed == _total:
                        logger.info(
                            "Compare progress: %d/%d done, %d in flight",
                            _completed, _total, _in_flight,
                        )

        compare_results = list(
            await asyncio.gather(*[compare_one(f) for f in due_forks])
        )
        _compare_duration = time.monotonic() - _compare_start
        logger.info(
            "Compare phase done in %.1fs — peak concurrency: %d (target: %d)",
            _compare_duration, _peak_in_flight, config.concurrency_compare,
        )

        # Update fork documents with compare results
        now_utc = datetime.now(timezone.utc)
        behind_forks: List[ForkDocument] = []

        for fork, status in compare_results:
            fork.last_checked_at = now_utc
            run.checked += 1

            if status is None:
                fork.status = "error"
                fork.last_error = "compare failed"
                run.errors += 1
                continue

            fork.behind_by = status.behind
            fork.ahead_by = status.ahead
            fork.compare_etag = status.etag
            fork.schedule_tier = get_tier(fork.upstream_pushed_at)

            if status.state == SyncState.UP_TO_DATE:
                fork.status = "synced"
                run.already_current += 1
            elif status.state == SyncState.AHEAD:
                fork.status = "ahead"
                run.skipped_ahead += 1
            elif status.state == SyncState.DIVERGED:
                fork.status = "diverged"
                # Don't sync diverged forks — could cause data loss
                logger.info(
                    "Skipping diverged fork %s/%s (behind=%d ahead=%d)",
                    fork.fork_owner, fork.fork_repo, status.behind, status.ahead,
                )
            elif status.state == SyncState.BEHIND:
                fork.status = "behind"
                behind_forks.append(fork)
            else:
                fork.status = "unknown"

        logger.info(
            "Compare complete: %d behind, %d already current, %d ahead, %d errors",
            len(behind_forks), run.already_current, run.skipped_ahead, run.errors,
        )

        # ── STEP 6 is implicit above (behind_forks is filtered) ─────────────

        # ── STEP 7: Sync BEHIND forks concurrently ───────────────────────────
        sync_results: List[SyncResult] = []
        if not dry_run and behind_forks:
            logger.info("Step 7: Syncing %d behind forks...", len(behind_forks))
            sync_semaphore = asyncio.Semaphore(config.concurrency_sync)

            async def sync_one(fork: ForkDocument) -> SyncResult:
                async with sync_semaphore:
                    return await sync_fork(fork, config.github_token)

            sync_results = list(
                await asyncio.gather(*[sync_one(f) for f in behind_forks])
            )
        elif dry_run and behind_forks:
            logger.info(
                "Step 7: DRY RUN — would sync %d behind forks", len(behind_forks)
            )
            # Create synthetic results for dry run
            sync_results = [
                SyncResult(fork=f, success=True, stdout="[dry run]", stderr="")
                for f in behind_forks
            ]

        # ── STEP 8: Verify sync results ──────────────────────────────────────
        if config.verify_after_sync and sync_results and not dry_run:
            logger.info("Step 8: Verifying %d sync results...", len(sync_results))
            verify_semaphore = asyncio.Semaphore(config.concurrency_compare)
            now_utc = datetime.now(timezone.utc)

            async def verify_one(result: SyncResult) -> SyncResult:
                if not result.success:
                    return result
                async with verify_semaphore:
                    # Invalidate the cache for this fork so we get a fresh compare
                    cache_key = f"fork_status:{result.fork.fork_owner}/{result.fork.fork_repo}"
                    await cache.delete(cache_key)

                    try:
                        verified_status = await compare_fork(
                            result.fork, cache, http, config.github_token
                        )
                        run.api_calls_used += 1
                        if verified_status.behind == 0:
                            result.verified = True
                            result.fork.status = "synced"
                            result.fork.last_synced_at = now_utc
                            result.fork.behind_by = 0
                            result.fork.ahead_by = verified_status.ahead
                        else:
                            # Still behind after sync — mark error
                            result.success = False
                            result.fork.status = "error"
                            result.fork.last_error = (
                                f"Still behind by {verified_status.behind} after sync"
                            )
                            logger.warning(
                                "Verification failed for %s/%s — still behind by %d",
                                result.fork.fork_owner,
                                result.fork.fork_repo,
                                verified_status.behind,
                            )
                    except Exception as exc:
                        logger.warning(
                            "Verification compare failed for %s/%s: %s",
                            result.fork.fork_owner, result.fork.fork_repo, exc,
                        )
                        # Can't verify — treat sync success at face value
                        result.verified = False
                        result.fork.status = "synced"
                        result.fork.last_synced_at = now_utc
                return result

            sync_results = list(
                await asyncio.gather(*[verify_one(r) for r in sync_results])
            )
        elif dry_run:
            # Mark dry run results
            now_utc = datetime.now(timezone.utc)
            for result in sync_results:
                result.fork.status = "synced"
                result.fork.last_synced_at = now_utc
                result.verified = False

        # Tally sync outcomes
        for result in sync_results:
            if result.success:
                run.synced += 1
            else:
                run.errors += 1
                result.fork.status = "error"
                result.fork.last_error = result.error

        # ── STEP 9: Batch write all results back to Firestore ────────────────
        all_checked = due_forks  # All forks that went through compare
        if use_firestore and db:
            logger.info("Step 9: Writing %d fork documents back to Firestore...", len(all_checked))
            await db.batch_update(all_checked)

        # ── STEP 10: Generate SYNC_REPORT.md ─────────────────────────────────
        run.completed_at = datetime.now(timezone.utc)
        run.duration_seconds = (run.completed_at - started_at).total_seconds()

        logger.info("Step 10: Generating SYNC_REPORT.md...")
        try:
            from forksync.report import generate_report
            report_md = generate_report(
                run=run,
                forks=fetched_forks,
                sync_results=sync_results,
                username=config.fork_owner,
            )
            with open("SYNC_REPORT.md", "w", encoding="utf-8") as f:
                f.write(report_md)
            logger.info("SYNC_REPORT.md written")
        except Exception as exc:
            logger.warning("Failed to generate SYNC_REPORT.md: %s", exc)

        # Save run to Firestore
        if use_firestore and db:
            await db.save_run(run)

        logger.info(
            "Sync complete: total=%d checked=%d synced=%d errors=%d duration=%.1fs",
            run.total_forks, run.checked, run.synced, run.errors, run.duration_seconds,
        )

        # Publish event to Pub/Sub (optional — skips if reporium-events not installed)
        try:
            from reporium_events import EventType, publish_event
            await publish_event(
                event_type=EventType.SYNC_COMPLETED,
                source="forksync",
                payload={
                    "repos_checked": run.checked,
                    "repos_synced": run.synced,
                    "duration_seconds": run.duration_seconds,
                    "errors": run.errors,
                },
                project_id=config.gcp_project_id or "perditio-platform",
            )
        except ImportError:
            logger.debug("reporium-events not installed — skipping event publish")
        except Exception as exc:
            logger.warning("Failed to publish sync event: %s", exc)

        return run

    finally:
        if owns_cache:
            await cache.close()
        if owns_http:
            await http.aclose()


def _parse_dt(value) -> Optional[datetime]:
    """Safely parse a datetime value from Firestore."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
