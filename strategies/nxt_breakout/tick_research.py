"""Causal research adapter for existing NXT rule parameters, one variant/account.

Requires normalized wall-clock seconds, trade direction and top-three quantities.
These inputs are NOT fabricated from raw-v1. Unknown venue is not certified NXT.
New behavior: enter only after observing regular-session open (legacy pre-open
logic looked ahead to that open). Fees and latency belong to TickSimulator.
"""
from collections import deque
from copy import deepcopy
from decimal import Decimal

from engine.nxt_tick_engine import PARAMS, get_tick_size
from execution.quote_validation import check_ordered_quote


class NxtResearchStrategy:
    def __init__(self, *, quantity, exit_rule="fixed", cooldown_ns=10_000_000_000, params=None):
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("positive integer quantity required")
        if exit_rule not in ("fixed", "tick_trail", "step_trail"):
            raise ValueError("unknown exit rule")
        if type(cooldown_ns) is not int or cooldown_ns < 0:
            raise ValueError("invalid cooldown")
        self.params = deepcopy(PARAMS if params is None else params)
        if self.params["macro"]["enabled"]:
            raise ValueError("point-in-time macro adapter not connected")
        self.quantity, self.exit_rule, self.cooldown_ns = quantity, exit_rule, cooldown_ns
        self.window, self.recent = deque(), deque()
        self.open = None
        self.pre_open = self.pre_last = None
        self.pre_volume = 0
        self.last_second = -1
        self.peak = self.average = Decimal(0)
        self.held = 0
        self.fill_cursor = 0
        self.cooldown_until = 0
        self.exit_requested = False
        self.serial = 0
        self.signals = []

    def _sync(self, sim):
        for fill in sim.fills[self.fill_cursor:]:
            if fill.side == "buy":
                self.average = (self.average * self.held + fill.price * fill.quantity) / (self.held + fill.quantity)
                self.held += fill.quantity
                self.peak = max(self.peak, fill.price)
            else:
                self.held -= fill.quantity
                if self.held == 0:
                    self.average = self.peak = Decimal(0)
                    self.cooldown_until = fill.time_ns + self.cooldown_ns
                    self.exit_requested = False
        self.fill_cursor = len(sim.fills)

    def _submit(self, sim, side, quantity, reason):
        self.serial += 1
        name = f"nxt-{self.exit_rule}-{self.serial}"
        self.signals.append((sim.now, side, quantity, reason))
        sim.submit(name, side, quantity)

    def __call__(self, view, sim):
        self._sync(sim)
        event = view.event
        second = event.market_second
        if type(second) is not int or not 0 <= second < 86400 or second < self.last_second:
            raise ValueError("valid nondecreasing market_second required")
        self.last_second = second
        entry, pre = self.params["entry"], self.params["nxt_overheat"]
        if event.kind == "trade":
            price = Decimal(str(event.price))
            if not price.is_finite() or price <= 0 or type(event.volume) is not int or event.volume <= 0 or type(event.is_buy) is not bool:
                raise ValueError("valid normalized trade price/volume/direction required")
            if pre["window"][0] <= second < pre["window"][1]:
                if self.pre_open is None:
                    self.pre_open = price
                self.pre_last, self.pre_volume = price, self.pre_volume + event.volume
            if second >= entry["session_start_sec"] and self.open is None:
                self.open = price
            while self.window and second - self.window[0][0] > entry["breakout_window_sec"]:
                self.window.popleft()
            prior_high = max((p for _, p in self.window), default=price)
            self.window.append((second, price))
            self.recent.append((event.volume, event.is_buy))
            while len(self.recent) > entry["recent_ticks"]:
                self.recent.popleft()
            if self.held:
                self.peak = max(self.peak, price)
        else:
            price = prior_high = None

        checked = check_ordered_quote(view)
        pending = [o for o in sim.orders.values() if o.status not in ("filled", "cancelled", "expired")]
        if self.held and checked.book:
            bid = checked.book.bid
            pnl = (bid - self.average) / self.average
            rules = self.params["exits"][self.exit_rule]
            trigger = pnl <= Decimal(str(rules["stop_loss_pct"]))
            if self.exit_rule == "fixed":
                trigger |= pnl >= Decimal(str(rules["take_profit_pct"]))
            elif self.exit_rule == "tick_trail":
                trigger |= bid <= self.peak - get_tick_size(self.peak) * rules["trail_ticks"]
            elif self.peak >= self.average * (1 + Decimal(str(rules["arm_threshold_pct"]))):
                trigger = (bid <= self.average + get_tick_size(self.average) * rules["breakeven_offset_ticks"]
                           or bid <= self.peak - get_tick_size(self.peak) * rules["trail_ticks"])
            self.exit_requested |= trigger
        if self.exit_requested:
            buys = [o for o in pending if o.side == "buy"]
            for order in buys:
                sim.cancel(order.order_id)
            # Wait for cancellation acknowledgement before sizing an exit.
            if not buys and self.held and not any(o.side == "sell" for o in pending):
                self._submit(sim, "sell", self.held, self.exit_rule)
            return
        if self.held or pending or sim.now < self.cooldown_until or price is None or self.open is None or not checked.book:
            return
        exhausted = (self.pre_open is not None and self.pre_last / self.pre_open - 1 >= Decimal(str(pre["gain_threshold"]))
                     and self.pre_volume >= pre["volume_threshold"])
        if exhausted:
            return
        q = view.quote
        sizes = (q.bid_sizes, q.ask_sizes)
        if any(not isinstance(levels, tuple) or len(levels) < 3 or
               any(type(n) is not int or n < 0 for n in levels[:3]) for levels in sizes):
            return  # top-one validation cannot supply the top-three OBI feature
        bid3, ask3 = sum(q.bid_sizes[:3]), sum(q.ask_sizes[:3])
        if q.bid_sizes[0] != q.bid_size or q.ask_sizes[0] != q.ask_size or ask3 <= 0:
            return
        volume = sum(v for v, _ in self.recent)
        buy_volume = sum(v for v, buy in self.recent if buy)
        spread = (checked.book.ask - checked.book.bid) / checked.book.bid
        if (price >= self.open and price >= prior_high
                and spread <= Decimal(str(entry["spread_max_pct"]))
                and Decimal(buy_volume) / volume >= Decimal(str(entry["buy_ratio_min"]))
                and bid3 > Decimal(str(entry["obi_min_ratio"])) * ask3
                and volume >= entry["min_vol_15t"]):
            self._submit(sim, "buy", self.quantity, "breakout")
