# 원본 보호 handle 기반 격리 복제 lab

관련 코드: [lab](../scripts/lab_raw_v2_clone.py), [회귀](test_raw_v2_clone_lab.py).
현재 원격 적용/CI 상태는 [HANDOFF](../HANDOFF.md)와
[PR #15](https://github.com/Peter-jackson12/Stock/pull/15)의 정확한 HEAD/run/job을 확인한다.

## 목적과 적용 경계

이는 [틱 연구 실행 안내](../TICK_RESEARCH_RUNBOOK.md)의 “다음 합성 계획 — 원본 비변경 복제본 경로” 중
**출처가 이 lab 안에서 생성·관측된 residue-only fixture**에 대한 부분 구현이다.
이 명세는 해당 부분의 구현 상태를 갱신한다. 계획 전체의 완료나 운영 적용을 선언하지 않는다.

외부 DB/출력 경로를 받는 CLI가 아니다. Windows local NTFS의 UUID TEMP root를 스스로 만든다.
32 MiB/file, 256 MiB/root 범위만 다룬다. 50.6 GB 운영 DB를 이 도구에 넣는 경로는 없다.
사용자 PC, raw/evidence/operations_state, collector/OCX, 전략, TickSimulator와 production qualification 코드를 변경하지 않는다.

## 검증 단계

1. CaptureSession으로 clean 또는 unsigned FID15 한 건을 만든다. 종료된 main의 논리 스냅샷과
   기존 qualification 결과를 기준으로 남긴 뒤, 이 fixture에 한해서 mode=ro reader residue의 출처를 만든다.
2. main/WAL/SHM 이외 파일, rollback journal, 누락, 비정상 크기, non-empty WAL, hardlink/reparse,
   fixture 생산 당시 bytes/stat/identity와의 차이를 거부한다. WAL 0 B와 SHM 32 KiB만으로 통과시키지 않는다.
3. 원본 세 파일을 모두 exclusive read handle로 확보한다. 기존 reader/writer/mapping과 충돌하면 실패한다.
   같은 handle에서 64 KiB chunk로 원시 `evidence/`와 처리용 `working/`에 따로 복사한다.
   새 destination은 `xb`로 만들며 충돌 파일을 덮어쓰지 않는다. source를 경로로 다시 open해 복사하지 않는다.
4. 전 파일의 크기/hash/비동일 identity를 대조한다. 원시 evidence에는 write/delete 배제 handle을 유지한다.
   원본에는 SQLite로 재연결하지 않고 모든 source handle을 후속 검증 종료까지 유지한다.
5. working 복제본에서만 SQLite-managed metadata page read + explicit close를 수행한다.
   sidecar가 남으면 immutable 우회를 하지 않는다. main hash와 manifest/seq/payload가 달라져도 실패한다.
6. sidecar 없는 working에 **변경하지 않은 기존 qualification**을 적용해 whole iterator를 소진한다.
   scan_complete/integrity/eligibility/counts/quality_diagnostics를 생산 단계 기준과 비교한다.
   원시 evidence와 원본 bytes/stat/identity를 재확인한 경우에만 `synthetic_copy_verified=true`다.

이 정상 경로에서 clean은 integrity=true/eligible=true, unsigned는 integrity=true/eligible=false를 기대한다.
unsigned 1건과 대응 parse_error 1건, trade_direction_unverified 이유·시간/종목 분포를 그대로 보존한다.
어느 경우에도 `production_approved`는 false다. parse_error를 corruption으로 재분류하지 않는다.

## 실제 이름 공간 검증에서 발견한 공백

초기 `dwDesiredAccess=0` directory handle은 빈 working 디렉터리 rename을 막지 못했다.
[진단 CI #96](https://github.com/Peter-jackson12/Stock/actions/runs/35700877506/job/106658335440)에서
source/main/WAL/SHM 접근 거절 뒤 working 이름 변경과 후속 identity 확인 실패를 관측했다.
실패 결과가 기록됐으며 성공으로 처리하지 않았다.

`FILE_LIST_DIRECTORY` 접근과 delete-sharing 배제로 바꾸고,
**같은 empty-directory 입력에서 access=0의 교체 가능 반례 / access=1의 교체 차단**을 회귀로 남긴다.
원본·복제본의 기존 조상 이름도 같은 방식으로 보호하고 handle/path의 volume/file identity를 비교한다.
Python CRT file open의 PermissionError/EACCES와 직접 Win32 호출의 WinError 32는 서로 다른 오류 표현이다.
자식 process 검사는 기존 파일이 평소 읽기/쓰기 가능함도 확인하며, SQLite 오류는 CANTOPEN으로 한정한다.

이는 **기존 이름의 교체 차단**이다. 디렉터리 안의 새 자식 생성 권한을 없애는 일반 namespace 격리는 아니다.
합성 후발 journal 생성은 실제 가능하며, inventory 재검사로 탐지해 실패시키고 journal도 남긴다.
검사 사이에 생성됐다 제거되는 순간 상태까지 연속적으로 배제했다고 주장하지 않는다.

## 회귀와 실패 기록

회귀는 clean/unsigned, 원본 SQLite 재연결 부재, 기존 read/write/SQLite reader,
Python writable mapping, 후발 자식 reader/writer, 파일 삭제·rename/조상 rename,
empty-directory 권한 반례, 복사 전/중/후 및 cleanup/qualification 사이의 중단,
non-empty WAL/journal/미상 파일/누락/alias/출처 불일치,
destination 충돌/hash 오류/잔여 sidecar/main 변경, I/O·시간 한도를 검사한다.

예외 시 원본과 이미 생성된 evidence/working/result를 삭제하거나 자동 재시도하지 않는다.
`status=failed`, verified/eligible=false로 남긴다. 예외 연결 traceback도 보존한다.
KeyboardInterrupt/SystemExit는 실패 기록 뒤 다시 전달한다.
원본 검증을 완료하지 못하면 `source_unchanged`를 true로 채우지 않는다.

CI는 Python/SQLite/Windows 버전과 pytest 요약을 로그에 출력한다.
최초 구현 기준 CI #92는 **1 failed / 1451 passed / 6 deselected**였다.
이를 전체 성공으로 세지 않는다. 이후 수정의 최종 검증은 PR의 최종 HEAD 로그를 따른다.
집중 단계와 전체 단계는 일부 테스트를 반복 실행하므로 pass 수를 서로 더하지 않는다.

## 이번 결과가 인증하지 않는 것

- 내부 Fixture/marker는 외부 입력을 인증하는 보안 API가 아니다. 별도 임시 작업 공간과 협조적 lab 생성 단계가 전제다.
  획득 이전 경쟁, 미관측 sidecar 출처, 악의적 관리자/커널 actor, 모든 mapping 형태를 포괄하지 않는다.
- working은 SQLite가 writable로 열어야 하므로 파일 handle을 계속 exclusive로 잡을 수 없다.
  작업 공간의 비협조적 쓰기·순간 교체·최종 검사 후 변경을 일반적으로 배제하는 운영 격리는 아직 없다.
- stream read/write 계수는 source streaming과 destination write만 센다.
  SQLite 내부 I/O와 일부 evidence 재해시는 제외된다. 총 물리 디스크 I/O 계수·50.6 GB 처리 예산이 아니다.
- 20초 제한은 협조적 checkpoint 검사다. 강제 프로세스 종료/전원 장애/fsync 실패/디스크 full까지
  반드시 완결된 failed JSON이 남는다는 보장은 없다. CI timeout도 복구 프로토콜을 대신하지 않는다.
- TEMP 증거는 실행 환경에 남는다. 현재 CI는 이를 별도 artifact로 영구 업로드하지 않는다.
  CI 로그 밖의 raw 복제 증거를 다운로드 가능한 장기 보존본이라고 설명하지 않는다.

따라서 실제 적용에는 대상 identity·sidecar 출처, 외부 reader/writer와 namespace 격리,
장외 시각, collector 부재, free space, 총 I/O/time 예산, 실패 보존 설계와 별도 사용자 승인이 필요하다.
qualification을 전략 검증·FIRST_RESEARCH_CANDIDATE·실거래 승인으로 승격하지 않는다.

## 외부 근거

[Microsoft CreateFile](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilea)는
access/share/delete의 차이와 attribute 접근 예외를 설명한다.
[SQLite WAL](https://www.sqlite.org/wal.html)은 WAL이 데이터베이스 상태 일부이며 main-only 복사가
committed 내용을 놓칠 수 있음을 설명한다. 두 문서 모두 운영 residue 삭제 승인을 대신하지 않는다.
