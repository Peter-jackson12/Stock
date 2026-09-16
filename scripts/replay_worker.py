"""Run one explicitly queued small offline job independently of the UI."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from control_tower.offline_worker import run_replay_job

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("one job id required")
    run_replay_job(ROOT, sys.argv[1])
