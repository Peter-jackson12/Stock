"""Fixed Windows task action. No login, collector launch or automatic retry."""
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from control_tower.replay_schedule import run_scheduled, validate_id

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("one job id required")
    job = sys.argv[1]
    validate_id(job)
    with (ROOT / "operations_state" / f"scheduled_replay_{job}.log").open("a", encoding="utf-8") as log:
        sys.stdout = sys.stderr = log
        try:
            run_scheduled(ROOT, job)
        except BaseException:
            traceback.print_exc()
            raise SystemExit(2)
