# 과거 수집기 revision 고정 입력 비교

이 진단은 2026-09-23 native/live 수집 장애를 좁히기 위한 **GitHub-hosted 합성 비교**다.
운영 raw/evidence/OCX/사용자 Windows 체크아웃을 읽지 않는다. 병합된 PR #18(수집 판단 계약)·
PR #21(세션 근거 표시)·PR #29(FID 진단)와 별개의 과거 진단 기록이다.
[문서 인덱스](../README.md) · [테스트 안내](../docs/TESTING.md) · [비교 스크립트](../scripts/benchmark_collector_revisions.py)

## 비교 revision

| label | revision | 당시 운영 단서 |
|---|---|---|
| 2026-09-18-mock | `f352e024fdde24b966846b783b60eb4dd9d45495` | mock 장전~마감 저장 종료 세션의 당시 코드와 동일 tree 계열 |
| 2026-09-21-live | `4821762fd93230b658339fee084d6c08e3e53ce9` | live 장전~마감 저장 종료 |
| 2026-09-22-live | `6a6d6076649befc767e5d8d59151cbcfb2f27c34` | live 오전 수신 중단 세션 |
| 2026-09-23-live | `5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70` | live telemetry ON 장애 세션의 collection revision |

9/18의 역사적 실행 SHA `f7f4e7c5...`는 전체 이력 한국어화 전 ID이고,
위 `f352e024...`는 대응되는 현재 Git 이력의 같은 tree다.

## 고정 입력

각 revision을 별도 detached worktree로 열고 같은 Python 프로세스 환경에서 다음을 실행한다.

- fake Qt/OCX, server flag는 항상 mock(`1`)
- 종목 `005930`
- 한 pair = 체결 1건 + 호가 1건
- 체결 FID 6개, 호가 FID 41개를 동일한 원문 값으로 제공
- 기본 2,000 pair = 4,000 callback
- warm-up 100 pair 뒤 3회 반복 median
- 실제 `LiveRawCapture` → `QueuedCapture` → SQLite 저장/drain 경로 사용
- stats/resource logging은 제거해 callback/storage 경로에 집중
- queue capacity 이하의 입력을 사용해 producer overflow를 성능 결과와 섞지 않음

모든 revision의 기본 비교에서는 telemetry를 끈다. 그 뒤 9/23 revision만 같은 입력으로
telemetry ON을 한 번 더 실행해 새 진단 경로의 추가 Python 비용을 분리한다.

## 측정값

- callback submit 구간 callbacks/s
- shutdown drain까지 포함한 end-to-end callbacks/s
- FID read 호출 수
- accepted/committed/closed 저장 불변식

성능 수치에는 CI 실패 임계값을 두지 않는다. hosted runner 노이즈 때문에 작은 비율 차이를
회귀로 자동 판정하지 않고, **큰 차이가 있는지와 어느 revision 경계에서 생기는지**를 보는 진단이다.
저장 불변식이나 FID 호출 수가 달라지면 실패한다.

## 해석 금지선

이 비교로 다음을 인증하지 않는다.

- 실제 32-bit CPython/2 GiB VA의 처리율·메모리 한계
- QAx/COM/native callback delivery, Qt event scheduling, GIL/native 상호작용
- mock/live 서버의 실제 이벤트량·burst·지연
- 키움 서버 freshness, FID cache 의미, 공급자 무누락
- Runtime popup 또는 `AfxThrowMemoryException`의 원인
- production 변경의 안전성

따라서 결과가 revision 사이 비슷해도 native/live 병목을 배제하지 않는다.
반대로 한 revision이 CI에서 느려도 실제 시장 장애의 원인으로 바로 승격하지 않는다.

## 실행 방식 — 명시적 진단 전용

일반 `ci.yml`은 master push와 pull_request의 Git-only 회귀를 유지하되 이 역사적 벤치마크를
매번 실행하지 않는다. 기존 스크립트·고정 revision·검증 불변식은 보존하며, 새 자동 workflow나
성능 gate를 추가하지 않는다. 일반 CI 성공을 이번 벤치마크의 재실행 성공으로 기록하지 않는다.

재측정이 필요한 별도 요청이 있을 때만, 운영 저장소가 아닌 폐기 가능한 개발/hosted 검증
checkout에서 기존 64비트 개발 환경으로 다음 명시적 명령을 사용한다.

```powershell
uv run --locked --offline python scripts/benchmark_collector_revisions.py --pairs 2000 --repeats 3
```

`--offline`은 uv 의존성 처리 옵션이지 전체 스크립트의 네트워크 차단이 아니다. 필요한 과거 commit이
없으면 스크립트는 `git fetch --no-tags --depth=256 origin master`를 시도하고, 임시 detached worktree를
생성·제거한다. 기존 운영 checkout·운영 `.venv32`에서 실행하지 않는다. 이 문서 자체는 실행 승인이 아니다.

## 보존된 완료 근거

[원래 검토 HEAD](https://github.com/Peter-jackson12/Stock/commit/32e6285e2bd5a224a63152c2ed1f492b57afd841),
[push CI #207](https://github.com/Peter-jackson12/Stock/actions/runs/35818078661),
[PR CI #208](https://github.com/Peter-jackson12/Stock/actions/runs/35818345055)과
[PR #19의 측정 결과](https://github.com/Peter-jackson12/Stock/pull/19)를 보존한다.
#205/#206의 benchmark harness 수명주기 실패도 삭제하지 않는다. `_shutdown()` 뒤 실제 `main()`의
`_finish_process_resources()`까지 따라 telemetry handle을 마무리하도록 보정한 이력이다.
이 역사적 성공은 현재 native/live 장애 원인 확정이나 미래 성능 보장이 아니다.
