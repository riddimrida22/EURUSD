"""Paper-trading bot: executes the scanner's signals with SIMULATED fills only.

This bot never talks to a broker. It keeps a JSON ledger (paper_state.json)
of simulated positions and applies the system's trade management on each run:
  - open on a fresh signal (risk PAPER_RISK_PCT% of PAPER_NAV, stop 1.5x ATR)
  - close half at TP1 (+1.5R) and move the stop to breakeven
  - close the rest at TP2 (+2.5R) or at the stop
Fills are evaluated against completed 1H bars (high/low touch), so results are
approximate — no spread, slippage or financing. It's a strategy scoreboard,
not a broker simulation.

Run it on the same schedule as the scanner:  python paper_bot.py
Env: PAPER_NAV (default 25000), PAPER_RISK_PCT (default 1.0),
     PAPER_STATE_FILE (default ./paper_state.json)
"""
import json
import os
import sys

import pandas as pd

import scanner
from scanner import WATCHLIST, fetch_data, calculate_indicators, apply_strategies, send_alert

NAV = float(os.environ.get("PAPER_NAV", "25000"))
RISK_PCT = float(os.environ.get("PAPER_RISK_PCT", "1.0"))
STATE_FILE = os.environ.get(
    "PAPER_STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_state.json"))


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"realized_pl": 0.0, "open": [], "closed": []}


def save_state(state):
    state["closed"] = state["closed"][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, default=str)


def _pl_usd(pair, units, entry, exit_price):
    """P/L converted to USD. Quote-currency P/L divided by the rate for JPY
    quotes; treated as USD for USD-quoted assets (approximation for GBP quote)."""
    quote_pl = units * (exit_price - entry)
    if "JPY" in pair:
        return quote_pl / exit_price
    if pair.startswith("EUR/GBP"):
        return quote_pl / 0.78  # rough GBP->USD; fine for a paper scoreboard
    return quote_pl


def close_portion(state, pos, portion, price, reason):
    units = pos["units_open"] * portion
    pl = _pl_usd(pos["pair"], units, pos["entry"], price)
    pos["units_open"] -= units
    state["realized_pl"] += pl
    state["closed"].append({
        "pair": pos["pair"], "units": round(units), "entry": pos["entry"],
        "exit": price, "reason": reason, "pl_usd": round(pl, 2),
        "closed_at": str(pd.Timestamp.utcnow())})
    send_alert(f"{reason} {pos['pair']}: {round(units):,} u @ {price:.5g} -> {pl:+.2f} USD\n"
               f"Session realized P/L: {state['realized_pl']:+.2f} USD",
               title=f"PAPER BOT {reason} {pos['pair']}")
    print(f"paper: {reason} {pos['pair']} {round(units):,}u @ {price} pl={pl:+.2f}")


def manage_position(state, pos, df_1h):
    """Walk completed bars newer than the last processed one."""
    bars = df_1h[df_1h.index > pd.Timestamp(pos["last_bar"])]
    for ts, bar in bars.iterrows():
        lo, hi = float(bar["Low"]), float(bar["High"])
        if lo <= pos["sl"]:
            close_portion(state, pos, 1.0, pos["sl"], "STOPPED")
            return False
        if not pos["half_closed"] and hi >= pos["tp1"]:
            close_portion(state, pos, 0.5, pos["tp1"], "TP1")
            pos["half_closed"] = True
            pos["sl"] = pos["entry"]  # breakeven per system rules
        if pos["half_closed"] and hi >= pos["tp2"]:
            close_portion(state, pos, 1.0, pos["tp2"], "TP2")
            return False
        pos["last_bar"] = str(ts)
    return True


def maybe_open(state, pair, config, df_1h):
    if any(p["pair"] == pair for p in state["open"]):
        return
    recent = df_1h.tail(2)
    triggers = recent.index[recent["Signal"]]
    if len(triggers) == 0:
        return
    sig_id = f"{pair}_{triggers[-1].isoformat()}"
    if any(p.get("sig_id") == sig_id for p in state["open"]) or \
       any(c.get("sig_id") == sig_id for c in state["closed"][-50:]):
        return
    latest = df_1h.iloc[-1]
    entry, atr = float(latest["Close"]), float(latest["ATR"])
    if not atr or atr != atr:
        return
    stop_d = 1.5 * atr
    risk = NAV * RISK_PCT / 100
    # USD-quoted assets (EUR/USD, ETFs): P/L is already USD -> units = risk/stop.
    # Other quotes (JPY, CAD): convert through the pair's own rate.
    usd_quoted = pair == "EUR/USD" or config["strategy"].startswith("Equity")
    units = risk / stop_d if usd_quoted else risk * entry / stop_d
    pos = {"pair": pair, "sig_id": sig_id, "entry": entry,
           "units_open": units, "units_initial": units,
           "sl": entry - stop_d, "tp1": entry + 1.5 * stop_d, "tp2": entry + 2.5 * stop_d,
           "half_closed": False, "opened_at": str(pd.Timestamp.utcnow()),
           "last_bar": str(df_1h.index[-1])}
    state["open"].append(pos)
    send_alert(f"Action: BUY (paper)\nEntry: {entry:.5g}\nStop: {pos['sl']:.5g}\n"
               f"TP1: {pos['tp1']:.5g} | TP2: {pos['tp2']:.5g}\n"
               f"Size: {round(units):,} units (risk ${risk:.0f})",
               title=f"PAPER BOT BUY {pair}")
    print(f"paper: OPEN {pair} {round(units):,}u @ {entry}")


def main():
    state = load_state()
    failures = 0
    for pair, config in WATCHLIST.items():
        try:
            df_1h, _df_1d, _src = fetch_data(config["ticker"])
            if df_1h.empty:
                continue
            df_1h = calculate_indicators(df_1h)
            df_1h = apply_strategies(df_1h, pair)
            state["open"] = [p for p in state["open"]
                             if p["pair"] != pair or manage_position(state, p, df_1h)]
            maybe_open(state, pair, config, df_1h)
        except Exception as e:
            failures += 1
            print(f"paper: {pair} failed: {e}")
    save_state(state)
    open_desc = ", ".join(f"{p['pair']}@{p['entry']:.5g}" for p in state["open"]) or "none"
    print(f"paper: realized P/L ${state['realized_pl']:+.2f} | open: {open_desc}")
    sys.exit(1 if failures == len(WATCHLIST) else 0)


if __name__ == "__main__":
    main()
