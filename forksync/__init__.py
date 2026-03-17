"""
forksync — Automated GitHub Fork Sync Tool

Keeps your forks automatically synced with upstream repos nightly.
Zero infrastructure. Zero cost. Zero human intervention for clean syncs.
"""

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from typing import Optional


class SyncState(Enum):
    UP_TO_DATE = "up_to_date"   # fork matches upstream HEAD
    BEHIND = "behind"            # upstream has new commits
    AHEAD = "ahead"              # fork has local commits
    DIVERGED = "diverged"        # both have unique commits
    CONFLICT = "conflict"        # merge would produce conflicts
    ARCHIVED = "archived"        # upstream is archived
    UNKNOWN = "unknown"          # could not determine


@dataclass
class ForkStatus:
    repo_name: str
    fork_url: str
    upstream_repo: str              # owner/repo
    upstream_url: str
    fork_default_branch: str
    upstream_default_branch: str
    state: SyncState
    behind_by: int                  # commits behind upstream
    ahead_by: int                   # commits ahead of upstream
    upstream_last_pushed: Optional[datetime]
    is_archived: bool
    can_fast_forward: bool          # True if safe to auto-sync


@dataclass
class SyncResult:
    repo_name: str
    state: SyncState
    action_taken: str               # "synced", "skipped", "conflict_reported", "dry_run"
    commits_merged: int
    error: Optional[str]
    duration_seconds: float
    issue_url: Optional[str]        # GitHub issue URL if conflict reported


__all__ = ["SyncState", "ForkStatus", "SyncResult"]
