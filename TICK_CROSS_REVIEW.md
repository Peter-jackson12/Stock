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

## 코드 검토 순서와 반례

- 수신 순서: `engine/tick_ordering.py`, `tests/test_tick_receive_order.py`.
  동일 초 호가 A → 체결 → 호가 B에서 체결이 B를 보지 않는가? 미래 suffix 변경이 과거를 바꾸는가?
- raw v1: `engine/raw_v1_reader.py`의 이전 초 정책이 연구 가정으로 표시되는가?
  서로 다른 테이블 rowid를 공통 수신 순번으로 위장하지 않는가?
- raw v2: `collector/raw_v2.py`, `tests/test_raw_v2.py`.
  append/commit/finish 실패, 미완료 파일, 체크섬 오류, 시퀀스 누락에서 정상 재생이 차단되는가?
  새로운 세션을 기존 세션에 무조건 이어 붙이지 않는가? 같은 이름의 파일을 덮지 않는가?
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

- 실제 OCX 콜백에서 순번·단조 시각 부여, 원문 추출·큐 연결은 아직 미적용이다.
- 접속/파싱 오류 등 제어 이벤트는 prototype_2의 공통 seq 기록과 연구 실패 처리로 추가했다.
  동기식 CaptureSession은 callback_error도 보존한다. 실제 OCX/큐 연결은 없으므로
  실시간 제어 이벤트가 이미 수집되고 있다고 가정하지 않는다. 이 클래스를 Qt 콜백에 직접 붙이지 않는다.
- 실제 피드의 방향·가격 부호/venue·세션 범위, 시장별 호가단위 및 비용은 별도 확인 대상이다.
- FULL/WAL 설정의 실제 지연·종료 drain·강제 종료 내구성은 장외 측정이 필요하다.
- 실제 하루 재생, raw 대조, 전략 성과/대시보드 연결은 합성 테스트 통과로 완료 처리하지 않는다.

변경 시 HANDOFF와 아키텍처 문서의 구현 상태를 함께 갱신한다. 푸시는 사용자가 직접 한다.
