"""64비트: core.universe로 키움 배치 계획 생성 / 수신 결과를 파일럿 스냅샷으로 변환."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from collector.kiwoom.meta_batch import validate_job
from collector.kiwoom.stock_meta import KST, normalize_opt10001, save_observation
from collector.kiwoom.run_meta_batch import STATE


def job_id(job):
    return hashlib.sha256(json.dumps(job, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]


def prepare(universe, server, multiplier, limit):
    from core.universe import load_universe, resolve_codes
    import pandas as pd
    spec = load_universe(universe)
    frame = pd.read_csv(ROOT / "sampledata/Daily/open.csv", encoding="cp949") if spec.source == "daily_matrix" else None
    day = datetime.now(KST).strftime("%Y%m%d")
    codes = resolve_codes(spec, daily_frame=frame, dates=[day])
    if not 1 <= limit <= 50:
        raise ValueError("pilot limit must be 1..50")
    return validate_job({"version": 1, "date": day, "server": server,
                         "shares_multiplier": multiplier, "codes": list(codes[:limit]),
                         "universe": spec.as_params(), "resolved_total": len(codes)})


def import_results(job, *, state=STATE, output=None):
    validate_job(job)
    # 서버별 파일럿 경로. 운영 Daily 승격은 이 명령이 자동으로 하지 않는다.
    output = Path(output) if output else ROOT / "sampledata/Daily_kiwoom_pilot" / job["server"]
    state = Path(state).resolve()
    if not state.is_file():
        raise FileNotFoundError(state)
    conn = sqlite3.connect(state.as_uri() + "?mode=ro", uri=True)
    try:
        records = conn.execute("SELECT code,observation FROM attempts WHERE job_id=? "
                               "AND status IN ('complete','missing') ORDER BY id", (job_id(job),)).fetchall()
    finally:
        conn.close()
    count = 0
    for code, payload in records:
        saved = json.loads(payload)
        at = datetime.fromisoformat(saved["received_at"])
        if saved["server"] != job["server"] or at.astimezone(KST).strftime("%Y%m%d") != job["date"]:
            raise ValueError("source server/date mismatch")
        observation = normalize_opt10001(saved["raw"], received_at=at,
                                        shares_multiplier=job["shares_multiplier"], mkt_to_eok=1)
        if observation["snapshot"]["code"] != code or code not in job["codes"]:
            raise ValueError("source code mismatch")
        observation["server"] = saved["server"]
        save_observation(output, observation)
        count += 1
    if count:
        from collector.daily_snapshot import rebuild_wide_csvs
        rebuild_wide_csvs(output, name_map={})
    return count, output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("prepare")
    plan.add_argument("--universe", default="blue_chips")
    plan.add_argument("--server", choices=["mock", "live"], required=True)
    plan.add_argument("--shares-multiplier", type=int, choices=[1, 1000], required=True)
    plan.add_argument("--limit", type=int, default=3)
    imp = sub.add_parser("import")
    imp.add_argument("--job", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        job = prepare(args.universe, args.server, args.shares_multiplier, args.limit)
        path = STATE.parent / "jobs" / f"{job['date']}_{job_id(job)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        print(path)
        print(f"선택 {len(job['codes'])}/{job['resolved_total']}종목, 서버={job['server']}, 4초 간격")
    else:
        count, output = import_results(json.loads(args.job.read_text(encoding="utf-8")))
        print(f"반영한 관측 {count}개: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
