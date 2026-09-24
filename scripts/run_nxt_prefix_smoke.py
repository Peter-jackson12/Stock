"""Run NXT portfolio smoke replay from a qualified bounded raw-v2 prefix.

This command is for pipeline validation only. It does not certify strategy
performance, the whole raw session, or live-trading readiness.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.nxt_portfolio_research import NxtPortfolioRunFailed
from engine.nxt_prefix_smoke import run_nxt_prefix_smoke
from scripts.run_tick_research import nanoseconds, nonnegative, positive_int


def instrument(text):
    code, sep, venue = text.partition("=")
    if not sep or not code.strip() or not venue.strip():
        raise argparse.ArgumentTypeError("instrument must be CODE=VENUE")
    return code.strip(), venue.strip()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--prefix-report", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    p.add_argument("--instrument", action="append", type=instrument, required=True,
                   help="repeatable CODE=VENUE, e.g. 005930=unknown")
    p.add_argument("--quantity", type=positive_int, required=True)
    p.add_argument("--cash", type=nonnegative, required=True)
    p.add_argument("--fee-rate", type=nonnegative, required=True)
    p.add_argument("--buy-latency-sec", type=nanoseconds, required=True)
    p.add_argument("--sell-latency-sec", type=nanoseconds, required=True)
    p.add_argument("--cancel-latency-sec", type=nanoseconds, required=True)
    p.add_argument("--max-quote-age-sec", type=nanoseconds, required=True)
    p.add_argument("--cooldown-sec", type=nanoseconds, default=10_000_000_000)
    p.add_argument("--exit-rule", choices=("fixed", "tick_trail", "step_trail"), default="fixed")
    p.add_argument("--dataset-label")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.fee_rate >= 1:
        p.error("--fee-rate must be less than 1")
    instruments = {}
    for code, venue in args.instrument:
        if code in instruments and instruments[code] != venue:
            p.error("one venue per code is required")
        instruments[code] = venue
    config = dict(
        instruments=instruments,
        cash=args.cash,
        fee_rate=args.fee_rate,
        buy_latency_ns=args.buy_latency_sec,
        sell_latency_ns=args.sell_latency_sec,
        cancel_latency_ns=args.cancel_latency_sec,
        max_quote_age_ns=args.max_quote_age_sec,
    )
    try:
        result = run_nxt_prefix_smoke(
            args.db,
            args.prefix_report,
            output_root=args.output_root,
            simulator_config=config,
            quantity=args.quantity,
            exit_rule=args.exit_rule,
            cooldown_ns=args.cooldown_sec,
            dataset_label=args.dataset_label,
        )
    except (OSError, ValueError, TypeError, NxtPortfolioRunFailed) as exc:
        print(f"NXT prefix smoke failed: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
