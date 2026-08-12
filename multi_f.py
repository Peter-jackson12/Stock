from multiprocessing import Pool
import pandas as pd
import os

def run_backtest(part):
    import legacy_engine  # 반드시 함수 안에서 import (multiprocessing 이슈 회피)
    SPLIT = 12
    test_date = None
    test_code = None

    print(f"[PART {part}] 시작")
    backtest = legacy_engine.BackTest(part, SPLIT, test_date, test_code)
    backtest.Data_load()
    print(f"[PART {part}] 완료")


if __name__ == '__main__':
    SPLIT = 12
    result_dir = r"C:\Users\user\PycharmProjects\CrossYhigh\foward"  # 결과 저장 경로

    # 병렬 실행
    with Pool(processes=SPLIT) as pool:
        pool.map(run_backtest, list(range(1, SPLIT + 1)))

    # 병렬 작업 끝난 뒤 자동 병합
    file_list = [f for f in os.listdir(result_dir) if f.startswith("Cross_yhigh_") and f.endswith(".csv")]
    df_list = [pd.read_csv(os.path.join(result_dir, f), encoding='euc-kr') for f in file_list]
    result = pd.concat(df_list, ignore_index=True)

    result.to_csv(os.path.join(result_dir, "20251104.csv"), encoding='euc-kr', index=False)
    print("✅ 모든 백테스트 완료 및 자동 병합 완료 → 20251104.csv 저장됨")
