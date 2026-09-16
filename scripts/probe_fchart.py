"""Bounded fchart HTTP probe. Writes diagnostics only; never daily prices or metadata."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import random
import re
import statistics
import sys
import time
import uuid

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collector.daily_collector import parse_candles
from core.universe import load_universe, resolve_codes


def prepare(universe="kospi_kosdaq_common", limit=50, seed=0):
    if not 1 <= limit <= 50:
        raise ValueError("limit must be 1..50")
    spec = load_universe(universe)
    if spec.source not in ("rule", "explicit"):
        raise ValueError("probe supports rule/explicit declarations only")
    codes = resolve_codes(spec)
    eligible = [code for code in codes if re.fullmatch(r"[0-9]{6}", code)]
    if len(eligible) < limit:
        raise ValueError(f"requested {limit} codes but only {len(eligible)} are eligible")
    selected = random.Random(seed).sample(eligible, limit)
    return {"universe": spec.as_params(), "resolved_count": len(codes),
            "eligible_count": len(eligible), "seed": seed, "codes": selected}


def summarize(plan, rows, elapsed, stopped):
    by_code = {}
    for row in rows:
        by_code[row["code"]] = row
    successes = [r for r in by_code.values() if r["ok"]]
    latencies = [r["request_ms"] for r in rows if r["ok"]]
    ordered = sorted(latencies)
    return {
        "planned": len(plan["codes"]), "attempted_codes": len(by_code),
        "successful_codes": len(successes), "failed_codes": len(by_code) - len(successes),
        "unattempted_codes": len(plan["codes"]) - len(by_code),
        "http_attempts": len(rows), "failed_attempts": sum(not r["ok"] for r in rows),
        "first_attempt_failures": sum(r["attempt"] == 1 and not r["ok"] for r in rows),
        "failure_rate_attempted": (len(by_code) - len(successes)) / len(by_code) if by_code else None,
        "elapsed_sec": round(elapsed, 3), "stopped_reason": stopped,
        "successful_request_ms": {
            "median": statistics.median(ordered) if ordered else None,
            "p95": ordered[math.ceil(len(ordered) * .95) - 1] if ordered else None,
            "first10_median": statistics.median(latencies[:10]) if len(latencies) >= 20 else None,
            "last10_median": statistics.median(latencies[-10:]) if len(latencies) >= 20 else None,
        },
        "latest_dates": sorted({r["last_date"] for r in successes}),
        "full_universe_approved": False,
    }


def probe(plan, directory, *, environment, count=4000, delay=1.0, timeout=10.0,
          get=requests.get, clock=time.perf_counter, sleep=time.sleep):
    codes = plan["codes"]
    if (not 1 <= len(codes) <= 50 or len(set(codes)) != len(codes)
            or not all(re.fullmatch(r"[0-9]{6}", code) for code in codes)):
        raise ValueError("probe requires 1..50 unique six-digit codes")
    if not 1 <= count <= 4000 or not 0.1 <= delay <= 60 or not 1 <= timeout <= 30:
        raise ValueError("invalid bounded probe settings")
    if environment not in ("agent_shell", "ordinary_powershell"):
        raise ValueError("execution environment must be explicitly labelled")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    rows, failures, stopped = [], 0, None
    start = clock()
    with (directory / "attempts.jsonl").open("x", encoding="utf-8") as stream:
        def record(value):
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        settings = {"environment": environment, "count": count, "delay_sec": delay,
                    "timeout_sec": timeout, "max_attempts_per_code": 3,
                    "proxy_env_present": any(os.environ.get(k) for k in
                                              ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                               "http_proxy", "https_proxy", "all_proxy"))}
        record({"event": "started", "at": datetime.now(timezone.utc).isoformat(),
                "plan": plan, "settings": settings})
        try:
            for index, code in enumerate(codes):
                if index:
                    sleep(delay)
                for attempt in range(1, 4):
                    record({"event": "request_started", "code": code, "attempt": attempt,
                            "at": datetime.now(timezone.utc).isoformat()})
                    began = clock()
                    row = {"event": "attempt", "code": code, "attempt": attempt,
                           "status": None, "ok": False, "error": None,
                           "at": datetime.now(timezone.utc).isoformat()}
                    response = None
                    try:
                        response = get("https://fchart.stock.naver.com/sise.nhn",
                                       params={"symbol": code, "timeframe": "day", "count": count, "requestType": 0},
                                       headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
                        row["request_ms"] = round((clock() - began) * 1000, 3)
                        row["status"] = response.status_code
                        row["bytes"] = len(response.content)
                        response.raise_for_status()
                        _, frame, reason = parse_candles(code, response.text)
                        row["ok"] = reason is None
                        row["error"] = reason
                        row["row_count"] = len(frame)
                        row["last_date"] = str(frame.index.max()) if len(frame) else None
                    except KeyboardInterrupt:
                        row["error"] = "interrupted"
                        stopped = "interrupted"
                    except requests.RequestException as exc:
                        row["error"] = type(exc).__name__
                    except ValueError as exc:
                        row["error"] = f"parse: {type(exc).__name__}"
                    except Exception as exc:
                        # XML parse errors and unexpected parser failures are diagnostics.
                        row["error"] = f"parse_or_probe: {type(exc).__name__}"
                    finally:
                        if response is not None:
                            response.close()
                    row.setdefault("request_ms", round((clock() - began) * 1000, 3))
                    row["elapsed_ms"] = round((clock() - began) * 1000, 3)
                    rows.append(row)
                    record(row)
                    if stopped:
                        break
                    if row["status"] in (403, 429):
                        stopped = f"http_{row['status']}"
                        break
                    transient = row["status"] is None or row["status"] == 408 or row["status"] >= 500
                    if row["ok"] or not transient or attempt == 3:
                        break
                    sleep(0.5 * (2 ** (attempt - 1)))
                failures = 0 if row["ok"] else failures + 1
                if stopped or failures >= 3:
                    stopped = stopped or "three_consecutive_failed_codes"
                    break
        except KeyboardInterrupt:
            stopped = "interrupted"
        result = {"plan": plan, "settings": settings,
                  "summary": summarize(plan, rows, clock() - start, stopped)}
        record({"event": "finished", "summary": result["summary"]})
    (directory / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default="kospi_kosdaq_common")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--execute", action="store_true", help="omit to show plan without HTTP")
    parser.add_argument("--environment", choices=["agent_shell", "ordinary_powershell"])
    args = parser.parse_args()
    plan = prepare(args.universe, args.limit, args.seed)
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if args.environment is None:
        parser.error("--execute requires --environment")
    output = ROOT / "logs/fchart_probe" / (datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8])
    result = probe(plan, output, environment=args.environment)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"Report: {output / 'summary.json'}")
    return 0 if result["summary"]["successful_codes"] == len(plan["codes"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
