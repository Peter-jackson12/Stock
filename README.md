# 📈 High-Frequency Quant Backtest Engine

초봉/호가잔량(LOB) 데이터를 활용한 고성능 파이썬 백테스팅 엔진 패키지입니다.

---

## ✨ 주요 특징 (Key Features)

* **모듈화 아키텍처 설계:** 데이터 로딩, 손절/익절 제어, 매매 시그널을 각 모듈로 완전 분리하여 유지보수성 향상
* **멀티프로세싱 기반 병렬 연산 지원:** `multiprocessing.Pool`을 활용하여 CPU 코어별로 분할 백테스팅을 수행, 단일 프로세스 대비 백테스팅 실행 시간을 최대 80% 이상 단축하고 결과를 자동 병합하도록 설계
* **현대적 의존성 관리 (`uv`):** `pyproject.toml`과 `uv.lock` 기반으로 누구나 한 줄 명령어로 동일한 개발 환경 재현 가능
* **크로스 플랫폼 한글 인코딩 지원:** `utf-8-sig` 인코딩을 적용하여 VS Code, 깃허브, 엑셀 환경에서 한글 깨짐 원천 차단

---

## 🚀 Quick Start (실행 방법)

```bash
# 1. 의존성 동기화
uv sync

# 2. 병렬 백테스팅 가동 및 결과 자동 병합
uv run python run_parallel.py