# 컨트롤타워 세션 근거 표시 계약

[문서 인덱스](../README.md) · [현재 인계](../HANDOFF.md) · [운영 제어 계약](../CONTROL_TOWER.md)

이 문서는 [세션 reducer](../control_tower/session_assessment.py)와
[읽기 전용 표시 컴포넌트](../dashboard/session_assessment_view.py)의 계약이다.
PR #18의 일반 ChatGPT 운영 판단/시장 세션 계약과 별개인 PR #21의 표시 개선이다.
전체 컨트롤타워 재설계나 새 제어 권한을 구현한 것이 아니다.
기존 운영 화면에도 경고와 별도 상태가 있었으므로, 종전에 모든 상태를 healthy 하나로
판정했다고 주장하지 않는다. 이번 변경은 그 구분을 명시적 데이터 모델로 보강한다.

## 근거와 범위

운영 상태는 [bounded status reader](../control_tower/status.py)의 최대 64 KiB JSON으로 읽는다.
reader와 reducer는 같은 `validate_raw_capture_payload`를 사용해 schema, identity, 콜백 계수,
finalization을 대조한다. 유효한 producer claim이어도 실제 DB 내용 검증은 아니다.
프로세스/창/잠금 입력은 호출자가 이미 확보한 `RuntimeObservation`만 받는다.
새 OS 조회, 창 열거, lease 확보, SQLite/raw 조회, 자동 복구를 수행하지 않는다.

| 축 | 이번 어댑터가 말할 수 있는 것 | 자동으로 결론내리지 않는 것 |
|---|---|---|
| 저장 보고 | 유효한 recent active/draining 보고, 과거 terminal 보고 | 현재 OS 생존, 전체 원본 무결성 |
| 프로세스 관측 | full identity와 시각이 맞는 별도 관측값 | PID 숫자만 같은 다른 프로세스의 상태 |
| native 창 관측 | 별도 관측된 clear/error/window 상태 | 일반 OCX 창 존재가 종료 장애라는 주장 |
| 잠금 관측 | 별도 held/free 관측 | free이면 프로세스가 종료됐다는 주장 |
| 세션 콜백 진행 | 현재 어댑터에서는 미확인 | 공용 로그가 최근이면 콜백도 증가했다는 주장 |
| 원천 시각 신선도 | 현재 어댑터에서는 미확인 | queue 0, 최근 로그, 연결값 1을 freshness로 승격 |
| 연구 입력 적격성 | 알려진 diagnostic feed_scope의 배제 표시 | closed/CI 성공으로 연구 적격 판정 |

## 시간과 identity

저장 보고의 `observed_at_utc`로 recency를 다시 계산한다. 외부 dictionary의 recent 라벨을
그대로 믿지 않는다. 30초 초과 active/draining 보고는 마지막 보고 상태를 보존하되 현재 축은
미확인으로 둔다. 과거 closed/interrupted/failed는 역사적 보고로 남기며 시각을 함께 표시한다.
5초를 넘겨 미래인 보고는 시각 이상이다. 오류가 있는 status도 clean storage로 승격하지 않는다.

`RuntimeObservation`은 session_id/PID/시작 UTC/executable/revision/bitness/server/feed_scope/
dataset_path가 모두 같은 경우만 결합한다. 관측 시각은 현재 기준 0~30초여야 한다.
이 30초는 표시용 보수적 TTL이지 실제 시작 승인 정책이 아니다.
타 세션, PID 재사용, 접근 거부, 미래/과거 관측은 종료 인증으로 쓰지 않는다.
관측 객체를 만든 호출자의 신뢰성까지 이 reducer가 인증하는 것은 아니다.

`termination=exit_observed`는 유효한 process absent + native clear 관측의 요약이다.
이전 `verified_exited`라는 과도한 이름을 사용하지 않는다. fresh 관측이어도 TOCTOU는 남고
새 실행 허가가 아니다. lease는 termination 계산에 넣지 않는다.
process alive + 일반 OCX 창은 정상 수집에서도 가능하므로 process_alive다.
저장 closed 보고 뒤 process alive + 관련 창이 남으면 residual_native다.
active 상태의 Runtime 창은 native_error이며 종료 장애로 단정하지 않는다.

## 일일 로그와 세션을 섞지 않기

일일 공용 로그에는 여러 세션과 같은 누적 계수의 재출력이 섞일 수 있다.
`observe_collector().status=recent`는 마지막 로그 기록의 시각일 뿐이다.
로그 갱신 시각은 별도 caption으로 표시하고 세션 콜백 진행이나 source freshness에는
연결하지 않는다. 현재 어댑터는 이 두 축에 미확인을 표시한다.
나중에 콜백 진행을 연결하려면 동일 identity의 여러 계수/시각 관측을 비교해야 한다.

## 진단 데이터와 제어 권한

유효한 identity의 `feed_scope=kiwoom_universe_fid_read_diagnostic`는
`diagnostic_only`와 연구 입력 제외 경고로 표시한다. 시간 경과나 저장 종료로 해제되지 않는다.
기타 scope의 연구 적격성은 미확인이다. 임의 `research="eligible"` 승격 입력은 없다.
sidecar/실제 raw는 이 표시에서 열지 않는다. 별도 qualification과 연구 입력 가드는 여전히 필요하다.

새 컴포넌트는 버튼·작업 DB·subprocess를 사용하지 않으며 기존 수집/연구 버튼 조건을 바꾸지 않는다.
따라서 진단 표시 자체가 모든 실행 진입점의 연구 제외를 강제한다고 주장하지 않는다.
기존 관리 세션 heartbeat/조회/시작 제어는 그대로 남아 있으므로 전체 운영 화면을
아무 부작용 없는 순수 viewer라고 부르지도 않는다.

## 회귀와 남은 작업

[모델 회귀](../tests/test_session_assessment.py)는 schema/계수 오류, 최근 로그의 미승격,
정상 OCX 창, closed+free+Runtime 잔류, 오래된/미래 관측, full identity 충돌,
diagnostic 배제와 입력 비변경을 검사한다.
[컴포넌트 UI 회귀](../tests/test_session_assessment_ui.py)는 실제 표시값/경고를 대조한다.
기존 control-tower UI 회귀는 초기 표시가 워커/작업을 만들지 않는 경계 등을 계속 검사한다.

실제 native adapter, source-clock 분류 정책, 연구 진입점의 diagnostic 강제 거부,
PR #18/#20과의 통합, 운영 적용은 별도 작업이다. 합성 성공은 현장 검증이나 병합 승인이 아니다.
