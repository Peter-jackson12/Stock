"""Archive/checkpoint control history only; optional >=30-day terminal rotation."""
import argparse
from datetime import datetime
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from control_tower.history_maintenance import archive_history

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["jobs", "managed_captures"])
    parser.add_argument("--prune-before", type=datetime.fromisoformat,
        help="explicit timezone-aware cutoff; terminal records only, keep at least 30 days")
    args = parser.parse_args()
    print(json.dumps(archive_history(ROOT, args.kind, prune_before=args.prune_before), ensure_ascii=False, indent=2))
