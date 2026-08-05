# Pro FX & ETF Terminal (EURUSD edition)

n> **Public repo note:** push alerts require setting an `NTFY_TOPIC` (GitHub Actions secret, Streamlit secret, or env var). No topic is baked into the code — pick a private, unguessable name.
Quantitative FX + ETF scanning dashboard + backtester (Streamlit), migrated from a Gemini web-chat project on 2026-08-04.

## What it does
- **Multi-asset routing** — a tailored strategy per instrument:
  - `EUR/GBP` — Mean Reversion (fractal swing-low floors + bullish engulfing)
  - `GBP/JPY`, `USD/JPY` — Trend Following (20/50 SMA crossover)
  - `USD/CAD` — Momentum Breakout (20-period Donchian high breakout, first-bar-only)
  - `SPY`, `QQQ` — Equity Pullback (close below lower Bollinger Band + RSI < 30 + above 200 SMA, first-bar-only)
- **MTF filter** — Daily data gives the macro trend badge; 1-Hour data drives entries.
- **Dynamic risk sizing** — 1–3 star confidence (RSI + 200 SMA confluence) scales risk % between user-set min/max; ATR×1.5 stop; notional sized from NAV.
- **Trade management** — entry, stop, breakeven trigger, TP1 (1.5R, close 50%), TP2 (2.5R).
- **Backtester** — 30-day 1H history: trade count, win rate, profit factor (24-bar exit).
- **Push alerts (ntfy.sh)** — no account or token needed; the secret topic name is the channel. Subscribe in the free ntfy app (or open `https://ntfy.sh/<topic>` in a browser) to receive alerts. Deduped across scanner runs via `alert_state.json`.
- **Volume pane** — ETFs show real exchange volume; spot FX (no centralized volume) shows CME currency-futures volume as an activity proxy (6E/6J/6C).

## Files
| File | Purpose |
|---|---|
| `app.py` | Streamlit dashboard (interactive) |
| `scanner.py` | Headless scanner for scheduled 24/7 alerting |
| `.github/workflows/fx_scan.yml` | Hourly GitHub Actions run of `scanner.py` |

## Run locally
```bash
.venv/Scripts/python -m streamlit run app.py
```
(First time: `python -m venv .venv && .venv/Scripts/pip install -r requirements.txt`)

## 24/7 alerts (GitHub Actions)
The workflow runs `scanner.py` hourly during FX market hours (Sun 21:00 UTC – Fri 21:00 UTC) and pushes alerts to the default ntfy topic baked into the code — no secrets required. Trigger manually anytime from the Actions tab (workflow_dispatch). To use a different topic, add an `NTFY_TOPIC` repository secret (or env var locally).

Alternative (local, no GitHub): schedule `scanner.py` with Windows Task Scheduler using the venv python.

⚠️ The ntfy topic works like a channel password: anyone who knows the name can read alerts. Keep the repo private / rename the topic if it leaks.

## Live data
`data_providers.py` upgrades data sources automatically when credentials exist:
- **FX:** set `OANDA_TOKEN` (free practice account at oanda.com → Manage API Access) → live OANDA candles + real FX tick volume replace delayed Yahoo data and the futures volume proxy. Set locally as an env var / `.streamlit/secrets.toml`, on Streamlit Cloud in app Settings → Secrets, and on GitHub as an `OANDA_TOKEN` Actions secret.
- **ETFs:** real-time last price via Webull OpenAPI — reads `WEBULL_APP_KEY`/`WEBULL_APP_SECRET` env vars or the desk's key file in `~/Downloads`. Bars/signals stay on yfinance history; only the displayed price is live.
Each pane shows its active data source; everything degrades gracefully to yfinance when no credentials are present.

## Notes / known limitations
- Without OANDA, yfinance FX data is delayed/indicative — fine for scanning, not for execution-grade fills.
- The backtester uses a fixed 24-bar exit, not the SL/TP ladder shown in signals — win rate and profit factor measure the raw signal edge, not the managed-trade outcome.
- GitHub Actions cron runs are stateless: unlike the dashboard (which dedupes via `session_state`), a signal that stays active across hours may alert more than once.
