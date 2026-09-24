# Stock 시작 안내 — AI 없이 점검하고 실행 경로 찾기

[문서 인덱스](README.md) · [현재 작업과 차단 조건](HANDOFF.md)

처음에는 **`help → doctor → status`**만 사용한다. 이 세 명령은 수집·연구·매매를 시작하지 않는다.
기존 Streamlit 화면을 열 때만 명시적으로 `ui`를 사용한다. 이 문서는 개발 변경의 로컬 배포나
운영 실행 승인을 대신하지 않는다. 실행 중인 수집기와 오류 근거를 보존해야 하는 상황에서는 먼저
[live 작업 경계](docs/COLLECTION_RUNBOOK.md#collection-live-boundary)를 따른다.

## 1. 무엇이 이미 있고, 이번에 무엇이 추가됐나

Stock에는 이미 [Streamlit 운영 화면](dashboard/app.py)이 있다. 이번 작업은 새 GUI를 만든 것이 아니다.
새 [stock.cmd](stock.cmd)와 [stock.ps1](stock.ps1)은 긴 Python/Streamlit 명령을 기억하지 않게 하는
얇은 진입점이다. [Python 도우미](scripts/stock_operator.py)는 기존
[상태 판독기](control_tower/status.py)와 운영 화면이 함께 쓰는
[읽기 전용 환경 목록 점검](control_tower/operator_environment.py)을 재사용한다.

| 목적 | 현재 경로 | 혼동하지 말 것 |
|---|---|---|
| 환경 목록 점검 | `stock doctor` | 수집 준비 완료나 로그인 성공 인증이 아님 |
| 저장된 수집 근거 읽기 | `stock status` | 현재 프로세스/피드 정상 여부는 미확인 |
| 기존 운영 화면 열기 | `stock ui` → `dashboard/app.py` | 화면 자체는 읽기 전용이 아님 |
| 기존 성과 런 비교 | 화면의 **백테스트 분석** | `runs/`용이며 새 selected-v2 결과와 같은 형식이 아님 |
| 현재 NXT 공유계좌 연구 | [연구 경로 지도](docs/PIPELINE_MAP.md)와 HANDOFF | 화면의 일반 재생이나 만능 baseline 버튼으로 대체하지 않음 |

## 2. 터미널에서 처음 할 일

탐색기에서 **`stock.cmd` / `stock.ps1` / `README.md`가 함께 있는 실제 Stock 폴더**를 열고
터미널을 연다. 문서의 예시 경로를 무조건 복사하거나 개발 worktree를 운영 저장소라고 가정하지 않는다.

PowerShell에서도 실행 정책에 영향을 받지 않는 **`.cmd` 진입점을 기본 추천**한다.

```powershell
.\stock.cmd help
.\stock.cmd doctor
.\stock.cmd status
```

PowerShell 스크립트를 선호하면 같은 기능을 쓸 수 있다.

```powershell
.\stock.ps1 help
.\stock.ps1 doctor
.\stock.ps1 status
```

두 진입점 모두 자기 폴더의 기존 64비트 `.venv\Scripts\python.exe`만 사용한다.
`.venv32`, PATH의 Python, `uv`로 자동 대체하지 않는다. 환경이 잘못돼도 자동 설치·복구·로그인이
일어나지 않는다. `help`는 Python 없이도 표시된다.

`doctor`에서 확인하는 것은 Windows, 실행 Python의 64비트/버전/`.venv` 경로,
필수 파일 존재와 GUI 주요 패키지의 설치 metadata다. 실제 import, 패키지 버전 호환성,
lock 일치, GUI 기동, 네트워크, OCX, 디스크 용량, 시장 일정은 인증하지 않는다.
`.venv32`는 파일 존재만 확인하며 실행하지 않는다. 없어도 GUI용 목록 점검을 실패시키지 않는다.
같은 목록은 운영 화면의 **Operator 요약 → 읽기 전용 환경 점검**에서도 확인할 수 있다.
화면도 자동 설치·수정·`uv sync`·OCX 생성·로그인·수집 시작·시장 조회를 하지 않는다.

`PASS`는 해당 작은 점검만 통과했다는 뜻이다. `FAIL`이면 자동 수정하지 말고 표시된 항목을 확인한다.
Python 도우미의 종료 코드 `0`은 보고서 출력 성공, `1`은 환경 목록 미충족 또는 점검 오류,
`2`는 잘못된 명령이다. wrapper의 `.venv` 누락도 `2`다.
`status`의 출력 성공은 수집 정상 판정이 아니다.

## 3. status 결과를 읽는 법

`status`는 **당일 로그 끝부분 최대 64 KiB**와 **capture_status.json 최대 64 KiB**만 기존 reader로 읽는다.
원본 틱 DB, 전체 로그, 다른 세션 검색, PID/창/lease, 거래일 조회는 하지 않는다.
종료·상태 파일에 기록된 세션을 오늘의 살아 있는 세션으로 승격하지 않는다.

| 표시 | 뜻 | 하면 안 되는 해석 |
|---|---|---|
| `unavailable` / `no_heartbeat` | 사용할 수 있는 해당 근거가 없음 | 수집기가 멈췄다고 단정 |
| `recent` | 저장된 근거의 시각이 최근임 | 현재 피드/프로세스가 정상이라고 단정 |
| `stale` | 근거가 오래됨 | 자동 재시작·lock 삭제 |
| `clock_ahead` | 근거 시각이 현재보다 앞섬 | 시각 차이를 무시하고 정상 판정 |
| 과거 생산자 주장 `closed` | 저장된 상태에 종료 주장이 있음 | 원본 데이터 품질·현재 프로세스 종료 인증 |

프로세스 생존·피드 정상·시장 구간·데이터 품질·연구 적격성은 이 도우미에서 **모두 미확인**으로 남긴다.
자세한 근거 축은 [세션 표시 계약](docs/SESSION_ASSESSMENT.md)을 따른다.

## 4. 기존 운영 화면을 바로 여는 방법

환경 점검이 통과했고 로컬 UI 실행이 허용된 상태라면 다음 명령으로 기존 Streamlit 화면을 연다.

```powershell
.\stock.cmd ui
```

또는:

```powershell
.\stock.ps1 ui
```

이 명령은 새 GUI를 만드는 것이 아니라 기존 `dashboard/app.py`를 기존 64비트 `.venv`의
Streamlit으로 **전경 실행**한다. 터미널을 유지하고, 화면 서버를 끝낼 때는 그 터미널에서 `Ctrl+C`를 누른다.
**브라우저 탭만 닫으면 Streamlit 서버는 계속 실행될 수 있다.** 반대로 Streamlit 서버를 끝내도 화면이
별도로 시작한 수집기·워커까지 자동 종료한다고 가정하지 않는다.

실행 명령을 먼저 확인만 하고 싶다면:

```powershell
.\stock.cmd ui-command
```

또는 `.\stock.ps1 ui-command`를 사용한다. 화면에는 **운영 관리**와 **백테스트 분석**이 있다.
GUI는 작업 상태 DB를 열 수 있고 버튼으로 작업·계획·소규모 관리 수집을 등록할 수 있으므로
`doctor/status/ui-command`와 달리 읽기 전용이 아니다. 기존 접근 제어를 우회하지 않고 외부 공개 주소나
방화벽 설정도 바꾸지 않는다.

첫 확인에서는 화면이 정상 열리고 표시·연결 범위를 읽는 것까지만 한다. 수집 시작/중지, 장외 재생 실행,
예약, 실제 데이터 입력은 [운영 계약](CONTROL_TOWER.md)에 맞춘 별도 행동이다.
수집 버튼은 화면이 소유한 새 소규모 세션용이며 외부 CLI 수집기를 자동 인수하지 않는다.

## 5. PowerShell 실행 정책과 실행 문제

`.ps1`이 실행 정책으로 차단돼도 프로젝트 사용을 위해 정책을 바꿀 필요는 없다.
PowerShell 터미널에서 **`.\stock.cmd ...`를 사용하면 PowerShell script execution policy를 거치지 않는다.**

현재 정책을 확인해야 할 때만:

```powershell
Get-ExecutionPolicy -List
```

를 사용한다. 이 프로젝트가 `Set-ExecutionPolicy`를 자동 호출하거나 관리자 권한을 요구하지 않는다.

`.venv`가 없으면 먼저 다른 폴더를 연 것은 아닌지 확인한다. 잘못된 worktree에서 환경을 새로 만들거나
실행 중인 운영 환경에 `uv sync`를 덮어 적용하지 않는다. 환경 구성이 실제로 필요하면
[테스트·환경 안내](docs/TESTING.md)의 기존 절차를 사용한다. `.venv32`를 GUI 환경으로 쓰지 않는다.

wrapper를 우회한 읽기 전용 점검은 다음처럼 직접 호출할 수 있다.

```powershell
.\.venv\Scripts\python.exe -I -B scripts\stock_operator.py doctor
.\.venv\Scripts\python.exe -I -B scripts\stock_operator.py status
```

기계 판독용 출력은 `.cmd`/Python에서 `--json`, PowerShell wrapper에서 `-Json`을 붙인다.
보고서에는 로컬 경로와 세션 식별자가 들어갈 수 있다. 공개 Git/PR에 원문을 자동 첨부하지 않는다.

## 6. 아직 연결하지 않은 명령과 병렬 개발 경계

`collector start/stop/canary/preflight`, `backtest baseline`, 실제 주문 실행은 아직 wrapper에 연결하지 않았다.
앞선 설계 대화에 나온 명령 예시는 구현됐다는 뜻이 아니다. 알 수 없는 명령은 실행 전에 거부한다.
현재 수집기 보류 조건과 연구의 다음 단계는 [HANDOFF](HANDOFF.md)를 따른다.

Operator 트랙은 wrapper, `scripts/stock_operator.py`, 전용 테스트와 이 안내를 담당한다.
collector/native, engine/execution/strategies, raw/qualification와 백테스트 결과 계약은 변경하지 않는다.

다음 확인은 **이 문서만 보고 `help → doctor → status → ui`까지 실행되는지** 보는 것이다.
그 후에야 자주 쓰는 승인된 작업을 하나씩 연결한다. UI용 `doctor`를 collector의 공식 preflight 또는
실행 admission으로 재사용하지 않는다.
