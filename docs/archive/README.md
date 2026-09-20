# 과거 기록과 보존본

현재 작업은 [HANDOFF](../../HANDOFF.md), 문서 선택은 [README](../../README.md)를 따른다.
이 폴더의 문서는 당시 판단을 추적하는 자료이며 현재 실행 지시가 아니다.
본문의 "현재", "다음 작업", "완료"는 기록 당시를 뜻한다. 현재 코드·운영 관측과 대조한다.

## 2026-09-20 구조 정리 전 원문

기준 커밋은 `1ca29221598830d521530a0b2f9bb64961c2086d`다.
다음 두 파일은 기존 Git blob을 그대로 재사용했다. 내용·바이트·기존 오판까지 수정하지 않았다.

| 보존본 | 원래 경로 | Git blob SHA |
|---|---|---|
| [인계 이력 전체](HANDOFF_20260920_PRE_STRUCTURE_REVIEW.md) | `HANDOFF.md` | `fdb32bd15727d7122a9009c5f8923cac1f3a6ee6` |
| [입력 후보 조사와 체크리스트](BACKTEST_TODO_20260920_PRE_STRUCTURE_REVIEW.md) | `BACKTEST_TODO.md` | `d7fe2cdadd9ea349a940b91be59093ef112b59c8` |

**상대 링크의 기준 위치도 원문 그대로다.** 보존본 안의 상대 링크는 archive 디렉터리가 아니라
당시 저장소 루트를 기준으로 작성됐다. 문서 간 이동·옛 절 제목 대조에는
[당시 HANDOFF](https://github.com/Peter-jackson12/Stock/blob/1ca29221598830d521530a0b2f9bb64961c2086d/HANDOFF.md)와
[당시 BACKTEST_TODO](https://github.com/Peter-jackson12/Stock/blob/1ca29221598830d521530a0b2f9bb64961c2086d/BACKTEST_TODO.md)를 사용한다.
현재 문서로 연결을 임의 변경하면 역사적 의미가 달라질 수 있으므로 보존본은 고치지 않는다.
활성 문서의 링크 검사와 보존본의 blob 동일성 검사를 구분한다.

9월 17일 방향 불일치 비율·9월 18일 제한 표본·로컬 preflight의 상세 근거는 위 인계 이력에 있다.
표본의 비율을 전체 파일 오류율로 확대하거나 과거의 로컬 관측을 현재 상태로 재사용하지 않는다.

## 더 이전 기록

- [2026-09-16 인계](HANDOFF_20260916.md): rev.2 검토와 당시 개발 기록.
- [상시 응답 연결 전 인계](HANDOFF_20260916_PRE_HEARTBEAT.md): 이전 운영 연결·실측 기록.
- [인덱스 전환 전 README](README_PRE_INDEX.md): 과거 문서 구조와 의사결정 기록.

새 인계를 작성할 때 지난 이력을 현재 HANDOFF 아래에 계속 붙이지 않는다.
현재 결정·차단 조건·다음 행동·근거 위치만 갱신하고, 보존이 필요하면 날짜와 원본 revision을 함께 남긴다.
