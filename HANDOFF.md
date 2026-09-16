# 현재 인계 — 2026-09-16 / 제어 이력 영속화·복구

## 구현과 검증

- `control_tower/capture_history.py`: 별도 SQLite 제어 이력, 명령 반환 전 커밋,
  등록된 세션의 이력 재생·관리자 소유권 세대 교체. 저장 실패 시 메모리 상태도 유지한다.
- `lifecycle.py`: 관리자 재시작 시 monotonic 시각·생존 정보를 무효화한다.
  진행 중 세션/미완료 stop은 unknown부터 대조하며, 기존 명령의 자동 재전송은 차단한다.
  연속 보고와 identity/종료 표식 검증은 그대로 적용한다. 저장된 closed는 과거 종료 결과다.
- 합성 검증: `test_capture_history.py`, `test_capture_lifecycle.py`, `test_control_tower.py`
  **103개 통과(4.47초)**. 작은 임시 DB·가짜 피어만 사용했다.
  첫 실행은 Python 시작 권한 오류였고 승인된 샌드박스 외부 실행에서 통과했다.
- 에이전트 지침의 중복 경로·절차를 줄이고 범위 내 자율 수행을 명시했다.
  정리 근거: [OpenAI의 간결한 AGENTS 안내](https://learn.chatgpt.com/guides/best-practices), [자율 수행·지침 충돌 점검](https://developers.openai.com/api/docs/guides/latest-model).
  기존 인계는 [보관본](docs/archive/HANDOFF_20260916.md)에 관측 시각·검증 수치와 함께 보존했다.

## 운영 범위

- 화면은 로그 관측·결과 조회 워커·장외 재생 계획 저장/취소·작업 이력까지다.
- 제어 영속화/복구는 라이브러리와 합성 검증 단계다. UI·IPC·OS 프로세스·OCX에는 미연결이다.
  DB 소유권 교체는 외부 전송을 차단하는 기능이 아니다. 실제 전송 계층의 소유권 검증이 남았다.
- 수집기·운영 raw·Daily_baseline·old_data는 변경하지 않았다. 서버/추가 로그인도 기동하지 않았다.
  현재 수집 상태는 이번에 관측하지 않았다. 마지막 관측은 보관본의 10:44:18 기록이다.

## 다음 작업

1. **장중 독립 가능:** 가짜 전송 계층의 소유권 검증과 누락 revision 대조 계약.
   제어 저널이 길어질 때의 checkpoint/재생 범위 제한은 운영 연결 전 검토한다.
2. **장외 실환경 필요:** 제어 IPC·프로세스 어댑터, raw v2 큐/콜백 연결,
   OCX 필드·부호·venue·서버 공존, 작은 수집/종료 drain/오류/재기동 실측.
3. **위 검증 후:** 화면의 수집 시작/종료 → 종료 데이터 검사 → 장외 틱 재생 실행을 연결한다.
4. **독립 데이터 경로:** 실제가 출처·fchart 실측·메타데이터·shares 과거 백필은 각 작업의
   선행 조건으로 진행한다. 한 소스의 실패가 당일 스냅샷/raw 축적을 막지 않게 한다.

상세 계약: [CONTROL_TOWER §5](CONTROL_TOWER.md#5-제어-계약과-후속-운영-연결).
과거 rev.2 근거와 관측 이력은 [보관본](docs/archive/HANDOFF_20260916.md)을 필요할 때만 읽는다.
