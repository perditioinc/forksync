"""
Firestore client for fork registry and sync run history.

Collections:
- forks: one document per fork, keyed by {owner}_{repo}
- sync_runs: one document per sync run
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from google.cloud import firestore as fs

from forksync.models import ForkDocument, SyncRun

logger = logging.getLogger(__name__)


class FirestoreClient:
    """
    Firestore client for fork registry and sync run history.

    Collections:
    - forks: one document per fork, keyed by {owner}_{repo}
    - sync_runs: one document per sync run
    """

    def __init__(self, project_id: str, collection: str = "forks"):
        self._db = fs.AsyncClient(project=project_id)
        self._collection = collection

    async def batch_get_forks(self, owner: str) -> Dict[str, dict]:
        """
        Return all fork documents for a given owner as a dict keyed by repo name.
        """
        try:
            query = self._db.collection(self._collection).where("fork_owner", "==", owner)
            result: Dict[str, dict] = {}
            async for doc in query.stream():
                data = doc.to_dict()
                result[data.get("fork_repo", doc.id)] = data
            logger.info("Loaded %d fork documents from Firestore", len(result))
            return result
        except Exception as exc:
            logger.error("Failed to load forks from Firestore: %s", exc)
            return {}

    async def batch_upsert(self, forks: List[ForkDocument]) -> None:
        """Upsert multiple fork documents in batches of 500."""
        if not forks:
            return
        try:
            batch = self._db.batch()
            count = 0
            for fork in forks:
                ref = self._db.collection(self._collection).document(fork.doc_id)
                batch.set(ref, _fork_to_dict(fork), merge=True)
                count += 1
                if count % 500 == 0:
                    await batch.commit()
                    batch = self._db.batch()
            if count % 500 != 0:
                await batch.commit()
            logger.info("Upserted %d fork documents to Firestore", count)
        except Exception as exc:
            logger.error("Failed to upsert forks to Firestore: %s", exc)

    async def batch_update(self, forks: List[ForkDocument]) -> None:
        """Update fork documents — same as upsert but semantically distinct."""
        await self.batch_upsert(forks)

    async def save_run(self, run: SyncRun) -> str:
        """Save a sync run document and return its ID."""
        try:
            data = {
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "duration_seconds": run.duration_seconds,
                "total_forks": run.total_forks,
                "checked": run.checked,
                "synced": run.synced,
                "already_current": run.already_current,
                "skipped_schedule": run.skipped_schedule,
                "skipped_ahead": run.skipped_ahead,
                "skipped_archived": run.skipped_archived,
                "errors": run.errors,
                "api_calls_used": run.api_calls_used,
                "dry_run": run.dry_run,
            }
            ref = await self._db.collection("sync_runs").add(data)
            doc_id = ref[1].id
            logger.info("Saved sync run document: %s", doc_id)
            return doc_id
        except Exception as exc:
            logger.error("Failed to save sync run: %s", exc)
            return ""

    async def get_latest_run(self) -> Optional[dict]:
        """Return the most recent sync run document."""
        try:
            query = (
                self._db.collection("sync_runs")
                .order_by("started_at", direction=fs.Query.DESCENDING)
                .limit(1)
            )
            async for doc in query.stream():
                return doc.to_dict()
            return None
        except Exception as exc:
            logger.error("Failed to get latest run: %s", exc)
            return None

    async def query_forks(
        self,
        tier: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[dict]:
        """Query forks with optional filters."""
        try:
            query = self._db.collection(self._collection)
            if tier:
                query = query.where("schedule_tier", "==", tier)
            if status:
                query = query.where("status", "==", status)
            results: List[dict] = []
            async for doc in query.stream():
                results.append(doc.to_dict())
            return results
        except Exception as exc:
            logger.error("Failed to query forks: %s", exc)
            return []


def _fork_to_dict(fork: ForkDocument) -> dict:
    return {
        "fork_owner": fork.fork_owner,
        "fork_repo": fork.fork_repo,
        "fork_branch": fork.fork_branch,
        "upstream_owner": fork.upstream_owner,
        "upstream_repo": fork.upstream_repo,
        "upstream_branch": fork.upstream_branch,
        "status": fork.status,
        "behind_by": fork.behind_by,
        "ahead_by": fork.ahead_by,
        "last_synced_at": fork.last_synced_at,
        "last_checked_at": fork.last_checked_at,
        "last_error": fork.last_error,
        "schedule_tier": fork.schedule_tier,
        "upstream_pushed_at": fork.upstream_pushed_at,
        "last_upstream_sha": fork.last_upstream_sha,
        "compare_etag": fork.compare_etag,
        "archived": fork.archived,
    }
