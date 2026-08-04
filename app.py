import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

st.set_page_config(page_title="Multi-Pair FX Scanner", page_icon="📈", layout="wide")
st.title("FX Watchlist & Visual Technical Scanner")
st.markdown("Automated candlestick charts with real-time overlays and RSI momentum gauges.")

# ---------------------------------------------------------
# 1. WATCHLIST & STRATEGY ASSIGNMENT
# ---------------------------------------------------------
WATCHLIST = {
    "EUR/GBP": {
        "ticker": "EURGBP=X", 
        "strategy_name": "Mean Reversion (Range Trading)",
        "description": "Scans for Bullish Engulfing patterns near a dynamic Support Floor."
    },
    "GBP/JPY": {
        "ticker": "GBPJPY=X", 
        "strategy_name": "Trend Following (Momentum)",
        "description": "Scans for 20/50 Moving Average Golden Crosses."
    },
    "USD/CAD": {
        "ticker": "USDCAD=X", 
        "strategy_name": "Momentum Breakout",
        "description": "Scans for price breaks above the 20-period ceiling."
    }
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
def calculate_rsi(df, periods=14):
    """Calculates the Relative Strength Index (RSI) using Wilder's Smoothing."""
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/periods, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/periods, adjust=False).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    return df

def apply_mean_reversion(df):
    n = 5 
    df['Swing_Low'] = df['Low'][(df['Low'] == df['Low'].rolling(window=2*n+1, center=True).min())]
    df['Live_Support'] = df['Swing_Low'].ffill()
    
    df['Prev_Open'] = df['Open'].shift(1)
    df['Prev_Close'] = df['Close'].shift(1)
    
    df['Bullish_Engulfing'] = (
        (df['Prev_Close'] < df['Prev_Open']) &  
        (df['Close'] > df['Open']) &            
        (df['Close'] > df['Prev_Open']) &       
        (df['Open'] < df['Prev_Close'])         
    )

    pip_tolerance = 0.0015 
    df['Signal'] = (df['Bullish_Engulfing'] & (abs(df['Low'] - df['Live_Support']) <= pip_tolerance))
    return df

def apply_trend_following(df):
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    df['SMA_50'] = df['Close'].rolling(window=50).mean()
    
    df['Prev_SMA_20'] = df['SMA_20'].shift(1)
    df['Prev_SMA_50'] = df['SMA_50'].shift(1)
    
    df['Signal'] = (df['SMA_20'] > df['SMA_50']) & (df['Prev_SMA_20'] <= df['Prev_SMA_50'])
    return df

def apply_breakout(df):
    df['Rolling_High_20'] = df['High'].rolling(window=20).max().shift(1)
    df['Signal'] = df['Close'] > df['Rolling_High_20']
    return df

# ---------------------------------------------------------
# 3. INTERACTIVE PLOTLY CHART BUILDER WITH SUBPLOTS
# ---------------------------------------------------------
def create_dual_chart(df, pair):
    chart_df = df.tail(80)
    
    # Create subplots: 2 rows, 1 column. 
    # Row 1 gets 75% of height (Candles), Row 2 gets 25% (RSI)
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, 
                        vertical_spacing=0.03, row_heights=[0.75, 0.25])

    # --- ROW 1: Price Action & Overlays ---
    fig.add_trace(go.Candlestick(
        x=chart_df.index, open=chart_df['Open'], high=chart_df['High'],
        low=chart_df['Low'], close=chart_df['Close'], name="Price",
        increasing_line_color='#26a69a', decreasing_line_color='#ef5350'
    ), row=1, col=1)

    if pair == "EUR/GBP":
        fig.add_trace(go.Scatter(
            x=chart_df.index, y=chart_df['Live_Support'], mode='lines', 
            name='Support Floor', line=dict(color='#00E676', width=2, dash='dash')
        ), row=1, col=1)
    elif pair == "GBP/JPY":
        fig.add_trace(go.Scatter(
            x=chart_df.index, y=chart_df['SMA_20'], mode='lines', 
            name='20 SMA (Fast)', line=dict(color='#FF9800', width=1.5)
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=chart_df.index, y=chart_df['SMA_50'], mode='lines', 
            name='50 SMA (Slow)', line=dict(color='#2196F3', width=1.5)
        ), row=1, col=1)
    elif pair == "USD/CAD":
        fig.add_trace(go.Scatter(
            x=chart_df.index, y=chart_df['Rolling_High_20'], mode='lines', 
            name='20-Period High', line=dict(color='#AB47BC', width=2, dash='dot')
        ), row=1, col=1)

    # Signal Direction Markers
    signals = chart_df[chart_df['Signal'] == True]
    if not signals.empty:
        fig.add_trace(go.Scatter(
            x=signals.index, y=signals['Low'] * 0.9992,
            mode='markers', marker=dict(symbol='triangle-up', size=14, color='#00E676'),
            name='Entry Signal'
        ), row=1, col=1)

    # --- ROW 2: Relative Strength Index (RSI) ---
    fig.add_trace(go.Scatter(
        x=chart_df.index, y=chart_df['RSI'], mode='lines', 
        name='RSI (14)', line=dict(color='#9E9E9E', width=1.5)
    ), row=2, col=1)

    # Add Overbought (70) and Oversold (30) boundary lines to RSI
    fig.add_hline(y=70, line_dash="dash", line_color="#ef5350", line_width=1, row=2, col=1)
    fig.add_hline(y=30, line_dash="dash", line_color="#26a69a", line_width=1, row=2, col=1)

    # Global Layout Formatting
    fig.update_layout(
        xaxis_rangeslider_visible=False,
        xaxis2_rangeslider_visible=False, # Disable rangeslider on the bottom plot too
        template="plotly_dark",
        height=550, # Increased height to accommodate the second chart comfortably
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    
    # Lock the RSI y-axis from 0 to 100
    fig.update_yaxes(range=[0, 100], row=2, col=1)
    
    return fig

# ---------------------------------------------------------
# 4. DASHBOARD RENDER
# ---------------------------------------------------------
for pair, config in WATCHLIST.items():
    st.divider()
    st.subheader(f"📊 {pair} — {config['strategy_name']}")
    
    df = fetch_data(config['ticker'])
    df = calculate_rsi(df) # Calculate RSI for all pairs
    
    if pair == "EUR/GBP":
        df = apply_mean_reversion(df)
    elif pair == "GBP/JPY":
        df = apply_trend_following(df)
    elif pair == "USD/CAD":
        df = apply_breakout(df)
        
    latest = df.iloc[-1]
    
    # Display Price Metrics
    col1, col2, col3 = st.columns(3)
    col1.metric("Current Price", f"{latest['Close']:.4f}")
    col2.metric("Current RSI", f"{latest['RSI']:.1f}")
    
    # Render Interactive Dual-Pane Chart
    fig = create_dual_chart(df, pair)
    st.plotly_chart(fig, use_container_width=True)
    
    # Render Alert Status
    recent_signals = df[df['Signal'] == True].tail(3)
    if not recent_signals.empty:
        st.success(f"🚀 Signal Detected recently!")
    else:
        st.info("Status: No active directional shift detected.")
