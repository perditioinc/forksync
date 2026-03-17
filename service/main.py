"""
forksync FastAPI service.

Exposes HTTP endpoints to trigger syncs, check status, and list forks.
Intended to run on Cloud Run or any container runtime.
"""

import asyncio
import json
import logging
import sys
import time
from typing import AsyncGenerator, Optional

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


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"ok": True}
