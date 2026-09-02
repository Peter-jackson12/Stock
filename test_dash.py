import sys
from pathlib import Path

# 콘솔 인코딩 설정
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dashboard.data_service import load_results

try:
    df = load_results()
    print(f"✅ load_results() 성공: 총 {len(df)}건 데이터 로드됨")
    print(df[["date", "name", "entry_time_text", "exit_time_text", "pnl", "msg"]].to_string())
except Exception as e:
    print(f"❌ load_results() 에러 발생: {e}")
