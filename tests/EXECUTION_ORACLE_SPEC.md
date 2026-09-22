# Execution oracle 감사 명세와 반례

기준: `98725431ca84f6fc88efb293e126b915799f0a28` (PR #10 병합).
[실행 계약](../TICK_RESEARCH_RUNBOOK.md#simulation-reality-contract-v1) ·
[시뮬레이터](../execution/tick_simulator.py) · [현재 인계](../HANDOFF.md).
이 파일은 **테스트 명세·감사 한계·반례**를 담당한다. 기계적 실행 설명은
`execution/reality_contract.py`, 연구 사용 안내는 기존 런북이 담당한다.

## 1. 구현 관찰과 독립적인 기대를 분리

### 기존 문서에서 얻은 기대

`ARCHITECTURE_TICK.md` §11 C, 런북의 실행 모델/순서, PR #10 계약을 출발점으로 삼는다.
시장가 buy는 유효한 ask, sell은 bid로 거래한다. 양쪽 유한 양수 가격/정수 잔량,
양수 spread, 호가 나이 이하 조건이 모두 필요하다. 체결가 fallback은 없다.
주문 ready 이전, 이미 효력 있는 취소 뒤, exclusive close 이상에서는 체결할 수 없다.
같은 시각의 타이머는 이전 호가를 보고, 외부 이벤트는 수신 seq순이며 전략은 그 뒤에 호출된다.
지연 0 주문도 제출 전에는 존재하지 않는다. 호가 budget은 새 quote seq에서만 갱신된다.
잔여량은 유지하고 마감에 만료한다. 공매도/차입/허위 청산을 만들지 않는다.

독립 ledger의 수량 n은 `0 <= n <= remaining`, `n <= 해당 방향 budget`,
buy이면 `n * ask * (1 + fee) <= cash`, sell이면 `n <= position`을 만족하는 최대 정수다.
매수 현금 변화는 `-n*ask-fee`, 매도는 `n*bid-fee`다. fee는 각 fill 금액에 적용한다.
이 부등식은 production의 `Decimal //` 계산과 독립적이다.

### production을 읽어 관찰한 구현

`_match`는 취소를 먼저 처리한 뒤 dict 접수순으로 **한 번** 순회한다. 같은 호출에서
뒤 주문의 체결이 앞 주문에 자원을 공급해도 앞 주문을 재방문하지 않는다.
`advance`는 호출 시작의 active 주문 deadline 집합을 수집한다. deadline별 매칭 외에
endpoint에서도 매칭하므로 마지막 deadline과 endpoint가 같아도 별도 라운드가 된다.
호출 도중 terminal이 된 주문의 이미 수집된 deadline도 그 호출에는 남는다.
`on_event`는 입력 순서를 검증하고 advance한 다음 새 호가를 반영하고 다시 매칭한다.
정상 submit 및 실제 접수된 cancel 요청도 매칭을 부른다. 중복/terminal cancel은 아니다.

### 모호한 resource_policy의 호환성 해석

기존 문자열은 자원이 생긴 뒤 재시도할 수 있다고만 설명했다. **즉시 고정점까지 재매칭**하는
정책과 **다음 매칭 라운드에서만 재시도**하는 정책을 구별하지 못했다. 이 감사의 기본 oracle는
기존 동작을 바꾸지 않는 두 번째 해석을 명시한다. 이는 문서에서 유일하게 도출된 시장 정답이
아니며, 매칭 횟수까지 고정한 공개 호출 계약의 보완이다.

따라서 oracle 일치는 이 명시적 해석과 유한 수치 범위에서의 일치다. 원래 문구의 충분성이나
production의 무제한 상태 공간을 증명하지 않는다. 추가 advance 삽입/삭제는 불변 변형이 아니다.

## 2. 독립성 및 범위

[oracle](execution_oracle.py)는 dataclasses/fractions만 import한다.
production helper, 계약 문자열, validator, replay clock 또는 production 출력을 정답으로 쓰지 않는다.
불변 State/Order의 ledger 전이, feasible 수량의 직접 열거, 작은 정수 시간 격자로 표현한다.
손계산 buy/sell 수수료 및 자금 부족 사례로 oracle 자체를 확인하고 import 독립성도 검사한다.
[비교 경계](execution_audit_support.py)만 양쪽 구현을 호출한다.

기본 수치 범위는 작은 정수 가격/수량, 유한 소수 fee/cash와 충분한 기본 Decimal 정밀도다.
Fraction은 arbitrary Decimal context의 반올림을 복제하지 않는다. 낮은 정밀도는 별도의
자원 보존 반례로 다룬다. 시간 구간 최대 32, 주문 수량 최대 8의 테스트 제한이 있다.
실제 전략은 oracle에 이식하지 않는다. 합성 개장/손절 사례의 주문 의도만 손으로 지정해
실제 세 전략 변형과 저장 실행기를 대조한다. 전략 전체의 독립 검증이라는 주장은 하지 않는다.

## 3. 유한 전수 공간

[전수 테스트](test_execution_oracle_audit.py)는 다섯 개의 별도 직교 행렬을 실행한다.
각 public command 직후 fill의 ID/방향/수량/가격/시각/fee/quote seq, cash, position,
주문 총량/잔여량/ready/cancel/status, clock/closed를 비교한다. private budget/audit는 비교하지 않는다.

| 행렬 | 차원 | parameter/program 조합 수 |
|---|---|---:|
| lifecycle | side 2 × quantity 2 × entry delay 3 × cancel delay 3 × cancel 유무 2 × bid size 2 × ask size 2 × close 3 × age 3 | 2,592 |
| resources | side쌍 4 × quantity쌍 4 × buy delay 3 × sell delay 3 × cash 3 × book 2 × 공개 매수 seed 유무 2 | 1,728 |
| quote_clock | 이전/이후 가격쌍 4 × bid size 2 × ask size 2 × event time 3 × buy delay 3 × age 3 × quote/trade 2 | 864 |
| words | 8개 명령 알파벳의 길이 3 문자열 × delay 3 | 1,536 |
| fees | cash 8 × fee 3 × quantity 2 × size 2 × quote 갱신 유무 2 | 192 |
| 합계 | 별도 행렬의 합. 전체 차원의 Cartesian product가 아님 | **6,912** |

조합 수는 pytest item 수나 unique reachable state 수가 아니다. 예를 들어 매도 seed,
체결 불가 또는 빈 주문의 cancel no-op 때문에 경제적으로 동등한 조합이 있을 수 있다.
실제 실행 checkpoint 수와 시간은 `ORACLE_MATRIX` CI 로그에 별도로 출력한다.
추가로 실제 세 전략의 각 16개 chunk 분할(48개)과 저장 실행 3개를 수동 의도 oracle와 비교한다.
새 dependency는 없으며 Actions에서 작은 메모리/임시 합성 DB만 쓴다.

## 4. 불변식과 변형 관계

각 checkpoint에서 oracle와 별개로 fill ledger를 재합산한다. cash/position 비음수,
주문량 보존, quote seq/방향별 budget 상한, buy ask/sell bid, fee 합산,
ready/cancel/close 인과 경계와 stale 조건을 검사한다.
취소 요청 전 확정된 fill이 지연 0 취소와 같은 ns를 가질 수 있으므로, 해당 과거 prefix는 보존한다.
요청 이후 생기는 fill에는 `time < cancel_effective_time`을 엄격하게 적용한다.

[변형 테스트](test_execution_metamorphic.py): 미래 suffix와 완료 prefix 분리,
동일 외부 호출열의 모든 chunk 경계, 접수순을 유지한 order ID 전단사 치환,
고정 주문에서 외부 trade payload/depth 변경, 설명 객체 변경의 비간섭을 검사한다.
새 quote seq만 budget을 갱신하고, 잘못된 새 quote는 이전 유효 호가로 fallback하지 않는다.
latency의 quote 경계 통과와 age 한도 축소는 손계산한 반대 결과를 확인한다.
fee 단조성은 **동일 호가의 단일 buy·추가 자원/갱신 없음**에만 주장한다.
손익, 최종 현금, 적응형 전략 또는 buy/sell 왕복 전체의 단조성은 주장하지 않는다.

[연구 경로](test_execution_research_audit.py): 실제 전략/청크/저장 경로,
unused provenance/raw envelope의 경제적 비간섭과 입력 identity 변화,
Decimal context별 계약 identity, 늦은 iterator 오류의 전체 실패 전파를 검사한다.
`raw_manifest`처럼 상태 분류가 사용하는 예약 metadata는 unused가 아니다.
[계약 보완 회귀](test_execution_contract_audit.py)는 v1 타입 유지, 라운드 설명,
취소 인과성, 한 advance 안에서의 예약 deadline 유지 및 명세 링크도 검사한다.

## 5. 불일치와 최소 반례의 분류

### RES-1 — 모호한 계약 표현 / 기존의 미검증 동작

q=(bid=2, ask=3, 각 size=2), cash=6, 모든 latency=0.
`quote@0 -> submit S sell1 -> submit B buy1`.
즉시 자원 재평가를 기대하면 B 뒤에 S도 체결되어 cash=5/position=0이어야 한다.
실제 1회 순회와 호환 oracle는 B만 체결되어 cash=3/position=1, S.remaining=1이다.
`advance(0)`을 한 번 더 하면 S가 체결된다.

반대 방향도 검증한다: cash=6에서 ask3으로 seed buy2, 새 q=(bid4,ask5),
B buy1(현금 0), S sell2. S가 cash=8을 공급해도 B는 그 submit 호출 안에서 재방문하지 않는다.
다음 `advance(0)` 후에 B가 체결되어 cash=3/position=1이다.

근거: `_match`의 한 번 순회, 런북의 잔여 주문 유지, 기존 cash/no-short 회귀.
기존 테스트는 이 양방향 재공급 및 같은 호출 안의 재방문 여부를 명시하지 않았다.
고정점 정책으로 바꾸면 시각/가격/수량이 달라진다. 이번에는 실행을 바꾸지 않고 구조화 설명만 보완한다.

### CLK-1 — 성립하지 않는 변형 관계 / 정책 경계

cash=3, q=(bid2,ask3,size2), age=1, buy delay=1, sell delay=0.
`quote@0 -> submit S sell1 -> submit B buy1` 다음:

- `advance(2) -> close(3)`: B@1만 체결. S를 재평가할 endpoint 2에서는 stale. cash=0/position=1.
- `advance(1) -> advance(2) -> close(3)`: deadline 1에서 B, 별도 endpoint 1에서 S. cash=2/position=0.

각각의 명시적 호출열에서는 호환 oracle와 일치한다. **advance 분할 불변성은 반례가 있으므로
metamorphic invariant에 넣지 않는다.** 청크 분할은 추가 advance/close 없이 호출열을 보존할 때만 불변이다.
시계 진행 호출을 경제적으로 무관하게 만들려면 별도 정책 결정/behavior-change PR이 필요하다.

### NUM-1 — production 수치/자원 보존 bug, 이번에 미수정

안정된 `Context(prec=3, ROUND_HALF_EVEN)`, cash=1.01, ask=1.01, bid=1,
fee=0.005, size=1, buy latency=0에서 quote 후 buy1을 제출한다.
정확한 비용은 1.01505이므로 독립 oracle는 미체결/cash=1.01/position=0이다.
production은 `1+fee`를 먼저 1.00으로 반올림하여 1주를 허용하고,
실제 차감은 별도 반올림되어 cash=-0.01/position=1, fee=0.00505가 된다.

근거: `_match`의 capacity 산식과 별도 gross/fee 차감, 생성자가 이 context/입력을 거부하지 않음.
Python Decimal의 정밀도/반올림 설명: <https://docs.python.org/3/library/decimal.html#floating-point-notes>.
context를 기록하고 안정적으로 유지하는 것만으로 solvency가 보장되지는 않는다.
이는 oracle rounding 복제 누락으로 정상화할 문제가 아니라 **음수 cash라는 독립 불변식 위반**이다.
경제적 영향이 있어 수정은 별도 PR로 분리한다. 정밀도/입력 규모의 지원 범위와 체결전 차감 검증을
확정해야 하며, precision 28이면 모든 입력에 안전하다고 일반화하지 않는다.
재현 증명 테스트와 의도된 불변식의 `strict xfail`을 모두 둔다. xfail은 해결/통과를 뜻하지 않는다.

### RUN-1 — 연구 결과 종료 기록 bug, 이번에 미수정

빈 normalized iterator와 `input_provenance={"raw_manifest": None}`.
해당 값은 JSON 직렬화가 가능하지만 종료 분류의 `.get()`에서 AttributeError가 발생한다.
production은 `running` result만 남기고 `ResearchRunFailed`/완료 진단을 기록하지 못한다.
기대는 예약 provenance 구조를 출력 생성 전에 ValueError로 거부하는 것이다.
이는 unused metadata 불변성의 반례가 아니라 사용되는 예약 필드의 검증 결함이다.
경제적 체결 결과는 없는 최소 사례다. 정상 run_raw_v2가 만드는 manifest의 결함을 뜻하지 않는다.
입력 검증/진단 보완 후속으로 분리하고 재현 테스트와 별도 `strict xfail`로 추적한다.

### AUD-1 — 감사기 자체 오류, 수정

첫 CI는 `tests` 패키지의 보조 모듈 import 경로 오류로 collection 단계에서 중단됐다.
수정 뒤의 CI에서 다음 두 검사기 오류도 발견했다. production과 oracle 전이는 일치한 상태였다.

- q@0(size1) -> buy2(latency0) -> cancel(latency0)에서 이미 확정된 buy1@0까지
  `fill_time < cancel_time`으로 소급 거부했다. 취소 요청 당시 fill prefix와 요청 이후를 분리해 수정했다.
  기존 callback 취소의 소급 금지 계약이 근거이며 새 quote@0에도 잔여 주문이 체결되지 않는 회귀를 추가했다.
- ID 치환 시험에서 q@0와 q@1의 각 size2를 보고 세 주문 모두의 체결을 기대했다.
  그러나 latency2라면 첫 quote를 소모하기 전에 두 번째 quote가 **대체**하므로 두 주문만 체결된다.
  손계산 기대 수량을 delay2에서 2, delay0/1에서 3으로 바로잡았다. ID 치환 불변식은 유지했다.

oracle의 수량 산식/상태전이, production 체결 행동을 이 실패에 맞춰 변경하지 않았다.
이는 oracle bug 범주에 가까운 **검증 harness/기대 assertion 오류**이며 production 버그로 집계하지 않는다.

## 6. 변경/해석 한계와 다음 검증

TickSimulator/ReceiveOrderReplay/quote validation/전략/연구 실행기의 행동은 바꾸지 않는다.
계약 v1의 기존 필드 타입은 유지하고 `resource_retry_policy`, `same_timestamp_policy.matching_rounds`,
공개 호출열 의존성과 `numeric_safety`를 additive로 보완한다. 음수 cash를 올바른 정책으로 허용하는 수정이 아니다.
`input_capabilities`의 형식 지원과 관측 인증 구분은 유지한다. 응답 지연 미모델링, 부분체결,
유동성 재충전 및 마감 설명도 읽고 대조했으며 새 기능을 주장하지 않는다.
설명/관련 코드 hash가 달라져 contract_sha256 및 reproducibility_key가 바뀌지만
과거 파일을 덮어쓰거나 v2로 자동 재발급하지 않는다. 고정 연구 실행기의 호출열은 해당 코드와 이벤트에서 결정된다.

CI success는 유한 감사 회귀의 통과를 뜻한다. NUM-1/RUN-1의 예상 실패 두 건을 해결했다는 뜻이 아니다.
최종 HEAD/run/job/실측 수치는 PR 본문에 남긴다. 지원 수치 범위의 solvency 검증을 첫 실제 연구 전에
확정해야 한다. 자원 부족 양방향 주문을 쓰는 호출자는 advance 일정의 의미도 먼저 받아들여야 한다.
실제 strategy 전체, 임의 타입, 극대 수량/지수, 실행 중 context 변경, 전 피드/전 날짜의 정확성은 미증명이다.

queue, impact, L2/L3, MBO, response/feed latency는 모델링하지 않는다. top1 새 snapshot을
실제 신규 유동성으로 재인식하는 가정은 특히 큰 한계다. 원천 정확성·실제 체결 가능성·수익성은 미인증이다.
사용자 Windows 저장소, 실제 raw/evidence/DB, OCX, live collector, qualification,
실제 백테스트와 로컬 git 조작은 이번 작업의 입력/실행 범위가 아니다.
