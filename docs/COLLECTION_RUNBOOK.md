# 데이터 수집 실행 안내

[시작점](../README.md) · [운영 상태와 남은 작업](../HANDOFF.md) · [틱 설계](../ARCHITECTURE_TICK.md)

기존 README의 현재 수집·메타데이터 절차를 분리한 문서다. 아래 모든 명령은 저장소 루트에서 실행한다.
수집 중에는 재시작·추가 OCX 로그인·전체 DB 조회·변환·실제 재생을 하지 않는다.
실행 중인 코드 버전과 현재 상태를 먼저 확인한다. 기록된 관측값은 현재 상태가 아니다.

## 환경과 수집 시작

64비트 분석/운영 화면은 `.venv`, 키움 OCX는 `.venv32`를 쓴다.
환경 설치·변경은 수집 세션 밖에서 수행한다. 시작 전 변경을 커밋하고 중복 수집 여부를 확인한다.

```powershell
# 장외: 최초 환경 준비가 필요한 경우
uv sync
.\setup_kiwoom.ps1

# 새 수집 세션 시작이 허용된 때만
.\.venv32\Scripts\python.exe collector/kiwoom/kiwoom_universe_logger.py
```

사용자의 운영 적용 요청에 따라 위 진입점의 기본 저장 방식을 **raw v2**로 변경했다.
변경 후 새로 실행한 세션부터 적용되며 실행 중인 프로세스를 교체하지 않는다.
`sampledata/raw_ticks_v2/YYYYMMDD/session_id.db`에 새 파일을 만들고 기존 raw v1 파일은 열지 않는다.
`--storage raw-v1`을 지정한 경우에만 예전 날짜별 저장 경로를 사용한다.
운영 화면에서 1~10종목·1~300초의 관리 세션을 시작하고 종료를 요청할 수 있다.
선택한 mock/live 서버와 실제 로그인 응답이 다르면 중단한다. 화면 밖 기존 세션은 인수하지 않는다.
관리 요청·수락·최종 보고는 별도 SQLite에 보존한다. 미수락 기동 취소는 뒤늦은 자식의 시작 권한을 폐기한다.
프로세스 중단 후에는 **종료된 관리 프로세스 이력 대조**로 종료 보고를 대조한다.
실행 중/접근 거부 상태를 종료로 판정하지 않으며 종료 요청을 재전송하지 않는다.

```powershell
# 로그인/OCX 객체 생성 없이 32비트 환경과 OCX 등록만 확인
.\.venv32\Scripts\python.exe collector/kiwoom/kiwoom_universe_logger.py --preflight

# OCX 없이 운영 백엔드에 합성 콜백 20건을 넣어 저장/종료/체크섬 확인
.\.venv32\Scripts\python.exe scripts/probe_live_capture.py

# 실피드 최초 확인용: 중복 접속이 없을 때 한 종목, 최대 60초
.\.venv32\Scripts\python.exe collector/kiwoom/kiwoom_universe_logger.py --codes 005930 --duration-seconds 60
```

- 기본 실행은 기존 유니버스와 15:35 종료 조건을 사용한다. 제한 시간 옵션은 1~10종목 명시 시에만
  1~300초로 허용한다. 장외 제한 실행도 실제 로그인이며 체결이 없으면 실피드 검증이 되지 않는다.
- CLI는 OCX 생성 전 같은 체크아웃의 단일 수집 잠금을 획득한다. 남은 lock 파일 자체로 생존을
  판정하지 않는다. 다른 앱/체크아웃의 브로커 로그인 공존까지 보장하는 잠금은 아니다.
- 로그인 후 실제 서버 응답이 0/1인지 확인하고 미확인은 중단한다. 콜백 진입 시 단조/UTC 시각을
  잡고 원문 FID를 Qt 스레드에서 추출한다. 누적 거래대금 FID 14로 방향을 추정하지 않는다.
  방향과 venue는 unknown, 가격 부호 처리는 명시된 signed_magnitude 정책이며 원문은 그대로 남는다.
- 큐 8,192콜백·워커 배치 최대 512콜백으로 저장한다. 큐 초과·FID 조회 예외·연결 단절·구독 거부는
  중단하고 정상 완료로 기록하지 않는다. 파싱 불가 값은 원문과 품질 오류 레코드로 남긴다.
- `operations_state/capture_sessions/session_id/`에 보고 저널·상태를 남긴다.
  `operations_state/capture_status.json`은 약 5초마다 갱신하는 최신 관측 사본이다.
  운영 화면은 최대 64 KiB의 이 파일만 읽으며 raw DB를 스캔하지 않는다.
- 2026-09-16 19:28~19:30 KST 실제 OCX **mock** 로그인·삼성전자 1종목 구독·구독 후 60초 자동 종료를
  확인했다. 체결/호가 0건이며 raw session_start 1건의 체크섬·closed 보고·잔여 큐 0을 대조했다.
  저장 종료는 확인했지만 실피드 의미·전 종목 부하·지연·공급자 무누락 검증은 아니다. 근거는 HANDOFF에 있다.
  기존 큐 단독 점검 `scripts/probe_raw_v2_queue.py`도 계속 사용할 수 있다.
KIS `collector/run_daily_daemon.py`는 별도 프로그램으로 같은 날짜 raw 경로를 사용하므로
키움 후처리를 하려고 함께 실행하지 않는다. 현재 수집기는 메타데이터나 백테스트를 자동 실행하지 않는다.

## 수집 중 가벼운 확인

우선 컨트롤 타워의 수동 로그 관측을 사용한다. 추가로 `scripts/check_tick_collection.py`의
짧은 읽기 전용 관측을 사용할 수 있다. 전체 건수 집계·해시·인덱스 생성·변환은 장외 작업이다.
하트비트가 최근이라는 이유만으로 무누락·DB 저장 완료·매수 방향 정확성을 인증하지 않는다.

## 일봉 수집과 기준선

`collector/daily_collector.py`는 새 fchart 가격을 `unverified_fchart/`에 격리하고 당일
메타데이터 스냅샷을 만든다. 운영 Daily 실제가 공급자 연결은 미완료다. 현재 shares를 과거 날짜에
소급하지 않고 결측을 상수로 대체하지 않는다. `Daily_baseline`과 사용자 `old_data`를 보존한다.
전체 유니버스 확장은 아래 50종목 실측과 소스 정책을 확인한 뒤 진행한다.

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

- 기본 raw v2는 수신 중단 → 모든 콜백 정규화/커밋 → draining 보고 저장 → manifest 마감 및 파일 닫기
  → closed 보고 저장 순서다. 로컬 종료에는 원격 stop 수락 ID를 만들어 붙이지 않는다.
- `raw v2 저장 완료`와 세션 상태/보고를 대조한다. raw 레코드는 session_start/parse_error도 포함하므로
  콜백 건수와 다르다. 저장 완료는 데이터 품질 합격이 아니다. 최초 실피드의 전체 체크섬은 장외에 검증한다.
- 실패/제한 시간 초과에는 종료 코드 2와 진단을 남긴다. 아직 저장 워커가 살아 있으면 단일 수집 잠금을
  유지한 채 기다린다. 강제 종료·전원 장애 내구성은 별도 실측 대상이다.

아래는 `--storage raw-v1` 호환 모드의 종료 확인이다.

- Ctrl+C 또는 15:35 자동 종료 시 수신을 멈추고 대기큐와 저장 중인 배치를
  끝까지 커밋한 뒤 종료한다. `종료 후 미커밋: 0 건`과 `DB 저장 결과: 커밋 완료`를 확인한다.
- 저장 중에는 프로세스를 기다린다. 두 번째 Ctrl+C, 창 강제 닫기 또는 전원 장애는
  저장 완료를 보장하지 않는다. SQLite의 기존 `synchronous=OFF` 설정은 유지한다.
- DB 오류가 발생하면 수집을 중단하고 오류 및 미커밋 건수를 기록하며 종료 코드 2를
  반환한다. 미커밋 데이터는 재실행만으로 복구되지 않는다.
- 변경된 종료 처리는 다음 실행부터 적용된다. 변경 전부터 실행 중인 프로세스에는
  자동 반영되지 않으므로, 운영 적용 전에 변경분을 커밋한다.

### 종료 후 raw v1 저장 대조

종료 로그·프로세스 부재·짧은 DB 관측을 먼저 대조한다. 수집 종료를 확인한 뒤에만
`scripts/verify_tick_storage.py`로 전체 SQLite `quick_check(10)`와 정확한 행 수를 검사한다.
예상 건수는 해당 날짜/세션 로그에서 가져온다. 마지막 rowid를 실제 건수로 대신하지 않는다.

```powershell
# 2026-09-16 종료 로그의 건수. 다른 날짜는 DB와 예상 건수를 함께 바꾼다.
$checkStamp = Get-Date -Format yyyyMMdd_HHmmss
.\.venv\Scripts\python.exe scripts/verify_tick_storage.py --db sampledata/raw_ticks/20260916_raw.db --expected-trades 12989488 --expected-quotes 29704393 --off-hours --max-seconds 300 --output "operations_state/storage_checks/$checkStamp.json"
```

읽기 전용 연결·단일 스냅샷을 사용하며 원본 변경·체크포인트·인덱스 생성은 하지 않는다.
SQLite 진행 콜백으로 시간 제한을 검사한다. 오류/시간 초과, 건수 불일치, 검사 중 DB/WAL
변경은 통과로 처리하지 않는다. 결과 파일은 새 파일만 허용한다.
`passed`는 SQLite 구조 검사와 로그 건수 대조 통과다. 공급자 이벤트 무누락, 방향·venue 정확성,
전원 장애 내구성 또는 과거 프로세스의 최종 커밋 절차를 인증하지 않는다.

2026-09-16 실측: 16:15 KST 수집 Python 프로세스 부재와 15:35 종료 로그를 확인했다.
오늘 실행분에는 최신 종료 패치의 미커밋/저장 결과 표식이 없었다. 16:18:41부터 167.494초간
7,046,569,984바이트 DB를 검사해 `quick_check=ok`, 실제 체결 12,989,488건·호가 29,704,393건과
로그 건수의 일치를 확인했다. DB/WAL 변경 없음. 마지막 양쪽 기록 시각은 15:32:56이었다.
진단 JSON은 `operations_state/storage_checks/20260916_after_close.json`에 보존했다.

### 일봉 실제가 출처 검사 (D-6)

2026-09-16 18:32 KST, `scripts/probe_price_source.py`로 pykrx 1.0.51의
삼성전자 20180427~20180504 비수정 OHLCV/과거 shares 후보를 제한 요청했다.
상장/상폐 목록 2회는 HTTP 200, 뒤 가격 요청은 HTTP 400/`LOGOUT`으로 중단했다. shares 후속 요청도 보내지 않았다.
결과는 `operations_state/price_source_probes/79fd6005c32e416cac0289035bb8d55d/result.json`이며,
공급자 접근·원천 검증 미완료다. 이 진단은 최대 4요청·요청당 10초·응답당 2 MiB이고 Daily에 쓰지 않는다.

같은 진단의 목록 응답을 설치된 pykrx 호출 순서(상장→상폐)와 대조해 로컬 연구 유니버스와 비교했다.
2,221종목 중 상장 목록만 2,055, 상폐 목록만 166, 양쪽/어느 쪽에도 없는 경우는 각각 0이었다.
`listing_reconciliation.json`에 코드 목록과 응답 SHA256을 남겼다. 긴 상품 코드는 6자리로 잘라 합치지 않는다.
`scripts/reconcile_listing_snapshot.py --listed <응답> --delisted <응답> --output <새 진단.json>`으로 재현한다.
목록 포함 여부만 확인하며 거래 가능 여부·보통주 분류·과거 PIT를 인증하거나 유니버스에서 삭제하지 않는다.

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

#### 2026-09-16 장외 측정

- 16:16 KST, `agent_shell`, 로컬 유니버스 2,221종목에서 seed 0으로 50종목 선택.
  50회 요청 모두 HTTP/파싱 성공, 실패·재시도 0회, 전체 56.295초.
- 성공 요청 지연 중앙값 82.8515 ms, p95 110.466 ms. 첫 10건 중앙값 79.155 ms,
  마지막 10건 81.4205 ms. 이 표본만으로 동시 수집 부하나 전체 종목 안정성을 판단하지 않는다.
- 마지막 날짜는 48종목이 20260916, `299910`은 20250213, `101060`은 20220117이다.
  후속 KRX 공시 대조: [애닉(299910)](https://kind.krx.co.kr/external/2025/02/03/001423/20250203003643/70769.htm)은
  2025-02-14, [SBS미디어홀딩스(101060)](https://kind.krx.co.kr/external/2022/01/13/000449/20220113001380/68051.htm)는
  2022-01-18 상장폐지다. 응답 마지막 날짜는 각각 공시상 폐지 직전 날짜와 일치한다.
  해당 표본은 현재 상장 종목 전용 유니버스가 아니었다. 이는 2종목의 설명이며 전체 목록의 최신성을 인증하지 않는다.
  과거 연구용 종목/원본은 삭제하지 않는다. HTTP 성공과 요청 기간 유효 가격 수신을 분리한다.
- 일봉 수집 manifest/종목별 추기 기록에 요청 달력 대비 complete/partial/no_requested_rows/unknown_calendar를 남긴다.
  오래된 응답만 있는 종목을 가격 수집 성공으로 세지 않고, 당일 메타데이터 관측은 독립 보존한다.
- 원문 진단: `logs/fchart_probe/20260916_161620_525ca421/attempts.jsonl`, `summary.json`.
  Daily CSV는 수정하지 않았다. fchart의 실제가 출처(D-6) 차단은 해소되지 않았다.
