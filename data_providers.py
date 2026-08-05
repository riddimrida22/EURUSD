"""Data layer for the Pro FX & ETF Terminal.

Sources, in order of preference:
- FX pairs:  OANDA v20 candles (live, real tick volume) when OANDA_TOKEN is set;
             otherwise yfinance (delayed ~1-15 min).
- ETFs:      yfinance hourly/daily bars for history, plus a real-time last price
             from Webull OpenAPI when credentials are available (env vars
             WEBULL_APP_KEY / WEBULL_APP_SECRET, or the same key file the
             options-bot desk uses in ~/Downloads).

Env vars:
    OANDA_TOKEN   personal access token from an OANDA practice account
    OANDA_ENV     "practice" (default) or "live" -> chooses API host
"""
import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime
from urllib.parse import quote, urlencode
import urllib.request

import pandas as pd
import requests
import yfinance as yf

# ---------------------------------------------------------------------------
# OANDA (FX)
# ---------------------------------------------------------------------------
OANDA_INSTRUMENTS = {
    "EURGBP=X": "EUR_GBP",
    "GBPJPY=X": "GBP_JPY",
    "USDJPY=X": "USD_JPY",
    "USDCAD=X": "USD_CAD",
}


_OANDA_TOKEN_PATHS = ["~/Downloads/oanda.txt", "~/Downloads/OANDA.txt"]
_OANDA_TOKEN_RE = re.compile(r"[A-Za-z0-9\-]{30,}")


def _oanda_token():
    tok = os.environ.get("OANDA_TOKEN", "").strip()
    if tok and "PASTE" not in tok.upper():
        return tok
    # Same convention as the Webull keys: read from a file in ~/Downloads
    for p in _OANDA_TOKEN_PATHS:
        try:
            txt = open(os.path.expanduser(p), encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        m = _OANDA_TOKEN_RE.search(txt)
        if m:
            return m.group(0)
    return ""


_OANDA_HOSTS = {"practice": "https://api-fxpractice.oanda.com", "live": "https://api-fxtrade.oanda.com"}
_oanda_detected_env = None


def _oanda_base():
    """Host for the token's environment. OANDA_ENV=practice|live overrides;
    otherwise auto-detect once by probing /v3/accounts with the token."""
    global _oanda_detected_env
    env = os.environ.get("OANDA_ENV", "").strip().lower()
    if env in _OANDA_HOSTS:
        return _OANDA_HOSTS[env]
    if _oanda_detected_env is None:
        for name, host in _OANDA_HOSTS.items():
            try:
                r = requests.get(f"{host}/v3/accounts",
                                 headers={"Authorization": f"Bearer {_oanda_token()}"}, timeout=10)
                if r.status_code == 200:
                    _oanda_detected_env = name
                    break
            except Exception:
                continue
        _oanda_detected_env = _oanda_detected_env or "practice"
    return _OANDA_HOSTS[_oanda_detected_env]


def _oanda_candles(instrument, granularity, count):
    resp = requests.get(
        f"{_oanda_base()}/v3/instruments/{instrument}/candles",
        params={"granularity": granularity, "count": count, "price": "M"},
        headers={"Authorization": f"Bearer {_oanda_token()}"},
        timeout=20,
    )
    resp.raise_for_status()
    rows = []
    for c in resp.json()["candles"]:
        rows.append({
            "Time": c["time"],
            "Open": float(c["mid"]["o"]),
            "High": float(c["mid"]["h"]),
            "Low": float(c["mid"]["l"]),
            "Close": float(c["mid"]["c"]),
            "Volume": float(c.get("volume", 0)),  # real FX tick volume
        })
    df = pd.DataFrame(rows)
    df["Time"] = pd.to_datetime(df["Time"], format="ISO8601", utc=True)
    return df.set_index("Time")


# ---------------------------------------------------------------------------
# yfinance fallback (both asset classes)
# ---------------------------------------------------------------------------
def _yf_pair(ticker):
    df_1h = yf.download(ticker, period="1mo", interval="1h", progress=False)
    df_1d = yf.download(ticker, period="6mo", interval="1d", progress=False)
    for df in (df_1h, df_1d):
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.dropna(inplace=True)
    return df_1h, df_1d


OANDA_GRANULARITY = {"5m": "M5", "15m": "M15", "1H": "H1", "4H": "H4", "1D": "D", "1W": "W"}
_YF_CHART = {"5m": ("5m", "1mo"), "15m": ("15m", "1mo"), "1H": ("1h", "3mo"),
             "4H": ("1h", "3mo"), "1D": ("1d", "2y"), "1W": ("1wk", "5y")}


def fetch_chart(ticker, tf="1H", bars=80, allow_live=True):
    """OHLCV at an arbitrary display timeframe for charting. Returns (df, source).
    Fetches extra history so long overlays (e.g. SMA 200) have warm-up data."""
    count = min(bars + 220, 2000)
    inst = OANDA_INSTRUMENTS.get(ticker)
    if allow_live and inst and _oanda_token():
        try:
            df = _oanda_candles(inst, OANDA_GRANULARITY[tf], count)
            if not df.empty:
                return df, "OANDA (live)"
        except Exception as e:
            print(f"OANDA chart fetch failed for {inst} {tf} ({e}); falling back to Yahoo")
    interval, period = _YF_CHART[tf]
    df = yf.download(ticker, period=period, interval=interval, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.dropna(inplace=True)
    if tf == "4H" and not df.empty:  # yfinance has no native 4H bars
        df = df.resample("4h").agg({"Open": "first", "High": "max", "Low": "min",
                                    "Close": "last", "Volume": "sum"}).dropna()
    return df.tail(count), "Yahoo (delayed)"


def fetch_asset(ticker, allow_live=True):
    """Return (df_1h, df_1d, source_label) for any watchlist ticker.
    allow_live=False forces the delayed yfinance path regardless of tokens."""
    inst = OANDA_INSTRUMENTS.get(ticker)
    if allow_live and inst and _oanda_token():
        try:
            df_1h = _oanda_candles(inst, "H1", 720)   # ~30 trading days of 1H bars
            df_1d = _oanda_candles(inst, "D", 180)    # ~6 months of daily bars
            if not df_1h.empty and not df_1d.empty:
                return df_1h, df_1d, "OANDA (live)"
        except Exception as e:
            print(f"OANDA fetch failed for {inst} ({e}); falling back to Yahoo")
    df_1h, df_1d = _yf_pair(ticker)
    return df_1h, df_1d, "Yahoo (delayed)"


def oanda_account():
    """Read-only account summary + open trades for every OANDA account the
    token can see. Returns a list of dicts, or None when no token is set."""
    if not _oanda_token():
        return None
    base = _oanda_base()
    h = {"Authorization": f"Bearer {_oanda_token()}"}
    out = []
    try:
        accounts = requests.get(f"{base}/v3/accounts", headers=h, timeout=15).json()["accounts"]
        for a in accounts:
            aid = a["id"]
            s = requests.get(f"{base}/v3/accounts/{aid}/summary", headers=h, timeout=15).json()["account"]
            trades = requests.get(f"{base}/v3/accounts/{aid}/openTrades", headers=h, timeout=15).json()["trades"]
            rows = [{
                "instrument": t["instrument"], "units": float(t["currentUnits"]),
                "entry": float(t["price"]), "unrealized P/L": float(t["unrealizedPL"]),
                "SL": float(t["stopLossOrder"]["price"]) if t.get("stopLossOrder") else None,
                "TP": float(t["takeProfitOrder"]["price"]) if t.get("takeProfitOrder") else None,
                "trail dist": t.get("trailingStopLossOrder", {}).get("distance"),
            } for t in trades]
            out.append({"id": aid, "NAV": float(s["NAV"]), "balance": float(s["balance"]),
                        "unrealized": float(s["unrealizedPL"]), "open_count": int(s["openTradeCount"]),
                        "margin_used": float(s.get("marginUsed", 0)), "trades": rows})
    except Exception as e:
        print(f"OANDA account fetch failed: {e}")
        return None
    return out


# ---------------------------------------------------------------------------
# Webull OpenAPI real-time ETF price (vendored from the options-bot desk's
# verified pure-python adapter: HMAC-SHA1 request signing, no SDK needed)
# ---------------------------------------------------------------------------
_WB_HOST = "api.webull.com"
_WB_CREDS_PATHS = [
    "~/Downloads/Webull_App Key.txt",
    "~/Downloads/Webull_OpenAPI.txt",
    "~/Downloads/webull_openapi.txt",
]
_WB_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{16,}$")


def _wb_load_keys():
    key = os.environ.get("WEBULL_APP_KEY", "").strip()
    sec = os.environ.get("WEBULL_APP_SECRET", "").strip()
    if key and sec:
        return key, sec
    for p in _WB_CREDS_PATHS:
        try:
            txt = open(os.path.expanduser(p), encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        k = s = None
        for i, ln in enumerate(lines):
            lab = re.sub(r"[^a-z]", "", ln.lower())
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if _WB_TOKEN_RE.match(nxt):
                if lab in ("appkey", "key") and not k:
                    k = nxt
                elif lab in ("appsecret", "secret") and not s:
                    s = nxt
        if k and s:
            return k, s
    return None, None


def _wb_signed_headers(app_key, secret, uri, query=None):
    sp = {
        "x-app-key": app_key,
        "x-timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "x-signature-version": "1.0",
        "x-signature-algorithm": "HMAC-SHA1",
        "x-signature-nonce": str(uuid.uuid4()),
        "host": _WB_HOST,
    }
    for k, v in (query or {}).items():
        sp[k] = str(v)
    sts = uri + "&" + "&".join(f"{k}={sp[k]}" for k in sorted(sp))
    sts = quote(sts, safe="")
    sig = base64.b64encode(hmac.new((secret + "&").encode(), sts.encode(), hashlib.sha1).digest()).decode().strip()
    return {
        "x-app-key": app_key, "x-timestamp": sp["x-timestamp"],
        "x-signature-version": "1.0", "x-signature-algorithm": "HMAC-SHA1",
        "x-signature-nonce": sp["x-signature-nonce"], "x-signature": sig,
        "x-version": "v1", "Content-Type": "application/json",
    }


def live_etf_price(symbol):
    """Real-time last price for an ETF via Webull OpenAPI (includes pre/after
    market). Returns None when credentials or data are unavailable."""
    key, sec = _wb_load_keys()
    if not key:
        return None
    query = {"symbol": symbol, "category": "US_ETF", "timespan": "M1", "count": "2"}
    uri = "/market-data/bars"
    try:
        h = _wb_signed_headers(key, sec, uri, query)
        url = f"https://{_WB_HOST}{uri}?" + urlencode(query)
        with urllib.request.urlopen(urllib.request.Request(url, headers=h, method="GET"), timeout=10) as r:
            data = json.loads(r.read().decode() or "null")
        if isinstance(data, list) and data:
            return float(data[-1]["close"])
    except Exception as e:
        print(f"Webull live price failed for {symbol}: {e}")
    return None
