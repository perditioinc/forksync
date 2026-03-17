"""
forksync v2 — GCP-native GitHub fork sync tool.

Public API:
- run_sync(config, dry_run=False) -> SyncRun
- ForkDocument, ForkStatus, SyncRun, SyncState, SyncResult
"""

from forksync.engine import run_sync
from forksync.models import ForkDocument, ForkStatus, SyncRun, SyncState, SyncResult

__all__ = [
    "run_sync",
    "ForkDocument",
    "ForkStatus",
    "SyncRun",
    "SyncState",
    "SyncResult",
]
