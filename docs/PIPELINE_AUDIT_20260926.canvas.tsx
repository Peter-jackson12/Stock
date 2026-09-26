import { Stack, Row, Grid, H1, H2, Text, Table, Button, Divider, useCanvasState, useCanvasAction, useHostTheme } from "cursor/canvas";

const root = "C:/Projects/TotalStock";
const wt = root + "/worktrees/mwfd-04-full-run";
const run = root + "/_data/mwfd_04/20260926T084815+0900-1286-cell-full-run";
type Finding = { priority: string; stage: string; title: string; effect: string; action: string; evidence: [string, number, string][] };
const findings: Finding[] = [
  {
    priority: "P1", stage: "완료 판정 전", title: "검증 FAIL도 COMPLETE로 게시할 수 있음",
    effect: "교차 검증은 FAIL을 기록해도 종료 코드 0을 반환합니다. 최종 manifest는 교차 검증·재개 검증 PASS와 필수 산출물의 완비 여부를 검사하지 않고 COMPLETE를 기록합니다. 현재 완료 결과가 잘못됐다는 뜻이 아니라, 앞으로 잘못된 완료 판정이 가능한 코드입니다.",
    action: "셀 계산 완료와 최종 승인 상태를 분리하고, 필수 검증 PASS·필수 산출물 확인 뒤 완료 표식을 마지막에 게시합니다.",
    evidence: [[wt + "/scripts/finalize_mwfd_04.py", 283, "교차 검증 결과"], [wt + "/scripts/finalize_mwfd_04.py", 676, "최종 COMPLETE 조건"]]
  },
  {
    priority: "미해결", stage: "완료 판정 전", title: "0161M0 셀은 호스트 시간 제한으로 미완료",
    effect: "실패 기록의 셀 252(0161M0=unknown, 173,892 events)는 feature 계산이 약 180초 호출 제한에 걸렸습니다. 문서가 권고한 Windows 실행 대신 실제 manifest는 Linux VM·2 CPU·분할 호출을 기록합니다. 나머지 셀은 진행 중이지만 이 셀의 완료 증거 없이는 1,286셀 완료가 아닙니다.",
    action: "현재 계산과 체크포인트를 보존합니다. 실패 셀의 동일 입력·동일 계산 결과를 유지하는 복구 방법을 별도 검증한 뒤 해결하고, 그전에는 PARTIAL로 판정합니다. 이번 감사는 재시도나 실행 환경 변경을 하지 않았습니다.",
    evidence: [[run + "/failures/0252-0161M0-unknown.json", 1, "실패 셀 기록"], [run + "/run_manifest.json", 1, "실제 실행 환경"], [root + "/MWFD-04_local_agent_handoff.md", 80, "원래 실행 지침"]]
  },
  {
    priority: "P2", stage: "완료 판정 전", title: "결합·요약의 중단 후 재개가 불완전",
    effect: "4개 출력의 진행 상태를 순차 저장하므로 그 사이 중단되면 위치가 달라져 재개가 막힙니다. 일부 파일만 최종 게시된 경우도 복구가 어렵습니다. 요약은 full_run_summary.json 하나만 있으면 완료로 간주하지만 gate/runtime/factor 파일은 나중에 만들어집니다.",
    action: "여러 출력의 공통 저장 지점을 두고, 임시 파일 집합을 검증한 뒤 완료 표식을 마지막에 게시합니다. 중간 종료를 주입하는 합성 검증은 실행 종료 후 별도 환경에서 수행합니다.",
    evidence: [[wt + "/scripts/finalize_mwfd_04.py", 149, "결합 위치 일치 검사"], [wt + "/scripts/finalize_mwfd_04.py", 187, "순차 저장·최종 게시"], [wt + "/scripts/finalize_mwfd_04.py", 342, "요약 재개 조건"]]
  },
  {
    priority: "P2", stage: "완료 판정 전", title: "재현성 기록과 성능 집계의 범위 누락",
    effect: "고정 해시 목록에 경제 계산이 참조하는 engine/nxt_tick_engine.py와 후처리 코드가 없습니다. finalizer·호스트 보조 스크립트는 Git 미추적 상태입니다. 보조 스크립트는 자체 해시를 예외 기록에 남기지만, 성능 요약은 run_log만 집계하여 실제 사용된 예외 실행의 시간·CPU·메모리를 놓칩니다.",
    action: "현재 파일과 예외 실행 이력을 보존하고, 후처리 버전·의존성·해시를 별도 확정합니다. 성능 보고에 예외 실행을 포함하고 실패 이력과 미해결 실패를 구분합니다. 현재 코드가 오염됐다고 관측된 것은 아닙니다.",
    evidence: [[wt + "/scripts/materialize_mwfd_04_events.py", 52, "코드 해시 목록"], [wt + "/research/fast_backtest/sweep.py", 10, "경제 계산 의존성"], [wt + "/scripts/mwfd_04_host_limit_step.py", 54, "예외 실행 기록"], [wt + "/scripts/finalize_mwfd_04.py", 500, "성능 집계 범위"]]
  },
  {
    priority: "P2", stage: "실행 종료 후", title: "경로·환경·진행 문서가 checkout별로 다름",
    effect: "main 지침은 신규 데이터도 StockSnapshots에, MWFD-04 지침은 기존 evidence 보존 + 신규 _data/snapshots에 두도록 합니다. README는 가상환경 재생성을 권고하지만 현재 대표 activation/launcher는 새 경로를 사용합니다. MWFD-04 HANDOFF는 여전히 MWFD-03 완료 상태입니다.",
    action: "기존 evidence와 과거 경로 provenance는 보존하고, 신규 저장 위치·실제 환경 복구 결과·MWFD-04 최종 상태를 현재 문서에 통일합니다. 과거 경로를 일괄 치환하거나 환경을 다시 만들 이유는 없습니다.",
    evidence: [[root + "/Stock/AGENTS.md", 36, "main 데이터 위치"], [wt + "/AGENTS.md", 36, "연구 데이터 위치"], [wt + "/README.md", 120, "환경 안내"], [wt + "/HANDOFF.md", 1, "현재 인계 제목"]]
  },
  {
    priority: "P2", stage: "다음 연구 단계", title: "Fast/exact parity PASS와 회계 검증은 별개",
    effect: "exact bridge는 신호 시각·체결/거래 개수·PnL을 비교하지만 diagnostics 상태와 현금·포지션·회계 항등식 검증값을 확인하지 않습니다. 현재 MWFD-04는 screening-only라 이 단계는 실행 범위 밖입니다.",
    action: "향후 exact 승인 전에 parity와 accounting acceptance를 분리하고, 회계 불일치 결과가 PASS가 되지 않는 반례를 검증합니다.",
    evidence: [[wt + "/research/fast_backtest/exact.py", 37, "parity 비교 범위"], [wt + "/engine/nxt_portfolio_research.py", 132, "회계 검증 결과"]]
  }
];
const projectFindings: Finding[] = [
  {
    "priority": "P2",
    "stage": "다음 운영 전",
    "title": "시장 프로필이 회복·종료 판정에서 누락",
    "effect": "명시적 NXT 프로필을 사용해도 finish는 09:00~15:30 any-event 기준으로 종료를 평가합니다. 체결 침묵 경고의 회복도 호가를 포함한 last_event_ts로 판단하므로, 체결 없이 호가만 와도 수신 재개·healthy가 될 수 있습니다. 저장 false-closed 문제로 확인된 것은 아닙니다.",
    "action": "경고·회복·종료에서 동일한 프로필과 이벤트 종류 계약을 사용합니다. 호가만 재개되는 경우와 NOT_EXPECTED 구간 종료를 합성 검증합니다.",
    "evidence": [
      [
        "C:/Projects/TotalStock/Stock/collector/kiwoom/session_monitor.py",
        377,
        "침묵 회복"
      ],
      [
        "C:/Projects/TotalStock/Stock/collector/kiwoom/session_monitor.py",
        521,
        "종료 판정"
      ]
    ]
  },
  {
    "priority": "P2",
    "stage": "다음 운영 전",
    "title": "raw-v2 적체 경보의 임계치와 시간 창이 맞지 않음",
    "effect": "기본 경보 바닥 20,000건은 raw-v2 큐 8,192건 + batch 최대 512건보다 큽니다. 또한 시간 창 이전 표본을 전부 버린 뒤 창 전체가 경과해야 경고하므로, 10초 창에 1.2초 간격으로 표본을 받으면 최대 9.6초만 남아 경보가 불발됩니다. 큐 포화 시 incomplete로 중단하는 보호는 유지됩니다.",
    "action": "임계치를 실제 수용량에 맞추고 경계 직전 표본을 보존하거나 지속 시간을 별도로 추적합니다. 불규칙 간격과 기본 설정을 함께 검증합니다.",
    "evidence": [
      [
        "C:/Projects/TotalStock/Stock/collector/kiwoom/session_monitor.py",
        63,
        "기본 임계치"
      ],
      [
        "C:/Projects/TotalStock/Stock/collector/kiwoom/session_monitor.py",
        467,
        "시간 창"
      ],
      [
        "C:/Projects/TotalStock/Stock/collector/kiwoom/live_capture.py",
        40,
        "큐 용량"
      ]
    ]
  },
  {
    "priority": "P2",
    "stage": "다음 운영 전",
    "title": "오래된 미완료 작업이 화면에서 사라짐",
    "effect": "최신 30개에만 실행·취소 버튼이 제공됩니다. 계획 하나 뒤에 다른 작업 30개가 쌓이면 그 계획이 화면에서 사라집니다. 동일 계획 재등록도 중복 방지가 기존 ID를 반환하므로 UI에서 다시 접근할 수 없습니다.",
    "action": "미완료 작업은 별도 목록으로 항상 표시하고, 최근 개수 제한은 완료 이력에만 적용합니다.",
    "evidence": [
      [
        "C:/Projects/TotalStock/Stock/control_tower/jobs.py",
        69,
        "최근 목록"
      ],
      [
        "C:/Projects/TotalStock/Stock/control_tower/jobs.py",
        88,
        "동일 요청 재사용"
      ],
      [
        "C:/Projects/TotalStock/Stock/dashboard/operations.py",
        138,
        "목록 제어"
      ]
    ]
  },
  {
    "priority": "P2",
    "stage": "다음 운영 전",
    "title": "자식 초기화 실패가 launching 상태로 남을 수 있음",
    "effect": "프로세스 생성은 성공했지만 자식이 PyQt import·소유권 claim 전에 종료하면 완료 보고가 없습니다. 부모는 생성 호출 자체의 예외만 기록하므로 launching과 다음 시작 차단이 남습니다. UI 종료 요청으로 미claim 요청을 취소할 수 있으므로 자동 재로그인을 추가할 사안은 아닙니다.",
    "action": "자식 identity와 초기화 응답·실패 원인을 연결하고, 사용자가 시작 실패를 구별할 수 있게 합니다. 자동 정리보다 소유권 확인을 유지합니다.",
    "evidence": [
      [
        "C:/Projects/TotalStock/Stock/control_tower/managed_capture.py",
        283,
        "자식 생성"
      ],
      [
        "C:/Projects/TotalStock/Stock/collector/kiwoom/kiwoom_universe_logger.py",
        1228,
        "claim 경계"
      ]
    ]
  },
  {
    "priority": "P2",
    "stage": "다음 연구 단계",
    "title": "main에 마감 시각 사전 검증 수정이 미통합",
    "effect": "main의 NXT runner는 close_ns=0·음수·문자열에도 running 파일을 먼저 만들고, 하위 ValueError를 실패 기록으로 전환하지 못합니다. 입력은 실행되지 않아도 running 기록이 남습니다. MWFD 작업 폴더에는 출력 전 검증 수정과 테스트가 이미 있습니다.",
    "action": "실행 종료 후 운영 main과 연구 브랜치의 의도된 차이를 검토하고 해당 수정을 별도로 통합합니다.",
    "evidence": [
      [
        "C:/Projects/TotalStock/Stock/engine/nxt_portfolio_research.py",
        257,
        "main 출력 생성"
      ],
      [
        "C:/Projects/TotalStock/Stock/engine/portfolio_session.py",
        76,
        "하위 마감 검증"
      ]
    ]
  },
  {
    "priority": "P2",
    "stage": "다음 연구 단계",
    "title": "조회기가 모순된 완료 결과 일부를 받아들임",
    "effect": "completed_no_fills인데 open_quantity=1이거나, completed_empty_input인데 fills가 있는 조합을 거부하지 않습니다. 형식상 성공 필드가 맞으면 종료 코드 0이 됩니다. 엔진이 실제로 이런 결과를 만들었다는 관측은 아닙니다.",
    "action": "상태별 이벤트·체결·포지션 조건을 공통 검증기로 교차 검사합니다. 조회 성공과 연구 승인도 구분합니다.",
    "evidence": [
      [
        "C:/Projects/TotalStock/Stock/scripts/inspect_tick_research.py",
        53,
        "상태 교차검사"
      ],
      [
        "C:/Projects/TotalStock/Stock/control_tower/service.py",
        69,
        "화면 조회"
      ]
    ]
  },
  {
    "priority": "P2",
    "stage": "레거시 경로 사용 전",
    "title": "다음 날짜 피처 빌드가 앞 날짜 품질 기록을 지움",
    "effect": "FeatureStore.write가 피처 선언만으로 manifest를 덮고 build_day가 현재 날짜 coverage만 추가합니다. 두 날짜 연속 빌드 시 앞 날짜 parquet은 남지만 coverage는 삭제됩니다. 기존 테스트는 한 날짜만 확인합니다. 현재 MWFD Fast feature cache와는 별개입니다.",
    "action": "기존 날짜별 coverage를 보존하고 재빌드한 날짜만 갱신합니다. 두 날짜 빌드와 앞 날짜 재빌드 보존성을 검증합니다.",
    "evidence": [
      [
        "C:/Projects/TotalStock/Stock/features/store.py",
        115,
        "manifest 갱신 호출"
      ],
      [
        "C:/Projects/TotalStock/Stock/features/store.py",
        149,
        "전체 덮어쓰기"
      ],
      [
        "C:/Projects/TotalStock/Stock/scripts/build_features.py",
        232,
        "날짜별 호출"
      ]
    ]
  },
  {
    "priority": "P2",
    "stage": "레거시 경로 사용 전",
    "title": "strict 품질 실패 전에 피처 결과가 이미 게시됨",
    "effect": "build_day는 parquet과 manifest를 저장한 다음 coverage 기준을 검사합니다. strict 실패로 끝나도 미달 산출물이 정상 저장 위치에 남고, 같은 날짜 파일이 있었다면 이미 교체됩니다.",
    "action": "품질 검사 후 검증된 임시 출력만 게시하고 실패 시 기존 결과를 보존합니다.",
    "evidence": [
      [
        "C:/Projects/TotalStock/Stock/scripts/build_features.py",
        230,
        "저장·검사 순서"
      ],
      [
        "C:/Projects/TotalStock/Stock/features/store.py",
        109,
        "날짜 파일 쓰기"
      ]
    ]
  },
  {
    "priority": "P2",
    "stage": "레거시 경로 사용 전",
    "title": "KIS 데몬이 후처리 실패를 전체 완료 표시에서 무시",
    "effect": "LOB 변환은 failures를 반환하고 일봉 수집은 데이터 0건이면 반환할 수 있지만, 데몬은 이를 확인하지 않고 전체 수집·전처리 완료를 출력합니다. 키움 raw-v2나 현재 MWFD를 관리하는 경로가 아닌 별도 레거시 진입점입니다.",
    "action": "각 후처리 성공·부분완료·실패를 결과로 받아 집계하고 미완료가 있으면 전체 완료를 표시하지 않습니다.",
    "evidence": [
      [
        "C:/Projects/TotalStock/Stock/collector/run_daily_daemon.py",
        223,
        "후처리 호출"
      ],
      [
        "C:/Projects/TotalStock/Stock/collector/build_lob_db.py",
        407,
        "실패 반환"
      ],
      [
        "C:/Projects/TotalStock/Stock/collector/daily_collector.py",
        274,
        "일봉 0건 반환"
      ]
    ]
  }
];
findings.push(...projectFindings);

function Evidence({ items }: { items: Finding["evidence"] }) {
  const dispatch = useCanvasAction();
  return <Row wrap gap={6}>{items.map(([path, line, label]) =>
    <Button key={path + line} variant="ghost" onClick={() => dispatch({ type: "openFile", path, selection: { startLineNumber: line, startColumn: 1, endLineNumber: line, endColumn: 1 } })}>{label} · {line}행</Button>
  )}</Row>;
}
export default function Audit() {
  const theme = useHostTheme();
  const [scope, setScope] = useCanvasState("mwfd04-audit-scope", "전체");
  const filtered = scope === "전체" ? findings : findings.filter(f => f.stage === scope);
  return <Stack gap={20} style={{ padding: 24, maxWidth: 1120, margin: "0 auto", color: theme.text.primary }}>
    <Stack gap={5}>
      <Text size="small" tone="secondary">TotalStock · 읽기 전용 감사 · 2026-09-26 KST</Text>
      <H1>TotalStock 프로젝트 파이프라인·설계 검수</H1>
      <Text>주요 계층의 보호 장치는 연결돼 있습니다. 감시·실패/완료 상태·결과 조회·재현성 기록과 일부 레거시 저장 경계는 보완이 필요합니다.</Text>
      <Text size="small" tone="secondary">상태 스냅샷: 10:41:39 · 자동 갱신 없음 · 코드·환경·실행 설정을 변경하지 않음</Text>
    </Stack>
    <Grid columns="repeat(auto-fit, minmax(230px, 1fr))" gap={18}>
      <Stack gap={4}><H2>276 / 1,286셀</H2><Text>진행 기록 RUNNING · 182,988 / 852,618 candidate-cell</Text><Text size="small" tone="secondary">236셀(10:27) → 258셀(10:33) → 276셀(10:41:39)</Text></Stack>
      <Stack gap={4}><H2>미해결 실패 1셀</H2><Text>0161M0 · feature 단계의 호스트 호출 제한</Text><Text size="small" tone="secondary">진행 중이라는 사실은 전체 정상 완료를 보장하지 않음</Text></Stack>
    </Grid>
    <Divider />
    <Stack gap={10}>
      <H2>전체 프로젝트 검토 범위</H2>
      <Text>운영 main 1811fba와 연구 작업 폴더 6436255를 기준으로 주요 진입점·계층 경계·대표 실패 경로를 문서 및 기존 테스트와 대조했습니다. 전 파일 전수 실행이나 실운영 인증은 아닙니다.</Text>
      <Table headers={["영역", "대조한 연결", "판정과 남은 경계"]} rows={[
        ["수집·저장", "Qt/OCX → bounded queue → 정규화 → raw-v2 → drain/closed", "소유 스레드·저장 I/O 분리와 실패 보존 연결. 실피드·부하 미검증."],
        ["수집 감시", "시장 프로필·체결/호가 침묵 → 회복·종료 보고·큐 적체", "프로필 종료/회복과 큐 경보 조건에 결함."],
        ["원본 확정·품질", "snapshot → whole-file / strict-prefix / selected-v2 qualification", "구조·품질·선택 범위와 checksum·seq·cutoff 재검증 연결."],
        ["정밀 연구·체결·회계", "receive-order replay → 타이머/호가/매칭 → fill ledger → accounting", "주요 경계 연결. main 설정 검증과 조회 교차검사는 보완."],
        ["Fast 연구", "캐시 → 인과 피처 → feasibility → sweep → exact bridge", "screening 범위 유지. 최종 승인·후처리 복구·exact acceptance 보완."],
        ["운영 제어·예약", "UI → service → job/lease/managed capture → worker/scheduler", "중복 claim·owner·OS identity 보호 연결. 오래된 작업 접근과 초기화 실패 전달 보완."],
        ["레거시 분석", "KIS raw-v1 → LOB → fs_v1 → 기존 엔진 → runs/화면", "보존 경로. coverage·strict 게시 순서·후처리 완료 표시 결함."],
        ["일별 metadata", "관측 snapshot → 파생 CSV/가격 출처 제한 → PIT universe", "과거 관측 없는 값·unknown float를 임의 보충하지 않음. MWFD에는 daily metadata 미결합."],
        ["환경·검증·문서", "경로/작업 폴더/venv → pytest marker → CI trigger → 인계", "경로 연결 정상. TESTING은 push/PR 자동 CI라고 쓰지만 실제는 주간/수동."],
        ["향후 제품 통합", "전략 registry · 전략별 account · Paper/Live broker · 연구 UI", "공통 전략/브로커 포트 일부는 scaffold. 현재 tick/portfolio 구현과 구분."]
      ]} />
      <Text>runs/, research_runs/, _data/MWFD는 서로 다른 결과 계약입니다. 현재 UI 조회기는 checkout 내부 tick_research_result_v1만 지원하므로 portfolio·MWFD 결과까지 통합된 화면은 아닙니다.</Text>
      <Text size="small" tone="secondary">이번에 pytest·앱·수집기를 실행하지 않았습니다. Windows OCX 실측, 장시간 부하, 전원 장애 복구, 원본 전수 검증, 다일자 성과 검증은 미실시입니다.</Text>
      <Evidence items={[[root + "/Stock/docs/PIPELINE_MAP.md", 10, "기본 구조"], [root + "/Stock/control_tower/service.py", 29, "UI 결과 경계"], [root + "/Stock/docs/TESTING.md", 5, "테스트 문서"], [root + "/Stock/.github/workflows/ci.yml", 3, "실제 CI"], [root + "/Stock/execution/broker.py", 103, "브로커 구현 경계"]]} />
    </Stack>
    <Stack gap={10}>
      <H2>MWFD-04 세부 흐름</H2>
      <Table headers={["단계", "이번 점검의 판단", "다음 조건"]} rows={[
        ["수집 → qualification → 고정 원본", "기존 evidence 사용. 원본 크기·수정 시각 동일, WAL/SHM 없음.", "수집·OCX·원본 재처리 없음"],
        ["MWFD-02 depth → MWFD-03 표본", "기존 캐시 identity를 MWFD-04가 참조", "원본·대형 캐시 해시 재계산은 하지 않음"],
        ["MWFD-04 event cache", "기록상 1,286셀 / 6,373,396 events. 표본 45셀 event digest 일치.", "이번 감사가 캐시 전체 내용을 재인증한 것은 아님"],
        ["Fast sweep · 현재", "한 날짜의 다종목 screening. 셀별 체크포인트 진행.", "실패 셀 해결 + 전 셀 완료 증거"],
        ["결합 → crosscheck → resumecheck → 요약", "완료 판정·중단 후 재개 코드에 결함 확인", "필수 검증 PASS 뒤 최종 완료 게시"],
        ["PIT universe · exact · 다일자/holdout", "이번 MWFD-04의 완료 범위 밖", "다음 연구로 별도 검증; 자동 착수하지 않음"]
      ]} />
      <Text size="small" tone="secondary">이번 데이터: 2026-09-21 고정 prefix · venue unknown · daily metadata NOT_READY/미결합 · 수익 확정·KRX/NXT 적격성·실전 준비 완료를 의미하지 않음</Text>
    </Stack>
    <Stack gap={10}>
      <H2>발견 사항과 조치 순서</H2>
      <Row wrap gap={6}>{["전체", "완료 판정 전", "다음 운영 전", "레거시 경로 사용 전", "실행 종료 후", "다음 연구 단계"].map(s =>
        <Button key={s} variant={scope === s ? "primary" : "secondary"} onClick={() => setScope(s)}>{s}</Button>
      )}</Row>
      {filtered.map(f => <details key={f.title} open={f.priority === "P1"} style={{ borderBottom: "1px solid " + theme.stroke.tertiary, padding: "12px 0" }}>
        <summary style={{ cursor: "pointer", fontWeight: 500 }}>{f.priority} · {f.title} — {f.stage}</summary>
        <Stack gap={8} style={{ paddingTop: 12 }}>
          <Text>{f.effect}</Text>
          <Text weight="medium">조치: {f.action}</Text>
          <Evidence items={f.evidence} />
        </Stack>
      </details>)}
    </Stack>
    <Stack gap={10}>
      <H2>확인한 기반과 검증 한계</H2>
      <Table headers={["대상", "직접 관측 또는 정적 대조"]} rows={[
        ["Git 연결", "main + linked worktree 11개가 TotalStock 내부. MWFD-04 연결 정상."],
        ["핵심 소스", "manifest에 등록된 15개 파일 SHA-256 전부 일치. HEAD 6436255도 manifest와 일치."],
        ["기존 경로", "추적 실행 소스에서 옛 root 참조 미검출. C:/StockSnapshots 호환 junction 존재."],
        ["Windows 가상환경", "cfg·activation·대표 launcher는 새 경로. 기존 복구 보고도 확인. 이번 실행 테스트는 없음."],
        ["체크아웃 역할", "Stock은 운영용, mwfd-04-full-run은 연구용. 운영 main에는 Fast/MWFD 모듈 없음."],
        ["Git 변경", "main clean. 연구 쪽 미추적 finalizer/host helper 2개는 감사 시작·종료 동일."],
        ["원격 상태", "master a8b9cac. 열린 PR #20/#22/#23/#25/#26/#27/#28. 최신 표시 CI 성공은 b7ac3a2 대상이므로 현재 연구 코드를 인증하지 않음."],
        ["이번에 하지 않은 검증", "pytest·실제 재생·benchmark·DB 읽기·캐시 전수 해시·OCX/login·Git 동기화·push/merge·프로세스 제어 없음."]
      ]} />
      <Text size="small" tone="secondary">원격 조회: 10:29 KST. 현재 실행 host는 manifest의 Linux VM 기록으로 확인했으며 Windows Python PID로 실행 전체를 판정하지 않았습니다. 디스크 여유는 약 974 GiB였습니다.</Text>
      <Evidence items={[[run + "/progress.json", 1, "진행 상태 원본"], [run + "/event_materialization.json", 1, "캐시 생성 기록"], [root + "/_data/venv_rebuild_01/old-path-audit-after.json", 1, "기존 환경 복구 근거"]]} />
    </Stack>
    <Stack gap={8} style={{ background: theme.fill.tertiary, padding: 16 }}>
      <H2>권장 순서</H2>
      <Text>현재 계산·원본·체크포인트 보존 → MWFD 실패 셀과 최종 승인/후처리 해결 → 다음 수집 전 감시·초기화 실패·작업 접근 보완 → 공통 결과 검증과 portfolio/MWFD 조회 계약 정리 → 레거시 저장·완료 표시 보강 → main/연구 차이 및 HANDOFF·TODO·경로·CI 문서 동기화</Text>
      <Text size="small" tone="secondary">지금 추적 소스를 수정하면 다음 분할 호출이 CODE_IDENTITY_DRIFT로 중단될 수 있습니다. 이 감사는 수정안 적용·실행 재시작·MWFD-05 착수가 아닙니다.</Text>
    </Stack>
  </Stack>;
}


