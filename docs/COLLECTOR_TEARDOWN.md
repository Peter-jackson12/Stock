# 수집 종료 단계와 선택적 ActiveX 해제

## 목적과 경계

수신 결손 감지, 저장 워커 종료, ActiveX 해제, Qt 이벤트 루프 반환, OS 프로세스 종료는
서로 다른 상태다. 2026-09-22 수신 중단의 원인을 고쳤다고 주장하지 않는다.
원본 DB·raw-v2 schema·큐 수락/커밋/정규화·방향 및 venue 정책을 변경하지 않는다.
자동 로그인·재접속·재시작·운영 sidecar 처리 기능은 없다.

`--explicit-ocx-teardown`은 **기본 꺼짐**이다. 실제 키움 컨트롤의 종료 동작은 GitHub의
Python 대역만으로 검증할 수 없기 때문에, 기존 실행 명령에 몰래 활성화하지 않는다.
별도 실측 승인 뒤 실행할 때만 이 옵션을 검토한다. 이 문서는 로그인 승인이나 실행 지시가 아니다.
다중 세션 전환의 모든 워커 수명은 아직 이 옵션으로 검증하지 않았으므로 애프터마켓 전환과의
동시 사용은 CLI 및 생성자에서 Qt/OCX 생성 전에 거부한다. 기본 전환 동작은 유지한다.

## 순서

일반 종료의 순서는 입력 차단 → 타이머 중단 → 실시간 등록 해제 → 통계 스레드 종료 →
저장 마무리 → (옵션 활성화 시) 워커 종료 확인 → OCX clear → 로그 close → app.quit이다.

- OCX를 만든 Qt 스레드 밖의 종료/clear 호출은 상태 변경과 native 호출 전에 거부한다.
- 입력 수용 중이거나 종료 가드가 없거나 전환 중이면 `ocx_clear_blocked_active`를 기록하고
  직접 해제 호출을 거부한다. 활성 수집을 편의상 강제 중단시키는 API가 아니다.
- clear 전에 종료 가드를 세우므로 재진입한 login/real-data/poll/shutdown은 새 FID 조회나
  raw 입력을 하지 않는다. 명시적 clear는 성공/실패를 포함해 한 번만 시도한다.
- raw-v2 워커의 `wait(0)`가 false면 타임아웃을 종료로 간주하지 않고 clear를 보류한다.
  이 `deferred`/active 차단은 teardown 실패가 아니므로 그 사실만으로 exit_code를 2로 올리지 않는다.
  실제 clear 예외 또는 clear 후 non-null인 `failed`만 teardown 실패로 승격한다.
  main finally의 `_finish_process_resources`는 예외 경로에서도 입력·타이머를 먼저 차단하고
  기존 drain 대기를 수행한다. 이 예외 경로에서는 app.quit 요청보다 clear가 늦어질 수 있다.
- 초기화 실패로 컨트롤이 없거나 이미 null이면 불필요하게 clear하지 않는다.
- clear의 Python 예외 또는 clear 후 non-null은 해제 실패로 남기고 프로세스의 예정 종료 코드를
  비정상으로 둔다. 이미 closed인 저장 결과를 incomplete로 다시 쓰거나 원본을 보정하지 않는다.
- 일반 로그 close가 실패해도 그 실패를 기록하고 exit_code를 비정상으로 두되 `app.quit()` 호출은
  계속 시도한다. 로그 close 실패 때문에 Qt 이벤트 루프 종료 요청 자체를 건너뛰지 않는다.
- 네이티브 hang/abort를 Python try/except로 복구하지 않는다. DLL unload 완료나 OS 종료 성공을
  clear 반환·isNull·`qt_quit_returned`로 인증하지 않는다. 새 timeout/강제 종료는 없다.

## 단계 기록

기존 `logs/collector_fault_<UTC>_<PID>.log`에 `kind=collector_phase` JSON 행을 추가한다.
`CaptureDiagnostics`가 없는 직접 호출은 기록을 생략한다.

`shutdown_enter`, `unregister_enter/returned/failed`, `storage_finish_enter/returned`,
`ocx_clear_enter/returned/failed/deferred/disabled/absent/blocked_active`, `qt_quit_enter/returned`,
`event_loop_enter/returned/unwinding`, `main_finally_enter/returned`, `diagnostics_closing`을 구분한다.

enter만 있고 returned가 없으면 그 구간이 조사 대상이지만, 기록 실패와 프로세스 중단을
마커 하나만으로 구분할 수 없다. `storage_finish_returned.clean`은 저장 경로의 반환값이며
수신 무누락·연구 적격성·프로세스 정상 종료를 뜻하지 않는다.

최대 64행, 행당 2,048바이트다. 침묵 스택/종료 스택의 기존 예산과는 별개다. 쓰기 실패는
`phase_error`에 남고 단계 기록만 중단한다. 과거 기록을 잘라내거나 다시 쓰지 않는다.
flush는 수행하지만 강제 종료·전원 손실 시 원자적 보존이나 시간 상한을 보장하지 않는다.
진단 핸들러가 이미 활성화돼 있으면 기존 소유권을 유지하고 임의로 비활성화하지 않는다.

## 2026-09-22 증거와 해석

사용자가 제공한 세션 `39b5af8b45024458be9a3ae2a2259685`의 텍스트 로그·status 사본:
마지막 콜백 10:23:47 KST, 보호 종료 요청 10:33:47, 저장 마무리 보고 10:33:48.
accepted=committed=11,198,913, final_seq=11,212,670, data_quality=unverified다.
본 PR은 운영 DB를 열거나 해시·복사하지 않았다. 작은 로그 사본 대조는 whole-file 검증이 아니다.

후속 사용자 관측은 **16시경 오류창이 이미 있었고 17:07경 직접 닫았다**는 것이다.
17:07의 Application Error/WER 기록은 이 닫기 시각과 부합한다. 최초 팝업 시각으로 쓰지 않는다.
정확한 최초 표시 시각과 원인 native 스택은 없다. **팝업이 10:33:48 이후에 발생했다는
하한도 확인되지 않았다.** 저장 종료 뒤 파괴 과정의 오류는 후보이지 확정 사실이 아니다.

## 공식 참고와 합성 검증의 한계

- [Qt 5 QAxWidget](https://doc.qt.io/archives/qt-5.15/qaxwidget.html#clear): clear는 ActiveX를 종료한다.
  destructor의 자동 정리와 명시적 정리의 시점을 구별한다.
- [Python 3.10 faulthandler](https://docs.python.org/3.10/library/faulthandler.html):
  파일을 핸들러 사용 중 열어 두어야 한다. 모든 C++ 예외를 포착한다는 계약은 없다.
- [CPython 3.10.11 구현](https://github.com/python/cpython/blob/v3.10.11/Modules/faulthandler.c):
  Windows 처리기에서 MSC `0xE06D7363`을 제외한다. fault 로그가 없다고 조기 close를 단정하지 않는다.

`tests/test_ocx_teardown.py`는 대역 및 작은 합성 저장으로 순서·중복/재진입·실패를 검증한다.
실제 32비트 Python/키움/Qt/보안 모듈 조합, native 예외 재현, 수신 성능, 오류 재발 방지는 미검증이다.
로그·덤프를 GitHub에 업로드하거나 전원을 끄는 운영 조치도 이 PR의 범위가 아니다.
