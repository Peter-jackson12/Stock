"""Read one bounded research result JSON; never opens its raw dataset.

Exit 0: completed outcome; 2: running/failed; 3: unreadable/invalid/too large.
Counts are order/fill counts, not round-trip trade counts or profit metrics.
"""
import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys


STATUSES = {"running", "failed", "completed_empty_input", "completed_no_selected_events",
            "completed_no_fills", "completed_with_open_position", "completed_flat"}
MAX_BYTES = 8 * 1024 * 1024


def inspect(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("result exceeds 8 MiB lightweight inspection limit")
    result = json.loads(data)
    if not isinstance(result, dict) or result.get("schema") != "tick_research_result_v1":
        raise ValueError("unsupported result schema")
    status = result.get("status")
    if status not in STATUSES:
        raise ValueError("unknown status")
    if status.startswith("completed_"):
        # These fields are emitted by the v1 writer. A completion label alone
        # cannot override partial input, diagnostic-only output or an error.
        if result.get("input_complete") is not True:
            raise ValueError("completed status requires input_complete=true")
        if result.get("diagnostics_only") is not False:
            raise ValueError("completed status requires diagnostics_only=false")
        if "error" not in result or result["error"] is not None:
            raise ValueError("completed status requires error=null")
    summary = dict(status=status, diagnostics_only=status in ("failed", "running"))
    if status != "running":
        for name in ("event_count", "open_quantity"):
            value = result.get(name)
            if type(value) is not int or value < 0:
                raise ValueError(f"invalid {name}")
            summary[name] = value
        for name in ("orders", "fills", "signals"):
            if not isinstance(result.get(name), list):
                raise ValueError(f"invalid {name}")
            summary[name + "_count"] = len(result[name])
        cash = Decimal(str(result.get("final_cash")))
        if not cash.is_finite() or cash < 0:
            raise ValueError("invalid final_cash")
        summary["final_cash"] = str(cash)
        if status == "completed_flat" and summary["open_quantity"] != 0:
            raise ValueError("flat status conflicts with open quantity")
        if status == "completed_with_open_position" and summary["open_quantity"] <= 0:
            raise ValueError("open-position status conflicts with quantity")
        if status in ("completed_empty_input", "completed_no_selected_events") and summary["event_count"] != 0:
            raise ValueError("empty-selection status conflicts with event count")
        if status == "completed_no_fills" and summary["fills_count"] != 0:
            raise ValueError("no-fills status conflicts with fills")
        summary["quote_checks"] = result.get("quote_checks", {})
        summary["error"] = result.get("error")
    summary["note"] = "Cash excludes open holdings; no profit or round-trip trade count inferred."
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    args = parser.parse_args(argv)
    try:
        summary = inspect(args.result)
    except (OSError, ValueError, TypeError, InvalidOperation) as exc:
        print(f"Cannot inspect result: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if summary["diagnostics_only"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
