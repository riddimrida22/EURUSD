"""Headless FX scanner for scheduled runs (GitHub Actions / Task Scheduler / cron).

Runs the same per-pair strategies as app.py, but with no Streamlit UI —
if a signal fired on either of the two most recent 1H bars, it sends a
push alert via ntfy.sh and exits. No account or token needed: the secret
topic name is the channel (override with the NTFY_TOPIC env var).
Subscribe in the ntfy app or at https://ntfy.sh/<topic> to receive alerts.

Alerts are deduplicated across runs via a small JSON state file
(alert_state.json next to this script, overridable with FX_STATE_FILE) —
each signal is keyed by pair + triggering bar timestamp, so a signal that
stays active for hours alerts exactly once.

Usage:  python scanner.py
"""
import json
import os
import sys

import pandas as pd
import requests
import yfinance as yf

import data_providers as dp

# Windows consoles default to cp1252, which can't print the emoji in alerts
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def _eq(sym):
    return {"ticker": sym, "strategy": "Equity Pullback (Buy the Dip)"}

WATCHLISTS = {
    "FX Majors": {
        "EUR/USD": {"ticker": "EURUSD=X", "strategy": "Trend Following"},
        "EUR/GBP": {"ticker": "EURGBP=X", "strategy": "Mean Reversion"},
        "GBP/JPY": {"ticker": "GBPJPY=X", "strategy": "Trend Following"},
        "EUR/JPY": {"ticker": "EURJPY=X", "strategy": "Trend Following"},
        "USD/JPY": {"ticker": "USDJPY=X", "strategy": "Trend Following"},
        "USD/CAD": {"ticker": "USDCAD=X", "strategy": "Momentum Breakout"},
    },
    "Index ETFs": {"SPY": _eq("SPY"), "QQQ": _eq("QQQ")},
    "Magnificent 7": {s: _eq(s) for s in ("AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA")},
    "Hyperscalers": {s: _eq(s) for s in ("MSFT", "AMZN", "GOOGL", "META", "ORCL", "CRWV")},
    "Memory (semis tell)": {s: _eq(s) for s in ("MU", "SNDK")},
    "AI Picks & Shovels": {s: _eq(s) for s in ("NVDA", "AMD", "AVGO", "TSM", "ASML", "ANET", "VRT", "SMCI")},
}

# Flat, de-duplicated: overlapping symbols are scanned and alerted only once
WATCHLIST = {}
for _g_assets in WATCHLISTS.values():
    for _k, _v in _g_assets.items():
        WATCHLIST.setdefault(_k, _v)

STATE_FILE = os.environ.get(
    "FX_STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert_state.json"))


def load_state():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_state(sent):
    # keep the file bounded; old signal ids never recur
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(sent)[-500:], f)

NAV = float(os.environ.get("FX_NAV", "10000"))
MIN_RISK = float(os.environ.get("FX_MIN_RISK", "0.5"))
MAX_RISK = float(os.environ.get("FX_MAX_RISK", "2.0"))


DEFAULT_NTFY_TOPIC = ""  # public repo: set the topic via NTFY_TOPIC secret/env only


def send_alert(message, title="FX Terminal Signal"):
    topic = os.environ.get("NTFY_TOPIC", DEFAULT_NTFY_TOPIC).strip()
    if not topic:
        print("NTFY_TOPIC empty; printing alert instead:\n" + message)
        return
    # HTTP headers are latin-1 only; emoji comes from the Tags instead
    safe_title = title.encode("latin-1", "ignore").decode("latin-1").strip()
    resp = requests.post(f"https://ntfy.sh/{topic}",
                         data=message.encode("utf-8"),
                         headers={"Title": safe_title, "Priority": "high", "Tags": "rotating_light"},
                         timeout=30)
    print(f"ntfy response: {resp.status_code}")


def fetch_data(ticker):
    """OANDA live candles for FX when OANDA_TOKEN is set; yfinance otherwise."""
    df_1h, df_1d, source = dp.fetch_asset(ticker)
    return df_1h, df_1d, source


def calculate_indicators(df):
    df['SMA_200'] = df['Close'].rolling(window=200).mean()
    df['H-L'] = df['High'] - df['Low']
    df['H-C'] = abs(df['High'] - df['Close'].shift(1))
    df['L-C'] = abs(df['Low'] - df['Close'].shift(1))
    df['TR'] = df[['H-L', 'H-C', 'L-C']].max(axis=1)
    df['ATR'] = df['TR'].rolling(14).mean()

    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    df['RSI'] = 100 - (100 / (1 + (gain / loss)))
    return df


def apply_strategies(df, pair):
    df['Signal'] = False

    if pair == "EUR/GBP":
        df['Swing_Low'] = df['Low'][(df['Low'] == df['Low'].rolling(window=11, center=True).min())].ffill()
        df['Prev_Open'], df['Prev_Close'] = df['Open'].shift(1), df['Close'].shift(1)
        df['Bullish_Engulfing'] = ((df['Prev_Close'] < df['Prev_Open']) & (df['Close'] > df['Open']) &
                                   (df['Close'] > df['Prev_Open']) & (df['Open'] < df['Prev_Close']))
        df['Signal'] = (df['Bullish_Engulfing'] & (abs(df['Low'] - df['Swing_Low']) <= 0.0015))

    elif pair in ("GBP/JPY", "USD/JPY", "EUR/USD", "EUR/JPY"):
        df['SMA_20'], df['SMA_50'] = df['Close'].rolling(20).mean(), df['Close'].rolling(50).mean()
        df['Signal'] = (df['SMA_20'] > df['SMA_50']) & (df['SMA_20'].shift(1) <= df['SMA_50'].shift(1))

    elif pair == "USD/CAD":
        df['Rolling_High_20'] = df['High'].rolling(20).max().shift(1)
        # First-bar-only: alert on the breakout bar, not every bar above the high
        breakout = df['Close'] > df['Rolling_High_20']
        df['Signal'] = breakout & (~breakout.shift(1, fill_value=False))

    elif "/" not in pair:  # all equities run the pullback strategy
        df['SMA_20'] = df['Close'].rolling(window=20).mean()
        df['Std_Dev'] = df['Close'].rolling(window=20).std()
        df['Lower_Band'] = df['SMA_20'] - (df['Std_Dev'] * 2)
        dip = (df['Close'] < df['Lower_Band']) & (df['RSI'] < 30) & (df['Close'] > df['SMA_200'])
        # First-bar-only: an oversold dip can persist for hours; trigger once
        df['Signal'] = dip & (~dip.shift(1, fill_value=False))

    return df


def scan_pair(pair, config, sent):
    df_1h, df_1d, source = fetch_data(config['ticker'])
    if df_1h.empty or df_1d.empty:
        print(f"{pair}: no data returned, skipping")
        return

    df_1h = calculate_indicators(df_1h)
    df_1h = apply_strategies(df_1h, pair)
    latest = df_1h.iloc[-1]

    px = ".2f" if config['strategy'].startswith("Equity") else ".4f"
    daily_sma_50 = df_1d['Close'].rolling(50).mean().iloc[-1]
    daily_trend = "Bullish" if df_1d['Close'].iloc[-1] > daily_sma_50 else "Bearish"
    print(f"{pair}: close={latest['Close']:{px}} daily_trend={daily_trend} "
          f"signal_last2={bool(df_1h['Signal'].tail(2).any())} source={source}")

    recent = df_1h.tail(2)
    trigger_bars = recent.index[recent['Signal']]
    if len(trigger_bars) == 0:
        return

    sig_id = f"{pair}_{trigger_bars[-1].isoformat()}"
    if sig_id in sent:
        print(f"{pair}: signal {sig_id} already alerted, skipping")
        return
    sent.add(sig_id)

    confidence = 1
    if latest['RSI'] < 55:
        confidence += 1
    if latest['Close'] > latest['SMA_200']:
        confidence += 1

    if confidence == 1:
        risk_pct = MIN_RISK
    elif confidence == 2:
        risk_pct = (MIN_RISK + MAX_RISK) / 2
    else:
        risk_pct = MAX_RISK

    atr = latest['ATR']
    stop_loss = latest['Close'] - (atr * 1.5)
    sl_dist = latest['Close'] - stop_loss
    tp1 = latest['Close'] + (sl_dist * 1.5)
    tp2 = latest['Close'] + (sl_dist * 2.5)
    risk_amount = NAV * (risk_pct / 100)

    msg = (f"Action: BUY (go long)\n"
           f"Confidence: {confidence}/3\n"
           f"Daily Trend: {daily_trend}\n"
           f"Entry: {latest['Close']:{px}}\n"
           f"Stop: {stop_loss:{px}} (below entry)\n"
           f"TP1: {tp1:{px}} | TP2: {tp2:{px}}\n"
           f"Risk: {risk_pct}% (${risk_amount:,.2f})")
    send_alert(msg, title=f"BUY {pair} ({config['strategy']})")


def main():
    sent = load_state()
    failures = 0
    for pair, config in WATCHLIST.items():
        try:
            scan_pair(pair, config, sent)
        except Exception as e:
            failures += 1
            print(f"{pair}: scan failed: {e}")
    save_state(sent)
    sys.exit(1 if failures == len(WATCHLIST) else 0)


if __name__ == "__main__":
    main()
