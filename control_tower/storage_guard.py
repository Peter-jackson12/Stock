"""Cheap admission check; runtime write failures still require normal abort/drain."""
from pathlib import Path
import shutil

MIN_FREE_BYTES = 256 * 1024 * 1024


def require_disk_space(root, *, minimum=MIN_FREE_BYTES, disk_usage=shutil.disk_usage):
    if type(minimum) is not int or minimum < MIN_FREE_BYTES:
        raise ValueError("storage reserve must be at least 256 MiB")
    root = Path(root).resolve()
    while not root.exists():
        if root.parent == root:
            raise OSError("no existing storage volume for destination")
        root = root.parent
    free = disk_usage(root).free
    if free < minimum:
        raise OSError(f"insufficient free disk space: {free} bytes; {minimum} required")
    return free
