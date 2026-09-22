# 선택적 수집 진단: 폴링과 기존 FID 시각

## 범위

`--capture-telemetry`는 기본 꺼짐이며 단일 raw-v2 세션에만 명시적으로 활성화한다.
raw-v1 및 애프터마켓 세션 전환과의 조합은 Qt/OCX 생성 전에 거부한다.
[종료 계약](COLLECTOR_TEARDOWN.md)의 명시적 ActiveX 해제 옵션과 독립이다.
두 옵션을 한꺼번에 켜야 하는 것이 아니다. 문서·PR·CI는 실제 로그인/수집 승인이 아니다.

이 기능은 원인을 좁힐 관측을 추가할 뿐, 자동 재연결·재시작·종목 축소·오류 보정은 하지 않는다.
raw schema/manifest/방향 정책/venue/큐 수락/커밋/연구 적격성은 변경하지 않는다.
`status.json`의 `control_heartbeat=false`도 그대로다. 별도 관측을 관리 채널 인증으로 승격하지 않는다.

## 출력

로그인 뒤 생성된 해당 세션 evidence 디렉터리에 `capture_telemetry.jsonl`을 새로 만든다.
`xb`로 열며 이미 같은 이름이 있으면 덮어쓰지 않고 진단만 비활성화한다.
header는 session_id, code_revision, 표본 간격과 저장 예산을 담는다.
이후 `sample_batch`에는 UTC 관측 시각, monotonic ns, 폴링 상태, 선택된 콜백 표본이 들어간다.

### Qt 폴링

기존 `_poll_control` 진입/복귀 횟수, in_flight, 마지막 진입·복귀 ns/경과 시간과
기존 `GetConnectState()` 호출이 실제로 반환한 값·자료형·poll_id를 보존한다. 추가 OCX 호출은 없다.

이 값은 **마지막 관측**이며 전체 호출 이력이 아니다. connection의 poll_id/observed_ns가
오래되면 최신 폴링에서도 같은 값이었다고 추정하지 않는다. 진입 후 native 호출이 돌아오지
않으면 stats 스레드의 다음 flush가 오래된 return과 in_flight를 관측할 수 있다.
그러나 GIL/프로세스 전체 정지로 stats도 못 돌면 새 로그는 보장되지 않는다.
`last_duration_ns`는 제어 폴링 전체(상태 기록·종료 처리 포함)의 시간이지 OCX 함수만의 시간은 아니다.
연결 값 1은 시장 이벤트 무누락·최신성·정상 수신 인증이 아니다.

### 콜백 표본

체결/호가 종류별로 **5초마다 처음 만난 콜백 한 건**을 선택한다. 종목별 표본이나 무작위 표본이
아니며, 전체 지연 분포의 평균/백분위수나 시장 coverage를 추정하는 자료로 쓰지 않는다.
이미 원본 저장을 위해 읽은 FID20(체결) 또는 FID21(호가)만 가져온다.
code, 원문 시각, 콜백 진입 UTC/perf ns, 큐 submit 반환까지의 처리 ns, accepted를 담는다.
표본 처리를 위해 추가 clock 호출은 선택된 표본에서만 한다. 추가 GetCommRealData 호출은 없다.

원문 HHMMSS와 콜백 UTC의 차이는 **같은 KST 날짜라는 미검증 가정**으로 계산한 signed 시계 차이다.
시계 동기화·장 구간·초 정밀도·날짜 모호성이 있어 순수 네트워크 지연이나 feed latency 보정값이 아니다.
음수나 자정 경계를 0으로 clamp하거나 +/-24시간 보정하지 않는다. 잘못된/잘린 시각은 null+이유다.
이 진단 숫자는 raw의 received_ns/exchange_ts, 정규화 값, 연구 입력 승인에 사용하지 않는다.

## 부하와 실패 보존

콜백에서 JSON 생성·datetime 파싱·파일 I/O를 하지 않는다. 종류당 제한 표본만 작은 버퍼로 옮긴다.
버퍼 상한은 64개이며 nonblocking 잠금 경합/가득 참은 진단 표본만 버리고 별도 계수를 증가시킨다.
`diagnostic_samples_dropped`는 원본 콜백 drop과 전혀 다른 값이다.

stats의 기존 1초 루프에서 flush 여부를 확인하고 보통 60초에 한 번 JSONL을 쓴다.
침묵 종료 요청/정리 시에는 강제 flush가 추가될 수 있다. 기존 원시 파일을 열거나 해시하지 않는다.
파일 8 MiB, 1,024 batch, 단일 행 64 KiB의 상한을 적용한다. 이전 증거를 자르거나 회전 삭제하지 않는다.
쓰기 실패/예산 초과는 진단만 중단하며 수집 상태·raw의 complete/품질 판정은 바꾸지 않는다.
부분 write/강제 종료/전원 장애의 마지막 행과 아직 flush하지 않은 표본은 유실될 수 있다.
일반 flush는 디스크 I/O의 실시간 상한을 보장하지 않는다. 저부하는 설계 목표이며 실부하 벤치마크 결과가 아니다.

최종 flush의 예외도 기록하되 파일 닫기 시도를 건너뛰지 않는다. 첫 진단 오류를 보존하며,
파일 close 자체가 실패하거나 다른 flush가 잠금을 소유하면 closed 성공으로 표시하지 않는다.
이는 모든 OS/파일시스템 장애에서의 닫힘 보장이 아니다. 실패한 최종 표본을 성공 기록으로 꾸미지 않는다.

`telemetry_bind`/`telemetry_summary` phase는 진단 상태의 제한된 단서다. 파일 누락이나 비어 있는
표본을 시장 이벤트 없음으로 해석하지 말고 활성화 여부·오류·한도·종료 상태와 함께 읽는다.

## 검증

`tests/test_capture_telemetry.py`는 고정 시계·대역·작은 합성 raw로 검사한다.
켜짐/꺼짐의 동일 입력 packet과 payload stream hash, 6개 체결 FID/41개 호가 FID 호출 수,
원본 예외 전파·방향 미확인 보존·실제 폴링 경로·파일 충돌·예산·기록 실패를 확인한다.

`tests/test_collector_diagnostics_integration.py`는 두 옵션의 4가지 조합, clear 중 재진입,
반복 종료, 진단 메서드 예외, 이벤트 루프 반환/예외, clear 실패와 raw 종료 상태 보존을 함께 검사한다.
이 통합 검사의 최초 실행 CI #156에서 flush 예외 주입 시 close가 생략되는 반례가 나왔다
(1 failed / 53 passed). `close`의 최종 flush 경계를 보강하고 동일 회귀를 유지했다.
그 실행은 성공으로 계산하지 않는다. 수정 후 정확한 HEAD/run/job 및 결과는 PR #17에서 확인한다.

GitHub의 Windows/Python 3.14 테스트와 3.10 문법 검사는 사용자 32비트 키움/Qt/보안 모듈의
native 실행, 실제 처리량, 2026-09-22 장애 원인 또는 재발 방지를 인증하지 않는다.
초기 실측은 별도 승인 아래 기존 수집 제약을 유지해야 하며 여기서 실행하지 않는다.
