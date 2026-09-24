"""Synthetic regressions for the NXT unknown-direction quarantine window."""
from decimal import Decimal

import pytest

from strategies.nxt_breakout.direction_window import (
    POLICY,
    DirectionFeatureWindow,
)


def test_known_directions_preserve_current_volume_weighted_ratio_behavior():
    window = DirectionFeatureWindow(15)
    window.append(30, True)
    state = window.append(10, False)

    assert state.policy == POLICY
    assert state.observed_ticks == 2
    assert state.unknown_direction_ticks == 0
    assert state.total_volume == 40
    assert state.buy_volume == 30
    assert state.buy_ratio == Decimal("0.75")
    assert state.entry_direction_eligible is True


def test_unknown_direction_preserves_volume_but_blocks_direction_feature():
    window = DirectionFeatureWindow(15)
    window.append(30, True)
    state = window.append(237016, None)

    assert window.snapshot() == ((30, True), (237016, None))
    assert state.observed_ticks == 2
    assert state.total_volume == 237046
    assert state.unknown_direction_ticks == 1
    assert state.buy_volume is None
    assert state.buy_ratio is None
    assert state.entry_direction_eligible is False


def test_large_unknown_volume_is_never_counted_as_buy_or_sell():
    window = DirectionFeatureWindow(15)
    window.append(237016, None)
    state = window.append(1, True)

    assert state.total_volume == 237017
    assert state.buy_volume is None
    assert state.buy_ratio is None
    assert state.entry_direction_eligible is False


def test_unknown_ages_out_only_after_fifteen_new_trades():
    window = DirectionFeatureWindow(15)
    first = window.append(237016, None)
    assert first.entry_direction_eligible is False

    for _ in range(14):
        state = window.append(1, True)
        assert state.entry_direction_eligible is False
        assert state.unknown_direction_ticks == 1

    state = window.append(1, True)
    assert state.observed_ticks == 15
    assert state.unknown_direction_ticks == 0
    assert state.total_volume == 15
    assert state.buy_volume == 15
    assert state.buy_ratio == Decimal(1)
    assert state.entry_direction_eligible is True


def test_multiple_unknowns_block_until_the_last_one_ages_out():
    window = DirectionFeatureWindow(3)
    window.append(5, None)
    window.append(6, True)
    state = window.append(7, None)
    assert state.unknown_direction_ticks == 2
    assert state.entry_direction_eligible is False

    state = window.append(8, True)
    assert state.unknown_direction_ticks == 1
    assert state.entry_direction_eligible is False
    state = window.append(9, False)
    assert state.unknown_direction_ticks == 1
    assert state.entry_direction_eligible is False
    state = window.append(10, True)
    assert state.unknown_direction_ticks == 0
    assert state.buy_volume == 18
    assert state.total_volume == 27
    assert state.buy_ratio == Decimal(2) / Decimal(3)
    assert state.entry_direction_eligible is True


def test_partial_window_is_eligible_when_every_observed_direction_is_known():
    window = DirectionFeatureWindow(15)
    state = window.append(10, False)

    assert state.observed_ticks == 1
    assert state.buy_ratio == Decimal(0)
    assert state.entry_direction_eligible is True


@pytest.mark.parametrize("recent_ticks", [0, -1, True, 1.5, None])
def test_invalid_window_size_rejected(recent_ticks):
    with pytest.raises(ValueError, match="recent_ticks"):
        DirectionFeatureWindow(recent_ticks)


@pytest.mark.parametrize("volume", [0, -1, True, 1.5, None])
def test_invalid_volume_rejected(volume):
    window = DirectionFeatureWindow(15)
    with pytest.raises(ValueError, match="volume"):
        window.append(volume, True)


@pytest.mark.parametrize("direction", [0, 1, "buy", "", object()])
def test_non_boolean_non_null_direction_rejected(direction):
    window = DirectionFeatureWindow(15)
    with pytest.raises(ValueError, match="direction"):
        window.append(10, direction)


def test_repeated_state_reads_do_not_mutate_window():
    window = DirectionFeatureWindow(15)
    window.append(10, True)
    before = window.snapshot()

    a = window.state()
    b = window.state()

    assert a == b
    assert window.snapshot() == before
