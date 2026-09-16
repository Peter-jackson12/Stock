# AI 작업 시작점 — 저장소 전체에 적용

## 읽는 순서

1. `git status --short`로 사용자 변경을 확인한다.
2. [HANDOFF.md](HANDOFF.md)의 **현재 인계**에서 최신 상태·검증 범위·다음 작업을 읽는다.
3. [README.md](README.md)의 **문서 찾기**에서 이번 작업에 필요한 상세 문서만 선택한다.
4. 해당 코드와 테스트를 확인한다. 문서의 완료 문구만으로 구현/배포/실데이터 검증을 확정하지 않는다.

## 작업별 경로

- 운영 화면·작업 관리·수집 제어: [CONTROL_TOWER.md](CONTROL_TOWER.md).
- raw·순서·전략·체결·PIT 설계: [ARCHITECTURE_TICK.md](ARCHITECTURE_TICK.md).
- 틱 연구 CLI·결과 상태/한계: [TICK_RESEARCH_RUNBOOK.md](TICK_RESEARCH_RUNBOOK.md).
- 실제 수집·종료·opt10001·일봉 D-6·fchart 실측: [수집 안내](docs/COLLECTION_RUNBOOK.md).
- Codex/Claude 교차 검토와 반례: [TICK_CROSS_REVIEW.md](TICK_CROSS_REVIEW.md).
- rev.2 수정 근거와 이전 검증: HANDOFF의 과거 기록. 현재 진행 상태와 구분한다.
- 레거시 런/피처/Broker 설계: [ARCHITECTURE_V2.md](ARCHITECTURE_V2.md).
  [REFACTORING_PLAN.md](REFACTORING_PLAN.md), `docs/archive/`는 과거 의사결정/보존본이다.

## 유지할 결정

- 주 경로는 원본 틱 재생이다. LOB/초봉 변환을 기본 선행 작업으로 넣지 않는다.
- `sampledata/Daily_baseline`, 사용자 `sampledata/old_data`, 원본 수집 데이터는 보존한다.
- 거래 5건/-1.301%는 레거시 회귀 기준선이다. 새 틱 연구의 정답이 아니며 20260911 부재는 전체 게이트가 아니다.
- 장중 작업은 코드·문서·작은 합성 테스트와 짧은 관측으로 제한한다. 수집 재시작/교체,
  추가 OCX 로그인, 전체 DB 스캔/변환/해시/백테스트와 부하 측정은 장외 검증 범위다.
- 프로세스 PID·로그·파일 존재만으로 정상 종료/저장 완료/무누락을 판정하지 않는다.
- shares 과거 백필 소스의 실패가 당일 스냅샷/raw 축적을 막지 않게 작업별 선행 조건을 적용한다.
- 현재 수집기와 파일럿/가짜 피어/연구 프로토타입을 구분한다. unknown 값을 그럴듯한 상수로 채우지 않는다.
- 관련된 작은 테스트를 실행한다. 테스트 통과와 실제 OCX/데이터/성능 검증은 각각 보고한다.
- 변경은 로컬 커밋까지 정리한다. 현재 협업 방식에서 푸시는 사용자가 직접 한다.

## 문서 갱신 규칙

- 상세 계약은 담당 문서 한 곳에서 관리한다. README/AGENTS에는 경로와 짧은 요약만 둔다.
- 최신 상태·다음 작업·검증 결과는 HANDOFF의 **현재 인계**를 갱신한다. 과거 수치는 관측 시각과 함께 보존한다.
- 새 문서를 추가하면 README 문서 찾기에 목적과 읽을 시점을 추가한다.
- 설계 충돌은 현재 사용자 지시와 최신 결정 근거를 확인해 해결한다. 과거 문서가 현재 계획을 덮지 않게 한다.
- 인계 시 코드 변경, 합성 검증, 운영 미적용/실환경 확인 항목을 분리한다.
