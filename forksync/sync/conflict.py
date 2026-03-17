"""
Conflict detection and GitHub Issue creation for forksync.

When a fork has diverged from upstream (both ahead AND behind),
forksync creates a GitHub Issue in the fork repo to flag it for
manual attention instead of silently failing.
"""

import logging
from typing import Optional

from forksync import ForkStatus, SyncState

logger = logging.getLogger(__name__)

ISSUE_TITLE = "forksync: manual sync needed"

ISSUE_LABELS = ["forksync", "needs-attention"]

ISSUE_TEMPLATE = """## Fork sync requires manual attention

**Repository**: {repo_name}
**Upstream**: [{upstream_repo}]({upstream_url})

### Status
- Your fork is **{ahead_by} commits ahead** of upstream
- Upstream is **{behind_by} commits ahead** of your fork

This means your fork has diverged from upstream and cannot be
automatically fast-forwarded. Manual resolution is required.

### How to resolve
```bash
git remote add upstream {upstream_url}
git fetch upstream
git merge upstream/{upstream_branch}
# resolve any conflicts
git push origin {fork_branch}
```

### Upstream changes
[View diff on GitHub]({diff_url})

---
*Created by [forksync](https://github.com/perditioinc/forksync)*
"""


class ConflictReporter:
    """
    When a fork is diverged (ahead AND behind), creates a GitHub Issue
    in the USER'S fork repo to flag it for manual attention.

    Avoids creating duplicate issues by checking for existing open issues
    with the 'forksync' label before creating a new one.
    """

    def __init__(self, username: str):
        self.username = username

    def _render_issue_body(self, status: ForkStatus) -> str:
        """Render the issue body from the template."""
        upstream_owner_repo = status.upstream_repo
        upstream_branch = status.upstream_default_branch
        fork_branch = status.fork_default_branch

        # Build upstream diff URL
        diff_url = (
            f"{status.upstream_url}/compare/{upstream_branch}...{self.username}:{fork_branch}"
        )

        return ISSUE_TEMPLATE.format(
            repo_name=status.repo_name,
            upstream_repo=status.upstream_repo,
            upstream_url=status.upstream_url,
            ahead_by=status.ahead_by,
            behind_by=status.behind_by,
            upstream_branch=upstream_branch,
            fork_branch=fork_branch,
            diff_url=diff_url,
        )

    async def _has_existing_conflict_issue(self, status: ForkStatus, rest_client) -> Optional[str]:
        """
        Check if a forksync conflict issue already exists for this repo.
        Returns the existing issue URL if found, None otherwise.
        """
        try:
            issues = await rest_client.list_issues(
                owner=self.username,
                repo=status.repo_name,
                labels=["forksync"],
                state="open",
            )
            for issue in issues:
                if ISSUE_TITLE in issue.get("title", ""):
                    existing_url = issue.get("html_url", "")
                    logger.info(
                        "Existing conflict issue found for %s: %s",
                        status.repo_name,
                        existing_url,
                    )
                    return existing_url
        except Exception as exc:
            logger.warning(
                "Could not check existing issues for %s: %s",
                status.repo_name,
                exc,
            )
        return None

    async def create_conflict_issue(
        self,
        status: ForkStatus,
        rest_client,
    ) -> str:
        """
        Creates a GitHub Issue with:
        - Clear title: "forksync: manual sync needed"
        - Upstream diff link
        - Instructions for manual resolution
        - Labels: ["forksync", "needs-attention"]

        Skips creating duplicate issues if one already exists.
        Returns the issue URL.
        """
        # Check for existing issue first
        existing_url = await self._has_existing_conflict_issue(status, rest_client)
        if existing_url:
            logger.info(
                "Skipping duplicate issue creation for %s — issue already exists: %s",
                status.repo_name,
                existing_url,
            )
            return existing_url

        body = self._render_issue_body(status)

        try:
            issue_url = await rest_client.create_issue(
                owner=self.username,
                repo=status.repo_name,
                title=ISSUE_TITLE,
                body=body,
                labels=ISSUE_LABELS,
            )
            logger.info("Created conflict issue for %s: %s", status.repo_name, issue_url)
            return issue_url

        except Exception as exc:
            logger.error(
                "Failed to create conflict issue for %s: %s",
                status.repo_name,
                exc,
            )
            raise
