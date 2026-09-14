"""
tests/test_universe.py — 유니버스 선언과 대조 (Phase B-4)

ARCHITECTURE_V2.md §3.6.1 '추가 발견'이 지목한 문제는 유니버스가 좁다는 것이
아니라 **좁아지는 것이 보이지 않는다**는 것이었다. 일봉 매트릭스 컬럼 수가
유니버스를 암묵적으로 정했고, 매니페스트의 universe_size 는 그냥 작은 숫자를
보고할 뿐이라 아무도 의심하지 않았다.

그래서 여기서 고정하는 것은 "선언대로 돌았는가"가 아니라
**"선언과 실제가 다를 때 그 차이가 사유별로 드러나는가"** 다.

실행:
    uv run pytest tests/test_universe.py -v
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.universe import (                                            # noqa: E402
    DEFAULT_UNIVERSE_PATH,
    UniverseSpec,
    load_universe,
    reconcile,
    resolve_codes,
)

YAML = textwrap.dedent("""
    version: "1.0"
    default: only_two

    universes:
      only_two:
        source: explicit
        codes: ["000270", "005930"]
        description: 명시 목록

      from_daily:
        source: daily_matrix

      kospi_common:
        source: rule
        market: [KOSPI]
        common_only: true
""")


@pytest.fixture
def universe_file(tmp_path: Path) -> Path:
    path = tmp_path / "universe.yaml"
    path.write_text(YAML, encoding="utf-8")
    return path


@pytest.fixture
def key_csv(tmp_path: Path) -> Path:
    """key.csv 축소판 — 0행 종목명, 1행 시장구분."""
    frame = pd.DataFrame({
        "Code": ["Name", "20210422"],
        "A000270": ["기아", "KOSPI"],
        "A000275": ["기아우", "KOSPI"],          # 우선주 (끝자리 5)
        "A035720": ["카카오", "KOSDAQ"],
        "A900110": ["외감종목", "외감"],
    })
    path = tmp_path / "key.csv"
    frame.to_csv(path, index=False, encoding="CP949")
    return tmp_path


# ---------------------------------------------------------------------------
# 선언 로딩
# ---------------------------------------------------------------------------

def test_default_universe_is_used_when_name_is_omitted(universe_file: Path):
    assert load_universe(None, universe_file).name == "only_two"


def test_unknown_name_lists_the_available_ones(universe_file: Path):
    """조용히 기본값으로 떨어지면 '내가 선언한 유니버스로 돌고 있다'는 착각이 생긴다."""
    with pytest.raises(KeyError, match="only_two"):
        load_universe("typo", universe_file)


def test_explicit_source_requires_codes(tmp_path: Path):
    path = tmp_path / "u.yaml"
    path.write_text("default: e\nuniverses:\n  e:\n    source: explicit\n", encoding="utf-8")
    with pytest.raises(ValueError, match="codes"):
        load_universe("e", path)


def test_unknown_source_is_rejected(tmp_path: Path):
    path = tmp_path / "u.yaml"
    path.write_text("default: x\nuniverses:\n  x:\n    source: vibes\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source"):
        load_universe("x", path)


def test_run_id_input_carries_the_declaration_not_the_result(universe_file: Path):
    """
    해석 결과(종목 수)가 run_id 에 들어가면 데이터가 채워지는 대로 id 가 바뀐다.
    선언이 같으면 같은 런이어야 한다.
    """
    params = load_universe("only_two", universe_file).as_params()

    assert params == {"name": "only_two", "source": "explicit", "codes": ["000270", "005930"]}
    assert "declared" not in params


# ---------------------------------------------------------------------------
# 해석
# ---------------------------------------------------------------------------

def test_explicit_codes_are_zero_padded_and_sorted(universe_file: Path):
    codes = resolve_codes(load_universe("only_two", universe_file))
    assert codes == ("000270", "005930")


def test_daily_matrix_source_strips_the_a_prefix(universe_file: Path):
    frame = pd.DataFrame(columns=["Code", "A000270", "A005930"])
    codes = resolve_codes(load_universe("from_daily", universe_file), daily_frame=frame)

    assert codes == ("000270", "005930")


def test_rule_source_filters_market_and_preferred_shares(universe_file: Path, key_csv: Path):
    """보통주는 끝자리 0, 우선주는 5/7/9/K/L/M — key.csv 에 구분 컬럼이 없어 관행을 쓴다."""
    codes = resolve_codes(load_universe("kospi_common", universe_file), csv_path=key_csv)

    assert codes == ("000270",)          # 000275(우선주) · 035720(KOSDAQ) · 900110(외감) 제외


def test_rule_source_can_include_preferred_shares(universe_file: Path, key_csv: Path):
    spec = UniverseSpec(name="t", source="rule", market=("KOSPI",), common_only=False)
    assert resolve_codes(spec, csv_path=key_csv) == ("000270", "000275")


def test_rule_source_needs_the_market_key_file(universe_file: Path, tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="시장 분류"):
        resolve_codes(load_universe("kospi_common", universe_file), csv_path=tmp_path / "empty")


# ---------------------------------------------------------------------------
# 대조 — 이 기능의 핵심
# ---------------------------------------------------------------------------

def test_missing_codes_are_bucketed_by_reason():
    """
    사유가 붙어야 행동이 달라진다. no_daily 197 이면 일봉 수집을 보게 되고,
    no_lob 1050 이면 수집기를 보게 된다. 총계만 보면 둘 다 '원래 그런가 보다'가 된다.
    """
    rec = reconcile(
        ["A", "B", "C", "D", "E"],
        processed=["A"],
        seen_in_lob=["A", "B", "C", "D"],       # E 는 수집 자체가 안 됨
        seen_in_daily=["A", "B"],               # C, D 는 일봉이 없음
        missing_features=["B"],                 # B 는 LOB·일봉 있는데 피처가 없음
    )

    assert rec.declared == 5
    assert rec.processed == 1
    assert rec.missing == {"no_daily": 2, "no_features": 1, "no_lob": 1}
    assert rec.missing_codes["no_lob"] == ("E",)
    assert rec.missing_codes["no_daily"] == ("C", "D")


def test_reason_order_is_data_flow_order():
    """LOB 도 일봉도 없으면 no_lob 이다 — 먼저 끊긴 지점을 사유로 삼는다."""
    rec = reconcile(["X"], processed=[], seen_in_lob=[], seen_in_daily=[])

    assert rec.missing == {"no_lob": 1}


def test_unexplained_gap_is_labelled_other():
    """모든 조건을 만족하는데 처리되지 않았다면 원천 데이터 오류다 — 삼키지 않는다."""
    rec = reconcile(["X"], processed=[], seen_in_lob=["X"], seen_in_daily=["X"])

    assert rec.missing == {"other": 1}


def test_full_processing_reports_no_gap():
    rec = reconcile(["A", "B"], processed=["A", "B"], seen_in_lob=["A", "B"], seen_in_daily=["A", "B"])

    assert rec.missing == {}
    assert rec.missing_pct == 0.0
    assert rec.within(0.0)


def test_processed_outside_the_declaration_is_ignored():
    """선언에 없는 종목이 처리됐다고 커버리지가 올라가면 대조가 거짓말을 한다."""
    rec = reconcile(["A"], processed=["A", "Z"], seen_in_lob=["A", "Z"], seen_in_daily=["A", "Z"])

    assert rec.declared == 1
    assert rec.processed == 1


def test_manifest_shape_matches_the_spec():
    rec = reconcile(
        [f"{i:06d}" for i in range(200)],
        processed=["000000", "000001", "000002"],
        seen_in_lob=[f"{i:06d}" for i in range(200)],
        seen_in_daily=["000000", "000001", "000002"],
    )
    payload = rec.as_manifest(sample=3)

    assert payload["declared"] == 200
    assert payload["processed"] == 3
    assert payload["missing"] == {"no_daily": 197}
    assert payload["missing_pct"] == pytest.approx(98.5)
    assert len(payload["missing_sample"]["no_daily"]) == 3


# ---------------------------------------------------------------------------
# 실제 선언 파일
# ---------------------------------------------------------------------------

def test_shipped_universe_yaml_is_loadable():
    """저장소에 든 선언이 전부 유효해야 한다 — 오타는 런타임이 아니라 여기서 잡는다."""
    import yaml

    raw = yaml.safe_load(DEFAULT_UNIVERSE_PATH.read_text(encoding="utf-8"))
    for name in raw["universes"]:
        assert load_universe(name).name == name
    assert raw["default"] in raw["universes"]
