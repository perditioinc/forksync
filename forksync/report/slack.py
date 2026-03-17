"""
Slack/Discord webhook notifications for forksync.

Sends:
- Immediate conflict notifications when diverged forks are found
- Daily summary after sync completes

Never crashes — all webhook calls are wrapped in try/except.
"""

import logging
from typing import List, Optional

import httpx

from forksync import SyncResult, SyncState

logger = logging.getLogger(__name__)


def _build_conflict_text(conflicts: List[SyncResult]) -> str:
    """Build conflict notification text."""
    lines = [f"\u26a0\ufe0f forksync: {len(conflicts)} repo(s) need manual attention"]
    for result in conflicts[:10]:  # Cap at 10 to avoid huge messages
        issue_ref = f"\u2192 {result.issue_url}" if result.issue_url else ""
        lines.append(f"\u2022 {result.repo_name} {issue_ref}")
    if len(conflicts) > 10:
        lines.append(f"\u2022 ... and {len(conflicts) - 10} more")
    return "\n".join(lines)


def _build_summary_text(results: List[SyncResult], report_url: str) -> str:
    """Build daily summary notification text."""
    synced = sum(1 for r in results if r.action_taken == "synced")
    current = sum(
        1 for r in results if r.action_taken == "skipped" and r.state == SyncState.UP_TO_DATE
    )
    conflicts = sum(1 for r in results if r.action_taken == "conflict_reported")
    total = len(results)

    lines = [
        f"\u2705 forksync complete: {synced} synced, {current} current, {conflicts} conflicts ({total} total)"
    ]
    if report_url:
        lines.append(f"View full report: {report_url}")
    return "\n".join(lines)


class NotificationClient:
    """
    Sends sync summary to Slack or Discord webhooks.
    Only sends if configured. Never crashes if webhook fails.

    Conflict notification (immediate):
    "⚠️ forksync: 3 repos need manual attention
     • my-fork: 5 ahead, 12 behind → Issue #42"

    Daily summary (after sync):
    "✅ forksync complete: 47 synced, 652 current, 3 conflicts
     View full report: {report_url}"
    """

    def __init__(
        self,
        slack_webhook_url: Optional[str] = None,
        discord_webhook_url: Optional[str] = None,
        slack_enabled: bool = False,
        discord_enabled: bool = False,
    ):
        self.slack_webhook_url = slack_webhook_url
        self.discord_webhook_url = discord_webhook_url
        self.slack_enabled = slack_enabled and bool(slack_webhook_url)
        self.discord_enabled = discord_enabled and bool(discord_webhook_url)

    async def _send_slack(self, text: str) -> None:
        """Send a message to Slack webhook."""
        if not self.slack_enabled or not self.slack_webhook_url:
            return
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.slack_webhook_url,
                    json={"text": text},
                    timeout=15.0,
                )
                response.raise_for_status()
                logger.info("Slack notification sent")
        except Exception as exc:
            logger.warning("Slack notification failed (non-fatal): %s", exc)

    async def _send_discord(self, text: str) -> None:
        """Send a message to Discord webhook."""
        if not self.discord_enabled or not self.discord_webhook_url:
            return
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.discord_webhook_url,
                    json={"content": text},
                    timeout=15.0,
                )
                response.raise_for_status()
                logger.info("Discord notification sent")
        except Exception as exc:
            logger.warning("Discord notification failed (non-fatal): %s", exc)

    async def send_conflict_notification(self, conflicts: List[SyncResult]) -> None:
        """
        Send immediate conflict notification.

        Called as soon as conflicts are detected, before the full sync completes.
        Never crashes.
        """
        if not conflicts:
            return
        if not self.slack_enabled and not self.discord_enabled:
            logger.debug("No notification webhooks configured — skipping conflict notification")
            return

        text = _build_conflict_text(conflicts)
        logger.info("Sending conflict notification for %d repo(s)", len(conflicts))

        await self._send_slack(text)
        await self._send_discord(text)

    async def send_summary(
        self,
        results: List[SyncResult],
        report_url: str = "",
    ) -> None:
        """
        Send daily sync summary notification.

        Called after sync completes.
        Never crashes.
        """
        if not self.slack_enabled and not self.discord_enabled:
            logger.debug("No notification webhooks configured — skipping summary")
            return

        text = _build_summary_text(results, report_url)
        logger.info("Sending sync summary notification")

        await self._send_slack(text)
        await self._send_discord(text)
