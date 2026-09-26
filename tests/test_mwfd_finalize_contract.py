import hashlib
import json
from types import SimpleNamespace

from scripts import finalize_mwfd_04 as finalizer


class DummyEnv:
    def __init__(self, run_root):
        self.run_root = run_root


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def entry(path):
    data = path.read_bytes()
    return {"path": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def crosscheck(status="PASS"):
    return {
        "mwfd03_45_cell_economic_digest": {"status": status},
        "unchunked_recompute": {"status": status},
    }


def test_existing_failed_crosscheck_returns_nonzero(tmp_path):
    write_json(tmp_path / "crosscheck.json", crosscheck("FAIL"))
    assert finalizer.command_crosscheck(SimpleNamespace(), DummyEnv(tmp_path)) == 3


def test_existing_failed_resumecheck_returns_nonzero(tmp_path):
    write_json(tmp_path / "resume_validation.json", {"status": "FAIL"})
    assert finalizer.command_resumecheck(SimpleNamespace(), DummyEnv(tmp_path)) == 3


def build_complete_gate_fixture(tmp_path):
    combined = {}
    for name in finalizer.OUTPUTS:
        path = tmp_path / name
        path.write_text("{}\n", encoding="utf-8")
        combined[name] = entry(path)

    write_json(tmp_path / "combine.json", {
        "schema": "mwfd_04_combine_v1",
        "status": "COMPLETED",
        "outputs": combined,
    })
    write_json(tmp_path / "crosscheck.json", crosscheck())
    write_json(tmp_path / "resume_validation.json", {"status": "PASS"})

    (tmp_path / "candidate_summary.jsonl").write_text("{}\n", encoding="utf-8")
    write_json(tmp_path / "full_run_summary.json", {
        "coverage": {
            "cells_completed": finalizer.EXPECTED_CELLS,
            "candidates": finalizer.EXPECTED_CANDIDATES,
            "candidate_cell_evaluations": finalizer.EXPECTED_CELLS * finalizer.EXPECTED_CANDIDATES,
            "duplicate_count": 0,
            "failure_count": 0,
        }
    })
    write_json(tmp_path / "gate_summary.json", {
        "reconciled": True,
        "mwfd02_inventory_match": True,
    })
    write_json(tmp_path / "runtime_summary.json", {})
    write_json(tmp_path / "factor_dataset_manifest.json", {})

    marker_outputs = {}
    for name in finalizer.SUMMARY_OUTPUTS:
        marker_outputs[name] = entry(tmp_path / name)
    write_json(tmp_path / "summarize.json", {
        "schema": "mwfd_04_summarize_completion_v1",
        "status": "COMPLETED",
        "outputs": {
            name: {"bytes": value["bytes"], "sha256": value["sha256"]}
            for name, value in marker_outputs.items()
        },
    })

    return {
        path.name: entry(path)
        for path in tmp_path.iterdir()
        if path.is_file()
    }


def test_completion_gate_accepts_only_reconciled_complete_outputs(tmp_path):
    entries = build_complete_gate_fixture(tmp_path)
    assert finalizer._completion_gate_errors(tmp_path, entries) == []


def test_completion_gate_rejects_validation_failure(tmp_path):
    entries = build_complete_gate_fixture(tmp_path)
    write_json(tmp_path / "crosscheck.json", crosscheck("FAIL"))
    entries["crosscheck.json"] = entry(tmp_path / "crosscheck.json")
    assert "crosscheck:FAIL" in finalizer._completion_gate_errors(tmp_path, entries)


def test_completion_gate_rejects_unresolved_failure(tmp_path):
    entries = build_complete_gate_fixture(tmp_path)
    failures = tmp_path / "failures"
    failures.mkdir()
    write_json(failures / "0252.json", {"status": "FAILED"})
    errors = finalizer._completion_gate_errors(tmp_path, entries)
    assert "unresolved_failures:1" in errors


def test_summary_marker_detects_partial_or_changed_output(tmp_path):
    build_complete_gate_fixture(tmp_path)
    marker = json.loads((tmp_path / "summarize.json").read_text(encoding="utf-8"))
    assert finalizer._summary_marker_valid(tmp_path, marker)
    (tmp_path / "runtime_summary.json").write_text('{"changed": true}', encoding="utf-8")
    assert not finalizer._summary_marker_valid(tmp_path, marker)


def test_combine_recovers_after_partial_final_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(finalizer, "EXPECTED_CELLS", 1)
    monkeypatch.setattr(finalizer, "EXPECTED_CANDIDATES", 1)

    cell = SimpleNamespace(index=1, code="A", venue="unknown", cell_id="A=unknown")
    checkpoint = tmp_path / "checkpoints" / "0001-A-unknown"
    checkpoint.mkdir(parents=True)
    write_json(checkpoint / "completion.json", {"status": "COMPLETED"})

    # One output was already renamed to its final name before the process died.
    (tmp_path / finalizer.OUTPUTS[0]).write_text("{}\n", encoding="utf-8")

    # The other writers reached the common committed cell boundary but were not renamed yet.
    for name in finalizer.OUTPUTS[1:]:
        writer = finalizer.ResumableConcatWriter(tmp_path / name)
        writer.open()
        writer.write_lines([b"{}\n"])
        writer.commit_cell(1)
        writer.close()

    (tmp_path / ".combine_ledger.jsonl").write_text(
        json.dumps({
            "cell_index": 1,
            "cell_id": "A=unknown",
            "candidate_results_digest": "economic",
            "file_digest": "serialized",
        }) + "\n",
        encoding="utf-8",
    )

    env = SimpleNamespace(
        run_root=tmp_path,
        population=SimpleNamespace(cells=(cell,)),
        checkpoints=tmp_path / "checkpoints",
    )
    result = finalizer.command_combine(SimpleNamespace(budget_seconds=1), env)

    assert result == 0
    assert (tmp_path / "combine.json").is_file()
    assert (tmp_path / "combine_ledger.jsonl").is_file()
    assert not (tmp_path / ".combine_ledger.jsonl").exists()
    assert all((tmp_path / name).is_file() for name in finalizer.OUTPUTS)
    combined = json.loads((tmp_path / "combine.json").read_text(encoding="utf-8"))
    assert combined["status"] == "COMPLETED"
    assert combined["checks"]["publication_recovered"] is True
