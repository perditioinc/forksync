"""
GitHub API rate limit tracking.

Tracks calls made during a run so we can report usage
and avoid hitting the 5,000/hour limit.
"""

import logging
import threading

logger = logging.getLogger(__name__)

GITHUB_HOURLY_LIMIT = 5000


class RateLimitTracker:
    """
    Tracks GitHub API call counts for a single run.

    Thread-safe. Can be shared between GraphQL and REST clients.

    Properties:
        calls_made  — total API calls recorded so far
        remaining   — estimated calls remaining in current window
    """

    def __init__(self, initial_remaining: int = GITHUB_HOURLY_LIMIT):
        self._calls_made: int = 0
        self._initial_remaining: int = initial_remaining
        self._lock = threading.Lock()

    @property
    def calls_made(self) -> int:
        """Total number of API calls recorded."""
        with self._lock:
            return self._calls_made

    @property
    def remaining(self) -> int:
        """Estimated remaining API calls in current window."""
        with self._lock:
            return max(0, self._initial_remaining - self._calls_made)

    def record_call(self, count: int = 1) -> None:
        """Record one or more API calls."""
        with self._lock:
            self._calls_made += count
            total = self._calls_made
            remaining = max(0, self._initial_remaining - total)

        if remaining < 500:
            logger.warning(
                "Rate limit warning: %d calls made, ~%d remaining", total, remaining
            )
        else:
            logger.debug("Rate limit: %d calls made, ~%d remaining", total, remaining)

    def reset(self) -> None:
        """Reset the counter (useful for tests)."""
        with self._lock:
            self._calls_made = 0

    def __repr__(self) -> str:
        return (
            f"RateLimitTracker(calls_made={self.calls_made}, remaining={self.remaining})"
        )
