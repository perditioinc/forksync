"""
forksync FastAPI service.

Exposes HTTP endpoints to trigger syncs, check status, and list forks.
Intended to run on Cloud Run or any container runtime.
"""

import asyncio
import base64
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from forksync import run_sync
from forksync.config import load_config
from forksync.storage.firestore import FirestoreClient

# Configure root logger explicitly so it works regardless of uvicorn's setup.
# force=True overrides any existing handler configuration.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
# Ensure the forksync namespace is always at INFO — uvicorn may set third-party
# loggers to WARNING by default.
logging.getLogger("forksync").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

app = FastAPI(title="forksync", version="2.0.0")
config = load_config()

_UNPROTECTED = {"/health"}


@app.middleware("http")
async def require_bearer_token(request: Request, call_next):
    """Require a valid Bearer token on all routes except /health."""
    if request.url.path in _UNPROTECTED:
        return await call_next(request)

    if not config.api_key:
        # No API key configured — allow all requests (dev mode)
        return await call_next(request)

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[len("Bearer "):] != config.api_key:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    return await call_next(request)


class RunRequest(BaseModel):
    dry_run: bool = False


async def _stream_sync(dry_run: bool) -> AsyncGenerator[str, None]:
    """
    Run the sync engine and stream log output as newline-delimited JSON.

    Keeps the HTTP connection open for the full duration of the sync,
    which prevents Cloud Run from treating the instance as idle and
    shutting it down mid-run.

    Each line is a JSON object: {"time": ..., "level": ..., "logger": ..., "msg": ...}
    A final summary line is yielded when the run completes.
    """
    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()  # sentinel to signal the run has finished

    class _QueueHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            entry = {
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            queue.put_nowait(entry)

    handler = _QueueHandler()
    handler.setLevel(logging.INFO)
    # Attach to all loggers that produce sync output
    for name in ("forksync", __name__):
        logging.getLogger(name).addHandler(handler)

    final: dict = {}

    async def _run() -> None:
        try:
            run_result = await run_sync(config, dry_run)
            final["run"] = run_result
        except Exception as exc:
            final["error"] = str(exc)
            logger.exception("Sync run failed with unhandled exception")
        finally:
            await queue.put(_DONE)

    task = asyncio.create_task(_run())

    try:
        while True:
            entry = await queue.get()
            if entry is _DONE:
                break
            yield json.dumps(entry) + "\n"

        # Drain any log lines queued after the sentinel
        while not queue.empty():
            entry = queue.get_nowait()
            if entry is not _DONE:
                yield json.dumps(entry) + "\n"

        # Final summary line
        if "run" in final:
            r = final["run"]
            yield json.dumps({
                "level": "INFO",
                "logger": "forksync.engine",
                "msg": "sync complete",
                "synced": r.synced,
                "checked": r.checked,
                "errors": r.errors,
                "skipped_schedule": r.skipped_schedule,
                "duration_seconds": r.duration_seconds,
            }) + "\n"
            # Commit SYNC_REPORT.md to GitHub after each non-dry-run
            try:
                await _commit_sync_report(r)
            except Exception as exc:
                logger.warning("Failed to commit sync report: %s", exc)
        elif "error" in final:
            yield json.dumps({
                "level": "ERROR",
                "logger": __name__,
                "msg": f"sync failed: {final['error']}",
            }) + "\n"
    finally:
        for name in ("forksync", __name__):
            logging.getLogger(name).removeHandler(handler)
        if not task.done():
            task.cancel()


@app.post("/run")
async def trigger_sync(req: RunRequest):
    """Run fork sync and stream log output as newline-delimited JSON (NDJSON).

    Streams log lines for the full duration of the sync so Cloud Run keeps
    the instance alive. Each line is a JSON object. The final line contains
    the run summary with synced/checked/errors counts.
    """
    logger.info("Sync triggered via /run (dry_run=%s)", req.dry_run)
    return StreamingResponse(
        _stream_sync(req.dry_run),
        media_type="application/x-ndjson",
    )


@app.get("/status")
async def get_status():
    """Return the most recent sync run from Firestore."""
    if not config.gcp_project_id:
        return JSONResponse(
            {"message": "Firestore not configured (no gcp_project_id)"},
            status_code=503,
        )
    db = FirestoreClient(config.gcp_project_id, config.firestore_collection)
    result = await db.get_latest_run()
    if not result:
        return JSONResponse({"message": "No runs yet"}, status_code=404)
    return result


@app.get("/forks")
async def list_forks(
    tier: Optional[str] = None,
    status: Optional[str] = None,
):
    """List fork documents with optional filters."""
    if not config.gcp_project_id:
        return JSONResponse(
            {"message": "Firestore not configured (no gcp_project_id)"},
            status_code=503,
        )
    db = FirestoreClient(config.gcp_project_id, config.firestore_collection)
    return await db.query_forks(tier=tier, status=status)


def _generate_sync_report(run_result) -> str:
    """Generate SYNC_REPORT.md content from a run result."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d %H:%M UTC")
    date_short = now.strftime("%Y-%m-%d")
    duration = run_result.duration_seconds

    if duration >= 60:
        mins = int(duration // 60)
        secs = int(duration % 60)
        dur_str = f"{mins}m {secs}s"
    else:
        dur_str = f"{duration:.0f}s"

    lines = [
        "# Fork Sync Report",
        f"**perditioinc's GitHub Forks** \u00b7 {date_str} \u00b7 {dur_str}",
        "",
        "---",
        "",
        "## Summary",
        "| Status | Count |",
        "|--------|-------|",
        f"| Synced | {run_result.synced} |",
        f"| Checked | {run_result.checked} |",
        f"| Skipped (schedule) | {run_result.skipped_schedule} |",
        f"| Errors | {run_result.errors} |",
        "",
        "## Machine-readable fields",
        f"- date: {date_short}",
        f"- duration_seconds: {int(duration)}",
        f"- repos_checked: {run_result.checked}",
        f"- repos_synced: {run_result.synced}",
        f"- already_current: {run_result.checked - run_result.synced - run_result.errors}",
        f"- api_calls_used: {run_result.checked + run_result.synced}",
        f"- errors: {run_result.errors}",
        f"- skipped_schedule: {run_result.skipped_schedule}",
        "- peak_concurrency: 50",
        "- source: forksync v2 on Cloud Run",
        "",
        "---",
        "",
        "*Generated by [forksync v2](https://github.com/perditioinc/forksync) on Cloud Run*",
        "*Next run: tomorrow at 6am UTC*",
    ]
    return "\n".join(lines) + "\n"


async def _commit_sync_report(run_result) -> None:
    """Commit SYNC_REPORT.md to GitHub after each run via GitHub API."""
    token = os.getenv("GH_TOKEN", "")
    if not token:
        logger.warning("GH_TOKEN not set — skipping SYNC_REPORT.md commit")
        return

    content = _generate_sync_report(run_result)
    encoded = base64.b64encode(content.encode()).decode()
    repo = "perditioinc/forksync"
    path = "SYNC_REPORT.md"
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            # Get current file SHA
            r = await http.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers={"Authorization": f"Bearer {token}"},
            )
            sha = r.json().get("sha") if r.status_code == 200 else None

            payload = {
                "message": f"chore: sync report {date_str}",
                "content": encoded,
                "branch": "main",
                "committer": {
                    "name": "forksync[bot]",
                    "email": "forksync[bot]@users.noreply.github.com",
                },
            }
            if sha:
                payload["sha"] = sha

            r = await http.put(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            if r.status_code in (200, 201):
                logger.info("SYNC_REPORT.md committed to GitHub")
            else:
                logger.warning("Failed to commit SYNC_REPORT.md: %s %s", r.status_code, r.text[:200])
    except Exception as exc:
        logger.warning("Error committing SYNC_REPORT.md: %s", exc)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"ok": True}
