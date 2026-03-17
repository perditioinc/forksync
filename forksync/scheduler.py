from datetime import date, datetime, timezone
from typing import Optional


def get_tier(upstream_pushed_at: Optional[datetime]) -> str:
    """Determine the schedule tier based on how recently the upstream was pushed."""
    if upstream_pushed_at is None:
        return "nightly"
    age_days = (datetime.now(timezone.utc) - upstream_pushed_at).days
    if age_days <= 30:
        return "nightly"
    if age_days <= 365:
        return "weekly"
    return "monthly"


def is_due(tier: str, last_checked: Optional[date]) -> bool:
    """Return True if the fork is due for a check based on its tier and last check date."""
    if last_checked is None:
        return True
    days = (date.today() - last_checked).days
    return days >= {"nightly": 1, "weekly": 7, "monthly": 30}.get(tier, 1)
