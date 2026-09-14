"""대시보드 조회 계층 스모크 — 런 스토어에서 거래가 읽히는지 확인한다.

§7.5 이후 load_results() 는 CSV 파일이 아니라 런을 읽는다. 인자를 주지 않으면
가장 최근 런 하나를 연다.

    uv run python test_dash.py
"""

import sys

# 콘솔 인코딩 설정
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dashboard.data_service import list_runs, load_results, run_label

try:
    runs = list_runs()
    print(f"✅ 저장된 런: {len(runs)}개")
    for r in runs[:5]:
        print(f"   - {run_label(r)}")

    df = load_results()
    print(f"✅ load_results() 성공: 총 {len(df)}건 데이터 로드됨")
    if not df.empty:
        cols = ["date", "code", "name", "exit_rule", "entry_time_text",
                "exit_time_text", "net_pnl_pct", "exit_reason"]
        print(df[[c for c in cols if c in df.columns]].to_string())
    else:
        print("   (가장 최근 런에는 거래가 없습니다. 다른 런을 지정해 보세요.)")
except Exception as e:
    print(f"❌ load_results() 에러 발생: {e}")
