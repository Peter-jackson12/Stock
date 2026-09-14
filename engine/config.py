import os
from pathlib import Path

# ==========================================
# 1. 프로젝트 기본 경로 설정 (상대 경로 자동 계산)
# ==========================================
# config.py가 위치한 폴더를 기준(Stock/)으로 절대 경로 계산
# BASE_DIR을 프로젝트 루트(Stock/)로 지정
BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "sampledata"
CSV_PATH = DATA_DIR / "Daily"
SEC_PATH = DATA_DIR / "temp"
RESULT_DIR = BASE_DIR / "results"

# 결과 폴더가 없으면 자동으로 생성
RESULT_DIR.mkdir(parents=True, exist_ok=True)


# ==========================================
# 2. 백테스팅 전략 매개변수 (전략 파라미터)
# ==========================================
STRATEGY_NAME = "cross_Today"
SET_TIME = 1300  # 백테스팅 제한 시간 조건

# 전략 로직/계산식이 바뀌면 올린다. run_id 의 입력이므로, 올리지 않고 로직만
# 바꾸면 과거 런의 재현성이 조용히 깨진다. (ARCHITECTURE_V2.md §6.3)
STRATEGY_VERSION = "1.0.0"

# 세금 + 수수료 (%). 기존에 engine.py 의 손익 계산식에 0.23 으로 박혀 있던 값을
# 꺼낸 것이다. 숫자 자체는 그대로라 결과는 바뀌지 않는다.
FEE_PCT = 0.23

# 이 엔진의 청산 규칙 집합 식별자. risk_manager.check_exit_signals 가
# time_1 / upper / los / trail 네 사유를 한 묶음으로 판정한다.
# nxt_tick_engine 의 fixed / tick_trail / step_trail 과 나란히 비교되는 축이다.
EXIT_RULE_ID = "risk_manager"

# 아직 피처 스토어(L2)가 없다. Phase B 에서 "fs_v1" 로 바뀐다.
FEATURE_SET_VERSION = "none"


# ==========================================
# 3. 인코딩 및 멀티프로세스 설정
# ==========================================
# 한글 깨짐 방지를 위한 글로벌 표준 인코딩
ENCODING = "utf-8-sig"

# 멀티프로세싱 기본 분할 수 (CPU 코어 수에 맞게 제한)
DEFAULT_SPLIT = min(12, os.cpu_count() or 4)