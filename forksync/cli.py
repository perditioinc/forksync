"""
forksync CLI — command-line interface for running and inspecting fork syncs.
"""

import asyncio
import logging

import click

from forksync.config import load_config
from forksync.engine import run_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@click.group()
def main() -> None:
    """forksync — keep GitHub forks in sync at scale."""
    pass


@main.command()
@click.option("--dry-run", is_flag=True, default=False, help="Simulate sync without making changes.")
@click.option("--config", "config_path", default=None, help="Path to fork-sync.yml.")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable debug logging.")
def run(dry_run: bool, config_path: str, verbose: bool) -> None:
    """Run the fork sync pipeline."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config(config_path)
    if dry_run:
        config.dry_run = True

    if not config.github_token:
        raise click.ClickException(
            "GitHub token not found. Set GH_TOKEN or GITHUB_TOKEN environment variable."
        )
    if not config.fork_owner:
        raise click.ClickException(
            "Fork owner not configured. Set GH_USERNAME or fork_owner in fork-sync.yml."
        )

    result = asyncio.run(run_sync(config, dry_run=dry_run))

    click.echo(f"\nSync complete:")
    click.echo(f"  Total forks : {result.total_forks}")
    click.echo(f"  Checked     : {result.checked}")
    click.echo(f"  Synced      : {result.synced}")
    click.echo(f"  Up to date  : {result.already_current}")
    click.echo(f"  Skipped     : {result.skipped_schedule} (schedule)")
    click.echo(f"  Ahead       : {result.skipped_ahead}")
    click.echo(f"  Archived    : {result.skipped_archived}")
    click.echo(f"  Errors      : {result.errors}")
    click.echo(f"  API calls   : {result.api_calls_used}")
    click.echo(f"  Duration    : {result.duration_seconds:.1f}s")
    if dry_run:
        click.echo("  [DRY RUN — no changes made]")


@main.command()
@click.option("--config", "config_path", default=None, help="Path to fork-sync.yml.")
def status(config_path: str) -> None:
    """Show the latest sync run status from Firestore."""

    async def _get_status() -> None:
        config = load_config(config_path)
        if not config.gcp_project_id:
            click.echo("No GCP project configured — Firestore unavailable.")
            return
        try:
            from forksync.storage.firestore import FirestoreClient
            db = FirestoreClient(config.gcp_project_id, config.firestore_collection)
            run_data = await db.get_latest_run()
            if run_data:
                click.echo("Latest sync run:")
                for key, value in sorted(run_data.items()):
                    click.echo(f"  {key}: {value}")
            else:
                click.echo("No sync runs found.")
        except Exception as exc:
            click.echo(f"Error: {exc}", err=True)

    asyncio.run(_get_status())


@main.command()
@click.option("--tier", default=None, help="Filter by schedule tier (nightly/weekly/monthly).")
@click.option("--status-filter", "status_filter", default=None, help="Filter by status.")
@click.option("--config", "config_path", default=None, help="Path to fork-sync.yml.")
def forks(tier: str, status_filter: str, config_path: str) -> None:
    """List fork documents from Firestore."""

    async def _list_forks() -> None:
        config = load_config(config_path)
        if not config.gcp_project_id:
            click.echo("No GCP project configured — Firestore unavailable.")
            return
        try:
            from forksync.storage.firestore import FirestoreClient
            db = FirestoreClient(config.gcp_project_id, config.firestore_collection)
            docs = await db.query_forks(tier=tier, status=status_filter)
            if not docs:
                click.echo("No forks found.")
                return
            for doc in docs:
                owner = doc.get("fork_owner", "?")
                repo = doc.get("fork_repo", "?")
                st = doc.get("status", "?")
                schedule = doc.get("schedule_tier", "?")
                behind = doc.get("behind_by", 0)
                click.echo(f"  {owner}/{repo}  status={st}  tier={schedule}  behind={behind}")
        except Exception as exc:
            click.echo(f"Error: {exc}", err=True)

    asyncio.run(_list_forks())
