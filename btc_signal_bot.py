"""
BTC Signal Bot v3 — Dual timeframe, volume-confirmed, structure-based signals
- 1H chart sets the trend bias
- 5-min chart finds the entry at key S/R levels
- Volume must confirm every signal
- TP at next S/R level, SL behind real structure
"""

import time
import logging
import os
import math
from datetime import datetime, timezone
from collections import deque
import requests

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOL            = "XBTUSD"        # Kraken pair
INTERVAL_HTF      = 60              # 1 hour candles for trend
INTERVAL_LTF      = 5               # 5 min candles for entry
LOOKBACK_HTF      = 100             # 1H candles to keep
LOOKBACK_LTF      = 200             # 5min candles to keep
FETCH_INTERVAL    = 120             # check every 2 minutes
MIN_CONFIDENCE    = 70              # minimum confidence to alert
COOLDOWN_SEC      = 900             # 15 min between alerts
MIN_VOL_MULT      = 1.4             # volume must be 1.4x average to confirm
SR_ZONE_PCT       = 0.0015          # price must be within 0.15% of S/R level

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("BTCBot")

# ─── DATA FETCHING ────────────────────────────────────────────────────────────

def fetch_kraken(interval: int, limit: int) -> list[dict]:
    """Fetch OHLCV from Kraken. interval in minutes."""
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": SYMBOL, "interval": interval},
            headers={"User-Agent": "btc-signal-bot/3.0"},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
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
        log.warning(f"Kraken fetch error: {e}")
        return []

# ─── INDICATORS ──────────────────────────────────────────────────────────────

def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
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
    trs = []
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

# ─── HIGHER TIMEFRAME TREND ───────────────────────────────────────────────────

def get_htf_bias(htf_candles: list[dict]) -> str:
    """
    Returns 'BULL', 'BEAR', or 'NEUTRAL' based on 1H chart structure.
    Uses EMA20/EMA50 alignment + price position + recent swing structure.
    """
    if len(htf_candles) < 55:
        return "NEUTRAL"
    closes = [c["close"] for c in htf_candles]
    e20    = ema(closes, 20)
    e50    = ema(closes, 50)
    if not e20 or not e50:
        return "NEUTRAL"

    price  = closes[-1]
    # EMA alignment
    ema_bull = e20[-1] > e50[-1]
    ema_bear = e20[-1] < e50[-1]
    # Price above/below EMAs
    above_both = price > e20[-1] and price > e50[-1]
    below_both = price < e20[-1] and price < e50[-1]
    # Recent higher highs / lower lows (last 10 candles vs prior 10)
    recent_high = max(c["high"] for c in htf_candles[-10:])
    prior_high  = max(c["high"] for c in htf_candles[-20:-10])
    recent_low  = min(c["low"]  for c in htf_candles[-10:])
    prior_low   = min(c["low"]  for c in htf_candles[-20:-10])
    hh_hl = recent_high > prior_high and recent_low > prior_low
    lh_ll = recent_high < prior_high and recent_low < prior_low

    bull_score = sum([ema_bull, above_both, hh_hl])
    bear_score = sum([ema_bear, below_both, lh_ll])

    if bull_score >= 2:
        return "BULL"
    elif bear_score >= 2:
        return "BEAR"
    return "NEUTRAL"

# ─── KEY S/R LEVELS FROM 1H ──────────────────────────────────────────────────

def get_key_levels(htf_candles: list[dict]) -> list[float]:
    """
    Find key S/R levels from 1H swing highs and lows.
    Returns sorted list of price levels.
    """
    if len(htf_candles) < 20:
        return []
    highs  = [c["high"] for c in htf_candles]
    lows   = [c["low"]  for c in htf_candles]
    levels = []
    window = 5
    # Swing highs
    for i in range(window, len(highs) - window):
        if highs[i] == max(highs[i-window:i+window+1]):
            levels.append(highs[i])
    # Swing lows
    for i in range(window, len(lows) - window):
        if lows[i] == min(lows[i-window:i+window+1]):
            levels.append(lows[i])
    # Cluster nearby levels (within 0.2%)
    levels.sort()
    clustered = []
    for lvl in levels:
        if not clustered or abs(lvl - clustered[-1]) / clustered[-1] > 0.002:
            clustered.append(lvl)
        else:
            clustered[-1] = (clustered[-1] + lvl) / 2  # average the cluster
    return clustered

def nearest_levels(price: float, levels: list[float], direction: str) -> tuple[float | None, float | None]:
    """
    Returns (nearest_level_below, nearest_level_above) relative to price.
    Used to set TP at next S/R and SL behind structure.
    """
    below = [l for l in levels if l < price * 0.9995]
    above = [l for l in levels if l > price * 1.0005]
    nearest_below = max(below) if below else None
    nearest_above = min(above) if above else None
    return nearest_below, nearest_above

def price_at_sr_zone(price: float, levels: list[float]) -> float | None:
    """Returns the S/R level if price is within SR_ZONE_PCT of it, else None."""
    for lvl in levels:
        if abs(price - lvl) / lvl <= SR_ZONE_PCT:
            return lvl
    return None

# ─── 5-MIN ENTRY STRATEGIES ──────────────────────────────────────────────────

def strategy_sr_rejection(ltf_candles, closes, highs, lows, atr_val,
                           avg_vol, sr_level, bias) -> dict | None:
    """
    Price touches a key S/R level and rejects with a confirmation candle.
    Only trades in direction of HTF bias.
    """
    if len(ltf_candles) < 10 or not atr_val:
        return None
    # Use the CLOSED candle (second to last) as confirmation
    # Last candle may still be forming
    confirm = ltf_candles[-2]
    prev    = ltf_candles[-3]
    last_vol = confirm["vol"]
    body     = abs(confirm["close"] - confirm["open"])
    range_   = confirm["high"] - confirm["low"]
    if range_ == 0:
        return None
    body_pct = body / range_

    # Bullish rejection off support (only in BULL or NEUTRAL bias)
    if bias in ("BULL", "NEUTRAL"):
        if (confirm["close"] > confirm["open"] and   # green candle
                body_pct > 0.5 and                   # decent body
                confirm["low"] <= sr_level * 1.001 and  # touched the level
                confirm["close"] > sr_level and         # closed above it
                last_vol > avg_vol * MIN_VOL_MULT):     # volume confirmed
            conf  = 72 + min(10, int(body_pct * 10))
            below, above = nearest_levels(confirm["close"],
                                          [sr_level], "LONG")
            tp = confirm["close"] + atr_val * 2.5
            sl = min(confirm["low"], prev["low"]) - atr_val * 0.3
            # cap
            tp = min(tp, confirm["close"] * 1.03)
            sl = max(sl, confirm["close"] * 0.985)
            return {"direction": "LONG",
                    "strategy": "S/R Level Rejection (Support)",
                    "entry": confirm["close"], "tp": tp, "sl": sl,
                    "confidence": conf,
                    "note": f"Rejected off ${sr_level:,.0f} support | Vol {last_vol/avg_vol:.1f}x avg"}

    # Bearish rejection off resistance (only in BEAR or NEUTRAL bias)
    if bias in ("BEAR", "NEUTRAL"):
        if (confirm["close"] < confirm["open"] and   # red candle
                body_pct > 0.5 and
                confirm["high"] >= sr_level * 0.999 and
                confirm["close"] < sr_level and
                last_vol > avg_vol * MIN_VOL_MULT):
            conf  = 72 + min(10, int(body_pct * 10))
            tp = confirm["close"] - atr_val * 2.5
            sl = max(confirm["high"], prev["high"]) + atr_val * 0.3
            tp = max(tp, confirm["close"] * 0.97)
            sl = min(sl, confirm["close"] * 1.015)
            return {"direction": "SHORT",
                    "strategy": "S/R Level Rejection (Resistance)",
                    "entry": confirm["close"], "tp": tp, "sl": sl,
                    "confidence": conf,
                    "note": f"Rejected off ${sr_level:,.0f} resistance | Vol {last_vol/avg_vol:.1f}x avg"}
    return None

def strategy_breakout_retest(ltf_candles, closes, highs, lows, atr_val,
                              avg_vol, sr_level, bias) -> dict | None:
    """
    Price breaks a key level, pulls back to retest it, then continues.
    The retest is the entry — much higher probability than chasing the break.
    """
    if len(ltf_candles) < 15 or not atr_val:
        return None
    # Look at last 10 candles for the break, last 3 for the retest
    recent_closes = closes[-10:]
    confirm = ltf_candles[-2]
    last_vol = confirm["vol"]

    # Bullish: price broke above level in last 10 candles, now retesting it
    if bias in ("BULL", "NEUTRAL"):
        broke_above = any(c > sr_level for c in recent_closes[:-3])
        retesting   = abs(confirm["close"] - sr_level) / sr_level < 0.002
        reclaimed   = confirm["close"] >= sr_level
        if (broke_above and retesting and reclaimed and
                last_vol > avg_vol * 1.2):
            conf = 74
            tp   = confirm["close"] + atr_val * 3.0
            sl   = sr_level - atr_val * 0.5
            tp   = min(tp, confirm["close"] * 1.025)
            sl   = max(sl, confirm["close"] * 0.985)
            return {"direction": "LONG",
                    "strategy": "Breakout Retest (Bull)",
                    "entry": confirm["close"], "tp": tp, "sl": sl,
                    "confidence": conf,
                    "note": f"Retesting broken level ${sr_level:,.0f} as support"}

    # Bearish: price broke below level, now retesting from below
    if bias in ("BEAR", "NEUTRAL"):
        broke_below = any(c < sr_level for c in recent_closes[:-3])
        retesting   = abs(confirm["close"] - sr_level) / sr_level < 0.002
        rejected    = confirm["close"] <= sr_level
        if (broke_below and retesting and rejected and
                last_vol > avg_vol * 1.2):
            conf = 74
            tp   = confirm["close"] - atr_val * 3.0
            sl   = sr_level + atr_val * 0.5
            tp   = max(tp, confirm["close"] * 0.975)
            sl   = min(sl, confirm["close"] * 1.015)
            return {"direction": "SHORT",
                    "strategy": "Breakout Retest (Bear)",
                    "entry": confirm["close"], "tp": tp, "sl": sl,
                    "confidence": conf,
                    "note": f"Retesting broken level ${sr_level:,.0f} as resistance"}
    return None

def strategy_volume_climax_reversal(ltf_candles, closes, atr_val,
                                     avg_vol, bias) -> dict | None:
    """
    Massive volume spike with a wick rejection candle = exhaustion move.
    Price likely to reverse. One of the highest probability setups.
    """
    if len(ltf_candles) < 20 or not atr_val:
        return None
    confirm  = ltf_candles[-2]
    prev     = ltf_candles[-3]
    last_vol = confirm["vol"]
    range_   = confirm["high"] - confirm["low"]
    if range_ == 0:
        return None
    upper_wick = confirm["high"] - max(confirm["open"], confirm["close"])
    lower_wick = min(confirm["open"], confirm["close"]) - confirm["low"]
    body       = abs(confirm["close"] - confirm["open"])

    # Bearish exhaustion: big volume, large upper wick, small body
    if (bias in ("BEAR", "NEUTRAL") and
            last_vol > avg_vol * 2.0 and
            upper_wick > body * 1.5 and
            upper_wick > range_ * 0.4 and
            confirm["close"] < confirm["open"]):
        conf = 76
        tp   = confirm["close"] - atr_val * 2.5
        sl   = confirm["high"] + atr_val * 0.3
        tp   = max(tp, confirm["close"] * 0.975)
        sl   = min(sl, confirm["close"] * 1.015)
        return {"direction": "SHORT",
                "strategy": "Volume Climax Reversal (Bearish)",
                "entry": confirm["close"], "tp": tp, "sl": sl,
                "confidence": conf,
                "note": f"Exhaustion wick with {last_vol/avg_vol:.1f}x volume — buyers trapped"}

    # Bullish exhaustion: big volume, large lower wick, small body
    if (bias in ("BULL", "NEUTRAL") and
            last_vol > avg_vol * 2.0 and
            lower_wick > body * 1.5 and
            lower_wick > range_ * 0.4 and
            confirm["close"] > confirm["open"]):
        conf = 76
        tp   = confirm["close"] + atr_val * 2.5
        sl   = confirm["low"] - atr_val * 0.3
        tp   = min(tp, confirm["close"] * 1.025)
        sl   = max(sl, confirm["close"] * 0.985)
        return {"direction": "LONG",
                "strategy": "Volume Climax Reversal (Bullish)",
                "entry": confirm["close"], "tp": tp, "sl": sl,
                "confidence": conf,
                "note": f"Exhaustion wick with {last_vol/avg_vol:.1f}x volume — sellers trapped"}
    return None

def strategy_trend_continuation(ltf_candles, closes, atr_val,
                                  avg_vol, bias) -> dict | None:
    """
    Strong trend move with pullback to EMA20, confirmed by volume on resumption.
    Only trades WITH the HTF bias.
    """
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

    if bias == "BULL":
        # Price pulled back to EMA20, now bouncing with volume
        if (dist_pct < 0.003 and
                confirm["close"] > confirm["open"] and
                confirm["close"] > e20[-2] and
                last_vol > avg_vol * MIN_VOL_MULT and
                rsi_val and 35 < rsi_val < 60):
            conf = 73
            tp   = price + atr_val * 3.0
            sl   = min(confirm["low"], e20[-2]) - atr_val * 0.3
            tp   = min(tp, price * 1.025)
            sl   = max(sl, price * 0.985)
            return {"direction": "LONG",
                    "strategy": "Trend Continuation (EMA20 Bounce)",
                    "entry": price, "tp": tp, "sl": sl,
                    "confidence": conf,
                    "note": f"EMA20 bounce in uptrend | RSI {rsi_val:.0f} | Vol {last_vol/avg_vol:.1f}x"}

    if bias == "BEAR":
        # Price pulled back up to EMA20, now rejecting with volume
        if (dist_pct < 0.003 and
                confirm["close"] < confirm["open"] and
                confirm["close"] < e20[-2] and
                last_vol > avg_vol * MIN_VOL_MULT and
                rsi_val and 40 < rsi_val < 65):
            conf = 73
            tp   = price - atr_val * 3.0
            sl   = max(confirm["high"], e20[-2]) + atr_val * 0.3
            tp   = max(tp, price * 0.975)
            sl   = min(sl, price * 1.015)
            return {"direction": "SHORT",
                    "strategy": "Trend Continuation (EMA20 Rejection)",
                    "entry": price, "tp": tp, "sl": sl,
                    "confidence": conf,
                    "note": f"EMA20 rejection in downtrend | RSI {rsi_val:.0f} | Vol {last_vol/avg_vol:.1f}x"}
    return None

def strategy_momentum_breakout(ltf_candles, closes, highs, lows,
                                 atr_val, avg_vol, bias) -> dict | None:
    """
    Strong momentum candle breaking out of a tight range with high volume.
    Must align with HTF bias.
    """
    if len(ltf_candles) < 20 or not atr_val or bias == "NEUTRAL":
        return None
    confirm  = ltf_candles[-2]
    last_vol = confirm["vol"]
    body     = abs(confirm["close"] - confirm["open"])
    range_   = confirm["high"] - confirm["low"]
    if range_ == 0:
        return None
    body_pct = body / range_

    # Check if prior 10 candles were in a tight range (consolidation)
    prior_range = max(highs[-12:-2]) - min(lows[-12:-2])
    if prior_range > atr_val * 3:
        return None  # not a tight consolidation

    if (bias == "BULL" and
            confirm["close"] > confirm["open"] and
            body_pct > 0.65 and
            body > atr_val * 0.8 and
            last_vol > avg_vol * 2.0):
        conf = 75 + min(8, int(last_vol / avg_vol * 2))
        tp   = confirm["close"] + atr_val * 3.0
        sl   = confirm["low"] - atr_val * 0.3
        tp   = min(tp, confirm["close"] * 1.025)
        sl   = max(sl, confirm["close"] * 0.985)
        return {"direction": "LONG",
                "strategy": "Momentum Breakout (Bull)",
                "entry": confirm["close"], "tp": tp, "sl": sl,
                "confidence": conf,
                "note": f"Range breakout | {last_vol/avg_vol:.1f}x vol | Body {body_pct*100:.0f}%"}

    if (bias == "BEAR" and
            confirm["close"] < confirm["open"] and
            body_pct > 0.65 and
            body > atr_val * 0.8 and
            last_vol > avg_vol * 2.0):
        conf = 75 + min(8, int(last_vol / avg_vol * 2))
        tp   = confirm["close"] - atr_val * 3.0
        sl   = confirm["high"] + atr_val * 0.3
        tp   = max(tp, confirm["close"] * 0.975)
        sl   = min(sl, confirm["close"] * 1.015)
        return {"direction": "SHORT",
                "strategy": "Momentum Breakout (Bear)",
                "entry": confirm["close"], "tp": tp, "sl": sl,
                "confidence": conf,
                "note": f"Range breakdown | {last_vol/avg_vol:.1f}x vol | Body {body_pct*100:.0f}%"}
    return None

# ─── MAIN ANALYSIS ───────────────────────────────────────────────────────────

def analyse(htf_candles: list[dict], ltf_candles: list[dict]) -> dict | None:
    if len(htf_candles) < 55 or len(ltf_candles) < 25:
        return None

    # 1. Get higher timeframe bias
    bias = get_htf_bias(htf_candles)

    # 2. Get key S/R levels from 1H
    sr_levels = get_key_levels(htf_candles)

    # 3. Prepare LTF data
    closes = [c["close"] for c in ltf_candles]
    highs  = [c["high"]  for c in ltf_candles]
    lows   = [c["low"]   for c in ltf_candles]
    atr_v  = atr(ltf_candles, 14)
    avg_v  = avg_volume(ltf_candles, 20)
    price  = closes[-2]  # use confirmed closed candle price

    # 4. Find nearest S/R level to current price
    sr_zone = price_at_sr_zone(price, sr_levels)

    # 5. Run strategies
    results = []

    # S/R based strategies (only if price is at a level)
    if sr_zone:
        for strat in [strategy_sr_rejection, strategy_breakout_retest]:
            try:
                sig = strat(ltf_candles, closes, highs, lows,
                            atr_v, avg_v, sr_zone, bias)
                if sig and sig["confidence"] >= MIN_CONFIDENCE:
                    results.append(sig)
            except Exception as e:
                log.debug(f"{strat.__name__} error: {e}")

    # Non-SR strategies (run always)
    for strat in [strategy_volume_climax_reversal,
                  strategy_trend_continuation,
                  strategy_momentum_breakout]:
        try:
            sig = strat(ltf_candles, closes, atr_v, avg_v, bias)
            if sig and sig["confidence"] >= MIN_CONFIDENCE:
                results.append(sig)
        except Exception as e:
            log.debug(f"{strat.__name__} error: {e}")

    if not results:
        return None

    best = max(results, key=lambda x: x["confidence"])

    # Confluence bonus
    same_dir = [r for r in results if r["direction"] == best["direction"]]
    if len(same_dir) > 1:
        best["confidence"] = min(95, best["confidence"] + (len(same_dir) - 1) * 3)
        best["confluence"] = [r["strategy"] for r in same_dir
                              if r["strategy"] != best["strategy"]]

    best["htf_bias"] = bias
    if sr_zone:
        best["sr_level"] = sr_zone
    return best

# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — printing signal:")
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
    arrow   = "🟢" if sig["direction"] == "LONG" else "🔴"
    rr      = abs(sig["tp"] - sig["entry"]) / max(abs(sig["entry"] - sig["sl"]), 1)
    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    conf    = sig["confidence"]
    bars    = "█" * (conf // 10) + "░" * (10 - conf // 10)
    bias_emoji = {"BULL": "📈", "BEAR": "📉", "NEUTRAL": "➡️"}.get(sig.get("htf_bias", ""), "")

    lines = [
        f"{arrow} <b>BTC {sig['direction']} SIGNAL</b>",
        f"",
        f"📊 <b>Strategy:</b> {sig['strategy']}",
        f"{bias_emoji} <b>1H Trend:</b> {sig.get('htf_bias', 'N/A')}",
        f"🕐 <b>Time:</b> {now_str}",
        f"",
        f"💰 <b>Entry:</b>  ${sig['entry']:,.2f}",
        f"🎯 <b>TP:</b>     ${sig['tp']:,.2f}  ({'+' if sig['tp'] > sig['entry'] else ''}{(sig['tp']-sig['entry'])/sig['entry']*100:.2f}%)",
        f"🛑 <b>SL:</b>     ${sig['sl']:,.2f}  ({(sig['sl']-sig['entry'])/sig['entry']*100:.2f}%)",
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

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def keep_alive():
    from flask import Flask
    from threading import Thread
    app = Flask("")
    @app.route("/")
    def home():
        return "BTC Signal Bot v3 running ✅"
    t = Thread(target=lambda: app.run(host="0.0.0.0", port=8080))
    t.daemon = True
    t.start()

def run():
    log.info("═" * 55)
    log.info("  BTC Signal Bot v3")
    log.info(f"  HTF: {INTERVAL_HTF}min  |  LTF: {INTERVAL_LTF}min")
    log.info(f"  Min confidence: {MIN_CONFIDENCE}%  |  Cooldown: {COOLDOWN_SEC}s")
    log.info("═" * 55)

    keep_alive()

    last_alert_time      = 0
    last_signal_dir      = None
    last_signal_price    = 0.0
    htf_buffer: deque    = deque(maxlen=LOOKBACK_HTF)
    ltf_buffer: deque    = deque(maxlen=LOOKBACK_LTF)

    # Prime buffers
    htf_init = fetch_kraken(INTERVAL_HTF, LOOKBACK_HTF)
    ltf_init = fetch_kraken(INTERVAL_LTF, LOOKBACK_LTF)
    htf_buffer.extend(htf_init)
    ltf_buffer.extend(ltf_init)
    log.info(f"Loaded {len(htf_init)} HTF + {len(ltf_init)} LTF candles")

    while True:
        try:
            # Refresh data
            htf_new = fetch_kraken(INTERVAL_HTF, 5)
            ltf_new = fetch_kraken(INTERVAL_LTF, 10)

            for c in htf_new:
                if not htf_buffer or c["ts"] > htf_buffer[-1]["ts"]:
                    htf_buffer.append(c)
                elif c["ts"] == htf_buffer[-1]["ts"]:
                    htf_buffer[-1] = c

            for c in ltf_new:
                if not ltf_buffer or c["ts"] > ltf_buffer[-1]["ts"]:
                    ltf_buffer.append(c)
                elif c["ts"] == ltf_buffer[-1]["ts"]:
                    ltf_buffer[-1] = c

            htf = list(htf_buffer)
            ltf = list(ltf_buffer)
            price = ltf[-1]["close"] if ltf else 0

            bias = get_htf_bias(htf) if len(htf) >= 55 else "?"
            log.info(f"BTC ${price:,.2f}  |  1H bias: {bias}  |  ltf={len(ltf)} htf={len(htf)}")

            now = time.time()
            if now - last_alert_time < COOLDOWN_SEC:
                secs = int(COOLDOWN_SEC - (now - last_alert_time))
                log.info(f"  (cooldown {secs}s remaining)")
            else:
                signal = analyse(htf, ltf)
                if signal:
                    # Block flip unless price moved meaningfully
                    price_move = (abs(price - last_signal_price) / last_signal_price * 100
                                  if last_signal_price else 999)
                    if (last_signal_dir and
                            signal["direction"] != last_signal_dir and
                            price_move < 0.4):
                        log.info(f"  ⛔ Flip blocked {last_signal_dir}→{signal['direction']} "
                                 f"(move={price_move:.2f}%)")
                    else:
                        log.info(f"  ✨ {signal['direction']} | {signal['strategy']} | "
                                 f"{signal['confidence']}% | bias={signal.get('htf_bias')}")
                        ok = send_telegram(format_signal(signal))
                        if ok:
                            log.info("  📨 Telegram sent")
                        last_alert_time   = now
                        last_signal_dir   = signal["direction"]
                        last_signal_price = price
                else:
                    log.info("  No setup detected")

        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")

        time.sleep(FETCH_INTERVAL)

if __name__ == "__main__":
    run()
