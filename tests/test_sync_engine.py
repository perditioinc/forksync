"""
Tests for forksync/sync/engine.py

Tests the 5 core decision-tree scenarios:
1. Archived upstream is skipped
2. Ahead fork is skipped
3. Diverged fork creates conflict issue
4. Behind fork is synced
5. Dry run makes no changes
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from forksync import ForkStatus, SyncResult, SyncState
from forksync.sync.engine import SyncEngine
from forksync.sync.conflict import ConflictReporter


def make_fork_status(
    repo_name="test-repo",
    state=SyncState.BEHIND,
    behind=5,
    ahead=0,
    archived=False,
) -> ForkStatus:
    """Helper to build ForkStatus for tests."""
    return ForkStatus(
        name=repo_name,
        fork_url=f"https://github.com/testuser/{repo_name}",
        fork_branch="main",
        upstream_owner="upstream",
        upstream_repo=repo_name,
        upstream_url=f"https://github.com/upstream/{repo_name}",
        upstream_branch="main",
        upstream_pushed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        ahead=ahead,
        behind=behind,
        archived=archived,
        state=state,
        schedule_tier="nightly",
        due=True,
    )


def make_engine(
    dry_run=False,
    merge_upstream_result=None,
    conflict_issue_url="https://github.com/testuser/test-repo/issues/42",
) -> SyncEngine:
    """Build a SyncEngine with mocked dependencies."""
    rest_client = AsyncMock()
    rest_client.merge_upstream = AsyncMock(
        return_value=merge_upstream_result or {"merge_type": "fast-forward", "message": "ok"}
    )
    rest_client.create_issue = AsyncMock(return_value=conflict_issue_url)
    rest_client.list_issues = AsyncMock(return_value=[])  # no existing issues

    conflict_reporter = ConflictReporter(username="testuser")
    # Patch the rest_client into conflict reporter calls
    conflict_reporter.create_conflict_issue = AsyncMock(return_value=conflict_issue_url)

    engine = SyncEngine(
        rest_client=rest_client,
        conflict_reporter=conflict_reporter,
        history=None,
        dry_run=dry_run,
        username="testuser",
    )
    return engine


# ---------------------------------------------------------------------------
# Test 1: Archived upstream is skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_archived_upstream_is_skipped():
    """
    When upstream is archived, the fork must be skipped with
    action_taken='skipped' and error containing 'archived'.
    """
    status = make_fork_status(archived=True, state=SyncState.ARCHIVED)
    engine = make_engine()

    result = await engine.sync_fork(status)

    assert result.action_taken == "skipped"
    assert result.commits_merged == 0
    assert result.error is not None
    assert "archived" in result.error.lower()
    # Should NOT call merge_upstream
    engine.rest_client.merge_upstream.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: Ahead fork is skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ahead_fork_is_skipped():
    """
    When fork is ahead of upstream (has local commits), it must be skipped.
    commits_merged must be 0.
    """
    status = make_fork_status(
        state=SyncState.AHEAD,
        ahead=3,
        behind=0,
    )
    engine = make_engine()

    result = await engine.sync_fork(status)

    assert result.action_taken == "skipped"
    assert result.commits_merged == 0
    engine.rest_client.merge_upstream.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: Diverged fork creates conflict issue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_diverged_creates_issue():
    """
    When fork is diverged (ahead AND behind), engine must:
    - Report a conflict issue
    - Return action_taken='conflict_reported'
    - Return the issue URL
    """
    status = make_fork_status(
        state=SyncState.DIVERGED,
        ahead=5,
        behind=12,
    )
    engine = make_engine(conflict_issue_url="https://github.com/testuser/test-repo/issues/99")

    result = await engine.sync_fork(status)

    assert result.action_taken == "conflict_reported"
    assert result.issue_url is not None
    assert result.issue_url == "https://github.com/testuser/test-repo/issues/99"
    engine.conflict_reporter.create_conflict_issue.assert_called_once_with(
        status, engine.rest_client
    )
    engine.rest_client.merge_upstream.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: Behind fork is synced
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_behind_fork_syncs():
    """
    When fork is behind upstream and can fast-forward, engine must:
    - Run gh repo sync
    - Return action_taken='synced'
    - Return commits_merged equal to behind count
    """
    status = make_fork_status(
        state=SyncState.BEHIND,
        behind=5,
        ahead=0,
    )
    engine = make_engine()

    # Mock _run_gh_sync to succeed
    with patch("forksync.sync.engine._run_gh_sync", new_callable=AsyncMock) as mock_gh:
        mock_gh.return_value = (0, "synced", "")
        engine.rest_client.verify_sync = AsyncMock(return_value=True)

        result = await engine.sync_fork(status)

    assert result.action_taken == "synced"
    assert result.commits_merged == 5
    assert result.error is None


# ---------------------------------------------------------------------------
# Test 5: Dry run makes no changes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_makes_no_changes():
    """
    In dry_run mode, engine must:
    - NOT call merge_upstream
    - NOT create issues
    - Return action_taken='dry_run'
    - Return commits_merged == 0
    """
    status = make_fork_status(
        state=SyncState.BEHIND,
        behind=7,
        ahead=0,
    )
    engine = make_engine(dry_run=True)

    result = await engine.sync_fork(status)

    assert result.action_taken == "dry_run"
    assert result.commits_merged == 0
    engine.rest_client.merge_upstream.assert_not_called()
    engine.conflict_reporter.create_conflict_issue.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: Up-to-date fork is skipped cleanly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_up_to_date_fork_is_skipped():
    """Fork that is already current should be skipped with no error."""
    status = make_fork_status(
        state=SyncState.UP_TO_DATE,
        behind=0,
        ahead=0,
    )
    engine = make_engine()

    result = await engine.sync_fork(status)

    assert result.action_taken == "skipped"
    assert result.commits_merged == 0
    assert result.error is None
    engine.rest_client.merge_upstream.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7: sync_all runs concurrently
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_all_processes_all_forks():
    """sync_all must return one SyncResult per ForkStatus."""
    statuses = [
        make_fork_status(repo_name=f"repo-{i}", state=SyncState.UP_TO_DATE, behind=0)
        for i in range(5)
    ]
    engine = make_engine()

    results = await engine.sync_all(statuses)

    assert len(results) == 5
    for result in results:
        assert result.action_taken == "skipped"


# ---------------------------------------------------------------------------
# Test 8: Schedule-skipped forks are not processed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_schedule_skipped_fork_is_not_synced():
    """
    When a fork has due=False, it should be skipped with
    action_taken='schedule_skipped' immediately.
    """
    status = make_fork_status(
        state=SyncState.BEHIND,
        behind=5,
        ahead=0,
    )
    # Mark as not due
    status.due = False
    status.schedule_tier = "monthly"

    engine = make_engine()

    result = await engine.sync_fork(status)

    assert result.action_taken == "schedule_skipped"
    assert result.commits_merged == 0
    engine.rest_client.merge_upstream.assert_not_called()
