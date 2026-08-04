import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

st.set_page_config(page_title="Multi-Pair FX Scanner", page_icon="📈", layout="wide")
st.title("FX Watchlist & Strategy Scanner")

# ---------------------------------------------------------
# 0. SIDEBAR: ACCOUNT & RISK MANAGEMENT
# ---------------------------------------------------------
st.sidebar.header("⚙️ Account Settings")
nav = st.sidebar.number_input("Account Balance (NAV in $)", value=10000, step=1000)
risk_pct = st.sidebar.slider("Risk per Trade (%)", min_value=0.5, max_value=5.0, value=1.0, step=0.5)

st.sidebar.markdown("""
**How Sizing Works:**
The app calculates the volatility of the asset using the Average True Range (ATR). 
It sets a stop-loss just outside the ATR, and sizes your position so that if the stop-loss is hit, you only lose your specified Risk %.
""")

# ---------------------------------------------------------
# 1. WATCHLIST & STRATEGY ASSIGNMENT
# ---------------------------------------------------------
WATCHLIST = {
    "EUR/GBP": {"ticker": "EURGBP=X", "strategy_name": "Mean Reversion (Range)"},
    "GBP/JPY": {"ticker": "GBPJPY=X", "strategy_name": "Trend Following (Momentum)"},
    "USD/CAD": {"ticker": "USDCAD=X", "strategy_name": "Momentum Breakout"}
}

@st.cache_data(ttl=300)
def fetch_data(ticker):
    df = yf.download(ticker, period="1mo", interval="1h", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.dropna(inplace=True)
    return df

# ---------------------------------------------------------
# 2. ALGORITHMS & INDICATORS
# ---------------------------------------------------------
def calculate_indicators(df):
    # RSI
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # Average True Range (ATR) for Stop Loss Calculation
    df['H-L'] = df['High'] - df['Low']
    df['H-C'] = abs(df['High'] - df['Close'].shift(1))
    df['L-C'] = abs(df['Low'] - df['Close'].shift(1))
    df['TR'] = df[['H-L', 'H-C', 'L-C']].max(axis=1)
    df['ATR'] = df['TR'].rolling(14).mean()
    return df

def apply_strategies(df, pair):
    df['Signal'] = False
    
    if pair == "EUR/GBP":
        n = 5 
        df['Swing_Low'] = df['Low'][(df['Low'] == df['Low'].rolling(window=2*n+1, center=True).min())]
        df['Live_Support'] = df['Swing_Low'].ffill()
        df['Prev_Open'], df['Prev_Close'] = df['Open'].shift(1), df['Close'].shift(1)
        df['Bullish_Engulfing'] = ((df['Prev_Close'] < df['Prev_Open']) & (df['Close'] > df['Open']) & 
                                   (df['Close'] > df['Prev_Open']) & (df['Open'] < df['Prev_Close']))
        df['Signal'] = (df['Bullish_Engulfing'] & (abs(df['Low'] - df['Live_Support']) <= 0.0015))
        
    elif pair == "GBP/JPY":
        df['SMA_20'] = df['Close'].rolling(window=20).mean()
        df['SMA_50'] = df['Close'].rolling(window=50).mean()
        df['Signal'] = (df['SMA_20'] > df['SMA_50']) & (df['SMA_20'].shift(1) <= df['SMA_50'].shift(1))
        
    elif pair == "USD/CAD":
        df['Rolling_High_20'] = df['High'].rolling(window=20).max().shift(1)
        df['Signal'] = df['Close'] > df['Rolling_High_20']
        
    return df

# ---------------------------------------------------------
# 3. BACKTESTING ENGINE
# ---------------------------------------------------------
def run_backtest(df):
    # Assume a 24-hour (24 candle) holding period for simplicity
    df['Exit_Price'] = df['Close'].shift(-24)
    
    # Only look at historical signals, excluding the current live candle
    historical_signals = df[(df['Signal'] == True) & (df['Exit_Price'].notna())]
    
    total_trades = len(historical_signals)
    if total_trades == 0:
        return 0, 0, 0
        
    historical_signals['Won'] = historical_signals['Exit_Price'] > historical_signals['Close']
    wins = len(historical_signals[historical_signals['Won'] == True])
    win_rate = (wins / total_trades) * 100
    
    # Calculate rough pip gain (simplified)
    historical_signals['Pip_Gain'] = historical_signals['Exit_Price'] - historical_signals['Close']
    total_gain = historical_signals['Pip_Gain'].sum()
    
    return total_trades, win_rate, total_gain

# ---------------------------------------------------------
# 4. CHART BUILDER (Simplified for space)
# ---------------------------------------------------------
def create_dual_chart(df, pair):
    chart_df = df.tail(80)
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.75, 0.25])

    fig.add_trace(go.Candlestick(x=chart_df.index, open=chart_df['Open'], high=chart_df['High'],
                                 low=chart_df['Low'], close=chart_df['Close'], name="Price"), row=1, col=1)

    if pair == "EUR/GBP":
        fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df['Live_Support'], mode='lines', name='Support', line=dict(color='#00E676', dash='dash')), row=1, col=1)
    elif pair == "GBP/JPY":
        fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df['SMA_20'], mode='lines', name='20 SMA', line=dict(color='#FF9800')), row=1, col=1)
        fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df['SMA_50'], mode='lines', name='50 SMA', line=dict(color='#2196F3')), row=1, col=1)
    elif pair == "USD/CAD":
        fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df['Rolling_High_20'], mode='lines', name='20-High', line=dict(color='#AB47BC', dash='dot')), row=1, col=1)

    signals = chart_df[chart_df['Signal'] == True]
    if not signals.empty:
        fig.add_trace(go.Scatter(x=signals.index, y=signals['Low'] * 0.999, mode='markers', marker=dict(symbol='triangle-up', size=14, color='#00E676'), name='Signal'), row=1, col=1)

    fig.add_trace(go.Scatter(x=chart_df.index, y=chart_df['RSI'], mode='lines', name='RSI', line=dict(color='#9E9E9E')), row=2, col=1)
    fig.add_hline(y=70, line_dash="dash", line_color="#ef5350", row=2, col=1)
    fig.add_hline(y=30, line_dash="dash", line_color="#26a69a", row=2, col=1)

    fig.update_layout(xaxis_rangeslider_visible=False, xaxis2_rangeslider_visible=False, template="plotly_dark", height=500, margin=dict(l=10, r=10, t=10, b=10))
    fig.update_yaxes(range=[0, 100], row=2, col=1)
    return fig

# ---------------------------------------------------------
# 5. DASHBOARD RENDER
# ---------------------------------------------------------
for pair, config in WATCHLIST.items():
    st.divider()
    st.subheader(f"{pair} — {config['strategy_name']}")
    
    df = fetch_data(config['ticker'])
    df = calculate_indicators(df)
    df = apply_strategies(df, pair)
    
    latest = df.iloc[-1]
    
    # Backtest Stats
    total_trades, win_rate, total_gain = run_backtest(df)
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Current Price", f"{latest['Close']:.4f}")
    col2.metric("Monthly Trades", total_trades)
    col3.metric("Historical Win Rate", f"{win_rate:.1f}%")
    
    # Determine Pip Multiplier (JPY pairs are calculated differently)
    multiplier = 100 if "JPY" in pair else 10000
    col4.metric("Net Pip Gain (30d)", f"{(total_gain * multiplier):.1f}")
    
    # ---------------------------------------------------------
    # POPUP ALERT & SIZING CALCULATOR
    # ---------------------------------------------------------
    # Check if a signal triggered in the last 2 candles
    recent_signals = df['Signal'].tail(2) 
    
    if recent_signals.any():
        st.toast(f"🔥 {pair} Signal Detected!", icon="🚨")
        
        # Sizing Math based on ATR
        atr = latest['ATR']
        stop_loss_dist = atr * 1.5
        stop_loss_price = latest['Close'] - stop_loss_dist
        take_profit_price = latest['Close'] + (stop_loss_dist * 2) # 1:2 Risk/Reward
        
        # Dollar Risk
        risk_amount = nav * (risk_pct / 100)
        
        # Stop loss percentage distance
        sl_pct_dist = stop_loss_dist / latest['Close']
        
        # Total Notional Position Size
        notional_size = risk_amount / sl_pct_dist
        percent_of_nav = (notional_size / nav) * 100

        st.warning(f"""
        ### 🚨 ACTION REQUIRED: BUY SIGNAL ON {pair}
        **Entry Strategy:** {config['strategy_name']}
        
        **Trade Parameters:**
        * **Entry Price:** {latest['Close']:.4f}
        * **Stop Loss:** {stop_loss_price:.4f} *(1.5x ATR below entry)*
        * **Take Profit:** {take_profit_price:.4f} *(1:2 Risk-Reward Ratio)*
        
        **Sizing Recommendation (Based on ${nav:,.2f} NAV):**
        To strictly limit your risk to **{risk_pct}% (${risk_amount:,.2f})**, you should open a total trade size of **${notional_size:,.2f}**. 
        * *This position represents **{percent_of_nav:.1f}%** of your total NAV, utilizing leverage.*
        """)
        
    st.plotly_chart(create_dual_chart(df, pair), use_container_width=True)
