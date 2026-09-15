# 인수인계 노트 (2026-09-15 밤 갱신 — 새 세션/컨텍스트로 넘어갈 때 먼저 읽을 것)

## 지금 상태 — rev.2 개정지시서 실행 결과

2026-09-15 낮에 작성된 "2-a 개정 지시서(rev.2)"가 지목한 4개 항목(§1·§2'·§6·§5,
pykrx 의존성 0)을 전부 실행하고 실측/테스트로 검증했다. 아래 §2·§3-a·§3-b·2-b는
rev.2 가 "사용자가 셸 밖에서 직접 확인"하라고 못박은 3가지에 걸려 있어 손대지
않았다(§5 절 참조).

### §1 — 회귀 기준선 백업 + 고정 ✅ (완결, LOB 복구로 격상)

- `sampledata/Daily/*.csv` → `sampledata/Daily_baseline/`에 냉동 백업 (git 추적).
- `scripts/verify_phase_a.py`가 `engine.data_loader.CSV_PATH`/`SEC_PATH`를
  이 백업(+ 아래 LOB)으로 monkeypatch 하도록 고정 — daily_collector 가
  `sampledata/Daily`를 다시 덮어써도 회귀 검증 입력은 안 바뀐다.
- **검증 도중 2022년 8일 샘플(20220425~20220504)의 LOB DB
  (`sampledata/temp/*_LOB.db`)가 이 머신에 없다는 걸 발견했다** — `temp/`는
  최근 실전 수집분(20260914 등)만 남기고 사라져 있었다. 사용자가 보관하던
  백업 zip을 `sampledata/old_data/`로 풀어줘서 그 안의 LOB DB 8개를 복구,
  `scripts/verify_phase_a.py`가 `sampledata/old_data/temp/`도 함께 고정하도록
  연결했다.
- 결과: **회귀 기준선(거래 5건, 누적 -1.301%)이 거래 단위로 완전히 재현됨**을
  확인(`VERIFY_CODES=(000270,005930,000660)`, `split=1`,
  `feature_source=inline` — 2022년 8일치 fs_v1 parquet 도 함께 사라져 있어
  인라인 경로로 재현. B-1 때 이미 인라인↔스토어 동치가 확인된 조합이라 문제
  없음).
- `sampledata/old_data/`는 5GB 규모라 `.gitignore`에 추가해 git 추적 대상에서
  뺐다 — 로컬에만 보존.
- 남은 흠: `verify_phase_a.py`의 틱 엔진 검증부(`--date 20260911`)는
  `sampledata/raw_ticks/20260911_raw.db` 부재로 여전히 실패한다. rev.2 범위
  밖의 별개 이슈라 손대지 않았다.

### §2' — daily_collector 스텁 제거 ✅

`collector/daily_collector.py`:

- `float 60.0` 상수, `shares 1억주` 폴백 제거 → 실패 사유(`*_reason`)를 남기고
  값은 `None`/NaN.
- **shares broadcast 버그 제거**(발견 #0-3): 네이버에서 스크랩한 "지금 이 순간"
  상장주식수를 전 거래일(2022~오늘)에 그대로 채우던 것을 **수집 시점(최신
  거래일)에만** 반영하도록 수정. `mkt`도 그 시점에만 계산, 나머지는 NaN.
- 실행마다 `sampledata/Daily/_meta_manifest.json`에 종목별 결측 사유를 남김.
- **부수 발견**: 네이버 상장주식수 페이지 스크랩 정규식이 지금 항상
  `page_pattern_not_found`로 실패한다 — 사이트 구조가 바뀐 듯. §3-a(키움
  `opt10001`로 전환)의 필요성을 실측으로 뒷받침한다.

### §6 — 큐 경보 재보정 ✅ (실측 백테스트로 완결)

- `scripts/backtest_queue_alerts.py` 신규 — 세션 로그(`체결/호가/대기큐` 상태줄)를
  파싱해 `SessionMonitor`에 그대로 재생시키는 재사용 가능한 도구.
- `logs/kiwoom_universe_20260915.log`(그날 하루 전체, 09:16:55 대기큐 최대
  203,692건 포함)로 돌린 결과 **경보 0건** — 현재 임계값(바닥 20,000건/추세창
  300초/침묵 120초)이 개장 폭주 같은 정상 패턴에 오작동하지 않음을 실측으로
  확인. (합성 드라이런은 이미 `tests/test_kiwoom_session_monitor.py`에 있음 —
  민감도는 그쪽, 특이도는 이 스크립트.)

### §5 — tidy 스냅샷 스키마 (D-5) ✅

- `collector/daily_snapshot.py` 신규: `mkt`/`shares`/`float`를 더 이상 wide
  CSV에 직접 쓰지 않는다. 수집 시점 관측값을 날짜별 tidy 파일
  (`sampledata/Daily/snapshots/YYYYMMDD.csv`)에 저장하고, 누적된 스냅샷
  전체를 피벗해 `mkt.csv`/`shares.csv`/`float.csv`를 **파생**시킨다
  (`pivot_table(..., dropna=False)` — 전부 NaN인 종목도 컬럼 자체는 유지).
  스냅샷이 없는 날짜는 구조적으로 NaN이라 broadcast 버그가 재발할 경로가
  없다.
- `open`/`high`/`low`/`close`/`tradamt`는 기존 방식(네이버 fchart 다년치
  재수집 → wide CSV 직접 저장) 그대로 — 매일 다시 받아도 그날그날의 실제
  시세라 시점 문제가 없다.
- **⚠️ 다음 실전 수집 때 벌어질 일**: `mkt.csv`/`shares.csv`/`float.csv`가
  지금의 "2022~2026 전체 이력(단, shares broadcast로 오염된)"에서 **오늘부터
  하루씩 쌓이는 얇은 파일**로 바뀐다. rev.2가 명시적으로 승인한
  트레이드오프("그럴듯한 상수보다 정직한 NaN")이지만 눈에 띄는 변화이므로
  실전 배포 전에 인지하고 있을 것. 회귀 기준선은 `Daily_baseline/`에 얼려둬서
  이 변화와 무관하다.
- D-6(수정주가 아닌 실제가 저장)은 코드 변경 불필요 — [0-1] 재조사 결과
  네이버 소스가 이미 실제가를 준다는 게 확인됐다(SK하이닉스는 분할 이력 자체가
  없음, "시뮬레이션 데이터" 판정은 전제 오류로 철회됨). 정책만 문서화하면 됨.

## 대기 중 — 사용자 확인 필요 (rev.2 §5, 에이전트 셸 밖)

아래 3가지가 있어야 다음 단계(§2 유니버스 하드코딩 해제, §3-a 키움 연동,
§3-b 과거 백필, 2-b float 소스 확정)를 진행할 수 있다.

1. **pykrx 차단 원인 분리** — 일반 PowerShell(Claude Code 밖)에서
   `stock.get_market_cap_by_date('20180420','20180515','005930')` 실행 →
   상장주식수가 분할 전후 50배 뛰면 에이전트 샌드박스 프록시 문제(프로덕션은
   무사), `LOGOUT`/빈 결과면 pykrx 자체를 버리고 공공 API 로.
2. **키움 `opt10001` 출력 필드** — KOA Studio에서 `상장주식`/`유통주식`/
   `유통비율` 유무 확인. 전자는 §3-a(shares 앞으로 수집)를, 후자는 2-b
   (float_ratio 소스)를 결정한다.
3. **네이버 fchart 50종목 실측** — 소요시간/실패율/응답 지연. §2(유니버스
   3종목→3,754종목 확대) 전 필수 — 지금 구조로는 하루 3,750회 요청이 된다.

## 환경 주의사항

- 체크아웃 2개: 학원(`C:\Projects\03_Personal_Quant\Stock`), 집(`C:\Projects\Stock`). **GitHub로만 동기화됨** — Cowork 세션은 한 번에 컴퓨터 1대만 연결되고, 컴퓨터를 바꾸면 이전 연결은 사라짐(추가되는 게 아니라 대체됨).
- 컴퓨터 옮기기 전 항상: 현재 컴퓨터에서 git commit + push → 도착 후 git pull.
- 폴더 연결은 세션마다 새로 필요할 수 있음(데스크톱 앱에서 폴더 재연결).
- 실데이터(raw_ticks/*.db, temp/*.db, features/*.parquet)는 .gitignore 대상이라 git으로 안 따라감 — 집컴에만 존재.
- `sampledata/old_data/`(2022 샘플 LOB DB + 옛 Daily 매트릭스, 5GB)도 이제
  `.gitignore` 대상 — 삭제하지 말 것, §1 회귀 검증의 유일한 LOB 출처다.

## 우선순위

`... → B-5 ✅ → [오늘 수집 검증] → rev.1 대기 작업 ①② ✅(rev.2 로 대체) → rev.2 §1·§2'·§6·§5 ✅ → [사용자 확인 1/2/3] → §2·§3-a·§3-b·2-b → C(전략 추상화)`
