from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class SyncState(Enum):
    UP_TO_DATE = "up_to_date"
    BEHIND = "behind"
    AHEAD = "ahead"
    DIVERGED = "diverged"
    ARCHIVED = "archived"
    UNKNOWN = "unknown"


@dataclass
class ForkDocument:
    fork_owner: str
    fork_repo: str
    fork_branch: str
    upstream_owner: str
    upstream_repo: str
    upstream_branch: str
    status: str               # 'synced', 'behind', 'ahead', 'diverged', 'error', 'unknown'
    behind_by: int = 0
    ahead_by: int = 0
    last_synced_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    last_error: Optional[str] = None
    schedule_tier: str = "nightly"
    upstream_pushed_at: Optional[datetime] = None
    last_upstream_sha: str = ""
    compare_etag: Optional[str] = None
    archived: bool = False

    @property
    def doc_id(self) -> str:
        return f"{self.fork_owner}_{self.fork_repo}"

    @property
    def upstream_full(self) -> str:
        return f"{self.upstream_owner}/{self.upstream_repo}"


@dataclass
class ForkStatus:
    state: SyncState
    behind: int
    ahead: int
    etag: Optional[str] = None

    @classmethod
    def from_cache(cls, data: dict) -> "ForkStatus":
        return cls(
            state=SyncState(data["state"]),
            behind=data.get("behind", 0),
            ahead=data.get("ahead", 0),
        )

    def to_cache(self) -> dict:
        return {"state": self.state.value, "behind": self.behind, "ahead": self.ahead}


@dataclass
class SyncResult:
    fork: ForkDocument
    success: bool
    stdout: str = ""
    stderr: str = ""
    verified: bool = False
    error: Optional[str] = None


@dataclass
class SyncRun:
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_seconds: float = 0.0
    total_forks: int = 0
    checked: int = 0
    synced: int = 0
    already_current: int = 0
    skipped_schedule: int = 0
    skipped_ahead: int = 0
    skipped_archived: int = 0
    errors: int = 0
    api_calls_used: int = 0
    dry_run: bool = False
