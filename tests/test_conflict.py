"""
Tests for forksync/sync/conflict.py

Tests:
- Issue template rendering produces correct content
- Duplicate issue detection prevents double-creation
- Issue is created when none exists
"""

import pytest
from unittest.mock import AsyncMock
from datetime import datetime, timezone

from forksync import ForkStatus, SyncState
from forksync.sync.conflict import ConflictReporter, ISSUE_TITLE, ISSUE_LABELS, ISSUE_TEMPLATE


def make_diverged_status(
    repo_name: str = "my-fork",
    ahead: int = 5,
    behind: int = 12,
) -> ForkStatus:
    """Build a diverged ForkStatus for testing."""
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
        archived=False,
        state=SyncState.DIVERGED,
        schedule_tier="nightly",
        due=True,
    )


# ---------------------------------------------------------------------------
# Test 1: Issue template renders correctly
# ---------------------------------------------------------------------------

def test_issue_template_contains_key_fields():
    """
    Rendered issue body must contain:
    - repo name
    - upstream URL
    - ahead and behind counts
    - git commands for manual resolution
    - forksync credit link
    """
    reporter = ConflictReporter(username="testuser")
    status = make_diverged_status(repo_name="cool-project", ahead=3, behind=8)

    body = reporter._render_issue_body(status)

    assert "cool-project" in body
    assert "upstream/cool-project" in body
    assert "https://github.com/upstream/cool-project" in body
    assert "3 commits ahead" in body
    assert "8 commits ahead" in body
    assert "git remote add upstream" in body
    assert "git fetch upstream" in body
    assert "git merge upstream/main" in body
    assert "git push origin main" in body
    assert "forksync" in body


def test_issue_template_diff_url_is_correct():
    """Diff URL should point to upstream comparing fork vs upstream."""
    reporter = ConflictReporter(username="myuser")
    status = make_diverged_status(repo_name="myrepo")

    body = reporter._render_issue_body(status)

    # The diff URL should allow viewing upstream changes
    assert "compare" in body
    assert "myuser" in body


def test_issue_title_is_correct():
    """Issue title constant should match expected value."""
    assert ISSUE_TITLE == "forksync: manual sync needed"


def test_issue_labels_are_correct():
    """Issue labels should include forksync and needs-attention."""
    assert "forksync" in ISSUE_LABELS
    assert "needs-attention" in ISSUE_LABELS


# ---------------------------------------------------------------------------
# Test 2: Issue is created when none exists
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_creates_issue_when_none_exists():
    """
    When no open forksync issues exist, create_conflict_issue
    should call rest_client.create_issue exactly once.
    """
    reporter = ConflictReporter(username="testuser")
    status = make_diverged_status()

    rest_client = AsyncMock()
    rest_client.list_issues = AsyncMock(return_value=[])  # No existing issues
    rest_client.create_issue = AsyncMock(
        return_value="https://github.com/testuser/my-fork/issues/1"
    )

    issue_url = await reporter.create_conflict_issue(status, rest_client)

    rest_client.create_issue.assert_called_once()
    call_kwargs = rest_client.create_issue.call_args[1]
    assert call_kwargs["owner"] == "testuser"
    assert call_kwargs["repo"] == "my-fork"
    assert call_kwargs["title"] == ISSUE_TITLE
    assert "forksync" in call_kwargs["labels"]
    assert issue_url == "https://github.com/testuser/my-fork/issues/1"


# ---------------------------------------------------------------------------
# Test 3: Duplicate issue detection prevents double-creation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skips_duplicate_issue_creation():
    """
    When an open forksync issue already exists, create_conflict_issue
    should NOT call rest_client.create_issue — it returns the existing URL.
    """
    reporter = ConflictReporter(username="testuser")
    status = make_diverged_status()

    existing_url = "https://github.com/testuser/my-fork/issues/99"
    existing_issue = {
        "title": ISSUE_TITLE,
        "html_url": existing_url,
        "state": "open",
    }

    rest_client = AsyncMock()
    rest_client.list_issues = AsyncMock(return_value=[existing_issue])
    rest_client.create_issue = AsyncMock()

    issue_url = await reporter.create_conflict_issue(status, rest_client)

    # Should NOT create a new issue
    rest_client.create_issue.assert_not_called()
    assert issue_url == existing_url


# ---------------------------------------------------------------------------
# Test 4: Issue list failure is handled gracefully
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_issue_list_failure_falls_back_to_creation():
    """
    If listing issues fails, create_conflict_issue should still
    attempt to create the issue (fail-open, not fail-closed).
    """
    reporter = ConflictReporter(username="testuser")
    status = make_diverged_status()

    rest_client = AsyncMock()
    rest_client.list_issues = AsyncMock(side_effect=Exception("API error"))
    rest_client.create_issue = AsyncMock(
        return_value="https://github.com/testuser/my-fork/issues/2"
    )

    issue_url = await reporter.create_conflict_issue(status, rest_client)

    # Should still create the issue despite list failure
    rest_client.create_issue.assert_called_once()
    assert issue_url == "https://github.com/testuser/my-fork/issues/2"


# ---------------------------------------------------------------------------
# Test 5: Issue body contains upstream branch and fork branch
# ---------------------------------------------------------------------------

def test_issue_body_contains_branch_names():
    """Issue body should contain the correct branch names."""
    reporter = ConflictReporter(username="testuser")
    status = ForkStatus(
        name="my-project",
        fork_url="https://github.com/testuser/my-project",
        fork_branch="develop",
        upstream_owner="original-org",
        upstream_repo="my-project",
        upstream_url="https://github.com/original-org/my-project",
        upstream_branch="master",
        upstream_pushed_at=None,
        ahead=2,
        behind=3,
        archived=False,
        state=SyncState.DIVERGED,
        schedule_tier="nightly",
        due=True,
    )

    body = reporter._render_issue_body(status)

    assert "upstream/master" in body
    assert "origin develop" in body
