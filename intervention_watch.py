"""Intervention tripwire: detects intervention-scale bursts in yen pairs.

Signature calibrated on the Jul 31 2026 US/Japan operation (hourly bars ran
9x normal tick volume with 150-190 pip ranges): we alert when any rolling
5-minute window shows BOTH
  - tick volume >= 6x the same-hour norm, and
  - price range >= 35 pips.
Volume alone is positioning; violence alone is news; intervention is both.

Stdlib + urllib only (no pandas) so CI runs are cheap. Read-only: only the
OANDA candles endpoint is called. One alert per pair per 6h (state file).

Env: OANDA_TOKEN (or ~/Downloads/oanda.txt), NTFY_TOPIC (optional override).
"""
import json
import os
import re
import statistics
import sys
import time
import urllib.request

TOPIC = os.environ.get("NTFY_TOPIC", "fx-terminal-bq7k2m9xp4").strip()
PAIRS = {"USD_JPY": 100, "EUR_JPY": 100}  # instrument -> pips per unit price
VOL_RATIO_MIN = 6.0
RANGE_PIPS_MIN = 35.0
COOLDOWN_S = 6 * 3600
STATE_FILE = os.environ.get(
    "IW_STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "intervention_state.json"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def token():
    tok = os.environ.get("OANDA_TOKEN", "").strip()
    if tok and "PASTE" not in tok.upper():
        return tok
    for p in ("~/Downloads/oanda.txt", "~/Downloads/OANDA.txt"):
        try:
            m = re.search(r"[A-Za-z0-9\-]{30,}", open(os.path.expanduser(p)).read())
            if m:
                return m.group(0)
        except Exception:
            continue
    return ""


def get_json(url, tok):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def candles(base, tok, inst, gran, count):
    url = f"{base}/v3/instruments/{inst}/candles?granularity={gran}&count={count}&price=M"
    return get_json(url, tok)["candles"]


def send_alert(title, message):
    if not TOPIC:
        print("no topic; alert suppressed:", title)
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}", data=message.encode(),
        headers={"Title": title, "Priority": "urgent", "Tags": "zap"})
    with urllib.request.urlopen(req, timeout=20) as r:
        print("ntfy:", r.status)


def main():
    tok = token()
    if not tok:
        sys.exit("no OANDA token available")
    base = None
    for host in ("https://api-fxpractice.oanda.com", "https://api-fxtrade.oanda.com"):
        try:
            candles(host, tok, "USD_JPY", "M1", 1)
            base = host
            break
        except Exception:
            continue
    if not base:
        sys.exit("OANDA unreachable with this token")

    try:
        state = json.load(open(STATE_FILE))
    except Exception:
        state = {}
    now = time.time()

    for inst, pipx in PAIRS.items():
        m1 = [c for c in candles(base, tok, inst, "M1", 30)]
        h1 = candles(base, tok, inst, "H1", 720)
        cur_hour = int(m1[-1]["time"][11:13])
        same_hour = [float(c["volume"]) for c in h1 if int(c["time"][11:13]) == cur_hour]
        norm_min = (statistics.mean(same_hour) / 60) if same_hour else 100.0

        best = {"ratio": 0.0, "range": 0.0, "move": 0.0, "t": ""}
        tail = m1[-20:]
        for i in range(len(tail) - 4):
            w = tail[i:i + 5]
            vol = sum(float(c["volume"]) for c in w)
            hi = max(float(c["mid"]["h"]) for c in w)
            lo = min(float(c["mid"]["l"]) for c in w)
            ratio = vol / (5 * norm_min) if norm_min else 0
            rng = (hi - lo) * pipx
            if ratio > best["ratio"]:
                move = (float(w[-1]["mid"]["c"]) - float(w[0]["mid"]["o"])) * pipx
                best = {"ratio": ratio, "range": rng, "move": move, "t": w[-1]["time"][11:16]}

        pair = inst.replace("_", "/")
        trig = best["ratio"] >= VOL_RATIO_MIN and best["range"] >= RANGE_PIPS_MIN
        print(f"{pair}: peak 5-min window {best['t']}Z vol={best['ratio']:.1f}x norm "
              f"range={best['range']:.0f}p move={best['move']:+.0f}p -> "
              f"{'TRIGGER' if trig else 'quiet'}")
        if trig and now - state.get(inst, 0) > COOLDOWN_S:
            send_alert(f"INTERVENTION WATCH {pair}",
                       f"Intervention-scale burst detected\n"
                       f"5-min window ending {best['t']}Z\n"
                       f"Volume: {best['ratio']:.1f}x normal\n"
                       f"Range: {best['range']:.0f} pips | Move: {best['move']:+.0f} pips\n"
                       f"Check open yen positions NOW.")
            state[inst] = now

    json.dump(state, open(STATE_FILE, "w"))


if __name__ == "__main__":
    main()
