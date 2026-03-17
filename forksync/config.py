"""
Configuration loader for forksync.

Loads fork-sync.yml if present, falls back to sensible defaults.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


@dataclass
class NotificationConfig:
    enabled: bool = False
    webhook_url: str = ""
    on: List[str] = field(default_factory=lambda: ["conflicts", "summary"])


@dataclass
class NotificationsConfig:
    slack: NotificationConfig = field(default_factory=NotificationConfig)
    discord: NotificationConfig = field(default_factory=NotificationConfig)


@dataclass
class Config:
    default_sync: bool = True
    strategy: str = "fast_forward_only"
    conflict_issues: bool = True
    skip_patterns: List[str] = field(default_factory=list)
    always_sync: List[str] = field(default_factory=list)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)


def load_config(path: str = "fork-sync.yml") -> Config:
    """
    Load configuration from fork-sync.yml.
    Falls back to defaults if file is missing or malformed.
    """
    config_path = Path(path)

    if not config_path.exists():
        logger.info("No fork-sync.yml found, using defaults.")
        return Config()

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        sync_section = raw.get("sync", {})
        notif_section = raw.get("notifications", {})

        slack_raw = notif_section.get("slack", {})
        discord_raw = notif_section.get("discord", {})

        slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", slack_raw.get("webhook_url", ""))
        discord_webhook = os.environ.get("DISCORD_WEBHOOK_URL", discord_raw.get("webhook_url", ""))

        notifications = NotificationsConfig(
            slack=NotificationConfig(
                enabled=bool(slack_raw.get("enabled", False)),
                webhook_url=slack_webhook,
                on=slack_raw.get("on", ["conflicts", "summary"]),
            ),
            discord=NotificationConfig(
                enabled=bool(discord_raw.get("enabled", False)),
                webhook_url=discord_webhook,
                on=discord_raw.get("on", ["conflicts", "summary"]),
            ),
        )

        config = Config(
            default_sync=bool(sync_section.get("default", True)),
            strategy=sync_section.get("strategy", "fast_forward_only"),
            conflict_issues=bool(sync_section.get("conflict_issues", True)),
            skip_patterns=list(sync_section.get("skip", [])),
            always_sync=list(sync_section.get("always_sync", [])),
            notifications=notifications,
        )

        logger.info("Loaded config from %s", config_path)
        return config

    except Exception as exc:
        logger.warning("Failed to load config from %s: %s — using defaults", path, exc)
        return Config()
