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
import threading
import time
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
# READ-ONLY GUARD
# This module is the only place in the project that talks to broker APIs.
# Every request is method-locked to GET and its path must match the read-only
# allowlist below, or the call raises before anything is sent. There is no
# code path anywhere in this codebase that can place, modify, or cancel an
# order — by construction, not convention.
# ---------------------------------------------------------------------------
_OANDA_READONLY_PATHS = (
    "/v3/instruments/",   # candles / pricing history
    "/v3/accounts",       # account list, summaries, open trades (GET only)
)
_WEBULL_READONLY_PATHS = (
    "/market-data/",            # bars / quotes
    "/app/subscriptions/list",  # account enumeration (read)
    "/account/profile",         # account metadata (read)
    "/account/balance",         # balances (read)
    "/account/positions",       # holdings (read)
)


def _assert_readonly(path, allowlist):
    if not any(path.startswith(p) for p in allowlist):
        raise PermissionError(f"Blocked non-read-only broker API path: {path}")


# ---------------------------------------------------------------------------
# OANDA (FX)
# ---------------------------------------------------------------------------
OANDA_INSTRUMENTS = {
    "EURUSD=X": "EUR_USD",
    "EURGBP=X": "EUR_GBP",
    "GBPUSD=X": "GBP_USD",
    "GBPJPY=X": "GBP_JPY",
    "EURJPY=X": "EUR_JPY",
    "USDJPY=X": "USD_JPY",
    "USDCAD=X": "USD_CAD",
    "USDMXN=X": "USD_MXN",
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
        _assert_readonly("/v3/accounts", _OANDA_READONLY_PATHS)
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
    _assert_readonly(f"/v3/instruments/{instrument}/candles", _OANDA_READONLY_PATHS)
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
    # 3mo so equities get >200 hourly bars (SMA-200 needs warm-up even on fallback)
    df_1h = yf.download(ticker, period="3mo", interval="1h", progress=False)
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
    if allow_live and inst is None and not ticker.endswith("=X"):  # ETFs only
        try:
            fetch_n = count * 4 if tf == "4H" else count * 5 if tf == "1W" else count
            df = wb_bars(ticker, _WB_CHART_TF[tf], fetch_n)
            if not df.empty:
                if tf == "4H":
                    df = df.resample("4h").agg(_OHLC_AGG).dropna()
                elif tf == "1W":
                    df = df.resample("W").agg(_OHLC_AGG).dropna()
                return df.tail(count), "Webull (live)"
        except Exception as e:
            print(f"Webull chart fetch failed for {ticker} {tf} ({e}); falling back to Yahoo")
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
    if allow_live and inst is None and not ticker.endswith("=X"):  # ETFs only
        try:
            df_1h = wb_bars(ticker, "M60", 720)
            df_1d = wb_bars(ticker, "D", 180)
            if len(df_1h) >= 250 and not df_1d.empty:  # enough for SMA 200
                return df_1h, df_1d, "Webull (live)"
        except Exception as e:
            print(f"Webull bars failed for {ticker} ({e}); falling back to Yahoo")
    df_1h, df_1d = _yf_pair(ticker)
    return df_1h, df_1d, "Yahoo (delayed)"


# Damodaran-style synthetic rating: interest-coverage ratio -> (rating, typical
# spread over treasuries, %). Approximate large-cap table; NOT an agency rating.
_SYNTH_RATING = [
    (8.5, "AAA", 0.63), (6.5, "AA", 0.78), (5.5, "A+", 0.98), (4.25, "A", 1.08),
    (3.0, "A-", 1.22), (2.5, "BBB", 1.56), (2.25, "BB+", 2.00), (2.0, "BB", 2.40),
    (1.75, "B+", 3.51), (1.5, "B", 4.21), (1.25, "B-", 5.15), (0.8, "CCC", 8.20),
    (0.65, "CC", 8.64), (0.2, "C", 11.34), (float("-inf"), "D", 15.12),
]


def _synthetic_rating(icr):
    for floor, rating, spread in _SYNTH_RATING:
        if icr >= floor:
            return rating, spread
    return "D", 15.12


def us10y_yield():
    """Current US 10-year treasury yield in percent (^TNX), or None."""
    try:
        df = yf.download("^TNX", period="5d", interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        y = float(df["Close"].dropna().iloc[-1])
        return y / 10 if y > 20 else y  # guard against x10-scaled quotes
    except Exception as e:
        print(f"10Y fetch failed: {e}")
        return None


_fund_cache = {}          # symbol -> (fetched_at, data)
_FUND_TTL = 6 * 3600
_prewarm_started = False


def prewarm_fundamentals(symbols):
    """Fill the fundamentals cache in a background daemon thread (one symbol
    per second, gentle on Yahoo). Safe to call repeatedly; starts once."""
    global _prewarm_started
    if _prewarm_started:
        return
    _prewarm_started = True

    def _run():
        time.sleep(45)  # let the first page render win the bandwidth race
        for s in symbols:
            try:
                equity_fundamentals(s)
            except Exception as e:
                print(f"prewarm {s} failed: {e}")
            time.sleep(1)
        print(f"fundamentals prewarm complete: {len(_fund_cache)} symbols cached")

    threading.Thread(target=_run, daemon=True, name="fund-prewarm").start()


def equity_fundamentals(symbol):
    """Key fundamentals via yfinance .info (None on failure). Cached in-module
    for 6h; the app prewarms this cache in the background at startup."""
    hit = _fund_cache.get(symbol)
    if hit and time.time() - hit[0] < _FUND_TTL:
        return hit[1]
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as e:
        print(f"fundamentals fetch failed for {symbol}: {e}")
        return None
    if not info.get("marketCap") and not info.get("totalAssets"):
        return None
    g = info.get
    # Credit quality: interest coverage -> synthetic rating + typical spread
    icr = rating = spread = None
    try:
        inc = yf.Ticker(symbol).income_stmt
        ebit = interest = None
        for row in ("EBIT", "Operating Income"):
            if row in inc.index:
                ebit = float(inc.loc[row].dropna().iloc[0]); break
        if "Interest Expense" in inc.index:
            iv = inc.loc["Interest Expense"].dropna()
            interest = abs(float(iv.iloc[0])) if len(iv) else None
        if ebit is not None:
            if not interest:  # no/negligible interest expense
                if (g("totalDebt") or 0) < 0.05 * (g("marketCap") or 1):
                    icr, (rating, spread) = float("inf"), ("AAA (net cash)", 0.63)
            else:
                icr = ebit / interest
                rating, spread = _synthetic_rating(icr)
    except Exception as e:
        print(f"credit calc failed for {symbol}: {e}")
    result = {
        "icr": icr, "credit_rating": rating, "credit_spread": spread,
        "name": g("shortName") or symbol,
        "is_fund": bool(g("totalAssets")) and not g("totalDebt"),
        "market_cap": g("marketCap") or g("totalAssets"),
        "pe": g("trailingPE"), "fwd_pe": g("forwardPE"),
        "ps": g("priceToSalesTrailing12Months"),
        "total_debt": g("totalDebt"), "debt_to_equity": g("debtToEquity"),
        "total_cash": g("totalCash"), "fcf": g("freeCashflow"),
        "profit_margin": g("profitMargins"), "rev_growth": g("revenueGrowth"),
        "div_yield": g("dividendYield"), "beta": g("beta"),
        "wk52_low": g("fiftyTwoWeekLow"), "wk52_high": g("fiftyTwoWeekHigh"),
    }
    _fund_cache[symbol] = (time.time(), result)
    return result


def live_fx_price(ticker):
    """Latest 1-minute candle close from OANDA — effectively the live price.
    Returns None without a token or for non-FX tickers."""
    inst = OANDA_INSTRUMENTS.get(ticker)
    if not (inst and _oanda_token()):
        return None
    try:
        df = _oanda_candles(inst, "M1", 1)
        if not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception as e:
        print(f"OANDA live price failed for {inst}: {e}")
    return None


def oanda_account():
    """Read-only account summary + open trades for every OANDA account the
    token can see. Returns a list of dicts, or None when no token is set."""
    if not _oanda_token():
        return None
    base = _oanda_base()
    h = {"Authorization": f"Bearer {_oanda_token()}"}
    out = []
    try:
        _assert_readonly("/v3/accounts", _OANDA_READONLY_PATHS)
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


_WB_CHART_TF = {"5m": "M5", "15m": "M15", "1H": "M60", "4H": "M60", "1D": "D", "1W": "D"}
_OHLC_AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}


def _wb_get(key, sec, uri, query=None, retries=1):
    """Signed read-only GET against the Webull OpenAPI, with 429 backoff
    (the options desk polls the same account endpoints; quota is shared)."""
    import time as _time
    import urllib.error
    _assert_readonly(uri, _WEBULL_READONLY_PATHS)
    for attempt in range(retries + 1):
        h = _wb_signed_headers(key, sec, uri, query or {})
        url = f"https://{_WB_HOST}{uri}" + (("?" + urlencode(query)) if query else "")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h, method="GET"), timeout=15) as r:
                return json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                _time.sleep(4 + attempt * 6)
                continue
            raise


def webull_account():
    """Read-only Webull account summary + positions (same endpoints as the
    options-desk adapter). Returns a list of account dicts, or None."""
    key, sec = _wb_load_keys()
    if not key:
        return None
    try:
        subs = _wb_get(key, sec, "/app/subscriptions/list")
        out, seen = [], set()
        for s in (subs if isinstance(subs, list) else []):
            aid = s.get("account_id")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            bal = _wb_get(key, sec, "/account/balance", {"account_id": aid, "total_asset_currency": "USD"})
            cur = (bal.get("account_currency_assets") or [{}])[0]
            raw = _wb_get(key, sec, "/account/positions", {"account_id": aid})
            holdings = raw.get("holdings", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])

            def _num(x):
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return 0.0

            rows = [{
                "symbol": p.get("symbol", "") + (" (opt)" if p.get("instrument_type") == "OPTION" else ""),
                "qty": _num(p.get("qty") or p.get("quantity")),
                "avg cost": round(_num(p.get("unit_cost") or p.get("cost_price")), 4),
                "last": round(_num(p.get("last_price")), 4),
                "value": round(_num(p.get("market_value")), 2),
                "unrealized P/L": round(_num(p.get("unrealized_profit_loss")), 2),
                "day P/L": round(_num(p.get("day_profit_loss")), 2),
            } for p in holdings]
            out.append({
                "id": str(aid),
                "type": "",
                "net_liq": _num(bal.get("total_net_liquidation_value") or cur.get("net_liquidation_value")),
                "cash": _num(bal.get("total_cash_balance") or cur.get("cash_balance")),
                "buying_power": _num(cur.get("option_buying_power") or cur.get("margin_power") or cur.get("day_buying_power")),
                "upl": round(sum(r["unrealized P/L"] for r in rows), 2),
                "day_pl": round(sum(r["day P/L"] for r in rows), 2),
                "positions": rows,
            })
        return out or None
    except Exception as e:
        print(f"Webull account fetch failed: {e}")
        return None


_wb_category_cache = {}  # symbol -> "US_STOCK" | "US_ETF", learned on first success


def wb_bars(symbol, timespan, count):
    """Webull OpenAPI candles (real-time, includes pre/after-market) as a
    DataFrame. Tries US_STOCK then US_ETF and remembers which one the symbol
    is. Empty frame when credentials or data are unavailable."""
    key, sec = _wb_load_keys()
    if not key:
        return pd.DataFrame()
    uri = "/market-data/bars"
    _assert_readonly(uri, _WEBULL_READONLY_PATHS)
    cats = [_wb_category_cache[symbol]] if symbol in _wb_category_cache else ["US_STOCK", "US_ETF"]
    for cat in cats:
        query = {"symbol": symbol, "category": cat, "timespan": timespan, "count": str(min(int(count), 1200))}
        try:
            # fail fast (no 429 backoff sleeps): bars must not stall page renders;
            # the caller falls back to Yahoo instead
            h = _wb_signed_headers(key, sec, uri, query)
            url = f"https://{_WB_HOST}{uri}?" + urlencode(query)
            with urllib.request.urlopen(urllib.request.Request(url, headers=h, method="GET"), timeout=8) as r:
                data = json.loads(r.read().decode() or "null")
        except Exception:
            continue
        rows = [{"Time": b["time"], "Open": float(b["open"]), "High": float(b["high"]),
                 "Low": float(b["low"]), "Close": float(b["close"]), "Volume": float(b.get("volume") or 0)}
                for b in (data if isinstance(data, list) else [])]
        if rows:
            _wb_category_cache[symbol] = cat
            df = pd.DataFrame(rows)
            df["Time"] = pd.to_datetime(df["Time"], format="ISO8601", utc=True)
            return df.set_index("Time").sort_index()
    return pd.DataFrame()


def live_etf_price(symbol):
    """Real-time last price for a stock/ETF via Webull OpenAPI (includes
    pre/after market). Returns None when credentials or data are unavailable."""
    try:
        df = wb_bars(symbol, "M1", 2)
        if not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception as e:
        print(f"Webull live price failed for {symbol}: {e}")
    return None
