# Stock — 틱 수집과 재현 연구

원본 체결·호가 이벤트를 수집하고 **틱 단위 전략 재현의 정확성**을 검증하는 프로젝트다.
운영 화면은 수집·조회·연구 작업을 관리하며, 화면·수집기·작업 워커는 별도 프로세스로 둔다.
실제 주문 연결과 전략 수익성 입증은 별도 단계다. 합성/CI 성공을 실데이터 검증 완료로 해석하지 않는다.

[처음 실행하는 사람](START_HERE.md)은 `.\stock.cmd help → doctor → status`부터 시작한다.
이 세 명령은 읽기 전용 점검이며, `.\stock.cmd ui`를 명시적으로 실행할 때만 기존 Streamlit 운영 화면을 연다.
PowerShell wrapper도 제공하지만 `.cmd`는 PowerShell script execution policy를 요구하지 않는다.

처음에는 이 문서에서 필요한 경로만 고른다. AI 작업자는 [AGENTS](AGENTS.md) →
[현재 HANDOFF](HANDOFF.md)부터 읽고, 상세 계약은 해당 작업에 필요한 것만 연다.
전체 과거 인계를 매 작업마다 읽지 않는다.

## 기본 연구 흐름

```text
신규 raw-v2 수집 (원문 + 정책에 따른 정규화 기록)
  → 동일 세션 종료 근거 대조
  → 최초 100건부터 제한 표본
  → 별도 장외 전체 무결성·품질/입력 계약 확인
  → 첫 틱 연구 실행
  → 결과·대표 사례·재현성 대조
```

이것은 운영자가 지킬 단계다. 모든 화살표가 자동 실행되는 통합 명령이라는 뜻은 아니다.
특히 직접 연구 CLI는 검증과 재생을 함께 수행한다. 입력 합격을 먼저 확정하려면 별도 검사 절차가 필요하다.
현재 작업 상태·승인 조건은 [HANDOFF](HANDOFF.md), 완료 체크는 [BACKTEST_TODO](BACKTEST_TODO.md)에만 기록한다.
실제 진입점과 보호 범위의 차이는 [파이프라인 지도](docs/PIPELINE_MAP.md)를 본다.

LOB/초봉 변환과 기존 런 비교는 유지하는 레거시 경로이며 위 틱 연구의 선행 조건이 아니다.
`runs/`의 Trade/성과 분석과 `research_runs/`의 틱 진단 결과도 같은 결과 계약이 아니다.

## 문서 찾기 — 사실마다 담당 문서 하나

| 알고 싶은 것 | 담당 문서 |
|---|---|
| 현재 결정·차단 조건·바로 다음 행동 | [HANDOFF](HANDOFF.md) |
| 첫 실제 시험의 남은 체크 항목 | [BACKTEST_TODO](BACKTEST_TODO.md) |
| 어떤 코드가 무엇을 실행하는지·경로 간 차이·단순화 후속 항목 | [파이프라인 지도](docs/PIPELINE_MAP.md) |
| 수집 시각 결정 전 필수 사실·장전/정규장/NXT 구분 | [수집 의사결정 계약](docs/COLLECTION_RUNBOOK.md#collection-decision) · [구간별 coverage 표](docs/COLLECTION_RUNBOOK.md#collection-coverage) |
| 수집 시작/종료·저장·메타데이터·별도 확장 절차 | [수집 실행 안내](docs/COLLECTION_RUNBOOK.md#환경과-수집-시작) |
| 종료 단계 증거·선택적 ActiveX 해제와 미검증 경계 | [종료 계약](docs/COLLECTOR_TEARDOWN.md) |
| 선택적 Qt 폴링·기존 FID 시각 표본과 해석 한계 | [수신 진단 계약](docs/COLLECTOR_TELEMETRY.md) |
| raw 표본·연구 실행·결과 상태와 한계 | [틱 연구 실행 안내](TICK_RESEARCH_RUNBOOK.md) |
| 독립 execution oracle·유한 검증 범위·미해결 반례 | [실행 감사 명세](tests/EXECUTION_ORACLE_SPEC.md) |
| 원본 보호 handle 기반 격리 복제 합성 lab·운영 미승인 경계 | [복제 lab 명세](tests/RAW_V2_CLONE_LAB_SPEC.md) |
| 운영 화면·제어 계약·IPC·예약·권한 | [CONTROL_TOWER](CONTROL_TOWER.md) |
| 세션별 저장·시각·종료·연구 적격 근거의 표시 경계 | [컨트롤타워 세션 근거 표시](docs/SESSION_ASSESSMENT.md) |
| 이벤트·시계·체결·전략의 설계 근거 | [ARCHITECTURE_TICK](ARCHITECTURE_TICK.md) |
| Git-only/로컬 검증의 구분과 명령 | [테스트 안내](docs/TESTING.md) |
| AI 읽기 순서·역할·문서 유지 원칙 | [AGENTS](AGENTS.md) |

상세 수집/제어 문서에는 날짜별 구현 경과도 있다. 과거 "미연결" 문구를 현재 판정으로 복사하지 말고
[현재 연결 지도](docs/PIPELINE_MAP.md)와 해당 함수·관련 테스트를 대조한다. 상충하는 문구를 발견하면 담당 문서에 바로잡는다.
문서 크기만으로 모듈/안전장치가 불필요하다고 판단하지 않는다.

### 설계 배경과 과거 기록 — 필요할 때만

- [코드 교차 검토 기록](TICK_CROSS_REVIEW.md): 반례와 검토 포인트. 기록 당시 적용 상태와 현재 코드를 구분한다.
- [과거 수집기 고정 입력 비교](tests/COLLECTOR_REVISION_BENCHMARK.md): 완료된 revision 비교의 근거와 명시적 재측정 방법. 일반 CI의 상시 실행 항목이 아니다.
- [ARCHITECTURE_V2](ARCHITECTURE_V2.md): 기존 런 스토어·피처·전략/Broker 분리 배경과 레거시 계약.
- [REFACTORING_PLAN](REFACTORING_PLAN.md): 초기 L0/L1 의사결정과 LOB 스키마. 과거 무유실·방향 판정 주장은 현 raw-v2 인증이 아니다.
- [archive 안내](docs/archive/README.md): 날짜별 인계·후보 조사·이전 README. 원문 보존 위치와 당시 revision.
- [커밋 ID 대응표](docs/COMMIT_ID_MAP_20260918.json): 한국어화 전후 과거 코드 리비전 대조.

과거 문서를 무조건 지우거나 이동하지 않는다. 현재도 사용되는 레거시 스키마·회귀 계약이 들어 있을 수 있다.

## 운영 화면 실행

로컬 실행이 승인된 경우 저장소 루트에서 다음처럼 기존 화면을 연다.

```powershell
.\stock.cmd ui
```

PowerShell wrapper를 선호하면 `.\stock.ps1 ui`도 같다. 두 wrapper는 기존 64비트 `.venv`에서
`dashboard/app.py`를 Streamlit으로 전경 실행한다. 상세 사용법과 실행 정책 대안은
[처음 실행 안내](START_HERE.md)를 따른다.

기본 **운영 관리** 화면은 상태·작업을, 왼쪽 **백테스트 분석**은 기존 런 비교를 보여준다.
운영 관리의 preflight 다음에는 실행과 분리된 **수집 Run Plan**이 있으며 계획 검토/저장만 한다.
수집 버튼은 화면에서 새로 만든 소규모 세션만 제어하며 외부 CLI 수집기를 자동 인수하지 않는다.
폰 사용은 기존 PC 원격 접속 안에서 이 화면을 여는 방식이다. 외부 공개 서버로 배포된 상태가 아니다.
사용법은 [CONTROL_TOWER의 사용 흐름](CONTROL_TOWER.md#3-지금-사용할-수-있는-흐름)을 따른다.

정상 사용은 운영 관리의 **Windows 실행 바로가기**에서 만든 `Stock Operator` 아이콘 더블클릭을 기본으로 한다.
처음 한 번만 `.\stock.cmd ui`로 화면을 연 뒤 바탕화면 또는 시작 메뉴 바로가기를 만들면 된다.
바로가기는 현재 사용자 범위에서 현재 저장소의 `stock.cmd ui`만 호출하며 관리자 권한·레지스트리·영구 실행 정책
변경을 요구하지 않는다. 프로젝트 폴더를 옮기면 새 위치에서 `.\stock.cmd ui`로 한 번 연 뒤 바로가기를 갱신한다.
문제 해결용 fallback은 계속 `.\stock.cmd ui` 하나다. exe 패키징은 수행하지 않는다.


## Canonical workspace

현재 로컬 경로 계약은 다음과 같다. 환경 생성이나 데이터 실행 승인은 별도다.

| 용도 | 경로 |
|---|---|
| Workspace | `C:\Projects\TotalStock` |
| Main repository | `C:\Projects\TotalStock\Stock` |
| Linked worktrees | `C:\Projects\TotalStock\worktrees` |
| Research/cache/audit data | `C:\Projects\TotalStock\_data` |
| Historical snapshots | `C:\Projects\TotalStock\StockSnapshots` |

실제 research 하위 폴더는 `_data\krx_pit_probe`, `_data\pit_metadata_audit`,
`_data\fast_backtest`다. `_data` 뒤에 `Stock`을 추가하지 않는다.
repo 내부 경로는 해당 checkout 기준이며 shared input/output은 CLI/plan의 명시적 경로를 사용한다.
기존 plan/result/manifest의 경로·digest를 덮어쓰지 않고 다음 실행에는 별도 새 plan을 작성한다.
archive와 과거 benchmark/provenance의 당시 절대 경로는 보존한다.
`C:\StockSnapshots`는 canonical snapshot root로 향하는 호환 junction이다.
대표 result JSON의 동일 파일 identity·크기·SHA-256을 확인했으며 새 실행은 canonical root를 쓴다.

### 이동된 가상환경

2026-09-25 검사에서 main의 `.venv` Python 3.14.7은 64비트, `.venv32` Python 3.10.11은
32비트였으며 `sys.executable`과 `sys.prefix`는 새 위치였다. 두 환경 모두 pip 모듈은 없고
uv 관리 환경이다. pip 부재만으로 이동 손상을 판단하지 않는다.
활성화 스크립트와 실행 launcher에 이전 경로가 남아 **RECREATE_RECOMMENDED**다.
자동 삭제/재생성·문자열 패치는 하지 않았다. 별도 환경 복구 전에는 활성화나 launcher에 의존하지 않고
검증된 main의 Python 절대 경로와 `-m`으로 필요한 모듈을 호출한다. 이것이 OCX 준비를 인증하지 않는다.
worktree에서 검증할 때도 main의 Python을 사용하되 작업 디렉터리는 검증할 worktree로 지정한다.

`sqlite.py`는 legacy 수동 DB 조회 도구다. 기본 파일은 해당 checkout의
`sampledata/raw_ticks/20260914_raw.db`이며 `--database`로 별도 경로를 지정한다.
명시적으로 실행할 때만 읽기 전용으로 열고, import/`--help`는 DB를 열지 않는다.
이전 경로 정리 검증에서는 실제 raw DB를 실행하지 않는다.

## 코드와 데이터 위치

| 위치 | 역할 |
|---|---|
| `collector/` | 키움/KIS 수집·원본 저장·일봉/메타데이터. OCX는 `.venv32` |
| `control_tower/`, `dashboard/` | 작업·제어 계약과 운영/분석 화면. `.venv` |
| `engine/`, `execution/`, `strategies/` | 이벤트 재생·가상 체결·전략 |
| `scripts/`, `tests/` | 명시적 실행 도구와 검증 |
| `sampledata/raw_ticks_v2/YYYYMMDD/` | 새 운영 세션별 raw-v2. raw-v1은 `sampledata/raw_ticks/`에 보존 |
| `research_runs/`, `runs/` | 각각 새 틱 연구 JSON과 기존 성과 분석 런 |
| `operations_state/` | Git 제외 작업/수집 상태와 근거. raw와 분리 |
| `sampledata/Daily_baseline`, `sampledata/old_data` | 보존 대상 기준선과 과거 데이터 |

## 개발 검증과 GitHub Actions

일반 CI는 매주 일요일 09:00 KST 정기 실행과 필요 시 수동 실행만 한다. master push/PR마다 자동 실행하지 않는다.
Windows + 64비트 Python 3.14 + 고정 uv/lock으로 Git-only pytest를 실행한다.
Windows 파일 잠금·프로세스·소켓 합성 회귀를 포함한다. 상세 범위는 [테스트 안내](docs/TESTING.md)를 따른다.
아래 로컬 명령은 live 수집 중에 실행하지 않는다. GitHub-only 작업의 검증 위치는 GitHub-hosted Actions다.

```powershell
uv sync --locked --group dev
uv run --locked --offline python -m pytest -ra
```

기본 pytest는 `tests/`만 수집하고 `local_data`·`local_env`를 선택 해제한다.
문서 경로/절 링크·필수 의존 연결·원문 보존·HANDOFF 크기도 Git-only 회귀로 확인한다.
CI는 실제 시장 데이터·OCX 로그인·실데이터 백테스트·수집 부하를 인증하지 않는다.
일반 Chat + GitHub 작업은 브랜치 → PR → 변경 검증/CI → 원격 재확인 순서다.
수집 중에는 master 변경·병합을 보류하며 종료 후 별도 확인해야 한다.
[작업 경계](docs/COLLECTION_RUNBOOK.md#collection-live-boundary)를 넘는 로컬 작업은 자동 승인하지 않는다.
대규모 구현·다파일 통합·고위험 감사는 개발용 로컬 에이전트에 묶어서 맡길 수 있다([역할](AGENTS.md)).
개발 위임은 운영 PC 관측·배포·로그인·실데이터 처리 승인이 아니며, 그런 운영 단계는 별도 승인 범위에서만 넘긴다.
