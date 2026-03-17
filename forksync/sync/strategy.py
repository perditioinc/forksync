"""
Sync strategies for forksync.

Defines how forks should be synced. Currently only
fast-forward-only is supported (safe for unattended runs).
"""

from enum import Enum


class SyncStrategy(Enum):
    """Available sync strategies."""

    FAST_FORWARD_ONLY = "fast_forward_only"
    """
    Only sync forks where the fork branch can be fast-forwarded to upstream.
    This means: fork has NO commits that upstream doesn't have (ahead_by == 0).
    Safe for unattended/nightly runs because it never discards local commits.
    """


class FastForwardOnlyStrategy:
    """
    Strategy implementation: fast-forward only.

    Rules:
    - If fork is ahead_by > 0: SKIP (has local commits)
    - If fork is behind_by > 0 and ahead_by == 0: SYNC (safe to fast-forward)
    - If fork is ahead AND behind (diverged): SKIP (report conflict)
    - Up-to-date or unknown: SKIP
    """

    name = SyncStrategy.FAST_FORWARD_ONLY

    @staticmethod
    def should_sync(ahead_by: int, behind_by: int) -> bool:
        """
        Returns True if the fork can be safely synced.

        A fork is safe to sync iff:
        - It has no commits ahead of upstream (ahead_by == 0)
        - It has commits to pull in (behind_by > 0)
        """
        return ahead_by == 0 and behind_by > 0

    @staticmethod
    def should_skip_ahead(ahead_by: int) -> bool:
        """Returns True if fork should be skipped because it's ahead."""
        return ahead_by > 0

    def __repr__(self) -> str:
        return "FastForwardOnlyStrategy()"
