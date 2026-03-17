"""
forksync CLI entry point.

Commands:
  run      — run sync (or dry-run)
  status   — show all fork statuses without syncing
  history  — show sync history from SQLite
  config   — show loaded configuration
"""

import asyncio
import logging
import os
import sys
import time
from typing import List, Optional

import click

from forksync import ForkStatus, SyncResult, SyncState
from forksync.config import load_config
from forksync.github.graphql import GitHubGraphQLClient
from forksync.github.rate_limit import RateLimitTracker
from forksync.github.rest import GitHubRestClient
from forksync.report.markdown import ReportGenerator
from forksync.report.slack import NotificationClient
from forksync.storage.history import SyncHistory
from forksync.sync.conflict import ConflictReporter
from forksync.sync.engine import SyncEngine

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("forksync")


def _require_env(name: str) -> str:
    """Get a required environment variable or exit with an error."""
    value = os.environ.get(name, "").strip()
    if not value:
        click.echo(f"Error: environment variable {name!r} is not set.", err=True)
        sys.exit(1)
    return value


def _filter_statuses(
    statuses: List[ForkStatus],
    config,
    repos_filter: Optional[tuple],
) -> List[ForkStatus]:
    """
    Apply config-based filtering:
    - Honour skip_patterns (fnmatch-style)
    - Honour always_sync override
    - Honour --repos filter if specified
    """
    import fnmatch

    filtered = []
    for s in statuses:
        name = s.repo_name

        # --repos CLI filter takes priority
        if repos_filter:
            if name not in repos_filter:
                continue
            filtered.append(s)
            continue

        # always_sync overrides skip_patterns
        if name in (config.always_sync or []):
            filtered.append(s)
            continue

        # Check skip patterns
        skip = False
        for pattern in (config.skip_patterns or []):
            if fnmatch.fnmatch(name, pattern):
                skip = True
                logger.info("Skipping %s — matches skip pattern %r", name, pattern)
                break

        if not skip:
            filtered.append(s)

    return filtered


async def _run_sync(
    token: str,
    username: str,
    dry_run: bool,
    repos_filter: Optional[tuple],
    config_path: str,
) -> None:
    """Core async sync runner."""
    start_time = time.monotonic()

    config = load_config(config_path)
    rate_tracker = RateLimitTracker()

    click.echo(f"forksync — {'DRY RUN' if dry_run else 'LIVE'} mode")
    click.echo(f"User: {username}")

    # Build clients
    rest_client = GitHubRestClient(token=token, rate_tracker=rate_tracker)
    graphql_client = GitHubGraphQLClient(token=token, rate_tracker=rate_tracker)
    conflict_reporter = ConflictReporter(username=username)
    history = SyncHistory()

    # Fetch all fork statuses
    click.echo("Fetching fork statuses via GraphQL...")
    try:
        statuses = await graphql_client.get_all_fork_statuses(
            username=username,
            rest_client=rest_client,
        )
    except Exception as exc:
        logger.error("Failed to fetch fork statuses: %s", exc)
        click.echo(f"Error fetching fork statuses: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Found {len(statuses)} fork(s)")

    # Filter based on config + CLI options
    filtered = _filter_statuses(statuses, config, repos_filter)
    if len(filtered) != len(statuses):
        click.echo(f"After filtering: {len(filtered)} fork(s) to process")

    # Build status lookup
    status_map = {s.repo_name: s for s in statuses}

    # Run sync engine
    engine = SyncEngine(
        rest_client=rest_client,
        conflict_reporter=conflict_reporter,
        history=history,
        dry_run=dry_run,
        username=username,
    )

    click.echo("Running sync engine...")
    results: List[SyncResult] = await engine.sync_all(filtered)

    duration = time.monotonic() - start_time

    # Print quick summary
    synced = sum(1 for r in results if r.action_taken == "synced")
    dry_synced = sum(1 for r in results if r.action_taken == "dry_run")
    skipped = sum(1 for r in results if r.action_taken == "skipped")
    conflicts = sum(1 for r in results if r.action_taken == "conflict_reported")
    errors = sum(1 for r in results if r.action_taken == "error")

    click.echo("")
    click.echo("=== Sync Summary ===")
    if dry_run:
        click.echo(f"  Would sync:      {dry_synced}")
    else:
        click.echo(f"  Synced:          {synced}")
    click.echo(f"  Skipped:         {skipped}")
    click.echo(f"  Conflicts:       {conflicts}")
    if errors:
        click.echo(f"  Errors:          {errors}")
    click.echo(f"  API calls used:  {rate_tracker.calls_made}")
    click.echo(f"  Duration:        {duration:.1f}s")

    # Generate report
    generator = ReportGenerator()
    report_content = generator.generate(
        results=results,
        statuses=status_map,
        username=username,
        duration=duration,
        api_calls=rate_tracker.calls_made,
    )

    if not dry_run:
        generator.write(report_content, "SYNC_REPORT.md")
        click.echo("SYNC_REPORT.md updated")

        # Record in history
        mode = "scheduled" if not os.environ.get("GITHUB_ACTIONS") else "scheduled"
        history.record_run(
            mode=mode,
            results=results,
            duration=duration,
            api_calls=rate_tracker.calls_made,
        )
    else:
        click.echo("\n--- DRY RUN REPORT PREVIEW ---")
        click.echo(report_content[:2000])
        if len(report_content) > 2000:
            click.echo("... (truncated)")

        # Still record dry run in history
        history.record_run(
            mode="dry_run",
            results=results,
            duration=duration,
            api_calls=rate_tracker.calls_made,
        )

    # Notifications
    slack_url = os.environ.get("SLACK_WEBHOOK_URL", config.notifications.slack.webhook_url)
    discord_url = os.environ.get("DISCORD_WEBHOOK_URL", config.notifications.discord.webhook_url)

    notifier = NotificationClient(
        slack_webhook_url=slack_url or None,
        discord_webhook_url=discord_url or None,
        slack_enabled=config.notifications.slack.enabled,
        discord_enabled=config.notifications.discord.enabled,
    )

    conflict_results = [r for r in results if r.action_taken == "conflict_reported"]
    if conflict_results:
        await notifier.send_conflict_notification(conflict_results)

    report_url = ""
    if not dry_run:
        await notifier.send_summary(results, report_url=report_url)


async def _run_status(token: str, username: str) -> None:
    """Show status of all forks without making changes."""
    rate_tracker = RateLimitTracker()
    rest_client = GitHubRestClient(token=token, rate_tracker=rate_tracker)
    graphql_client = GitHubGraphQLClient(token=token, rate_tracker=rate_tracker)

    click.echo(f"Fetching fork statuses for {username}...")
    statuses = await graphql_client.get_all_fork_statuses(
        username=username, rest_client=rest_client
    )

    # Print table
    click.echo(f"\n{'Repo':<40} {'State':<15} {'Behind':>8} {'Ahead':>8}")
    click.echo("-" * 75)

    state_counts = {s.value: 0 for s in SyncState}
    for s in sorted(statuses, key=lambda x: x.state.value):
        state_counts[s.state.value] += 1
        click.echo(
            f"{s.repo_name:<40} {s.state.value:<15} {s.behind_by:>8} {s.ahead_by:>8}"
        )

    click.echo("")
    click.echo("=== Summary ===")
    for state_val, count in sorted(state_counts.items()):
        if count:
            click.echo(f"  {state_val:<20}: {count}")
    click.echo(f"\nTotal: {len(statuses)} forks | API calls: {rate_tracker.calls_made}")


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """forksync — Automated GitHub Fork Sync Tool"""


@cli.command()
@click.option("--dry-run", is_flag=True, default=False, help="Show what would sync, make no changes")
@click.option("--repos", multiple=True, help="Sync specific repos only (can repeat)")
@click.option("--config", "config_path", default="fork-sync.yml", help="Path to config file")
def run(dry_run: bool, repos: tuple, config_path: str) -> None:
    """Run fork sync (or dry-run with --dry-run)."""
    # Also honour DRY_RUN env var set by GitHub Actions
    env_dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    effective_dry_run = dry_run or env_dry_run

    token = _require_env("GITHUB_TOKEN")
    username = _require_env("GH_USERNAME")

    asyncio.run(
        _run_sync(
            token=token,
            username=username,
            dry_run=effective_dry_run,
            repos_filter=repos or None,
            config_path=config_path,
        )
    )


@cli.command()
def status() -> None:
    """Show current fork sync status (no changes made)."""
    token = _require_env("GITHUB_TOKEN")
    username = _require_env("GH_USERNAME")
    asyncio.run(_run_status(token=token, username=username))


@cli.command()
@click.option("--limit", default=10, help="Number of recent runs to show")
@click.option("--run-id", "run_id", default=None, type=int, help="Show events for a specific run")
def history(limit: int, run_id: Optional[int]) -> None:
    """Show sync history from SQLite database."""
    db = SyncHistory()

    if run_id is not None:
        events = db.get_run_events(run_id)
        if not events:
            click.echo(f"No events found for run #{run_id}")
            return
        click.echo(f"\n=== Events for run #{run_id} ===")
        click.echo(f"{'Repo':<40} {'Action':<20} {'Commits':>8} {'Duration':>10}")
        click.echo("-" * 82)
        for e in events:
            click.echo(
                f"{e['repo_name']:<40} {e['action']:<20} "
                f"{e['commits_merged']:>8} {e['duration_s']:>9.2f}s"
            )
            if e.get("error"):
                click.echo(f"  Error: {e['error']}")
            if e.get("issue_url"):
                click.echo(f"  Issue: {e['issue_url']}")
    else:
        runs = db.get_recent_runs(limit=limit)
        if not runs:
            click.echo("No sync history found.")
            return
        click.echo(f"\n=== Recent {limit} Sync Run(s) ===")
        click.echo(
            f"{'#':>6}  {'Date':<25} {'Mode':<12} {'Total':>6} {'Synced':>7} "
            f"{'Conflicts':>10} {'Status':<10}"
        )
        click.echo("-" * 80)
        for r in runs:
            click.echo(
                f"{r['id']:>6}  {r['run_at']:<25} {r['mode']:<12} {r['total']:>6} "
                f"{r['synced']:>7} {r['conflicts']:>10} {r['status']:<10}"
            )


@cli.command("config")
@click.option("--path", "config_path", default="fork-sync.yml", help="Path to config file")
def show_config(config_path: str) -> None:
    """Show loaded configuration."""
    config = load_config(config_path)
    click.echo("=== forksync Configuration ===")
    click.echo(f"  default_sync:      {config.default_sync}")
    click.echo(f"  strategy:          {config.strategy}")
    click.echo(f"  conflict_issues:   {config.conflict_issues}")
    click.echo(f"  skip_patterns:     {config.skip_patterns}")
    click.echo(f"  always_sync:       {config.always_sync}")
    click.echo(f"  slack enabled:     {config.notifications.slack.enabled}")
    click.echo(f"  discord enabled:   {config.notifications.discord.enabled}")


def main() -> None:
    """Entry point for python -m forksync."""
    cli()


if __name__ == "__main__":
    main()
