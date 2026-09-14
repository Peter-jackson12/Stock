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

# 이 엔진이 읽는 피처셋 디렉토리 (sampledata/features/<version>/).
# run_id 의 입력이므로, 인라인 계산으로 돌린 런은 이 값 대신 "none" 을 기록한다
# (BackTestEngine.feature_set_version). 같은 결과라도 출처가 다르면 런이 달라야 한다.
FEATURE_SET_VERSION = "fs_v1"


# ==========================================
# 3. 대상 종목 유니버스
# ==========================================
# 특정 종목만 골라 백테스트하고 싶을 때 지정한다. 기본값(None)은 전체 종목이다.
#
# 과거에는 engine.py 소스 안에 다음과 같이 박혀 있었다 (ARCHITECTURE_V2.md §1.6).
#     if code != '000270': continue
# 릴리스 코드에 테스트용 필터가 살아 있으면, 그 사실을 잊는 순간 "왜 백테스트가
# 종목 1개만 도는가"를 코드를 뒤져서 찾아야 한다. 지정 경로를 셋으로 열어둔다.
#
#   1) 환경변수  : TARGET_CODES=000270,005930 (콤마 구분)
#   2) CLI 인자  : uv run python -m engine.main --codes 000270 005930
#   3) 코드      : BackTestEngine(codes=["000270"])
#
# 우선순위는 CLI/코드로 명시한 값이 항상 이 기본값을 덮어쓴다.
def _parse_target_codes() -> tuple[str, ...] | None:
    raw = os.environ.get("TARGET_CODES", "").strip()
    if not raw:
        return None
    codes = tuple(c.strip() for c in raw.split(",") if c.strip())
    return codes or None


TARGET_CODES: tuple[str, ...] | None = _parse_target_codes()


# ==========================================
# 4. 인코딩 및 멀티프로세스 설정
# ==========================================
# 한글 깨짐 방지를 위한 글로벌 표준 인코딩
ENCODING = "utf-8-sig"

# 멀티프로세싱 기본 분할 수 (CPU 코어 수에 맞게 제한)
DEFAULT_SPLIT = min(12, os.cpu_count() or 4)