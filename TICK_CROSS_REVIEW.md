# Claude / Codex 틱 파이프라인 교차 검증

완료 문구 대신 코드와 작은 반례로 검증한다. 운영 DB 재생·수집기 교체·TR 요청은 장중 검토에 포함하지 않는다.

## 즉시 우선 확인

1. `collector/kiwoom/kiwoom_universe_logger.py`는 FID 14를 방향 판정에 사용한다.
   [키움 OpenAPI+ 가이드 1.7 §8.2](https://download.kiwoom.com/web/openapi/kiwoom_openapi_plus_devguide_ver_1.7.pdf)
   에서 14는 누적거래대금, 15는 거래량/체결량이다. 기존 raw의 is_buy는 원천 방향 인증 값이 아니다.
   운영 프로세스와 기존 기록은 변경하지 않았다. 새로운 `tick_normalizer.py`는 14를 방향 판정에 쓰지 않는다.
2. OpenAPI+ 가이드의 해당 표만으로 15의 부호 의미까지 확정하지 않는다.
   공식 REST 문서의 부호 설명을 OCX 피드 검증으로 대체하지 않는다.
   변환기 기본 방향 정책은 unknown이고, signed_volume은 실제 피드 확인 뒤 명시적으로 선택한다.
3. 가격 부호 제거도 명시적 정책이다. 원문을 보존하고 positive_only/signed_magnitude를 기록한다.
   일반 호가 검증기에서 음수를 무조건 abs 처리하는 것은 금지한다.

## 작업에 맞춰 선택할 코드와 반례

- 수집 제어 계약: `control_tower/lifecycle.py`, `tests/test_capture_lifecycle.py`.
  PID가 재사용되거나 과거 세션 응답이 도착해도 대상이 섞이지 않는가? 중복 명령이 stop을 반복하는가?
  큐/in-flight/커밋 불일치와 저장 오류를 정상 종료로 표시하는가? stale/시간 초과를 실패 확정 또는 재시작으로 바꾸는가?
  이력/복구는 `control_tower/capture_history.py`, `tests/test_capture_history.py`를 함께 본다.
  커밋 전 명령이 노출되는가? 재시작 후 과거 heartbeat/시간 제한이 되살아나는가?
  누락 보고·PID 재사용·저장 실패·이전 관리자 쓰기를 거부하는가? 실제 프로세스/IPC는 미연결이다.
  `test_capture_dispatch.py`, `test_capture_reconciliation.py`: 전송 의도 저장 실패 후에도 sender가
  호출되는가? 관리자 교체 직전의 명령이 전송되는가? 과거 보고 묶음으로 현재 생존을 오판하는가?

- 수신 순서: `engine/tick_ordering.py`, `tests/test_tick_receive_order.py`.
  동일 초 호가 A → 체결 → 호가 B에서 체결이 B를 보지 않는가? 미래 suffix 변경이 과거를 바꾸는가?
- raw v1: `engine/raw_v1_reader.py`의 이전 초 정책이 연구 가정으로 표시되는가?
  서로 다른 테이블 rowid를 공통 수신 순번으로 위장하지 않는가?
- raw v2: `collector/raw_v2.py`, `tests/test_raw_v2.py`.
  append/commit/finish 실패, 미완료 파일, 체크섬 오류, 시퀀스 누락에서 정상 재생이 차단되는가?
  새로운 세션을 기존 세션에 무조건 이어 붙이지 않는가? 같은 이름의 파일을 덮지 않는가?
- 큐/저장 워커: `collector/kiwoom/queued_capture.py`, `tests/test_queued_capture.py`.
  콜백 객체 변경·큐 초과·in-flight 저장 오류·종료 경계·파일 닫기 실패를 보존하는가?
  접수 콜백 수와 저장된 raw seq를 혼동하거나 wait 시간 초과를 완료로 표시하는가?
- OS/종료 보고: `control_tower/windows_process.py`, `collector/kiwoom/queue_control.py`,
  `tests/test_windows_process.py`, `tests/test_queue_control.py`, `tests/test_capture_ipc.py`.
  PID·생성 시각·실행 파일·비트 수 불일치로 전송이 차단되는가? 프로세스 종료 뒤 버퍼 보고를
  생존으로 오인하거나, 유효 최종 보고를 버리는가? 파싱 오류 콜백 수와 raw 수를 구분하는가?
  보고 중단·마감 대기 시간 초과·파일 닫기 실패에서 closed가 잘못 생성되는가?
- 피어 보고 복구: `control_tower/report_journal.py`, `tests/test_report_journal.py`.
  송신 전 저장 실패 시 전송을 막는가? 송신 실패 후 보고가 남는가? 페이지의 잘못된 후속 상태를
  부분 반영하는가? 과거 receiving/빈 페이지를 생존으로 오인하거나 stop을 재전송하는가?
  identity/cursor/순번/메시지 크기 제한과 다른 소켓으로 대조하는 실제 자식 테스트를 확인한다.
- 변환: `collector/kiwoom/tick_normalizer.py`, `tests/test_tick_normalizer.py`.
  원문·결측·정책·오류가 남는가? 제공자 시각과 UTC/KST 수신 시각을 혼합하지 않는가?
- 체결: `execution/tick_simulator.py`, `tests/test_tick_simulator.py`.
  매수/매도/취소 지연, 같은 시각 타이머 우선, 잔량 재사용, 현금/보유량 제한, 마감 경계를 확인한다.
  새로운 호가마다 잔량을 새로 제공하는 가정은 실제 주문 큐/시장 충격 검증이 아니다.
- 전략: `strategies/nxt_breakout/tick_research.py`, `tests/test_nxt_tick_research.py`.
  미래 정규장 시가 참조, partial entry 취소 후 exit 수량, top3 없는 OBI, 과열/쿨다운을 점검한다.
- 실행/결과: `engine/tick_research_run.py`, CLI, 결과 조회 도구.
  실패가 정상 결과로 저장되는가? 미청산 현금을 수익/총자산으로 표시하는가?
  선택 종목 없음, 빈 입력, 체결 없음, 실행 중을 구분하는가?

## 알려진 미완료 범위

- 운영 화면/작업 관리: `control_tower/`, `dashboard/control_tower.py`,
  `tests/test_control_tower.py`, `tests/test_control_tower_ui.py`를 [설계](CONTROL_TOWER.md)와 대조한다.
  동일 요청이 중복 실행되는가, 대기 취소와 선점이 경쟁하는가, 다른 owner가 완료를 기록하는가,
  계획만 저장한 재생이 실제로 실행되는가, stale 로그를 수집 종료로 오인하는가를 점검한다.
  현재 별도 워커는 JSON 조회만 실행한다. 수집기 제어/장외 실행/예약 복구는 후속이다.
  raw v2 헤더 조회 제한은 전체 체크섬 검증을 대신하지 않는다. AppTest 통과는 실제 운영 배포 인증이 아니다.

- 실제 OCX 콜백에서 순번·단조 시각 부여, 원문 추출·큐 연결은 아직 미적용이다.
- 접속/파싱 오류 등 제어 이벤트는 prototype_2의 공통 seq 기록과 연구 실패 처리로 추가했다.
  동기식 CaptureSession은 callback_error도 보존한다. 제한 큐/워커는 합성 연결했지만 실제 OCX 연결은 없으므로
  실시간 제어 이벤트가 이미 수집되고 있다고 가정하지 않는다. 이 클래스를 Qt 콜백에 직접 붙이지 않는다.
- 실제 피드의 방향·가격 부호/venue·세션 범위, 시장별 호가단위 및 비용은 별도 확인 대상이다.
- FULL/WAL 설정의 실제 지연·종료 drain·강제 종료 내구성은 장외 측정이 필요하다.
- 실제 하루 재생, raw 대조, 전략 성과/대시보드 연결은 합성 테스트 통과로 완료 처리하지 않는다.
