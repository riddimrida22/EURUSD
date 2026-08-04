import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go

st.set_page_config(page_title="Multi-Pair FX Scanner", page_icon="📈", layout="wide")
st.title("FX Watchlist & Visual Technical Scanner")
st.markdown("Automated candlestick charts with real-time indicators and signal directional markers.")

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
# 2. STRATEGY ALGORITHMS
# ---------------------------------------------------------
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
# 3. INTERACTIVE PLOTLY CHART BUILDER
# ---------------------------------------------------------
def create_candlestick_chart(df, pair):
    # Slice the last 80 candles so the chart remains crisp on mobile screens
    chart_df = df.tail(80)
    
    fig = go.Figure()

    # Base Candlestick Chart
    fig.add_trace(go.Candlestick(
        x=chart_df.index,
        open=chart_df['Open'],
        high=chart_df['High'],
        low=chart_df['Low'],
        close=chart_df['Close'],
        name="Price",
        increasing_line_color='#26a69a', 
        decreasing_line_color='#ef5350'
    ))

    # Add Strategy Specific Overlays
    if pair == "EUR/GBP":
        fig.add_trace(go.Scatter(
            x=chart_df.index, y=chart_df['Live_Support'], 
            mode='lines', name='Support Floor', 
            line=dict(color='#00E676', width=2, dash='dash')
        ))
    elif pair == "GBP/JPY":
        fig.add_trace(go.Scatter(
            x=chart_df.index, y=chart_df['SMA_20'], 
            mode='lines', name='20 SMA (Fast)', 
            line=dict(color='#FF9800', width=1.5)
        ))
        fig.add_trace(go.Scatter(
            x=chart_df.index, y=chart_df['SMA_50'], 
            mode='lines', name='50 SMA (Slow)', 
            line=dict(color='#2196F3', width=1.5)
        ))
    elif pair == "USD/CAD":
        fig.add_trace(go.Scatter(
            x=chart_df.index, y=chart_df['Rolling_High_20'], 
            mode='lines', name='20-Period High Ceiling', 
            line=dict(color='#AB47BC', width=2, dash='dot')
        ))

    # Add Signal Direction Markers (Green Triangles below the entry candle)
    signals = chart_df[chart_df['Signal'] == True]
    if not signals.empty:
        fig.add_trace(go.Scatter(
            x=signals.index,
            y=signals['Low'] * 0.9992, # Position slightly below low price
            mode='markers',
            marker=dict(symbol='triangle-up', size=14, color='#00E676'),
            name='Entry Signal'
        ))

    fig.update_layout(
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        height=380,
        margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    return fig

# ---------------------------------------------------------
# 4. DASHBOARD RENDER
# ---------------------------------------------------------
for pair, config in WATCHLIST.items():
    st.divider()
    st.subheader(f"📊 {pair} — {config['strategy_name']}")
    
    df = fetch_data(config['ticker'])
    
    if pair == "EUR/GBP":
        df = apply_mean_reversion(df)
    elif pair == "GBP/JPY":
        df = apply_trend_following(df)
    elif pair == "USD/CAD":
        df = apply_breakout(df)
        
    latest = df.iloc[-1]
    
    # Display Price Metrics
    col1, col2 = st.columns(2)
    col1.metric("Current Price", f"{latest['Close']:.4f}")
    
    # Render Interactive Chart
    fig = create_candlestick_chart(df, pair)
    st.plotly_chart(fig, use_container_width=True)
    
    # Render Alert Status
    recent_signals = df[df['Signal'] == True].tail(3)
    if not recent_signals.empty:
        st.success(f"🚀 Signal Detected recently!")
    else:
        st.info("Status: No active directional shift detected.")
