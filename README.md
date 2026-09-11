# 📈 High-Frequency Quant Backtest Engine & Trading Platform

> **3년간의 실전 트레이딩 노하우와 2022년 하락장 실계좌 검증을 바탕으로 구축된 고성능 End-to-End 퀀트 트레이딩 플랫폼**

[![Python Version](https://img.shields.io/badge/Python-64bit%203.11%2B%20%7C%2032bit%203.10-blue.svg)](https://www.python.org/)
[![Package Manager](https://img.shields.io/badge/Package_Manager-uv-blueviolet.svg)](https://github.com/astral-sh/uv)
[![Architecture](https://img.shields.io/badge/Architecture-Dual--Engine%20%26%20Event--Driven-success.svg)]()
[![Broker API](https://img.shields.io/badge/Broker_API-Kiwoom%20%7C%20KIS%20OpenAPI-green.svg)]()
[![Frontend](https://img.shields.io/badge/Dashboard-Streamlit%20%7C%20Ollama-orange.svg)]()

---

## 1. 📌 프로젝트 개요 (Executive Summary)

본 프로젝트는 단순한 이론적 백테스팅 스크립트가 아닙니다.  
**3년간 실전 시장에서 직접 트레이딩하며 누적한 호가/체결 데이터 처리 노하우를 바탕으로, 과거 레거시 코드를 고빈도(HF) 틱 이벤트 엔진 및 엔터프라이즈 모듈형 시스템으로 전면 재설계한 퀀트 트레이딩 플랫폼**입니다.

* **선택 편향(Selection Bias) 0% 추구**: 아침 장전 사전 필터링으로 데이터를 오염시키지 않고, **코스피/코스닥 전 종목 보통주(~2,550개)의 실시간 틱/호가 원천 데이터를 무가공으로 100% 수집**
* **하이브리드 듀얼 엔진 구조**:
  - **1초봉 LOB 엔진 (`engine.py`)**: 51개 컬럼 LOB 기반의 시계열 백테스트 및 멀티프로세싱 대규모 탐색
  - **초정밀 틱 이벤트 엔진 (`nxt_tick_engine.py`)**: 틱 단위 호가 체결, 1~2초 통신 렉(Latency) 시뮬레이션, 대체거래소(Nextrade, 08:00) 과열 필터링, 3대 트레일링 컷 승자 결정전 지원
* **안정적인 2단계 데이터 파이프라인**: 
  - **Stage 1 (장중 08:00~20:00)**: 키움 Open API+ 화면번호 26개 분할 등록 기반 초고속 무가공 적재 (`raw_ticks/*.db`)
  - **Stage 2 (장마감 후)**: 당일 원본 틱 ➔ 51개 컬럼 1초봉 LOB 리샘플링 (`temp/*_LOB.db`)

---

## 2. 📂 프로젝트 아키텍처 (Directory Structure)

```text
Stock/
├── collector/                     # [Upstream] 데이터 수집 파이프라인
│   ├── kiwoom/                    # [Core Collector] 키움증권 32비트 전 종목 수집기
│   │   ├── kiwoom_universe_logger.py # 2,550개 보통주 체결/10호가 비동기 큐 초고속 덤프
│   │   ├── test_login.py          # 키움 OpenAPI 연결 및 로그인 검증 스크립트
│   │   └── requirements.txt       # 32비트 보조 가상환경 의존성 명세 (PyQt5)
│   ├── daily_collector.py         # 네이버 금융 XML 직결 일봉 8대 매트릭스 수집기
│   ├── run_daily_daemon.py        # 일봉 데이터 자동 수집 데몬
│   ├── build_lob_db.py            # [Stage 2] 당일 Raw 틱 ➔ 51컬럼 1초봉 LOB DB 배치 변환기
│   ├── tick_raw_logger.py         # [옵션] 한투 KIS 웹소켓 실시간 로거
│   └── universe.py                # 집중 모니터링 주도주 유니버스 관리
├── engine/                        # [Core Engine] 백테스팅 & 시뮬레이션 엔진
│   ├── nxt_tick_engine.py         # [신규] NXT 과열방어 + 1초 지연체결 + 3대 트레일컷 틱 엔진
│   ├── tick_engine.py             # 틱 단위 이벤트 드리븐 백테스트 프로토타입
│   ├── engine.py                  # 51개 컬럼 1초봉 LOB 기반 타임라인 백테스트 엔진
│   ├── main.py                    # 백테스트 실행 메인 엔트리포인트
│   ├── strategy.py                # 5/10/30/60초 윈도우 CBV 및 모멘텀 진입 시그널
│   ├── risk_manager.py            # 손절, 트레일링스탑, 장마감 동시 청산 제어
│   ├── data_loader.py             # 일봉 매트릭스 CSV & 초봉 LOB DB 로더
│   ├── config.py                  # 경로, 전역 설정, 인코딩 관리
│   └── utils.py                   # 호가단위(Tick Size), 상한가 산출 유틸
├── dashboard/                     # [UI / Analytics] Streamlit 인터랙티브 대시보드
│   ├── app.py                     # 대시보드 메인 진입점
│   ├── data_service.py            # 결과 CSV 로드 및 파생 컬럼 전처리
│   ├── metrics.py                 # PnL, MDD, 승률, Profit Factor, TPI 산출
│   ├── charts.py                  # Plotly 기반 인터랙티브 시각화
│   ├── analyzer.py                # 구간별 성과 분석 및 규칙 기반 인사이트
│   └── ollama_client.py           # Ollama(llama3.2) 기반 로컬 AI 전략 분석기
├── sampledata/                    # 계층화 데이터 저장소 (Data Lake)
│   ├── Daily/                     # 8대 일봉 매트릭스 CSV (open, high, low, close, tradamt, mkt, shares, float)
│   ├── raw_ticks/                 # [Stage 1] 당일 원본 틱/호가 SQLite DB ({YYYYMMDD}_raw.db)
│   └── temp/                      # [Stage 2] 리샘플링된 초봉 LOB DB ({YYYYMMDD}_LOB.db)
├── results/                       # 백테스트 성과 리포트 CSV
├── setup_kiwoom.ps1               # [자동화] 32비트 키움 수집 환경 1클릭 복구 스크립트
├── run_parallel.py                # 멀티프로세스 병렬 백테스트 가동 진입점
├── pyproject.toml                 # uv 패키지 명세서 (64비트 메인 환경)
└── .gitignore                     # 비밀키 및 대용량 DB 파일 제외 명세
```

---

## 3. 💾 데이터 파이프라인 & 미시구조 설계 (Data Architecture)

### 3.1 2,550개 보통주 실시간 수집 (`kiwoom_universe_logger.py`)
- **선택 편향 차단**: 거래대금이나 시총으로 장전에 종목을 걸러내지 않고, 시장 전체 보통주를 전수 수집하여 사후 필터링 시 왜곡 방지
- **노이즈 및 트래픽 최적화**: 거래는 적고 LP(알고리즘)의 기계적 호가 정정만 초당 수십 건씩 쏟아지는 **ETF, ETN, 스팩, 우선주를 자동 배제**하여 일일 DB 용량을 50% 이상 절감
- **생산자-소비자 큐 아키텍처**: 초당 1,500건 이상의 체결/호가를 GUI 스레드에서 메모리 큐(`queue.Queue`)로 분리, 백그라운드 스레드가 SQLite WAL 모드로 대량 배치(`executemany`) 적재 (패킷 유실 0%)
- **2중 매수 판정 (Lee-Ready 알고리즘)**: 키움 OpenAPI FID 14(`체결구분`)의 문자열 누락 이슈를 방어하기 위해 **체결가 $\ge$ 최우선 매도호가(`ask_p1`)**를 직접 대조하여 시장가 매수/매도 틱 100% 정밀 분류

### 3.2 대체거래소(NXT) 반영 및 가혹한 실전 슬리피지 모델링
- **Nextrade(08:00~08:50) 과열 필터**: 8시 프리마켓에서 호재가 선반영되어 이미 폭등/거래 폭발한 종목은 09:00 정규장 개장 직후 차익실현 음봉(설거지) 위험이 높으므로 진입 제한
- **1~2초 지연 체결 페널티 (`LATENCY_SEC = 1`)**: 시그널 즉시 체결되는 환상을 깨고, 현실적인 통신 렉과 호가 붕괴를 보수적으로 시뮬레이션하기 위해 1초 뒤의 실제 호가창 매도1호가로 체결

---

## 4. 🚀 실행 방법 (Quick Start)

### 4.1 메인 64비트 환경 복구 및 일봉 수집
```powershell
# 1) 64비트 의존성 설치
uv sync

# 2) 일봉 8대 매트릭스 최신 데이터 수집 (네이버 fchart 직결)
uv run python collector/daily_collector.py
```

### 4.2 키움 32비트 전 종목 틱 수집 데몬 가동
```powershell
# 1) [최초 1회] 키움 전용 32비트 가상환경 원클릭 자동 구축
.\setup_kiwoom.ps1

# 2) [장중 08:00~15:35] 전 종목 실시간 틱 수집기 가동 (관리자 권한 터미널 권장)
.\.venv32\Scripts\python.exe collector/kiwoom/kiwoom_universe_logger.py
```

### 4.3 백테스트 및 3대 트레일링 컷 성적표 확인
```powershell
# 1) [Stage 2] 당일 틱 데이터를 51컬럼 1초봉 LOB로 변환 (옵션)
uv run python collector/build_lob_db.py

# 2) 순수 틱 이벤트 백테스트 가동 (삼성전자 예시)
uv run python engine/nxt_tick_engine.py 005930

# 3) 대시보드 웹앱 실행
uv run streamlit run dashboard/app.py
```

---

## 5. 🗺️ 개발 로드맵 & 마일스톤

- [x] **Phase 1: 코어 엔진 리팩토링 & 병렬화 (완료 ✅)**
  - 레거시 1,000줄 단일 코드를 7대 독립 모듈로 분리 및 `multiprocessing` 가속
- [x] **Phase 2: Streamlit 분석 대시보드 & Ollama LLM 통합 (완료 ✅)**
  - PnL, MDD, 승률 인터랙티브 차트 및 로컬 AI 분석관 구축
- [x] **Phase 3: 엔터프라이즈 전 종목 데이터 인제스천 파이프라인 (완료 ✅)**
  - 32비트 키움 Open API+ 기반 보통주 2,550개 전 종목 틱/호가 무누락 수집 데몬 완성
  - 51개 컬럼 LOB 2단계 리샘플러 완비
- [x] **Phase 3.5: 미시구조 틱 이벤트 엔진 및 Nextrade 방어 로직 (완료 ✅)**
  - NXT 08:00 프리마켓 과열 방어 + 1초 지연 체결 + 3대 트레일링 컷 비교 엔진 탑재
- [ ] **Phase 4: 홈 PC 원격 무인 데이터 센터 가동 (진행 중 🔄)**
  - 크롬 원격 데스크톱 기반 24시간 상시 가동 환경 구축
- [ ] **Phase 5: 실전/모의 자동 주문 집행기 (`trader/`) 연동 (예정 🎯)**
  - KIS / 키움 REST API 기반 SOR(최선집행) 자동 주문 봇 구현
