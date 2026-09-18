import gzip
import hashlib
import json
import os
import sqlite3

import pytest

from collector import raw_archive as archive
from collector.kiwoom.collector_lease import CollectorLease
from collector.kiwoom.capture_session import CaptureSession
from collector.raw_v2 import read_raw_v2
from engine.tick_research_run import ResearchRunFailed, run_raw_v2

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows sharing-lock prototype")


def create(path, *, finish=True):
    with CaptureSession(path, source="test", session_id="s", market_date="2026-09-19",
                        feed_scope="fixture", price_policy="signed_magnitude",
                        direction_policy="signed_volume", started_ns=0,
                        started_at_utc="2026-09-19T00:00:00Z") as session:
        session.on_tick(code="005930", venue="unknown", real_type="주식체결",
                        fids={"10": "+10001", "15": " 52244", "20": "153219"},
                        received_ns=1, received_at_utc="2026-09-19T00:00:01Z")
        session.commit()
        if finish:
            session.finish(2)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source" / "raw.db"
    create(path)
    return path


def pack(source, tmp_path, **kwargs):
    return archive.archive_raw(source, tmp_path / "bundle", root=tmp_path,
                               session_id=kwargs.get("session_id", "s"),
                               closure_note="synthetic fixture: context exited, writer closed")


def test_roundtrip_keeps_unknown_direction_and_research_rejection(source, tmp_path):
    before = source.read_bytes()
    receipt = pack(source, tmp_path)
    restored = archive.restore_raw(tmp_path / "bundle", tmp_path / "restored")
    assert restored.read_bytes() == before == source.read_bytes()
    assert receipt["source"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert receipt["verification"]["research_quality"] == "not_certified"
    assert receipt["verification"]["control_counts"]["parse_error"] == 1
    with read_raw_v2(restored) as (_, records):
        rows = list(records)
    assert rows[1]["event"].is_buy is None
    assert rows[1]["raw_fields"]["fids"]["15"] == " 52244"
    with pytest.raises(ResearchRunFailed):
        run_raw_v2(restored, output_root=tmp_path / "research",
                   simulator_config=dict(source="test", session_id="s", code="005930",
                       venue="unknown", cash=100000, max_quote_age_ns=100,
                       buy_latency_ns=5, sell_latency_ns=5, cancel_latency_ns=2, fee_rate="0.001"),
                   quantity=2)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sidecars_preserved_and_rejected(source, tmp_path, suffix):
    sidecar = source.with_name(source.name + suffix)
    sidecar.write_bytes(b"evidence")
    with pytest.raises(ValueError, match="sidecar"):
        pack(source, tmp_path)
    assert sidecar.read_bytes() == b"evidence"
    assert not (tmp_path / "bundle").exists()


def test_incomplete_closed_connection_rejected(tmp_path):
    source = tmp_path / "source" / "raw.db"
    create(source, finish=False)
    with pytest.raises(ValueError, match="incomplete"):
        pack(source, tmp_path)
    assert not (tmp_path / "bundle/archive.json").exists()


def test_live_sqlite_connection_even_with_closed_header_rejected(source, tmp_path):
    conn = sqlite3.connect(source)
    try:
        conn.execute("SELECT value FROM metadata").fetchone()
        with pytest.raises((OSError, ValueError)):
            pack(source, tmp_path)
    finally:
        conn.close()


def test_guard_blocks_write_and_delete(source):
    with archive._sealed_source(source):
        with pytest.raises(OSError):
            source.open("r+b")
        with pytest.raises(OSError):
            source.unlink()


def test_collector_lease_blocks_archive(source, tmp_path):
    with CollectorLease(tmp_path):
        with pytest.raises(RuntimeError, match="collector"):
            pack(source, tmp_path)
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("stage", ["archive", "restore"])
def test_destination_collision_preserved(source, tmp_path, stage):
    if stage == "restore":
        pack(source, tmp_path)
    target = tmp_path / ("bundle" if stage == "archive" else "restored")
    target.mkdir(exist_ok=True)
    sentinel = target / "sentinel"
    sentinel.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        pack(source, tmp_path) if stage == "archive" else archive.restore_raw(tmp_path / "bundle", target)
    assert sentinel.read_bytes() == b"keep"


@pytest.mark.parametrize("damage", ["truncate", "flip", "inflate", "hash"])
def test_damaged_archive_never_publishes_restored_db(source, tmp_path, damage):
    pack(source, tmp_path)
    zipped = tmp_path / "bundle/raw.db.gz"
    receipt_path = tmp_path / "bundle/archive.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    data = zipped.read_bytes()
    if damage == "truncate":
        data = data[:-8]
    elif damage == "flip":
        data = data[:20] + bytes([data[20] ^ 255]) + data[21:]
    elif damage == "inflate":
        data = gzip.compress(b"x" * (receipt["source"]["size"] + 1))
    else:
        receipt["source"]["sha256"] = "0" * 64
    zipped.write_bytes(data)
    # Even if the compressed digest is replaced, CRC/EOF/byte checks must fail.
    receipt["compression"].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises((ValueError, OSError, EOFError, archive.zlib.error)):
        archive.restore_raw(tmp_path / "bundle", tmp_path / "restored")
    assert not (tmp_path / "restored/raw.db").exists()


@pytest.mark.parametrize("failure", ["space", "sync", "verify", "publish"])
def test_intermediate_failure_has_no_completion_marker(source, tmp_path, monkeypatch, failure):
    before = source.read_bytes()
    def fail(*args, **kwargs):
        raise OSError("synthetic ENOSPC/interruption")
    method = {"space": "require_disk_space", "sync": "_sync", "verify": "_inflate",
              "publish": "_json_publish"}[failure]
    monkeypatch.setattr(archive, method, fail)
    with pytest.raises(OSError):
        pack(source, tmp_path)
    assert not (tmp_path / "bundle/archive.json").exists()
    assert source.read_bytes() == before
    with pytest.raises(FileNotFoundError):
        archive.restore_raw(tmp_path / "bundle", tmp_path / "restored")


def test_wrong_session_does_not_publish(source, tmp_path):
    with pytest.raises(ValueError, match="session mismatch"):
        pack(source, tmp_path, session_id="other")
    assert not (tmp_path / "bundle/archive.json").exists()


def test_large_source_rejected_before_read(source, tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "MAX_BYTES", 1)
    with pytest.raises(ValueError, match="32 MiB"):
        pack(source, tmp_path)
    assert not (tmp_path / "bundle").exists()


def test_publish_never_overwrites(tmp_path):
    partial, final = tmp_path / "partial", tmp_path / "final"
    partial.write_bytes(b"new")
    final.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        archive._publish(partial, final)
    assert final.read_bytes() == b"old"


def test_missing_receipt_rejects_even_complete_gzip(source, tmp_path):
    pack(source, tmp_path)
    (tmp_path / "bundle/archive.json").unlink()
    with pytest.raises(FileNotFoundError):
        archive.restore_raw(tmp_path / "bundle", tmp_path / "restored")


def test_compressed_hash_mismatch_rejected_before_output(source, tmp_path):
    pack(source, tmp_path)
    with (tmp_path / "bundle/raw.db.gz").open("ab") as stream:
        stream.write(b"damage")
    with pytest.raises(ValueError, match="compressed byte"):
        archive.restore_raw(tmp_path / "bundle", tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_raw_checksum_damage_rejected(source, tmp_path):
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE events SET payload=replace(payload, '52244', '52245') WHERE seq=2")
    conn.close()
    with pytest.raises(ValueError, match="checksum"):
        pack(source, tmp_path)
    assert not (tmp_path / "bundle/archive.json").exists()


def test_restore_failure_before_publication(source, tmp_path, monkeypatch):
    pack(source, tmp_path)
    def fail(*args):
        raise KeyboardInterrupt("synthetic interruption")
    monkeypatch.setattr(archive, "_publish", fail)
    with pytest.raises(KeyboardInterrupt):
        archive.restore_raw(tmp_path / "bundle", tmp_path / "restored")
    assert not (tmp_path / "restored/raw.db").exists()


def test_payload_memory_budget_rejected(source, tmp_path):
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE events SET payload=? WHERE seq=2", ("x" * (65536 + 1),))
    conn.close()
    with pytest.raises(ValueError, match="record budget"):
        pack(source, tmp_path)
    assert not (tmp_path / "bundle/archive.json").exists()


def test_cli_archive_restore(source, tmp_path, monkeypatch, capsys):
    from scripts import archive_raw_v2 as cli
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    bundle, restored = tmp_path / "bundle", tmp_path / "restored"
    monkeypatch.setattr("sys.argv", ["archive_raw_v2", "archive", str(source), str(bundle),
                                    "--session-id", "s", "--closure-note", "synthetic closed fixture"])
    cli.main()
    assert str(bundle / "archive.json") in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["archive_raw_v2", "restore", str(bundle), str(restored)])
    cli.main()
    assert str(restored / "raw.db") in capsys.readouterr().out
    assert source.read_bytes() == (restored / "raw.db").read_bytes()
