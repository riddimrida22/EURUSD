"""Ladder backtest: replays history with the system's REAL trade management.

Entry on signal close; stop 1.5x ATR; TP1 at +1.5R closes half and moves the
stop to breakeven; TP2 at +2.5R closes the rest. One position per asset at a
time (matching paper_bot). Outcomes in R-multiples:
  stopped before TP1 = -1.0R | TP1 then breakeven = +0.75R | TP1+TP2 = +2.0R
Fills are evaluated on completed bar highs/lows (stop checked first - the
conservative assumption). No spread/slippage/financing. History = whatever the
live feeds return (~720 1H bars, roughly 6 weeks). Small samples - read the
numbers as descriptive, not gospel.

Usage:  python backtest.py            # all assets
        python backtest.py "USD/JPY"  # one asset
"""
import sys

import data_providers as dp
from scanner import WATCHLIST, calculate_indicators, apply_strategies

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def ladder_backtest(df):
    trades = []
    pos = None
    for i in range(len(df)):
        row = df.iloc[i]
        if pos is not None:
            lo, hi = float(row["Low"]), float(row["High"])
            if lo <= pos["sl"]:
                r = -1.0 if not pos["half"] else pos["banked"] + 0.0  # BE exit on rest
                trades.append(r)
                pos = None
            else:
                if not pos["half"] and hi >= pos["tp1"]:
                    pos["half"] = True
                    pos["banked"] = 0.75  # half the position at +1.5R
                    pos["sl"] = pos["entry"]
                if pos and pos["half"] and hi >= pos["tp2"]:
                    trades.append(pos["banked"] + 1.25)  # rest at +2.5R
                    pos = None
        if pos is None and bool(row["Signal"]) and i < len(df) - 1:
            entry, atr = float(row["Close"]), float(row["ATR"])
            if atr and atr == atr:
                d = 1.5 * atr
                pos = {"entry": entry, "sl": entry - d, "tp1": entry + 1.5 * d,
                       "tp2": entry + 2.5 * d, "half": False, "banked": 0.0}
    open_r = None
    if pos is not None:  # mark the dangling position to market
        last = float(df["Close"].iloc[-1])
        frac = 0.5 if pos["half"] else 1.0
        open_r = pos.get("banked", 0.0) + frac * (last - pos["entry"]) / (pos["entry"] - (pos["entry"] - 1.5 * float(df["ATR"].iloc[-1])) )
    return trades, open_r


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    rows, all_r = [], []
    for pair, cfg in WATCHLIST.items():
        if only and pair != only:
            continue
        try:
            df, _d1d, src = dp.fetch_asset(cfg["ticker"])
            df = calculate_indicators(df)
            df = apply_strategies(df, pair)
            trades, open_r = ladder_backtest(df)
            n = len(trades)
            wins = sum(1 for r in trades if r > 0)
            tot = sum(trades)
            gp = sum(r for r in trades if r > 0)
            gl = -sum(r for r in trades if r < 0)
            pf = (gp / gl) if gl else float("inf") if gp else 0.0
            rows.append((pair, cfg["strategy"], n, wins, tot, pf, open_r, src))
            all_r.extend(trades)
        except Exception as e:
            rows.append((pair, cfg["strategy"], 0, 0, 0.0, 0.0, None, f"error: {e}"))

    print(f"{'Asset':<16}{'Strategy':<30}{'N':>3}{'Win%':>7}{'NetR':>7}{'PF':>6}  open")
    for p, strat, n, w, tot, pf, opn, src in rows:
        wr = f"{100*w/n:.0f}%" if n else "-"
        o = f"{opn:+.2f}R" if opn is not None else ""
        print(f"{p:<16}{strat:<30}{n:>3}{wr:>7}{tot:>+7.2f}{pf:>6.2f}  {o}")
    if all_r:
        n = len(all_r)
        wins = sum(1 for r in all_r if r > 0)
        print(f"\nALL: {n} trades | win rate {100*wins/n:.0f}% | net {sum(all_r):+.2f}R "
              f"| avg {sum(all_r)/n:+.3f}R/trade")


if __name__ == "__main__":
    main()
