# 📌 주식 백테스팅 & 트레이딩 시스템 리팩토링 및 아키텍처 기록 (ADR)

## 1. 개요 및 목적
- **과거 문제**: 단일 스크립트(`legacy_engine.py`)에 데이터 수집, DB 로딩, 전략, 리스크 제어가 결합되어 하드코딩된 로컬 경로와 단일 프로세스 병목 발생.
- **개선 목표**:
  1. 단일 책임 원칙(SRP) 기반의 독립 모듈화 및 CPU 멀티프로세싱 병렬 가속.
  2. 재현 가능한 패키지 관리 (`uv` 기반 격리).
  3. 실시간 고빈도(HF) 데이터 수집 및 1초봉 LOB 자동 변환 파이프라인 구축.
  4. 웹 기반 대시보드(Streamlit) 및 로컬 LLM(Ollama) 연동을 통한 결과 해석 자동화.

---

## 2. 해결 완료된 문제 및 기술적 의사결정 (ADR)

### ✅ Issue 1. 레거시 스파게티 코드 모듈 분리
- **해결**: 단일 1,000줄 코드를 `engine/`, `config.py`, `data_loader.py`, `strategy.py`, `risk_manager.py`, `utils.py`로 분리.
- **효과**: 기능별 유닛 테스트 가능, 유지보수 용이성 및 전략 파라미터 튜닝 유연성 확보.

### ✅ Issue 2. 대용량 데이터 처리 속도 개선 (CPU 병렬화)
- **해결**: `multiprocessing.Pool` 기반의 `run_parallel.py`를 도입하여 날짜 구간을 분할(`DEFAULT_SPLIT`) 처리 후 자동 CSV 병합.
- **효과**: 백테스트 수행 속도 약 80% 이상 단축.

### ✅ Issue 3. 데이터 수집 라이브러리 차단 및 404 이슈 해결
- **문제**: `pykrx`의 KRX 강제 로그인 정책 도입 및 `FinanceDataReader`의 상장사 캐시 URL 404 에러로 인해 일봉 수집 실패.
- **해결**: 외부 라이브러리 의존성을 제거하고 **네이버 금융 fchart API(XML) 직결 수집기(`daily_collector.py`)** 구현.
- **결과**: 인증 없이 0.05초 만에 종목명과 8대 일봉 매트릭스(`open`, `high`, `low`, `close`, `tradamt`, `mkt`, `shares`, `float`) 생성 완료.

### ✅ Issue 4. 고빈도(HF) 1초봉 LOB 데이터 파이프라인 정착
- **문제**: 장중 실시간으로 1초봉(51개 컬럼)을 조립하면 장초반 틱 폭주 시 웹소켓 지연(Lag) 및 패킷 유실 발생 위험.
- **해결 (2-Step Architecture)**:
  - **Stage 1 (`tick_raw_logger.py`)**: 장중에는 KIS WebSocket 틱/호가 데이터를 SQLite WAL 모드로 무가공 초고속 기록.
  - **Stage 2 (`build_lob_db.py`)**: 장 마감 후 일괄 리샘플링하여 엔진 표준인 **51개 컬럼의 `{YYYYMMDD}_LOB.db`** 빌드.

---

## 3. 데이터 스키마 명세 (51 Columns LOB)

엔진과 수집기가 공유하는 1초봉 LOB 스키마 구조:
* **0**: `time` (HHMMSS)
* **1 ~ 4**: `open`, `high`, `low`, `close`
* **5 ~ 7**: `vol`, `buy_vol`, `sell_vol`
* **8 ~ 10**: `tick`, `buy_tick`, `sell_tick`
* **11 ~ 20**: `offer_p1` ~ `offer_p10` (매도호가 1~10단계)
* **21 ~ 30**: `offer_v1` ~ `offer_v10` (매도호가 잔량 1~10단계)
* **31 ~ 40**: `bid_p1` ~ `bid_p10` (매수호가 1~10단계)
* **41 ~ 50**: `bid_v1` ~ `bid_v10` (매수호가 잔량 1~10단계)

---

## 4. 진행 현황 체크리스트 (Progress)
- [x] 로컬 절대 경로(`C:\Users\...`) ➔ 상대 경로 자동 계산 구조 개편
- [x] CP949 / UTF-8 크로스 플랫폼 인코딩 정합성 확보
- [x] 일봉 8대 매트릭스 CSV 생성기 구현 (`daily_collector.py`)
- [x] 1초봉 51개 컬럼 LOB 생성기 구현 (`ws_lob_collector.py`, `build_lob_db.py`)
- [x] KIS WebSocket 실시간 틱 수집 데몬 구현 (`tick_raw_logger.py`)
- [x] Streamlit 대시보드 및 로컬 Ollama AI 연동 (`dashboard/`)
- [ ] 실시간 잔고 조회 및 주문 실행기 어댑터 구현 (`trader/broker_adapter.py`)
---

## 5. 현재 작업 위치 및 다음 실행 태스크 (Current Handover)
* **직전 완료 사항**:
  - `daily_collector.py` (네이버 직결 일봉 8대 매트릭스 생성) 검증 완료
  - `tick_raw_logger.py` 및 `build_lob_db.py` 2단계 파이프라인 가상 테스트(`--test`) 검증 완료
  - 한국투자증권 모의투자 API Key 발급 및 루트 `KIS_APP.env` 파일 로드 연동 완료
* **지금 즉시 실행할 태스크 (Next Action)**:
  1. 실제 장중 웹소켓 수집 가동: `uv run python collector/tick_raw_logger.py` (10~20초 수집 후 Ctrl+C)
  2. 수집된 실제 틱데이터 1초봉 LOB 변환: `uv run python collector/build_lob_db.py`
  3. 변환된 실제 데이터 기반 백테스트 및 대시보드 연동 테스트