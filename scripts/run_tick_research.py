"""Offline CLI for a closed raw-v2 prototype file; never discovers today's DB.

This scans the whole input for integrity, even when selecting one instrument.
Run large datasets only after capture hours. Fees are a per-side simulation rate.
"""
import argparse
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.raw_v2 import read_raw_v2
from engine.tick_research_run import ResearchRunFailed, run_raw_v2


def nonnegative(text):
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("finite nonnegative number required") from exc
    if not value.is_finite() or value < 0:
        raise argparse.ArgumentTypeError("finite nonnegative number required")
    return value


def nanoseconds(text):
    scaled = nonnegative(text) * 1_000_000_000
    if scaled != scaled.to_integral_value():
        raise argparse.ArgumentTypeError("duration must be representable in whole nanoseconds")
    return int(scaled)


def positive_int(text):
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("positive integer required") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("positive integer required")
    return value


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    p.add_argument("--code", required=True)
    p.add_argument("--venue", required=True, help="explicit venue or unknown")
    p.add_argument("--quantity", type=positive_int, required=True)
    p.add_argument("--cash", type=nonnegative, required=True)
    p.add_argument("--fee-rate", type=nonnegative, required=True, help="per-side fraction, e.g. 0.001")
    p.add_argument("--buy-latency-sec", type=nanoseconds, required=True)
    p.add_argument("--sell-latency-sec", type=nanoseconds, required=True)
    p.add_argument("--cancel-latency-sec", type=nanoseconds, required=True)
    p.add_argument("--max-quote-age-sec", type=nanoseconds, required=True)
    p.add_argument("--cooldown-sec", type=nanoseconds, default=10_000_000_000)
    p.add_argument("--exit-rule", choices=("fixed", "tick_trail", "step_trail"), default="fixed")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.fee_rate >= 1:
        p.error("--fee-rate must be less than 1")
    if not args.code.strip() or not args.venue.strip():
        p.error("nonempty code and venue required")
    try:
        # Only metadata here; run_raw_v2 performs the complete checksum validation.
        with read_raw_v2(args.db) as (manifest, _):
            source, session_id = manifest["source"], manifest["session_id"]
        config = dict(source=source, session_id=session_id, code=args.code, venue=args.venue,
                      cash=args.cash, fee_rate=args.fee_rate,
                      buy_latency_ns=args.buy_latency_sec, sell_latency_ns=args.sell_latency_sec,
                      cancel_latency_ns=args.cancel_latency_sec, max_quote_age_ns=args.max_quote_age_sec)
        result = run_raw_v2(args.db, output_root=args.output_root, simulator_config=config,
                            quantity=args.quantity, exit_rule=args.exit_rule, cooldown_ns=args.cooldown_sec)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, ResearchRunFailed) as exc:
        print(f"Research run failed: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
