# README 재정리 전 기록 — 2026-09-16

이력 보존본이다. 과거 완료/성능 표현은 현재 운영 검증 결과가 아니다.
현재 안내는 [README](../../README.md)에서 시작한다. 이 문서의 명령을 현재 실행 순서로 사용하지 않는다.

---

# 📈 High-Frequency Quant Backtest Engine & Trading Platform

> **현재 주 경로: 원본 틱 백테스트.** LOB/초봉 변환은 필요하지 않습니다.
> 첫 목표는 기존 NXT 돌파 전략의 틱 재현 정확성 확인입니다.
> 설계와 구현 순서의 기준: [틱 중심 파이프라인 설계](../../ARCHITECTURE_TICK.md).

## 운영 화면 — 컨트롤 타워 1단계

수집 로그 관측, 별도 워커의 연구 결과 조회, 장외 재생 **계획 저장**, 작업 이력을
기존 대시보드에 연결했다. 수집기 시작/종료·실제 재생·자동매매 제어는 후속 단계다.
설계·현재 연결 범위·다음 순서는 [CONTROL_TOWER.md](../../CONTROL_TOWER.md)를 따른다.

```powershell
.\.venv\Scripts\python.exe -m streamlit run dashboard/app.py --server.address 127.0.0.1
```

기본 화면은 운영 관리다. 왼쪽에서 **백테스트 분석**으로 전환하면 기존 런 비교를 볼 수 있다.

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

* **현재 수집**: 키움 체결·10호가 이벤트를 `raw_ticks`에 저장한다. 실제 커버리지는 수신 로그와 명부로 확인한다.
* **주 경로 — 틱 백테스트**: 원본 이벤트 → 순서/품질 확인 → 틱 재생 → 전략 → 가상 주문/체결 → 결과.
  현재 `nxt_tick_engine.py`는 프로토타입이며 동일 초 호가 연결·무호가 대체·체결 모델을 검증할 계획이다.
* **레거시 — 1초봉 엔진**: `engine.py`, LOB 변환, `features/fs_v1`은 기존 회귀용으로 보존한다.
  거래 5건 / -1.301% 기준선은 틱 백테스트의 목표 수익률이나 완료 기준이 아니다.
* **별도 입력 — 메타데이터**: 종목·shares·float·기준가격은 그 시점에 이용 가능했던 값만 사용한다.
  일봉 수집은 틱을 초봉으로 변환하기 위한 단계가 아니다.

---

## 2. 📂 프로젝트 아키텍처 (Directory Structure)

```text
Stock/
├── collector/                     # [Upstream] 데이터 수집 파이프라인
│   ├── kiwoom/                    # [Core Collector] 키움증권 32비트 전 종목 수집기
│   │   ├── kiwoom_universe_logger.py # 2,550개 보통주 체결/10호가 비동기 큐 초고속 덤프
│   │   ├── test_login.py          # 키움 OpenAPI 연결 및 로그인 검증 스크립트
│   │   └── requirements.txt       # 32비트 보조 가상환경 의존성 명세 (PyQt5)
│   ├── daily_collector.py         # fchart 가격 격리 저장 + 당일 메타데이터 스냅샷
│   ├── run_daily_daemon.py        # 별도 KIS 틱 수집 + LOB/일봉 후처리 데몬
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
│   ├── run_parallel.py            # 멀티프로세스 병렬 백테스트 가동 진입점
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
│   └── temp/                      # [레거시] 리샘플링된 초봉 LOB DB ({YYYYMMDD}_LOB.db)
├── results/                       # 백테스트 성과 리포트 CSV
├── setup_kiwoom.ps1               # [자동화] 32비트 키움 수집 환경 1클릭 복구 스크립트
├── pyproject.toml                 # uv 패키지 명세서 (64비트 메인 환경)
└── .gitignore                     # 비밀키 및 대용량 DB 파일 제외 명세
```

---

## 3. 💾 데이터 파이프라인 & 미시구조 설계 (Data Architecture)

### 3.1 2,550개 보통주 실시간 수집 (`kiwoom_universe_logger.py`)
- **선택 편향 차단**: 거래대금이나 시총으로 장전에 종목을 걸러내지 않고, 시장 전체 보통주를 전수 수집하여 사후 필터링 시 왜곡 방지
- **노이즈 및 트래픽 최적화**: 거래는 적고 LP(알고리즘)의 기계적 호가 정정만 초당 수십 건씩 쏟아지는 **ETF, ETN, 스팩, 우선주를 자동 배제**하여 일일 DB 용량을 50% 이상 절감
- **생산자-소비자 큐**: GUI 콜백에서 큐를 거쳐 SQLite WAL 배치 저장한다. 실제 처리량·무누락은 별도 측정 대상이다.
- **방향 판정 미확인**: 현재 v1 코드가 방향 판단에 사용하는 FID 14는 누적거래대금이다.
  기존 `is_buy`를 검증된 원천 방향으로 취급하지 않는다. 새 정규화기는 기본값을 unknown으로 두며
  실제 피드 부호 확인 후 정책을 선택한다. 근거와 제한은 [교차 검증 문서](../../TICK_CROSS_REVIEW.md)를 따른다.

### 3.2 대체거래소(NXT) 반영 및 가혹한 실전 슬리피지 모델링
- **Nextrade(08:00~08:50) 과열 필터**: 8시 프리마켓에서 호재가 선반영되어 이미 폭등/거래 폭발한 종목은 09:00 정규장 개장 직후 차익실현 음봉(설거지) 위험이 높으므로 진입 제한
- **1~2초 지연 체결 페널티 (`LATENCY_SEC = 1`)**: 시그널 즉시 체결되는 환상을 깨고, 현실적인 통신 렉과 호가 붕괴를 보수적으로 시뮬레이션하기 위해 1초 뒤의 실제 호가창 매도1호가로 체결

---

## 4. 🚀 실행 방법 (Quick Start)

### 4.1 메인 64비트 환경 복구 및 일봉 수집
```powershell
# 1) 64비트 의존성 설치
uv sync

# 2) 소수 종목 fchart 수집 (가격은 격리 저장, 실제가 공급자 연결은 미완료)
uv run python collector/daily_collector.py
```

### 4.2 키움 32비트 전 종목 틱 수집 데몬 가동
```powershell
# 1) [최초 1회] 키움 전용 32비트 가상환경 원클릭 자동 구축
.\setup_kiwoom.ps1

# 2) [장중 08:00~15:35] 전 종목 실시간 틱 수집기 가동 (관리자 권한 터미널 권장)
.\.venv32\Scripts\python.exe collector/kiwoom/kiwoom_universe_logger.py
```

### 4.3 주 경로: 수집 종료 후 틱 백테스트

아래는 현재 프로토타입의 실행법이다. 미래 호가/체결 정확성 검증 완료를 뜻하지 않는다.

```powershell
# 수집이 끝난 실제 날짜를 명시한다. LOB 변환이나 fs_v1 빌드는 필요 없다.
uv run python engine/nxt_tick_engine.py 005930 --date 20260916

# 저장된 런 결과 조회
uv run streamlit run dashboard/app.py
```

현재 틱 CLI는 날짜를 생략하면 20260911을 사용하므로 반드시 날짜를 지정한다.
장중에는 같은 날짜 raw를 전량 읽는 백테스트를 실행하지 않는다.

<details>
<summary>레거시 1초봉 경로 — 기존 회귀/비교가 필요할 때만</summary>

이 경로는 새 틱 백테스트의 선행 단계가 아니다. 보존된 Daily_baseline/old_data를 덮어쓰지 않는다.

```powershell
uv run python collector/build_lob_db.py 20260916
uv run python scripts/build_features.py --date 20260916
uv run python -m engine.main --dates 20260916 --require-actual-prices
```

LOB 변환은 원본 DB 인덱스와 결과 DB를 쓴다. 실제가 소스 연결 전에는 엄격 모드가 실패할 수 있다.

</details>

### 4.4 현재 연결 상태와 수집 중 가능한 점검

- 현재 키움 `kiwoom_universe_logger.py`는 raw 저장과 세션 보고까지만 수행한다.
  LOB 변환·피처 생성·일봉 수집·메타데이터 파일럿·백테스트를 자동 실행하지 않는다.
- `run_daily_daemon.py`는 KIS 접속을 사용하는 별도 프로그램이다. 키움과 동일한
  날짜별 raw DB 경로를 사용하므로, 키움 후처리만 실행하려고 함께 켜지 않는다.
  KIS 데몬에도 피처 생성과 백테스트 자동 연결은 없다.
- 주 경로는 `raw → nxt_tick_engine → runs → dashboard`다.
  레거시는 `raw → temp/LOB → features/fs_v1 → engine.main → runs → dashboard`이며 Daily도 읽는다.
  opt10001 결과는 현재 `Daily_kiwoom_pilot`에만 반영되고 운영 Daily로 승격되지 않는다.
- 수집 중 가벼운 점검: 코드/설정 읽기, 로그 끝부분, 프로세스 상태,
  `scripts/check_tick_collection.py`의 짧은 읽기 전용 관측.
  변환기의 `--limit`은 종목 처리만 줄이고 raw 전체 인덱스 생성은 막지 않는다.
- LOB 변환·피처 생성·실데이터 백테스트·병렬 실행·전체 데이터 검증은 수집 종료 후 진행한다.
  파일 존재나 연결 코드 확인만으로 데이터 완전성·전 단계 성공을 판정하지 않는다.

## 5. 🗺️ 개발 로드맵 & 마일스톤

> 아래는 이전 단계의 진행 기록이다. 현재 틱 재생의 정확성 완료를 뜻하지 않는다.
> 다음 작업 순서는 [틱 중심 설계 §11](../../ARCHITECTURE_TICK.md#11-구현-순서와-완료-기준)을 따른다.

- [x] **Phase 1: 코어 엔진 리팩토링 & 병렬화 (완료 ✅)**
  - 레거시 1,000줄 단일 코드를 7대 독립 모듈로 분리 및 `multiprocessing` 가속
- [x] **Phase 2: Streamlit 분석 대시보드 & Ollama LLM 통합 (완료 ✅)**
  - PnL, MDD, 승률 인터랙티브 차트 및 로컬 AI 분석관 구축
- [x] **Phase 3: 엔터프라이즈 전 종목 데이터 인제스천 파이프라인 (완료 ✅)**
  - 32비트 키움 Open API+ 기반 보통주 2,550개 전 종목 틱/호가 무누락 수집 데몬 완성
  - 51개 컬럼 LOB 2단계 리샘플러 완비
- [x] **Phase 3.5: 미시구조 틱 이벤트 엔진 및 Nextrade 방어 로직 (완료 ✅)**
  - NXT 08:00 프리마켓 과열 방어 + 1초 지연 체결 + 3대 트레일링 컷 비교 엔진 탑재
- [ ] **Phase 3.7: 엔진/전략/피처 아키텍처 확장 (Architecture V2, 진행 중 🔄)**
  - 전략 1개 → N개로 확장 가능한 구조로 재설계: 런 스토어, 피처 스토어, 전략 추상화, Broker 포트
  - 세부 단계와 진행 상황은 `ARCHITECTURE_V2.md` §8 로드맵 참조
- [ ] **Phase 4: 홈 PC 원격 무인 데이터 센터 가동 (진행 중 🔄)**
  - 크롬 원격 데스크톱 기반 24시간 상시 가동 환경 구축
- [ ] **Phase 5: 실전/모의 자동 주문 집행기 (`trader/`) 연동 (예정 🎯)**
  - KIS / 키움 REST API 기반 SOR(최선집행) 자동 주문 봇 구현
  - 기술적 하부 구조는 Architecture V2 Phase E(Broker 포트)에서 준비됨 — `ARCHITECTURE_V2.md` §5 참조


## 키움 메타데이터 파일럿 배치 (opt10001)

실시간 틱 수집기와 별도 실행한다. 주문 기능은 없으며 최대 50종목의
shares·시가총액·유통비율을 수신한다. 현재 자동 실행/운영 Daily 반영은 연결하지 않았다.
아래는 저장소 루트 PowerShell에서 실행한다.

```powershell
# 1. 64비트: core.universe의 3종목 선언으로 오늘 계획 생성 (네트워크 요청 없음)
.\.venv\Scripts\python.exe scripts/kiwoom_metadata.py prepare --universe blue_chips --server mock --shares-multiplier 1000 --limit 3

# 2. 32비트: 출력된 계획 파일 경로를 사용. 로그인 창에서 해당 서버로 접속한다.
.\.venv32\Scripts\python.exe collector/kiwoom/run_meta_batch.py --job <계획파일.json>

# 3. 64비트: 성공/결측 원응답을 파일럿 tidy 및 파생 CSV로 반영
.\.venv\Scripts\python.exe scripts/kiwoom_metadata.py import --job <계획파일.json>
```

- 첫 실행은 모의서버 소수 종목으로 확인한다. 모의서버 중복 로그인은 제한되므로
  KOA Studio 등 다른 모의 OpenAPI 접속과 동시 실행하지 않는다.
- 주식수 배수 `1000`은 사용자 제공 삼성전자 표본과 정합성 검사에 근거한 명시적
  설정이다. 원천 단위와 유통비율 정의/갱신 시점의 확인은 별도로 필요하다.
- 실제 서버 구분 응답이 계획과 다르거나 알 수 없으면 TR 전송 전에 중단한다.
  실서버 확인은 `--server live`로 별도 계획을 만들며 원응답/완료 상태를 분리한다.
- 기본 호출 간격 4초, 응답 제한 20초, 종목당 실행당 최대 2회 시도.
  요청 거부/연결 끊김은 중단한다. 이 배치 밖의 API 호출량까지 제한하지는 못한다.
- Ctrl+C 후 **같은 날 같은 계획으로 재실행**하면 완료 종목을 건너뛴다.
  실패/결측 종목은 다시 요청한다. 날짜가 바뀌면 새 계획을 만든다.
- 상태와 원응답: `sampledata/kiwoom_meta/state.db`.
  스냅샷: `sampledata/Daily_kiwoom_pilot/mock/` 또는 `live/`.
  변환 재실행은 스냅샷 유효값을 보존한다(원응답 보관 JSON은 다시 추가될 수 있음).
- 완료는 세 메타데이터 값 수신과 정합성 검사 통과를 뜻한다. 일봉 실제가 인증,
  과거 백필 또는 전략 필터의 사용 승인을 뜻하지 않는다.

공식 참고: [키움 OpenAPI+ TR 제한](https://www1.kiwoom.com/h/customer/download/VOpenApiInfoView?dummyVal=0),
[요청·응답 메서드와 이벤트 명세](https://download.kiwoom.com/web/openapi/kiwoom_openapi_plus_devguide_ver_1.7.pdf).

### 틱 수집기 종료와 저장 확인

- Ctrl+C 또는 15:35 자동 종료 시 수신을 멈추고 대기큐와 저장 중인 배치를
  끝까지 커밋한 뒤 종료한다. `종료 후 미커밋: 0 건`과 `DB 저장 결과: 커밋 완료`를 확인한다.
- 저장 중에는 프로세스를 기다린다. 두 번째 Ctrl+C, 창 강제 닫기 또는 전원 장애는
  저장 완료를 보장하지 않는다. SQLite의 기존 `synchronous=OFF` 설정은 유지한다.
- DB 오류가 발생하면 수집을 중단하고 오류 및 미커밋 건수를 기록하며 종료 코드 2를
  반환한다. 미커밋 데이터는 재실행만으로 복구되지 않는다.
- 변경된 종료 처리는 다음 실행부터 적용된다. 변경 전부터 실행 중인 프로세스에는
  자동 반영되지 않으므로, 운영 적용 전에 변경분을 커밋한다.

### 일봉 실제가 출처 검사 (D-6)

- 1초봉 엔진은 알려진 fchart 수정주가를 읽기 전에 차단한다. 기존
  `unverified_fchart/` 경로와 새 `_price_manifest.json` 표식을 검사한다.
- 앞으로의 데이터 검증에는 `python -m engine.main --require-actual-prices`를 사용한다.
  실제가 출처 선언이 없거나 불명확하면 실패한다. 아직 실제가 공급자를 연결하지
  않았으므로 현재 운영 Daily가 이 검사를 통과한다고 보장하지 않는다.
- 옵션을 생략하면 출처가 없는 기존 기준선의 재현을 허용한다. 이 경우 콘솔과
  런 매니페스트에 출처 미확정을 기록하며, 실제가로 인증한 것으로 해석하지 않는다.
- 가격 표식은 **같은 디렉터리의 지정된 파일만** 설명한다. 기존
  `_meta_manifest.json`의 fchart 정보로 상위 Daily의 보존된 가격을 재분류하지 않는다.
  새 fchart 수집분은 CSV보다 먼저 격리 폴더에 표식을 저장한다.
- 실제가 선언(`actual_at_event_time`)은 검증한 공급자 어댑터가 작성할 계약이다.
  표식만으로 원천 가격을 독립 검증하지 않는다. 분할일 기준가와 전일 종가 결측 시
  처리도 별도 검증 대상이며, 이번 출처 검사로 해당 계산의 정확성을 보장하지 않는다.
- 이 옵션은 `engine.main`의 1초봉 엔진에 연결되어 있다.
  `nxt_breakout/params/default.yaml`의 정책이 틱 엔진에 자동 적용된다는 뜻은 아니다.
- 엄격 모드에서는 전일 종가 결측 시 당일 시가 × 1.3 근사를 허용하지 않는다.
  날짜 형식·중복·정렬, open/close의 당일 행 및 바로 앞 날짜 일치, 전일 종가와
  LOB 시가의 양의 유한값을 검사한다. 가격 입력 오류는 실행 전체를 실패시키며
  성공 결과를 저장하지 않는다. 기존 레거시 재현 모드의 결측 처리는 유지한다.
- 이 검사는 주어진 두 일봉 파일을 대조한다. 양쪽에서 함께 빠진 거래일과
  분할·권리변동일의 거래소 기준가는 별도 캘린더/공급자 데이터로 검증해야 한다.

### fchart 50종목 응답 측정 (rev.2 확인 3)

```powershell
# 로컬 key.csv와 core.universe에서 고정 난수 표본 50종목 선택: HTTP 요청 없음
.\.venv\Scripts\python.exe scripts/probe_fchart.py

# 일반 PowerShell에서 실제 응답 측정
.\.venv\Scripts\python.exe scripts/probe_fchart.py --execute --environment ordinary_powershell
```

- 기본 규칙은 `kospi_kosdaq_common`, 표본 seed는 0, 상한 50종목이다.
  계획에 해석한 종목 수와 실제 목록을 기록한다. 로컬 key.csv의 최신성은 별도 확인한다.
- 종목 사이 1초, 요청 제한 10초, 일시 오류는 최대 3회 시도한다.
  403/429는 즉시 중단하고 종목 3개가 연속 실패해도 멈춘다.
- 수집기와 동일한 응답 파서로 검증하며, 일봉/메타데이터 CSV는 저장하지 않는다.
  `logs/fchart_probe/<실행별 폴더>/`에 요청 시작·결과 JSONL과 요약 JSON을 남긴다.
- HTTP 상태, 실패/재시도, 응답 크기·행 수·마지막 날짜, 요청 지연 중앙값/p95 및
  첫 10건/마지막 10건 중앙값을 기록한다. Ctrl+C 중단도 부분 결과로 남는다.
  강제 종료 시에는 JSONL의 요청 시작/완료 기록으로 진행 상태를 확인한다.
- `--environment`는 실행 위치의 자기 신고다. 프록시 환경변수 유무를 기록하지만
  실제 경로/IP를 입증하지 않는다. 에이전트 실행은 `agent_shell`로 표시한다.
  50종목 성공도 전 종목 요청 안정성·원천 가격 정확성·최신 유니버스를 보장하지 않는다.
## 틱 연구 실행 안내

닫힌 raw v2 프로토타입 파일용 CLI와 결과 해석은
[TICK_RESEARCH_RUNBOOK.md](../../TICK_RESEARCH_RUNBOOK.md)를 참고한다.
현재 운영 수집기와의 연결은 아직 완료되지 않았다. 실제 파일의 전체 검증·재생은 장외에 한다.
