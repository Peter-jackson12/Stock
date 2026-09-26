# 테스트 실행 계약

## GitHub에서 재현하는 범위

``.github/workflows/ci.yml`은 **매주 일요일 09:00 KST 정기 실행과 수동 `workflow_dispatch`**에서
Windows, 64비트 Python 3.14, uv 0.12.5와 기존 `uv.lock`으로 실행한다. 작은 push/PR마다 자동 실행하지
않는다. `uv sync --locked --group dev`가
잠금 불일치를 실패로 처리한다. Actions 자체도 커밋으로 고정하고 읽기 권한만 부여한다.
시장 DB·운영 원본·사용자 32비트 환경을 다운로드하거나 업로드하지 않는다.

기본 명령은 `uv run --locked --offline python -m pytest -ra`다.
`tests/`만 수집하므로 `collector/kiwoom/test_login.py`의 수동 OCX 로그인은 포함하지 않는다.
엄격한 marker 검사로 분류 오타를 실패시킨다.

- 기본: 엔진/피처 batch-stream 동치·미래 참조 방지, 합성 틱·호가·체결, 파싱·품질 차단,
  런 저장/조회, 제어 계약, 작은 임시 SQLite, Windows 잠금/자식 프로세스, 화면 회귀.
- `local_env`: `.venv32`와 실제 32비트 기반 Python이 필요한 IPC 4개 및 관리 수집기 1개.
  모두 합성 입력이며 실제 OCX 로그인/실피드 인증은 아니다. 환경 누락은 실패다.
- `local_data`: 기존 `scripts/verify_phase_a.py`의 냉동 LOB 기준선 검증을 호출하는 1개.
  필수 입력을 먼저 확인하고 누락 시 백테스트 시작 전에 실패한다. 기본 CI에서는 실행하지 않는다.

2026-09-20 변경 기준 전체 수집은 **1,213개**, 기본 Windows 검증은 **1,207개 통과,
6개 선택 해제, 0개 skip**다. 1개 local_data 테스트 내부의 세부 대조를 별도 pytest 건수로
부풀리지 않는다. 추가 테스트가 생기면 실행 결과의 수집/선택 건수를 기준으로 보고한다.
Windows 전용 테스트의 플랫폼 skip은 남아 있으므로 Linux 실행만으로 동일 범위를 인증하지 않는다.

기존 Git 추적 Daily/Daily_baseline CSV와 results CSV는 이번 변경에서 이동·삭제하지 않았다.
Git으로 받을 수 있다는 것만으로 대용량 자료를 CI 검증 입력으로 사용하지 않는다.
새 합성 회귀는 `tmp_path`와 고정 입력을 사용하고, 실제 데이터/특정 설치가 필요한 테스트에만
각 marker를 붙인다. 선택된 필수 입력의 누락을 `skip`이나 빈 결과 성공으로 처리하지 않는다.
새 테스트의 import/수집 단계에서도 로컬 데이터 접근·백테스트 실행·로그인을 하지 않는다.

## Windows 로컬 명령

저장소 루트 `C:\Projects\TotalStock\Stock`에서 실행한다. 기존 `.venv32`나 운영 환경을 교체하지 않는다.

```powershell
uv sync --locked --group dev
# 기본 Git-only 검증 (CI와 동일)
uv run --locked --offline python -m pytest -ra
# 데이터는 읽지 않고 전체 테스트/marker 수집만 확인
uv run --locked --offline python -m pytest --collect-only -q -m ""
# 32비트 합성 검증만 추가 실행
.\.venv\Scripts\python.exe -m pytest -ra -m local_env
```

**아래 실데이터 명령은 입력·장외 시간·I/O 예산을 확정한 후에만 실행한다.**
문서에 명령이 있다고 운영 스캔/백테스트가 승인된 것은 아니다. 수집 중에는 실행하지 않는다.

```powershell
# 실제 냉동 LOB 기준선만 (최대 600초, 임시 결과 디렉터리 사용)
.\.venv\Scripts\python.exe -m pytest -ra -m local_data
# 기본 + local_env + local_data 전체 검증
.\.venv\Scripts\python.exe -m pytest -ra -m ""
```

local_data 필수 입력:

- `sampledata/old_data/temp/{YYYYMMDD}_LOB.db` 8개:
  20220425, 20220426, 20220427, 20220428, 20220429, 20220502, 20220503, 20220504.
- `sampledata/Daily_baseline/{name}.csv` 9개:
  close, float, high, key, low, mkt, open, shares, tradamt.
- 기존 `results/cross_Today_1.csv` 기준선. 기존 Phase A 코드가 3종목
  (000270, 005930, 000660)의 재현성·CSV/parquet·거래/PnL 기준선을 대조한다.

local_env는 `.venv32/Scripts/python.exe`, `.venv32/pyvenv.cfg`와 그 파일이 가리키는
실제 32비트 기반 Python이 필요하다. 새 CI를 위해 자동 설치하거나 OCX에 로그인하지 않는다.

## pytest 전체 통과로 대체되지 않는 검증

전체 pytest를 실행해도 다음 운영/연구 검증이 자동 실행되지는 않는다.

- `scripts/verify_features_vs_legacy.py --date YYYYMMDD --codes CODE`:
  `sampledata/temp/*_LOB.db`를 이용한 실제 피처 대조. 현재 스크립트는 일부 입력을 건너뛸 수 있어
  종료 코드뿐 아니라 요청한 날짜/종목의 실제 대조 건수와 제외 사유를 확인해야 한다.
- `scripts/verify_engine_feature_parity.py --start YYYYMMDD --end YYYYMMDD --codes CODE`:
  실제 LOB·Daily·`sampledata/features/fs_v1/`를 사용하는 인라인/스토어 백테스트 대조.
- `scripts/verify_phase_a.py --date YYYYMMDD --code CODE`: 기존 기준선에 더해
  명시한 날짜의 레거시 틱 엔진 검증. 기본 local_data 1개에는 이 추가 틱 검증이 없다.
- 원본 raw v2 전체 무결성·품질 확인, 틱 연구, 대용량 CSV/병합 결과 대조,
  실피드·공급자 인증·수집 부하·OS 정시 예약/재부팅·전원 장애 검증.

원본 틱 연구가 주 경로다. 첫 실제 시험은 [BACKTEST_TODO](../BACKTEST_TODO.md)와
[틱 연구 안내](../TICK_RESEARCH_RUNBOOK.md), 운영 보호는
[수집 안내](COLLECTION_RUNBOOK.md)를 따른다. LOB 회귀를 틱 연구의 선행 조건으로 만들지 않는다.
위 스크립트는 승인된 범위에서 `uv run --locked --offline python` 뒤에 경로/인수를 붙여 실행한다.
실제 원본과 대형 산출물을 Git/Actions artifact/cache에 올리지 않는다.
