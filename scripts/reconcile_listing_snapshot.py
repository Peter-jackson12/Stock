"""Compare a research universe with explicit saved KRX finder responses.

Membership is an observation, not tradability, common-stock classification or PIT.
No universe declarations or original data are changed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.universe import load_universe, resolve_codes


def read_finder(path):
    path = Path(path)
    with path.open("rb") as stream:
        data = stream.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("finder response exceeds 2 MiB")
    rows = json.loads(data)["block1"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10_000:
        raise ValueError("bounded nonempty finder response required")
    codes = set()
    for row in rows:
        code = row.get("short_code")
        if not isinstance(code, str) or not re.fullmatch(r"[0-9A-Z]{6,12}", code):
            raise ValueError("unexpected finder code")
        codes.add(code)
    return codes, dict(path=str(path.resolve()), sha256=hashlib.sha256(data).hexdigest(), rows=len(rows),
                      non_six_character_codes=sum(len(code) != 6 for code in codes))


def reconcile(candidates, listed, delisted):
    candidates = set(candidates)
    return dict(listed_only=sorted(candidates & (listed - delisted)),
                both_lists=sorted(candidates & listed & delisted),
                delisted_only=sorted((candidates & delisted) - listed),
                absent_from_both=sorted(candidates - listed - delisted))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listed", type=Path, required=True)
    parser.add_argument("--delisted", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    listed, first = read_finder(args.listed)
    delisted, second = read_finder(args.delisted)
    candidates = resolve_codes(load_universe("kospi_kosdaq_common"))
    groups = reconcile(candidates, listed, delisted)
    report = dict(schema="listing_reconciliation_v1", checked_at_utc=datetime.now(timezone.utc).isoformat(),
        universe="kospi_kosdaq_common", candidates=len(candidates), sources=[first, second],
        source_role="explicit caller assignment; check acquisition provenance", groups=groups,
        counts={k: len(v) for k, v in groups.items()}, production_approved=False,
        classification="finder membership only; no tradability/common-stock/PIT certification")
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report["counts"], ensure_ascii=False))
