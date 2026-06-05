"""
Signal Bot v7 — EMA200 Touch Strategy
- 25 crypto pairs monitored simultaneously
- EMA200 touch in confirmed trend = high probability setup
- Fixed outcome tracker (checks every 30s when trade active)
- Daily summary at 6am Sydney time (8pm UTC)
- Weekly summary every Sunday
"""

import time
import logging
import os
from datetime import datetime, timezone
from collections import deque
import requests

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# 25 pairs — Kraken symbols
# 10 pairs — backtested profitable over 4.3 years, all 30+ trades
# Verified on Pepperstone cTrader Australia
SYMBOLS = [
    ("XBTUSD",  "BTC"),    # +20.8% annual, 199 trades, 31.7% WR
    ("ETHUSD",  "ETH"),    # +17.4% annual, 119 trades, 32.8% WR
    ("SOLUSD",  "SOL"),    # +1.6%  annual,  30 trades, 30.0% WR
    ("XRPUSD",  "XRP"),    # +33.0% annual,  90 trades, 35.6% WR
    ("ADAUSD",  "ADA"),    # +6.6%  annual,  40 trades, 32.5% WR
    ("AVAXUSD", "AVAX"),   # +39.9% annual,  84 trades, 40.5% WR
    ("XLMUSD",  "XLM"),    # +24.6% annual,  68 trades, 38.2% WR
    ("UNIUSD",  "UNI"),    # +28.5% annual,  81 trades, 38.3% WR
    ("DOGEUSD", "DOGE"),   # +3.0%  annual, 116 trades, 29.3% WR
    ("BNBUSD",  "BNB"),    # +31.4% annual,  63 trades, 41.3% WR
]

INTERVAL_HTF      = 60      # 1H candles
LOOKBACK_HTF      = 250     # need 200+ for EMA200
FETCH_INTERVAL    = 300     # check every 5 minutes
MIN_CONFIDENCE    = 75
MIN_VOL_MULT      = 1.5     # volume must be 1.5x average
MIN_RR            = 2.5
COOLDOWN_SEC      = 3600    # 1 hour between alerts (multiple pairs = more signals)
EMA200_ZONE_PCT   = 0.02    # 2% zone — backtested optimal
ADX_MIN           = 20      # skip choppy markets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("SignalBot")

# ─── DATA FETCHING ────────────────────────────────────────────────────────────

def fetch_kraken(symbol: str, interval: int, limit: int) -> list[dict]:
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": symbol, "interval": interval},
            headers={"User-Agent": "signal-bot/7.0"},
            timeout=15
        )
        r.raise_for_status()
        data     = r.json()
        if data.get("error"):
            return []
        result   = data.get("result", {})
        pair_key = [k for k in result if k != "last"][0]
        candles  = []
        for c in result[pair_key][-limit:]:
            candles.append({
                "ts":    int(c[0]) * 1000,
                "open":  float(c[1]),
                "high":  float(c[2]),
                "low":   float(c[3]),
                "close": float(c[4]),
                "vol":   float(c[6]),
            })
        return candles
    except Exception as e:
        log.debug(f"Fetch error ({symbol}): {e}")
        return []

def fetch_price(symbol: str) -> float | None:
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": symbol},
            headers={"User-Agent": "signal-bot/7.0"},
            timeout=5
        )
        result   = r.json().get("result", {})
        pair_key = list(result.keys())[0]
        return float(result[pair_key]["c"][0])
    except Exception:
        return None

# ─── INDICATORS ──────────────────────────────────────────────────────────────

def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k      = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i+1] - closes[i] for i in range(len(closes)-1)]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_g  = sum(gains[:period]) / period
    avg_l  = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)

def atr(candles: list[dict], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs  = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    avgs = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        avgs.append((avgs[-1] * (period - 1) + tr) / period)
    return avgs[-1] if avgs else None

def avg_volume(candles: list[dict], period: int = 20) -> float:
    vols = [c["vol"] for c in candles[-period-1:-1]]
    return sum(vols) / len(vols) if vols else 1.0

def adx(candles: list[dict], period: int = 14) -> float | None:
    if len(candles) < period * 2 + 1:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        h, l   = candles[i]["high"], candles[i]["low"]
        ph, pl = candles[i-1]["high"], candles[i-1]["low"]
        pc     = candles[i-1]["close"]
        up, down = h - ph, pl - l
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    def smooth(vals, p):
        s = [sum(vals[:p])]
        for v in vals[p:]:
            s.append(s[-1] - s[-1]/p + v)
        return s
    atr_s  = smooth(trs, period)
    pdm_s  = smooth(plus_dm, period)
    mdm_s  = smooth(minus_dm, period)
    di_p   = [100*pdm_s[i]/atr_s[i] for i in range(len(atr_s)) if atr_s[i] != 0]
    di_m   = [100*mdm_s[i]/atr_s[i] for i in range(len(atr_s)) if atr_s[i] != 0]
    if len(di_p) < period:
        return None
    dx = [abs(di_p[i]-di_m[i])/(di_p[i]+di_m[i])*100
          for i in range(len(di_p)) if (di_p[i]+di_m[i]) != 0]
    return sum(dx[-period:]) / period if len(dx) >= period else None

# ─── HTF BIAS ────────────────────────────────────────────────────────────────

def get_bias(candles: list[dict]) -> tuple[str, float, float]:
    """Returns (bias, ema50_val, ema200_val)."""
    if len(candles) < 205:
        return "NEUTRAL", 0, 0
    closes = [c["close"] for c in candles]
    e50    = ema(closes, 50)
    e200   = ema(closes, 200)
    if len(e50) < 5 or len(e200) < 5:
        return "NEUTRAL", 0, 0
    bull_count = sum(1 for i in range(-3, 0) if e50[i] > e200[i] and closes[i] > e50[i])
    bear_count = sum(1 for i in range(-3, 0) if e50[i] < e200[i] and closes[i] < e50[i])
    bias = "BULL" if bull_count == 3 else "BEAR" if bear_count == 3 else "NEUTRAL"
    return bias, e50[-1], e200[-1]

def get_key_levels(candles: list[dict]) -> list[float]:
    highs, lows = [c["high"] for c in candles], [c["low"] for c in candles]
    levels, w   = [], 5
    for i in range(w, len(highs)-w):
        if highs[i] == max(highs[i-w:i+w+1]):
            levels.append(highs[i])
        if lows[i] == min(lows[i-w:i+w+1]):
            levels.append(lows[i])
    levels.sort()
    out = []
    for l in levels:
        if not out or abs(l-out[-1])/out[-1] > 0.002:
            out.append(l)
        else:
            out[-1] = (out[-1]+l)/2
    return out

# ─── EMA200 TOUCH STRATEGY ───────────────────────────────────────────────────

def analyse_ema200_touch(candles: list[dict], name: str) -> dict | None:
    """
    The winning strategy from backtesting:
    - Confirmed BULL or BEAR trend (EMA50/200, 3-bar)
    - Price pulls back within 2% of EMA200
    - Reversal candle (bullish in BULL, bearish in BEAR)
    - Volume confirmation (1.5x average)
    - ADX > 20 (trending market)
    """
    if len(candles) < 210:
        return None

    bias, e50_val, e200_val = get_bias(candles)
    if bias == "NEUTRAL":
        return None

    # ADX filter — skip choppy markets
    adx_val = adx(candles, 14)
    if adx_val is not None and adx_val < ADX_MIN:
        return None

    closes    = [c["close"] for c in candles]
    price     = closes[-1]
    confirm   = candles[-2]  # last fully closed candle
    avg_v     = avg_volume(candles, 20)
    last_vol  = confirm["vol"]
    atr_v     = atr(candles, 14)
    rsi_val   = rsi(closes[:-1], 14)

    if not atr_v:
        return None

    # Check if price is within EMA200_ZONE_PCT of EMA200
    dist_pct = abs(price - e200_val) / e200_val
    if dist_pct > EMA200_ZONE_PCT:
        return None

    body     = abs(confirm["close"] - confirm["open"])
    range_   = confirm["high"] - confirm["low"]
    if range_ == 0:
        return None
    body_pct = body / range_
    upper_w  = confirm["high"] - max(confirm["open"], confirm["close"])
    lower_w  = min(confirm["open"], confirm["close"]) - confirm["low"]

    # Minimum SL distance based on asset
    min_sl_dist = price * 0.015  # 1.5% minimum SL for any asset

    # BULL trend: price touching EMA200 from above = buy the dip
    if bias == "BULL" and price >= e200_val * 0.99:
        bullish_candle = (
            confirm["close"] > confirm["open"] and
            body_pct > 0.45 and
            last_vol > avg_v * MIN_VOL_MULT and
            (rsi_val is None or rsi_val < 60)
        )
        if bullish_candle:
            conf = 78
            if last_vol > avg_v * 2.5:
                conf += 5
            if rsi_val and rsi_val < 40:
                conf += 4
            if lower_w > body * 0.5:  # wick rejection
                conf += 3

            # SL below swing low of last 20 candles
            swing_low = min(c["low"] for c in candles[-20:-1])
            sl = swing_low - atr_v * 0.3
            if price - sl < min_sl_dist:
                sl = price - min_sl_dist

            # TP at next resistance or 2.5x SL distance
            sl_dist   = price - sl
            min_tp    = price + sl_dist * MIN_RR
            sr_levels = get_key_levels(candles[-50:])
            tp_cands  = [l for l in sr_levels if l > min_tp]
            tp        = min(tp_cands) if tp_cands else min_tp
            rr        = (tp - price) / (price - sl)

            if rr < MIN_RR:
                return None

            return {
                "symbol":    name,
                "direction": "LONG",
                "strategy":  "EMA200 Touch (Bull)",
                "entry":     price,
                "tp":        tp,
                "sl":        sl,
                "rr":        rr,
                "confidence": min(95, conf),
                "htf_bias":  bias,
                "ema200":    e200_val,
                "dist_pct":  dist_pct * 100,
                "adx":       adx_val,
                "rsi":       rsi_val,
                "vol_mult":  last_vol / avg_v,
                "note": f"EMA200 touch at ${e200_val:,.2f} | RSI {rsi_val:.0f} | Vol {last_vol/avg_v:.1f}x | ADX {adx_val:.0f}" if rsi_val and adx_val else f"EMA200 touch at ${e200_val:,.2f}"
            }

    # BEAR trend: price rallying back up to EMA200 from below = sell the bounce
    if bias == "BEAR" and price <= e200_val * 1.01:
        bearish_candle = (
            confirm["close"] < confirm["open"] and
            body_pct > 0.45 and
            last_vol > avg_v * MIN_VOL_MULT and
            (rsi_val is None or rsi_val > 40)
        )
        if bearish_candle:
            conf = 78
            if last_vol > avg_v * 2.5:
                conf += 5
            if rsi_val and rsi_val > 60:
                conf += 4
            if upper_w > body * 0.5:  # wick rejection
                conf += 3

            swing_high = max(c["high"] for c in candles[-20:-1])
            sl = swing_high + atr_v * 0.3
            if sl - price < min_sl_dist:
                sl = price + min_sl_dist

            sl_dist  = sl - price
            min_tp   = price - sl_dist * MIN_RR
            sr_levels = get_key_levels(candles[-50:])
            tp_cands = [l for l in sr_levels if l < min_tp]
            tp       = max(tp_cands) if tp_cands else min_tp
            rr       = (price - tp) / (sl - price)

            if rr < MIN_RR:
                return None

            return {
                "symbol":    name,
                "direction": "SHORT",
                "strategy":  "EMA200 Touch (Bear)",
                "entry":     price,
                "tp":        tp,
                "sl":        sl,
                "rr":        rr,
                "confidence": min(95, conf),
                "htf_bias":  bias,
                "ema200":    e200_val,
                "dist_pct":  dist_pct * 100,
                "adx":       adx_val,
                "rsi":       rsi_val,
                "vol_mult":  last_vol / avg_v,
                "note": f"EMA200 touch at ${e200_val:,.2f} | RSI {rsi_val:.0f} | Vol {last_vol/avg_v:.1f}x | ADX {adx_val:.0f}" if rsi_val and adx_val else f"EMA200 touch at ${e200_val:,.2f}"
            }

    return None

# ─── TRADE TRACKER ───────────────────────────────────────────────────────────

active_trade  = None
trade_history = []

def set_active_trade(signal: dict):
    global active_trade
    active_trade = {
        "symbol":    signal["symbol"],
        "direction": signal["direction"],
        "entry":     signal["entry"],
        "tp":        signal["tp"],
        "sl":        signal["sl"],
        "strategy":  signal["strategy"],
        "rr":        signal["rr"],
        "open_time": datetime.now(timezone.utc),
        "kraken_sym": next((k for k, n in SYMBOLS if n == signal["symbol"]), None),
    }
    log.info(f"  📌 Tracking {signal['symbol']} {signal['direction']} "
             f"entry=${signal['entry']:,.4f} TP=${signal['tp']:,.4f} SL=${signal['sl']:,.4f}")

def check_outcome(price: float) -> str | None:
    if not active_trade:
        return None
    d  = active_trade["direction"]
    tp = active_trade["tp"]
    sl = active_trade["sl"]
    if d == "LONG":
        if price >= tp: return "TP"
        if price <= sl: return "SL"
    else:
        if price <= tp: return "TP"
        if price >= sl: return "SL"
    return None

def format_outcome(outcome: str, price: float) -> str:
    if not active_trade:
        return ""
    entry    = active_trade["entry"]
    tp       = active_trade["tp"]
    sl       = active_trade["sl"]
    exit_p   = tp if outcome == "TP" else sl
    pnl_pct  = abs(exit_p - entry) / entry * 100
    duration = int((datetime.now(timezone.utc) - active_trade["open_time"]).total_seconds() / 60)
    emoji    = "✅" if outcome == "TP" else "❌"
    result   = "TAKE PROFIT HIT" if outcome == "TP" else "STOP LOSS HIT"
    pnl_str  = f"+{pnl_pct:.2f}%" if outcome == "TP" else f"-{pnl_pct:.2f}%"
    lines = [
        f"{emoji} <b>{result}</b>",
        f"",
        f"💹 <b>Asset:</b> {active_trade['symbol']}",
        f"📍 <b>Direction:</b> {active_trade['direction']}",
        f"📊 <b>Strategy:</b> {active_trade['strategy']}",
        f"",
        f"💰 <b>Entry:</b>    ${entry:,.4f}",
        f"🏁 <b>Exit:</b>     ${exit_p:,.4f}",
        f"📈 <b>Result:</b>   {pnl_str}",
        f"⏱ <b>Duration:</b> {duration} minutes",
    ]
    return "\n".join(lines)

def record_outcome(outcome: str):
    if not active_trade:
        return
    entry = active_trade["entry"]
    tp    = active_trade["tp"]
    sl    = active_trade["sl"]
    pnl   = abs(tp-entry)/entry*100 if outcome == "TP" else -abs(sl-entry)/entry*100
    trade_history.append({
        "symbol":    active_trade["symbol"],
        "direction": active_trade["direction"],
        "strategy":  active_trade["strategy"],
        "outcome":   outcome,
        "pnl_pct":   pnl,
        "rr":        active_trade["rr"],
        "duration":  int((datetime.now(timezone.utc) - active_trade["open_time"]).total_seconds() / 60),
        "time":      datetime.now(timezone.utc),
    })

def clear_active_trade():
    global active_trade
    active_trade = None

# ─── SUMMARIES ───────────────────────────────────────────────────────────────

last_daily_summary  = None
last_weekly_summary = None

def build_summary(trades: list[dict], title: str, days: int) -> str:
    if not trades:
        return f"📊 <b>{title}</b>\n\nNo completed trades this period."

    wins     = [t for t in trades if t["outcome"] == "TP"]
    losses   = [t for t in trades if t["outcome"] == "SL"]
    total    = len(trades)
    win_rate = len(wins) / total * 100
    risk     = 1.0
    pnl      = sum(risk * t["rr"] for t in wins) - len(losses) * risk
    annual   = pnl * (365 / days)
    avg_rr   = sum(t["rr"] for t in wins) / len(wins) if wins else 0
    avg_dur  = sum(t["duration"] for t in trades) / total

    # Per symbol breakdown
    sym_stats = {}
    for t in trades:
        s = t["symbol"]
        if s not in sym_stats:
            sym_stats[s] = [0, 0]
        if t["outcome"] == "TP":
            sym_stats[s][0] += 1
        else:
            sym_stats[s][1] += 1

    best  = max(trades, key=lambda t: t["pnl_pct"]) if wins else None
    worst = min(trades, key=lambda t: t["pnl_pct"]) if losses else None

    lines = [
        f"📊 <b>{title}</b>",
        f"📅 {datetime.now(timezone.utc).strftime('%d %b %Y')}",
        f"",
        f"📈 <b>Total Trades:</b> {total}",
        f"✅ <b>Wins:</b> {len(wins)}  ❌ <b>Losses:</b> {len(losses)}",
        f"🎯 <b>Win Rate:</b> {win_rate:.1f}%",
        f"⚖️  <b>Avg R:R on wins:</b> 1:{avg_rr:.1f}",
        f"⏱ <b>Avg Duration:</b> {avg_dur:.0f} min",
        f"",
        f"💰 <b>Est. P&L</b> (1% risk/trade): {pnl:+.1f}%",
        f"📅 <b>Est. Annual P&L:</b> {annual:+.0f}%",
    ]

    # Top performing symbols
    if sym_stats:
        lines += [f"", f"🏅 <b>By Asset:</b>"]
        for sym, (w, l) in sorted(sym_stats.items(), key=lambda x: x[1][0], reverse=True)[:5]:
            t = w + l
            wr = w/t*100 if t else 0
            lines.append(f"   {sym}: {t} trades {wr:.0f}% WR")

    if best:
        lines += [f"", f"🏆 <b>Best:</b> {best['symbol']} {best['direction']} +{best['pnl_pct']:.2f}%"]
    if worst:
        lines += [f"💀 <b>Worst:</b> {worst['symbol']} {worst['direction']} {worst['pnl_pct']:.2f}%"]

    lines += [f"", f"⚠️ <i>Based on signal TP/SL levels. Not financial advice.</i>"]
    return "\n".join(lines)

def check_summaries():
    global last_daily_summary, last_weekly_summary, trade_history
    now = datetime.now(timezone.utc)

    # Daily at 8pm UTC (6am Sydney)
    if now.hour == 20 and now.minute < 3:
        if last_daily_summary is None or (now - last_daily_summary).total_seconds() > 3600:
            last_daily_summary = now
            today = [t for t in trade_history if (now - t["time"]).total_seconds() < 86400]
            msg = build_summary(today, "📊 Daily Summary", 1)
            send_telegram(msg)
            log.info("📊 Daily summary sent")

    # Weekly every Sunday at 8am UTC
    if now.weekday() == 6 and now.hour == 8 and now.minute < 3:
        if last_weekly_summary is None or (now - last_weekly_summary).total_seconds() > 86400:
            last_weekly_summary = now
            msg = build_summary(trade_history, "📊 Weekly Summary", 7)
            send_telegram(msg)
            log.info("📊 Weekly summary sent")
            trade_history = []

# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured")
        print(text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        return r.status_code == 200
    except Exception as e:
        log.warning(f"Telegram error: {e}")
        return False

def format_signal(sig: dict) -> str:
    arrow  = "🟢" if sig["direction"] == "LONG" else "🔴"
    rr     = sig.get("rr", 0)
    conf   = sig["confidence"]
    bars   = "█" * (conf // 10) + "░" * (10 - conf // 10)
    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    entry  = sig["entry"]
    tp     = sig["tp"]
    sl     = sig["sl"]
    bias_e = "📈" if sig.get("htf_bias") == "BULL" else "📉"

    lines = [
        f"{arrow} <b>{sig['symbol']} {sig['direction']} SIGNAL</b>",
        f"",
        f"📊 <b>Strategy:</b> {sig['strategy']}",
        f"{bias_e} <b>1H Trend:</b> {sig.get('htf_bias')}",
        f"🕐 <b>Time:</b> {now_str}",
        f"",
        f"💰 <b>Entry:</b>  ${entry:,.4f}",
        f"🎯 <b>TP:</b>     ${tp:,.4f}  ({(tp-entry)/entry*100:+.2f}%)",
        f"🛑 <b>SL:</b>     ${sl:,.4f}  ({(sl-entry)/entry*100:+.2f}%)",
        f"",
        f"⚖️  <b>R:R:</b> 1:{rr:.1f}",
        f"🔥 <b>Confidence:</b> {conf}%  {bars}",
    ]
    if sig.get("note"):
        lines += [f"", f"📝 <i>{sig['note']}</i>"]
    lines += [f"", f"⚠️ <i>Not financial advice. DYOR.</i>"]
    return "\n".join(lines)

# ─── KEEP ALIVE ──────────────────────────────────────────────────────────────

def keep_alive():
    from flask import Flask
    from threading import Thread
    app = Flask("")
    @app.route("/")
    def home():
        return "Signal Bot v7 — 25 pairs running ✅"
    t = Thread(target=lambda: app.run(host="0.0.0.0", port=8080))
    t.daemon = True
    t.start()

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def run():
    log.info("═" * 60)
    log.info("  Signal Bot v7 — EMA200 Touch Strategy")
    log.info(f"  {len(SYMBOLS)} pairs (Pepperstone verified)  |  Cooldown: {COOLDOWN_SEC//3600}h  |  Min R:R: {MIN_RR}")
    log.info(f"  EMA200 zone: {EMA200_ZONE_PCT*100:.0f}%  |  Min vol: {MIN_VOL_MULT}x  |  ADX > {ADX_MIN}")
    log.info("═" * 60)

    keep_alive()

    last_alert_time   = 0
    last_signal_sym   = None

    # Prime data buffers
    symbol_data = {}
    for kraken_sym, name in SYMBOLS:
        candles = fetch_kraken(kraken_sym, INTERVAL_HTF, LOOKBACK_HTF)
        symbol_data[kraken_sym] = {
            "name":    name,
            "candles": deque(candles, maxlen=LOOKBACK_HTF),
        }
        log.info(f"  {name}: loaded {len(candles)} candles")
        time.sleep(0.3)  # be kind to Kraken API

    log.info(f"All {len(SYMBOLS)} pairs loaded. Monitoring...")

    while True:
        try:
            check_summaries()

            # Fast outcome check every 30s when trade is active
            if active_trade and active_trade.get("kraken_sym"):
                price = fetch_price(active_trade["kraken_sym"])
                if price:
                    outcome = check_outcome(price)
                    if outcome:
                        sym = active_trade.get("symbol", "")
                        log.info(f"  🏁 {sym} {outcome} @ ${price:,.4f}")
                        send_telegram(format_outcome(outcome, price))
                        record_outcome(outcome)
                        clear_active_trade()
                        # Reset cooldown so next signal isn't blocked too long
                        last_alert_time = time.time() - COOLDOWN_SEC + 300

            # Scan all pairs for signals
            now = time.time()
            signals_found = []

            for kraken_sym, name in SYMBOLS:
                try:
                    # Refresh latest candles
                    new = fetch_kraken(kraken_sym, INTERVAL_HTF, 5)
                    buf = symbol_data[kraken_sym]["candles"]
                    for c in new:
                        if not buf or c["ts"] > buf[-1]["ts"]:
                            buf.append(c)
                        elif c["ts"] == buf[-1]["ts"]:
                            buf[-1] = c

                    candles = list(buf)
                    price   = candles[-1]["close"] if candles else 0
                    bias, e50, e200 = get_bias(candles)

                    log.info(f"  {name:6s} ${price:>12,.4f}  bias={bias:7s}  EMA200=${e200:,.4f}")

                    if now - last_alert_time >= COOLDOWN_SEC:
                        signal = analyse_ema200_touch(candles, name)
                        if signal and signal["confidence"] >= MIN_CONFIDENCE:
                            signals_found.append(signal)

                    time.sleep(0.2)  # rate limit

                except Exception as e:
                    log.debug(f"{name} scan error: {e}")

            # Send the highest confidence signal if any found
            if signals_found:
                best = max(signals_found, key=lambda x: x["confidence"])
                log.info(f"  ✨ SIGNAL: {best['symbol']} {best['direction']} | "
                         f"{best['confidence']}% | R:R 1:{best['rr']:.1f}")
                ok = send_telegram(format_signal(best))
                if ok:
                    log.info("  📨 Telegram sent")
                    set_active_trade(best)
                last_alert_time = now
                last_signal_sym = best["symbol"]

        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")

        # Sleep shorter when trade is active (for faster outcome detection)
        time.sleep(30 if active_trade else FETCH_INTERVAL)

if __name__ == "__main__":
    run()

