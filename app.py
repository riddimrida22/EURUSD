import json
import os

import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests

import data_providers as dp

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

st.set_page_config(page_title="Pro FX & ETF Terminal", page_icon="🏦", layout="wide")
st.title("Pro Watchlist & Institutional Scanner")

# Make Streamlit Cloud secrets visible to the data layer (env wins if both set)
try:
    for _k in ("OANDA_TOKEN", "OANDA_ENV", "WEBULL_APP_KEY", "WEBULL_APP_SECRET", "NTFY_TOPIC", "FX_LIVE_DEFAULT"):
        if _k not in os.environ and _k in st.secrets:
            os.environ[_k] = str(st.secrets[_k])
except Exception:
    pass  # no secrets file locally

# ---------------------------------------------------------
# 0. SIDEBAR: ACCOUNT, RISK, & TELEGRAM SETTINGS
# ---------------------------------------------------------
st.sidebar.header("📡 Data")
# Persist the toggle in the URL (?live=1) so refreshes, redeploys and phone
# tab suspensions don't reset it; FX_LIVE_DEFAULT=1 env/secret flips the default.
_qp_live = st.query_params.get("live")
_default_live = (_qp_live == "1") if _qp_live is not None else os.environ.get("FX_LIVE_DEFAULT", "0") == "1"
live_mode = st.sidebar.toggle(
    "Live data (OANDA / Webull)",
    value=_default_live,
    help="Off = delayed Yahoo data. On = live OANDA FX candles and real-time "
         "Webull ETF prices where credentials are available. Your choice is "
         "kept in the URL - bookmark it and it sticks.")
if _qp_live != ("1" if live_mode else "0"):
    st.query_params["live"] = "1" if live_mode else "0"

_REFRESH_MS = {"Off": 0, "30s": 30_000, "60s": 60_000, "5m": 300_000}
refresh_choice = st.sidebar.selectbox("Auto-refresh", list(_REFRESH_MS), index=2,
                                      help="Reruns the page on a timer so prices tick. Charts/scans "
                                           "refetch at most every 5 min; live prices every 20-30s.")
if st_autorefresh and _REFRESH_MS[refresh_choice]:
    st_autorefresh(interval=_REFRESH_MS[refresh_choice], key="auto_refresh")

# Spot FX has no centralized volume (Yahoo reports 0), so each pair maps a CME
# currency future whose volume serves as an activity proxy for the volume pane.
# Equities (vol_proxy None) have real exchange volume and use their own.
def _eq(sym):
    return {"ticker": sym, "strategy": "Equity Pullback (Buy the Dip)", "vol_proxy": None}

WATCHLISTS = {
    "FX Majors": {
        "EUR/USD": {"ticker": "EURUSD=X", "strategy": "Trend Following", "vol_proxy": "6E=F"},
        "EUR/GBP": {"ticker": "EURGBP=X", "strategy": "Mean Reversion", "vol_proxy": "6E=F"},
        "GBP/JPY": {"ticker": "GBPJPY=X", "strategy": "Trend Following", "vol_proxy": "6J=F"},
        "EUR/JPY": {"ticker": "EURJPY=X", "strategy": "Trend Following", "vol_proxy": "6J=F"},
        "USD/JPY": {"ticker": "USDJPY=X", "strategy": "Trend Following", "vol_proxy": "6J=F"},
        "USD/CAD": {"ticker": "USDCAD=X", "strategy": "Momentum Breakout", "vol_proxy": "6C=F"},
    },
    "Index ETFs": {"SPY (S&P 500)": _eq("SPY"), "QQQ (Nasdaq)": _eq("QQQ")},
    "Magnificent 7": {s: _eq(s) for s in ("AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA")},
    "Hyperscalers": {s: _eq(s) for s in ("MSFT", "AMZN", "GOOGL", "META", "ORCL", "CRWV")},
    "Memory (semis tell)": {s: _eq(s) for s in ("MU", "SNDK")},
    "AI Picks & Shovels": {s: _eq(s) for s in ("NVDA", "AMD", "AVGO", "TSM", "ASML", "ANET", "VRT", "SMCI")},
}

# Flat, de-duplicated view: symbols appearing in several groups (NVDA, the
# cloud giants) are fetched, scanned and alerted only once.
WATCHLIST = {}
for _g_assets in WATCHLISTS.values():
    for _k, _v in _g_assets.items():
        WATCHLIST.setdefault(_k, _v)

st.sidebar.header("📊 Chart Settings")
chart_tf = st.sidebar.selectbox("Timeframe", ["5m", "15m", "1H", "4H", "1D", "1W"], index=2)
chart_bars = st.sidebar.slider("Bars shown", 50, 400, 80, step=10)
overlays = st.sidebar.multiselect(
    "Indicator overlays",
    ["SMA 20", "SMA 50", "SMA 200", "EMA 9", "EMA 21", "VWAP (day)",
     "Bollinger (20,2)", "Keltner (20, 2xATR)", "Donchian 20", "Ichimoku Cloud"],
    default=[])
show_volume = st.sidebar.checkbox("Volume pane", value=True)
show_rsi = st.sidebar.checkbox("RSI pane", value=True)
show_macd = st.sidebar.checkbox("MACD pane (12,26,9)", value=False)
if chart_tf != "1H":
    st.sidebar.caption("Signals, metrics and backtests always run on 1H — other timeframes are chart views.")

st.sidebar.header("⚙️ Risk Management")
nav = st.sidebar.number_input("Account Balance ($)", value=10000, step=1000)
min_risk = st.sidebar.slider("Base Risk (1-Star Setup) %", 0.1, 2.0, 0.5, step=0.1)
max_risk = st.sidebar.slider("Max Risk (3-Star Setup) %", 0.5, 5.0, 2.0, step=0.1)

# ntfy.sh push alerts: no account needed — the topic name IS the channel.
# Subscribe in the ntfy app (or https://ntfy.sh/<topic>) to receive alerts.
DEFAULT_NTFY_TOPIC = ""  # public repo: set the topic via NTFY_TOPIC secret/env only

st.sidebar.header("📱 Push Alerts (ntfy.sh)")
st.sidebar.caption("Subscribe to this topic in the free ntfy app to get pushes. Clear the field to disable.")
ntfy_topic = st.sidebar.text_input("ntfy topic", value=os.environ.get("NTFY_TOPIC", DEFAULT_NTFY_TOPIC))

if "alerts_sent" not in st.session_state:
    st.session_state.alerts_sent = {}

def send_alert(message, title="FX Terminal Signal"):
    if ntfy_topic:
        try:
            # HTTP headers are latin-1 only; emoji comes from the Tags instead
            safe_title = title.encode("latin-1", "ignore").decode("latin-1").strip()
            requests.post(f"https://ntfy.sh/{ntfy_topic.strip()}",
                          data=message.encode("utf-8"),
                          headers={"Title": safe_title, "Priority": "high", "Tags": "rotating_light"},
                          timeout=10)
        except Exception:
            pass # Fail silently if network error

# ---------------------------------------------------------
# 1. WATCHLIST & DATA FETCHING (MTF)
# ---------------------------------------------------------
# (WATCHLIST is defined above the sidebar so the instrument selector can use it)

@st.cache_data(ttl=300)
def fetch_data(ticker, live):
    # live=True: OANDA candles for FX when a token is available; else yfinance
    return dp.fetch_asset(ticker, allow_live=live)

@st.cache_data(ttl=30)
def live_price(ticker):
    # Real-time ETF last price via Webull OpenAPI (None if unavailable)
    return dp.live_etf_price(ticker)

@st.cache_data(ttl=20)
def live_fx(ticker):
    # Latest OANDA 1-minute close (None without token / non-FX)
    return dp.live_fx_price(ticker)

@st.cache_data(ttl=300)
def fetch_chart_data(ticker, tf, bars, live):
    # OHLCV at the user-selected display timeframe
    return dp.fetch_chart(ticker, tf, bars, allow_live=live)

@st.cache_data(ttl=300)
def fetch_proxy_volume(ticker):
    df = yf.download(ticker, period="1mo", interval="1h", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[df['Volume'] > 0]
    return df[['Close', 'Volume']]

# ---------------------------------------------------------
# 2. INDICATORS & DYNAMIC SIZING
# ---------------------------------------------------------
def calculate_indicators(df):
    df['SMA_200'] = df['Close'].rolling(window=200).mean() # Long-term trend
    df['H-L'], df['H-C'], df['L-C'] = df['High'] - df['Low'], abs(df['High'] - df['Close'].shift(1)), abs(df['Low'] - df['Close'].shift(1))
    df['TR'] = df[['H-L', 'H-C', 'L-C']].max(axis=1)
    df['ATR'] = df['TR'].rolling(14).mean()

    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
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
        # First-bar-only: alert on the breakout bar itself, not every bar that
        # stays above the Donchian high afterwards.
        breakout = df['Close'] > df['Rolling_High_20']
        df['Signal'] = breakout & (~breakout.shift(1, fill_value=False))

    elif "/" not in pair:  # all equities run the pullback strategy
        df['SMA_20'] = df['Close'].rolling(window=20).mean()
        df['Std_Dev'] = df['Close'].rolling(window=20).std()
        df['Lower_Band'] = df['SMA_20'] - (df['Std_Dev'] * 2)
        df['RSI_Oversold'] = df['RSI'] < 30
        dip = (df['Close'] < df['Lower_Band']) & df['RSI_Oversold'] & (df['Close'] > df['SMA_200'])
        # First-bar-only: an oversold dip can persist for hours; trigger once
        df['Signal'] = dip & (~dip.shift(1, fill_value=False))

    return df

# ---------------------------------------------------------
# 3. PROFIT FACTOR BACKTESTER
# ---------------------------------------------------------
def run_backtest(df):
    df['Exit_Price'] = df['Close'].shift(-24)
    history = df[(df['Signal'] == True) & (df['Exit_Price'].notna())].copy()

    total = len(history)
    if total == 0: return 0, 0.0, 0.0

    history['Pip_Gain'] = history['Exit_Price'] - history['Close']
    wins = history[history['Pip_Gain'] > 0]['Pip_Gain'].sum()
    losses = abs(history[history['Pip_Gain'] < 0]['Pip_Gain'].sum())

    win_rate = (len(history[history['Pip_Gain'] > 0]) / total) * 100
    profit_factor = (wins / losses) if losses > 0 else float('inf')

    return total, win_rate, profit_factor

# ---------------------------------------------------------
# 4. DASHBOARD RENDER
# ---------------------------------------------------------
def render_asset(pair, config):
    is_etf = config['vol_proxy'] is None
    px = ".2f" if is_etf else ".4f"  # ETFs quote in cents, FX in pips
    df_1h, df_1d, data_source = fetch_data(config['ticker'], live_mode)
    df_1h = calculate_indicators(df_1h)
    df_1h = apply_strategies(df_1h, pair)
    latest = df_1h.iloc[-1]

    # 1. Multi-Timeframe Filter
    daily_sma_50 = df_1d['Close'].rolling(50).mean().iloc[-1]
    daily_trend = "🟢 Bullish" if df_1d['Close'].iloc[-1] > daily_sma_50 else "🔴 Bearish"

    st.subheader(f"{pair} — {config['strategy']}")

    # Live price: Webull real-time for ETFs; OANDA's forming candle is
    # already live for FX. Bars (and signals) stay on the fetched data.
    current_price, price_note = latest['Close'], data_source
    if live_mode:
        lp = live_price(config['ticker']) if is_etf else live_fx(config['ticker'])
        if lp:
            current_price, price_note = lp, "Webull (live)" if is_etf else "OANDA (live tick)"
    st.caption(f"Data: {data_source}" + (f" · Price: {price_note}" if price_note != data_source else ""))

    # 2. Advanced Backtest Metrics
    total_trades, win_rate, profit_factor = run_backtest(df_1h)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Current Price", f"{current_price:{px}}")
    col2.metric("Daily Macro Trend", daily_trend)
    col3.metric("Historical Win Rate (1mo)", f"{win_rate:.1f}%")
    col4.metric("Profit Factor", f"{profit_factor:.2f}",
                delta="Profitable" if profit_factor > 1.0 else "Unprofitable", delta_color="normal")

    # 3. Dynamic Risk & Advanced Targets
    recent_signals = df_1h['Signal'].tail(2)
    if recent_signals.any():
        # Calculate Confidence Score (Confluence)
        confidence = 1
        if latest['RSI'] < 55: confidence += 1 # RSI is favorable/not overbought
        if latest['Close'] > latest['SMA_200']: confidence += 1 # Trade aligns with 1H trend

        # Scale Risk Dynamically
        if confidence == 1: current_risk_pct = min_risk
        elif confidence == 2: current_risk_pct = (min_risk + max_risk) / 2
        else: current_risk_pct = max_risk

        atr = latest['ATR']
        stop_loss = latest['Close'] - (atr * 1.5)
        sl_dist = latest['Close'] - stop_loss

        # Advanced Trade Management Targets
        breakeven_target = latest['Close'] + sl_dist
        tp1 = latest['Close'] + (sl_dist * 1.5)
        tp2 = latest['Close'] + (sl_dist * 2.5)

        # Exact Sizing Math
        risk_amount = nav * (current_risk_pct / 100)
        sl_pct = sl_dist / latest['Close']
        notional_size = risk_amount / sl_pct

        st.warning(f"""
        ### 🚨 BUY SIGNAL (long): {pair} | ⭐ {'⭐' * (confidence-1)} ({confidence}/3 Confidence)

        **Advanced Trade Management:**
        * **Entry Price:** {latest['Close']:{px}}
        * **Stop Loss:** {stop_loss:{px}}
        * **Breakeven Target:** {breakeven_target:{px}} *(Move SL to entry here)*
        * **Take Profit 1:** {tp1:{px}} *(Close 50% of position)*
        * **Take Profit 2:** {tp2:{px}} *(Close remainder)*

        **Dynamic Sizing (Risking {current_risk_pct}% / ${risk_amount:,.2f}):**
        Open a total position size of **${notional_size:,.2f}**.
        """)

        # Send Telegram Alert
        sig_id = f"{pair}_{latest.name}"
        if sig_id not in st.session_state.alerts_sent:
            msg = f"Action: BUY (go long)\nConfidence: {confidence}/3\nEntry: {latest['Close']:{px}}\nRisk: {current_risk_pct}%\nTP1: {tp1:{px}}"
            send_alert(msg, title=f"BUY {pair}")
            st.session_state.alerts_sent[sig_id] = True

    # 4. Chart Render (user-configurable timeframe / lookback / overlays / panes)
    if chart_tf == "1H":
        full_df, chart_src = df_1h, data_source
    else:
        raw_df, chart_src = fetch_chart_data(config['ticker'], chart_tf, chart_bars, live_mode)
        full_df = calculate_indicators(raw_df.copy()) if not raw_df.empty else raw_df
    if full_df.empty:
        st.info(f"No {chart_tf} data available for {pair}.")
        return
    chart_df = full_df.tail(chart_bars)

    pane_rows = [("price", 0.60)]
    has_own_vol = 'Volume' in chart_df.columns and chart_df['Volume'].sum() > 0
    vol_ok = show_volume and (has_own_vol or chart_tf == "1H")
    if vol_ok: pane_rows.append(("volume", 0.16))
    if show_rsi: pane_rows.append(("rsi", 0.20))
    if show_macd: pane_rows.append(("macd", 0.20))
    heights = [h for _, h in pane_rows]
    heights[0] = round(1 - sum(heights[1:]), 2)
    row_of = {name: i + 1 for i, (name, _) in enumerate(pane_rows)}

    fig = make_subplots(rows=len(pane_rows), cols=1, shared_xaxes=True, row_heights=heights, vertical_spacing=0.03)
    fig.add_trace(go.Candlestick(x=chart_df.index, open=chart_df['Open'], high=chart_df['High'], low=chart_df['Low'], close=chart_df['Close'], name="Price"), row=1, col=1)

    # User-selected overlays, computed on the full displayed-timeframe history
    close_full = full_df['Close']
    OVERLAY_SPECS = {
        "SMA 20":  (lambda c: c.rolling(20).mean(),  dict(color='#FF9800')),
        "SMA 50":  (lambda c: c.rolling(50).mean(),  dict(color='#2196F3')),
        "SMA 200": (lambda c: c.rolling(200).mean(), dict(color='#E91E63')),
        "EMA 9":   (lambda c: c.ewm(span=9, adjust=False).mean(), dict(color='#4DD0E1')),
        "EMA 21":  (lambda c: c.ewm(span=21, adjust=False).mean(), dict(color='#FFEB3B')),
    }
    for name in overlays:
        if name in OVERLAY_SPECS:
            calc, style = OVERLAY_SPECS[name]
            fig.add_trace(go.Scatter(x=chart_df.index, y=calc(close_full).tail(chart_bars),
                                     mode='lines', line=style, name=name), row=1, col=1)
    if "Bollinger (20,2)" in overlays:
        bb_mid = close_full.rolling(20).mean()
        bb_sd = close_full.rolling(20).std()
        for band in (bb_mid + 2 * bb_sd, bb_mid - 2 * bb_sd):
            fig.add_trace(go.Scatter(x=chart_df.index, y=band.tail(chart_bars), mode='lines',
                                     line=dict(color='#78909C', dash='dot'), name='BB'), row=1, col=1)
    if "Keltner (20, 2xATR)" in overlays:
        k_mid = close_full.ewm(span=20, adjust=False).mean()
        k_tr = pd.concat([full_df['High'] - full_df['Low'],
                          (full_df['High'] - close_full.shift(1)).abs(),
                          (full_df['Low'] - close_full.shift(1)).abs()], axis=1).max(axis=1)
        k_atr = k_tr.rolling(20).mean()
        fig.add_trace(go.Scatter(x=chart_df.index, y=k_mid.tail(chart_bars), mode='lines',
                                 line=dict(color='#8D6E63'), name='Keltner mid'), row=1, col=1)
        for band in (k_mid + 2 * k_atr, k_mid - 2 * k_atr):
            fig.add_trace(go.Scatter(x=chart_df.index, y=band.tail(chart_bars), mode='lines',
                                     line=dict(color='#8D6E63', dash='dot'), name='Keltner'), row=1, col=1)
    if "Donchian 20" in overlays:
        for series, nm in ((full_df['High'].rolling(20).max(), 'Donchian high'),
                           (full_df['Low'].rolling(20).min(), 'Donchian low')):
            fig.add_trace(go.Scatter(x=chart_df.index, y=series.tail(chart_bars), mode='lines',
                                     line=dict(color='#AB47BC', dash='dash'), name=nm), row=1, col=1)
    if "VWAP (day)" in overlays and 'Volume' in full_df.columns and full_df['Volume'].sum() > 0 and chart_tf in ("5m", "15m", "1H", "4H"):
        _tp = (full_df['High'] + full_df['Low'] + close_full) / 3
        _day = full_df.index.date
        vwap = (_tp * full_df['Volume']).groupby(_day).cumsum() / full_df['Volume'].groupby(_day).cumsum()
        fig.add_trace(go.Scatter(x=chart_df.index, y=vwap.tail(chart_bars), mode='lines',
                                 line=dict(color='#26C6DA', dash='dashdot'), name='VWAP'), row=1, col=1)
    if "Ichimoku Cloud" in overlays:
        _hi, _lo = full_df['High'], full_df['Low']
        tenkan = (_hi.rolling(9).max() + _lo.rolling(9).min()) / 2
        kijun = (_hi.rolling(26).max() + _lo.rolling(26).min()) / 2
        span_a = ((tenkan + kijun) / 2).shift(26)
        span_b = ((_hi.rolling(52).max() + _lo.rolling(52).min()) / 2).shift(26)
        chikou = close_full.shift(-26)
        fig.add_trace(go.Scatter(x=chart_df.index, y=span_b.tail(chart_bars), mode='lines',
                                 line=dict(color='rgba(239,83,80,0.5)', width=1), name='Senkou B'), row=1, col=1)
        fig.add_trace(go.Scatter(x=chart_df.index, y=span_a.tail(chart_bars), mode='lines',
                                 line=dict(color='rgba(38,166,154,0.5)', width=1), name='Senkou A',
                                 fill='tonexty', fillcolor='rgba(120,144,156,0.18)'), row=1, col=1)
        fig.add_trace(go.Scatter(x=chart_df.index, y=tenkan.tail(chart_bars), mode='lines',
                                 line=dict(color='#EF5350', width=1.4), name='Tenkan (9)'), row=1, col=1)
        fig.add_trace(go.Scatter(x=chart_df.index, y=kijun.tail(chart_bars), mode='lines',
                                 line=dict(color='#42A5F5', width=1.4), name='Kijun (26)'), row=1, col=1)
        fig.add_trace(go.Scatter(x=chart_df.index, y=chikou.tail(chart_bars), mode='lines',
                                 line=dict(color='#9E9E9E', dash='dot', width=1), name='Chikou'), row=1, col=1)

    # Strategy overlays + signal markers belong to the 1H system timeframe only
    if chart_tf == "1H":
        if pair == "EUR/GBP":
            fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df['Swing_Low'], mode='lines', line=dict(color='#00E676', dash='dash'), name='Support'), row=1, col=1)
        elif pair in ("GBP/JPY", "USD/JPY", "EUR/USD", "EUR/JPY"):
            fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df['SMA_20'], mode='lines', line=dict(color='#FF9800'), name='SMA 20'), row=1, col=1)
            fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df['SMA_50'], mode='lines', line=dict(color='#2196F3'), name='SMA 50'), row=1, col=1)
        elif pair == "USD/CAD":
            fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df['Rolling_High_20'], mode='lines', line=dict(color='#AB47BC', dash='dot'), name='20-High'), row=1, col=1)
        elif config['strategy'] == "Equity Pullback (Buy the Dip)":
            fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df['Lower_Band'], mode='lines', line=dict(color='#00E676', dash='dash'), name='Lower BB'), row=1, col=1)

        signals = chart_df[chart_df['Signal'] == True]
        if not signals.empty:
            fig.add_trace(go.Scatter(x=signals.index, y=signals['Low'] * 0.999, mode='markers', marker=dict(symbol='triangle-up', size=14, color='#00E676')), row=1, col=1)
    else:
        fig.add_annotation(text=f"{chart_tf} view ({chart_src}) — signals plot on 1H", xref="x domain", yref="y domain",
                           x=0.01, y=0.98, showarrow=False, font=dict(size=10, color="#9E9E9E"), row=1, col=1)

    # Volume pane: own volume (ETF exchange / OANDA tick), else 1H futures proxy
    if vol_ok:
        try:
            if has_own_vol:
                vol_df = chart_df
                vol_label = "Volume" if is_etf else "FX tick volume (OANDA)"
            else:
                vol_df = fetch_proxy_volume(config['vol_proxy'])
                vol_df = vol_df[vol_df.index >= chart_df.index[0].tz_convert(vol_df.index.tz)]
                vol_label = f"Volume proxy: {config['vol_proxy']} futures"
            if not vol_df.empty:
                vol_colors = np.where(vol_df['Close'].diff().fillna(0) >= 0, '#26a69a', '#ef5350')
                fig.add_trace(go.Bar(x=vol_df.index, y=vol_df['Volume'],
                                     marker_color=vol_colors, name="Volume"), row=row_of['volume'], col=1)
                fig.add_annotation(text=vol_label, xref="x domain", yref="y domain",
                                   x=0.01, y=0.95, showarrow=False, font=dict(size=10, color="#9E9E9E"), row=row_of['volume'], col=1)
        except Exception:
            pass  # keep the chart usable if the volume feed is down

    if show_rsi:
        fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df['RSI'], mode='lines', line=dict(color='#9E9E9E')), row=row_of['rsi'], col=1)
        fig.add_hline(y=70, line_dash="dash", line_color="#ef5350", row=row_of['rsi'], col=1)
        fig.add_hline(y=30, line_dash="dash", line_color="#26a69a", row=row_of['rsi'], col=1)

    if show_macd:
        macd_line = close_full.ewm(span=12, adjust=False).mean() - close_full.ewm(span=26, adjust=False).mean()
        macd_sig = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = (macd_line - macd_sig).tail(chart_bars)
        hist_colors = np.where(macd_hist >= 0, '#26a69a', '#ef5350')
        fig.add_trace(go.Bar(x=chart_df.index, y=macd_hist, marker_color=hist_colors, name='MACD hist'), row=row_of['macd'], col=1)
        fig.add_trace(go.Scatter(x=chart_df.index, y=macd_line.tail(chart_bars), mode='lines', line=dict(color='#42A5F5', width=1.2), name='MACD'), row=row_of['macd'], col=1)
        fig.add_trace(go.Scatter(x=chart_df.index, y=macd_sig.tail(chart_bars), mode='lines', line=dict(color='#FF9800', width=1.2), name='Signal'), row=row_of['macd'], col=1)

    fig.update_layout(template="plotly_dark", height=620 + 80 * (len(pane_rows) - 1),
                      margin=dict(l=10, r=10, t=10, b=10), showlegend=True)
    for _i in range(1, len(pane_rows) + 1):
        fig.update_layout(**{f"xaxis{'' if _i == 1 else _i}_rangeslider_visible": False})
    st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------
# 5. LAYOUT: TABS
# ---------------------------------------------------------
@st.cache_data(ttl=20)
def fetch_oanda_account():
    return dp.oanda_account()

@st.cache_data(ttl=60)
def fetch_webull_account():
    return dp.webull_account()

tab_chart, tab_pos, tab_scan = st.tabs(["📈 Chart", "💼 Positions & Account", "🔎 Scanner"])

with tab_chart:
    c_wl, c_asset = st.columns([1, 2])
    sel_group = c_wl.selectbox("Watchlist", list(WATCHLISTS.keys()))
    sel_pair = c_asset.selectbox("Asset", list(WATCHLISTS[sel_group].keys()))
    render_asset(sel_pair, WATCHLISTS[sel_group][sel_pair])

with tab_pos:
    from datetime import datetime as _dtnow
    st.caption(f"Updated {_dtnow.now().strftime('%H:%M:%S')} — refreshes with the sidebar Auto-refresh timer "
               f"(OANDA every 20s, Webull every 60s, paper ledger hourly from the cloud bot)")
    st.subheader("🤖 Paper bot")
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_state.json")) as _f:
            _ps = json.load(_f)
        c1, c2, c3 = st.columns(3)
        c1.metric("Realized P/L", f"${_ps['realized_pl']:+,.2f}")
        c2.metric("Open positions", len(_ps["open"]))
        c3.metric("Closed trades", len(_ps["closed"]))
        if _ps["open"]:
            st.dataframe(pd.DataFrame(_ps["open"]), use_container_width=True)
        if _ps["closed"]:
            st.caption("Recent closes (newest first)")
            st.dataframe(pd.DataFrame(_ps["closed"][-20:]).iloc[::-1], use_container_width=True)
    except FileNotFoundError:
        st.info("No paper ledger yet — it appears after the bot's first run "
                "(hourly on GitHub Actions, or run paper_bot.py locally).")

    st.subheader("🪙 Webull account (read-only)")
    wb_accounts = fetch_webull_account()
    if wb_accounts:
        for wba in wb_accounts:
            if wba["net_liq"] == 0 and not wba["positions"]:
                continue  # skip empty sub-account shells
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric(f"Net liq ({wba['type'] or wba['id']})", f"${wba['net_liq']:,.2f}")
            c2.metric("Cash", f"${wba['cash']:,.2f}")
            c3.metric("Buying power", f"${wba['buying_power']:,.2f}")
            c4.metric("Day P/L", f"${wba['day_pl']:+,.2f}")
            c5.metric("Unrealized P/L", f"${wba['upl']:+,.2f}")
            if wba["positions"]:
                st.dataframe(pd.DataFrame(wba["positions"]), use_container_width=True)
    else:
        st.info("Set WEBULL_APP_KEY / WEBULL_APP_SECRET (secrets or Downloads key file) to see account data.")

    st.subheader("🏦 OANDA account (read-only)")
    accounts = fetch_oanda_account()
    if accounts:
        for acct in accounts:
            if acct["open_count"] == 0 and acct["balance"] == 0:
                continue  # skip empty sub-accounts
            c1, c2, c3, c4 = st.columns(4)
            c1.metric(f"NAV (…{acct['id'][-3:]})", f"${acct['NAV']:,.2f}")
            c2.metric("Balance", f"${acct['balance']:,.2f}")
            c3.metric("Unrealized P/L", f"${acct['unrealized']:+,.2f}")
            c4.metric("Margin used", f"${acct['margin_used']:,.2f}")
            if acct["trades"]:
                st.dataframe(pd.DataFrame(acct["trades"]), use_container_width=True)
    else:
        st.info("Set OANDA_TOKEN (env / secrets / Downloads token file) to see live account data.")

with tab_scan:
    def _scan_row(_p, _cfg):
        try:
            _d1h, _d1d, _src = fetch_data(_cfg['ticker'], live_mode)
            _d1h = calculate_indicators(_d1h)
            _d1h = apply_strategies(_d1h, _p)
            _lt = _d1h.iloc[-1]
            _n, _wr, _pf = run_backtest(_d1h)
            _trend = "🟢 Bullish" if float(_d1d['Close'].iloc[-1]) > float(_d1d['Close'].rolling(50).mean().iloc[-1]) else "🔴 Bearish"
            _px = ".2f" if _cfg['vol_proxy'] is None else ".4f"
            return {"Asset": _p, "Strategy": _cfg['strategy'], "Price": f"{float(_lt['Close']):{_px}}",
                    "Daily trend": _trend, "Signal (2 bars)": "🚨 YES" if bool(_d1h['Signal'].tail(2).any()) else "—",
                    "Win rate %": round(_wr, 1), "Profit factor": round(_pf, 2), "Data": _src}
        except Exception as _e:
            return {"Asset": _p, "Strategy": _cfg['strategy'], "Data": f"error: {_e}"}

    for _gname, _g_assets in WATCHLISTS.items():
        st.subheader(_gname)
        st.dataframe(pd.DataFrame([_scan_row(_p, _cfg) for _p, _cfg in _g_assets.items()]),
                     use_container_width=True, hide_index=True)
