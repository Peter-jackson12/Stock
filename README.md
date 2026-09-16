# Stock — 틱 수집과 재현 연구

**처음에는 이 문서부터 읽으면 된다.** 목적에 맞는 상세 문서로 이동하고, 작업을 이어갈 때만
[HANDOFF의 현재 인계](HANDOFF.md)를 확인한다. AI 작업자는 [AGENTS.md](AGENTS.md)부터 시작한다.

## 지금 무엇을 만드는가

원본 체결·호가 이벤트를 수집하고 **틱 단위로 기존 NXT 돌파 전략의 재현 정확성**을 검증한다.
컨트롤 타워는 이 흐름을 한 화면에서 관리하며, 수집·화면·작업 워커는 독립 프로세스로 둔다.
LOB/초봉 변환은 레거시 회귀용이고 틱 재생의 선행 조건이 아니다.

- **운영 화면:** 로그 관측, 결과 조회 워커, 장외 재생 계획 저장·취소, 작업 이력.
- **수집 제어 2단계:** 프로세스/세션 식별, 정상 종료 명령·응답, 시간 초과/실패 처리 계약과 가짜 피어 검증.
- **남은 운영 연결:** 실제 raw v2 콜백·큐, 수집기 시작/종료 제어, 장외 재생 실행, 원격 인증, 주문 기능.

합성 테스트 통과를 운영 적용이나 실제 데이터 정확성 확인으로 해석하지 않는다.
현재 수집이 계속되는지는 시각이 붙은 최신 관측으로 확인한다.

## 문서 찾기

### 처음 이해하거나 사용하려면

1. **전체 데이터 흐름·왜 틱인가:** [ARCHITECTURE_TICK.md](ARCHITECTURE_TICK.md).
2. **컨트롤 타워 사용법·구축 순서·제어 계약:** [CONTROL_TOWER.md](CONTROL_TOWER.md).
3. **지금 어디까지 했고 다음에 무엇을 하는가:** [HANDOFF.md](HANDOFF.md)의 현재 인계.
4. **수집 시작/종료·메타데이터·일봉·fchart 확인:** [수집 실행 안내](docs/COLLECTION_RUNBOOK.md).
5. **닫힌 raw v2로 연구 실행·결과 해석:** [TICK_RESEARCH_RUNBOOK.md](TICK_RESEARCH_RUNBOOK.md).

### 개발하거나 AI에게 이어 맡기려면

- **AI 읽기 순서·작업 원칙:** [AGENTS.md](AGENTS.md). [CLAUDE.md](CLAUDE.md)는 같은 지침으로 안내한다.
- **코드 검토 순서·반례·미확인 사항:** [TICK_CROSS_REVIEW.md](TICK_CROSS_REVIEW.md).
- **rev.2의 D-4/D-5/D-6 수정 근거·기존 검증 기록:** [HANDOFF.md](HANDOFF.md)의 과거 기록.
- **기존 런 스토어·피처·전략/Broker 분리 배경:** [ARCHITECTURE_V2.md](ARCHITECTURE_V2.md).
- **더 이전 수집·리샘플링 의사결정:** [REFACTORING_PLAN.md](REFACTORING_PLAN.md).
- **문서 정리 전 README 보존본:** [과거 README](docs/archive/README_PRE_INDEX.md). 현재 실행 지시가 아니다.

이 문서와 AGENTS는 **길잡이**, HANDOFF는 **현재 상태와 인계**, 상세 문서는 **각 기능의 계약과 절차**를 담당한다.
과거 설계/완료 문구와 충돌하면 현재 사용자 결정, 담당 설계 문서, 코드와 검증 근거를 확인한다.
문서의 완료 표시만으로 현재 상태를 확정하지 않는다.

## 운영 화면 실행

저장소 루트 `C:\Projects\Stock`에서 기존 64비트 환경으로 실행한다.

```powershell
.\.venv\Scripts\python.exe -m streamlit run dashboard/app.py --server.address 127.0.0.1
```

기본 **운영 관리** 화면에서 상태와 작업을 확인한다. 왼쪽 **백테스트 분석**은 기존 런 비교다.
실제 수집기 시작/종료 버튼은 아직 연결하지 않았다. 세부 사용법은 [컨트롤 타워 §3](CONTROL_TOWER.md#3-지금-사용할-수-있는-흐름)을 본다.
폰에서는 현재 사용 중인 PC 원격 접속 안에서 열 수 있다. 별도 인터넷 공개 서버로 배포된 상태는 아니다.

## 코드와 데이터 위치

- `collector/`: 키움/KIS 수집, 원본 저장, 일봉/메타데이터. OCX 실행은 `.venv32`를 사용한다.
- `control_tower/`, `dashboard/`: 작업 관리·제어 계약·운영/분석 화면. `.venv`를 사용한다.
- `engine/`, `execution/`, `strategies/`: 이벤트 재생·가상 체결·전략.
- `scripts/`, `tests/`: 명시적 실행 도구와 검증.
- `sampledata/raw_ticks/`: 현재 raw v1. `sampledata/raw_ticks_v2/`: 새 형식용 경로, 운영 연결 미적용.
- `research_runs/`: 새 틱 연구 JSON. `runs/`: 기존 Trade/성과 분석 런. 두 결과 계약은 아직 별개다.
- `operations_state/`: 경량 작업 이력. 수집 DB와 분리하며 Git에 넣지 않는다.
- `sampledata/Daily_baseline`, `sampledata/old_data`: 보존 대상.

## 운영 전에 기억할 것

- 수집 중에는 코드·문서·작은 합성 테스트·짧은 관측을 진행한다. 재시작, 추가 OCX 로그인,
  전체 DB 스캔/변환/해시/백테스트와 부하 측정은 장외에서 검증한다.
- 기존 5건/-1.301%는 레거시 회귀 기준선이다. 새 틱 연구의 정답이 아니며 20260911 부재가 전체 작업을 막지 않는다.
- 현재 v1의 FID 14 방향 판정은 근거 오류다. 새 정규화기의 기본 방향은 unknown이며 실제 피드 확인이 남았다.
- 일봉 실제가 출처, shares의 시점성, 스냅샷 재실행 보존은 [수집 안내](docs/COLLECTION_RUNBOOK.md)와 담당 코드로 확인한다.
- 소스 하나의 실패는 그 소스를 쓰는 작업에만 적용한다. 과거 shares 백필 때문에 당일 스냅샷/raw 축적을 멈추지 않는다.

새 문서를 추가할 때는 위 **문서 찾기**에 목적과 읽을 시점을 연결한다. 상세 내용을 시작점 문서들에 중복 복사하지 않는다.
