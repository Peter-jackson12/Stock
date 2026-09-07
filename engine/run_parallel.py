import sys
from pathlib import Path

# UTF-8 콘솔 인코딩 설정 (Windows 이모지 및 한글 출력 호환)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from multiprocessing import Pool
import os
import pandas as pd
from engine.config import (
    DEFAULT_SPLIT,
    RESULT_DIR,
    ENCODING,
    STRATEGY_NAME,
)  # ⭐️ engine. 추가
from engine.engine import BackTestEngine




def run_worker(task):
    part, split = task
    engine = BackTestEngine(part=part, split=split)
    engine.run()

if __name__ == '__main__':
    print(f"⚙️ {DEFAULT_SPLIT}개 멀티프로세스로 백테스트 분할 가동 시작...")
    tasks = [(part, DEFAULT_SPLIT) for part in range(1, DEFAULT_SPLIT + 1)]
    
    with Pool(processes=DEFAULT_SPLIT) as pool:
        pool.map(run_worker, tasks)

    # 내용이 존재하는 CSV만 안전하게 병합
    file_list = [f for f in os.listdir(RESULT_DIR) if f.startswith(STRATEGY_NAME) and f.endswith(".csv")]
    df_list = []
    
    for f in file_list:
        file_path = RESULT_DIR / f
        if file_path.stat().st_size > 0: # 파일 크기가 0보다 큰 것만 병합
            try:
                df = pd.read_csv(file_path, encoding=ENCODING)
                if not df.empty:
                    df_list.append(df)
            except Exception:
                pass

    if df_list:
        merged_df = pd.concat(df_list, ignore_index=True)
        final_csv = RESULT_DIR / "final_total_result.csv"
        merged_df.to_csv(final_csv, encoding=ENCODING, index=False)
        print(f"🎉 모든 백테스팅 및 병합 완료! 최종 결과 파일: {final_csv} (총 {len(merged_df)}건 거래 기록)")
    else:
        print("⚠️ 백테스트 조건에 일치하는 매매 거래 기록(PnL)이 발생하지 않았습니다.")