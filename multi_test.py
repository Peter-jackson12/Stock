from multiprocessing import Pool
import pandas as pd
import os


def run_backtest(part):
    import engine
    SPLIT = 3
    test_date = "20241009"  # 문자열 형태로 넘김
    test_code = None

    print(f"[PART {part}] 시작 (기준일: {test_date} 이후)")
    backtest = engine.BackTest(part, SPLIT, test_date, test_code)
    backtest.Data_load()
    print(f"[PART {part}] 완료")


if __name__ == '__main__':
    SPLIT = 3
    result_dir = r"C:\\Users\\user\\PycharmProjects\\CrossTopen\\fowardtest"

    with Pool(processes=SPLIT) as pool:
        pool.map(run_backtest, list(range(1, SPLIT + 1)))

    # 자동 병합
    file_list = [f for f in os.listdir(result_dir) if f.startswith("cross_Today_") and f.endswith(".csv")]
    df_list = [pd.read_csv(os.path.join(result_dir, f), encoding='euc-kr') for f in file_list]
    result = pd.concat(df_list, ignore_index=True)
    result.to_csv(os.path.join(result_dir, "1.csv"), encoding='euc-kr', index=False)

    print("✅ 2024/10/10 이후 테스트 완료 및 자동 병합 → 1.csv")
