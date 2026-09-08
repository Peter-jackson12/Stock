# 📈 High-Frequency Quant Backtest Engine & Trading Platform

> **3년간의 실전 트레이딩 노하우와 2022년 하락장 실계좌 검증을 바탕으로 구축된 고성능 End-to-End 퀀트 트레이딩 플랫폼**

[![Python Version](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![Package Manager](https://img.shields.io/badge/Package_Manager-uv-blueviolet.svg)](https://github.com/astral-sh/uv)
[![Architecture](https://img.shields.io/badge/Architecture-Modular%20%26%20Parallel-success.svg)]()
[![Broker API](https://img.shields.io/badge/Broker_API-KIS%20OpenAPI-green.svg)]()
[![Frontend](https://img.shields.io/badge/Dashboard-Streamlit%20%7C%20Ollama-orange.svg)]()

---

## 1. 📌 프로젝트 개요 (Executive Summary)

본 프로젝트는 단순한 이론적 백테스팅 스크립트가 아닙니다.  
**3년간 실전 시장에서 직접 트레이딩하며 누적한 호가/체결 데이터 처리 노하우를 바탕으로, 과거 레거시(단일 스크립트) 코드를 엔터프라이즈급 모듈화·병렬 엔진으로 전면 재설계한 고빈도(HF) 퀀트 트레이딩 시스템**입니다.

* **핵심 지향점:** 데이터 수집(Upstream) ➔ 멀티프로세싱 백테스팅(Core) ➔ 인터랙티브 웹 대시보드 및 로컬 LLM 해석(UI/AI) ➔ 실시간 자동주문(Downstream)으로 이어지는 **올인원 퀀트 파이프라인 구축**
* **안정적인 2단계 데이터 파이프라인:** 장중 틱 지연(Lag)을 방지하기 위해 **"장중 초고속 Raw 틱 수집 ➔ 장 마감 후 51개 컬럼 1초봉 LOB 리샘플링"** 구조 채택

---

## 2. 📂 프로젝트 아키텍처 (Directory Structure)

```text
Stock/
├── collector/                 # [Upstream] 데이터 수집 파이프라인
│   ├── daily_collector.py     # 네이버 금융 다이렉트 API 기반 일봉 8대 매트릭스 수집기
│   ├── tick_raw_logger.py     # [Stage 1] 한투 WebSocket 실시간 틱/호가 무가공 초고속 덤프
│   ├── build_lob_db.py        # [Stage 2] Raw 틱 ➔ 51개 컬럼 1초봉 LOB SQLite 변환 배치
│   └── ws_lob_collector.py    # 실시간 즉시 1초봉 버퍼링 수집기 (테스트 및 모의용)
├── engine/                    # [Core] 고성능 모듈화 백테스팅 엔진
│   ├── config.py              # 경로, 전략 파라미터, 인코딩 글로벌 설정
│   ├── data_loader.py         # 일봉 매트릭스 CSV & 초봉 LOB DB 로더
│   ├── strategy.py            # 5/10/30/60초 윈도우 CBV 및 모멘텀 진입 시그널 연산
│   ├── risk_manager.py        # 원칙 손절(-6%), 트레일링스탑, 장마감 동시 청산 제어
│   ├── engine.py              # 타임라인 기반 가상 체결 및 포지션 조율 엔진
│   └── utils.py               # 호가단위(Tick Size), 상한가 산출, 시간 연산 유틸
├── dashboard/                 # [UI / Analytics] Streamlit 인터랙티브 대시보드
│   ├── app.py                 # 대시보드 메인 진입점
│   ├── data_service.py        # 결과 CSV 로드 및 파생 컬럼 전처리
│   ├── metrics.py             # PnL, MDD, 승률, Profit Factor, TPI 산출
│   ├── charts.py              # Plotly 기반 시계열 및 분포 인터랙티브 시각화
│   ├── analyzer.py            # 변수 구간별 성과 분석 및 규칙 기반 인사이트
│   └── ollama_client.py       # Ollama(llama3.2) 기반 로컬 AI 전략 분석 어시스턴트
├── sampledata/                # 데이터 저장소
│   ├── Daily/                 # 8대 일봉 매트릭스 CSV (open, high, low, close, tradamt, mkt, shares, float)
│   ├── raw_ticks/             # [Stage 1] 당일 원본 틱/호가 SQLite DB ({YYYYMMDD}_raw.db)
│   └── temp/                  # [Stage 2] 리샘플링된 초봉 LOB DB ({YYYYMMDD}_LOB.db)
├── results/                   # 백테스트 성과 리포트 CSV
├── main.py                    # 단일 프로세스 디버깅 및 테스트 진입점
├── run_parallel.py            # 멀티프로세스 병렬 가동 및 자동 리포트 병합기
├── pyproject.toml             # uv 패키지 명세서
└── .gitignore                 # 환경변수 및 대용량 DB 파일 제외 명세
```

---

## 3. 💾 데이터 수집 및 2단계 파이프라인 (Data Pipeline)

### 3.1 일봉 8대 매트릭스 데이터 수집 (`daily_collector.py`)
- **수집 방식**: 외부 라이브러리 인증 차단 이슈를 배제하고, 네이버 금융 fchart API(XML)를 직접 파싱하여 0.05초 만에 종목명과 일봉 데이터를 동시 확보
- **생성 파일 (8종)**: `open`, `high`, `low`, `close`, `tradamt`(억원), `mkt`(억원), `shares`(주), `float`(%)
- **규격**: 행=날짜, 열=`A+6자리코드`, 1행=종목명, 인코딩=`CP949`

### 3.2 고빈도(HF) 1초봉 LOB 파이프라인
1. **[Stage 1] 장중 실시간 틱 덤프 (`tick_raw_logger.py`)**:  
   - 한국투자증권 WebSocket 스트리밍 체결(`H0STCNT0`) 및 10호가(`H0STASP0`)를 무가공으로 초고속 SQLite WAL 모드에 단순 `INSERT` (장중 패킷 드랍 0% 보장)
2. **[Stage 2] 장 마감 후 LOB 리샘플링 (`build_lob_db.py`)**:  
   - 15:35 장 마감 후 틱 데이터를 1초 단위로 묶어 OHLCV, 매수/매도체결량, 틱수 집계 및 당시 최신 10호가 스냅샷을 결합하여 **정확히 51개 컬럼의 `{YYYYMMDD}_LOB.db`** 생성

---

## 4. 🚀 실행 방법 (Quick Start)

### 4.1 의존성 설치
```bash
uv sync
```

### 4.2 데이터 파이프라인 가동
```bash
# 1) 일봉 8대 매트릭스 데이터 수집
uv run python collector/daily_collector.py

# 2) [장중 09:00~15:35] 실시간 틱 로거 가동
uv run python collector/tick_raw_logger.py

# 3) [장마감 15:35 이후] 당일 틱데이터를 1초봉 LOB.db로 변환
uv run python collector/build_lob_db.py
```

### 4.3 백테스트 및 대시보드 실행
```bash
# 멀티프로세싱 병렬 백테스트 가동
uv run python run_parallel.py

# 인터랙티브 Streamlit 대시보드 실행
uv run streamlit run dashboard/app.py
```

---

## 5. 🗺️ 개발 로드맵 & 마일스톤

- [x] **Phase 1: 코어 엔진 리팩토링 & 병렬화 (완료 ✅)**
  - 레거시 1,000줄 단일 코드를 7대 독립 모듈로 분리 및 `multiprocessing` 80% 가속
- [x] **Phase 2: Streamlit 분석 대시보드 & Ollama 통합 (완료 ✅)**
  - PnL, MDD, Profit Factor, TPI 시각화 및 로컬 LLM 전략 자동 해석 기능 구현
- [x] **Phase 3: 엔터프라이즈 데이터 인제스천 파이프라인 구축 (완료 ✅)**
  - 네이버 다이렉트 일봉 8대 매트릭스 생성기 완비
  - 한투 WebSocket 2단계(Raw 덤프 ➔ 1초봉 51컬럼 LOB 변환) 고빈도 파이프라인 구축
- [ ] **Phase 4: 실전/모의 자동매매 주문 집행기 (`trader/`) 연동 (예정 🎯)**
  - KIS REST API 기반 실시간 잔고 동기화 및 조건 충족 시 1-Click 자동 주문 집행
