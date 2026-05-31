"""
BTC Signal Bot — AI-powered Bitcoin trading signal detector
Runs 24/7, analyses price data, sends Telegram notifications on high-confidence setups.
"""

import time
import logging
import os
import json
import math
from datetime import datetime, timezone
from collections import deque
import requests

# ─── CONFIG (set via environment variables) ──────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOL = "BTCUSDT"
INTERVAL = "1m"
LOOKBACK_CANDLES = 200       # candles kept in memory
FETCH_INTERVAL_SEC = 60      # how often to fetch new data
MIN_CONFIDENCE = 65          # minimum confidence % to send alert
COOLDOWN_SEC = 300           # minimum seconds between alerts (avoid spam)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("BTCBot")

# ─── BINANCE DATA FETCHER ─────────────────────────────────────────────────────

def fetch_klines(symbol: str, interval: str, limit: int = 200) -> list[dict]:
    """Fetch OHLCV candles from Bybit public API (no auth needed, no geo-blocks)."""
    url = "https://api.bybit.com/v5/market/kline"
    # Bybit interval format: 1 = 1min, 5 = 5min, etc.
    bybit_interval = interval.replace("m", "").replace("h", "60").replace("d", "D")
    params = {"category": "spot", "symbol": symbol, "interval": bybit_interval, "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        raw = data.get("result", {}).get("list", [])
        candles = []
        for c in reversed(raw):  # Bybit returns newest first
            candles.append({
                "ts":    int(c[0]),
                "open":  float(c[1]),
                "high":  float(c[2]),
                "low":   float(c[3]),
                "close": float(c[4]),
                "vol":   float(c[5]),
            })
        return candles
    except Exception as e:
        log.warning(f"Bybit fetch error: {e}")
        return []

def fetch_ticker(symbol: str) -> float | None:
    """Get latest price from Bybit."""
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "spot", "symbol": symbol}, timeout=5
        )
        return float(r.json()["result"]["list"][0]["lastPrice"])
    except Exception:
        return None

# ─── INDICATORS ───────────────────────────────────────────────────────────────

def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def sma(values: list[float], period: int) -> list[float]:
    return [
        sum(values[i:i+period]) / period
        for i in range(len(values) - period + 1)
    ]

def rsi(closes: list[float], period: int = 14) -> list[float]:
    if len(closes) < period + 1:
        return []
    deltas = [closes[i+1] - closes[i] for i in range(len(closes)-1)]
    gains, losses = [], []
    for d in deltas:
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    result = []
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs = avg_g / avg_l if avg_l != 0 else 100
        result.append(100 - 100 / (1 + rs))
    return result

def atr(candles: list[dict], period: int = 14) -> list[float]:
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return []
    avgs = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        avgs.append((avgs[-1] * (period - 1) + tr) / period)
    return avgs

def bollinger(closes: list[float], period: int = 20, num_std: float = 2.0):
    if len(closes) < period:
        return [], [], []
    mid = sma(closes, period)
    upper, lower = [], []
    for i in range(len(mid)):
        window = closes[i:i+period]
        std = math.sqrt(sum((x - mid[i])**2 for x in window) / period)
        upper.append(mid[i] + num_std * std)
        lower.append(mid[i] - num_std * std)
    return upper, mid, lower

def macd(closes: list[float], fast=12, slow=26, signal=9):
    e_fast = ema(closes, fast)
    e_slow = ema(closes, slow)
    diff = len(e_fast) - len(e_slow)
    macd_line = [e_fast[i+diff] - e_slow[i] for i in range(len(e_slow))]
    sig_line = ema(macd_line, signal)
    hist_diff = len(macd_line) - len(sig_line)
    histogram = [macd_line[i+hist_diff] - sig_line[i] for i in range(len(sig_line))]
    return macd_line, sig_line, histogram

def pivot_highs(highs: list[float], window: int = 5) -> list[tuple[int, float]]:
    pivots = []
    for i in range(window, len(highs) - window):
        if highs[i] == max(highs[i-window:i+window+1]):
            pivots.append((i, highs[i]))
    return pivots

def pivot_lows(lows: list[float], window: int = 5) -> list[tuple[int, float]]:
    pivots = []
    for i in range(window, len(lows) - window):
        if lows[i] == min(lows[i-window:i+window+1]):
            pivots.append((i, lows[i]))
    return pivots

def volume_profile(candles: list[dict], bins: int = 20) -> dict:
    """Simple volume-at-price distribution."""
    all_lows = [c["low"] for c in candles]
    all_highs = [c["high"] for c in candles]
    price_min, price_max = min(all_lows), max(all_highs)
    bin_size = (price_max - price_min) / bins
    profile = {}
    for c in candles:
        mid = (c["high"] + c["low"]) / 2
        b = int((mid - price_min) / bin_size)
        b = min(b, bins - 1)
        profile[b] = profile.get(b, 0) + c["vol"]
    # find POC (point of control = highest volume bin)
    poc_bin = max(profile, key=profile.get)
    poc_price = price_min + (poc_bin + 0.5) * bin_size
    return {"poc": poc_price, "profile": profile,
            "price_min": price_min, "price_max": price_max, "bin_size": bin_size}

# ─── STRATEGY ENGINES ────────────────────────────────────────────────────────

def strategy_breakout(candles, atr_vals, closes, highs, lows) -> dict | None:
    """Detects breakouts above key resistance or below key support."""
    if len(candles) < 50 or len(atr_vals) < 1:
        return None
    recent = candles[-50:]
    # consolidation: check if price was ranging in a tight band
    r_highs = [c["high"] for c in recent[:-3]]
    r_lows  = [c["low"]  for c in recent[:-3]]
    band = max(r_highs) - min(r_lows)
    current_atr = atr_vals[-1]
    if band < current_atr * 4:
        return None  # not a valid consolidation (too tight or no range)

    resistance = max(r_highs)
    support    = min(r_lows)
    last_close = closes[-1]
    last_vol   = candles[-1]["vol"]
    avg_vol    = sum(c["vol"] for c in candles[-20:-1]) / 19

    # Bullish breakout
    if last_close > resistance and last_vol > avg_vol * 1.3:
        conf = min(90, 55 + int((last_close - resistance) / current_atr * 15) +
                   int(min(last_vol / avg_vol - 1, 1) * 15))
        tp = last_close + band * 0.618
        sl = resistance - current_atr * 0.5
        return {"direction": "LONG", "strategy": "Consolidation Breakout",
                "entry": last_close, "tp": tp, "sl": sl, "confidence": conf,
                "note": f"Broke ${resistance:,.0f} resistance with {last_vol/avg_vol:.1f}x volume"}

    # Bearish breakdown
    if last_close < support and last_vol > avg_vol * 1.3:
        conf = min(90, 55 + int((support - last_close) / current_atr * 15) +
                   int(min(last_vol / avg_vol - 1, 1) * 15))
        tp = last_close - band * 0.618
        sl = support + current_atr * 0.5
        return {"direction": "SHORT", "strategy": "Consolidation Breakdown",
                "entry": last_close, "tp": tp, "sl": sl, "confidence": conf,
                "note": f"Broke ${support:,.0f} support with {last_vol/avg_vol:.1f}x volume"}
    return None

def strategy_trend_pullback(candles, closes, atr_vals) -> dict | None:
    """Buy pullbacks in uptrend; sell bounces in downtrend."""
    if len(closes) < 60 or len(atr_vals) < 1:
        return None
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    if len(ema20) < 5 or len(ema50) < 5:
        return None
    trend_up   = ema20[-1] > ema50[-1] and ema20[-3] > ema50[-3]
    trend_down = ema20[-1] < ema50[-1] and ema20[-3] < ema50[-3]
    current_atr = atr_vals[-1]
    last_close  = closes[-1]

    rsi_vals = rsi(closes, 14)
    if not rsi_vals:
        return None

    # Bullish pullback to EMA20
    if trend_up:
        dist = abs(last_close - ema20[-1])
        if dist < current_atr * 0.6 and 35 < rsi_vals[-1] < 55:
            conf = 68 + min(12, int((55 - rsi_vals[-1]) / 5))
            tp   = last_close + current_atr * 3.0
            sl   = last_close - current_atr * 1.5
            return {"direction": "LONG", "strategy": "Trend Pullback to EMA20",
                    "entry": last_close, "tp": tp, "sl": sl, "confidence": conf,
                    "note": f"EMA20={ema20[-1]:,.0f} | RSI={rsi_vals[-1]:.0f}"}

    # Bearish bounce rejection
    if trend_down:
        dist = abs(last_close - ema20[-1])
        if dist < current_atr * 0.6 and 45 < rsi_vals[-1] < 65:
            conf = 68 + min(12, int((rsi_vals[-1] - 45) / 5))
            tp   = last_close - current_atr * 3.0
            sl   = last_close + current_atr * 1.5
            return {"direction": "SHORT", "strategy": "Trend Bounce Rejection",
                    "entry": last_close, "tp": tp, "sl": sl, "confidence": conf,
                    "note": f"EMA20={ema20[-1]:,.0f} | RSI={rsi_vals[-1]:.0f}"}
    return None

def strategy_support_resistance_flip(candles, closes, highs, lows, atr_vals) -> dict | None:
    """S/R flip: old resistance becomes support (bullish) or vice versa."""
    if len(candles) < 80 or len(atr_vals) < 1:
        return None
    p_highs = pivot_highs(highs[-80:], 5)
    p_lows  = pivot_lows(lows[-80:], 5)
    last    = closes[-1]
    current_atr = atr_vals[-1]
    tolerance   = current_atr * 0.5

    # Look for old resistance now acting as support
    for _, ph in p_highs[-6:]:
        if abs(last - ph) < tolerance and last > ph:
            conf = 72
            tp   = last + current_atr * 2.5
            sl   = last - current_atr * 1.2
            return {"direction": "LONG", "strategy": "S/R Flip (Resistance→Support)",
                    "entry": last, "tp": tp, "sl": sl, "confidence": conf,
                    "note": f"Old resistance ${ph:,.0f} flipped to support"}

    # Look for old support now acting as resistance
    for _, pl in p_lows[-6:]:
        if abs(last - pl) < tolerance and last < pl:
            conf = 72
            tp   = last - current_atr * 2.5
            sl   = last + current_atr * 1.2
            return {"direction": "SHORT", "strategy": "S/R Flip (Support→Resistance)",
                    "entry": last, "tp": tp, "sl": sl, "confidence": conf,
                    "note": f"Old support ${pl:,.0f} flipped to resistance"}
    return None

def strategy_momentum_surge(candles, closes, atr_vals) -> dict | None:
    """Strong momentum candle with volume confirmation."""
    if len(candles) < 30 or len(atr_vals) < 3:
        return None
    last     = candles[-1]
    prev     = candles[-2]
    avg_vol  = sum(c["vol"] for c in candles[-20:-1]) / 19
    current_atr = atr_vals[-1]
    body     = abs(last["close"] - last["open"])
    range_   = last["high"] - last["low"]
    if range_ == 0:
        return None
    body_pct = body / range_

    rsi_vals = rsi(closes, 14)
    if not rsi_vals:
        return None

    # Bullish surge
    if (last["close"] > last["open"] and
            body_pct > 0.7 and
            body > current_atr * 1.2 and
            last["vol"] > avg_vol * 1.5 and
            rsi_vals[-1] > 55):
        conf = min(88, 60 + int(body_pct * 20) + int(min(last["vol"]/avg_vol - 1, 1.5) * 8))
        tp   = last["close"] + body * 1.5
        sl   = last["open"] - current_atr * 0.4
        return {"direction": "LONG", "strategy": "Bullish Momentum Surge",
                "entry": last["close"], "tp": tp, "sl": sl, "confidence": conf,
                "note": f"Body {body_pct*100:.0f}% of range | {last['vol']/avg_vol:.1f}x volume"}

    # Bearish surge
    if (last["close"] < last["open"] and
            body_pct > 0.7 and
            body > current_atr * 1.2 and
            last["vol"] > avg_vol * 1.5 and
            rsi_vals[-1] < 45):
        conf = min(88, 60 + int(body_pct * 20) + int(min(last["vol"]/avg_vol - 1, 1.5) * 8))
        tp   = last["close"] - body * 1.5
        sl   = last["open"] + current_atr * 0.4
        return {"direction": "SHORT", "strategy": "Bearish Momentum Surge",
                "entry": last["close"], "tp": tp, "sl": sl, "confidence": conf,
                "note": f"Body {body_pct*100:.0f}% of range | {last['vol']/avg_vol:.1f}x volume"}
    return None

def strategy_fakeout_reversal(candles, closes, highs, lows, atr_vals) -> dict | None:
    """Detects fakeouts: price spikes past a key level then snaps back."""
    if len(candles) < 50 or len(atr_vals) < 1:
        return None
    recent_high = max(highs[-20:-1])
    recent_low  = min(lows[-20:-1])
    last = candles[-1]
    prev = candles[-2]
    current_atr = atr_vals[-1]

    # Bull trap fakeout (spike above high, then closed back below)
    if (prev["high"] > recent_high and
            prev["close"] < recent_high and
            last["close"] < prev["low"]):
        conf = 74
        tp   = last["close"] - current_atr * 2.5
        sl   = prev["high"] + current_atr * 0.3
        return {"direction": "SHORT", "strategy": "Bull Trap / Fakeout Short",
                "entry": last["close"], "tp": tp, "sl": sl, "confidence": conf,
                "note": f"Fakeout above ${recent_high:,.0f}, now rejected"}

    # Bear trap fakeout (spike below low, then recovered)
    if (prev["low"] < recent_low and
            prev["close"] > recent_low and
            last["close"] > prev["high"]):
        conf = 74
        tp   = last["close"] + current_atr * 2.5
        sl   = prev["low"] - current_atr * 0.3
        return {"direction": "LONG", "strategy": "Bear Trap / Fakeout Long",
                "entry": last["close"], "tp": tp, "sl": sl, "confidence": conf,
                "note": f"Fakeout below ${recent_low:,.0f}, now recovered"}
    return None

def strategy_bollinger_squeeze(candles, closes, atr_vals) -> dict | None:
    """Bollinger band squeeze followed by expansion = volatility breakout."""
    if len(closes) < 40 or len(atr_vals) < 1:
        return None
    upper, mid, lower = bollinger(closes, 20)
    if len(upper) < 10:
        return None
    current_atr = atr_vals[-1]
    bw_now  = (upper[-1] - lower[-1]) / mid[-1]   # band width now
    bw_prev = max((upper[-i] - lower[-i]) / mid[-i] for i in range(2, 11))
    # Squeeze condition: band width was compressed; now expanding
    if bw_prev < 0.015 or bw_now < bw_prev * 1.1:
        return None
    last_close = closes[-1]
    # direction = momentum
    if last_close > mid[-1] and last_close > closes[-3]:
        conf = 67
        tp   = upper[-1] + current_atr * 1.5
        sl   = mid[-1] - current_atr * 0.5
        return {"direction": "LONG", "strategy": "Bollinger Squeeze Breakout",
                "entry": last_close, "tp": tp, "sl": sl, "confidence": conf,
                "note": f"BB width expanding from {bw_prev*100:.2f}% to {bw_now*100:.2f}%"}
    if last_close < mid[-1] and last_close < closes[-3]:
        conf = 67
        tp   = lower[-1] - current_atr * 1.5
        sl   = mid[-1] + current_atr * 0.5
        return {"direction": "SHORT", "strategy": "Bollinger Squeeze Breakdown",
                "entry": last_close, "tp": tp, "sl": sl, "confidence": conf,
                "note": f"BB width expanding from {bw_prev*100:.2f}% to {bw_now*100:.2f}%"}
    return None

def strategy_macd_divergence(candles, closes, atr_vals) -> dict | None:
    """MACD histogram divergence — price makes new low/high but MACD doesn't."""
    if len(closes) < 60 or len(atr_vals) < 1:
        return None
    _, _, hist = macd(closes)
    if len(hist) < 20:
        return None
    current_atr = atr_vals[-1]
    last_close  = closes[-1]

    # Bullish divergence: lower price low, higher MACD low
    if (closes[-1] < closes[-10] and
            hist[-1] > hist[-10] and
            hist[-1] < 0):
        conf = 70
        tp   = last_close + current_atr * 2.5
        sl   = last_close - current_atr * 1.2
        return {"direction": "LONG", "strategy": "MACD Bullish Divergence",
                "entry": last_close, "tp": tp, "sl": sl, "confidence": conf,
                "note": f"Price lower but MACD histogram recovering"}

    # Bearish divergence: higher price high, lower MACD high
    if (closes[-1] > closes[-10] and
            hist[-1] < hist[-10] and
            hist[-1] > 0):
        conf = 70
        tp   = last_close - current_atr * 2.5
        sl   = last_close + current_atr * 1.2
        return {"direction": "SHORT", "strategy": "MACD Bearish Divergence",
                "entry": last_close, "tp": tp, "sl": sl, "confidence": conf,
                "note": f"Price higher but MACD histogram weakening"}
    return None

def strategy_volume_poc_reaction(candles, closes, atr_vals) -> dict | None:
    """Price reaction to volume Point of Control — high-probability magnet zone."""
    if len(candles) < 100 or len(atr_vals) < 1:
        return None
    vp = volume_profile(candles[-100:])
    poc  = vp["poc"]
    last = closes[-1]
    current_atr = atr_vals[-1]
    dist = abs(last - poc)

    if dist > current_atr * 0.8:
        return None

    rsi_vals = rsi(closes, 14)
    if not rsi_vals:
        return None

    # Bouncing off POC upward
    if last >= poc and rsi_vals[-1] < 55:
        conf = 66
        tp   = last + current_atr * 2.0
        sl   = poc - current_atr * 1.0
        return {"direction": "LONG", "strategy": "Volume POC Bounce",
                "entry": last, "tp": tp, "sl": sl, "confidence": conf,
                "note": f"POC at ${poc:,.0f} acting as support | RSI={rsi_vals[-1]:.0f}"}

    # Rejecting off POC downward
    if last <= poc and rsi_vals[-1] > 45:
        conf = 66
        tp   = last - current_atr * 2.0
        sl   = poc + current_atr * 1.0
        return {"direction": "SHORT", "strategy": "Volume POC Rejection",
                "entry": last, "tp": tp, "sl": sl, "confidence": conf,
                "note": f"POC at ${poc:,.0f} acting as resistance | RSI={rsi_vals[-1]:.0f}"}
    return None

# ─── SIGNAL AGGREGATOR ───────────────────────────────────────────────────────

ALL_STRATEGIES = [
    strategy_breakout,
    strategy_trend_pullback,
    strategy_support_resistance_flip,
    strategy_momentum_surge,
    strategy_fakeout_reversal,
    strategy_bollinger_squeeze,
    strategy_macd_divergence,
    strategy_volume_poc_reaction,
]

def analyse(candles: list[dict]) -> dict | None:
    """Run all strategies; return highest-confidence signal, or None."""
    if len(candles) < 60:
        return None
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    atr_v  = atr(candles, 14)

    results = []
    for strat in ALL_STRATEGIES:
        try:
            sig = strat(candles, *[x for x in [closes, highs, lows, atr_v]
                                    if True][:strat.__code__.co_argcount - 1])
        except TypeError:
            # strategies take different arg subsets — route correctly
            sig = None
            try:
                n = strat.__code__.co_varnames[:strat.__code__.co_argcount]
                kwargs = {}
                mapping = {"candles": candles, "closes": closes,
                           "highs": highs, "lows": lows, "atr_vals": atr_v}
                kwargs = {k: mapping[k] for k in n if k in mapping}
                sig = strat(**kwargs)
            except Exception as e:
                log.debug(f"Strategy {strat.__name__} error: {e}")
        if sig and sig["confidence"] >= MIN_CONFIDENCE:
            results.append(sig)

    if not results:
        return None
    best = max(results, key=lambda x: x["confidence"])
    # bonus if multiple strategies agree on direction
    same_dir = [r for r in results if r["direction"] == best["direction"]]
    if len(same_dir) > 1:
        best["confidence"] = min(95, best["confidence"] + (len(same_dir) - 1) * 3)
        best["confluence"] = [r["strategy"] for r in same_dir if r["strategy"] != best["strategy"]]
    return best

# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — printing signal instead:")
        print(text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.warning(f"Telegram send error: {e}")
        return False

def format_signal(sig: dict, btc_price: float) -> str:
    arrow   = "🟢" if sig["direction"] == "LONG" else "🔴"
    rr      = abs(sig["tp"] - sig["entry"]) / abs(sig["entry"] - sig["sl"])
    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    conf    = sig["confidence"]
    bars    = "█" * (conf // 10) + "░" * (10 - conf // 10)

    lines = [
        f"{arrow} <b>BTC {sig['direction']} SIGNAL</b>",
        f"",
        f"📊 <b>Strategy:</b> {sig['strategy']}",
        f"🕐 <b>Time:</b> {now_str}",
        f"",
        f"💰 <b>Entry:</b>  ${sig['entry']:,.2f}",
        f"🎯 <b>TP:</b>     ${sig['tp']:,.2f}  (+{abs(sig['tp']-sig['entry'])/sig['entry']*100:.2f}%)",
        f"🛑 <b>SL:</b>     ${sig['sl']:,.2f}  (-{abs(sig['entry']-sig['sl'])/sig['entry']*100:.2f}%)",
        f"",
        f"⚖️  <b>R:R ratio:</b> 1 : {rr:.1f}",
        f"🔥 <b>Confidence:</b> {conf}%  {bars}",
    ]
    if "note" in sig:
        lines += [f"", f"📝 <i>{sig['note']}</i>"]
    if sig.get("confluence"):
        lines += [f"✅ <b>Also confirmed by:</b> {', '.join(sig['confluence'])}"]
    lines += [f"", f"⚠️ <i>Not financial advice. DYOR.</i>"]
    return "\n".join(lines)

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def run():
    log.info("═" * 55)
    log.info("  BTC Signal Bot starting up")
    log.info(f"  Symbol: {SYMBOL}  |  Interval: {INTERVAL}")
    log.info(f"  Min confidence: {MIN_CONFIDENCE}%  |  Cooldown: {COOLDOWN_SEC}s")
    log.info("═" * 55)

    if not TELEGRAM_TOKEN:
        log.warning("⚠  TELEGRAM_TOKEN not set — signals will print to console only")

    last_alert_time = 0
    candle_buffer: deque[dict] = deque(maxlen=LOOKBACK_CANDLES)

    # prime the buffer
    initial = fetch_klines(SYMBOL, INTERVAL, LOOKBACK_CANDLES)
    candle_buffer.extend(initial)
    log.info(f"Loaded {len(initial)} initial candles")

    while True:
        try:
            new_candles = fetch_klines(SYMBOL, INTERVAL, 5)
            if new_candles:
                # update / append latest candle
                latest = new_candles[-1]
                if candle_buffer and candle_buffer[-1]["ts"] == latest["ts"]:
                    candle_buffer[-1] = latest
                else:
                    candle_buffer.append(latest)

            candles = list(candle_buffer)
            price   = candles[-1]["close"] if candles else 0.0
            log.info(f"BTC ${price:,.2f}  |  candles={len(candles)}")

            now = time.time()
            if now - last_alert_time < COOLDOWN_SEC:
                secs_left = int(COOLDOWN_SEC - (now - last_alert_time))
                log.info(f"  (cooldown — {secs_left}s remaining)")
            else:
                signal = analyse(candles)
                if signal:
                    log.info(f"  ✨ Signal found: {signal['direction']} | {signal['strategy']} | {signal['confidence']}%")
                    msg = format_signal(signal, price)
                    ok  = send_telegram(msg)
                    if ok:
                        log.info("  📨 Telegram message sent")
                    last_alert_time = now
                else:
                    log.info("  No high-confidence setup detected")

        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")

        time.sleep(FETCH_INTERVAL_SEC)

if __name__ == "__main__":
    # Route strategy args correctly (patch __code__ issue for simpler dispatch)
    # Re-map strategies with explicit arg routing
    def _run_strat(strat, candles, closes, highs, lows, atr_vals):
        n = strat.__code__.co_varnames[:strat.__code__.co_argcount]
        mapping = {"candles": candles, "closes": closes,
                   "highs": highs, "lows": lows, "atr_vals": atr_vals}
        kwargs = {k: mapping[k] for k in n if k in mapping}
        return strat(**kwargs)

    # Patch analyse() to use the router
    def analyse(candles: list[dict]) -> dict | None:
        if len(candles) < 60:
            return None
        closes = [c["close"] for c in candles]
        highs  = [c["high"]  for c in candles]
        lows   = [c["low"]   for c in candles]
        atr_v  = atr(candles, 14)
        results = []
        for strat in ALL_STRATEGIES:
            try:
                sig = _run_strat(strat, candles, closes, highs, lows, atr_v)
                if sig and sig["confidence"] >= MIN_CONFIDENCE:
                    results.append(sig)
            except Exception as e:
                log.debug(f"{strat.__name__} skipped: {e}")
        if not results:
            return None
        best = max(results, key=lambda x: x["confidence"])
        same_dir = [r for r in results if r["direction"] == best["direction"]]
        if len(same_dir) > 1:
            best["confidence"] = min(95, best["confidence"] + (len(same_dir) - 1) * 3)
            best["confluence"] = [r["strategy"] for r in same_dir if r["strategy"] != best["strategy"]]
        return best

    run()

