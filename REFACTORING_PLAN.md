# 📌 데이터 수집·리샘플링 파이프라인 리팩토링 기록 (ADR) — L0/L1 범위

> **범위 안내**: 이 문서는 L0(수집)·L1(표준화/리샘플링) 계층에 한정된 의사결정 기록(ADR)이다.
> **현재 문서 시작점은 [README.md](README.md), 최신 상태·다음 작업은 [HANDOFF.md](HANDOFF.md)의 현재 인계다.**
> 틱 주 경로는 [ARCHITECTURE_TICK.md](ARCHITECTURE_TICK.md), 운영 제어는 [CONTROL_TOWER.md](CONTROL_TOWER.md)를 따른다.
> L2~L6의 이전 설계는 [ARCHITECTURE_V2.md](ARCHITECTURE_V2.md)를 참고한다. 이 문서의 과거 완료 판정을 현재 검증으로 읽지 않는다.
> 아래 §4 체크리스트와 §5 핸드오버는 Architecture V2 착수 이전(~2026-08 말) 시점에 고정된 내용이며,
> §5는 이미 완료/대체되어 이력으로만 남겨둔다.

## 1. 개요 및 목적
- **과거 문제**: 단일 스크립트(`legacy_engine.py`)에 데이터 수집, DB 로딩, 전략, 리스크 제어가 결합되어 하드코딩된 로컬 경로와 단일 프로세스 병목 발생. 1초봉 백테스트 결과와 실전 체결 간의 슬리피지 괴리 심화.
- **개선 목표**:
  1. 단일 책임 원칙(SRP) 기반의 독립 모듈화 및 CPU 멀티프로세싱 가속.
  2. 선택 편향(Selection Bias) 없는 코스피/코스닥 전 종목 실시간 틱/호가 원천 데이터 레이크 구축.
  3. 1초봉의 한계를 극복하는 틱 단위 미시구조(Order Book Microstructure) 이벤트 백테스트 엔진 구축.
  4. 대체거래소(Nextrade, 08:00) 출범에 따른 갭 왜곡 및 설거지 음봉 방어 로직 정립.

---

## 2. 해결 완료된 문제 및 기술적 의사결정 (ADR)

### ✅ Issue 1. 레거시 스파게티 코드 모듈 분리
- **해결**: 단일 1,000줄 코드를 `engine/`, `config.py`, `data_loader.py`, `strategy.py`, `risk_manager.py`, `utils.py`로 분리.
- **효과**: 기능별 유닛 테스트 가능 및 유지보수 용이성 확보.

### ✅ Issue 2. 대용량 데이터 처리 속도 개선 (CPU 병렬화)
- **해결**: `multiprocessing.Pool` 기반의 `run_parallel.py`를 도입하여 날짜 구간을 분할(`DEFAULT_SPLIT`) 처리 후 자동 CSV 병합. 백테스트 수행 속도 약 80% 이상 단축.

### ✅ Issue 3. 데이터 수집 라이브러리 404 및 로그인 차단 우회
- **문제**: `pykrx`의 로그인 강제화 및 `FinanceDataReader`의 상장사 캐시 URL 404 에러.
- **해결**: 외부 라이브러리 의존성을 제거하고 **네이버 금융 fchart API(XML) 직결 수집기(`daily_collector.py`)** 구현. 0.05초 만에 1,074영업일의 8대 일봉 매트릭스 CSV 생성 완료.

### ✅ Issue 4. 고빈도(HF) 2단계 데이터 파이프라인 정착
- **문제**: 장중 실시간으로 1초봉(51개 컬럼)을 조립하면 장초반 틱 폭주 시 웹소켓 지연(Lag) 및 패킷 유실 발생 위험.
- **해결 (2-Step Architecture)**:
  - **Stage 1**: 장중에는 체결/호가 데이터를 SQLite WAL 모드로 무가공 초고속 Append.
  - **Stage 2**: 장 마감 후 일괄 리샘플링하여 51개 컬럼 `{YYYYMMDD}_LOB.db` 생성.

### ✅ Issue 5. 선택 편향 극복: 키움 Open API+ 32비트 전 종목 수집기 채택
- **문제**: 한투 KIS 무료 웹소켓의 세션당 40종목 제한으로 인해 사전 선별 수집 시 선택 편향(Selection Bias) 및 장중 급등주 누락 발생.
- **해결**:
  - `uv venv .venv32 --python cpython-3.10.11-windows-i686-none` 기반 32비트 격리 환경 구축.
  - 키움 Open API+ 화면번호 26개 분할 매핑을 통해 코스피/코스닥 보통주(~2,550개) 전 종목 동시 실시간 구독.
  - 불필요한 기계 호가 트래픽을 유발하는 ETF, ETN, 스팩, 우선주를 자동 배제하여 DB 용량 50% 절감.
  - 생산자-소비자 큐(`queue.Queue`)와 백그라운드 DB 스레드로 **18만 건 수집 중 대기 큐 85개 유지 (유실률 0%)** 달성.

### ✅ Issue 6. 실전 틱 체결 방향(is_buy) 데이터 정합성 버그 해결
- **문제**: DBeaver 검증 결과 모든 체결 틱이 `is_buy = 0(매도)`으로 적재되는 현상 발견 (키움 FID 14 빈 문자열 반환 문제).
- **해결**: **호가 비교 알고리즘(Lee-Ready)**을 도입하여 체결가 $\ge$ 최우선 매도호가(`ask_p1`) 조건을 결합한 2중 방어 판정 구현. 매도 58% vs 매수 42%의 정상 시장 분포 복구.

### ✅ Issue 7. 실전 통신 렉(Latency) 및 Nextrade(NXT) 미시구조 엔진 구축
- **문제**: 다음 틱($t+1$) 즉시 체결 가정은 실전 50~100ms 통신 렉과 호가 붕괴를 반영하지 못함. 또한 08:00 NXT 프리마켓 과열 후 09:00 개장 시 설거지 음봉 발생.
- **해결 (`nxt_tick_engine.py`)**:
  - **NXT 과열 필터**: 08:00~08:50 거래량/상승률 과열 종목은 09:00 정규장 돌파 매수 금지.
  - **1초 지연 체결**: 시그널 발생 1초 뒤의 실제 호가창 `ask_p1`으로 체결하여 가혹한 스트레스 테스트 수행.
  - **3대 트레일링 컷 동시 시뮬레이션**: 고정 익절, 2틱 반락 트레일링 컷, 본전 보존형 계단식 컷의 성과를 병렬 비교.

> **주의**: 이 항목의 3대 트레일링 컷은 이후 `ARCHITECTURE_V2.md` §4에서 `ExitRule` 3종으로 정식화되었고,
> §L5 런 스토어 구현 이후 실제 비교 결과가 `runs/*/manifest.json`에 남는다. 최신 상태는 그쪽을 볼 것.

---

## 3. 데이터 스키마 명세

> 이 스키마는 (구) `PIPELINE_DESIGN.md`(저장소 정리 과정에서 삭제됨)에도 동일 내용이 있었다.
> 현재 이 문서가 L0/L1 스키마의 유일한 서면 기록이므로, 스키마 변경 시 반드시 여기를 갱신할 것.

### 3.1 51개 컬럼 LOB 스키마 (`*_LOB.db`)
* **0**: `time` (HHMMSS)
* **1 ~ 4**: `open`, `high`, `low`, `close`
* **5 ~ 7**: `vol`, `buy_vol`, `sell_vol`
* **8 ~ 10**: `tick`, `buy_tick`, `sell_tick`
* **11 ~ 20**: `offer_p1` ~ `offer_p10` (매도호가 1~10단계)
* **21 ~ 30**: `offer_v1` ~ `offer_v10` (매도호가 잔량 1~10단계)
* **31 ~ 40**: `bid_p1` ~ `bid_p10` (매수호가 1~10단계)
* **41 ~ 50**: `bid_v1` ~ `bid_v10` (매수호가 잔량 1~10단계)

### 3.2 Raw 틱 스키마 (`*_raw.db`)
* **`raw_trades`**: `(t_time TEXT, code TEXT, price REAL, vol INTEGER, is_buy INTEGER)`
* **`raw_quotes`**: `(q_time TEXT, code TEXT, offer_p TEXT, offer_v TEXT, bid_p TEXT, bid_v TEXT)`

---

## 4. 진행 현황 체크리스트 — L0/L1 범위, 동결 (Progress, frozen)

> 이 체크리스트는 L0/L1(수집·리샘플링) 범위에서 더 이상 갱신하지 않는다.
> L2 이상(피처/전략/실행/런스토어/대시보드) 진행 상황은 `ARCHITECTURE_V2.md`의
> "진행 상황 로그"와 §8 로드맵 표를 볼 것 — 마지막 두 항목도 그쪽 Phase E/Phase 4 논의로 이어진다.

- [x] 로컬 절대 경로 ➔ 상대 경로 자동 계산 구조 개편
- [x] CP949 / UTF-8 크로스 플랫폼 인코딩 정합성 확보
- [x] 일봉 8대 매트릭스 CSV 생성기 구현 (`daily_collector.py`)
- [x] 1초봉 51개 컬럼 LOB 생성기 구현 (`build_lob_db.py`)
- [x] 키움 Open API+ 32비트 격리 가상환경 및 원클릭 복구 스크립트 완비 (`setup_kiwoom.ps1`)
- [x] 코스피/코스닥 보통주 2,550개 전 종목 실시간 틱/호가 비동기 큐 수집기 검증 완료 (`kiwoom_universe_logger.py`)
- [x] 틱 단위 매수/매도 실시간 판정 2중 방어 알고리즘 검증 완료
- [x] Nextrade 프리마켓 방어 및 1초 지연 체결 틱 백테스트 엔진 구현 (`nxt_tick_engine.py`)
- [x] 3대 트레일링 컷(고정, 2틱 반락, 계단식 본전보존) 동시 비교 시뮬레이터 구축
- [x] Streamlit 대시보드 및 로컬 Ollama AI 연동 (`dashboard/`)
- [ ] 홈 PC 원격 무인 수집 환경(크롬 원격 데스크톱) 정착 — README.md Phase 4
- [ ] 실시간 잔고 조회 및 SOR 최선집행 주문 어댑터 구현 (`trader/broker_adapter.py`) — ARCHITECTURE_V2.md Phase E

---

## 5. (이력) Architecture V2 착수 이전 핸드오버 — 대체됨

> **이 섹션은 retire되었다.** 아래 내용은 2026-09-11 시점의 스냅샷이며, 그 이후 실제로는
> Architecture V2 설계 → Phase A(런 스토어) → Phase B(피처 스토어) 순으로 진행되었다.
> "지금 뭘 해야 하나"는 더 이상 여기서 찾지 말고 `ARCHITECTURE_V2.md`의 진행 상황 로그를 볼 것.
> 아래는 그 판단이 왜 바뀌었는지 추적할 수 있도록 원문 그대로 남겨둔 이력이다.

<details>
<summary>2026-09-11 시점 원문 (펼치기)</summary>

* **직전 완료 사항**:
  - 키움증권 32비트 전 종목 보통주(~2,550개) 수집 엔진(`kiwoom_universe_logger.py`) 실전 테스트 검증 완료 (108초간 18만 건 무유실 적재).
  - DBeaver 쿼리를 통해 매도 58% vs 매수 42%의 정상 틱 데이터 정합성 확인.
  - 호가 스프레드, 1초 통신 렉, NXT 프리마켓 과열 방어, 3대 트레일링 컷을 통합한 `engine/nxt_tick_engine.py` 구축 완료.
* **지금 즉시 실행할 태스크 (Next Action)**:
  1. 집 컴퓨터에 크롬 원격 데스크톱 설치 및 `.\setup_kiwoom.ps1`을 통한 3분 원클릭 환경 복구.
  2. 다음 영업일(08:00~15:35) 동안 집 컴퓨터에서 `kiwoom_universe_logger.py` 풀타임 가동하여 대규모 전 종목 틱 데이터 레이크 적재.
  3. 적재된 하루치 전체 틱 데이터를 바탕으로 `nxt_tick_engine.py`를 가동하여 3대 트레일링 컷 중 최종 승자 룰 확정 및 전략 최적화.

</details>

**실제로 대체된 경로**: 위 1~3번(홈PC 무인 가동)은 아직 실행되지 않았다. 대신 `nxt_tick_engine.py`가
샘플 데이터(20260911, 종목 053260)로 런 스토어에 결과를 남기는 형태로 §Issue 7의 청산 룰 비교가
`ARCHITECTURE_V2.md`의 Phase A 작업을 통해 먼저 구조화되었다. 홈PC 무인 가동 자체는 여전히 미착수 —
`ARCHITECTURE_V2.md` 진행 로그와 README.md Phase 4를 참조할 것.
