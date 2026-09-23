# Mock 전종목 FID 읽기 A-B-A — 진단 계약과 90초 tail 분리

[현재 인계](../HANDOFF.md) · [수신 계측](../docs/COLLECTOR_TELEMETRY.md) ·
[종료 계약](../docs/COLLECTOR_TEARDOWN.md) · [파이프라인](../docs/PIPELINE_MAP.md)

## 상태와 범위

이 계약은 PR #20의 진단 준비(`c263003da6be82cefe740d6b22896babfcc89cbc`),
PR #22의 안전성 보강(`f50bd55120d1a3f754fc135cd98c5e2eadf4438d`),
PR #23의 tail 분리(`b5ae402aac0088d27382aa54ff29a619fc078185`)에서 순차 검증됐다.
현재 통합 후보는 이 세 단계의 최종 누적 계약을 master 기준으로 검증한다.
PR #18의 운영 판단 계약이나 PR #21의 UI 구현을 가져오지 않는다.
**실제 시장 실행, master 병합, 로컬 동기화 또는 새 로그인 승인이 아니다.**

과거 로컬 canary/offline/dump 수치와 경로는 PR #21의
[고정 인계](https://github.com/Peter-jackson12/Stock/blob/4ce504dabca9baf37fd5c0a8dc062f484511c7ad/HANDOFF.md)를 따른다.
그 자료는 사용자 전달 로컬 보고이며 이번 GitHub 검토자가 원본을 직접 확인한 사실이 아니다.
기존 x86 offline 시험이나 완료된 PR #21 검사를 이 감사 때문에 재실행하지 않는다.

## 무엇을 비교하는 실험인가

구독 코드·화면·`SetRealReg`/`REAL_FIDS`는 유지하고, 콜백이 읽는 FID 집합을 바꾼다.
FULL의 체결/호가는 각각 6/41개, ESSENTIAL은 3/23개다.
같은 체결 T건·호가 Q건에 필요한 조회는 FULL `6T + 41Q`, ESSENTIAL `3T + 23Q`다.
실제 callback 혼합과 수신량을 모르면서 초당 조회 수를 고정값으로 적지 않는다.

**순수 COM 비용만 바꾸는 단일변수 실험은 아니다.** 미조회 FID는 None이므로
COM 호출뿐 아니라 반환 문자열 할당, Python dictionary 처리, JSON 인코딩,
저장 payload 크기, worker 처리 비용이 함께 달라질 수 있다. 비용이 항상 일정 비율로
감소한다고도 가정하지 않는다. sidecar의 기존 `independent_variable` 키는 명목상 조작
대상 이름이며 `pure_com_cost_experiment=false`와 `co_varying_costs`를 함께 읽는다.
`subscription_unchanged`는 설계 선언이지 실제 서버 구독을 독립 관측한 계측값이 아니다.

ESSENTIAL에서 정상화가 가능하다는 것은 원래 raw와 정보량·연구 용도가 같다는 뜻이 아니다.
이 진단의 목적은 **읽기 정책을 줄이는 복합 개입과 지연 패턴의 연관성**을 탐색하는 것이다.
한 번의 A-B-A 결과만으로 최초 native allocation 실패나 Qt/COM/GIL 병목 위치를 확정하지 않는다.

## 시계, callback 경계, 90초 이후

기준점은 `_register_all_universe()`가 반환한 뒤 설정한 `_subscribed_at`이다.
`FidReadAbController.current_phase()`는 각 callback의 FID 읽기 직전에
`time.monotonic` 계열 시계를 한 번 읽고 정책을 고정한다. source FID 시각을 phase 시계로 쓰지 않는다.
logger의 최초 callback 진입 시각과 이 정책 선택 시점은 같다고 가정하지 않는다.
FID를 읽는 도중 30/60초 경계를 넘더라도 그 callback 안에서는 목록을 바꾸지 않는다.

| 구독 완료 뒤 경과 시간 | 읽기 정책 | 해석 |
|---|---|---|
| 완료 전 | PRE_SUBSCRIPTION_FULL | 별도 집계; A1 표본에 섞지 않는다 |
| [0, 30)초 | A1_FULL | 명목상 첫 30초 |
| [30, 60)초 | B_ESSENTIAL | 명목상 가운데 30초 |
| [60, 90)초 | A2_FULL | 마지막 분석 구간 |
| 90초 이후 실제 입력 차단까지 | POST_90S_FULL | FULL 읽기는 유지하지만 분석 구간에서 제외 |

종료는 기존 stats 루프가 duration 초과를 보고 Qt 경로가 종료 요청을 처리하는 방식이다.
스케줄링 지연·긴 callback·native 정지 시 90초에 정확히 입력이 차단된다는 보장은 없다.
그래서 읽기 정책을 바꾸거나 강제 종료하는 대신 **90초 이후 callback을 별도 POST phase로 분리**한다.
A2 누적치는 FID 읽기 정책을 결정한 monotonic 시점이 [60,90)인 callback만 포함하고,
POST 누적치는 종료 요청 뒤 실제 입력 차단까지의 꼬리를 보존한다.

경계 직전에 시작해 FID 읽기 도중 90초를 넘긴 callback은 시작 시 결정된 A2에 남는다.
이는 callback 중간에 정책을 바꾸지 않는 기존 계약과 일치한다. 따라서 A2에서 post-90 시작 callback이
섞이는 문제는 제거되지만, 30초 phase마다 backlog를 초기화하지 않으므로 callback 수/30을
독립 정상상태의 순수 service rate로 해석하지 않는다. 자동 실행 시간을 늘리거나 강제 종료를 추가하지 않았다.

## 누적 backlog와 표본의 한계

30초마다 Python queue나 Qt/COM/provider backlog를 비우지 않는다. 등록 중 쌓인 입력과
A1의 잔여 backlog가 B로, B의 잔여 상태가 A2로 이어질 수 있다. 각 phase는 독립적으로
초기화한 정상상태 반복 실험이 아니며 A1과 A2의 같은 정책만으로 시간 추세를 제거할 수 없다.
Python queue=0은 native/provider 앞단 backlog=0의 증거가 아니다.

telemetry는 **이벤트 종류별 5초 슬롯에서 처음 만난 callback 하나**이며 종목별/무작위 표본이 아니다.
연속 표본의 종목이 다르면 공급자 시계 진행의 단일 시계열로 이어 붙이지 않는다.
같은 종목·종류·세션으로 비교해도 희소 표본과 초 정밀도, 데이터 제공자의 시계 의미는 남는다.
전체 feed의 평균/백분위 지연이나 시장 coverage를 이 표본으로 추정하지 않는다.

FID20/21과 수신 UTC의 차이는 미검증 동일 KST 날짜 가정의 signed 시계 차이다.
순수 network latency가 아니다. source progression과 lag slope는 같은 두 시계에서 도출하므로
독립된 두 증거로 세지 않는다. 작은 `processing_ns`, Python queue 0, offline 성공만으로
실제 Qt/COM/GIL/native 병목을 배제하지 않는다.

## 계측 정의와 실패

`processing_ns`는 logger가 준 callback 진입 시계부터 queue submit 반환까지다.
진단의 `fid_read_ns`는 그 진입점부터 FID 읽기·분기·계수 기록 뒤 표본 시계까지로,
순수 COM 서비스 시간이 아니다. `queue_submit_ns`는 그 표본 시계부터 submit 반환까지로,
관련 Python 처리/시계 호출의 교란도 포함한다. SQLite commit/drain 시간은 측정하지 않는다.
선택된 표본에서만 추가 timing clock을 읽으며, sampling/계수 처리 자체의 부하는 0이 아니다.

계측 시계의 예외/부적절한 값은 진단 실패로 격리하고 실제 FID 오류나 queue 오류로 바꾸지 않는다.
queue submit의 원래 예외를 finally의 계측 예외로 덮지 않는다. 표본 누락/진단 disabled 상태를
정상 계측 성공이나 시장 데이터 누락으로 해석하지 않는다.

FID 조회 자체가 실패하면 그 이전의 원문과 의도적 None을 같은 `callback_error`에 보존한다.
실패 뒤 아직 방문하지 않은 FID key는 만들지 않는다. `fid_call_count`/`fid_calls_by_phase`는
성공적으로 반환한 조회 수이며 `fid_attempts_by_phase`는 예외가 난 호출도 포함한다.
`fid_read_failures_by_phase`와 callback 계수는 읽기 시도 기준이지 accepted/committed 계수가 아니다.
계수 관측 자체가 실패하면 sidecar `diagnostic_error`를 남기며 오류를 감춘 완전한 집계로 쓰지 않는다.

## Mock 및 연구 입력 강제 차단

CLI/생성자 조합은 raw-v2, telemetry ON, 전체 universe, duration 90초만 허용한다.
`--codes`, NXT plan, aftermarket transition, managed launch, 명시적 OCX teardown 조합은 거부한다.
logger는 관측 서버 flag가 mock `1`이 아닐 때 backend/구독 전에 중단한다.
이때 정상적인 종료 요청과 **성공한 진단 실행**은 다르다.

직접 `LiveRawCapture` 생성도 mock-only를 확인하고 진단 controller가 있으면
`feed_scope=kiwoom_universe_fid_read_diagnostic`로 고정한다. 모순된 scope는 파일 생성 전에 거부한다.
이 값은 status뿐 아니라 raw manifest에 들어간다. 일반 OFF 기본 FID·payload·종료 경로는 유지한다.

[공유 정책](../collector/research_input_policy.py)의 강제 배제 지점:

- `run_raw_v2`: manifest를 읽고 원본 row/전략/result 생성 전에 거부한다. 연구 CLI도 이 경로를 쓴다.
- `run_research`: 예약 `input_provenance.raw_manifest`에 진단 scope가 있으면 iteration/output 전에 거부한다.
- `offline_worker.run_replay_job`: 실행 당시 다시 읽은 manifest로, 전체 사전 scan보다 먼저 거부한다.
- `qualify_raw_v2`: 원본 무결성/품질 검사는 유지하되 진단 scope이면 항상 연구 적격 false와 이유를 남긴다.

파일명 변경이나 외부 sidecar 누락으로 이 배제를 해제하지 않는다. header-only UI 계획 생성은
실행 승인이 아니며 계획이 있어도 워커에서 다시 거부한다. 저수준 raw reader는 진단 검사에 계속 쓸 수 있다.
일반 scope가 이 가드를 통과했다는 이유로 연구 적격을 인증하지 않는다. 호출자가 manifest/provenance를
삭제·위조한 임의 normalized stream의 출처까지 이 함수들이 인증할 수는 없다.

## sidecar와 종료 보존

초기 sidecar 쓰기 실패는 구독 전에 시작 실패로 처리하고 새 저장 worker 종료를 시도한다.
종료 시 counter snapshot 쓰기/replace 실패는 경고를 남기고 기존 sidecar와 raw 종료 경로를 보존한다.
기존 sidecar가 없거나 counter flush가 실패한 실행은 완전한 실험 근거로 사용하지 않는다.
sidecar schema는 tail 분리로 `fid_read_ab_test_v2`이며 A1/B/A2를
`analysis_window=true`, PRE/POST를 false로 표시한다. POST가 0이어도 90초 정시 종료를 인증하지 않는다.
sidecar counter snapshot은 저장 drain 전에 쓰이며 정상 drain/프로세스 종료 증거가 아니다.

저장 close, Qt quit, PID 부재, native 창 부재, lease free는 다른 사실이다.
새 PID/창/lease 조회나 자동 collector 제어는 추가하지 않았다. sidecar 파일 원자 교체도
전원 장애·native abort·모든 파일시스템 장애에서 완전 보존을 보장하지 않는다.

## 실제 결과 보기 전 사전등록한 A-B-A 판정 규칙

실제 Mock A-B-A 운영 결과를 보기 전에 `fid_read_ab_assessment_v1` 규칙을 코드와 회귀로 고정한다.
이 규칙은 원인 판정기가 아니라 **표본의 A-B-A 방향을 사후 변경 없이 기술하기 위한 pre-registration**이다.

- bounded analyzer 결과가 `CAPTURE_COMPLETE_ANALYSIS_READY`가 아니면 자동 A-B-A 판정을 하지 않는다.
- 1차 지표는 `fid_read_ns`다. callback 진입~FID 읽기·진단 bookkeeping 구간이며 순수 COM 시간이 아니다.
- 체결과 호가는 FID 수가 다르므로 절대 한 표본으로 합치지 않고 각각 따로 판정한다.
- A1/B/A2 각 phase·real_type에서 `fid_read_ns` 유효 표본이 **최소 3개** 있어야 한다.
  3개는 통계적 power 기준이 아니라 30초/5초 sampling에서 절반 이상의 phase coverage를 요구하는 고정 guardrail이다.
- 효과크기 임계값은 두지 않고 p-value/유의성 검정도 하지 않는다. 실제 결과를 본 뒤 threshold를 조정하지 않는다.
- 각 real_type의 중앙값 순서만 `B < A1 & A2`, `B > A1 & A2`, 그 외 mixed/tied로 분류한다.
- 체결·호가가 둘 다 `B < A1 & A2`이면 `PRIMARY_B_LOWER_BOTH_REAL_TYPES`,
  둘 다 반대면 `PRIMARY_B_HIGHER_BOTH_REAL_TYPES`, 나머지는 mixed 또는 not-assessable이다.
- `processing_ns`는 보조 corroboration일 뿐 1차 `fid_read_ns` 판정을 뒤집지 않는다.
- `queue_submit_ns`는 guardrail 기술만 하고, source clock difference·callback/30·resource history는 자동 판정에서 제외한다.
- PRE와 POST는 A-B-A 비교에서 제외한다. POST는 증거로 보존하지만 A2에 섞지 않는다.

`B_LOWER_THAN_BOTH_FULL`은 ESSENTIAL 구간에서 **표본화된 FID-read 경로 시간이 낮은 순서와 양립**한다는 뜻뿐이다.
COM 호출 감소가 원인이라고 증명하지 않으며 문자열·dict·JSON·저장 payload·worker 비용이 함께 달라진다.
`B_HIGHER_THAN_BOTH_FULL`도 fewer reads가 해롭다는 증명이 아니다. 시장 부하·누적 backlog·표본 종목 차이는 남는다.

실행 명령은 bounded analyzer와 같은 세션 근거만 사용하며 raw DB를 열지 않는다.

```powershell
C:\Projects\Stock\.venv32\Scripts\python.exe scripts\assess_fid_read_ab.py `
  --session-dir operations_state\capture_sessions\<session_id> `
  --expected-revision <실행한 정확한 SHA>
```

출력의 `analysis`와 `assessment`를 함께 보존한다. `assessment`는 causal verdict, research eligibility,
실시장 승인, Qt/COM/GIL/native 원인 규명이 아니다.

## 실행 후 bounded 결과 analyzer

실제 90초 실행 뒤에는 raw DB 전체를 열기 전에 세션 폴더의 작은 근거만 먼저 대조한다.
`scripts/analyze_fid_read_ab.py`는 다음 네 파일만 크기 상한을 두고 읽는다.

- `status.json`
- `fid_read_ab_test.json`
- `capture_telemetry.jsonl`
- `resource_history.jsonl`

raw DB의 `dataset_path`는 identity claim으로만 표시하며 파일을 열거나 COUNT/hash/SQLite scan하지 않는다.

```powershell
C:\Projects\Stock\.venv32\Scripts\python.exe scripts\analyze_fid_read_ab.py `
  --session-dir operations_state\capture_sessions\<session_id> `
  --expected-revision <실행한 정확한 SHA>
```

결과 분류는 수집/진단 근거의 완결성만 뜻한다.

- `CAPTURE_COMPLETE_ANALYSIS_READY` — clean closed + sidecar counter + A1/B/A2 telemetry + resource 근거가 일관됨.
- `CAPTURE_COMPLETE_EVIDENCE_LIMITED` — 저장은 닫혔지만 phase callback/telemetry/resource 근거가 일부 부족함.
- `CAPTURE_COMPLETE_DIAGNOSTIC_ERROR` — 저장은 닫혔지만 diagnostic_error 또는 FID read failure가 있음.
- `CAPTURE_INCOMPLETE` — writer close/pending/drop/error/finalization 중 clean close 조건이 깨짐.
- `CAPTURE_EVIDENCE_INVALID` — session/revision/scope/schema/counter accounting 같은 작은 근거끼리 모순됨.

이 분류는 research eligibility가 아니다. 진단 raw는 계속 `research_eligible=false`다.
analyzer의 telemetry 통계도 real_type별 5초 최초 callback 표본 요약일 뿐 feed-wide 분포가 아니다.
`clock_difference_seconds`는 동일 KST 날짜 가정의 signed 표본이며 network latency가 아니다.
A1/B/A2 callback 수를 30으로 나눠 순수 service rate로 부르지 않는다.

## 합성 검증과 다음 판단

[기존 helper 회귀](test_fid_read_ab_diagnostic.py), [독립 경계 회귀](test_fid_read_ab_safety.py),
[logger/저장 회귀](test_fid_read_ab_logger_safety.py)를 함께 실행한다.
FID 실패 위치 전수 대조는 6+41+3+23=73개 위치이며 native 오류 재현이 아니다.
통합 후보에서는 별도 stack 전용 workflow를 남기지 않고 이 세 회귀를 기존
`.github/workflows/ci.yml`의 집중 단계와 최종 전체 pytest에서 함께 검증한다.
집중/전체 실행은 중복이므로 합산하지 않는다.

남은 실제 실험 판단에는 현재 원격 ref, 공식 거래일/시장 구간, RUNBOOK/현재 CLI,
별도 종료 관측과 승인을 다시 대조해야 한다. A2/POST 분리는 분석 오염을 줄이는 합성 보강일 뿐
실제 시장 실행 승인이나 native 원인 확정은 아니다.
이번 패치/CI만으로 실행이나 병합을 승인하지 않는다.
