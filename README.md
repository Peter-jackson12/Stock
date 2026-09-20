# Stock — 틱 수집과 재현 연구

**처음에는 이 문서부터 읽으면 된다.** 목적에 맞는 상세 문서로 이동하고, 작업을 이어갈 때만
[HANDOFF의 현재 인계](HANDOFF.md)를 확인한다. AI 작업자는 [AGENTS.md](AGENTS.md)부터 시작한다.

## 지금 무엇을 만드는가

원본 체결·호가 이벤트를 수집하고 **틱 단위로 기존 NXT 돌파 전략의 재현 정확성**을 검증한다.
컨트롤 타워는 이 흐름을 한 화면에서 관리하며, 수집·화면·작업 워커는 독립 프로세스로 둔다.
LOB/초봉 변환은 레거시 회귀용이고 틱 재생의 선행 조건이 아니다.

- **운영 화면:** 소규모 수집 제어·상시 응답·중단 대조, 결과 조회, 장외 검사·재생·1회 예약.
- **수집 제어 계약:** 종료 명령·이력·관리자 복구, OS 프로세스 식별·제한 시간 IPC·raw v2 종료 보고의 합성 검증.
- **운영 수집기:** 새 실행의 기본 raw v2 콜백·큐·종료 보고와 상태 관측 연결. 실피드 부하/품질 검증은 남아 있다.
- **운영 보호:** 제어 이력 백업·회전/checkpoint, 기본 localhost, 외부 접속 시 OIDC·운영자 허용 목록.
- **남은 검증/확장:** 실피드 필드·부하, 공급자 인증/실제가 검증, 재부팅 실측, 주문 기능.
  원격 사용은 기존 PC 원격 접속으로 확정했다. 별도 IdP/TLS 연결은 외부 공개가 필요할 때의 선택 확장이다.

합성 테스트 통과를 운영 적용이나 실제 데이터 정확성 확인으로 해석하지 않는다.
현재 수집이 계속되는지는 시각이 붙은 최신 관측으로 확인한다.

## 문서 찾기

### 처음 이해하거나 사용하려면

1. **전체 데이터 흐름·왜 틱인가:** [ARCHITECTURE_TICK.md](ARCHITECTURE_TICK.md).
2. **컨트롤 타워 사용법·구축 순서·제어 계약:** [CONTROL_TOWER.md](CONTROL_TOWER.md).
3. **지금 어디까지 했고 다음에 무엇을 하는가:** [HANDOFF.md](HANDOFF.md)의 현재 인계.
4. **수집 시작/종료·장외 저장 검사·메타데이터·일봉·fchart 확인:** [수집 실행 안내](docs/COLLECTION_RUNBOOK.md).
5. **닫힌 raw v2로 연구 실행·결과 해석:** [TICK_RESEARCH_RUNBOOK.md](TICK_RESEARCH_RUNBOOK.md).
6. **첫 실데이터 시험 백테스트까지 남은 일:** [체크리스트](BACKTEST_TODO.md).

### 개발하거나 AI에게 이어 맡기려면

- **AI 읽기 순서·작업 원칙:** [AGENTS.md](AGENTS.md). [CLAUDE.md](CLAUDE.md)는 같은 지침으로 안내한다.
- **2026-09-18 커밋 한국어화 전후 ID:** [커밋 대응표](docs/COMMIT_ID_MAP_20260918.json). 과거 문서·수집 리비전 조회에 사용한다.
- **코드 검토 순서·반례·미확인 사항:** [TICK_CROSS_REVIEW.md](TICK_CROSS_REVIEW.md).
- **rev.2의 D-4/D-5/D-6 수정 근거·기존 검증 기록:** [과거 인계](docs/archive/HANDOFF_20260916.md). 현재 실행 지시가 아니다.
- **상시 응답 추가 전 운영 연결·실측 기록:** [2026-09-16 이전 인계](docs/archive/HANDOFF_20260916_PRE_HEARTBEAT.md). 현재 상태는 HANDOFF를 본다.
- **기존 런 스토어·피처·전략/Broker 분리 배경:** [ARCHITECTURE_V2.md](ARCHITECTURE_V2.md).
- **더 이전 수집·리샘플링 의사결정:** [REFACTORING_PLAN.md](REFACTORING_PLAN.md).
- **문서 정리 전 README 보존본:** [과거 README](docs/archive/README_PRE_INDEX.md). 현재 실행 지시가 아니다.

## 운영 화면 실행

저장소 루트 `C:\Projects\Stock`에서 기존 64비트 환경으로 실행한다.

```powershell
.\.venv\Scripts\python.exe -m streamlit run dashboard/app.py --server.address 127.0.0.1
```

기본 **운영 관리** 화면에서 상태와 작업을 확인한다. 왼쪽 **백테스트 분석**은 기존 런 비교다.
수집 버튼은 이 화면에서 새로 만든 소규모 세션만 제어한다. 세부 사용법은 [컨트롤 타워 §3](CONTROL_TOWER.md#3-지금-사용할-수-있는-흐름)을 본다.
폰에서는 현재 사용 중인 PC 원격 접속 안에서 열 수 있다. 별도 인터넷 공개 서버로 배포된 상태는 아니다.

## 코드와 데이터 위치

- `collector/`: 키움/KIS 수집, 원본 저장, 일봉/메타데이터. OCX 실행은 `.venv32`를 사용한다.
- `control_tower/`, `dashboard/`: 작업 관리·제어 계약·운영/분석 화면. `.venv`를 사용한다.
- `engine/`, `execution/`, `strategies/`: 이벤트 재생·가상 체결·전략.
- `scripts/`, `tests/`: 명시적 실행 도구와 검증.
- `sampledata/raw_ticks/`: 기존 raw v1 보존. `sampledata/raw_ticks_v2/YYYYMMDD/`: 새 운영 실행의 세션별 raw v2.
- `research_runs/`: 새 틱 연구 JSON. `runs/`: 기존 Trade/성과 분석 런. 두 결과 계약은 아직 별개다.
- `operations_state/`: 경량 작업 이력. 수집 DB와 분리하며 Git에 넣지 않는다.
- `sampledata/Daily_baseline`, `sampledata/old_data`: 보존 대상.

## 개발 검증과 GitHub Actions

push/PR마다 Windows + Python 3.14 + uv 0.12.5에서 `uv.lock`을 변경하지 않고
설치한 뒤 Git으로 재현 가능한 테스트를 실행한다. Windows 파일 공유 잠금·프로세스·소켓
합성 회귀도 포함하기 때문에 Windows runner를 사용한다. 상세 계약과 로컬 전체 실행은
[테스트 안내](docs/TESTING.md)를 따른다.

```powershell
uv sync --locked --group dev
uv run --locked --offline python -m pytest -ra
```

기본 `pytest`는 `tests/`만 수집하고 `local_data`·`local_env`를 명시적으로 선택 해제한다.
OCX 로그인 도구는 테스트 자동 수집 대상이 아니다. CI 성공은 실제 시장 데이터·백테스트·
수집 운영 검증 완료를 뜻하지 않는다. 일반 Chat + GitHub에서는 작업 브랜치 → PR →
Actions 실패 원인 수정 → 최신 커밋의 성공 확인 순서로 개발한다.
