# WideTrail-short — BTC 24h Race Bot

[![make.fun](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/Masrukhan25/btc-race-24h-20261118-1444-entry-acct1/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/Masrukhan25/btc-race-24h-20261118-1444-entry-acct1/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444)

A long-only trading bot for Coinbase Wrapped BTC (CBBTC) in the make.fun
24-hour BTC race. It starts with 1,000 USDC of paper money and trades minute
by minute, at most three full-size entries per window.

## Strategy

Two setups, both long-only, both with a hard stop and a trailing exit
(arm/trail 90/90 bps):

- **Setup B — capitulation bounce.** After a deep fall from the 720-bar
  high (>= 550 bps) with a long lower wick and a reversal bar, it buys when
  RSI(14) turns up and the room back to the 720-bar high is at least twice
  the risk.
- **Setup D — sharp-drop dip-buy.** A fast fall from the 60-bar high
  (>= 120 bps) that is not a capitulation (720-bar fall under 550 bps),
  RSI(14) under 50, this bar closing up, and a close-to-close fall of
  >= 150 bps over the last 60 bars.

The short lookback (60 bars, first entry from 62 closes) lets the bot catch
a dip that prints early in the window, before a 120-bar high even exists.

Guards: max 3 entries per window (1 dip + 2 bounce), no entry after 2
consecutive bounce losses, no entry when equity falls under 98% of start,
60-bar cooldown after every exit, no entries after bar 1320, max slippage
200 bps.

## Layout

- `main.py` — the bot (standard library only).
- `vendor/makefun_bars.py` — byte-identical copy of the kit's Python shim.

## Local grade

Dev window (kit): net PnL **+$0.31**, exactly 1 trade — buy bar 67
@ ~$63,646, sell bar 108 @ ~$63,717.
