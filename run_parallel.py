import os
from multiprocessing import Pool
import pandas as pd
from config import DEFAULT_SPLIT, RESULT_DIR, ENCODING, STRATEGY_NAME
from engine import BackTestEngine

def run_worker(task):
    part, split = task
    engine = BackTestEngine(part=part, split=split)
    engine.run()

if __name__ == '__main__':
    print(f"⚙️ {DEFAULT_SPLIT}개 멀티프로세스로 백테스트 분할 가동 시작...")
    tasks = [(part, DEFAULT_SPLIT) for part in range(1, DEFAULT_SPLIT + 1)]
    
    with Pool(processes=DEFAULT_SPLIT) as pool:
        pool.map(run_worker, tasks)

    # 자동 병합 로직
    file_list = [f for f in os.listdir(RESULT_DIR) if f.startswith(STRATEGY_NAME) and f.endswith(".csv")]
    if file_list:
        df_list = [pd.read_csv(RESULT_DIR / f, encoding=ENCODING) for f in file_list]
        merged_df = pd.concat(df_list, ignore_index=True)
        
        final_csv = RESULT_DIR / "final_total_result.csv"
        merged_df.to_csv(final_csv, encoding=ENCODING, index=False)
        print(f"🎉 모든 멀티프로세스 완료 및 병합 완료: {final_csv}")