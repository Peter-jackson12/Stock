"""Causal quarantine policy for NXT recent-trade direction features.

This pure helper does not change NxtResearchStrategy or any smoke gate. It
defines how an opt-in strategy integration may handle a trade whose price and
volume are valid but whose aggressor direction is unknown.

Policy:
- preserve every trade in the recent N-trade window,
- preserve its volume even when direction is unknown,
- never infer, coerce, drop, or relabel unknown direction,
- while any unknown direction remains in the window, the directional entry
  feature is unavailable and new entry must be suppressed,
- once unknown observations age out naturally, volume-weighted buy ratio is
  available again from the remaining fully-known window.

Existing strict behavior remains the default until a later integration opts in.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal


POLICY = "unknown_direction_recent_window_quarantine_v0"


@dataclass(frozen=True)
class DirectionWindowState:
    policy: str
    window_size: int
    observed_ticks: int
    unknown_direction_ticks: int
    total_volume: int
    buy_volume: int | None
    buy_ratio: Decimal | None
    entry_direction_eligible: bool


class DirectionFeatureWindow:
    """Bounded recent-trade window with fail-closed unknown-direction semantics."""

    def __init__(self, recent_ticks: int):
        if type(recent_ticks) is not int or recent_ticks <= 0:
            raise ValueError("positive integer recent_ticks required")
        self.recent_ticks = recent_ticks
        self._items = deque(maxlen=recent_ticks)

    def append(self, volume: int, is_buy: bool | None) -> DirectionWindowState:
        if type(volume) is not int or volume <= 0:
            raise ValueError("positive integer trade volume required")
        if is_buy is not None and type(is_buy) is not bool:
            raise ValueError("trade direction must be bool or None")
        self._items.append((volume, is_buy))
        return self.state()

    def state(self) -> DirectionWindowState:
        total_volume = sum(volume for volume, _ in self._items)
        unknown = sum(direction is None for _, direction in self._items)
        if unknown:
            buy_volume = None
            buy_ratio = None
            eligible = False
        else:
            buy_volume = sum(volume for volume, direction in self._items if direction)
            buy_ratio = (
                Decimal(buy_volume) / Decimal(total_volume)
                if total_volume > 0 else None
            )
            eligible = total_volume > 0
        return DirectionWindowState(
            policy=POLICY,
            window_size=self.recent_ticks,
            observed_ticks=len(self._items),
            unknown_direction_ticks=unknown,
            total_volume=total_volume,
            buy_volume=buy_volume,
            buy_ratio=buy_ratio,
            entry_direction_eligible=eligible,
        )

    def snapshot(self) -> tuple[tuple[int, bool | None], ...]:
        """Read-only test/diagnostic view; callers must not mutate strategy state."""
        return tuple(self._items)
