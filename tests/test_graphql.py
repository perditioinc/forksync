"""
Tests for forksync/github/graphql.py

Tests:
- Pagination works correctly (multiple pages)
- Batch fetch uses far fewer API calls than one-per-repo
- ForkStatus objects are parsed correctly
- Handles missing parent (non-fork) gracefully
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from forksync import ForkStatus, SyncState
from forksync.github.graphql import GitHubGraphQLClient, FORK_STATUS_QUERY
from forksync.github.rate_limit import RateLimitTracker


def _make_repo_node(
    name: str,
    fork_oid: str = "abc123",
    upstream_oid: str = "abc123",
    is_archived: bool = False,
    pushed_at: str = "2024-01-01T00:00:00Z",
    fork_branch: str = "main",
    upstream_branch: str = "main",
    upstream_owner: str = "upstream",
) -> dict:
    """Helper to build a GraphQL repository node."""
    return {
        "name": name,
        "url": f"https://github.com/testuser/{name}",
        "defaultBranchRef": {"name": fork_branch},
        "parent": {
            "nameWithOwner": f"{upstream_owner}/{name}",
            "url": f"https://github.com/{upstream_owner}/{name}",
            "defaultBranchRef": {
                "name": upstream_branch,
                "target": {"oid": upstream_oid},
            },
            "pushedAt": pushed_at,
            "isArchived": is_archived,
        },
        "ref": {"target": {"oid": fork_oid}},
    }


def _make_graphql_response(
    nodes: list,
    has_next_page: bool = False,
    end_cursor: str = None,
) -> dict:
    """Build a mock GraphQL response dict."""
    return {
        "data": {
            "user": {
                "repositories": {
                    "pageInfo": {
                        "hasNextPage": has_next_page,
                        "endCursor": end_cursor,
                    },
                    "nodes": nodes,
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# Test 1: Single page, no pagination
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_page_fetch():
    """A single page of 3 forks should produce 3 ForkStatus objects."""
    nodes = [
        _make_repo_node("repo-a", fork_oid="aaa", upstream_oid="aaa"),
        _make_repo_node("repo-b", fork_oid="bbb", upstream_oid="ccc"),
        _make_repo_node("repo-c", is_archived=True),
    ]
    mock_response = _make_graphql_response(nodes, has_next_page=False)

    client = GitHubGraphQLClient(token="fake-token")

    # Patch the _execute_query method
    client._execute_query = AsyncMock(return_value=mock_response)

    statuses = await client.get_all_fork_statuses("testuser")

    assert len(statuses) == 3
    assert client._execute_query.call_count == 1  # Single page, single call


# ---------------------------------------------------------------------------
# Test 2: Pagination works correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pagination_fetches_all_pages():
    """
    When there are multiple pages, all forks should be returned
    and _execute_query should be called once per page.
    """
    page1_nodes = [_make_repo_node(f"repo-{i}") for i in range(3)]
    page2_nodes = [_make_repo_node(f"repo-{i}") for i in range(3, 6)]

    page1_response = _make_graphql_response(
        page1_nodes, has_next_page=True, end_cursor="cursor1"
    )
    page2_response = _make_graphql_response(
        page2_nodes, has_next_page=False
    )

    client = GitHubGraphQLClient(token="fake-token")
    client._execute_query = AsyncMock(side_effect=[page1_response, page2_response])

    statuses = await client.get_all_fork_statuses("testuser")

    assert len(statuses) == 6
    assert client._execute_query.call_count == 2

    # Verify second call used the cursor from page 1
    second_call_args = client._execute_query.call_args_list[1]
    # The cursor is the third positional arg (client, login, after)
    assert second_call_args[0][2] == "cursor1"  # after=cursor1


# ---------------------------------------------------------------------------
# Test 3: Batch fetch uses far fewer calls than one-per-repo
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_fetch_uses_single_query():
    """
    For 700 forks (7 pages of 100), total API calls should be
    far less than 700 — specifically 7 (one per page).
    """
    # Simulate 7 pages of 100 repos each
    page_responses = []
    for page_num in range(7):
        is_last = page_num == 6
        nodes = [_make_repo_node(f"repo-{page_num * 100 + i}") for i in range(100)]
        cursor = f"cursor{page_num + 1}" if not is_last else None
        page_responses.append(
            _make_graphql_response(nodes, has_next_page=not is_last, end_cursor=cursor)
        )

    client = GitHubGraphQLClient(token="fake-token")
    client._execute_query = AsyncMock(side_effect=page_responses)

    statuses = await client.get_all_fork_statuses("testuser")

    assert len(statuses) == 700
    # Should be 7 calls (one per page), NOT 700
    assert client._execute_query.call_count == 7
    assert client._execute_query.call_count < 20  # Much less than 700


# ---------------------------------------------------------------------------
# Test 4: OID equality → UP_TO_DATE
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_equal_oids_means_up_to_date():
    """When fork OID equals upstream OID, state should be UP_TO_DATE."""
    nodes = [_make_repo_node("synced-repo", fork_oid="same_oid", upstream_oid="same_oid")]
    mock_response = _make_graphql_response(nodes)

    client = GitHubGraphQLClient(token="fake-token")
    client._execute_query = AsyncMock(return_value=mock_response)

    statuses = await client.get_all_fork_statuses("testuser")

    assert len(statuses) == 1
    assert statuses[0].state == SyncState.UP_TO_DATE
    assert statuses[0].behind_by == 0
    assert statuses[0].ahead_by == 0


# ---------------------------------------------------------------------------
# Test 5: Archived upstream → ARCHIVED state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_archived_upstream_gives_archived_state():
    """When upstream is archived, state should be ARCHIVED."""
    nodes = [
        _make_repo_node("archived-repo", is_archived=True, fork_oid="aaa", upstream_oid="bbb")
    ]
    mock_response = _make_graphql_response(nodes)

    client = GitHubGraphQLClient(token="fake-token")
    client._execute_query = AsyncMock(return_value=mock_response)

    statuses = await client.get_all_fork_statuses("testuser")

    assert len(statuses) == 1
    assert statuses[0].state == SyncState.ARCHIVED
    assert statuses[0].is_archived is True


# ---------------------------------------------------------------------------
# Test 6: Differing OIDs → BEHIND (before REST refinement)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_differing_oids_marked_behind():
    """
    When fork OID differs from upstream OID (non-archived),
    node is initially marked BEHIND (awaiting REST refinement).
    """
    nodes = [
        _make_repo_node("behind-repo", fork_oid="fork_oid", upstream_oid="upstream_oid")
    ]
    mock_response = _make_graphql_response(nodes)

    client = GitHubGraphQLClient(token="fake-token")
    client._execute_query = AsyncMock(return_value=mock_response)

    # No rest_client passed, so no refinement
    statuses = await client.get_all_fork_statuses("testuser")

    assert len(statuses) == 1
    assert statuses[0].state == SyncState.BEHIND
    # behind_by == -1 signals needs REST refinement
    assert statuses[0].behind_by == -1


# ---------------------------------------------------------------------------
# Test 7: REST refinement correctly classifies DIVERGED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rest_refinement_detects_diverged():
    """
    When REST comparison shows both ahead_by > 0 and behind_by > 0,
    state should be refined to DIVERGED.
    """
    nodes = [
        _make_repo_node("diverged-repo", fork_oid="fork_oid", upstream_oid="upstream_oid")
    ]
    mock_response = _make_graphql_response(nodes)

    client = GitHubGraphQLClient(token="fake-token")
    client._execute_query = AsyncMock(return_value=mock_response)

    # Mock REST client returning ahead_by=3, behind_by=10
    rest_client = AsyncMock()
    rest_client.get_fork_commit_comparison = AsyncMock(return_value=(3, 10))

    statuses = await client.get_all_fork_statuses("testuser", rest_client=rest_client)

    assert len(statuses) == 1
    assert statuses[0].state == SyncState.DIVERGED
    assert statuses[0].ahead_by == 3
    assert statuses[0].behind_by == 10
    assert statuses[0].can_fast_forward is False


# ---------------------------------------------------------------------------
# Test 8: Node with no parent is skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_node_without_parent_is_skipped():
    """
    A repository node without a 'parent' key should be skipped gracefully.
    (This should not happen with isFork:true, but we guard anyway.)
    """
    nodes = [
        {
            "name": "not-a-fork",
            "url": "https://github.com/testuser/not-a-fork",
            "defaultBranchRef": {"name": "main"},
            "parent": None,  # No parent
            "ref": {"target": {"oid": "abc"}},
        }
    ]
    mock_response = _make_graphql_response(nodes)

    client = GitHubGraphQLClient(token="fake-token")
    client._execute_query = AsyncMock(return_value=mock_response)

    statuses = await client.get_all_fork_statuses("testuser")

    # Node with no parent is filtered out
    assert len(statuses) == 0
