import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np

st.set_page_config(page_title="EUR/USD Scanner", page_icon="📈")
st.title("EUR/USD Live Signals")

@st.cache_data(ttl=300) # Refreshes data every 5 minutes
def fetch_and_analyze():
    df = yf.download("EURUSD=X", period="1mo", interval="1h", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.dropna(inplace=True)

    n = 5 
    df['Swing_High'] = df['High'][(df['High'] == df['High'].rolling(window=2*n+1, center=True).max())]
    df['Swing_Low'] = df['Low'][(df['Low'] == df['Low'].rolling(window=2*n+1, center=True).min())]
    df['Live_Resistance'] = df['Swing_High'].ffill()
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
    df['Entry_Signal'] = (
        df['Bullish_Engulfing'] & 
        (abs(df['Low'] - df['Live_Support']) <= pip_tolerance)
    )
    return df

df = fetch_and_analyze()
latest = df.iloc[-1]

col1, col2, col3 = st.columns(3)
col1.metric("Current Price", f"{latest['Close']:.4f}")
col2.metric("Resistance (Ceiling)", f"{latest['Live_Resistance']:.4f}")
col3.metric("Support (Floor)", f"{latest['Live_Support']:.4f}")

recent_signals = df[df['Entry_Signal'] == True]

st.subheader("Actionable Alerts")
if not recent_signals.empty:
    st.success("High Probability Entry Found! Recent Bullish Reversal near Support.")
    st.dataframe(recent_signals[['Close', 'Live_Support']].tail(3))
else:
    st.info("No immediate entry patterns detected near support right now. Wait for the price to approach the Support Floor.")
