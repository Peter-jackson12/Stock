# main.py
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

from engine.engine import BackTestEngine

if __name__ == "__main__":
    print("🚀 단일 프로세스 백테스팅 가동 시작...")
    
    # split=1, part=1로 단 1번만 전체 백테스팅 수행
    engine = BackTestEngine(part=1, split=1)
    engine.run()
    
    print("🎉 백테스팅 완료! results/ 폴더를 확인하세요.")

