### 📄 `PIPELINE_DESIGN.md` (파이프라인 설계서 전문)

프로젝트 루트에 **`PIPELINE_DESIGN.md`** 파일을 생성하고 아래 내용을 그대로 넣어주세요.

```markdown
# 🏗️ End-to-End High-Frequency Quant Data & Trading Pipeline Design

> **본 문서는 코스피/코스닥 전 종목(~2,550개) 실시간 틱 수집부터 대체거래소(Nextrade) 반영, 1초봉 LOB 리샘플링, 이벤트 기반 백테스팅 및 대시보드 시각화까지 이어지는 전체 파이프라인의 아키텍처 설계서입니다.**

---

## 1. 🌐 파이프라인 전체 조감도 (System Architecture Flow)

시스템은 **[32비트 수집 전담 계층]**과 **[64비트 메인 연산 계층]**이 SQLite 파일 기반의 이벤트 브로커를 통해 완전히 디커플링(Decoupled)되어 동작합니다.

```mermaid
flowchart TD
    subgraph Market [1. 실시간 시장 데이터 (Upstream)]
        NXT[대체거래소 Nextrade<br/>08:00 ~ 20:00]
        KRX[한국거래소 KRX<br/>09:00 ~ 15:30]
        NAVER[네이버 금융 fchart API<br/>과거/당일 일봉 XML]
    end

    subgraph Ingestion [2. 32비트 고속 수집 계층 (.venv32)]
        KW[Kiwoom Open API+<br/>화면번호 26개 분할 매핑]
        FILTER[보통주 선별 필터<br/>ETF/ETN/스팩/우선주 배제]
        Q_BUFFER[(Producer-Consumer<br/>메모리 큐 버퍼)]
        RAW_LOGGER[kiwoom_universe_logger.py<br/>초당 1,500+ 이벤트 무유실 적재]
        DAILY_COL[daily_collector.py<br/>8대 일봉 매트릭스 수집]
    end

    subgraph Storage [3. 계층화 데이터 레이크 (Storage Tiering)]
        RAW_DB[(Raw Lake: raw_ticks/<br/>YYYYMMDD_raw.db<br/>trades / quotes)]
        DAILY_CSV[(Macro Lake: Daily/<br/>8대 매트릭스 CSV<br/>open, high, mkt, float...)]
        LOB_DB[(Micro Lake: temp/<br/>YYYYMMDD_LOB.db<br/>51개 컬럼 1초봉)]
        COLD[(Cold Archive: HDD<br/>30일 경과 ZSTD 압축 보관)]
    end

    subgraph Processing [4. 64비트 ETL 및 백테스트 코어 (uv .venv)]
        RESAMPLE[build_lob_db.py<br/>1초봉 + 10호가 리샘플링]
        TICK_ENG[nxt_tick_engine.py<br/>이벤트 기반 틱 엔진<br/>1초 렉 시뮬레이션]
        PARALLEL[run_parallel.py<br/>multiprocessing LOB 엔진]
        STRAT[전략 및 청산 평가<br/>NXT 과열방어 + 3대 트레일컷]
    end

    subgraph Downstream [5. 서비스 및 실행 계층 (Serving & Execution)]
        DASH[dashboard/app.py<br/>Streamlit 인터랙티브 UI]
        LLM[ollama_client.py<br/>로컬 AI 전략 분석관]
        REPORT[(results/<br/>성과 리포트 CSV)]
        TRADER[trader/broker_adapter.py<br/>SOR 최선집행 주문 집행기]
    end

    %% 연결 관계
    NXT & KRX --> KW
    KW --> FILTER --> Q_BUFFER --> RAW_LOGGER --> RAW_DB
    NAVER --> DAILY_COL --> DAILY_CSV

    RAW_DB --> RESAMPLE --> LOB_DB
    RAW_DB --> TICK_ENG
    DAILY_CSV & LOB_DB --> PARALLEL
    
    RAW_DB -.->|30일 경과 자동 이전| COLD

    TICK_ENG --> STRAT --> REPORT
    PARALLEL --> REPORT
    REPORT --> DASH
    DASH <--> LLM
    STRAT -.->|실거래 시그널| TRADER

    style Market fill:#ECEFF1,stroke:#607D8B
    style Ingestion fill:#FFF3E0,stroke:#FF9800
    style Storage fill:#E8F5E9,stroke:#4CAF50
    style Processing fill:#E3F2FD,stroke:#2196F3
    style Downstream fill:#F3E5F5,stroke:#9C27B0
```

---

## 2. ⏱️ 시간대별 파이프라인 생애주기 (Time-based Operational Lifecycle)

하루 24시간 동안 파이프라인이 어떤 일정과 조건으로 전환되는지를 정의한 운영 명세입니다.

| 시간 (KST) | 프로세스 / 트리거 | 수행 작업 및 데이터 흐름 | 책임 모듈 |
| :---: | :--- | :--- | :--- |
| **07:50** | Pre-flight Check | • 디스크 용량 점검 및 네트워크 상태 확인<br/>• 32비트 수집기 환경 점검 | `setup_kiwoom.ps1` |
| **08:00 ~ 08:50** | **NXT 프리마켓 수집** | • 대체거래소(Nextrade) 프리마켓 거래 수집 시작<br/>• 밤사이 호재에 따른 갭상승/과열 거래량 실시간 누적 | `kiwoom_universe_logger.py` |
| **08:50 ~ 09:00** | 호가 정합 및 동시호가 | • KRX 시가 동시호가 호가잔량 변동 감시<br/>• 수집기 메모리 큐 정합성 체크 | `kiwoom_universe_logger.py` |
| **09:00 ~ 15:30** | **KRX 정규장 전수 수집** | • 2,550개 보통주 체결(`raw_trades`) 및 10호가(`raw_quotes`) 무가공 적재<br/>• 초당 1,500+ 이벤트 SQLite WAL 모드 고속 Flush | `kiwoom_universe_logger.py` |
| **15:35** | 장 마감 및 수집기 셧다운 | • 키움 실시간 등록 일괄 해제(`SetRealRemove("ALL", "ALL")`)<br/>• 잔여 큐 Flush 후 SQLite 정상 Close | `kiwoom_universe_logger.py` |
| **15:36 ~ 15:45** | **Post-Market Batch ETL** | • **Step 1**: 당일 틱 데이터 ➔ 51개 컬럼 1초봉 LOB 리샘플링<br/>• **Step 2**: 네이버 XML 직결 당일 일봉 8대 매트릭스 CSV 갱신 | `build_lob_db.py`<br/>`daily_collector.py` |
| **15:45 ~ 16:00** | **백테스트 및 전략 평가** | • **Track A**: 3대 트레일링 컷 틱 백테스트 실행 (`nxt_tick_engine.py`)<br/>• **Track B**: 대규모 멀티프로세싱 LOB 백테스트 (`run_parallel.py`) | `engine/` |
| **16:00 ~** | 대시보드 서빙 & 아카이빙 | • Streamlit 대시보드 리포트 생성 및 Ollama LLM 해석<br/>• 30일 경과 원본 틱 데이터 ZSTD 압축 후 Cold HDD 이전 | `dashboard/`<br/>`archive_worker.py` |

---

## 3. 📑 단계별 데이터 계약 (Data Contracts & Pipeline Stages)

### Stage 1: 원천 데이터 수집 (Raw Ingestion Lake)
* **목적**: 장중 통신 렉 및 패킷 유실을 원천 차단하기 위해 무가공(Raw) 상태로 고속 적재
* **입력**: Kiwoom OpenAPI+ 실시간 패킷 (`주식체결`, `주식호가잔량`)
* **저장 위치**: `sampledata/raw_ticks/{YYYYMMDD}_raw.db` (SQLite WAL 모드)
* **스키마 명세**:
  ```sql
  -- 체결 이벤트 (Trade Event)
  CREATE TABLE raw_trades (
      t_time  TEXT,     -- 체결 시각 (HHMMSS)
      code    TEXT,     -- 6자리 종목코드
      price   REAL,     -- 체결 가격 (원)
      vol     INTEGER,  -- 체결 수량 (주)
      is_buy  INTEGER   -- 1: 시장가 매수(Ask 체결), 0: 시장가 매도(Bid 체결)
  );

  -- 10단계 호가 이벤트 (Quote Event)
  CREATE TABLE raw_quotes (
      q_time  TEXT,     -- 호가 접수 시각 (HHMMSS)
      code    TEXT,     -- 6자리 종목코드
      offer_p TEXT,     -- 매도1~10호가 (쉼표 구분: "78800,78900,...")
      offer_v TEXT,     -- 매도1~10잔량 (쉼표 구분: "1200,3400,...")
      bid_p   TEXT,     -- 매수1~10호가 (쉼표 구분: "78700,78600,...")
      bid_v   TEXT      -- 매수1~10잔량 (쉼표 구분: "5000,2100,...")
  );
  ```

---

### Stage 2: 데이터 정제 및 리샘플링 (LOB Transformation ETL)
* **목적**: 체결과 10단계 호가를 결합하여 타임라인 기반 백테스트가 가능한 표준 1초봉 테이블 구축
* **입력**: Stage 1의 `raw_trades`, `raw_quotes`
* **출력**: `sampledata/temp/{YYYYMMDD}_LOB.db` (종목별 51개 컬럼 개별 테이블)
* **처리 규칙**:
  1. 1초 동안 발생한 `raw_trades`를 그룹화하여 시/고/저/종, 거래량, 매수/매도체결량, 틱수 집계
  2. 해당 1초 순간 이하의 가장 최신 `raw_quotes` 10호가/잔량 40개 컬럼을 조인(As-of Join)
  3. 컬럼 구성 (총 51개): `time`(0), `OHLC`(1~4), `Vols`(5~7), `Ticks`(8~10), `Offer_P1~10`(11~20), `Offer_V1~10`(21~30), `Bid_P1~10`(31~40), `Bid_V1~10`(41~50)

---

### Stage 3: 거시 일봉 매트릭스 갱신 (Macro Matrix Lake)
* **목적**: 일봉 수준의 시총, 거래대금, 유통비율 사전 필터링 데이터 공급
* **입력**: 네이버 금융 fchart API (XML)
* **출력**: `sampledata/Daily/*.csv` (8대 매트릭스 파일, CP949 인코딩)
* **파일 구성**: `open`, `high`, `low`, `close`, `tradamt`(억원), `mkt`(억원), `shares`(주), `float`(%)
* **규격**: 행=날짜(`YYYYMMDD`), 열=`A+6자리코드`, 1행=종목명(`Name`)

---

### Stage 4: 퀀트 이벤트 백테스트 엔진 (Quantitative Execution)
* **엔진 분파**:
  1. **초정밀 틱 엔진 (`nxt_tick_engine.py`)**:
     - **NXT 과열 방어**: 08:00~08:50 수익률 $\ge +5\%$, 거래량 $\ge 5$만 주 종목은 09:00 정규장 돌파 매수 제외
     - **체결 렉 시뮬레이션**: 시그널 발생 시각 $+1$초 시점의 실제 `ask_p1`으로 체결 (실전 슬리피지 강제 주입)
     - **3대 트레일링 컷 동시 시뮬레이션**: 고정 익절 / 2틱 반락 컷 / 본전보존 계단식 컷 병렬 성적표 산출
  2. **병렬 1초봉 엔진 (`run_parallel.py` + `engine.py`)**:
     - 날짜 분할 멀티프로세싱 CPU 가속 기반 1초봉 LOB 시계열 전략 탐색

---

### Stage 5: 서빙 및 시각화 (Serving & Downstream)
* **결과 리포트**: `results/final_total_result.csv` (PnL, MDD, TPI, 수수료 차감 후 Net 순익)
* **인터랙티브 웹**: Streamlit 대시보드 (`dashboard/app.py`)를 통해 일별 누적 PnL 및 변수 민감도 분석
* **로컬 AI 분석**: Ollama(llama3.2) 연동을 통한 규칙 기반 전략 해석 및 자동 인사이트 리포트 생성

---

## 4. 🛡️ 장애 대응 및 아키텍처 원칙 (Architecture Decision Records)

1. **32비트 / 64비트 격리 원칙**:
   - 키움 Open API(32비트 ActiveX)와 메인 퀀트 시스템(64비트 Python 3.14/uv)의 의존성 충돌을 방지하기 위해 파일 I/O 기반으로 프로세스 완전 격리.
2. **선택 편향(Selection Bias) 배제**:
   - 아침에 종목을 임의 선별하지 않고 2,550개 보통주를 전수 수집하여 데이터 레이크의 순수성 보장.
3. **노이즈 및 스토리지 최적화**:
   - LP 기계 호가로 인해 DB 용량의 40%를 낭비시키는 ETF/ETN/스팩/우선주를 수집단에서 필터링하여 일일 용량을 2~3GB로 최적화.
4. **핫/콜드 계층화 스토리지 (Tiered Storage)**:
   - 최근 30일 활성 데이터는 2TB SSD에서 비압축으로 초고속 서빙.
   - 30일 경과 데이터는 2TB HDD에 Zstandard/Parquet 포맷으로 압축하여 5년 이상 영구 보존.
