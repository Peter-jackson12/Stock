# 현재 인계 — 2026-09-21 / READY_TO_START, 사용자 PowerShell 실행 대기

현재 작업 요약만 유지한다. 과거 인계 전체는 [보존본 안내](docs/archive/README.md)에 있다.
상세 계약은 [문서 인덱스](README.md), 연결 구조·남은 단순화 항목은 [파이프라인 지도](docs/PIPELINE_MAP.md)를 본다.

## 현재 목표와 상태

첫 실제 연구 입력은 **현재 코드로 새 clean closed raw-v2 세션을 확보하는 경로 A**다.
기존 raw를 가공하는 경로 B는 예비이며 구현하지 않는다. 후보 선정·전체 입력 검증·첫 백테스트는 미완료다.
과거 raw 판정과 다음 체크 항목은 [BACKTEST_TODO](BACKTEST_TODO.md)에만 유지한다.

## 당일 로컬 점검 — 07:01~07:04 KST

사용자가 오늘 live 시세 수집을 명시 승인했다. 거래 실행은 금지다.
판정은 **READY_TO_START**: 에이전트 작업 종료 후 자식 프로세스 유지 보장이 미확인이라 실행하지 않았다.
다음 행동은 사용자가 `C:\Projects\Stock`의 일반 PowerShell에서 아래 독립 CLI를 실행하고 live 로그인하는 것이다.
로그인 후 실제 identity/server와 running/accepting/error, callback/commit 증가를 확인해야 한다.
현재 신규 session_id/raw/PID는 없으며 9월 18일 status를 오늘 세션으로 재사용하지 않는다.

- fetch 후 master를 `ee9b36c0dda9c08ebaf905357269bf602f0a6bf7`로 ff-only 동기화했다. 차이는 HANDOFF뿐이다.
- 작업 트리 clean, worktree 1개, 다른 활성 작업·Python 수집기·키움/KOA 로그인 프로세스는 관측되지 않았다.
- Windows는 2026-09-21 KST. C: 여유 1,369,782,996,992 bytes, raw root는 로컬 일반 디렉터리다.
- lock 삭제 없이 획득/해제 성공, managed 상태 DB 없음. 공식 preflight는 32bit/OCX/ready 모두 정상이다.
- 공식 브라우저 목록의 최신 일자는 KRX 시장운영 09-18, 키움 Open API+ 09-10으로 신규 공지는 관측되지 않았다.
- 로그인·합성 probe·raw DB 조회는 하지 않았다. 자세한 로컬 근거: `operations_state/preflight_20260921/report.md`.

## 2026-09-20 늦은 원격/공식 자료 점검

- 전날 점검 시작 기준 master는 `497e42e18fe4d0733d7640b428563c366ff53aae`이고 열린 PR은 없었다.
  해당 master Actions `35505919234`는 success, Windows/Python 3.14.7에서
  1,226개 수집 / 1,220개 통과 / 6개 선택 해제였다. 이후 원격이 진행되면 최신 상태가 우선이다.
- 행정안전부 2026-09-16 공식 안내는 추석 연휴를 **9월 24~27일**로 명시한다.
  따라서 9월 21일은 추석 연휴에 포함되지 않는다.
- KRX 현재 공식 거래시간 안내는 정규장을 **09:00~15:30**으로 표시하고,
  휴장일을 토요일·관공서 공휴일·노동절·연말 휴장·거래소가 별도로 정한 날로 규정한다.
  9월 21일을 휴장으로 보는 현재 공식 근거는 확인하지 못했다.
- 키움 공식 제도개편 안내에는 **KRX 애프터마켓(16:00~20:00) 관련 변경이 09/21부터 적용 예정**이라고 적혀 있다.
  현재 기본 수집 경로는 정규장 전용이고 15:35에 종료하므로 이번 세션에 애프터마켓/NXT 전환을 끼워 넣지 않는다.
  첫 적용일이라는 사실은 종료 후 해석 시 기록해 둔다.
- 2026-09-20 현재 웹에서 확인 가능한 범위에서는 9월 21일 Open API+ 전용 점검/휴장 공지를 찾지 못했다.
  **검색 결과 없음은 공지 부재 인증이 아니다.** 당일 아침 KRX/키움에 새 특별 공지가 생겼는지만 delta-check한다.

공식 확인 위치:
- KRX 거래시간/휴장 규칙: https://global.krx.co.kr/contents/GLB/06/0602/0602020204/GLB0602020204T1.jsp
- 행정안전부 추석 연휴 안내: https://www.mois.go.kr/frt/bbs/type010/commonSelectBoardArticle.do?bbsId=BBSMSTR_000000000008&nttId=129490
- 키움 KRX 애프터마켓/NXT 제도개편: https://www.kiwoom.com/e/home/event/VEvent20260093View?dummyVal=0
- 키움 Open API+ 안내: https://www1.kiwoom.com/h/customer/download/VOpenApiInfoView?dummyVal=0

## 내일 다시 분석하지 않아도 되는 현재 코드 계약

현재 독립 실행은 다음이다.

```powershell
.\.venv32\Scripts\python.exe collector/kiwoom/kiwoom_universe_logger.py --storage raw-v2
```

`--storage raw-v2`는 기본값이라 생략 가능하지만, 운영 의도를 명시하려고 붙여도 된다.
독립 CLI에는 `--server` 옵션이 없다. 로그인 후 `GetServerGubun` 응답을
`0=live`, `1=mock`으로 해석하며 그 외 값은 시작 실패다.
**요청 server와 실제 server의 불일치 차단은 managed 실행에만 있다.**
따라서 독립 실행에서 live 여부는 로그인 창 선택과 로그인 후 기록된 identity/server로 대조한다.

기본 실행은 `--codes`, `--duration-seconds`, NXT/애프터마켓 계획을 주지 않는 기존 정규장 유니버스다.
코스피/코스닥 코드 목록에서 현재 코드/이름 필터를 적용하고 100종목씩 화면을 나눠 SetRealReg한다.
raw-v2에서 SetRealReg 반환이 0이 아니면 등록 실패로 종료한다. 종목 분류 정확성 자체는 별도 검증이다.

OCX 생성 전에 같은 체크아웃의 `CollectorLease`를 획득한다.
남아 있는 lock 파일 내용은 PID/생존 증거가 아니며 임의 삭제하지 않는다.
다른 체크아웃·다른 앱의 키움 로그인까지 이 lock이 막아 주는 것은 아니다.

로그인 성공 후 새 `LiveRawCapture`가 생성되며:
- session_id: 새 UUID hex
- raw: `sampledata/raw_ticks_v2/YYYYMMDD/<session_id>.db`
- evidence: `operations_state/capture_sessions/<session_id>/`
- latest status: `operations_state/capture_status.json`
- source: `kiwoom`
- feed_scope: `kiwoom_universe_venue_unverified`
- venue: 각 이벤트 `unknown`
- price_policy: `signed_magnitude`
- direction_policy: `signed_volume`
- 큐 capacity: 8,192 callbacks
- 저장 batch 최대: 512 callbacks

RawV2Writer는 파일을 `xb`로 새로 만들므로 기존 파일 overwrite/resume을 허용하지 않는다.
정상 체결의 FID15 명시적 +/-만 방향으로 사용하며, 무부호·0·비정상 값은 방향 미확인 품질 기록으로 보존한다.

중복 로그인/재접속 이벤트, FID 읽기 예외, 연결 끊김, 등록 실패, 큐 overflow,
잘못된 callback packet/수신시각, 저장 오류는 clean 완료로 바꾸지 않는다.
큐 overflow/invalid packet은 입력 수락을 중단하고 dropped를 증가시키며 파일을 정상 closed로 만들지 않는다.

기본 정규장 실행의 시간 종료 조건은 **Windows 로컬 시각 15:35**다.
수집기는 15:35에 종료 요청을 만들고, Qt 스레드에서 입력 중단/실시간 해제 후 저장 drain을 수행한다.
15:35는 DB 저장 완료 시각이 아니다.

clean 종료 후보의 최소 대조:
- 동일 session_id / dataset_path / code revision / server / feed_scope
- snapshot.state == closed
- accepting == false
- writer_closed == true
- error == null
- dropped_callbacks == 0
- pending_callbacks == 0
- accepted_callbacks == committed_callbacks
- finalization 존재
- committed_seq == finalization.final_seq
- starting → draining → closed 보고/저널의 일관성
- 종료 로그와 reason
- 본체 및 관련 실행 프로세스의 실제 종료
- raw 파일 크기와 LastWriteTimeUtc

`accepted_callbacks`는 callback 수이고 `committed_seq/final_seq`는 session_start·parse_error 등
제어 레코드까지 포함한 raw sequence이므로 서로 같아야 한다고 가정하지 않는다.
closed/CI/합성 성공만으로 데이터 품질·무누락을 승인하지 않는다.

## 내일 Astra/로컬에서만 다시 볼 delta-check

아침에는 저장소를 처음부터 재분석하지 않는다. 아래만 현재값으로 다시 확인한다.

1. `git status --short`, `git fetch origin`, HEAD/origin/master와 새 원격 commit/CI.
2. 다른 worktree/에이전트가 tracked 파일을 편집하거나 수집을 시작했는지.
3. 2026-09-20 점검 이후 KRX 특별 휴장/시간 변경 또는 키움 Open API+ 긴급 점검 공지가 새로 생겼는지.
4. Windows 날짜·시각·시간대가 KST인지.
5. 기존 Python 수집기, 다른 키움/KOA 접속, 관련 실행기·감시 프로세스.
6. collector lock과 managed capture 미해결 상태.
7. 저장 root와 C: 여유 공간.
8. `.venv32` Python 32bit와 공식 `--preflight` 결과, 현재 OCX 등록/파일.
9. 사용자가 login 창에서 **live**를 선택했고, 로그인 후 identity.server도 live인지.
10. 시작 후 실제 session_id/raw/evidence 경로와 초기 status가 running/accepting/error 없음인지.

이 중 불일치나 미확인이 있으면 로그인/구독을 밀어붙이지 않는다.

## Astra 사용량 최소화 전략

**아침 시작과 장 마감 후 검증을 두 작업으로 분리한다.**
Astra가 repository 전체를 다시 읽거나 장중 6시간 이상 계속 추론할 필요는 없다.

아침 작업은 위 delta-check와 preflight를 끝낸 뒤 collector를 시작하고 실제 session_id/server/raw 경로와
초기 running 상태까지만 확인한다. 이후 collector 자체는 Qt 이벤트 루프를 가진 독립 Python 프로세스로
15:35 종료 조건을 수행한다.

단, **저장소 코드는 “Astra 작업/셸 종료 시 자식 프로세스가 반드시 살아남는다”는 실행환경 계약을 제공하지 않는다.**
내일 로컬에서 실제 collector를 Astra 소유 임시 셸에 묶어 두지 않는 방식을 먼저 확인한다.
가능하면 일반 Windows PowerShell/터미널의 별도 프로세스로 시작하고 PID+시작시각+실행파일을 재확인한다.
Astra 작업 종료가 process tree 정리와 연결되는지 확정할 수 없다면 사용자가 자신의 일반 PowerShell에서
위 한 줄 CLI를 직접 실행하는 것을 안전한 fallback으로 사용한다. live 로그인 창 확인은 어차피 사용자 상호작용이 필요하다.

장중에는 status/log의 가벼운 관측 외에 코드 변경·추가 로그인·raw 조회·COUNT/checksum·표본 검사·백테스트를 하지 않는다.
15:35 이후에는 **같은 session_id**를 입력으로 별도 짧은 작업에서 종료 근거만 대조한다.

## 종료 후 다음

종료 근거가 맞으면 **첫 시험 후보**로만 올린다.
다음은 [최초 100건 표본](TICK_RESEARCH_RUNBOOK.md#닫힌-raw의-제한된-표본-대조)
→ 명시적 중간/말미 제한 구간 → 별도 장외 whole-file 무결성·품질 검사 → 첫 시험이다.

대용량 입력의 검사 전용 절차는 첫 연구 실행 전에 확정해야 한다.
현재 직접 연구 CLI는 전체 검증과 전략 재생을 함께 진행하므로 검사 전용으로 사용하지 않는다.
이는 신규 수집의 선행 조건은 아니다.

## 과거 로컬 관측의 근거 위치

2026-09-20 preflight 보고: `operations_state/preflight_20260920/report.md`.
당시 `.venv32` Python 3.10.11/32bit, OCX/preflight 성공, 수집기 부재, 잠금 획득/해제,
C: 여유 약 1.25 TiB와 working tree clean이 보고됐다. **현재 상태로 재사용하지 않는다.**

32bit 합성 probe 성공:
`operations_state/live_backend_probes/0067ae46003e4b35834086ac7f38211c/result.json`.
Git 제외 로컬 근거이므로 필요할 때 로컬에서만 대조한다.

## 계속 지킬 경계

합성/CI 통과 ≠ 실제 raw 품질, closed ≠ whole-file 검증, 표본 일치 ≠ 전체 정상,
입력 검증 ≠ 전략 수익성, 첫 백테스트 ≠ 실거래 승인이다.
`Daily_baseline`·`old_data`·운영 raw·operations_state·사용자 변경을 보존한다.
