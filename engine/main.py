# main.py
import argparse
import sys
from pathlib import Path

# UTF-8 콘솔 인코딩 설정 (Windows 이모지 및 한글 출력 호환)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 프로젝트 루트(Stock/)를 sys.path 최상단에 추가하여 모듈 임포트 섀도잉 방지
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.universe import MISSING_THRESHOLD_PCT
from engine.engine import FEATURE_SOURCE_INLINE, FEATURE_SOURCE_STORE, BackTestEngine


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="단일 프로세스 백테스트 실행")
    parser.add_argument(
        "--codes", nargs="*", default=None,
        help="대상 종목코드 목록 (기본: 전체 종목, 또는 TARGET_CODES 환경변수)",
    )
    parser.add_argument(
        "--dates", nargs="*", default=None,
        help="대상 날짜 목록 YYYYMMDD (기본: 일봉 매트릭스의 전체 날짜)",
    )
    parser.add_argument(
        "--feature-source", choices=[FEATURE_SOURCE_STORE, FEATURE_SOURCE_INLINE],
        default=FEATURE_SOURCE_STORE,
        help="피처 출처. store=fs_v1 parquet 조회(기본), inline=루프 계산(대조용)",
    )
    parser.add_argument(
        "--universe", default=None,
        help="universe.yaml 의 선언 이름 (기본: 파일의 default)",
    )
    parser.add_argument(
        "--missing-threshold", type=float, default=MISSING_THRESHOLD_PCT,
        help=f"선언 대비 미처리 비율 경고 임계치 %% (기본 {MISSING_THRESHOLD_PCT:.0f})",
    )
    parser.add_argument(
        "--strict-universe", action="store_true",
        help="미처리 비율이 임계를 넘으면 경고가 아니라 실패로 처리한다",
    )
    parser.add_argument(
        "--require-actual-prices", action="store_true",
        help="일봉의 실제가 출처 선언이 없거나 수정주가이면 실행을 중단한다",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    print("🚀 단일 프로세스 백테스팅 가동 시작...")

    # split=1, part=1로 단 1번만 전체 백테스팅 수행
    engine = BackTestEngine(
        part=1, split=1, codes=args.codes, dates=args.dates,
        feature_source=args.feature_source,
        universe=args.universe,
        missing_threshold_pct=args.missing_threshold,
        strict_universe=args.strict_universe,
        require_actual_prices=args.require_actual_prices,
    )
    engine.run()

    print("🎉 백테스팅 완료! results/ 폴더를 확인하세요.")

