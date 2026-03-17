"""
GitHub fork sync wrapper using the gh CLI.

Runs `gh repo sync` as a subprocess for each fork that is behind upstream.
"""

import asyncio
import logging
import os

from forksync.models import ForkDocument, SyncResult

logger = logging.getLogger(__name__)


async def sync_fork(fork: ForkDocument, token: str) -> SyncResult:
    """
    Run gh repo sync for a single fork.

    Uses the gh CLI which handles authentication and the sync protocol.
    Returns a SyncResult with stdout/stderr captured.
    """
    cmd = [
        "gh", "repo", "sync",
        f"{fork.fork_owner}/{fork.fork_repo}",
        "--source", f"{fork.upstream_owner}/{fork.upstream_repo}",
    ]
    env = {**os.environ, "GH_TOKEN": token}

    logger.debug(
        "Syncing %s/%s from %s/%s",
        fork.fork_owner, fork.fork_repo,
        fork.upstream_owner, fork.upstream_repo,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout = stdout_bytes.decode(errors="replace").strip()
        stderr = stderr_bytes.decode(errors="replace").strip()
        success = proc.returncode == 0

        if success:
            logger.info("Synced %s/%s successfully", fork.fork_owner, fork.fork_repo)
        else:
            logger.warning(
                "Sync failed for %s/%s (exit %d): %s",
                fork.fork_owner, fork.fork_repo, proc.returncode, stderr,
            )

        return SyncResult(
            fork=fork,
            success=success,
            stdout=stdout,
            stderr=stderr,
            error=None if success else f"exit {proc.returncode}: {stderr}",
        )

    except Exception as exc:
        logger.error("Sync subprocess error for %s/%s: %s", fork.fork_owner, fork.fork_repo, exc)
        return SyncResult(
            fork=fork,
            success=False,
            stdout="",
            stderr="",
            error=str(exc),
        )
