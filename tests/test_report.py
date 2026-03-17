"""
Tests for forksync/report/markdown.py

Tests:
- Report generation with known data produces correct markdown
- All sections are present and populated
- Empty sections render gracefully
- Duration formatting works correctly
"""

import pytest
from datetime import datetime, timezone

from forksync import ForkStatus, SyncResult, SyncState
from forksync.report.markdown import ReportGenerator, _format_duration


def make_status(
    repo_name: str,
    state: SyncState = SyncState.BEHIND,
    behind: int = 5,
    ahead: int = 0,
) -> ForkStatus:
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
        state=state,
        schedule_tier="nightly",
        due=True,
    )


def make_result(
    repo_name: str,
    action_taken: str,
    state: SyncState = SyncState.BEHIND,
    commits_merged: int = 0,
    issue_url: str = None,
    error: str = None,
) -> SyncResult:
    return SyncResult(
        repo_name=repo_name,
        state=state,
        action_taken=action_taken,
        commits_merged=commits_merged,
        error=error,
        duration_seconds=1.5,
        issue_url=issue_url,
    )


# ---------------------------------------------------------------------------
# Test 1: Report has all required sections
# ---------------------------------------------------------------------------

def test_report_has_all_sections():
    """Generated report must contain all required markdown sections."""
    generator = ReportGenerator()
    results = [
        make_result("synced-repo", "synced", state=SyncState.BEHIND, commits_merged=3),
        make_result(
            "conflict-repo",
            "conflict_reported",
            state=SyncState.DIVERGED,
            issue_url="https://github.com/testuser/conflict-repo/issues/1",
        ),
        make_result("ahead-repo", "skipped", state=SyncState.AHEAD),
        make_result("current-repo", "skipped", state=SyncState.UP_TO_DATE),
        make_result("archived-repo", "skipped", state=SyncState.ARCHIVED),
    ]

    statuses = {
        r.repo_name: make_status(r.repo_name, state=r.state)
        for r in results
    }
    # Fix ahead status
    statuses["ahead-repo"].ahead = 2
    statuses["ahead-repo"].behind = 0

    report = generator.generate(
        results=results,
        statuses=statuses,
        username="testuser",
        duration=42.5,
        api_calls=15,
    )

    # Must have title
    assert "# Fork Sync Report" in report
    # Must have username
    assert "testuser" in report
    # Must have summary section
    assert "## Summary" in report
    # Must have synced section
    assert "## \u2705 Synced" in report
    # Must have ahead section
    assert "## \u2b06\ufe0f Skipped" in report
    # Must have API calls
    assert "API calls used" in report
    assert "15" in report
    # Must have forksync credit
    assert "forksync" in report


# ---------------------------------------------------------------------------
# Test 2: Synced repos appear in synced table
# ---------------------------------------------------------------------------

def test_synced_repos_appear_in_table():
    """Synced repos must appear in the synced section with commit count."""
    generator = ReportGenerator()
    results = [
        make_result("cool-repo", "synced", state=SyncState.BEHIND, commits_merged=7),
    ]
    statuses = {"cool-repo": make_status("cool-repo")}

    report = generator.generate(
        results=results,
        statuses=statuses,
        username="testuser",
        duration=10.0,
        api_calls=5,
    )

    assert "cool-repo" in report
    assert "7" in report


# ---------------------------------------------------------------------------
# Test 3: Conflict repos do NOT appear as a separate section (diverged forks
#          create GitHub Issues — no separate section in the report)
# ---------------------------------------------------------------------------

def test_conflict_repos_not_in_separate_section():
    """
    Diverged repos have GitHub Issues created but do not appear
    in a 'Needs Manual Attention' section — that section was removed.
    """
    generator = ReportGenerator()
    issue_url = "https://github.com/testuser/diverged-repo/issues/42"
    results = [
        make_result(
            "diverged-repo",
            "conflict_reported",
            state=SyncState.DIVERGED,
            issue_url=issue_url,
        ),
    ]
    diverged_status = make_status("diverged-repo", state=SyncState.DIVERGED)
    diverged_status.ahead = 3
    diverged_status.behind = 8
    statuses = {"diverged-repo": diverged_status}

    report = generator.generate(
        results=results,
        statuses=statuses,
        username="testuser",
        duration=5.0,
        api_calls=3,
    )

    # "Needs Manual Attention" section was removed
    assert "Needs Manual Attention" not in report


# ---------------------------------------------------------------------------
# Test 4: Ahead repos appear in skipped list
# ---------------------------------------------------------------------------

def test_ahead_repos_appear_in_ahead_list():
    """Repos skipped because they're ahead must appear in the ahead section."""
    generator = ReportGenerator()
    results = [
        make_result("my-custom-fork", "skipped", state=SyncState.AHEAD),
    ]
    ahead_status = make_status("my-custom-fork", state=SyncState.AHEAD, ahead=5)
    ahead_status.ahead = 5
    statuses = {"my-custom-fork": ahead_status}

    report = generator.generate(
        results=results,
        statuses=statuses,
        username="testuser",
        duration=3.0,
        api_calls=2,
    )

    assert "my-custom-fork" in report
    assert "5" in report  # ahead count


# ---------------------------------------------------------------------------
# Test 5: Duration formatting
# ---------------------------------------------------------------------------

def test_duration_format_under_60s():
    """Duration under 60 seconds should show as seconds."""
    assert _format_duration(42.7) == "42.7s"
    assert _format_duration(5.0) == "5.0s"


def test_duration_format_over_60s():
    """Duration over 60 seconds should show as minutes and seconds."""
    result = _format_duration(125.0)
    assert "m" in result
    assert "s" in result
    assert "2m" in result


# ---------------------------------------------------------------------------
# Test 6: Empty results render gracefully
# ---------------------------------------------------------------------------

def test_empty_results_render_gracefully():
    """Report with zero results should not crash and should show zeros."""
    generator = ReportGenerator()
    report = generator.generate(
        results=[],
        statuses={},
        username="emptyuser",
        duration=1.0,
        api_calls=1,
    )

    assert "# Fork Sync Report" in report
    assert "emptyuser" in report
    # All counts should be 0 or represented as such
    assert "| ✅ Synced (verified) | 0 |" in report


# ---------------------------------------------------------------------------
# Test 7: Dry run results show in synced section with dry run marker
# ---------------------------------------------------------------------------

def test_dry_run_results_appear_with_dry_run_marker():
    """Dry run results should appear in synced section with a marker."""
    generator = ReportGenerator()
    results = [
        make_result("would-sync-repo", "dry_run", state=SyncState.BEHIND),
    ]
    statuses = {"would-sync-repo": make_status("would-sync-repo")}

    report = generator.generate(
        results=results,
        statuses=statuses,
        username="testuser",
        duration=2.0,
        api_calls=1,
    )

    assert "would-sync-repo" in report
    assert "dry run" in report.lower()


# ---------------------------------------------------------------------------
# Test 8: Summary counts match result list
# ---------------------------------------------------------------------------

def test_summary_counts_are_accurate():
    """The summary table counts must match actual result list."""
    generator = ReportGenerator()
    results = [
        make_result("r1", "synced", state=SyncState.BEHIND, commits_merged=2),
        make_result("r2", "synced", state=SyncState.BEHIND, commits_merged=1),
        make_result("r3", "conflict_reported", state=SyncState.DIVERGED,
                    issue_url="https://github.com/t/r3/issues/1"),
        make_result("r4", "skipped", state=SyncState.UP_TO_DATE),
        make_result("r5", "skipped", state=SyncState.ARCHIVED),
        make_result("r6", "skipped", state=SyncState.AHEAD),
    ]
    statuses = {r.repo_name: make_status(r.repo_name, state=r.state) for r in results}

    report = generator.generate(
        results=results,
        statuses=statuses,
        username="testuser",
        duration=10.0,
        api_calls=8,
    )

    # 2 synced
    assert "| ✅ Synced (verified) | 2 |" in report
    # 1 up to date
    assert "| ⏭️ Already current   | 1 |" in report
    # 1 archived
    assert "| 🗄️ Archived (skipped)| 1 |" in report
    # 1 ahead
    assert "| ⬆️ Ahead (skipped)   | 1 |" in report
