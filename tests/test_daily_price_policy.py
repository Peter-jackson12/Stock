import json
import sys

import pandas as pd
import pytest

from core.price_policy import ACTUAL, ADJUSTED, PRICE_MANIFEST, PriceBasisError
from engine import data_loader
from engine.engine import BackTestEngine


def write_prices(path):
    path.mkdir(parents=True, exist_ok=True)
    # Synthetic split-free sample. Lack of a split cannot certify price basis.
    frame = pd.DataFrame({"Code": ["Name", "20220425", "20220426"],
                          "A005930": ["삼성전자", 100000, 101000]})
    for name in ("open", "close"):
        frame.to_csv(path / f"{name}.csv", encoding="CP949", index=False)
    return frame


def mark(path, basis=ACTUAL, **overrides):
    value = {"schema_version": 1, "source": "synthetic_test_fixture",
             "price_basis": basis, "files": ["open.csv", "close.csv"]}
    value.update(overrides)
    (path / PRICE_MANIFEST).write_text(json.dumps(value), encoding="utf-8")


def loader(path, strict=False):
    result = data_loader.DataLoader(require_actual_prices=strict)
    result.csv_path = path
    return result


@pytest.mark.parametrize("strict", [False, True])
def test_adjusted_price_is_rejected_before_csv_read_even_without_split(tmp_path, monkeypatch, strict):
    write_prices(tmp_path)
    mark(tmp_path, ADJUSTED)
    def must_not_read(*a, **kw):
        raise AssertionError("price gate must run before CSV reading")
    monkeypatch.setattr(pd, "read_csv", must_not_read)
    with pytest.raises(PriceBasisError, match="수정주가"):
        loader(tmp_path, strict).load_daily_csvs()


def test_old_quarantine_without_manifest_is_also_rejected(tmp_path):
    directory = tmp_path / "UNVERIFIED_FCHART"
    write_prices(directory)
    with pytest.raises(PriceBasisError, match="격리"):
        loader(directory).load_daily_csvs()


def test_legacy_is_preserved_but_never_certified_actual(tmp_path):
    frame = write_prices(tmp_path)
    replay = loader(tmp_path)
    loaded = replay.load_daily_csvs()
    pd.testing.assert_frame_equal(loaded["close"], frame.astype(str))
    assert replay.price_provenance["price_basis"] == "unknown_legacy"
    with pytest.raises(PriceBasisError, match="선언이 없습니다"):
        loader(tmp_path, True).load_daily_csvs()


def test_parent_meta_manifest_does_not_relabel_preserved_root_prices(tmp_path):
    write_prices(tmp_path)
    (tmp_path / "_meta_manifest.json").write_text(json.dumps({"price_basis": ADJUSTED}))
    quarantine = tmp_path / "unverified_fchart"
    write_prices(quarantine)
    mark(quarantine, ADJUSTED)
    result = loader(tmp_path)
    result.load_daily_csvs()
    assert result.price_provenance["price_basis"] == "unknown_legacy"


@pytest.mark.parametrize("value", ["{", "[]", '{"schema_version": 9}'])
def test_invalid_sidecar_cannot_fall_back_to_legacy(tmp_path, value):
    write_prices(tmp_path)
    (tmp_path / PRICE_MANIFEST).write_text(value)
    with pytest.raises(PriceBasisError):
        loader(tmp_path).load_daily_csvs()


def test_actual_declaration_must_cover_loaded_price_files(tmp_path):
    write_prices(tmp_path)
    mark(tmp_path, files=["open.csv"])
    with pytest.raises(PriceBasisError, match="scope"):
        loader(tmp_path, True).load_daily_csvs()


def test_unknown_basis_fails_strict_mode(tmp_path):
    write_prices(tmp_path)
    mark(tmp_path, "unknown")
    assert loader(tmp_path).load_daily_csvs()
    with pytest.raises(PriceBasisError, match="확정되지"):
        loader(tmp_path, True).load_daily_csvs()


def test_strict_engine_wiring_and_run_identity(tmp_path, monkeypatch):
    write_prices(tmp_path)
    monkeypatch.setattr(data_loader, "CSV_PATH", tmp_path)
    with pytest.raises(PriceBasisError):
        BackTestEngine(require_actual_prices=True, runs_root=tmp_path / "runs")
    mark(tmp_path)
    strict = BackTestEngine(require_actual_prices=True, runs_root=tmp_path / "runs")
    replay = BackTestEngine(runs_root=tmp_path / "runs")
    for engine in (strict, replay):
        engine._prepare_run_identity(["20220426"])
        assert engine._prev_close("A005930", "20220426") == 100000
    assert strict.run_params["price_policy"]["price_basis"] == ACTUAL
    assert "price_policy" not in replay.run_params  # frozen legacy parameter hash
    assert strict.run_id != replay.run_id


def test_cli_can_require_actual_prices(monkeypatch):
    from engine.main import _parse_args
    monkeypatch.setattr(sys, "argv", ["engine.main", "--require-actual-prices"])
    assert _parse_args().require_actual_prices
