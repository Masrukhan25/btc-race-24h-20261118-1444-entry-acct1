"""Minute-bar dip-and-rebound bot for the CBBTC 24-hour race.

Sits in cash, waits for a sharp fall that is already turning up, buys the
whole stack, and lets a trailing stop carry the bounce. Two ways in:

* a deep washout that prints a long lower wick and reverses hard
  (the "rebound"), and
* a fast dip off a short-window high while the wider fall stays shallow
  (the "dip").

Integer math throughout (quote units and price ints), no clock, no
randomness, so two runs over the same bars answer byte for byte alike.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

TICKER = "CBBTC"
SCALE = 10_000  # one unit = 10_000 bps

# --- entry gates -----------------------------------------------------------
EARLIEST_BAR = 62        # closes needed before the first entry
CUTOFF_BAR = 1320        # no entries after this bar
ENTRY_LIMIT = 3          # entries per window, all kinds together
REST_BARS = 60           # bars to wait after an exit
SLIP_CAP = 200           # max slippage vs latest close, bps
EQUITY_GIVEUP = 200      # stop entering once equity is under 98% of start

# --- the deep-washout rebound ----------------------------------------------
REBOUND_FALL_NEAR = 150  # close-to-close fall over 60 bars, bps
REBOUND_FALL_FAR = 250   # close-to-close fall over 120 bars, bps
REBOUND_CRASH = 550      # fall from the 720-bar high, bps
REBOUND_WICK_BARS = 20   # look back this far for a wick bar
REBOUND_WICK_SIZE = 20   # wick bar range, bps
REBOUND_WICK_KEEP = 60   # close must keep >= 60% of the bar below it
REBOUND_JUMP = 60        # reversal bar must close up this much, bps
REBOUND_ARM = 90         # trailing arms at +90 bps
REBOUND_TRAIL = 90       # then trails the peak by 90 bps
REBOUND_HOLD = 300       # max bars held
REBOUND_LIMIT = 2        # max rebound trades per window
REBOUND_STOP_LOW = 100   # stop at most 100 bps under the entry
REBOUND_STOP_HIGH = 50   # stop at least 50 bps under the entry
REBOUND_STOP_GAP = 20    # stop sits 20 bps under the washout low
REBOUND_PAYOFF = 2       # room to the high must be >= 2x the risk

# --- the fast dip (short lookback) -----------------------------------------
DIP_PEAK_SPAN = 60       # high lookback for the dip, bars
DIP_DROP = 120           # fall from that 60-bar high, bps
DIP_CRASH_MAX = 550      # the 720-bar fall must stay under this, bps
DIP_RSI_MAX = 50         # RSI must sit under 50
DIP_SHARP = 150          # close-to-close fall over 60 bars, bps
DIP_STOP = 60            # hard stop under the entry, bps
DIP_ARM = 90             # trailing arms at +90 bps
DIP_TRAIL = 90           # then trails the peak by 90 bps
DIP_HOLD = 400           # max bars held
DIP_LIMIT = 1            # max dip trades per window

WIDE_SPAN = 720          # the long window every crash measure uses
RSI_SPAN = 14            # Wilder RSI length


class Market:
    """The tape: every close/high/low seen so far, plus the RSI trail.

    Every measure that reaches back N bars returns 0 when the tape is
    shorter than N -- never index history you do not have.
    """

    def __init__(self):
        self.close = []
        self.high = []
        self.low = []
        self.rsi = []
        self._up = None    # Wilder average gain
        self._down = None  # Wilder average loss

    def __len__(self):
        return len(self.close)

    def add(self, bar):
        """Append one bar (string or int fields) and refresh the RSI."""
        self.close.append(int(bar["c"]))
        self.high.append(int(bar["h"]))
        self.low.append(int(bar["l"]))
        self._feed_rsi()

    def _feed_rsi(self):
        """Append the RSI(14) for the newest close (Wilder smoothing)."""
        n = len(self.close)
        if n < 2:
            self.rsi.append(None)
            return
        up = self.close[-1] - self.close[-2]
        dn = self.close[-2] - self.close[-1]
        if up < 0:
            up = 0
        if dn < 0:
            dn = 0
        if n == RSI_SPAN + 1:
            # First value: plain mean of the first RSI_SPAN moves.
            tot_up = 0
            tot_dn = 0
            for i in range(1, RSI_SPAN + 1):
                move = self.close[i] - self.close[i - 1]
                if move > 0:
                    tot_up += move
                else:
                    tot_dn -= move
            self._up = tot_up / RSI_SPAN
            self._down = tot_dn / RSI_SPAN
        elif n > RSI_SPAN + 1:
            self._up = (self._up * (RSI_SPAN - 1) + up) / RSI_SPAN
            self._down = (self._down * (RSI_SPAN - 1) + dn) / RSI_SPAN
        else:
            self.rsi.append(None)
            return
        if self._down == 0:
            self.rsi.append(100.0)
        else:
            ratio = self._up / self._down
            self.rsi.append(100.0 - 100.0 / (1.0 + ratio))

    def peak_drop(self, span):
        """Fall in bps from the highest high of `span` bars to the prev close.
        The span ends at the prev bar; 0 when the tape is too short."""
        if len(self.high) < span + 1:
            return 0
        top = max(self.high[-span - 1:-1])
        prev = self.close[-2]
        if top <= 0:
            return 0
        return (top - prev) * SCALE // top

    def close_drop(self, bars):
        """Close-to-close fall in bps, from `bars` bars before the prev close
        to the prev close. 0 when the tape is too short."""
        if len(self.close) < bars + 2:
            return 0
        ref = self.close[-bars - 2]
        prev = self.close[-2]
        if ref <= 0:
            return 0
        return (ref - prev) * SCALE // ref

    def peak(self, span):
        """Highest high of `span` bars ending at the prev bar (0 if too short)."""
        if len(self.high) < span + 1:
            return 0
        return max(self.high[-span - 1:-1])

    def trough(self, span):
        """Lowest low of `span` bars ending at the prev bar (0 if too short)."""
        if len(self.low) < span + 1:
            return 0
        return min(self.low[-span - 1:-1])

    def wick_seen(self, span):
        """True if any of the last `span` bars has a long lower wick:
        range at least 20 bps and the close keeping 60% of it below."""
        n = len(self.close)
        start = n - span
        if start < 0:
            start = 0
        for j in range(start, n):
            hi = self.high[j]
            lo = self.low[j]
            cl = self.close[j]
            if hi <= lo or cl <= 0:
                continue
            if (hi - lo) * SCALE // cl < REBOUND_WICK_SIZE:
                continue
            if (cl - lo) * 100 // (hi - lo) >= REBOUND_WICK_KEEP:
                return True
        return False


class OpenTrade:
    """One open position: its kind, its entry, and where it gets out."""

    def __init__(self, kind, entry_px, stop_px, arm_bps, trail_bps, max_hold):
        self.kind = kind            # "rebound" or "dip"
        self.entry = entry_px       # fill price (the fill bar's open)
        self.stop = stop_px         # fixed at decision time
        self.peak = entry_px        # highest close seen while open
        self.held = 0               # bars held so far
        self.armed = False          # trailing stop switched on
        self.arm_bps = arm_bps
        self.trail_bps = trail_bps
        self.max_hold = max_hold

    def exit_price(self, close_px):
        """Advance one bar; the exit price when this bar closes the trade,
        else None to stay in."""
        self.held += 1
        if close_px > self.peak:
            self.peak = close_px
        if not self.armed and close_px >= self.entry * (SCALE + self.arm_bps) // SCALE:
            self.armed = True
        if close_px <= self.stop:
            return self.stop
        if self.armed and close_px <= self.peak * (SCALE - self.trail_bps) // SCALE:
            return self.peak * (SCALE - self.trail_bps) // SCALE
        if self.held >= self.max_hold:
            return close_px
        return None


def rebound_signal(mkt):
    """The deep-washout rebound. Returns its stop price, or 0 when absent.

    All measures anchor on the prev bar, because the reversal bar itself
    lifts the close and would shrink the fall.
    """
    if len(mkt) < 2 or mkt.rsi[-1] is None or mkt.rsi[-2] is None:
        return 0
    prev = mkt.close[-2]
    cur = mkt.close[-1]
    if prev <= 0:
        return 0
    # 1. deep fall at the prev bar: 60 bars or 120 bars.
    if mkt.close_drop(60) < REBOUND_FALL_NEAR and mkt.close_drop(120) < REBOUND_FALL_FAR:
        return 0
    # 2. a washout: the fall from the 720-bar high reaches 550 bps.
    if mkt.peak_drop(WIDE_SPAN) < REBOUND_CRASH:
        return 0
    # 3. a long lower wick printed in the last 20 bars.
    if not mkt.wick_seen(REBOUND_WICK_BARS):
        return 0
    # 4. reversal: this bar closes up >= 60 bps and above the prev high.
    if (cur - prev) * SCALE // prev < REBOUND_JUMP:
        return 0
    if cur <= mkt.high[-2]:
        return 0
    # 5. RSI turning up (no level filter).
    if mkt.rsi[-1] <= mkt.rsi[-2]:
        return 0
    # 6. reward/risk: room back to the 720-bar high at least twice the risk.
    top = mkt.peak(WIDE_SPAN)
    floor = mkt.trough(WIDE_SPAN)
    stop0 = floor * (SCALE - REBOUND_STOP_GAP) // SCALE
    low = cur * (SCALE - REBOUND_STOP_LOW) // SCALE
    high = cur * (SCALE - REBOUND_STOP_HIGH) // SCALE
    stop = min(max(stop0, low), high)
    if top - cur < REBOUND_PAYOFF * (cur - stop):
        return 0
    return stop


def dip_signal(mkt):
    """The fast dip off a short-window high. True when every part holds."""
    if len(mkt) < 2 or mkt.rsi[-1] is None:
        return False
    prev = mkt.close[-2]
    cur = mkt.close[-1]
    # 1. fall from the highest high of the last 60 bars (prev bar).
    if mkt.peak_drop(DIP_PEAK_SPAN) < DIP_DROP:
        return False
    # 2. not a washout: the 720-bar fall stays under 550 bps.
    if mkt.peak_drop(WIDE_SPAN) >= DIP_CRASH_MAX:
        return False
    # 3. RSI under 50.
    if mkt.rsi[-1] >= DIP_RSI_MAX:
        return False
    # 4. this bar closes up (any amount).
    if cur <= prev:
        return False
    # 5. sharp drop: close-to-close fall over the last 60 bars >= 150 bps.
    if mkt.close_drop(60) < DIP_SHARP:
        return False
    return True


# --- run state ---------------------------------------------------------------
tape = Market()
trade = None        # OpenTrade or None
order_out = None    # pending buy: {"kind", "stop", "arm", "trail", "hold"}
first_cash = None   # capital from hello
dips_taken = 0
rebounds_taken = 0
rebound_losses = 0  # consecutive rebound losses
rest_until = -1     # bar seq before which no new entry
last_stamp = None   # last batch t seen (duplicate guard)


def on_hello(state):
    """Capture the start capital from hello (never a hardcoded constant)."""
    global first_cash
    first_cash = int(state.hello["capital"]["quote_units"])


def on_batch(state, batch):
    """Decide the orders for this batch. Returns a list of orders (or [])."""
    global trade, order_out, first_cash
    global dips_taken, rebounds_taken, rebound_losses, rest_until, last_stamp

    # Find this batch's CBBTC bar; skip the batch when it carries none.
    bar = None
    for b in batch.get("bars", []):
        if b.get("symbol") == TICKER:
            bar = b
            break
    if bar is None:
        return []
    # Duplicate-bar guard: skip a batch that carries no new bar.
    stamp = batch.get("t")
    if stamp is not None and stamp == last_stamp:
        return []
    last_stamp = stamp

    cur = int(bar["c"])
    tape.add(bar)

    if first_cash is None:
        first_cash = int(state.hello["capital"]["quote_units"])

    holding = state.positions.get(TICKER)
    seq = state.seq

    # A pending buy either filled (position now held) or was rejected.
    if order_out is not None:
        if holding is not None:
            # Filled at this bar's open: entry is the fill bar's open.
            # The stop was already fixed at decision time.
            trade = OpenTrade(
                order_out["kind"],
                int(bar["o"]),
                order_out["stop"],
                order_out["arm"],
                order_out["trail"],
                order_out["hold"],
            )
            if order_out["kind"] == "dip":
                dips_taken += 1
            else:
                rebounds_taken += 1
        order_out = None

    # --- Manage an open position: stop, trailing, max hold. ---
    if trade is not None and holding is not None:
        out_px = trade.exit_price(cur)
        if out_px is not None:
            won = out_px > trade.entry
            was_rebound = trade.kind == "rebound"
            trade = None
            rest_until = seq + REST_BARS
            if was_rebound:
                rebound_losses = 0 if won else rebound_losses + 1
            return [{"side": "sell", "symbol": TICKER,
                     "qty": holding.qty, "max_slippage_bps": SLIP_CAP}]
        return []

    if trade is not None and holding is None:
        # Position vanished without our sell (should not happen): clear.
        trade = None

    # --- No position: look for an entry. ---
    if len(tape) < EARLIEST_BAR:
        return []
    if seq > CUTOFF_BAR:
        return []
    if dips_taken + rebounds_taken >= ENTRY_LIMIT:
        return []
    if seq < rest_until:
        return []
    if state.equity < first_cash * (SCALE - EQUITY_GIVEUP) // SCALE:
        return []
    if rebound_losses >= 2:
        return []

    if rebounds_taken < REBOUND_LIMIT:
        stop = rebound_signal(tape)
        if stop:
            order_out = {"kind": "rebound", "stop": stop, "arm": REBOUND_ARM,
                         "trail": REBOUND_TRAIL, "hold": REBOUND_HOLD}
            return [{"side": "buy", "symbol": TICKER,
                     "notional_quote_units": state.cash,
                     "max_slippage_bps": SLIP_CAP}]

    if dips_taken < DIP_LIMIT and dip_signal(tape):
        order_out = {"kind": "dip", "stop": cur * (SCALE - DIP_STOP) // SCALE,
                     "arm": DIP_ARM, "trail": DIP_TRAIL, "hold": DIP_HOLD}
        return [{"side": "buy", "symbol": TICKER,
                 "notional_quote_units": state.cash,
                 "max_slippage_bps": SLIP_CAP}]

    return []


if __name__ == "__main__":
    makefun_bars.run(on_batch, on_hello=on_hello)
