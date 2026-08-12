import sqlite3
import pandas as pd
from pathlib import Path
from config import CSV_PATH, SEC_PATH, ENCODING

class DataLoader:
    """일봉 CSV 데이터 및 초봉 SQLite DB 파일 로드를 전담하는 클래스"""

    def __init__(self):
        self.csv_path = CSV_PATH
        self.sec_path = SEC_PATH

    def load_daily_csvs(self) -> dict[str, pd.DataFrame]:
        """기본 일봉 데이터프레임들을 디셔너리로 로드"""
        files = ['mkt', 'open', 'high', 'low', 'close', 'tradamt', 'float', 'shares']
        daily_data = {}
        for f in files:
            file_file = self.csv_path / f"{f}.csv"
            if file_file.exists():
                daily_data[f] = pd.read_csv(file_file, encoding='CP949', low_memory=False)
        return daily_data

    def get_lob_db_connection(self, date_str: str) -> sqlite3.Connection | None:
        """해당 날짜의 LOB SQLite DB 연결 객체 반환"""
        db_file = self.sec_path / f"{date_str}_LOB.db"
        if db_file.exists():
            return sqlite3.connect(db_file)
        return None