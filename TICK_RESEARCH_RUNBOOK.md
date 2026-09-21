# 틱 연구 실행 안내

[문서 인덱스](README.md) · [현재 인계](HANDOFF.md) · [첫 시험 체크리스트](BACKTEST_TODO.md)

## 현재 가능한 범위

`raw_v2_prototype_1` 또는 `raw_v2_prototype_2` 형식으로 종료된 파일을 읽어 종목/venue 하나, 청산 규칙 하나,
독립 계좌 하나를 재생하고 JSON 결과를 저장한다. 원본 이벤트를 초봉으로 집계하지 않는다.
새 운영 키움 실행은 raw v2를 기본으로 기록한다. 기존 raw v1에 공통 순번을
붙여 v2라고 취급하지 않는다. 실피드 필드·성능·정밀 재현 검증은 아직 남아 있다.

운영 화면의 작업 이력에서 저장한 계획에 **장외 검사·재생 실행**을 요청할 수 있다.
별도 워커가 평일 08:00~16:30 차단, 수집 잠금, 활성 관리 요청/최근 로그를 확인한다.
화면 실행은 32 MiB·100,000 raw 이내이며 전체 reader를 소진해 무결성을 검사한 후 연구를 실행한다.
그 선행 스캔만으로 연구 품질 적합성을 승인하는 것은 아니다. 연구 단계에서 품질 기록을 다시 거부한다.
더 큰 파일은 명시적인 장외 CLI 계획이 필요하다. 실행 중 끊긴 작업은 자동 재시도하지 않는다.
방향 미확인 등 품질 표식은 연구 실패로 남으며, 수집 파일을 닫았다는 이유로 이를 제거하지 않는다.

저장 계획은 작업 이력에서 1회 장외 예약도 가능하다. Windows 사용자가 로그인되어 있고 PC가 켜져 있어야
하며, 놓친 예약/불확실한 실행은 자동 재시도하지 않는다. 예약·취소·만료와 이력 보관은
[컨트롤 타워 §5-5](CONTROL_TOWER.md#5-5-상시-응답이력-유지예약접근-제어)를 본다.

### 직접 CLI와 선행 검사 구분

[직접 CLI](scripts/run_tick_research.py)는 헤더에서 source/session을 얻은 뒤
[run_raw_v2](engine/tick_research_run.py)를 호출한다. 이 경로는 읽으면서 선택 이벤트를 전략에 넘기며,
전체 checksum은 [reader](collector/raw_v2.py)의 iterator를 끝까지 소비했을 때 확정된다.
품질 오류로 조기 종료하면 전체 파일 검증 완료가 아니다. 끝에서 오류를 발견해도 런 전체는 failed/진단 전용이다.

직접 CLI는 **검사 전용 명령이 아니고**, 화면 워커의 시간·수집 lock·크기 상한도 자동 적용하지 않는다.
첫 시험에서 전략 실행 전에 입력을 합격시키려면 별도 장외 검사 절차부터 확정한다.
화면 제한을 초과하는 실제 raw를 아래 명령으로 실행해 놓고 "검사만 했다"고 보고하지 않는다.
준비 항목은 [BACKTEST_TODO §3](BACKTEST_TODO.md#3-장외-실행-전-준비)에 있다.

장중 개발 검증에는 작은 합성 파일만 사용한다. 실제 파일의 전체 스트림 검증·재생은 장외에 한다.
아래는 명령 형식 예시이며 수수료·지연·호가 나이의 검증된 운영 설정이 아니다.

```powershell
.venv/Scripts/python.exe scripts/run_tick_research.py `
  --db C:/path/to/closed_v2_example.db `
  --output-root research_runs `
  --code 005930 --venue unknown --quantity 2 --cash 100000 `
  --fee-rate 0.001 `
  --buy-latency-sec 1 --sell-latency-sec 1 --cancel-latency-sec 1 `
  --max-quote-age-sec 2 --cooldown-sec 10 --exit-rule fixed
```

- 비용률은 매수와 매도 각각에 적용하는 비율이다. 기존 왕복 비용을 양쪽에 중복 적용하지 않는다.
- 시간 옵션은 초 단위이며 내부에서는 정수 나노초로 바꾼다. 음수/NaN/무한대는 거부한다.
- 청산 규칙은 `fixed`, `tick_trail`, `step_trail` 중 하나다. 비교 시 각 실행이 별도 계좌다.
- `unknown`은 거래소를 모른다는 뜻이다. 시간대만으로 NXT를 인증하지 않는다.
- 입력 경로·출력 폴더·비용·지연은 명시해야 한다. 오늘 날짜 DB를 자동으로 고르지 않는다.
- 종목 하나만 선택해도 성공하려면 원본 전체 읽기와 순번·체크섬 검증이 완료돼야 한다.
  선택 종목 밖의 `session_start`/`session_note` 외 CaptureControl도 거부한다.
- 성공 시 새 UUID 폴더의 `result.json` 경로를 출력한다. 이전 결과를 덮어쓰지 않는다.

## 닫힌 raw의 제한된 표본 대조

전체 재생 전에 원문 FID와 저장된 정규화 이벤트를 작은 구간에서 대조할 때 사용한다.
`scripts/inspect_raw_v2_sample.py`는 DB를 자동 선택하지 않으며 원본을 읽기 전용으로 연다.
전략·주문·체결 재생은 실행하지 않는다. 이 절차는 전체 무결성 검사의 대체물이 아니다.

1. 대상 경로와 session_id를 명시하고, 같은 세션의 종료 보고·writer_closed·마무리 정보로
   쓰기 종료를 먼저 확인한다. 도구의 `state=closed` 검사는 manifest 주장만 확인하며
   프로세스 종료나 저장 완료를 독립적으로 인증하지 않는다.
2. 최초 100건부터 대조하고, 필요하면 명시적인 시작 순번으로 중간/말미 표본을 별도 조회한다.
   순번은 저장된 manifest의 건수 주장을 참고하되, 위치 선정에 전체 COUNT/스캔을 사용하지 않는다.
   종목 필터와 OFFSET 없이 공통 순번의 연속 구간을 읽는다.

```powershell
.venv/Scripts/python.exe scripts/inspect_raw_v2_sample.py `
  --db C:/path/to/closed_v2_example.db --start-seq 1 --limit 100
```

3. JSON의 요청 구간·실제 첫/끝 순번·읽은 건수·비교한 틱 수와 `manifest_claim.session_id`를
   확인한다. `mismatches`는 저장된 값과 현재 변환기의 재계산 값이 다른 필드를 표시한다.
   `quality_issues`는 방향 미확인·FID 오류·오류 제어 기록·지원하지 않는 변환 형식을 남긴다.
   불일치나 품질 오류가 있으면 확대 재생을 멈추고 해당 순번과 정책을 조사한다.
4. 결과를 보존할 때 원본 데이터 폴더와 분리된 새 파일을 사용한다. 자동 보정·원문 덮어쓰기는 하지 않는다.

- 한 번에 1~1,000건, manifest 64 KiB, 레코드당 payload 64 KiB, 합계 8 MiB로 제한한다.
  `INTEGER PRIMARY KEY` 범위 조회 계획이 아니면 거부한다. SQLite 진행 콜백과 행 처리에
  2초 예산을 적용하며 잠금 대기는 0.5초다. OS I/O 자체를 강제 중단하는 실시간 보장은 아니다.
- 동일 읽기 트랜잭션 안에서 헤더와 표본을 읽고, 구간 내 순번·시각 역행·세션·닫기 경계를 검사한다.
  구간 직전/직후 레코드는 읽지 않는다. 헤더가 주장한 끝보다 일찍 끊긴 표본도 오류로 처리한다.
- 원문에 저장된 가격·방향 정책을 그대로 사용한다. 정책 누락/오류를 최신 기본값으로 채우지 않는다.
  표본 안의 종목·venue·수신 identity를 재사용하므로 이 값들의 원천 정확성을 검증하는 것은 아니다.
- 종료 코드 0은 `sample_consistent`, 2는 `sample_issues` 또는 `empty_sample`, 3은
  입력/구조/예산 오류다. 코드 0이어도 `full_integrity_verified=false`,
  `feed_accuracy_verified=false`, `replay_performed=false`이며 연구 입력 승인이나 무누락 인증이 아니다.
  체크섬·표본 밖 품질·실제 거래소·전략 성과는 미확인으로 둔다.
- 보고서에는 확인 시각과 사용 코드 해시를 남긴다. 저장된 해시는 비교하지 않은 manifest 주장이고,
  원본 전체 해시를 새로 계산하지 않는다. 실제 하루 검증은 별도 장외 전체 검사·재생으로 진행한다.

2026-09-18 제한 적용에서는 시작/중간 표본이 일치했지만 말미에서 무부호 FID15 체결 8건과
그에 대응하는 방향 미확인 parse_error 8건을 확인했다. 재계산 일치가 연구 적합성을 뜻하지 않는
실제 사례다. 원본 오류 기록을 삭제하거나 무부호 값을 임의의 방향으로 채우지 않는다.
대상·판정은 [현재 체크리스트](BACKTEST_TODO.md), 상세 당시 근거는 [인계 보존본](docs/archive/README.md)을 본다.

## 닫힌 raw의 whole-file qualification

전략·주문·체결 시뮬레이션 없이 전체 raw-v2의 구조 무결성과 품질 적합성을 분리해 검사할 때
`scripts/qualify_raw_v2.py`를 사용한다. 운영자가 같은 session의 writer 종료·프로세스 종료 근거를
먼저 대조하고 그 근거 위치나 설명을 `--closure-evidence`에 남긴다. 이 문자열은 운영자 주장으로
기록될 뿐 종료를 자동 인증하지 않는다. `--expected-session-id`는 manifest와 반드시 일치해야 한다.

```powershell
.venv/Scripts/python.exe scripts/qualify_raw_v2.py `
  --db C:/path/to/closed_v2_example.db `
  --expected-session-id expected-session-id `
  --closure-evidence "status/journal/process closure checked separately" `
  --output-root C:/path/to/qualification_runs
```

검사 reader는 다음 조건을 모두 만족할 때만 `immutable=1`을 사용한다.

- Windows 로컬 고정 NTFS의 일반 파일이며 입력 경로와 모든 상위 경로가 reparse point가 아니다.
  검사 전에 resolve로 junction/symlink를 숨기지 않는다. `..` 경로도 거부한다.
- `-wal`, `-shm`, `-journal`이 하나도 없다. 크기 0인 sidecar도 자동 삭제하거나 무시하지 않는다.
- 기존·신규 write/delete handle을 막는 Windows 공유 잠금을 scan 전체 동안 유지한다.
- scan 전후 같은 파일 identity·크기·mtime, 열린 handle과 경로의 일치, sidecar 부재를 다시 확인한다.

운영자는 검사 중 상위 디렉터리의 이동·교체·junction 변경도 없어야 함을 보장해야 한다.
파일 공유 잠금은 상위 디렉터리 전체의 이름 공간 잠금이 아니다. 전후 검사만으로 임의의 동시 경로 교체까지
원천 차단했다고 해석하지 않는다. 외부 접근과 경로 변경을 배제할 수 없으면 실행하지 않는다.
조건이 모호하면 읽지 않고 실패한다. 일반 `read_raw_v2()`는 기존 호출자를 위한 SQLite read-only
snapshot reader이며 WAL 처리 중 sidecar가 생길 수 있으므로 원본 비변경 qualification 계약이 아니다.
현재 sidecar가 있는 운영 raw는 별도 보존·해결 절차가 합성 검증되기 전까지 이 도구의 입력이 될 수 없다.

결과는 새 UUID 폴더의 `result.json`에만 생성한다. `stream_integrity_verified`는 manifest/session,
공통 seq, 수신 시각, 닫기 경계, event count, payload checksum, iterator 전체 소진이 모두 확인된 경우에만
참이다. 구조적으로 정상인 `parse_error`와 normalized issue는 scan을 중단하지 않고 control type·reason·
종목·수신 UTC 5분 구간·고정 상한 예시로 집계한다. 대응 tick의 issue와 바로 뒤 `parse_error`는 원시
레코드 수는 각각 보존하되 `logical_issue_counts`에서 두 개의 독립 체결 오류로 중복 계산하지 않는다.
사유가 누락/null/빈 목록인 `parse_error`도 `unspecified_parse_error`로 집계하며 연구 합격으로 통과시키지 않는다.
추가 보호 회귀는 [경계 테스트](tests/test_raw_v2_qualification_boundaries.py)를 따른다.

종료 코드 0은 구조 무결성과 현행 연구 품질 계약이 모두 합격, 2는 전체 구조 검증은 완료했지만 품질상
연구 부적합, 3은 입력/보호 조건/구조 검증 실패다. `stream_integrity_verified=true`와
`research_eligible=false`는 정상적인 진단 결과다. 어느 경우에도 공급자 무누락·venue·원천 정확성·
전략 성과를 인증하지 않으며 DB 본체 파일 바이트 hash를 새로 계산하지 않는다.

### 잔여 sidecar의 합성 검증 계획

**설계 단계이며 운영 처리 승인이 아니다.** 운영 raw·sidecar는 그대로 둔다. 아래 실험은 새로 만든
작은 Windows NTFS fixture에만 적용한다. 일반 입력 경로를 받는 운영 cleanup 도구를 먼저 만들지 않는다.

SQLite 공식 [WAL 계약](https://www.sqlite.org/wal.html#the_wal_file)은 WAL을 DB 상태의 일부로 다루며,
수동 삭제 대신 SQLite의 open/close 생명주기를 통한 정리를 안내한다. 따라서 우선 비교할 후보는
**SQLite가 정상 연결·명시적 close 과정에서 자체 정리하는 경로**다. 이는 쓰기 가능 연결이며 recovery나
checkpoint로 main DB를 바꿀 수 있으므로 원본 비변경 검사라고 부르지 않는다. 단순 connect/close와 실제
페이지 접근 후 close를 구분한다. Python 연결의 with 블록 종료만으로 연결이 닫혔다고 가정하지 않는다.

합성 실험은 다음을 대조한다.

1. CaptureSession/RawV2Writer 정상 finish·명시적 close·자식 프로세스 종료 후의 깨끗한 기준선,
   일반 read-only reader 이후 생긴 실제 0-byte WAL/SHM, 아직 read-only 연결이 열린 상태를 구분한다.
   단순히 임의 파일을 만들어 넣은 fixture를 실제 SQLite 생성 상태와 혼동하지 않는다.
2. 외부 프로세스가 없는 기준선의 main/존재하는 모든 sidecar를 바이트 hash·크기·시각·file identity로 보존한다.
   작은 fixture의 manifest·전체 payload checksum·행 순서·전체 논리 내용과 qualification 결과를 기록한다.
   원본+WAL의 일관성이 불명확하면 main만 복사하거나 immutable로 열어 비교 기준을 만들지 않는다.
3. 합성 복제본에서만 SQLite-managed 정리를 시험한다. 0-byte WAL 잔여물 사례에서 main 바이트가
   유지되는지, sidecar가 사라지는지, 전체 논리 내용과 qualification 판정이 유지되는지 각각 비교한다.
   바이트 일치와 논리 일치 중 하나만으로 다른 하나를 주장하지 않는다. 재조회가 sidecar를 다시 만드는지도 확인한다.
4. 미반영 committed WAL, non-empty/비정상 WAL, rollback journal, incomplete raw, 활성 reader/writer,
   sidecar handle, 후발 writer, junction·경로 교체는 별도 반례다. WAL에만 있는 표식 행을 보존하는지 확인한다.
   불명확한 상태는 잔여물 전용 절차에서 거부하고 수동 삭제·강제 checkpoint로 통과시키지 않는다.
5. 보호 handle 획득 → 해제 → SQLite 쓰기 가능 연결 사이의 경합 창을 시험한다. 현재 sealed_source를
   유지하면 자신의 쓰기 가능 연결도 막힐 수 있으며, 잠금을 놓고 다시 여는 것은 연속적인 배제 증명이 아니다.
   CollectorLease와 프로세스 목록은 비협조적인 외부 프로그램까지 막지 않는다. 충분한 배제 조건을
   입증하지 못하면 운영 미승인으로 남긴다. 필요하면 격리된 일관 복제본 경로를 별도 설계하되 원본을 교체하지 않는다.

실행 Python의 경로·bitness·버전, sqlite3.sqlite_version 및 sqlite_source_id(), NTFS와 잠금 오류 코드를 남긴다.
환경을 자동 업그레이드하거나 OCX에 로그인하지 않는다. DB별 32 MiB, 전체 fixture 256 MiB 이내에서
예산·자식 프로세스 종료를 관리하고 새 evidence 폴더에만 기록한다. 크기 상한은 이 합성 실험의 상한이지
운영 qualification의 크기 제한이 아니다.

합성 결과 검토 → 대상·조건을 명시한 별도 운영 승인 → sidecar 해결 → 장외 I/O/시간/공간/중단 예산 확인
→ 실제 whole-file qualification 순서를 유지한다. 처리 전 0-byte WAL/32 KiB SHM이라는 관측만으로
삭제 안전성·전체 품질을 승인하지 않는다. 실험 실패나 차단도 유효한 산출물이며 정책을 완화하지 않는다.

## 결과 읽기

원본 DB를 열지 않는 경량 조회:

```powershell
.venv/Scripts/python.exe scripts/inspect_tick_research.py research_runs/<run-id>/result.json
```

최대 8 MiB까지 읽는다. 완료 상태는 종료 코드 0, 실행 중/실패는 2,
파일 누락·크기 초과·형식/상태 모순은 3이다. 원본 전체 검증을 대신하지 않는다.
완료 결과는 `input_complete=true`, `diagnostics_only=false`, `error=null`이 모두
명시되어야 한다. 누락·자료형 오류·모순이 있으면 완료 표시만으로 승인하지 않으며,
운영 화면의 같은 조회 워커도 조회 실패로 기록한다. 원본 결과 JSON은 수정하지 않는다.

- `failed`: 결과는 진단 전용이다. 중간 체결이 있더라도 정상 수익으로 사용하지 않는다.
- `running`: 완료 기록이 없다. 강제 중단/저장 실패 등의 가능성이 있어 완료 결과로 사용하지 않는다.
- `completed_empty_input`: 입력 이벤트가 비어 있다.
- `completed_no_selected_events`: 파일에는 이벤트가 있지만 선택한 종목/venue에는 없다.
- `completed_no_fills`: 선택된 이벤트는 있으나 체결이 없다.
- `completed_with_open_position`: 마감에 보유분이 남아 있다. 현금을 총자산이나 실현 수익으로 해석하지 않는다.
- `completed_flat`: 마감 보유량이 0이다. 이것만으로 전략 수익성이나 체결 정확성을 인증하지 않는다.

`orders`, `fills`, `signals`, `order_audit`에서 주문 수명과 체결 가격/수량을 확인한다.
`quote_checks`는 재생 이벤트마다 본 호가 검증 결과의 횟수이며 고유 호가 수가 아니다.
결측/stale/잘못된 가격·잔량이 많다면 입력 계약과 정책을 먼저 확인한다.
`processed_event_counts`와 `event_count`는 실패 시 다를 수 있다. 후자는 소비한 이벤트 수다.
호가 검사가 eligible이어도 top3 OBI·시가·과열·진입 조건 미충족 등으로 신호가 없을 수 있다.

입력 정규화 이벤트 해시, raw manifest, 관련 코드 해시, 실행 설정을 함께 보존한다.
체크섬은 파일 내용의 일관성을 검사하며 공급자 원천의 진실성·무누락을 보증하지 않는다.
prototype_2의 접속 중단·파싱 오류·overflow 제어 기록은 연구 실행을 실패로 만든다.
정상 closed 파일에도 이런 기록이 있을 수 있다. 원문을 보존하는 것과 연구 입력 승인은 구분한다.

## 실행 모델의 한계

최우선 양쪽 호가만 사용한다. 동일/역전 호가는 거부한다. 새로운 호가 순번은 표시 잔량을
새로 제공하는 연구 가정이며 실제 보충 유동성이나 주문 큐 위치를 재현하지 않는다.
미체결 주문은 현금이 새로 생기면 다시 체결될 수 있으므로 원하지 않으면 취소해야 한다.
취소 지연 동안 체결 가능하고, 마감 잔여 주문은 만료한다. 보유분을 허위 청산하지 않는다.
실거래 주문 전송 기능은 없으며 기존 대시보드의 Trade/수익률 형식에 아직 연결하지 않았다.

## 운영 화면에서 조회/계획 저장

[컨트롤 타워](CONTROL_TOWER.md)에 `research_runs/<run-id>/result.json` 조회 요청을 저장하면
별도 경량 워커가 요약을 기록한다. 조회 완료와 연구 성공은 별도로 표시한다.
`sampledata/raw_ticks_v2/` 아래 닫힌 파일의 설정을 계획으로 저장하는 단계에서는 헤더만 확인하고 재생하지 않는다.
이후 명시적 **장외 검사·재생 실행**을 요청하면 제한된 워커가 실행한다. 계획과 실행의 완료를 구분한다.
큰 파일은 위 직접 CLI의 별도 장외 계획을 사용하며 화면의 보호 조건을 자동 상속한다고 가정하지 않는다.

## 다음 운영 전 확인

1. 실제 콜백 원문·순번·수신 단조 시각과 공식 원천 필드 정의의 대조.
2. 실제 입력의 방향/호가 깊이/벽시각, 세션 재접속·오류 이벤트 확인.
3. 장외 기록 지연·종료 drain·재시작/장애 복구 측정.
4. 검증된 실제 하루에서 원본 → 이벤트 → 신호 → 주문 → 체결 대조.

이는 연결 코드가 없다는 목록이 아니라 실측으로 남은 항목이다.
Daily_baseline·old_data·현재 수집 raw를 이 검증을 위해 수정하거나 재생성하지 않는다.

## 교차 검증 및 원천 필드 주의

[TICK_CROSS_REVIEW](TICK_CROSS_REVIEW.md)는 코드별 검토 기록이다. 당시 적용 상태와 현재 실행 경로를 구분한다.
FID 14는 누적거래대금이며 현재 방향 판정 근거가 아니다.
[순수 normalize_tick](collector/kiwoom/tick_normalizer.py)의 인수 기본값은 여전히 unknown이다.
반면 [현재 운영 LiveRawCapture](collector/kiwoom/live_capture.py)는 signed_volume과 signed_magnitude를 명시적으로 전달한다.
OCX 콜백 → 큐 → 정규화/원문 저장 연결은 구현돼 있다. 연결 존재와 실피드 품질·무누락 인증은 다르다.
기존 unknown 정책 raw와 raw-v1의 is_buy를 현 정책으로 검증된 값으로 소급 취급하지 않는다.
