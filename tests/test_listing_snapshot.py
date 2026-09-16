import json
import pytest
from scripts.reconcile_listing_snapshot import read_finder, reconcile


def test_listing_overlap_is_not_silently_treated_as_delisted():
    assert reconcile(["A", "B", "C", "D"], {"A", "B"}, {"B", "C"}) == dict(
        listed_only=["A"], both_lists=["B"], delisted_only=["C"], absent_from_both=["D"])


def test_empty_source_cannot_classify_every_candidate_missing(tmp_path):
    path = tmp_path / "finder.json"
    path.write_text(json.dumps({"block1": []}))
    with pytest.raises(ValueError):
        read_finder(path)


def test_long_instrument_code_is_not_truncated_to_stock_code(tmp_path):
    path = tmp_path / "finder.json"
    path.write_text(json.dumps({"block1": [{"short_code": "06031012"}]}))
    codes, source = read_finder(path)
    assert codes == {"06031012"} and source["non_six_character_codes"] == 1
    assert reconcile(["060310"], set(), codes)["absent_from_both"] == ["060310"]
