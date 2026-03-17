"""
Configuration loader for forksync.

Loads settings from fork-sync.yml and environment variables.
Environment variables take precedence over config file values.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


@dataclass
class Config:
    github_username: str
    github_token: str
    fork_owner: str = ""
    concurrency_compare: int = 50
    concurrency_sync: int = 20
    verify_after_sync: bool = True
    nightly_threshold_days: int = 30
    weekly_threshold_days: int = 365
    gcp_project_id: str = ""
    firestore_collection: str = "forks"
    redis_host: str = "localhost"
    redis_port: int = 6379
    slack_webhook: str = ""
    discord_webhook: str = ""
    dry_run: bool = False
    api_key: str = ""

    def __post_init__(self) -> None:
        if not self.fork_owner:
            self.fork_owner = self.github_username


def load_config(config_path: Optional[str] = None) -> Config:
    """
    Load configuration from fork-sync.yml and environment variables.
    Environment variables always override config file values.

    Search order for config file:
    1. Explicit path argument
    2. ./fork-sync.yml (current working directory)
    3. <package parent>/fork-sync.yml
    4. Config file not found → use defaults
    """
    file_cfg: dict = {}

    search_paths = []
    if config_path:
        search_paths.append(Path(config_path))
    search_paths.append(Path("fork-sync.yml"))
    search_paths.append(Path(__file__).parent.parent / "fork-sync.yml")

    if _YAML_AVAILABLE:
        for path in search_paths:
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        file_cfg = yaml.safe_load(f) or {}
                    break
                except Exception:
                    pass

    # Extract nested config sections
    sync_cfg = file_cfg.get("sync", {}) or {}
    schedule_cfg = file_cfg.get("schedule", {}) or {}
    gcp_cfg = file_cfg.get("gcp", {}) or {}
    notif_cfg = file_cfg.get("notifications", {}) or {}

    # Resolve github credentials from environment
    github_token = (
        os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    )
    github_username = os.environ.get("GH_USERNAME", "")

    # fork_owner: env > config > github_username
    fork_owner = str(sync_cfg.get("fork_owner", "") or "") or github_username

    # dry_run: env > default False
    dry_run_env = os.environ.get("DRY_RUN", "").lower()
    dry_run = dry_run_env in ("1", "true", "yes")

    # gcp_project_id: env > config
    gcp_project_id = os.environ.get("GCP_PROJECT_ID", "") or str(gcp_cfg.get("project_id", "") or "")

    # redis: env > config
    redis_host = os.environ.get("REDIS_HOST", "") or str(gcp_cfg.get("redis_host", "localhost") or "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", 0) or gcp_cfg.get("redis_port", 6379) or 6379)

    # firestore collection: env > config
    firestore_collection = (
        os.environ.get("FIRESTORE_COLLECTION", "")
        or str(gcp_cfg.get("firestore_collection", "forks") or "forks")
    )

    # webhooks: env > config
    slack_webhook = os.environ.get("SLACK_WEBHOOK", "") or str(notif_cfg.get("slack_webhook", "") or "")
    discord_webhook = os.environ.get("DISCORD_WEBHOOK", "") or str(notif_cfg.get("discord_webhook", "") or "")

    api_key = os.environ.get("FORKSYNC_API_KEY", "")

    return Config(
        github_username=github_username,
        github_token=github_token,
        fork_owner=fork_owner,
        concurrency_compare=int(sync_cfg.get("concurrency_compare", 50)),
        concurrency_sync=int(sync_cfg.get("concurrency_sync", 20)),
        verify_after_sync=bool(sync_cfg.get("verify_after_sync", True)),
        nightly_threshold_days=int(schedule_cfg.get("nightly_threshold_days", 30)),
        weekly_threshold_days=int(schedule_cfg.get("weekly_threshold_days", 365)),
        gcp_project_id=gcp_project_id,
        firestore_collection=firestore_collection,
        redis_host=redis_host,
        redis_port=redis_port,
        slack_webhook=slack_webhook,
        discord_webhook=discord_webhook,
        dry_run=dry_run,
        api_key=api_key,
    )
