"""Persist isolated NXT research runs over normalized in-memory events.

No raw database reader or production collector is invoked. The result schema is
separate from legacy round-trip Trade metrics: open positions are not realized PnL.
"""
from copy import deepcopy
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from execution.tick_simulator import TickSimulator
from execution.quote_validation import check_ordered_quote
from execution.reality_contract import input_capabilities, simulation_contract
from strategies.nxt_breakout.tick_research import NxtResearchStrategy


class ResearchRunFailed(RuntimeError):
    def __init__(self, path, cause):
        super().__init__(f"Research replay failed; diagnostics: {path}: {cause}")
        self.path = path


def _default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported result type: {type(value).__name__}")


def _json(value):
    return json.dumps(value, default=_default, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _provenance_event_count(provenance):
    """예약 키만 검증한다. 일반 JSON metadata/reader 설명은 해석하지 않는다."""
    if not isinstance(provenance, dict) or "raw_manifest" not in provenance:
        return 0
    manifest = provenance["raw_manifest"]
    if not isinstance(manifest, dict):
        raise ValueError("input_provenance.raw_manifest must be a dictionary")
    count = manifest.get("event_count", 0)
    if type(count) is not int or count < 0:
        raise ValueError("input_provenance.raw_manifest.event_count must be a nonnegative integer")
    return count


def _write(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _code_identity():
    root = Path(__file__).resolve().parents[1]
    names = ("engine/tick_research_run.py", "engine/tick_ordering.py",
             "execution/tick_simulator.py", "execution/quote_validation.py",
             "execution/reality_contract.py",
             "strategies/nxt_breakout/tick_research.py", "engine/nxt_tick_engine.py")
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}


def run_research(events, *, output_root, dataset_label, simulator_config,
                 close_ns, quantity, exit_rule="fixed", cooldown_ns=10_000_000_000,
                 params=None, input_provenance=None):
    """Save one variant/account per run; raise on replay failure after diagnostics.

    dataset_label is a caller label, not proof of raw file identity/finalization.
    event_sha256 covers the consumed normalized stream; complete only on success.
    Output locations are fresh UUID directories: reruns never overwrite results.
    Config/provenance validation and JSON serialization precede output creation.
    Generic JSON provenance remains opaque. A top-level dictionary's reserved
    raw_manifest must be a dictionary; its optional event_count must be an actual
    nonnegative int (not bool). Missing manifest/count means zero for classification.
    reader/reader_sha256 are retained metadata, not independently verified evidence.
    Once iteration starts, replay failures retain failed/diagnostics_only output;
    this is not a guarantee against filesystem failure or process termination.
    Memory usage scales with orders/fills; large real-data runs require later work.
    """
    if not isinstance(dataset_label, str) or not dataset_label.strip():
        raise ValueError("dataset label required")
    if type(close_ns) is not int or close_ns <= 0:
        raise ValueError("positive closing timestamp required")
    config = deepcopy(simulator_config)
    if "fee_rate" not in config:
        raise ValueError("explicit fee_rate required; no silent zero-cost run")
    sim = TickSimulator(**config)
    strategy = NxtResearchStrategy(quantity=quantity, exit_rule=exit_rule,
                                   cooldown_ns=cooldown_ns, params=params)
    settings = dict(simulator=config, quantity=quantity, exit_rule=exit_rule,
                    cooldown_ns=cooldown_ns, close_ns=close_ns, params=strategy.params)
    _json(settings)
    provenance = deepcopy(input_provenance)
    _json(provenance)
    raw_event_count = _provenance_event_count(provenance)
    contracts = dict(simulation_contract=simulation_contract(sim),
                     input_capabilities=input_capabilities(sim))
    contract_hash = hashlib.sha256(_json(contracts).encode("utf-8")).hexdigest()
    code_identity = _code_identity()
    run_dir = Path(output_root).resolve() / uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=False)
    path = run_dir / "result.json"
    base = dict(schema="tick_research_result_v1", dataset_label=dataset_label,
                raw_identity_verified=False, input_provenance=provenance,
                settings=settings, code_sha256=code_identity,
                **contracts, contract_sha256=contract_hash,
                started_at=datetime.now(timezone.utc).isoformat(),
                limitations=["normalized_events_only", "single_instrument_long_only",
                             "top_of_book_refresh_liquidity_assumption", "not_live_execution",
                             "no_round_trip_pnl_or_equity_valuation"])
    _write(path, base | dict(status="running"))
    digest = hashlib.sha256()
    count = 0
    processed_counts, quote_checks = Counter(), Counter()
    error = None
    try:
        for event in events:
            encoded = _json(asdict(event)).encode("utf-8") + b"\n"
            digest.update(encoded)
            count += 1
            if event.received_ns >= close_ns:
                raise ValueError("event at or after exclusive session close")
            view = sim.on_event(event)
            processed_counts[event.kind] += 1
            quote_checks[check_ordered_quote(view).reason] += 1
            strategy(view, sim)
        sim.close(close_ns)
    except Exception as exc:
        error = exc
    no_selection = count == 0 and raw_event_count > 0
    status = ("failed" if error is not None else "completed_no_selected_events" if no_selection
              else "completed_empty_input" if count == 0
              else "completed_with_open_position" if sim.position else
              "completed_no_fills" if not sim.fills else "completed_flat")
    event_hash = digest.hexdigest()
    reproducibility_key = hashlib.sha256(_json(dict(events=event_hash, settings=settings,
                                                     code=code_identity, provenance=provenance,
                                                     contracts=contract_hash)).encode("utf-8")).hexdigest()
    report = base | dict(status=status, finished_at=datetime.now(timezone.utc).isoformat(),
                         event_count=count, event_sha256=event_hash,
                         input_complete=error is None, reproducibility_key=reproducibility_key,
                         error=(f"{type(error).__name__}: {error}" if error else None),
                         diagnostics_only=error is not None,
                         processed_event_counts=dict(processed_counts), quote_checks=dict(quote_checks),
                         final_cash=sim.cash, open_quantity=sim.position,
                         orders=[asdict(order) for order in sim.orders.values()],
                         fills=[asdict(fill) for fill in sim.fills],
                         signals=strategy.signals, order_audit=sim.audit)
    _write(path, report)
    if error is not None:
        raise ResearchRunFailed(path, error) from error
    return path


def run_raw_v2(path, *, output_root, simulator_config, quantity, **strategy_options):
    """Offline closed raw-v2 -> full validation -> selected instrument -> result.

    All rows, including unselected symbols, are read to verify the stored checksum.
    Do not invoke on a large production file during capture hours.
    """
    from collector.raw_v2 import read_raw_v2, CaptureControl
    with read_raw_v2(path) as (manifest, envelopes):
        config = deepcopy(simulator_config)
        if config["source"] != manifest["source"] or config["session_id"] != manifest["session_id"]:
            raise ValueError("simulator source/session must match dataset")
        def selected():
            for envelope in envelopes:
                event = envelope["event"]
                if isinstance(event, CaptureControl):
                    if event.control_type not in ("session_start", "session_note"):
                        raise ValueError(f"dataset quality event {event.control_type} at seq={event.seq}: {event.details}")
                    continue
                if (event.code, event.venue) == (config["code"], config["venue"]):
                    yield event
        return run_research(selected(), output_root=output_root, dataset_label=str(Path(path).resolve()),
                            simulator_config=config, quantity=quantity, close_ns=manifest["close_ns"],
                            input_provenance=dict(raw_manifest=manifest, reader="raw_v2_reader_2",
                              reader_sha256=hashlib.sha256(Path(__file__).resolve().parents[1].joinpath(
                                  "collector/raw_v2.py").read_bytes()).hexdigest()), **strategy_options)
