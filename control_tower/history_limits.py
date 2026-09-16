"""Explicit finite budgets; exceeding them preserves existing evidence."""
import math

DEFAULT_RECORDS = 100_000
DEFAULT_BYTES = 128 * 1024 * 1024


class HistoryLimitError(RuntimeError):
    """Operator maintenance is required; never silently delete or skip history."""


def validate_limits(records, byte_count):
    if type(records) is not int or not 1 <= records <= 1_000_000:
        raise ValueError("history record limit must be 1..1000000")
    if type(byte_count) is not int or not 1 <= byte_count <= 1024 * 1024 * 1024:
        raise ValueError("history byte limit must be 1..1GiB")


def validate_timeout(timeout):
    if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("replay timeout must be in (0, 30] seconds")
