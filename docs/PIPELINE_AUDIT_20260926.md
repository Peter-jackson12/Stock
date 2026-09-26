# 전체 파이프라인·설계 검수 — 2026-09-26

## 새 컨텍스트가 먼저 알아야 할 결정

사용자는 디렉터리 이동 후 프로젝트 전반을 점검하되, 실행 중인 MWFD-04에 영향을 주지 않기를 요청했다. 점검 결과에 따른 코드·설정 변경은 MWFD 계산 종료 뒤로 미룬다. **계산 종료와 최종 완료 승인은 다르다.** 종료 후 결과와 체크포인트를 보존하고, 실패 셀 및 종료 검증 문제를 확인한 다음 최종 COMPLETE를 승인한다.

이 문서는 당시 점검의 보존본이며 실시간 상태판이 아니다. 다음 작업자는 작은 상태 파일과 실제 코드에서 진행 상태·수정 반영 여부를 다시 확인한다. 이 기록은 수정 완료나 전체 실행 검증을 뜻하지 않는다.

- [시각적 점검판 원본](PIPELINE_AUDIT_20260926.canvas.tsx): 근거·범위·우선순위 상세 표시. GitHub에서는 소스로 확인하며, Canvas 렌더링은 해당 도구가 필요하다.
- [프로젝트 인덱스](../README.md) · [현재 인계](../HANDOFF.md) · [파이프라인 지도](PIPELINE_MAP.md)
- 주 checkout: `C:/Projects/TotalStock/Stock`, 당시 HEAD `1811fbad631bcf93406c49ba02081eed90cfec71`.
- 실행 checkout: `C:/Projects/TotalStock/worktrees/mwfd-04-full-run`, 당시 HEAD `64362558510cf7e5a863a0236ed2b82715a98553`.
- 실행 데이터: `C:/Projects/TotalStock/_data/mwfd_04/20260926T084815+0900-1286-cell-full-run`.

## 검수 범위와 한계

주 저장소와 MWFD checkout의 코드·설계 문서·기존 테스트 내용을 정적으로 대조했다. 경로 이동, Git worktree 연결, 실행 provenance, 수집·종료·품질 판정, 작업 제어, exact 연구·회계, Fast/MWFD, 레거시 피처·일일 수집, 문서·CI 연결을 검토했다.

실행에 영향을 주지 않도록 프로젝트 테스트 실행, 대형 DB 열기·전체 해시, 데이터 재처리, 벤치마크, 실제 시장 피드·OCX 검증, 재시작은 하지 않았다. 따라서 실제 부하·장애 복구·전원 차단·전체 데이터 정합성까지 인증한 검수는 아니다. 소스 및 기존 테스트를 읽은 것과 테스트를 실행한 것을 구분한다.

## 보존한 발견 사항

아래 경로는 별도 표시가 없으면 주 checkout 기준이다. 줄 번호는 검수 당시 값이다. P1은 최종 결과 승인 전에 먼저 다룰 항목, P2는 후속 신뢰성·운영 개선 항목이다.

| 우선순위 | 발견 및 영향 | 근거·후속 확인 |
|---|---|---|
| P1 | MWFD crosscheck가 FAIL을 기록하고도 성공 종료하며 manifest는 검증 합격·필수 산출물 완전성을 확인하지 않고 COMPLETE를 쓸 수 있다. | 실행 checkout `scripts/finalize_mwfd_04.py:283–293,676–704`. 최종 승인 조건을 한곳에서 강제한다. |
| P1 | 당시 셀 #252, 코드 `0161M0`의 173,892 이벤트 피처 계산이 180초 제한을 넘어 실패로 보존되었다. | 당시 상태 파일 기준. 종료 후 실패 상태와 재시도 가능성을 다시 확인하고 누락된 셀을 완료로 간주하지 않는다. |
| P2 | MWFD combine·summarize의 부분 저장 후 중단 시, 일부 파일 존재만으로 재실행을 건너뛰거나 writer 진행 차이로 복구가 막힐 수 있다. | 실행 checkout finalizer `149–152,187–198,342–344,473,498,574,625`. 공통 완료 경계와 마지막 완료 표식이 필요하다. |
| P2 | MWFD 코드 식별 목록에서 tick 엔진·종료 도구·보조 실행 도구가 빠진다. 보조 실행의 시간·CPU·메모리는 runtime summary에서 누락될 수 있다. | 실행 checkout `scripts/materialize_mwfd_04_events.py:52`, `sweep.py:10`, finalizer `500`, `mwfd_04_host_limit_step.py:54,64`. 현재 데이터 손상을 입증하는 발견은 아니다. |
| P2 | checkout별 데이터 보존 경로·venv 안내·MWFD 진행 문구 및 일부 완료 체크가 서로 어긋난다. | 양쪽 AGENTS·README, 실행 HANDOFF, BACKTEST_TODO, PIPELINE_MAP. 현 코드·실제 경로와 대조해 담당 문서만 갱신한다. |
| P2 | Fast exact 합격 판단은 신호 시각·건수·PnL 중심으로, 상태 진단 및 현금·포지션·회계 대사를 충분히 포함하지 않는다. | `research/fast_backtest/exact.py:37`, `engine/nxt_portfolio_research.py:132`. 현재 screening 실행 중단 사유와 향후 exact 승인 기준을 구분한다. |
| P2 | 수집 trade-silence 복구가 체결 대신 임의 이벤트 시각으로 판정되어 호가만 와도 해제될 수 있다. 종료 판정은 명시한 NXT 세션 프로필과 다를 수 있다. | `collector/session_monitor.py:377–386,521–535`. 복구·종료 경계 테스트가 필요하다. |
| P2 | 큐 경고 기본 하한 20,000이 실제 큐 8,192와 배치 512보다 크다. 관측 창 처리도 정확한 시각 일치에 의존해 경고가 빠질 수 있다. | `collector/session_monitor.py:63,467–478`, `collector/live_capture.py:40,80`. 기존 overflow fail-closed 보호와는 별개다. |
| P2 | 작업 제어 화면이 최신 30개 이력에만 의존해 오래된 활성·예약 작업이 화면에서 사라질 수 있다. 중복 방지는 그 기존 ID를 계속 반환한다. | `control_tower/jobs.py:69–91`, `dashboard/operations.py:138,152–198`. 활성 작업 조회와 최근 이력을 분리한다. |
| P2 | 관리 수집 프로세스가 import·lease 초기화 중 실패하면 launch 요청이 launching에 남을 수 있다. | `managed_capture.py:283–294`, logger `30–31,1228–1236`. 초기화 성공·실패 응답을 명시한다. 중복 로그인 발생을 확인한 것은 아니다. |
| P2 | 잘못된 close_ns가 기록 시작 이후 ValueError를 내면 연구 상태가 running으로 남을 수 있다. 실행 checkout에는 관련 방어와 테스트가 있으나 main에는 없다. | `engine/nxt_portfolio_research.py:257`, `portfolio_session.py:76`. 실행 보존 후 필요한 변경만 대조·반영한다. |
| P2 | 결과 inspector가 no_fills와 열린 포지션, empty_input과 fills의 모순을 통과시킬 수 있다. | `scripts/inspect_tick_research.py:53–60,77`. 현재 엔진이 실제 모순 결과를 생성한다는 뜻은 아니다. |
| P2 | 레거시 피처 manifest 저장이 이전 날짜 coverage를 덮어써 여러 날짜 처리 후 마지막 날짜 정보만 남을 수 있다. | `features/store.py:115,149–153`, `scripts/build_features.py:232,244,374–383`. parquet 자체 소실과 구분한다. |
| P2 | 레거시 strict coverage 실패 판단이 parquet·manifest 게시 이후여서 실패 산출물이 이미 기존 날짜를 덮어쓸 수 있다. | `scripts/build_features.py:230–245`, `features/store.py:109`. 게시 전에 검증한다. |
| P2 | 레거시 KIS 일일 daemon이 LOB 변환 실패 반환값·무데이터 종료를 종합 성공으로 표시할 수 있다. | `collector/run_daily_daemon.py:223–234`, `build_lob_db.py:407`, `daily_collector.py:274`. 현재 Kiwoom/MWFD 실행 경로와 별개다. |

## 설계 경계와 정상 확인 사항

프로젝트에는 raw-v2 exact 연구, Fast/MWFD 연구, 레거시 raw-v1/LOB/fs_v1 흐름이 공존한다. 하나의 자동 종단 파이프라인으로 해석하면 안 된다. 운영 화면은 checkout의 `research_runs/**/result.json`과 tick 결과 계약 중심이며 portfolio·selected 결과 및 `_data`의 MWFD 결과를 모두 통합 표시하지 않는다. 실제 주문 연결 및 일부 전략/Broker 추상화도 완료된 기능으로 간주하지 않는다.

정적 검토에서는 수집 callback→유한 큐→단일 writer 분리, accepted/committed/closed 구분, overflow·FID 품질의 실패 차단, snapshot 잠금·식별, 입력 순서·cutoff 검증, 이전 호가와 타이머의 실행 순서, fill 기반 회계와 유효 bid 평가, 작업 소유권·PID 생성시각·lease·정지 응답·예약 중복 방지 구조를 확인했다. 이 구조 확인은 실제 장애 실험 성공과 다르다.

디렉터리 이전 후 주 저장소와 11개 worktree의 Git 연결은 정합했고, 조사한 실행 소스에서 옛 루트 경로 잔존은 찾지 못했다. 등록된 provenance 소스 15개 해시는 manifest와 일치했다. 원본 파일 stat 크기·수정시각도 일치했으나 대형 원본 전체 해시를 다시 계산하지 않았다. 기존 README의 venv 재생성 권고와 실제 경로 상태는 별도 정리가 필요하다.

CI 안내도 정리가 필요하다. `docs/TESTING.md`의 push/PR 자동 실행 설명과 실제 `.github/workflows/ci.yml`의 예약·수동 실행 설정이 다르다. 과거 CI 성공을 현재 MWFD revision 검증 완료로 사용하지 않는다.

## 종료 후 조치 순서

1. 실제 계산 종료 여부를 확인하고 체크포인트·실패 기록·로그·코드 식별 근거를 보존한다.
2. 실패 셀의 최신 상태를 확인하고 복구한다. 결과가 일부 존재한다는 이유로 완료 처리하지 않는다.
3. finalizer의 FAIL 전파, 필수 산출물, resume·crosscheck 합격, 부분 저장 복구를 검증한다. 이 검증 전에는 최종 COMPLETE를 승인하지 않는다.
4. provenance·보조 실행 통계와 exact/결과 inspector 합격 기준을 보강하고 필요한 회귀 검증을 수행한다.
5. 주 저장소의 수집 경고·작업 제어·레거시 게시 및 문서 불일치를 각각 수정하고 범위에 맞게 검증한다.

현재 문서 연결 작업은 발견 사항에 대한 수정 착수가 아니다. 실행 checkout의 코드·설정·HEAD·실행 데이터는 변경하지 않는다. 점검 기록과 점검판 원본은 함께 저장소에 보존한다. 원본 안의 로컬 근거 경로는 검수 당시 PC 기준이므로 다른 PC에서는 해당 checkout의 동일 소스를 대조한다.
