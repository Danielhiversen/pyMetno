"""Instance cache records and conservative server deadline handling."""

import datetime
from email.utils import parsedate_to_datetime
import random

MIN_FETCH_INTERVAL = datetime.timedelta(hours=3)
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 3600
UTC = datetime.timezone.utc


class CacheEntry:
    """State belonging to one normalized endpoint and location."""

    def __init__(self):
        self.data = None
        self.last_modified = None
        self.next_fetch_at = None
        self.last_error = None
        self.failures = 0

    def succeeded(self, now, headers):
        """Apply freshness metadata after validation or revalidation."""
        self.next_fetch_at = max(
            now + MIN_FETCH_INTERVAL, http_date(headers.get("Expires")) or now
        )
        self.last_error = None
        self.failures = 0

    def failed(self, now, headers, error):
        """Schedule a later attempt without replacing a valid representation."""
        self.failures = min(self.failures + 1, 7)
        jitter_fraction = 0.1
        base = min(
            RETRY_MAX_SECONDS / (1 + jitter_fraction),
            RETRY_BASE_SECONDS * 2 ** (self.failures - 1),
        )
        delay = base + random.uniform(0.001, base * jitter_fraction)
        self.next_fetch_at = max(
            self.next_fetch_at or now,
            now + datetime.timedelta(seconds=delay),
            http_date(headers.get("Expires")) or now,
            retry_after(headers.get("Retry-After"), now) or now,
        )
        self.last_error = error


def http_date(value):
    """Parse an HTTP date, ignoring malformed or out-of-range headers."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def retry_after(value, now):
    """Support both delay-seconds and HTTP-date Retry-After values."""
    if not value:
        return None
    if value.isascii() and value.isdecimal():
        try:
            return now + datetime.timedelta(seconds=int(value))
        except (ValueError, OverflowError):
            return None
    return http_date(value)
