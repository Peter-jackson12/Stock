"""Manually/UI-started bounded lightweight worker. No scheduling or trading."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from control_tower.service import run_one_inspection


def main():
    for _ in range(10):
        if run_one_inspection(ROOT) is None:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
