# 데이터 수집 실행 안내

[시작점](../README.md) · [운영 상태와 남은 작업](../HANDOFF.md) · [틱 설계](../ARCHITECTURE_TICK.md)

기존 README의 현재 수집·메타데이터 절차를 분리한 문서다. 아래 모든 명령은 저장소 루트에서 실행한다.
수집 중에는 재시작·추가 OCX 로그인·전체 DB 조회·변환·실제 재생을 하지 않는다.
실행 중인 코드 버전과 현재 상태를 먼저 확인한다. 기록된 관측값은 현재 상태가 아니다.

## Mock 전종목 FID A-B-A — 실행 전 사전점검과 1회 실행 계약

상세 실험 의미·연구 배제·A2/POST 해석은
[Mock 전종목 FID 읽기 A-B-A 계약](../tests/FID_READ_AB_DIAGNOSTIC.md)을 따른다.
이 절은 **Windows/OCX에서 실제로 실행하기 직전의 운영 절차**만 정한다.
GitHub CI 통과나 이 문서 자체는 로그인·실행 승인이 아니다.

### 1. 실행 시점

- 실제 피드 의미를 보려는 1차 실험은 **공식 거래일의 KRX 정규장 09:00~15:30 KST 안**에서 한다.
- 개장/마감 burst를 별도 변수로 만들지 않기 위해 최초 1회는 가능하면 09:15 이후~15:15 이전에 한다.
- 특별 개장·임시 휴장·공휴일은 실행 당일 공식 KRX 정보를 다시 확인한다. 휴장일·장외의 0건 실행으로 대신하지 않는다.
- 동일 PC의 다른 collector/OCX 세션이나 Runtime 오류 창/PID 잔류 여부가 미확인이면 새 로그인을 시작하지 않는다.

### 2. 로그인 없는 사전점검

전용 clean checkout/worktree에서 원격 PR #24의 현재 HEAD를 먼저 확인한다.
사용자 변경이 있는 checkout을 reset/stash/clean하지 않는다. 필요하면 새 worktree를 사용한다.
실제 실행과 같은 인자에 `--preflight`만 추가한다.

```powershell
git fetch origin
git rev-parse HEAD
git status --short

.\.venv32\Scripts\python.exe collector\kiwoom\kiwoom_universe_logger.py `
  --storage raw-v2 `
  --capture-telemetry `
  --fid-read-ab-test `
  --duration-seconds 90 `
  --preflight
```

정상 JSON은 최소한 `python_bits=32`, `login_attempted=false`, `ocx_instantiated=false`,
`ocx_registered=true`, `ocx_file_exists=true`, `ready=true`를 만족해야 한다.
`ready=true`는 로그인 성공·Mock 서버 가용성·실시간 수신·저장 여유를 인증하지 않는다.
실행 직전 프로젝트 볼륨의 free bytes도 기록한다. 코드 admission 하한은 256 MiB지만
그 하한 통과만으로 90초 전종목 raw에 충분한 공간이라고 일반화하지 않는다.

### 3. 실행 직전 차단 조건

아래 하나라도 충족하면 **로그인하지 않고 중단**한다.

- checkout HEAD가 승인된 PR #24 HEAD와 다르거나 working tree가 깨끗하지 않다.
- 32비트 Python/OCX preflight가 실패한다.
- 다른 `kiwoom_universe_logger.py` collector가 살아 있거나 기존 OCX/Runtime 오류 상태 종료가 미확인이다.
- collector lease를 안전하게 획득할 조건이 불명확하다. lock 파일 존재만으로 생존/종료를 단정하지 않는다.
- 공식 거래일/시장 구간이 확인되지 않았거나 목표 정규장 구간 밖이다.
- 저장 볼륨 여유가 코드 admission 하한보다 작다.
- 기존 운영 raw/dump/operations_state를 삭제·덮어써야만 실행할 수 있다.

차단을 없애려고 자동 kill/restart/relogin, lock 삭제, Runtime 창 강제 종료, LAA 변경, queue 확대, 기본 FID 축소를 하지 않는다.

### 3-1. 당일 admission checker

위 수동 차단 조건을 한 번에 정리하기 위해 `scripts/check_fid_read_ab_admission.py`를 사용한다.
이 도구는 OCX를 만들거나 로그인하지 않고, raw DB도 열지 않는다. Git fetch도 수행하지 않으므로
컨트롤타워/운영자가 **직전에 원격을 확인해 전달한 exact SHA**를 `--expected-revision`으로 넣는다.

공식 거래일 여부는 이 로컬 도구가 인터넷 없이 추정하지 않는다. 컨트롤타워가 당일 공식 KRX 근거를
확인한 뒤 그 날짜와 근거 메모를 명시적으로 전달한다. `--execution-approved`는 사용자가 그 1회 실행을
명시적으로 승인한 뒤에만 붙인다.

```powershell
C:\Projects\Stock\.venv32\Scripts\python.exe scripts\check_fid_read_ab_admission.py `
  --repo-root <PR 스택의 clean 실행 worktree> `
  --expected-revision <컨트롤타워가 방금 확인한 정확한 SHA> `
  --official-market-date YYYY-MM-DD `
  --official-market-source-note "<확인한 공식 KRX 근거와 시각>" `
  --execution-approved
```

checker는 **검사 대상 worktree 루트에서** 그 worktree의 `scripts\...`로 실행하고 `--repo-root`도 같은
worktree를 준다. 실제로 import된 checker 코드의 checkout, `git rev-parse --show-toplevel`, `--repo-root`가
하나라도 다르면 BLOCKED다. Python 실행 파일은 `C:\Projects\Stock\.venv32`처럼 다른 경로여도 된다
(정상 sibling worktree). ignored 하위 폴더가 부모 저장소의 HEAD/clean을 빌리지 못한다.

판정:

- `RUN_READY` — 관측 **시작과 완료** 두 시점 모두에서 로그인 전 계약이 충족됨. 실제 수집 성공 인증은 아님.
- `RUN_BLOCKED` — wrong HEAD/dirty tree/실행 root·checker 출처 불일치/collector entrypoint가 HEAD의 tracked
  blob과 다름/skip-worktree·assume-unchanged 항목/preflight 실패/저장공간 하한/collector 또는 Runtime 창/
  lease 미확인/시장 날짜·09:15~15:15 구간/승인 중 하나라도 차단.
- `RUN_UNCERTAIN` — process probe 실패·누락·잘림, 명령줄/실행 경로를 읽을 수 없는 Python, 같은 `.venv32`
  Python의 다른 프로세스, 벽시계 역행·단조 시계와 2초 초과 불일치처럼 정체나 시각을 확정하지 못한 상태.
  실제 로그인 금지.

READY 판정은 `RUN_READY` 문자열이나 `valid`/`meets_code_minimum` 플래그가 아니라 기록된 근거를 같은
엄격한 pure evaluator로 다시 계산한 결과다. 필수 필드 누락·타입 오류(bool을 숫자로 보지 않음)·내부 모순은
READY가 아니다. prepare와 verify도 같은 evaluator로 재검증한다.

프로세스 탐색은 checker 자신, 조회용 PowerShell, 그리고 checker를 띄운 venv launcher를 제외한다.
launcher 제외는 직계 부모가 같은 venv `python.exe` 경로이고, 부모와 checker의 명령줄을 모두 읽을 수 있으며
실행 파일 뒤 인자가 정확히 같을 때(launcher가 같은 인자로 자식을 띄운 근거)만 한다. 명령줄 누락·빈 값은
UNCERTAIN, 무관한 부모 스크립트는 일반 같은-runtime 프로세스(UNCERTAIN), collector 부모는 BLOCKED다. 조회 결과는 목록과 개수가 함께 있어야
하며, 명시적 빈 목록만 "없음"이다. 명령줄에 `kiwoom_universe_logger`가 있는 프로세스는 interpreter와
script/`-m` module 형식에 관계없이 BLOCKED다(편집기 등도 보수적으로 차단). 임의 CommandLine 원문은
출력하지 않고 일치 여부만 남긴다. 프로세스·창 조작은 하지 않는다. 기존 lease 파일은 새로 만들거나 삭제하지 않고
존재할 때만 잠금 가능 여부를 순간 확인한다. 파일이 없으면 실제 collector가 실행 시 다시 lease를 획득한다.

`RUN_READY`라도 서버 가용성·실시간 수신·native 안정성·실험 성공은 미인증이다.

### 3-2. short-lived run plan 생성과 fresh verify

`RUN_READY` admission을 그대로 오래 들고 있다가 실행하지 않는다. 시간 경계는 모두 반개구간이다.

- admission 근거: 관측 **시작** 기준 `0 <= age < 60초`. 완료 시각이 판단 시각보다 늦으면 거부.
- plan: `created <= t < expires`(TTL 300초). 정확히 `expires`인 순간은 이미 만료.
- verify: 시작 시각과, fresh admission·명령 재계산 뒤의 **완료 시각** 모두에서 TTL·09:15~15:15 구간·
  fresh admission 신선도를 검사한다. 단조 시계 경과로도 만료를 재확인하고 벽시계 역행/불일치면 명령을 숨긴다.
- prepare/verify CLI는 시작 시각을 고정하지 않는다. plan 생성 시각은 admission 완료 뒤에 읽는다.

plan은 admission 사본과 canonical SHA-256을 보존한다. digest는 서명이 아니며 사용자 승인을 인증하지 않으므로,
verify는 embedded admission이 **plan 생성 시각에도** READY·60초 이내였는지 재검증하고(created/expires만
옮긴 plan 거부), 실제 명령을 보여주기 전에 trusted SHA와 실행 승인을 다시 받는다.

plan 생성:

```powershell
C:\Projects\Stock\.venv32\Scripts\python.exe scripts\prepare_fid_read_ab_run.py `
  --repo-root <clean execution worktree> `
  --expected-revision <컨트롤타워가 방금 확인한 정확한 SHA> `
  --official-market-date YYYY-MM-DD `
  --official-market-source-note "<공식 KRX 근거와 확인 시각>" `
  --execution-approved `
  --output <repo-root>\operations_state\fid_read_ab_run_plans\<새 plan 이름>.json
```

prepare는 fresh admission을 직접 다시 수행한다. READY가 아니면 plan을 만들지 않는다.
output은 gitignored `operations_state/fid_read_ab_run_plans/` 아래 새 파일만 허용하며 기존 plan을
덮어쓰지 않는다. plan 생성 자체가 다음 fresh `git status`를 dirty로 만들지 않게 하는 계약이다.
collector는 실행하지 않는다.

실제 명령을 보기 직전 fresh verify:

```powershell
C:\Projects\Stock\.venv32\Scripts\python.exe scripts\verify_fid_read_ab_run_plan.py `
  --plan <방금 만든 plan JSON> `
  --expected-revision <컨트롤타워가 다시 확인한 정확한 SHA> `
  --execution-approved
```

verify는 plan 만료·embedded admission digest·trusted revision을 확인한 뒤 admission checker를 또 실행한다.
HEAD/clean tree/preflight/process/window/lease/시간 중 하나라도 달라지면 명령을 숨기고 NOT_READY로 끝난다.
READY여도 plan에 저장된 명령을 그대로 믿지 않고 fresh admission의 Python executable과 현재 worktree의
`kiwoom_universe_logger.py` 경로로 exact command를 다시 계산해 plan과 일치할 때만 출력한다.
PowerShell 표시는 `& '<exe>' '<arg>' ...` 형식이며 작은따옴표류는 이중화하고 큰따옴표·제어문자 token은 거부한다.
plan reader는 실제로 읽은 byte에도 1 MiB 상한을 두고 NaN/Infinity/중복 key JSON을 거부한다.
verify 완료 뒤 사람이 실제로 실행하기까지의 사이는 검사하지 못한다(race-free 아님).
digest가 plan 시각을 덮지 않으므로 created를 admission 60초 신선도 안에서 옮긴 plan은 구별하지 못한다.
이 경우에도 verify는 fresh admission을 다시 요구한다. 제목에 `kiwoom`/`키움`/`OpenAPI`가 들어간 모든 창
(터미널·편집기·브라우저 탭 포함)은 기존 계약대로 보수적으로 BLOCKED다.

verify의 `MANUAL_COMMAND_READY`도 **자동 실행 승인이 아니다**. 출력된 명령은 사람이 확인해 수동으로
실행할 대상일 뿐이고 verify/prepare 어느 쪽도 collector launch·OCX login·SetRealReg를 호출하지 않는다.

### 4. 승인 후 실제 Mock 1회 실행

사전점검과 당일 거래일/구간 확인이 통과한 경우에만 다음 **한 번의 독립 실행**을 사용한다.

```powershell
.\.venv32\Scripts\python.exe collector\kiwoom\kiwoom_universe_logger.py `
  --storage raw-v2 `
  --capture-telemetry `
  --fid-read-ab-test `
  --duration-seconds 90
```

추가하지 말아야 할 인자: `--codes`, `--nxt-codes`, `--managed-launch`, `--explicit-ocx-teardown`, 모든 `--aftermarket-*`.
관측 서버가 Mock이 아니면 backend/구독 전에 실패해야 하며 live로 계속 진행하지 않는다.
로그인 실패·구독 거부·FID 예외·queue/storage 오류가 나면 자동 재시도하지 않는다.

### 5. 실행 후 기계적 성공과 실험 해석을 분리

우선 새 session 폴더의 작은 근거만 bounded analyzer로 읽는다. 이 명령은 raw DB를 열거나
COUNT/hash/SQLite scan하지 않는다.

```powershell
C:\Projects\Stock\.venv32\Scripts\python.exe scripts\analyze_fid_read_ab.py `
  --session-dir operations_state\capture_sessions\<session_id> `
  --expected-revision <실행한 정확한 SHA>
```

analyzer가 READY를 반환해도 연구 적격이나 native 원인 규명으로 해석하지 않는다.
LIMITED/DIAGNOSTIC_ERROR/INCOMPLETE/INVALID이면 두 번째 실행으로 덮지 말고 해당 작은 근거를 보존한다.

READY인 경우에만 실제 결과 전에 고정한 `fid_read_ab_assessment_v1` 규칙을 적용한다.

```powershell
C:\Projects\Stock\.venv32\Scripts\python.exe scripts\assess_fid_read_ab.py `
  --session-dir operations_state\capture_sessions\<session_id> `
  --expected-revision <실행한 정확한 SHA>
```

1차 자동 판정은 체결/호가를 분리한 `fid_read_ns` A1/B/A2 중앙값의 방향만 본다.
각 phase·real_type 유효 표본 3개 미만이면 not-assessable이다. 효과크기 threshold와 p-value는 없고,
`processing_ns`는 보조, `queue_submit_ns`는 guardrail일 뿐이다.
source clock difference·callback/30·resource history·PRE/POST는 자동 판정에 넣지 않는다.

- raw manifest `feed_scope == "kiwoom_universe_fid_read_diagnostic"`
- sidecar `fid_read_ab_test.json`은 `fid_read_ab_test_v2`, `diagnostic_only=true`, `research_eligible=false`
- 최종 저장 snapshot의 closed/writer_closed, pending callback 0
- raw/sidecar/telemetry/diagnostics의 session identity와 code revision 일치
- sidecar `diagnostic_error`, FID read failure, queue/storage 오류 유무
- A1/B/A2/POST별 callback·FID attempt/success 계수와 telemetry 표본
- 저장 종료 뒤 collector PID·native Runtime 창·lease는 서로 독립 사실로 확인

저장이 정상 종료돼도 phase별 callback이 없거나 표본이 부족하면 기계적 성공과 해석 가능성을 분리한다.
A1/B/A2 callback 수를 30으로 나눈 값을 독립 정상상태 service rate로 부르지 않는다.
POST는 분석에서 제외하되 버리지 않는다. FID clock difference를 network latency라고 부르지 않는다.
정상 종료 뒤 PID/Runtime 창 잔류가 보이면 두 번째 실험을 시작하지 않는다.
첫 실행 검토 전에는 live 비교·동시 두 계정 비교·반복 전종목 실행으로 자동 확대하지 않는다.

## 2026-09-17 설치된 공식 명세 대조

`C:/OpenAPI/koa_devguide.xml`(CP949, 관측 당시 수정 시각 2026-09-12)의 다음 항목을 직접 확인했다.
실행 중 OCX 호출·추가 로그인·구독 변경 없이 파일만 읽었다.

- 280~299행: 종목코드로 분기하는 조회는 6자리=KRX, `_NX`=NXT, `_AL`=통합.
  해당 조회로 들어오는 실시간 이벤트도 요청한 접미사 포함 종목코드로 전달된다고 명시한다.
  현재 6자리 구독은 NXT 수집 증거가 아니다. SetRealReg의 접미사별 실제 동작과 모의서버 지원은
  장외에 준비하고 별도 소규모 실측으로 확인한다. 통합 구독을 개별 체결의 거래소 판정으로 대신하지 않는다.
- 2432~2435행: 실시간 타입에 포함된 FID 항목들은 함께 수신된다.
  저장 raw의 FID 목록은 코드가 GetCommRealData로 **읽어 보존한 목록**이다.
  등록/저장 목록에 거래소 FID가 없다는 것만으로 OCX가 거래소 정보를 제공할 수 없다고 단정하지 않는다.
- 2439~2442행: 주식체결 FID 15의 음수는 매도체결, 양수는 매수체결이라고 명시한다.
  signed_volume에 공식 근거가 생겼지만 **이 관측 시점에는** 수집기 방향 정책/기존 raw를 변경하지 않았다.
- FID 14 표본 `41`은 16,100원 × 2,555주 = 41,135,500원과 비교하면 백만원 단위와 양립한다.
  표본 한 건으로 단위 확정이나 오류 판정을 하지 않는다.

`2385928`의 결손 반례는 후속 `464193d`의 두 축 분리와 추가 합성 회귀로 보강했다.
`venue_resolution=per_event_venue`만으로 calm을 반환하지 않는다. 별도의 `nxt_coverage`는
기본 unconfirmed다. 빈 입력·대상 구간 무관측·알 수 없는 venue·통합시장 이벤트는 unverified다.
KRX만 있는 구간은 호출자가 NXT 수신 완전성을 별도 확인해 confirmed로 지정한 경우에만 calm이다.
confirmed는 증거를 자동 검증하는 기능이 아니라 호출자의 선언이다. 지금 운영 데이터에는 지정하지 않는다.
레거시 엔진의 `--allow-unverified-nxt`는 필터 우회이며 검증 승격이 아니다. 거래가 0건이어도
저장된 manifest의 `params.nxt_guard`에 두 정책·우회 여부·판정·이유를 남긴다.

## NXT 소규모 검증 준비 (실행 미연결)

`collector/kiwoom/nxt_probe.py`의 `prepare_plan`은 6자리 종목 하나에 대해 원 코드·`_NX`·`_AL`,
명시한 mock/live, KST 시작 시각과 1~300초 제한을 담은 오프라인 계획만 만든다.
`observation`은 요청/콜백 코드 원문, 이벤트 타입(알 수 없는 타입 포함), FID 원문 전체,
UTC·단조 수신 시각, 세션·서버와 수신 시각 기준 시장 구간을 분리 보존하는 순수 함수다.
접미사 힌트가 있어도 venue=unknown, coverage=unconfirmed를 유지한다. 이 코드는 운영 콜백이나
로그인에 연결하지 않았고 파일 저장/구독 실행기도 아니다. 수신 시각 구간은 거래소 시각 인증이 아니다.

별도 실측 순서는 다음과 같다.

1. 기존 수집의 정상 종료와 OS 프로세스 부재를 확인한 뒤 별도 세션/새 경로를 사용한다.
   NXT 대상 여부를 공식 수단으로 확인한 한 종목으로 시작한다. mock 지원과 live 지원은 따로 기록한다.
2. 각 코드의 SetRealReg를 순차로 시험한다. 요청 코드·화면번호·FID 요청 목록·반환값·실제 서버 응답·
   접속 중단/구독 해제 시각을 저널로 남긴다. 반환 성공과 실시간 수신 성공을 구분한다.
   기존 운영 CLI는 6자리만 허용하므로 접미사 코드를 억지로 넣거나 운영 필터를 풀지 않는다.
3. 콜백 타입별 공식 OCX FID 정의에서 읽을 필드를 정하고 원문을 보존한다.
   저장 목록만으로 전체 제공 필드를 판단하지 않는다. `_AL`은 개별 체결 거래소로 분류하지 않는다.
   `observation`의 힌트를 per_event_venue 인증으로 바로 넘기지 않는다.
4. 프리마켓 전략 구간 08:00~08:50과 정규장, 애프터마켓 15:40~20:00을 별도 관측한다.
   짧은 시험의 수신 성공은 해당 전략 구간 전체의 coverage 증거가 아니다. 0건도 무거래 인증이 아니다.
5. 애프터마켓 시험에는 현재 기본 15:35 자동 종료를 사용하지 않는다. 별도 실행기의 종료 계약을
   명시한 시작+최대 300초로 설계하고, 종료/drain/closed 대조 후 다음 코드를 시험한다.
   운영 중 세션의 종료 조건은 바꾸지 않는다. REST 명세를 OCX 계약으로 대체하지 않는다.

## 2026-09-17 종료 후 원문 표본 검증 순서

현재는 실행 중이므로 이 절차를 아직 수행하지 않았다. 먼저 같은 session_id의 종료 로그,
상태(writer_closed/finalization/error), starting/draining/closed 보고, OS 프로세스 부재를 대조한다.
pending/in_flight/queued=0만으로 종료를 판정하지 않는다. 정상 종료해도 10:31 이후 수신 정체는 별도 결손이다.

대조 후 읽기 전용 연결로 미리 정한 정규장 seq 구간에서 최대 1,000개 레코드씩, 최대 3구간만 읽는다.
전체 COUNT/해시/백테스트 없이 기본키 범위와 LIMIT·짧은 시간 제한을 사용하고, 날짜·세션·선택 범위를
새 진단 결과에 남긴다. 이벤트 타입, FID 15의 명시적 +/-·무부호·0·결측·비정상 문자열,
FID 10 가격 부호를 원문 기준으로 분류한다. 희귀 값이 표본에 없다는 것을 전체 부재로 보고하지 않는다.

기존 signed_volume은 명시적 비영(非零) 부호만 방향으로 사용한다. 합성 저장 회귀에서 정상 부호의
추가 trade_direction_unverified 레코드는 줄고 가격/시간/0 거래량 오류는 유지되는 것을 확인했다.
실제 표본 대조 전 **당시에는** 다음 실행 적용 여부가 미결정이어서 운영 기본 unknown을 유지했다.
전환하더라도 원본을 덮어쓰지 않는다. 필요 시 별도 파생 경로에 원 seq·정규화 정책·원본 참조를 보존한다.
v1 거래량은 abs() 처리됐으므로 부호 복원이 불가능하며 기존 FID 14·호가 추정 is_buy는 정답이 아니다.

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
  현재 방향은 FID 15의 명시적 비영(非零) 부호를 쓰는 `signed_volume`, venue는 `unknown`이며 가격은 `signed_magnitude` 정책을 사용한다. 원문은 그대로 남기고 무부호·0·비정상 거래량은 방향 미확인 품질 오류로 보존한다.
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

### 2026-09-17 수신 정체와 네이티브 충돌 조사

10:31:20 마지막 수신 이후 저장 대기 0, Qt 상태 갱신과 키움 CommsLog의 주기 송신은
11:29까지 계속됐다. 이는 저장 큐 적체를 지지하지 않지만, 실제 서버 수신/구독 유효성을 증명하지 않는다.
11:30:26 PID 18796의 Windows 오류 1000과 1001, 정상 종료 보고 부재를 대조했다.
사용자는 해당 시각 직접 종료하지 않았다고 확인했다. 수신 정체와 이후 실행 중 충돌의 인과관계는 별도 확인한다.

로컬 덤프 `python.exe.18796.dmp`(4,167,178바이트)를 보존하고 두 파서로 확인했다.
예외 c0000005, 정보 [8, 0]은 주소 0 실행 접근 위반이다.
덤프에는 스레드 14636 하나만 있으며 x86 EIP=0, ESP=0x0629ca0c다.
ESP의 값 0x074476fb는 AhnLab Safe Transaction `mkd25sdk.dll+0x76fb`이고,
설치 DLL의 직전 명령(+0x76f5)은 간접 `call [0x10027ac0]`이다.
재배치된 함수 포인터 0x07467ac0의 덤프 값은 0이었다.
설치 DLL과 덤프 모듈의 PE timestamp=1783062464, 이미지 크기=184320도 일치한다.
따라서 보안 모듈 내부 null 함수 호출을 강하게 지지하지만, 심볼 기반 전체 스택 복원이나
포인터가 0이 된 선행 원인은 확인하지 못했다. 예외 주소 필드와 EIP도 불일치하므로 해석 한계를 남긴다.
이 증거로 10:31 정체까지 같은 원인이라고 단정하거나 Python/raw-v2 책임을 완전히 배제하지 않는다.

근거는 `operations_state/session_observations/20260917_crash/`에 로컬 보존한다.
덤프 SHA256: `22b5f9c595bc828b856e7789404ff6f25d769cb005c5957a31cd7288899ec7c3`.
분석 라이브러리는 `operations_state/diagnostic_libs`에만 설치했으며 운영 가상환경은 변경하지 않았다.
덤프/보안 로그는 외부 전송하지 않았다. 보안 모듈 제거·무력화·바이너리 패치는 하지 않는다.
공식 업데이트/재설치 여부는 공급사와 확인하고, 전달이 필요하면 별도 승인 후 최소 근거만 공유한다.
설치 ASTx=1.18.1.1997, mkd25sdk=2.7.0.45이며 최신 여부는 아직 확인하지 못했다.
[공식 트레이 메뉴 안내](https://help.ahnlab.com/astx/1.0/ko_kr/operation_mode.htm)의 수동 업데이트로
결과를 확인한 뒤 진단 보강된 새 수집 세션을 시험한다. 특정 버전에서 이 충돌이 수정됐다는 근거는 없다.

다음 실행부터 `capture_diagnostics.py`가 새 `logs/collector_fault_<UTC>_<PID>.log`를 만든다.
Windows 예외용 faulthandler와 침묵 구간별 Python 전체 스레드 스택을 기록한다.
침묵 기록은 동일 구간 한 번, 실행당 최대 세 번이며 콜백마다 파일을 쓰지 않는다.
기존 `-X faulthandler` 등이 켜져 있으면 해당 충돌 출력 경로를 보존하고 자체 파일에는 침묵 기록만 쓴다.
진단 보강은 외부 모듈 결함의 수정이나 자동 재시작 기능이 아니다. 현재 실행 프로세스에는 소급 적용되지 않는다.

해석 근거: [Microsoft 실행 접근 위반](https://learn.microsoft.com/en-us/shows/inside/access-violation-c0000005-execute),
[Python 3.10 faulthandler](https://docs.python.org/3.10/library/faulthandler.html).

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
- 하위 `RawV2Writer`도 append/commit/finish 중 실패한 상태에서는 추가 commit을 거부한다.
  종료 커밋이 실패한 뒤 남은 closed 갱신을 재커밋하지 않으며, 정상 finish 이후에도 commit을 거부한다.
  커밋 전에 실패한 합성 DB에서는 연결 종료 시 미커밋 배치를 버리고 기존 커밋 배치를 보존함을 검증했다.
  실제 저장장치 오류의 커밋 여부·전원 장애 내구성까지 보장하는 것은 아니다.

아래는 `--storage raw-v1` 호환 모드의 종료 확인이다.

- Ctrl+C 또는 15:35 자동 종료 시 수신을 멈추고 대기큐와 저장 중인 배치를
  끝까지 커밋한 뒤 종료한다. `종료 후 미커밋: 0 건`과 `DB 저장 결과: 커밋 완료`를 확인한다.
- 저장 중에는 프로세스를 기다린다. 두 번째 Ctrl+C, 창 강제 닫기 또는 전원 장애는
  저장 완료를 보장하지 않는다. SQLite의 기존 `synchronous=OFF` 설정은 유지한다.
- DB 오류가 발생하면 수집을 중단하고 오류 및 미커밋 건수를 기록하며 종료 코드 2를
  반환한다. 미커밋 데이터는 재실행만으로 복구되지 않는다.
- 변경된 종료 처리는 다음 실행부터 적용된다. 변경 전부터 실행 중인 프로세스에는
  자동 반영되지 않으므로, 운영 적용 전에 변경분을 커밋한다.

### 닫힌 raw v2 압축 보관·복원 시제품

`collector/raw_archive.py`와 `scripts/archive_raw_v2.py`는 **Windows에서 32 MiB 이하의
작은 파일만 처리하는 시제품**이다. 실제 대용량 raw 적용은 미실행이며 상한 해제 옵션도 없다.
raw v1, DB 정리/VACUUM, 자동 원본 삭제, 원문 보정, LOB/초봉 변환은 범위 밖이다.

**형식과 처리 상한.** 현재 Python 환경의 표준 `gzip.GzipFile`(DEFLATE, level 6)을 사용한다.
별도 패키지나 `uv.lock` 변경 없이 일반 gzip 도구로도 복원할 수 있다. 전체 버퍼 압축 대신
1 MiB씩 읽고 쓰며 압축 파일은 최대 33 MiB, 복원 바이트는 기록된 원본 크기를 상한으로 검사한다.
기존 raw 읽기의 JSON 메모리도 제한하기 위해 복원본에서 최대 100,000레코드·payload당 64 KiB를
먼저 확인한다. SQLite 페이지 캐시는 약 2 MiB 설정이며 전체 프로세스 RSS의 하드 제한은 아니다.
원문 바이트는 그대로이며 압축률·속도는 측정 전 확정하지 않는다. 합성 결과를 실제 압축률로 일반화하지 않는다.
근거: [Python gzip](https://docs.python.org/3/library/gzip.html),
[SQLite 읽기 전용/immutable URI](https://www.sqlite.org/uri.html).

**닫힘 계약.** 운영 적용 전 운영자가 같은 session_id의 입력 중단·drain·writer_closed·마지막
보고·로그·OS 프로세스 부재를 대조해야 한다. `--closure-note`에는 확인 시각과 그 근거 위치를 남긴다.
이는 운영자 진술이며 도구가 근거 내용을 자동 인증하는 기능은 아니다. `closed` 헤더만으로 승인하지 않는다.
도구는 기존 `CollectorLease`를 전체 보관 작업 동안 유지하고, Windows 파일 핸들의 공유 모드를
읽기만 허용해 이미 열린 쓰기 핸들 및 이후 쓰기·삭제를 거부한다. 원본은 SQLite로 열지 않는다.
WAL/SHM/rollback journal이 하나라도 있으면 크기 0이어도 거부한다. 이를 삭제하거나 체크포인트해서
통과시키지 않는다. 잔여 sidecar의 복구·완결 확인은 별도 운영 작업이다. OS 잠금이 실패하는 환경을
우회하지 않는다. 장중 예약·수집 종료 기능은 없으며 운영 실행은 별도로 승인된 장외 작업이다.

**완료 게시와 실패.** 원본과 다른 부모의 새 보관 디렉터리만 허용한다. `raw.db.gz.partial`에
스트리밍 압축·SHA256 계산 → fsync → `verification.db.partial`로 복원·크기/바이트 SHA256 대조
→ 기존 `read_raw_v2` 전체 소진(순번·payload 체크섬·닫힘 계약) → 복원본 재해시 순서다.
검증용 복원본만 삭제하고 gzip을 게시한 뒤 `archive.json.partial`을 fsync해 `archive.json`으로 게시한다.
게시에는 같은 디렉터리 내 hard link 생성 후 임시 이름 해제를 사용해 기존 대상을 덮어쓰지 않는다.
해당 기능을 지원하는 로컬 파일시스템이 필요하며 실패 시 다른 방식으로 자동 우회하지 않는다.

`archive.json`이 유일한 보관 완료 표식이다. 원본 절대 경로·크기·mtime·바이트 SHA256,
raw manifest, 종료 근거 진술, gzip 형식/레벨·Python/zlib 버전·압축본 크기/해시와 검증 결과를 기록한다.
압축본만 있거나 `.partial`만 있는 디렉터리는 미완료다. 실패·중단·ENOSPC 시 잔여물은 진단용으로
남기고 자동 재개/삭제하지 않는다. 새 목적지 이름으로 재시도하며 잔여물 제거는 별도로 검토한다.
복원 검증용 사본의 `-wal`/`-shm` 정리(검증 성공 후 실행)가 실패해도 이후 게시 단계로 넘어가지
않는다 — archive 경로는 `raw.db.gz`/`archive.json`을, restore 경로는 `raw.db`를 게시하지 않으며
원본 바이트와 기존에 이미 게시된 보관본은 그대로 남는다. 두 사이드카(`-wal`, `-shm`) 중 먼저
삭제가 끝난 쪽은 잔여물에서 빠지므로, 남는 파일 집합은 정리가 어디까지 진행됐는지에 따라 달라진다
(고정된 목록이 아니다). 합성 검증:
[test_copy_sidecar_unlink_failure_blocks_publish, test_second_sidecar_unlink_failure_leaves_only_that_sidecar](../tests/test_raw_archive.py).
완료 표식도 복원 시 압축본 해시·gzip EOF/CRC·복원 크기/해시·raw 계약을 다시 확인한다.
해시는 우발 손상 대조이며 manifest와 데이터의 동시 악의적 변조를 막는 서명은 아니다.
파일 fsync/이름 게시는 OS 전원 장애·스토리지 고장 내구성의 실측 인증이 아니다.

**복원과 품질의 분리.** 복원도 존재하지 않는 별도 디렉터리만 받는다. `raw.db.partial`을 검증한 뒤에만
`raw.db`로 게시한다. 원본 경로로 덮어쓰지 않으며 `parse_error`, 방향 null, 원문 FID를 그대로 보존한다.
검증 결과 `research_quality=not_certified`는 연구 합격을 의미하지 않는다. parse_error 등 제어 기록
건수는 기록하지만 이를 제거하거나 연구 입력 거부 정책을 완화하지 않는다. 기존 실제 세션의 방향 미확인
8건에 대한 차단은 그대로다. raw 읽기 성공은 시장 데이터 무누락·venue·전략 정확성 인증이 아니다.

**재현(작은 합성 데이터만).** 아래 이름은 새 경로로 선택한다. 첫 명령은 빈 closed 합성 raw를 만든다.
방향 null·parse_error 보존과 연구 거부까지 포함하는 재현은 아래 pytest 묶음을 사용한다.

```powershell
.\.venv\Scripts\python.exe -c "from collector.raw_v2 import RawV2Writer; w=RawV2Writer('operations_state/archive_demo/source/s.db', source='synthetic', session_id='demo', market_date='2026-09-19', feed_scope='fixture'); w.finish(close_ns=1); w.__exit__(None,None,None)"
.\.venv\Scripts\python.exe scripts/archive_raw_v2.py archive operations_state/archive_demo/source/s.db operations_state/archive_demo/bundle --session-id demo --closure-note "합성 demo: 직접 finish 후 연결 종료; 운영 수집 없음"
.\.venv\Scripts\python.exe scripts/archive_raw_v2.py restore operations_state/archive_demo/bundle operations_state/archive_demo/restored
.\.venv\Scripts\python.exe -m pytest tests/test_raw_archive.py tests/test_raw_v2.py tests/test_capture_session.py -q -p no:cacheprovider
```

**향후 대용량 적용 계획(미실행).** 먼저 종료 근거·수집 잠금·쓰기가 차단된 원본·sidecar 부재를 확인하고,
파일시스템 hard link/동기화 지원과 장외 I/O 시간을 확보한다. 작은 대표 파일을 별도 승인하여 실제 압축률·속도를
측정한 뒤 상한과 raw 검증 예산을 재설계한다. 시제품 상수를 바꾸는 것만으로 운영 승인된 도구가 되지 않는다.
원본 크기 S, 압축 크기 C라면 검증 중 보관 위치에는 C+S가 동시에 필요하다. 사전 검사는 압축률을 가정하지 않고
3S+256 MiB 여유를 요구한다. 별도 복원에는 S+256 MiB가 필요하다. 여유 검사는 공간 예약이 아니므로
실행 중 ENOSPC 처리도 필요하다. 원본은 계속 S를 차지하므로 이 작업만으로 원본 디스크 공간이 반환되지는 않는다.

보관 I/O는 원본 S 읽기, C 쓰기, C 읽기+S 복원 쓰기, 복원본 SQL 사전 제한 검사/전체 raw 읽기/재해시,
압축본 C 재해시다. 별도 복원은 압축본 해시 C 읽기+압축 해제 C 읽기, S 쓰기와 같은 raw/재해시 읽기가 추가된다.
SQLite 페이지·캐시 접근에 따라 실제 읽기량은 달라지므로 처리 시간을 추정치로 확정하지 않는다.
운영 순서는 새 보관본 생성 → 별도 위치 실제 복원 → 바이트 대조/raw 계약 → 근거 보존이다.
원본 삭제 정책은 별도 결정이며 이 도구에는 넣지 않는다. 실제 대용량 압축·전체 해시·복사·복원·부하 측정,
디스크/전원 장애 내구성과 네트워크 파일시스템 동작은 아직 확인하지 않았다.

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
# 2026-09-17 진단 보강: 다음 실행부터 적용

## 독립 NXT 모의 구독 실측

### 애프터마켓 수집 후속 방향 (설계 검토, 운영 미적용)

CLI에서 직접 계획을 만드는 `--nxt-codes` 계열 인자는 `0cfc254`에서 추가했다(§ 아래 CLI 절 참고).
계획 실행은 독립 raw-v2 + 명시적 1~300초로 제한한다. 기존 15:35 종료를 적용하지 않는 대신
이 제한 시간으로 종료를 요청한다.

### 단일 로그인 내 정규장→애프터마켓 세션 전환 (`ed3a680` 이후, 운영 미검증)

같은 OCX 인스턴스·로그인 연결을 유지한 채 정규장 저장을 마치고 애프터마켓 저장으로 넘긴다.
구현은 `collector/kiwoom/session_transition.py`(순서 실행기)와
`KiwoomUniverseLogger.run_aftermarket_transition()`(수집기 연결)로 나뉜다.
**호출해야 도는 명시적 동작**이며, 시계 기반 자동 발동은 아직 없다.

네 단계를 순서대로 실행하고, 앞 단계가 성공했다고 확인되기 전에는 다음 단계로 넘어가지 않는다.

1. `unsubscribe_regular` — 신규 입력 차단(`accepting_events=False`)이 `SetRealRemove` 호출보다 먼저다.
2. `finalize_regular` — 정규장 writer 를 닫고 `state=="closed"`와 `finalization` 이 모두 있어야
   성공으로 본다. 예외가 아니라 거짓 반환도 실패로 취급한다 — 닫혔는지 모르는 채로 다음 파일을
   열지 않기 위해서다.
3. `open_aftermarket` — 새 `LiveRawCapture`(새 `session_id`·새 파일 경로)를 만들고
   `subscription_plan.json` 을 그 폴더에 남긴다. 감시기(`SessionMonitor`)도 애프터마켓
   시장 프로필로 **새 인스턴스**를 만들어 교체한다 — 정규장의 마지막 수신 시각·침묵 카운터를
   이어받지 않기 위해서다.
4. `subscribe_nxt` — 계획의 코드만 `SetRealReg` 로 등록한다. 성공해야 `accepting_events=True` 로
   복귀할 수 있다. 등록 성공 시각부터 애프터마켓 구간 자체의 제한 시간을 재며,
   단계 완료·전환 완료 기록까지 저장한 뒤 입력을 연다(아래 종료 조건 참고).

**전환 기록** (`operations_state/session_transitions/<transition_id>.json`, schema
`session_transition_v2`)에는 다음이 남는다. 이전 세션 폴더의 `session_transition.json` 경로는
더 이상 쓰지 않는다. 새 저장 세션 생성 전부터 같은 별도 진단 경로를 사용한다.
전환 식별자(`transition_id`), 이전/다음 `session_id`, 코드 리비전, 애프터마켓 계획 원문,
단계별 시작·완료 시각(단조 시계 + UTC + KST), 단계별 반환값/예외, 실패 단계, 마지막 정규장
수신·첫 애프터마켓 수신 시각과 그 공백(`reception_gap_sec`, 무누락을 보장하지 않는다는 note 포함).
수신 공백의 두 값은 모두 **콜백 진입 때의 monotonic 초**이며 `reception_clock`으로 명시한다.
`SessionMonitor.last_event_ts`의 epoch와 섞지 않는다. UTC/KST 표시 시각은 별도 필드다.
양쪽 실제 accepted 수신이 있어야 공백을 계산하고, 미수신 또는 잘못된 음수 간격은 미확인(null)이다.
첫 애프터마켓 수신이 도착하면 같은 진단을 갱신한다. 체결 침묵을 호가 수신이 초기화하지 않는
감시기의 종류별 시계는 그대로 유지한다.

**기록 성공이 다음 동작의 조건이다.** 전환 시작, 각 단계 시작·완료, 전체 완료·실패와 전환 중
콜백을 즉시 저장한다. 임시 파일 쓰기 → flush/fsync → 원자적 교체 순서다. 단계 시작 기록 실패면
그 단계의 부작용을 실행하지 않고, 완료 기록 실패면 다음 단계를 실행하지 않는다. `phase`와
단계별 시각은 마지막 확인된 진행을 나타내며, 도중 프로세스가 사라진 것을 완료로 해석하지 않는다.
완료된 전체 기록 전에는 succeeded가 참이 아니다. 이것은 전원 손실 내구성 인증이 아니다.

기록 실패는 `failed_step=diagnostic_write`와 `recording_error`에 남긴다. 실패 기록 저장 자체도
불가능하면 메모리/로그의 오류와 마지막 성공한 디스크 기록만 남을 수 있다. 파일 존재만으로
완료를 판정하지 않는다. 실패·취소·시간 초과 후 입력을 차단하고 구독 해제를 시도하며, 아직
닫히지 않은 writer를 abort하고 최대 5초씩 기다린 결과를 `cleanup`에 남긴다. 대기 만료는
정리 미확인이고 닫힌 정규장 raw·보고는 다시 쓰지 않는다. 첫 수신의 진단 갱신 실패도 입력을
차단하고 저장 오류 종료를 요청한다. 실패 후 새 전환이나 자동 재로그인은 하지 않는다.

전환 중 Qt 종료 재진입은 취소 요청만 남기고, 통계 루프는 세션 교체 중 감시/타이머 적용을
건너뛴다. 단계·기록 경계에서 취소와 단조 시계 기준 30초 경과를 검사한다. 기존 writer 준비/
drain/닫기 대기와 실패 정리 대기는 유한하다. 다만 이 경계 검사는 이미 실행 중인 네이티브
OCX 호출이나 OS 파일 I/O를 선점 중단하는 기능이 아니며, **30초 안의 강제 반환 보장은 아니다.**
실제 Qt 응답 지연·OCX 정체는 별도 실측 대상이다.

**전환 중 콜백**은 저장 파일 대신 이 기록에 원문으로 보존한다 — 콜백 코드·실시간 타입·등록된
FID(`GetCommRealData`로 읽을 수 있는 것만, 서버 전체 제공 필드라는 뜻은 아니다)·도착 시각·
그 순간 실행 중이던 단계(`transition_step`)·활성 구독 문맥(정규장 쪽/애프터마켓 계획 코드,
참고 정보일 뿐 실제 출처 확정이 아니다)을 남긴다. 보존에는 상한(기본 2,000건)이 있고, 넘으면
누락 건수를 세고 전환 자체를 `callback_capacity_exceeded` 로 실패 처리한다 — 무한정 쌓지도,
조용히 버리지도 않는다.

**종료 조건.** 정규장의 15:35 종료는 계획 모드(애프터마켓 포함)에 적용하지 않으므로, 전환 뒤에도
끝나는 시점이 있어야 한다. `aftermarket_duration_seconds`(1~300초, 생성자에서 필수 검증)를
`subscribe_nxt` 성공 시각부터 재고, 넘으면 `_stats_worker` 루프가 종료를 요청한다. 정규장
`--duration-seconds` 시각을 이어받지 않는다 — 로그인 시각부터 쟀다면 전환 전에 이미 만료됐을
값으로 애프터마켓을 곧장 끊을 수 있기 때문이다. **20시까지의 장시간 애프터마켓 운영은 이
상한을 없애는 별도 결정 없이는 기본으로 켜지지 않는다.**
애프터 타이머가 있으면 아직 만료되지 않아도 정규장 duration 분기로 내려가지 않는다.
독립 NXT 계획의 raw-v2·1~300초 제한과 기본 정규장 종료 경로는 유지한다.

2026-09-18 오프라인 회귀에서는 세 결함을 먼저 재현한 뒤 수정했다. 주입 시계의 7.25초 공백,
이미 만료된 정규장 타이머가 있는 실제 통계 루프의 애프터 제한 종료, 모든 필수 기록 지점의
실패·각 단계 취소/시간 초과/예외, raw와 session_id 분리 및 닫힌 합성 raw 바이트 보존을 검사했다.
대상은 임시 디렉터리와 가짜 Qt/OCX뿐이며 운영 적용·실제 전환·무누락 검증은 아니다.

**2026-09-18 CLI/자동 트리거 추가 (가짜 OCX 검증, 운영 미적용).**

`kiwoom_universe_logger.py` 의 `__main__` 에 명시적 전환 모드 인자 그룹("애프터마켓 전환
(단일 로그인)")을 추가했다. 아무 `--aftermarket-*` 인자도 주지 않으면 지금까지와 동일하게
전환 없음이다 — 명시적으로 켤 때만 동작이 달라진다.

- `--aftermarket-nxt-codes`, `--aftermarket-list-origin`, `--aftermarket-list-verified-at`,
  `--aftermarket-nxt-eligibility-confirmed`, `--aftermarket-list-note`,
  `--aftermarket-market-profile`(기본 `nxt_aftermarket`) — 전환 후 구독할 애프터마켓 계획.
  목록 근거 필수 규칙은 `--nxt-codes` 계열과 같다(`aftermarket_plan_from_cli`).
- `--aftermarket-duration-seconds` — 전환 후 구간 자체의 종료 상한, 1~300초 필수.
- `--aftermarket-transition-at HH:MM:SS`(KST) 또는 `--aftermarket-transition-after-seconds N`
  — 전환을 실행할 시각 또는 정규장 구독 등록 완료 시각부터의 경과 초. **정확히 하나만** 받는다.
  (`resolve_aftermarket_transition_cli`, `collector/kiwoom/subscription_plan.py`)
- 상충 인자는 OCX(QApplication) 를 만들기 전에 `parser.error` 로 거부한다: 정규장 `--nxt-codes`
  계획과의 동시 지정, `--storage raw-v1`, `--managed-launch`, 트리거 두 개 동시 지정, 트리거
  없이 계획만 지정, 계획 없이 트리거만 지정, 시각 형식 오류, 상한/경과초 범위 오류.
- **트리거는 정확히 한 번만 실행한다.** 배경 스레드(`_stats_worker`, 1초 루프)는 시각/경과초
  조건을 확인해 `_aftermarket_transition_due` 플래그만 세운다 — OCX 호출은 절대 이 스레드에서
  하지 않는다. 실제 `run_aftermarket_transition()` 호출은 Qt 스레드의 `_poll_control`
  (200ms 타이머)이 그 플래그를 보고 한 번 부른 뒤 즉시 내린다. 이번 tick에 다른 종료 사유
  (`_shutdown_requested`)가 이미 잡혔거나 전환이 이미 시작됐으면(`self.transition is not None`)
  다시 부르지 않는다 — 실패 뒤 재시도·재로그인은 하지 않는다는 기존 계약을 그대로 지킨다.
  `run_aftermarket_transition()` 은 그 자체로도 생성 스레드가 아니면 `RuntimeError` 로 거부한다
  (OCX/ActiveX 는 만든 스레드에서만 안전하다).
- 명시적 전환 계획이 있는 실행은 15:35 정규장 자동 종료를 적용하지 않는다
  (`aftermarket_plan is not None`). **전환 예정 시각까지 정규장 입력·저장을 계속하고, 트리거가
  발동할 때 신규 입력 차단→구독 해제→정규장 drain/닫힘 확인을 시작한다.** 15:35에 먼저
  파일을 닫고 대기하는 방식이 아니다. 기본 정규장 모드의 15:35 종료는 그대로다.
  정규장 등록이 완료되지 않으면 통계 루프와 Qt 제어 루프 모두 자동 전환을 시작하지 않는다.
  경과 시간 트리거의 기준점은 정규장 구독 등록 완료 시각(`_subscribed_at`)이고 동일 monotonic
  시계로 잰다. 시각 트리거는 OS 로컬 시간대와 무관하게 KST를 명시해 비교한다.
  HH:MM:SS는 날짜 예약이 아니다. 등록 완료 당시 이미 해당 KST 시각 이상이면 다음 통계/Qt
  tick에 전환한다. 실제 발동에는 통계 주기와 Qt 응답 지연이 더해지며 정시 실행을 보장하지 않는다.
- 자동 전환 트리거와 정규장 `--duration-seconds`의 동시 지정은 로그인 전에 거부한다.
  전환 전 종료 타이머가 전환을 취소하는 모호함을 없애고, 전환 뒤에는 별도
  `--aftermarket-duration-seconds` 1~300초 상한을 적용한다. 트리거 없는 직접 호출 API는
  유지하되, 정규장 duration이 먼저 만료되면 기존 종료 요청이 우선한다.
- 애프터마켓 시장 구간 프로필이 잘못됐으면(`resolve_profile` 이 모르는 이름) 생성자에서
  Qt/OCX 를 만들기 전에 바로 거부한다 — 로그인까지 간 뒤 전환 단계에서야 실패하지 않는다.
- `parse_collector_args`는 운영 잠금·Qt 생성 없이 CLI 전체 조합을 검증한다. 독립 NXT의
  필수 제한 시간·저장 방식, 프로필만 단독 지정한 경우, 빈 코드/시각 인자도 거부한다.
  직접 생성 경로도 두 계획의 코드·프로필 및 HHMMSS의 분·초 범위를 Qt 생성 전에 검증한다.
- 기존 전환 함수(`SessionTransition`/`run_aftermarket_transition`) 자체는 고치지 않았다 —
  네 단계 순서·기록 계약·종료 조건은 위 절 그대로다. 이번 변경은 그 함수를 부르는 트리거만 새로 놓았다.
- 검증: `tests/test_subscription_plan.py` 전체 28개(CLI 조합 검증 신규 6개 포함),
  `tests/test_collector_plan_wiring.py` 전체 83개(트리거 경계·중복 방지·다른 스레드 거부·
  같은 tick 종료 우선·15:35 비적용·저장 분리·잘못된 프로필 거부 신규 약 15개 포함),
  모두 가짜 Qt/OCX + 주입 시계다. `.venv32` 로 `--help`/여섯 가지 CLI 거부 조합을 직접
  실행해 `parser.error` 메시지도 확인했다(자동화 테스트는 아니고 수동 스모크 테스트다).
  `tests/` 전체 1106 통과, 실패 0.

후속 독립 대조에서는 위 전체 실행을 반복하지 않았다. 추가 경계 회귀 7개를 수정 전 실패로
확인하고, 전환/계획 168개·CLI/KST 16개·기본 수집/저장 종료 53개(서로 다른 총 237개)를
작은 묶음으로 검증했다. 임시 저장소와 가짜 Qt/OCX만 사용했으며 실제 로그인은 하지 않았다.

**아직 구현하지 않은 것.**
- 실제 OCX 로그인에서의 CLI/트리거 실측. 전환 소요 시간·실제 수신 공백 길이·`SetRealRemove`/
  `SetRealReg` 의 실제 반환값·트리거가 실제로 정확한 시각/경과초에 발동하는지는 가짜 OCX와
  주입 시계로만 확인했다. 실제 NXT 대상 목록의 출처·확인 시각도 별도로 확보해야 한다(임의로
  확정하지 않는다).
- 20시까지의 장시간 애프터마켓 운영, 그리고 이 상한을 없애는 결정은 별도다.
- 별개 프로세스 재로그인은 전환 실패 시 사용자가 선택하는 수동 복구 수단으로 남아 있다.
  자동 재시도·자동 재로그인은 구현하지 않았다(요구하지도 않는다) — 트리거도 실패 후 다시
  시도하지 않는다.

### 다음 소규모 실측의 조건과 관측 항목 (2026-09-18 정리, 실행 전 준비만)

`nxt_probe`의 일반 실측 순서(위 § NXT 소규모 검증 준비)와 별개로, 이 세션 전환 기능
자체를 실제 로그인에서 켜기 전에 확인할 항목이다. 아래는 정리일 뿐이며 실행하지 않았다.

**전제조건(모두 확인 후에만 새 로그인을 시작한다).**

- 오늘 정규장 세션의 실제 종료 — writer_closed=true, finalization 존재, 실행기/본체/로그
  감시 OS 프로세스 부재를 모두 대조한다. 예약된 정리 시각이나 파일 존재만으로 종료를
  확정하지 않는다.
- 대상 세션이 `server: mock`이거나 애프터마켓 계획 없이 시작됐다면 그 프로세스를 그대로
  이어 전환 실측에 쓸 수 없다 — 전환 실측은 처음부터 `aftermarket_plan`을 넘겨 새로
  생성한 별도 프로세스로 한다.
- CLI 스위치(`--aftermarket-nxt-codes` 등, 위 § CLI/자동 트리거 절 참고)가 이제 있다 —
  `.venv32\Scripts\python -m collector.kiwoom.kiwoom_universe_logger --aftermarket-nxt-codes ...
  --aftermarket-transition-at HH:MM:SS --aftermarket-duration-seconds 60` 형태로 직접 실행
  가능하다. CLI 조합 검증은 가짜 OCX로 확인했을 뿐 실제 로그인 실측은 아직이다 — 이 실측에서
  처음으로 CLI 경로 자체(트리거 발동 포함)를 실제 OCX로 확인하게 된다.
- 출처·확인 시각이 명시된 소수 NXT 대상 목록, 독립 raw-v2 경로, 단일 OCX 로그인 유지,
  애프터 제한 1~300초를 쓴다. 추가 로그인이나 기존 수집기 중단·재시작은 하지 않는다.

**관측 항목(실행 중·직후).**

- 단계별(`unsubscribe_regular`/`finalize_regular`/`open_aftermarket`/`subscribe_nxt`) 시작·
  완료 시각(단조/UTC/KST)과 반환값/예외를 `session_transitions/<transition_id>.json`
  (schema v2)에서 확인한다.
- `finalize_regular`가 실제로 `state=="closed"`와 `finalization`을 모두 반환하는지 —
  예외 없음만으로 성공 처리하지 않는다.
- `reception_gap_sec`이 양쪽 실제 accepted 콜백 기준으로 계산되는지, 미수신·음수면 null로
  남는지.
- `SetRealRemove`/`SetRealReg`의 실제 반환값.
- 애프터 제한 시간이 `subscribe_nxt` 성공 시각부터 별도로 재는지 — 정규장 duration
  잔여값이 조기 종료를 유발하지 않는지 실제 통계 루프에서 확인한다.
- 전환 중 콜백 보존 건수와 2,000건 상한, `callback_capacity_exceeded` 미발동 여부.
- 전환 후 `SessionMonitor`가 애프터마켓 프로필의 새 인스턴스로 교체돼 정규장 침묵
  카운터를 이어받지 않는지.
- 종료 후 애프터마켓 writer 정상 닫힘, 정규장 원본 파일 무변경(닫힌 파일만 대조, 전체
  DB 스캔 금지).
- 트리거가 지정한 시각/경과초에 정확히 한 번만 발동하는지(로그의 "🔄 애프터마켓 전환 완료"
  는 한 번만 찍혀야 한다), 그 전에는 15:35 자동 종료가 걸리지 않는지, 실패 후 같은 실행에서
  트리거가 다시 발동하거나 재로그인을 시도하지 않는지.

**금지(준비와 실측 모두).** 실행 중 수집기 변경, 추가 로그인, 원본 raw/Daily_baseline/
old_data 수정, 운영 DB 전체 처리, CLI 자동 전환 확장. 실제 로그인·실행은 이 정리와
별도의 승인 범위에서만 한다.

수집 목적은 다음 날 아침 전략의 비교 기준 확보다. 정규장 개장 후 진입에는 당일 프리마켓을,
NXT 개장부터 진입에는 전 거래일 애프터마켓도 참고하려는 사용자 구상이다. 저녁 매매 요청은 아니다.
당일 아침만으로 충분한지, 전날 저녁을 추가하면 유용한지는 아직 검증하지 않았다.
전 거래일 애프터마켓과 당일 프리마켓을 날짜/시장 구간별로 구분하고 휴장일과 야간 공백을 보존한다.
연구에서는 같은 진입 조건에서 두 입력 범위를 비교하고, 결정 시점 이후 데이터가 들어가지 않도록 한다.

- 사용자는 장 종료 후 애프터마켓도 모으는 방안을 검토하도록 요청했다. 오늘 1종목 실측을
  근거로 제한된 NXT 대상 목록부터 확장하는 것을 제안한다. 전 종목 지원으로 일반화하지 않는다.
- 초기 운영안은 정규장 결과를 보존한 별도 애프터마켓 세션/파일이다. 단일 OCX 로그인 안에서
  정규장 저장을 마무리하고 구독/저장 세션을 전환하는 방식을 우선 검토한다. 세션 전환 지연과
  결손은 기록하고, 서로 다른 수집기의 중복 로그인은 피한다. 구현 전에는 자동 전환을 약속하지 않는다.
- 15:40~20:00 체결 수집과 저장 마무리를 별도 설정한다. 기존 15:35 종료만 늦춰서는 부족하다.
  현재 침묵 판정도 정규장 시간에 묶여 있어 시장별 활동 구간과 휴장/구간 전환을 함께 반영해야 한다.
  소수 저유동성 종목의 무체결을 연결 장애로 단정하지 않고 호가·연결 상태와 함께 관측한다.
- 후보 구독은 우선 `_NX`로 제한하고, 당일 거래 대상 목록의 출처/시각을 남긴다.
  `_AL` 통합과 중복 구독은 별도 비교 검증에만 사용한다. 원문 구독 코드·응답 코드·시장 구간은
  보존하되 접미사만으로 개별 체결 venue나 NXT 가드의 coverage를 confirmed로 올리지 않는다.
- 최초에는 소수 종목의 제한 시간 실행으로 저장 종료·메모리 추이·침묵 감시·디스크 증가를 확인하고
  이후 종목 수와 관측 시간을 늘린다. 20시까지의 장시간 안정성과 저장량은 아직 미측정이다.
- 오늘 기존 raw·정규장 운영 설정은 바꾸지 않는다. 자동 기동/예약이나 추가 로그인도 이번 검토에 포함하지 않는다.

2026-09-17 16:01:18~16:04:18 KST 실측: 모의서버에서 삼성전자 6자리 / `_NX` / `_AL`
등록 반환 모두 0, 실제 체결·호가 콜백을 받았고 접미사가 보존됐다. 각 60초 순차 구간의
체결/호가는 각각 9/195, 106/197, 124/197이며 원문 828건을 결과와 재대조했다.
이는 해당 종목·시간의 OCX 모의 접미사 수신 근거다. 전체 지원·무누락·개별 체결 venue 인증은 아니다.
원문은 `operations_state/nxt_probes/5561eb702ca2409fa4fe7ee2dd45159a/`에 보존한다.

기존 수집기 종료와 프로세스 부재 확인 후 사용자가 로그인할 수 있을 때만 실행한다.
`.venv32/Scripts/python.exe -X utf8 -u -m collector.kiwoom.run_nxt_probe --code 005930 --seconds 60`

- 기존 수집 잠금을 공유하고, 로그인 후 모의서버 플래그 1이 아니면 구독 전에 종료한다.
- 한 종목의 6자리 / `_NX` / `_AL` 코드를 순서대로 각 60초씩 등록한다. 운영 종료 시각 15:35는 적용하지 않는다.
- 로그인 대기는 180초, 창당 최대 300초, 전체 이벤트 상한은 50,000건이다. 주문 함수는 호출하지 않는다.
- `operations_state/nxt_probes/<id>/observations.jsonl`에 등록 응답, 콜백 코드/타입/데이터와 선택 FID 원문을 기록한다.
  모든 서버 제공 FID를 저장하는 것은 아니다. 구독 전환 직후 지연 콜백은 이전 구독에서 왔을 수 있다.
  기록의 subscription_code는 활성 관측 창이며 개별 콜백의 인과적 구독 출처 인증이 아니다.
- `result.json`은 관측 창 종료 요약이다. 등록 성공이나 0건만으로 지원/미지원·무누락을 인증하지 않는다.
  접미사는 라우팅 근거이며 개별 체결 venue와 전체 NXT coverage는 미확인으로 유지한다.


- 마감 후 공식 OCX 명세(CP949 가이드 FID 15)와 제한 원문 표본을 대조해 새 raw-v2의
  기본 방향 정책을 `signed_volume`으로 변경했다. 명시적 양수는 매수, 음수는 매도이며
  무부호·0·비정상 문자열은 미확인이다. 가격 오류 등 다른 품질 오류는 그대로 남긴다.
  가격은 `signed_magnitude`, venue는 `unknown`을 유지한다. 기존 raw는 덮어쓰지 않는다.
  합성 검증과 실제 새 세션 적용은 별개다. 표본에서 미확인 0건도 전체 무누락 인증이 아니다.

- raw-v2 세션 폴더 `resource_history.jsonl`에 60초마다 프로세스 메모리를 기록한다.
  현재/최대 working set과 commit, PID, UTC 시각을 보존하고 5,000줄로 제한한다.
  상태 JSON 자체에는 아직 메모리 요약이 연결되지 않았으며 종료 로그에 요약한다.
  낮은 사용량도 메모리 훼손·주소 공간 문제를 배제하지 않는다. commit은 전체 주소 공간 점유가 아니다.
- 구독 완료 후 장중 침묵 600초를 기본 종료 시도 기준으로 사용한다. 임시 운영 기본값이다.
  `SessionMonitor(silence_stop_sec=...)`에서 변경하고 None으로 끌 수 있지만 CLI 옵션은 아직 없다.
  장외는 제외하고 수신 재개 시 권고를 해제한다. 자동 재로그인/재기동은 하지 않는다.
- 종료 요청 전에 경고 구간 스택(최대 3회)과 별도로 종료 스택을 1회 기록한다.
  진단 실패는 종료 요청을 막지 않는다. 수신 결손 사유와 비정상 종료 코드를 유지한다.
  저장 완료와 수신 완전성은 별개이며 Qt/OCX 정체 시 종료·파일 닫힘은 보장하지 않는다.
- Windows 합성 52개 및 OCX 없는 32비트 메모리 조회 검증 완료. 실제 장애 대응 효과는 미검증이다.
