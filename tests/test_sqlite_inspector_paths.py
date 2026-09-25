"""경로 이전 회귀: 실제 raw 파일 대신 임시 SQLite만 사용한다."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_inspector():
    spec = importlib.util.spec_from_file_location("stock_sqlite_inspector", ROOT / "sqlite.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_does_not_open_database_and_default_follows_checkout(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("import must not access a database")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    module = load_inspector()
    assert module.DEFAULT_DATABASE == ROOT / "sampledata" / "raw_ticks" / "20260914_raw.db"


def test_missing_override_does_not_create_database(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        load_inspector().main(["--database", str(path)])
    assert not path.exists()


def test_override_reads_synthetic_database_without_modifying_it(tmp_path, monkeypatch, capsys):
    path = tmp_path / "공백 # fixture.db"
    conn = sqlite3.connect(path)
    for table in ("raw_trades", "raw_quotes"):
        conn.execute(f"CREATE TABLE {table} (value TEXT)")
        conn.executemany(f"INSERT INTO {table} VALUES (?)", [("first",), ("last",)])
    conn.commit()
    conn.close()
    before = path.read_bytes()
    module = load_inspector()
    real_connect = sqlite3.connect
    def read_only_connect(database_uri, **kwargs):
        assert database_uri.endswith("?mode=ro") and kwargs == {"uri": True}
        return real_connect(database_uri, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", read_only_connect)
    monkeypatch.chdir(tmp_path)
    assert module.main(["--database", str(path)]) == 0
    output = capsys.readouterr().out
    assert "raw_trades" in output and "raw_quotes" in output
    assert "first" in output and "last" in output
    assert path.read_bytes() == before
