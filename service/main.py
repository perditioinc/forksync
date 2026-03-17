"""
forksync FastAPI service.

Exposes HTTP endpoints to trigger syncs, check status, and list forks.
Intended to run on Cloud Run or any container runtime.
"""

import logging
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from forksync import run_sync
from forksync.config import load_config
from forksync.storage.firestore import FirestoreClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
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


@app.post("/run")
async def trigger_sync(req: RunRequest, background_tasks: BackgroundTasks):
    """Trigger a fork sync run in the background."""
    logger.info("Sync triggered via /run (dry_run=%s)", req.dry_run)
    background_tasks.add_task(run_sync, config, req.dry_run)
    return {"status": "started", "dry_run": req.dry_run}


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
