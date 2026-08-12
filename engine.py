import os
import sqlite3
import numpy as np
import pandas as pd
from config import STRATEGY_NAME, SET_TIME, RESULT_DIR, ENCODING
from utils import calculate_ticksize, calculate_upperlimit, calculate_time_spread
from data_loader import DataLoader
from risk_manager import check_exit_signals
from strategy import calculate_window_metrics, check_entry_conditions

class BackTestEngine:
    def __init__(self, part: int = 1, split: int = 12):
        self.part = part
        self.split = split
        self.strategy = STRATEGY_NAME
        self.loader = DataLoader()
        self.trading = {}

    def run(self):
        """백테스트 가동 및 결과 저장 메인 루프"""
        print(f"🚀 [Part {self.part}/{self.split}] 백테스트 엔진 가동 시작")
        # 백테스트 메인 로직 가동...
        
        # 결과 CSV 저장
        result_df = pd.DataFrame(self.trading)
        output_file = RESULT_DIR / f"{self.strategy}_{self.part}.csv"
        result_df.to_csv(output_file, encoding=ENCODING, index=False)
        print(f"✅ 백테스팅 완료 결과 저장됨: {output_file}")