import os
from pathlib import Path

# ==========================================
# 1. 프로젝트 기본 경로 설정 (상대 경로 자동 계산)
# ==========================================
# config.py가 위치한 폴더를 기준(Stock/)으로 절대 경로 계산
BASE_DIR = Path(__file__).resolve().parent

# 데이터 경로 (sampledata/Daily, sampledata/temp)
DATA_DIR = BASE_DIR / "sampledata"
CSV_PATH = DATA_DIR / "Daily"
SEC_PATH = DATA_DIR / "temp"

# 결과 저장 경로 (results/)
RESULT_DIR = BASE_DIR / "results"

# 결과 폴더가 없으면 자동으로 생성
RESULT_DIR.mkdir(parents=True, exist_ok=True)


# ==========================================
# 2. 백테스팅 전략 매개변수 (전략 파라미터)
# ==========================================
STRATEGY_NAME = "cross_Today"
SET_TIME = 1300  # 백테스팅 제한 시간 조건


# ==========================================
# 3. 인코딩 및 멀티프로세스 설정
# ==========================================
# 한글 깨짐 방지를 위한 글로벌 표준 인코딩
ENCODING = "utf-8-sig"

# 멀티프로세싱 기본 분할 수 (CPU 코어 수에 맞게 제한)
DEFAULT_SPLIT = min(12, os.cpu_count() or 4)