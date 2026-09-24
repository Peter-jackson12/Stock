"""합성 다종목 스트림의 순수 주문 의도 경계와 메모리 내 연구 결과.

전략에는 현재 TickView와 불변 PortfolioSnapshot만 전달한다. 전략은 정렬된
list/tuple의 OrderIntent/CancelIntent를 반환한다. 순서는 전략 입력 계약이며
set/dict/generator의 암묵적 순서는 받지 않는다. Python 보안 격리 기능은 아니다.

기존 NxtResearchStrategy/run_research/실제 raw CLI에 자동 연결하지 않는다.
결과는 메모리 내 JSON-native dict다. 디스크 저장·실데이터 적격성·평가 PnL은
별도 단계이며, caller의 strategy_id는 코드 출처 인증이 아닌 식별용 주장이다.
"""
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from execution.portfolio_simulator import CancelIntent, OrderIntent, PortfolioSimulator


def _json(value):
    def encode(item):
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError(f"unsupported result type: {type(item).__name__}")
    return json.dumps(value, default=encode, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _apply_strategy(simulator, view, strategy, records):
    intents = strategy(view, simulator.snapshot())
    if type(intents) not in (list, tuple) or any(type(i) not in (OrderIntent, CancelIntent) for i in intents):
        raise ValueError("strategy must return an ordered list/tuple of order/cancel intents")
    for intent in intents:
        record = dict(event_seq=view.event.seq, time_ns=view.event.received_ns,
                      kind="order" if type(intent) is OrderIntent else "cancel", intent=asdict(intent))
        # Validate report encoding before execution. A malformed payload must
        # not make the later failure report throw and hide earlier valid fills.
        try:
            _json(record)
        except (TypeError, ValueError) as exc:
            records.append(dict(event_seq=view.event.seq, time_ns=view.event.received_ns,
                                kind="invalid_intent", intent_type=type(intent).__name__,
                                field_types={name: type(value).__name__ for name, value in record["intent"].items()},
                                serialization_error=type(exc).__name__))
            raise
        records.append(record)
        if type(intent) is OrderIntent:
            simulator.submit(intent)
        else:
            record["accepted"] = simulator.cancel(intent.order_id)


def replay_portfolio_chunk(simulator, events, strategy):
    """같은 simulator/strategy를 유지한다. chunk 경계에서 시계를 진행하거나 닫지 않는다."""
    records = []
    for event in events:
        view = simulator.on_event(event)
        _apply_strategy(simulator, view, strategy, records)
    return records


class PortfolioRunFailed(RuntimeError):
    def __init__(self, report, cause):
        super().__init__(f"portfolio replay failed: {cause}")
        self.report = report


def _code_identity():
    root = Path(__file__).resolve().parents[1]
    names = ("engine/portfolio_session.py", "execution/portfolio_simulator.py",
             "engine/tick_ordering.py", "execution/tick_simulator.py", "execution/quote_validation.py")
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}


def run_portfolio(events, *, simulator_config, close_ns, strategy, strategy_id):
    """합성/normalized 입력 전용. 실패는 diagnostics_only 부분 결과를 보존한 예외다."""
    if type(close_ns) is not int or close_ns <= 0:
        raise ValueError("positive exclusive close required")
    if not isinstance(strategy_id, str) or not strategy_id.strip() or not callable(strategy):
        raise ValueError("callable strategy and explicit strategy_id required")
    simulator = PortfolioSimulator(**simulator_config)
    settings = simulator.settings() | dict(close_ns=close_ns, strategy_id=strategy_id)
    _json(settings)
    identity = _code_identity()
    digest = hashlib.sha256()
    count, processed, records, error = 0, 0, [], None
    try:
        for event in events:
            digest.update((_json(asdict(event)) + "\n").encode("utf-8"))
            count += 1
            if type(event.received_ns) is not int or event.received_ns >= close_ns:
                raise ValueError("event at or after exclusive close or invalid receipt time")
            view = simulator.on_event(event)
            processed += 1
            _apply_strategy(simulator, view, strategy, records)
        simulator.close(close_ns)
    except Exception as exc:
        error = exc
    snapshot = asdict(simulator.snapshot())
    status = ("failed" if error else "completed_empty_input" if count == 0 else
              "completed_with_open_position" if any(n for _, n in snapshot["positions"]) else
              "completed_no_fills" if not snapshot["fills"] else "completed_flat")
    key = dict(events=digest.hexdigest(), settings=settings, code=identity, intents=records)
    report = dict(schema="portfolio_research_result_v1", status=status,
                  diagnostics_only=error is not None, input_complete=error is None,
                  event_count=count, processed_event_count=processed,
                  event_sha256=digest.hexdigest(), settings=settings, code_sha256=identity,
                  reproducibility_key=hashlib.sha256(_json(key).encode("utf-8")).hexdigest(),
                  error=f"{type(error).__name__}: {error}" if error else None,
                  order_intents=records, account=snapshot,
                  order_transitions=[asdict(t) for t in simulator.transitions],
                  realized_pnl=None, unrealized_pnl=None, equity=None,
                  raw_identity_verified=False,
                  limitations=["normalized_in_memory_only_no_raw_reader_or_eligibility_certification",
                               "single_source_session_one_venue_per_symbol_long_only",
                               "top_of_book_refresh_liquidity_assumption",
                               "full_remainder_reservation_recheck_not_exchange_guarantee",
                               "strategy_id_caller_claim_not_strategy_code_hash",
                               "public_call_schedule_part_of_input",
                               "no_pnl_valuation_no_forced_liquidation",
                               "no_live_broker_no_nxt_multi_asset_adapter"])
    report = json.loads(_json(report))
    if error:
        raise PortfolioRunFailed(report, error) from error
    return report
