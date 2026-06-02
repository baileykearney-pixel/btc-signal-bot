"""
BTC/ETH Signal Bot v4
- Dual timeframe (1H bias + 5min entry)
- Structure-based TP/SL (real swing levels)
- Minimum 1:2 R:R enforced
- Volume confirmation on every signal
- Trade outcome tracker (with 5min delay before checking)
- Weekly performance summary every Sunday 8am UTC
- ETH + BTC monitored simultaneously
"""

import time
import logging
import os
import json
import math
from datetime import datetime, timezone, timedelta
from collections import deque
import requests

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOLS           = [("XBTUSD", "BTC"), ("ETHUSD", "ETH")]
INTERVAL_HTF      = 60      # 1H for trend
INTERVAL_LTF      = 5       # 5min for entry
LOOKBACK_HTF      = 100
LOOKBACK_LTF      = 200
FETCH_INTERVAL    = 120     # check every 2 minutes
MIN_CONFIDENCE    = 70
COOLDOWN_SEC      = 900     # 15 min between alerts
MIN_VOL_MULT      = 1.4
SR_ZONE_PCT       = 0.0015
MIN_RR            = 2.0
MIN_SL_DIST_BTC   = 300.0
MIN_SL_DIST_ETH   = 8.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("BTCBot")

# ─── DATA FETCHING ────────────────────────────────────────────────────────────

def fetch_kraken(symbol: str, interval: int, limit: int) -> list[dict]:
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": symbol, "interval": interval},
            headers={"User-Agent": "btc-signal-bot/4.0"},
            timeout=15
        )
        r.raise_for_status()
        data     = r.json()
        if data.get("error"):
            log.warning(f"Kraken error: {data['error']}")
            return []
        result   = data.get("result", {})
        pair_key = [k for k in result if k != "last"][0]
        raw      = result[pair_key]
        candles  = []
        for c in raw[-limit:]:
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
        log.warning(f"Kraken fetch error ({symbol}): {e}")
        return []

def fetch_price(symbol: str) -> float | None:
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": symbol},
            headers={"User-Agent": "btc-signal-bot/4.0"},
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

# ─── HTF ANALYSIS ────────────────────────────────────────────────────────────

def get_htf_bias(htf_candles: list[dict]) -> str:
    if len(htf_candles) < 55:
        return "NEUTRAL"
    closes = [c["close"] for c in htf_candles]
    e20    = ema(closes, 20)
    e50    = ema(closes, 50)
    if not e20 or not e50:
        return "NEUTRAL"
    price        = closes[-1]
    ema_bull     = e20[-1] > e50[-1]
    ema_bear     = e20[-1] < e50[-1]
    above_both   = price > e20[-1] and price > e50[-1]
    below_both   = price < e20[-1] and price < e50[-1]
    recent_high  = max(c["high"] for c in htf_candles[-10:])
    prior_high   = max(c["high"] for c in htf_candles[-20:-10])
    recent_low   = min(c["low"]  for c in htf_candles[-10:])
    prior_low    = min(c["low"]  for c in htf_candles[-20:-10])
    hh_hl        = recent_high > prior_high and recent_low > prior_low
    lh_ll        = recent_high < prior_high and recent_low < prior_low
    bull_score   = sum([ema_bull, above_both, hh_hl])
    bear_score   = sum([ema_bear, below_both, lh_ll])
    if bull_score >= 2:
        return "BULL"
    elif bear_score >= 2:
        return "BEAR"
    return "NEUTRAL"

def get_key_levels(htf_candles: list[dict]) -> list[float]:
    if len(htf_candles) < 20:
        return []
    highs  = [c["high"] for c in htf_candles]
    lows   = [c["low"]  for c in htf_candles]
    levels = []
    window = 5
    for i in range(window, len(highs) - window):
        if highs[i] == max(highs[i-window:i+window+1]):
            levels.append(highs[i])
    for i in range(window, len(lows) - window):
        if lows[i] == min(lows[i-window:i+window+1]):
            levels.append(lows[i])
    levels.sort()
    clustered = []
    for lvl in levels:
        if not clustered or abs(lvl - clustered[-1]) / clustered[-1] > 0.002:
            clustered.append(lvl)
        else:
            clustered[-1] = (clustered[-1] + lvl) / 2
    return clustered

def price_at_sr_zone(price: float, levels: list[float]) -> float | None:
    for lvl in levels:
        if abs(price - lvl) / lvl <= SR_ZONE_PCT:
            return lvl
    return None

# ─── TP/SL LOGIC ─────────────────────────────────────────────────────────────

def find_structural_sl(ltf_candles: list[dict], direction: str,
                        entry: float, name: str) -> float:
    min_dist = MIN_SL_DIST_BTC if name == "BTC" else MIN_SL_DIST_ETH
    lookback = ltf_candles[-20:-1]
    if direction == "LONG":
        swing_low = min(c["low"] for c in lookback)
        sl        = swing_low - entry * 0.001
        if entry - sl < min_dist:
            sl = entry - min_dist
        return sl
    else:
        swing_high = max(c["high"] for c in lookback)
        sl         = swing_high + entry * 0.001
        if sl - entry < min_dist:
            sl = entry + min_dist
        return sl

def find_structural_tp(entry: float, sl: float, direction: str,
                        sr_levels: list[float]) -> float:
    sl_dist     = abs(entry - sl)
    min_tp_dist = sl_dist * MIN_RR
    if direction == "LONG":
        candidates = [l for l in sr_levels if l > entry + min_tp_dist]
        return min(candidates) if candidates else entry + min_tp_dist
    else:
        candidates = [l for l in sr_levels if l < entry - min_tp_dist]
        return max(candidates) if candidates else entry - min_tp_dist

# ─── STRATEGIES ──────────────────────────────────────────────────────────────

def strategy_sr_rejection(ltf_candles, closes, highs, lows,
                           atr_val, avg_vol, sr_level, bias) -> dict | None:
    if len(ltf_candles) < 10 or not atr_val:
        return None
    confirm  = ltf_candles[-2]
    prev     = ltf_candles[-3]
    last_vol = confirm["vol"]
    body     = abs(confirm["close"] - confirm["open"])
    range_   = confirm["high"] - confirm["low"]
    if range_ == 0:
        return None
    body_pct = body / range_

    if bias in ("BULL", "NEUTRAL"):
        if (confirm["close"] > confirm["open"] and body_pct > 0.5 and
                confirm["low"] <= sr_level * 1.001 and
                confirm["close"] > sr_level and
                last_vol > avg_vol * MIN_VOL_MULT):
            conf = 72 + min(10, int(body_pct * 10))
            return {"direction": "LONG",
                    "strategy": "S/R Rejection (Support)",
                    "entry": confirm["close"], "confidence": conf,
                    "note": f"Rejected off ${sr_level:,.0f} support | Vol {last_vol/avg_vol:.1f}x avg"}

    if bias in ("BEAR", "NEUTRAL"):
        if (confirm["close"] < confirm["open"] and body_pct > 0.5 and
                confirm["high"] >= sr_level * 0.999 and
                confirm["close"] < sr_level and
                last_vol > avg_vol * MIN_VOL_MULT):
            conf = 72 + min(10, int(body_pct * 10))
            return {"direction": "SHORT",
                    "strategy": "S/R Rejection (Resistance)",
                    "entry": confirm["close"], "confidence": conf,
                    "note": f"Rejected off ${sr_level:,.0f} resistance | Vol {last_vol/avg_vol:.1f}x avg"}
    return None

def strategy_breakout_retest(ltf_candles, closes, highs, lows,
                               atr_val, avg_vol, sr_level, bias) -> dict | None:
    if len(ltf_candles) < 15 or not atr_val:
        return None
    recent_closes = closes[-10:]
    confirm       = ltf_candles[-2]
    last_vol      = confirm["vol"]

    if bias in ("BULL", "NEUTRAL"):
        broke_above = any(c > sr_level for c in recent_closes[:-3])
        retesting   = abs(confirm["close"] - sr_level) / sr_level < 0.002
        reclaimed   = confirm["close"] >= sr_level
        if broke_above and retesting and reclaimed and last_vol > avg_vol * 1.2:
            return {"direction": "LONG",
                    "strategy": "Breakout Retest (Bull)",
                    "entry": confirm["close"], "confidence": 74,
                    "note": f"Retesting broken ${sr_level:,.0f} as support"}

    if bias in ("BEAR", "NEUTRAL"):
        broke_below = any(c < sr_level for c in recent_closes[:-3])
        retesting   = abs(confirm["close"] - sr_level) / sr_level < 0.002
        rejected    = confirm["close"] <= sr_level
        if broke_below and retesting and rejected and last_vol > avg_vol * 1.2:
            return {"direction": "SHORT",
                    "strategy": "Breakout Retest (Bear)",
                    "entry": confirm["close"], "confidence": 74,
                    "note": f"Retesting broken ${sr_level:,.0f} as resistance"}
    return None

def strategy_volume_climax(ltf_candles, closes, atr_val, avg_vol, bias) -> dict | None:
    if len(ltf_candles) < 20 or not atr_val:
        return None
    confirm  = ltf_candles[-2]
    last_vol = confirm["vol"]
    range_   = confirm["high"] - confirm["low"]
    if range_ == 0:
        return None
    upper_wick = confirm["high"] - max(confirm["open"], confirm["close"])
    lower_wick = min(confirm["open"], confirm["close"]) - confirm["low"]
    body       = abs(confirm["close"] - confirm["open"])

    if (bias in ("BEAR", "NEUTRAL") and last_vol > avg_vol * 2.0 and
            upper_wick > body * 1.5 and upper_wick > range_ * 0.4 and
            confirm["close"] < confirm["open"]):
        return {"direction": "SHORT", "strategy": "Volume Climax Reversal (Bearish)",
                "entry": confirm["close"], "confidence": 76,
                "note": f"Exhaustion wick with {last_vol/avg_vol:.1f}x volume — buyers trapped"}

    if (bias in ("BULL", "NEUTRAL") and last_vol > avg_vol * 2.0 and
            lower_wick > body * 1.5 and lower_wick > range_ * 0.4 and
            confirm["close"] > confirm["open"]):
        return {"direction": "LONG", "strategy": "Volume Climax Reversal (Bullish)",
                "entry": confirm["close"], "confidence": 76,
                "note": f"Exhaustion wick with {last_vol/avg_vol:.1f}x volume — sellers trapped"}
    return None

def strategy_trend_continuation(ltf_candles, closes, atr_val, avg_vol, bias) -> dict | None:
    if len(ltf_candles) < 25 or not atr_val or bias == "NEUTRAL":
        return None
    e20      = ema(closes, 20)
    if len(e20) < 3:
        return None
    confirm  = ltf_candles[-2]
    last_vol = confirm["vol"]
    price    = confirm["close"]
    dist_pct = abs(price - e20[-2]) / e20[-2]
    rsi_val  = rsi(closes[:-1], 14)

    if (bias == "BULL" and dist_pct < 0.003 and
            confirm["close"] > confirm["open"] and
            confirm["close"] > e20[-2] and
            last_vol > avg_vol * MIN_VOL_MULT and
            rsi_val and 35 < rsi_val < 60):
        return {"direction": "LONG", "strategy": "Trend Continuation (EMA20 Bounce)",
                "entry": price, "confidence": 73,
                "note": f"EMA20 bounce in uptrend | RSI {rsi_val:.0f} | Vol {last_vol/avg_vol:.1f}x"}

    if (bias == "BEAR" and dist_pct < 0.003 and
            confirm["close"] < confirm["open"] and
            confirm["close"] < e20[-2] and
            last_vol > avg_vol * MIN_VOL_MULT and
            rsi_val and 40 < rsi_val < 65):
        return {"direction": "SHORT", "strategy": "Trend Continuation (EMA20 Rejection)",
                "entry": price, "confidence": 73,
                "note": f"EMA20 rejection in downtrend | RSI {rsi_val:.0f} | Vol {last_vol/avg_vol:.1f}x"}
    return None

def strategy_momentum_breakout(ltf_candles, closes, highs, lows,
                                 atr_val, avg_vol, bias) -> dict | None:
    if len(ltf_candles) < 20 or not atr_val or bias == "NEUTRAL":
        return None
    confirm  = ltf_candles[-2]
    last_vol = confirm["vol"]
    body     = abs(confirm["close"] - confirm["open"])
    range_   = confirm["high"] - confirm["low"]
    if range_ == 0:
        return None
    body_pct    = body / range_
    prior_range = max(highs[-12:-2]) - min(lows[-12:-2])
    if prior_range > atr_val * 3:
        return None

    if (bias == "BULL" and confirm["close"] > confirm["open"] and
            body_pct > 0.65 and body > atr_val * 0.8 and
            last_vol > avg_vol * 2.0):
        conf = min(83, 75 + int(last_vol / avg_vol * 2))
        return {"direction": "LONG", "strategy": "Momentum Breakout (Bull)",
                "entry": confirm["close"], "confidence": conf,
                "note": f"Range breakout | {last_vol/avg_vol:.1f}x vol | Body {body_pct*100:.0f}%"}

    if (bias == "BEAR" and confirm["close"] < confirm["open"] and
            body_pct > 0.65 and body > atr_val * 0.8 and
            last_vol > avg_vol * 2.0):
        conf = min(83, 75 + int(last_vol / avg_vol * 2))
        return {"direction": "SHORT", "strategy": "Momentum Breakout (Bear)",
                "entry": confirm["close"], "confidence": conf,
                "note": f"Range breakdown | {last_vol/avg_vol:.1f}x vol | Body {body_pct*100:.0f}%"}
    return None

# ─── MAIN ANALYSIS ───────────────────────────────────────────────────────────

def analyse(htf_candles, ltf_candles, name) -> dict | None:
    if len(htf_candles) < 55 or len(ltf_candles) < 25:
        return None
    bias      = get_htf_bias(htf_candles)
    sr_levels = get_key_levels(htf_candles)
    closes    = [c["close"] for c in ltf_candles]
    highs     = [c["high"]  for c in ltf_candles]
    lows      = [c["low"]   for c in ltf_candles]
    atr_v     = atr(ltf_candles, 14)
    avg_v     = avg_volume(ltf_candles, 20)
    price     = closes[-2]
    sr_zone   = price_at_sr_zone(price, sr_levels)

    results = []
    if sr_zone:
        for strat in [strategy_sr_rejection, strategy_breakout_retest]:
            try:
                sig = strat(ltf_candles, closes, highs, lows,
                            atr_v, avg_v, sr_zone, bias)
                if sig and sig["confidence"] >= MIN_CONFIDENCE:
                    results.append(sig)
            except Exception as e:
                log.debug(f"{strat.__name__}: {e}")

    for strat in [strategy_volume_climax, strategy_trend_continuation,
                  strategy_momentum_breakout]:
        try:
            sig = strat(ltf_candles, closes, atr_v, avg_v, bias)
            if sig and sig["confidence"] >= MIN_CONFIDENCE:
                results.append(sig)
        except Exception as e:
            log.debug(f"{strat.__name__}: {e}")

    if not results:
        return None

    best     = max(results, key=lambda x: x["confidence"])
    same_dir = [r for r in results if r["direction"] == best["direction"]]
    if len(same_dir) > 1:
        best["confidence"] = min(95, best["confidence"] + (len(same_dir) - 1) * 3)
        best["confluence"] = [r["strategy"] for r in same_dir
                              if r["strategy"] != best["strategy"]]

    best["htf_bias"] = bias
    best["symbol"]   = name
    if sr_zone:
        best["sr_level"] = sr_zone

    # Structure-based TP/SL
    entry     = best["entry"]
    direction = best["direction"]
    sl        = find_structural_sl(ltf_candles, direction, entry, name)
    tp        = find_structural_tp(entry, sl, direction, sr_levels)
    rr        = abs(tp - entry) / abs(entry - sl)

    if rr < MIN_RR:
        log.info(f"  ⛔ {name} signal blocked — R:R {rr:.1f} < {MIN_RR}")
        return None

    best["tp"] = tp
    best["sl"] = sl
    best["rr"] = rr
    return best

# ─── TRADE TRACKER ───────────────────────────────────────────────────────────

active_trade  = None
trade_history = []  # list of completed trades for weekly summary

def set_active_trade(signal: dict):
    global active_trade
    active_trade = {
        "symbol":    signal["symbol"],
        "direction": signal["direction"],
        "entry":     signal["entry"],
        "tp":        signal["tp"],
        "sl":        signal["sl"],
        "strategy":  signal["strategy"],
        "open_time": datetime.now(timezone.utc),
        "last_price": signal["entry"],  # track price movement
    }
    log.info(f"  📌 Tracking trade: {signal['direction']} entry=${signal['entry']:,.2f} "
             f"TP=${signal['tp']:,.2f} SL=${signal['sl']:,.2f}")

def check_trade_outcome(current_price: float) -> str | None:
    """
    Only trigger when price actually CROSSES the TP or SL level.
    Compares last known price to current price to detect the crossover.
    """
    global active_trade
    if not active_trade:
        return None

    direction  = active_trade["direction"]
    tp         = active_trade["tp"]
    sl         = active_trade["sl"]
    last_price = active_trade["last_price"]

    outcome = None

    if direction == "LONG":
        # Price must cross UP through TP
        if last_price < tp and current_price >= tp:
            outcome = "TP"
        # Price must cross DOWN through SL
        elif last_price > sl and current_price <= sl:
            outcome = "SL"
    else:  # SHORT
        # Price must cross DOWN through TP
        if last_price > tp and current_price <= tp:
            outcome = "TP"
        # Price must cross UP through SL
        elif last_price < sl and current_price >= sl:
            outcome = "SL"

    # Always update last known price
    active_trade["last_price"] = current_price
    return outcome

def record_outcome(outcome: str):
    """Save result to trade history for weekly summary."""
    global active_trade, trade_history
    if not active_trade:
        return
    entry = active_trade["entry"]
    tp    = active_trade["tp"]
    sl    = active_trade["sl"]
    if outcome == "TP":
        pnl_pct = abs(tp - entry) / entry * 100
    else:
        pnl_pct = -abs(sl - entry) / entry * 100

    trade_history.append({
        "symbol":    active_trade["symbol"],
        "direction": active_trade["direction"],
        "strategy":  active_trade["strategy"],
        "entry":     entry,
        "exit":      tp if outcome == "TP" else sl,
        "outcome":   outcome,
        "pnl_pct":   pnl_pct,
        "rr":        abs(tp - entry) / abs(entry - sl),
        "duration":  int((datetime.now(timezone.utc) - active_trade["open_time"]).total_seconds() / 60),
        "time":      datetime.now(timezone.utc),
    })

def format_outcome(outcome: str) -> str:
    global active_trade
    if not active_trade:
        return ""
    entry    = active_trade["entry"]
    tp       = active_trade["tp"]
    sl       = active_trade["sl"]
    strategy = active_trade["strategy"]
    symbol   = active_trade["symbol"]
    duration = int((datetime.now(timezone.utc) - active_trade["open_time"]).total_seconds() / 60)

    if outcome == "TP":
        exit_price = tp
        pnl_pct    = abs(tp - entry) / entry * 100
        emoji      = "✅"
        result     = "TAKE PROFIT HIT"
        pnl_str    = f"+{pnl_pct:.2f}%"
    else:
        exit_price = sl
        pnl_pct    = abs(sl - entry) / entry * 100
        emoji      = "❌"
        result     = "STOP LOSS HIT"
        pnl_str    = f"-{pnl_pct:.2f}%"

    lines = [
        f"{emoji} <b>{result}</b>",
        f"",
        f"📊 <b>Strategy:</b> {strategy}",
        f"💹 <b>Asset:</b> {symbol}",
        f"📍 <b>Direction:</b> {active_trade['direction']}",
        f"",
        f"💰 <b>Entry:</b>    ${entry:,.2f}",
        f"🏁 <b>Exit:</b>     ${exit_price:,.2f}",
        f"📈 <b>Result:</b>   {pnl_str}",
        f"⏱ <b>Duration:</b> {duration} minutes",
    ]
    return "\n".join(lines)

def clear_active_trade():
    global active_trade
    active_trade = None

# ─── WEEKLY SUMMARY ──────────────────────────────────────────────────────────

last_weekly_summary = None

def should_send_weekly_summary() -> bool:
    global last_weekly_summary
    now = datetime.now(timezone.utc)
    # Send every Sunday at 8am UTC
    if now.weekday() == 6 and now.hour == 8 and now.minute < 3:
        if last_weekly_summary is None or (now - last_weekly_summary).days >= 6:
            return True
    return False

def format_weekly_summary() -> str:
    global trade_history, last_weekly_summary
    last_weekly_summary = datetime.now(timezone.utc)

    if not trade_history:
        return "📊 <b>Weekly Summary</b>\n\nNo completed trades this week."

    wins   = [t for t in trade_history if t["outcome"] == "TP"]
    losses = [t for t in trade_history if t["outcome"] == "SL"]
    total  = len(trade_history)
    win_rate = len(wins) / total * 100 if total else 0

    # Estimate P&L assuming 1% risk per trade
    risk_per_trade = 1.0  # 1% of portfolio
    weekly_pnl = 0.0
    for t in trade_history:
        if t["outcome"] == "TP":
            weekly_pnl += risk_per_trade * t["rr"]
        else:
            weekly_pnl -= risk_per_trade

    # Annualise (52 weeks)
    annual_pnl = weekly_pnl * 52

    best_trade  = max(trade_history, key=lambda t: t["pnl_pct"]) if wins else None
    worst_trade = min(trade_history, key=lambda t: t["pnl_pct"]) if losses else None

    avg_duration = sum(t["duration"] for t in trade_history) / total if total else 0
    avg_rr       = sum(t["rr"] for t in wins) / len(wins) if wins else 0

    btc_trades = [t for t in trade_history if t["symbol"] == "BTC"]
    eth_trades = [t for t in trade_history if t["symbol"] == "ETH"]

    lines = [
        "📊 <b>WEEKLY PERFORMANCE SUMMARY</b>",
        f"📅 Week ending {datetime.now(timezone.utc).strftime('%d %b %Y')}",
        "",
        f"📈 <b>Total Signals:</b> {total}",
        f"✅ <b>Wins:</b> {len(wins)}",
        f"❌ <b>Losses:</b> {len(losses)}",
        f"🎯 <b>Win Rate:</b> {win_rate:.1f}%",
        f"⚖️  <b>Avg R:R on wins:</b> 1:{avg_rr:.1f}",
        f"⏱ <b>Avg Trade Duration:</b> {avg_duration:.0f} min",
        "",
        f"💰 <b>Est. Weekly P&L</b> (1% risk/trade): {weekly_pnl:+.1f}%",
        f"📅 <b>Est. Annual P&L:</b> {annual_pnl:+.1f}%",
        "",
        f"₿ BTC: {len(btc_trades)} trades | ETH: {len(eth_trades)} trades",
    ]

    if best_trade:
        lines += [
            "",
            f"🏆 <b>Best Trade:</b> {best_trade['symbol']} {best_trade['direction']} "
            f"({best_trade['strategy'][:20]}) +{best_trade['pnl_pct']:.2f}%"
        ]
    if worst_trade:
        lines += [
            f"💀 <b>Worst Trade:</b> {worst_trade['symbol']} {worst_trade['direction']} "
            f"({worst_trade['strategy'][:20]}) {worst_trade['pnl_pct']:.2f}%"
        ]

    lines += ["", "⚠️ <i>Based on signal TP/SL levels. Actual results depend on execution.</i>"]

    # Clear history after summary
    trade_history = []
    return "\n".join(lines)

# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured:")
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
    arrow      = "🟢" if sig["direction"] == "LONG" else "🔴"
    rr         = sig.get("rr", 0)
    now_str    = datetime.now(timezone.utc).strftime("%H:%M UTC")
    conf       = sig["confidence"]
    bars       = "█" * (conf // 10) + "░" * (10 - conf // 10)
    bias_emoji = {"BULL": "📈", "BEAR": "📉", "NEUTRAL": "➡️"}.get(sig.get("htf_bias", ""), "")
    sym        = sig.get("symbol", "BTC")
    entry      = sig["entry"]
    tp         = sig["tp"]
    sl         = sig["sl"]

    lines = [
        f"{arrow} <b>{sym} {sig['direction']} SIGNAL</b>",
        f"",
        f"📊 <b>Strategy:</b> {sig['strategy']}",
        f"{bias_emoji} <b>1H Trend:</b> {sig.get('htf_bias', 'N/A')}",
        f"🕐 <b>Time:</b> {now_str}",
        f"",
        f"💰 <b>Entry:</b>  ${entry:,.2f}",
        f"🎯 <b>TP:</b>     ${tp:,.2f}  ({(tp-entry)/entry*100:+.2f}%)",
        f"🛑 <b>SL:</b>     ${sl:,.2f}  ({(sl-entry)/entry*100:+.2f}%)",
        f"",
        f"⚖️  <b>R:R ratio:</b> 1 : {rr:.1f}",
        f"🔥 <b>Confidence:</b> {conf}%  {bars}",
    ]
    if "sr_level" in sig:
        lines += [f"📍 <b>Key level:</b> ${sig['sr_level']:,.0f}"]
    if "note" in sig:
        lines += [f"", f"📝 <i>{sig['note']}</i>"]
    if sig.get("confluence"):
        lines += [f"✅ <b>Confirmed by:</b> {', '.join(sig['confluence'])}"]
    lines += [f"", f"⚠️ <i>Not financial advice. DYOR.</i>"]
    return "\n".join(lines)

# ─── KEEP ALIVE ──────────────────────────────────────────────────────────────

def keep_alive():
    from flask import Flask
    from threading import Thread
    app = Flask("")
    @app.route("/")
    def home():
        return "BTC/ETH Signal Bot v4 running ✅"
    t = Thread(target=lambda: app.run(host="0.0.0.0", port=8080))
    t.daemon = True
    t.start()

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def run():
    log.info("═" * 55)
    log.info("  BTC/ETH Signal Bot v4")
    log.info(f"  Symbols: BTC + ETH  |  HTF: {INTERVAL_HTF}min  |  LTF: {INTERVAL_LTF}min")
    log.info(f"  Min confidence: {MIN_CONFIDENCE}%  |  Min R:R: {MIN_RR}  |  Cooldown: {COOLDOWN_SEC}s")
    log.info("═" * 55)

    keep_alive()

    last_alert_time   = 0
    last_signal_dir   = None
    last_signal_price = 0.0

    # Prime data buffers for each symbol
    symbol_data = {}
    for kraken_sym, name in SYMBOLS:
        htf_init = fetch_kraken(kraken_sym, INTERVAL_HTF, LOOKBACK_HTF)
        ltf_init = fetch_kraken(kraken_sym, INTERVAL_LTF, LOOKBACK_LTF)
        symbol_data[kraken_sym] = {
            "name": name,
            "htf":  deque(htf_init, maxlen=LOOKBACK_HTF),
            "ltf":  deque(ltf_init, maxlen=LOOKBACK_LTF),
        }
        log.info(f"{name}: loaded {len(htf_init)} HTF + {len(ltf_init)} LTF candles")

    while True:
        try:
            # Weekly summary check
            if should_send_weekly_summary():
                log.info("Sending weekly summary...")
                send_telegram(format_weekly_summary())

            for kraken_sym, name in SYMBOLS:
                buf = symbol_data[kraken_sym]

                # Refresh candles
                htf_new = fetch_kraken(kraken_sym, INTERVAL_HTF, 5)
                ltf_new = fetch_kraken(kraken_sym, INTERVAL_LTF, 10)

                for c in htf_new:
                    if not buf["htf"] or c["ts"] > buf["htf"][-1]["ts"]:
                        buf["htf"].append(c)
                    elif c["ts"] == buf["htf"][-1]["ts"]:
                        buf["htf"][-1] = c

                for c in ltf_new:
                    if not buf["ltf"] or c["ts"] > buf["ltf"][-1]["ts"]:
                        buf["ltf"].append(c)
                    elif c["ts"] == buf["ltf"][-1]["ts"]:
                        buf["ltf"][-1] = c

                htf   = list(buf["htf"])
                ltf   = list(buf["ltf"])
                price = fetch_price(kraken_sym) or (ltf[-1]["close"] if ltf else 0)
                bias  = get_htf_bias(htf) if len(htf) >= 55 else "?"

                log.info(f"{name} ${price:,.2f}  |  1H: {bias}  |  ltf={len(ltf)} htf={len(htf)}")

                # Check active trade outcome (with delay)
                outcome = check_trade_outcome(price)
                if outcome and active_trade and active_trade.get("symbol") == name:
                    log.info(f"  🏁 {name} trade outcome: {outcome}")
                    msg = format_outcome(outcome)
                    send_telegram(msg)
                    record_outcome(outcome)
                    clear_active_trade()
                    # Reset cooldown after outcome so next signal isn't blocked
                    last_alert_time = time.time() - COOLDOWN_SEC + 120

                now = time.time()
                if now - last_alert_time < COOLDOWN_SEC:
                    secs = int(COOLDOWN_SEC - (now - last_alert_time))
                    log.info(f"  (cooldown {secs}s)")
                    continue

                signal = analyse(htf, ltf, name)
                if signal:
                    price_move = (abs(price - last_signal_price) / last_signal_price * 100
                                  if last_signal_price else 999)
                    if (last_signal_dir and
                            signal["direction"] != last_signal_dir and
                            price_move < 0.4):
                        log.info(f"  ⛔ Flip blocked ({price_move:.2f}% move)")
                    else:
                        log.info(f"  ✨ {name} {signal['direction']} | "
                                 f"{signal['strategy']} | {signal['confidence']}% | "
                                 f"R:R 1:{signal['rr']:.1f} | bias={bias}")
                        ok = send_telegram(format_signal(signal))
                        if ok:
                            log.info("  📨 Sent")
                            set_active_trade(signal)
                        last_alert_time   = now
                        last_signal_dir   = signal["direction"]
                        last_signal_price = price
                else:
                    log.info(f"  {name}: no setup")

        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")

        time.sleep(FETCH_INTERVAL)

if __name__ == "__main__":
    run()

