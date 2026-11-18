"""WideTrail-short: a long-only dip-and-bounce bot for the BTC 24h race.

The bot watches one asset (CBBTC) minute by minute and takes at most three
full-size entries per window:

* Setup B -- a capitulation bounce: after a deep fall from the 720-bar high
  (>= 550 bps) with a long lower wick and a reversal bar, it buys when
  RSI(14) turns up and the room back to the 720-bar high is at least twice
  the risk. Trailing exit, arm/trail 90/90 bps.
* Setup D -- a sharp-drop dip-buy: a fast fall from the 60-bar high
  (>= 120 bps) that is NOT a capitulation (720-bar fall under 550 bps),
  RSI(14) under 50, this bar closing up, and a close-to-close fall of
  >= 150 bps over the last 60 bars. Trailing exit, arm/trail 90/90 bps.

The short lookback (60 bars, first entry from 62 closes) lets the bot catch
a dip that prints early in the window, before a 120-bar high even exists.
Every history access is length-guarded: a helper returns 0 when there is
not enough history yet, so the bot never indexes past the start of the data.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars  # noqa: E402

SYMBOL = "CBBTC"
BPS = 10000
RSI_LEN = 14

# --- Setup B (capitulation bounce) ---
B_DROP60_BPS = 150       # fall60 at the previous bar
B_DROP120_BPS = 250      # ... or fall120 at the previous bar
B_DROP720_BPS = 550      # fall from the 720-bar high must reach this
B_WICK_LOOKBACK = 20     # bars scanned for a long lower wick
B_WICK_RANGE_BPS = 20    # the wick bar's range must reach this
B_WICK_FRAC = 60         # close in the top 40% of the bar (wick >= 60%)
B_REVERSAL_BPS = 60      # reversal bar closes up at least this much
B_STOP_BELOW_LOW_BPS = 20  # stop sits 20 bps under the capitulation low
B_STOP_LO_BPS = 100      # stop clamped to 50..100 bps below the entry
B_STOP_HI_BPS = 50
B_ARM_BPS = 90           # trailing stop arms at +90 bps
B_TRAIL_BPS = 90         # ... then trails the peak by 90 bps
B_MAX_HOLD = 300
B_MAX_TRADES = 2

# --- Setup D (sharp-drop dip-buy, short lookback) ---
D_DROP60_BPS = 120       # fall from the highest high of the last 60 bars
D_DROP720_MAX_BPS = 550  # the 720-bar fall must stay UNDER this
D_RSI_MAX = 50           # RSI(14) under 50
D_SHARP60_BPS = 150      # ... AND a fall of >= 150 bps over the last 60 bars
D_STOP_BPS = 60          # hard stop 60 bps under the entry
D_ARM_BPS = 90           # trailing stop arms at +90 bps
D_TRAIL_BPS = 90         # ... then trails the peak by 90 bps
D_MAX_HOLD = 400
D_MAX_TRADES = 1

# --- Guards ---
MAX_TRADES = 3            # at most 3 entries per window (1 dip + 2 bounce)
MAX_CONSEC_BOUNCE_LOSSES = 2   # stop after 2 consecutive BOUNCE losses
MAX_DAILY_LOSS_BPS = 200  # stop entering if equity drops under 98% of start
COOLDOWN_BARS = 60        # bars to wait after an exit
MAX_SLIP_BPS = 200        # max slippage vs the latest close
MIN_HISTORY = 62          # closes of history before any entry
NO_ENTRY_AFTER = 1320     # no entries after this bar

# --- Per-run state ---
closes = []               # closes, newest last
highs = []                # highs, newest last
lows = []                 # lows, newest last
rsi_hist = []             # RSI(14) per bar, None until enough closes
_avg_gain = None          # Wilder RSI running averages
_avg_loss = None
_last_t = None            # bar time of the last processed batch
_start_capital = None     # start capital from hello
_dip_trades = 0
_bounce_trades = 0
_consec_bounce_losses = 0
_cooldown_until = -1      # bar seq before which no new entry
_pending = None           # order decided this batch, awaiting its fill
_pos = None               # open position: kind, entry/stop/peak, bars held


def _fall_below(ref_px, px):
    """How far px sits below ref_px, in basis points (integer math)."""
    if ref_px <= 0:
        return 0
    return (ref_px - px) * BPS // ref_px


def _drop_from_high(lookback):
    """Fall from the max high over `lookback` bars to the previous close, bps.

    Positive for a fall: (max_high - prev) / max_high. The window ends
    just before the previous bar (highs[-lookback-1:-1]). Returns 0 when
    there is not enough history yet.
    """
    if len(highs) < lookback + 1:
        return 0
    hi = max(highs[-lookback - 1:-1])
    prev = closes[-2]
    if hi <= 0:
        return 0
    return (hi - prev) * BPS // hi


def _fall60():
    """Close-to-close fall over 60 bars: close 60 bars ago vs prev close."""
    if len(closes) < 62:
        return 0
    return _fall_below(closes[-62], closes[-2])


def _fall120():
    """Close-to-close fall over 120 bars: close 120 bars ago vs prev close."""
    if len(closes) < 122:
        return 0
    return _fall_below(closes[-122], closes[-2])


def _has_wick():
    """A long lower-wick bar printed in the last B_WICK_LOOKBACK bars."""
    n = len(closes)
    start = max(0, n - B_WICK_LOOKBACK)
    for j in range(start, n):
        hi = highs[j]
        lo = lows[j]
        c = closes[j]
        if hi <= lo or c <= 0:
            continue
        rng = (hi - lo) * BPS // c
        if rng < B_WICK_RANGE_BPS:
            continue
        # close in the top 40% of the range: lower wick >= 60% of it.
        if (c - lo) * 100 // (hi - lo) >= B_WICK_FRAC:
            return True
    return False


def _rsi_update():
    """Append the RSI(14) for the newest close (Wilder's smoothing)."""
    global _avg_gain, _avg_loss
    n = len(closes)
    if n < 2:
        rsi_hist.append(None)
        return
    gain = closes[-1] - closes[-2]
    if gain < 0:
        gain = 0
    loss = closes[-2] - closes[-1]
    if loss < 0:
        loss = 0
    if n == RSI_LEN + 1:
        # First value: plain mean of the first RSI_LEN changes.
        gs = 0
        ls = 0
        for i in range(1, RSI_LEN + 1):
            d = closes[i] - closes[i - 1]
            if d > 0:
                gs += d
            else:
                ls += -d
        _avg_gain = gs / RSI_LEN
        _avg_loss = ls / RSI_LEN
    elif n > RSI_LEN + 1:
        _avg_gain = (_avg_gain * (RSI_LEN - 1) + gain) / RSI_LEN
        _avg_loss = (_avg_loss * (RSI_LEN - 1) + loss) / RSI_LEN
    else:
        rsi_hist.append(None)
        return
    if _avg_loss == 0:
        rsi_hist.append(100.0)
    else:
        rs = _avg_gain / _avg_loss
        rsi_hist.append(100.0 - 100.0 / (1.0 + rs))


def _bounce_setup():
    """Check every Setup B condition on the latest bars.

    Returns (ok, stop_px): the prospective stop when the setup fires,
    else (False, 0). All fall measures anchor on the previous bar
    (idx -2), because the reversal bar itself lifts the close and
    would shrink the drop.
    """
    m = len(closes)
    if m < 2 or m < 721:
        return (False, 0)
    if rsi_hist[-1] is None or rsi_hist[-2] is None:
        return (False, 0)
    prev = closes[-2]
    cur = closes[-1]
    # 1. deep fall measured at the previous bar: 60 bars or 120 bars.
    fall60 = _fall60()
    fall120 = _fall120()
    if fall60 < B_DROP60_BPS and fall120 < B_DROP120_BPS:
        return (False, 0)
    # 2. capitulation: the fall from the 720-bar high must reach 550 bps.
    hi720 = max(highs[-721:-1])
    if _drop_from_high(720) < B_DROP720_BPS:
        return (False, 0)
    # 3. a long lower wick printed in the last 20 bars.
    if not _has_wick():
        return (False, 0)
    # 4. reversal: this bar closes up >= 60 bps and above the prev high.
    if (cur - prev) * BPS // prev < B_REVERSAL_BPS:
        return (False, 0)
    if cur <= highs[-2]:
        return (False, 0)
    # 5. RSI(14) turning up (no level filter).
    if rsi_hist[-1] <= rsi_hist[-2]:
        return (False, 0)
    # 6. reward/risk: room back to the 720-bar high at least twice the risk.
    cap_low = min(lows[-721:-1])
    stop0 = cap_low * (BPS - B_STOP_BELOW_LOW_BPS) // BPS
    lo_stop = cur * (BPS - B_STOP_LO_BPS) // BPS
    hi_stop = cur * (BPS - B_STOP_HI_BPS) // BPS
    stop = min(max(stop0, lo_stop), hi_stop)
    if hi720 - cur < 2 * (cur - stop):
        return (False, 0)
    return (True, stop)


def _dip_setup():
    """Check every Setup D condition on the latest bars.

    A range dip that is also a FAST fall: the close dropped hard from the
    60-bar high, is not a capitulation (720-bar fall under 550 bps),
    RSI(14) sits under 50, this bar closes up, and the close also fell
    at least 150 bps over the last 60 bars (the sharp-drop filter,
    close-to-close).
    """
    m = len(closes)
    if m < 2 or rsi_hist[-1] is None:
        return False
    prev = closes[-2]
    cur = closes[-1]
    # 1. fall from the highest high of the last 60 bars (prev bar).
    if _drop_from_high(60) < D_DROP60_BPS:
        return False
    # 2. not a capitulation: the 720-bar fall stays under 550 bps.
    if _drop_from_high(720) >= D_DROP720_MAX_BPS:
        return False
    # 3. RSI(14) under 50.
    if rsi_hist[-1] >= D_RSI_MAX:
        return False
    # 4. this bar closes up (any amount).
    if cur <= prev:
        return False
    # 5. sharp drop: close-to-close fall over the last 60 bars >= 150 bps.
    if _fall60() < D_SHARP60_BPS:
        return False
    return True


def _exit_is_win(exit_px):
    """Win/loss call for the exit price vs entry (cost-aware)."""
    if _pos is None:
        return False
    return exit_px > _pos["entry_px"]


def on_hello(state):
    """Capture the start capital from hello (never a hardcoded constant)."""
    global _start_capital
    _start_capital = int(state.hello["capital"]["quote_units"])


def on_batch(state, batch):
    """Decide the orders for this batch. Returns a list of orders (or [])."""
    global _last_t, _dip_trades, _bounce_trades, _consec_bounce_losses
    global _cooldown_until, _pending, _pos, _start_capital

    # Find this batch's CBBTC bar; skip the batch when it carries none.
    bar = None
    for b in batch.get("bars", []):
        if b.get("symbol") == SYMBOL:
            bar = b
            break
    if bar is None:
        return []
    # Duplicate-bar guard: skip a batch that carries no new bar.
    t = batch.get("t")
    if t is not None and t == _last_t:
        return []
    _last_t = t

    cur = int(bar["c"])
    hi = int(bar["h"])
    lo = int(bar["l"])
    closes.append(cur)
    highs.append(hi)
    lows.append(lo)
    _rsi_update()

    if _start_capital is None:
        _start_capital = int(state.hello["capital"]["quote_units"])

    holding = state.positions.get(SYMBOL)
    seq = state.seq

    # A pending buy either filled (position now held) or was rejected.
    if _pending is not None:
        if holding is not None:
            # Filled at this bar's open: entry_px is the fill bar's open
            # (the trailing-arm reference). The stop was already fixed
            # at decision time from the decision close.
            _pos = {
                "kind": _pending["kind"],
                "entry_px": int(bar["o"]),
                "stop_px": _pending["stop_px"],
                "peak_px": cur,
                "bars_held": 0,
                "armed": False,
                "stop_bps": _pending["stop_bps"],
                "arm_bps": _pending["arm_bps"],
                "trail_bps": _pending["trail_bps"],
                "max_hold": _pending["max_hold"],
            }
            if _pending["kind"] == "D":
                _dip_trades += 1
            else:
                _bounce_trades += 1
        _pending = None

    # --- Manage an open position: stop, trailing, max hold. ---
    if _pos is not None and holding is not None:
        _pos["bars_held"] += 1
        if cur > _pos["peak_px"]:
            _pos["peak_px"] = cur
        if not _pos["armed"] and cur >= _pos["entry_px"] * (BPS + _pos["arm_bps"]) // BPS:
            _pos["armed"] = True
        exit_px = None
        if cur <= _pos["stop_px"]:
            exit_px = _pos["stop_px"]
        elif _pos["armed"] and cur <= _pos["peak_px"] * (BPS - _pos["trail_bps"]) // BPS:
            exit_px = _pos["peak_px"] * (BPS - _pos["trail_bps"]) // BPS
        elif _pos["bars_held"] >= _pos["max_hold"]:
            exit_px = cur
        if exit_px is not None:
            won = _exit_is_win(exit_px)
            kind = _pos["kind"]
            _pos = None
            _cooldown_until = seq + COOLDOWN_BARS
            if kind == "B":
                if won:
                    _consec_bounce_losses = 0
                else:
                    _consec_bounce_losses += 1
            return [{"side": "sell", "symbol": SYMBOL,
                     "qty": holding.qty, "max_slippage_bps": MAX_SLIP_BPS}]
        return []

    if _pos is not None and holding is None:
        # Position vanished without our sell (should not happen): clear.
        _pos = None

    # --- No position: look for an entry. ---
    if len(closes) < MIN_HISTORY:
        return []
    if seq > NO_ENTRY_AFTER:
        return []
    if _dip_trades + _bounce_trades >= MAX_TRADES:
        return []
    if seq < _cooldown_until:
        return []
    if _consec_bounce_losses >= MAX_CONSEC_BOUNCE_LOSSES:
        return []
    if _start_capital is not None and state.equity < _start_capital * (BPS - MAX_DAILY_LOSS_BPS) // BPS:
        return []

    # Setup B first, then Setup D (mutually exclusive via the 720-bar fall).
    bounce_ok, bounce_stop = _bounce_setup()
    if bounce_ok and _bounce_trades < B_MAX_TRADES:
        _pending = {
            "kind": "B",
            "stop_px": bounce_stop,
            "stop_bps": B_STOP_HI_BPS,
            "arm_bps": B_ARM_BPS,
            "trail_bps": B_TRAIL_BPS,
            "max_hold": B_MAX_HOLD,
        }
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": state.cash,
                 "max_slippage_bps": MAX_SLIP_BPS}]

    if _dip_trades < D_MAX_TRADES and _dip_setup():
        _pending = {
            "kind": "D",
            "stop_px": cur * (BPS - D_STOP_BPS) // BPS,
            "stop_bps": D_STOP_BPS,
            "arm_bps": D_ARM_BPS,
            "trail_bps": D_TRAIL_BPS,
            "max_hold": D_MAX_HOLD,
        }
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": state.cash,
                 "max_slippage_bps": MAX_SLIP_BPS}]

    return []


if __name__ == "__main__":
    makefun_bars.run(on_batch, on_hello=on_hello)

