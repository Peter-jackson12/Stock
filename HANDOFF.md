# 현재 인계 — 2026-09-23 / PR #21 재검토와 독립 진단 트랙

[문서 인덱스](README.md) · [세션 근거 표시 계약](docs/SESSION_ASSESSMENT.md) ·
[운영 제어](CONTROL_TOWER.md) · [수집 런북](docs/COLLECTION_RUNBOOK.md) ·
[파이프라인 지도](docs/PIPELINE_MAP.md) · [첫 시험 체크리스트](BACKTEST_TODO.md) ·
[보존본 안내](docs/archive/README.md)

## 시작 규칙과 작업 분리

매 작업 시작 시 원격 master/열린 PR/최신 CI를 다시 확인하고 AGENTS → HANDOFF를 읽는다.
기준 master: `5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70`, master CI #195 success.
GitHub만 사용하는 대화는 사용자 Windows/로컬 git/pytest/OCX/raw/evidence에 접근하지 않는다.
로컬 working tree나 현재 PID 상태를 원격에서 확인했다고 쓰지 않는다.

| PR | 담당 범위 | 기준 HEAD / 상태 |
|---|---|---|
| #18 | 이 대화 초반의 **일반 ChatGPT 컨트롤타워 운영 판단 계약**: 읽기 순서, 장전/KRX/NXT 구분 | `65a2feb333d81f3666ce41ee58d23e15199e0b69`, draft/unmerged |
| #19 | 과거 revision 고정 합성 입력 비교 | `32e6285e2bd5a224a63152c2ed1f492b57afd841`, draft/unmerged |
| #20 | Mock 전용 FID A-B-A 실험 준비 | `c263003da6be82cefe740d6b22896babfcc89cbc`, draft/unmerged |
| #21 | **운영 UI의 근거 표시**와 회귀 보강. 전면 아키텍처 재구축이 아님 | `refactor/control-tower-evidence-model-20260923`, 최종 HEAD/CI는 PR에서 재확인 |

#18의 운영 계약과 #21의 UI를 같은 작업으로 부르지 않는다. ARCHITECTURE_V2는 레거시 배경이며
raw→LOB/feature가 현 raw-v2 틱 연구의 필수 선행 단계가 된 것은 아니다.
PR #18/#19/#20은 이번 재검토에서 수정하지 않는다. #21도 자동 merge하지 않는다.
CI 성공과 native 오류 해결, 실행 승인, 실제 원본 검증은 서로 다르다.

## PR #21 재검토 결과와 다음 행동

초기 HEAD `db5c24b97a13d010e760694c9910de6bdbf9343a`의 PR CI #226은
1,578 passed / 6 deselected였다. 성공했지만 다음 의미적 오판은 테스트가 놓쳤다.

- 일일 로그 recent는 계수 재출력일 수 있고 session_id도 없다. 세션 콜백 진행으로 승격하지 않는다.
- stale active 보고는 현재 활성으로 표시하지 않는다. 시각/마지막 보고 상태는 보존한다.
- 정상 실행 중 OCX 창 존재는 native 잔류가 아니다. 저장 닫힘 뒤 잔존과 active Runtime 오류를 분리한다.
- runtime 관측은 full identity와 UTC 시각/TTL을 확인한다. lease free는 종료 증거가 아니다.
- 알려진 diagnostic feed_scope는 표시에서 연구 입력 제외로 남기며 임의 eligible 승격은 받지 않는다.
- reader/reducer의 status 검증을 공유하고, 표시 컴포넌트 UI 회귀를 추가한다.

상세 구현·한계는 [전용 계약](docs/SESSION_ASSESSMENT.md)에만 둔다.
프로세스/창/lease 관측 연결, source-clock 정책, 연구 실행 진입점의 diagnostic 강제 배제는 아직 별도다.
새 상태 표시를 기존 시작/재시작 권한에 연결하지 않는다. collector/queue/OCX 기본 동작은 변경하지 않는다.
수정 후 최종 HEAD·집중/전체 CI 로그는 PR #21에서 확인한다. 중복 검사는 합산하지 않고 deselected는 통과가 아니다.

다음 독립 작업은 PR #20의 코드/실험 설계 리뷰다. 아직 시장에서 실행하지 않았다.
None으로 생략한 FID는 COM 호출 수뿐 아니라 문자열/JSON/저장 비용도 바꾸므로 완전한 단일 비용 실험이라고
과장하지 않는다. 30초 phase의 carry-over, 표본 종목 차이, source clock 해석, 진단 데이터 연구 제외 가드를
실행 전에 대조한다. 이 인계만으로 새 collector를 실행하지 않는다.

## 2026-09-23 장애 — 사용자 전달 로컬 보고

아래 native/운영 수치는 사용자가 전달한 로컬 에이전트 보고다. 이 GitHub 작업이 원본을 직접 재검증한 것은 아니다.
절대 시각은 KST이며 보고 시점 이후 현재 프로세스 상태를 인증하지 않는다.

session `7a35b11eddff4dcea88c98acdc8b37df`, collection revision `5b5156f...`,
PID 13572, CPython 3.10.11 x86, live, telemetry ON / explicit teardown OFF.
accepted=committed=12,416,350, final_seq=12,428,917 보고. raw 전체 품질은 미검증이다.

09:00 이후 FID20/FID21 차이가 증가해 약 1,700초에 도달했다.
memory recorded peak 10:08:19.624(commit 1,767.7 MiB/WS 1,747.7 MiB), 하락 첫 관측 10:11:22.124.
마지막 저빈도 표본 10:36:18~19, status last_commit 10:36:21.189, stall 10:38:21.937,
silence shutdown 10:46:21대. last_commit/표본을 전수 마지막 callback 시각과 동일시하지 않는다.
source progression과 lag slope는 같은 source/receive 시계에서 계산하므로 독립 증거가 아니다.
종목 교차 표본의 비율 중앙값도 전체 이벤트 service rate가 아니다.
작은 sampled processing_ns, poll 왕복, empty Python queue로 callback/Qt/GIL 전체 병목을 배제하지 않는다.

popup 최초 시각은 미확정. 디스크 직접 관측은 10:46:36.764~10:47:08.204 창 안에 있으며
first appearance ≤ 10:47:08.204라는 상한만 있다. last-absent 없음.
과거 대화의 10:36:33 존재 주장은 이 보존 파일 감사로 재입증되지 않았다.
WER 14:08:01.477은 늦은 APPCRASH 기록이며 popup 최초 시각이 아니다.
저장·Qt finally·lease 해제 뒤에도 PID/Runtime 창이 남았다는 사전점검 보고가 있다.
이후 canary들 전에는 부재를 다시 확인했다는 보고다. lease alone으로 재시작을 승인하지 않는다.

### 보존 dump 재감사 — 로컬 보고와 해석

11:30:43경 사후 dump, 4,538,551 bytes, SHA-256
`5BCA2EFC651DBCEB5CE1C09642FF0CE5E729890069A099EFA662BDEE78B50C68`.
exception stream은 없지만 stack의 저장 context와 로컬 image 대조로
`OPComms → operator new → malloc NULL → AfxNewHandler → AfxThrowMemoryException`,
요청 `0x3e14=15,892 bytes`를 복원했다는 보고다. 실제 python.exe LAA OFF.
dump-time VA: commit 199.59 MiB, reserve 1,757.36 MiB, free 90.98 MiB, 최대 free 60.31 MiB.
full-memory는 아니나 memory-region metadata 7,358개가 있고 heap header는 부족했다.
allocation failure는 지지되지만 failure-time VA exhaustion/fragmentation/최초 callback stop 인과는 미확정이다.
dump-time free를 10:08이나 최초 실패 순간으로 소급하지 않는다.

### 규모별 분리 시험 — 재실행하지 말고 보고를 재사용

1종목 60초 mock/live 및 10종목 60초 ABBA: FID 차이 약 1~3초, 저장 닫힘/PID·창 부재 보고.
3,757 후보 전종목 60초: Mock 약 1,602/s, Live 약 1,533/s; 차이는 약 2→16~20초,
메모리 약 +20~31 MiB, 저장 queue reported peak 71~82, accepted=committed/drop 0.
별도 진단 launcher 사용, 등록 중 callback 포함 약 63초 평균이며 CLI full-universe 제한과 구분한다.
실수집 순차 입력은 같지 않고 mock 외부 sampler는 wrapper PID를 측정해 비교에서 제외됐다.

x86 offline raw-v2: 96,000 callback/60초/1,600/s, capacity8192/batch512,
계획 유지·queue sampled peak32·drain0.016초·메모리 약+3 MiB 보고.
이는 그 고정 입력의 저장 경로 근거일 뿐 실제 burst/Qt/COM/GIL 병목 배제나 native 해결이 아니다.

로컬 근거 위치는 사용자 보고된 repository-relative 경로다. 원격에서 존재/해시 확인한 것이 아니다.
- `operations_state/offline_queue_replay/20260923T154733KST/`
- `operations_state/forensics/20260923_session_7a35_timeline_20260923T161353/`
- `operations_state/forensics/20260923_runtime_timing_20260923T162202/`
- `operations_state/forensics/20260923_dump_va_audit_20260923T163251/`

## 계속 유효한 과거 차단 조건 — 삭제하거나 완료로 바꾸지 않기

직전 원문: [master 기준 HANDOFF](https://github.com/Peter-jackson12/Stock/blob/5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70/HANDOFF.md).
CI #156 flush 예외 close 누락 / #162 일회성 raw startup 실패 이력을 유지한다.
보강 후 통과와 별개로 #162 근본 원인은 미확정이다. PR #19 중간 실패 #205/#206도 최종 성공으로 지우지 않는다.

9월 21일 session `6f39117671c048f6b60477ceafbf40b6`, revision `4821762fd93230b658339fee084d6c08e3e53ce9`:
raw 50,635,071,488 bytes; callbacks42,796,226/final_seq42,836,791.
파일 SHA 주장 `E4304FE3C1CAD8A85EC6C297CEB9CFDADCA2D20001E93756303D567EC5077569`,
payload SHA 주장 `a988d3bf86e36f44a209480658f537088a8910768c68c9fec83349f3de7f1755`는 대상이 다르고 재검증 안 됐다.
계수 차이 40,564는 parse_error 예상 단서이지 SQL 집계/무부호 체결 수가 아니다.
말미 unsigned FID15 5건 및 대응 parse_error로 FIRST_RESEARCH_CANDIDATE 미승격.
-wal 0 bytes / -shm 32,768 bytes는 보존. 원본 in-place writable 재연결/cleanup 미채택.
whole-file 무결성·품질/시간/종목 분포와 첫 실제 연구는 미완료다.
복제·sidecar 작업은 identity/namespace/외부 접근 배제/free space/I/O·시간 예산/실패 보존 설계와 별도 승인 필요.
합성 clone lab 성공으로 실제 50.6GB 처리나 삭제가 승인되지 않는다.

9월 22일 session `39b5af8b45024458be9a3ae2a2259685`, revision `6a6d6076649befc767e5d8d59151cbcfb2f27c34`:
마지막 callback10:23:47, 종료10:33:48, accepted=committed11,198,913/final_seq11,212,670 보고.
raw12,940,107,776 bytes와 21일50.6GB를 혼동하지 않는다. 17:07 WER는 최초 popup 시각이 아니다.

`Daily_baseline`, `old_data`, 운영 raw/dump/기존 operations_state를 보존한다.
closed != process exited != research eligible; stream integrity != strategy validation.
원본 폐기·시간 절단·방향/venue 보정·LAA·queue 확대·자동 kill/restart/relogin은 승인하지 않는다.

## 일반 ChatGPT / 로컬 에이전트 인계

GitHub에서 가능한 읽기·수정·PR·Actions는 일반 채팅에서 직접 처리한다. Work 자동 위임 금지.
Windows/OCX/실제 raw가 필요한 일만 완성형 복사 프롬프트로 넘긴다.
모델·추론·상향 조건·새 스레드 권장은 **프롬프트 코드블록 밖**에 둔다.
목표·금지선·예산 안의 방법/순서는 자율적으로 맡기되 권한 확대는 금지한다.
이미 시킨 작업에 중복 실행 프롬프트를 주지 않는다. 결과 검토 후 필요한 보충만 분리한다.
