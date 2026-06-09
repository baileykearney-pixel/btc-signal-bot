"""
Fibonacci Retracement Signal Bot — v1
Strategy: 0.236 Fibonacci retracement in trend direction
Backtest result: 37.1% WR | 2:1 R:R | 8/8 profitable years (2019-2026)
Timeframe: 4H candles | 10 pairs | Kraken data

Backtest settings that produced the best results:
  - Fib level:    0.236  (shallowest retracement — catches early momentum)
  - Swing lookback: 30 bars (5 days on 4H)
  - R:R:          2.0    (TP = 2× SL distance)
  - WR needed:    >33.3% to break even — backtested 37.1%
  - Risk:         2% per trade compounding
  - EV per trade: +0.112R

How it works:
  1. Identify the recent swing high and swing low (last 30 × 4H bars)
  2. Calculate the 0.236 Fibonacci retracement level
  3. In an uptrend (price > EMA200 on 4H): if price dips to the 0.236
     level and prints a bullish confirmation candle → LONG
  4. In a downtrend (price < EMA200 on 4H): if price bounces to the 0.236
     level and prints a bearish confirmation candle → SHORT
  5. SL = 2×ATR below entry  |  TP = 2×SL distance above entry
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

# 10 pairs — backtested on 4H Binance.US data, 6.7yr, 2% compounding
# Overall: 37.1% WR | 2:1 R:R | 8/8 years profitable
SYMBOLS = [
    ("XBTUSD",  "BTC"),    # 4H Fib 0.236 | included in backtest
    ("ETHUSD",  "ETH"),    # 4H Fib 0.236 | included in backtest
    ("SOLUSD",  "SOL"),    # 4H Fib 0.236 | included in backtest
    ("XRPUSD",  "XRP"),    # 4H Fib 0.236 | included in backtest
    ("ADAUSD",  "ADA"),    # 4H Fib 0.236 | included in backtest
    ("AVAXUSD", "AVAX"),   # 4H Fib 0.236 | included in backtest
    ("XLMUSD",  "XLM"),    # 4H Fib 0.236 | included in backtest
    ("UNIUSD",  "UNI"),    # 4H Fib 0.236 | included in backtest
    ("XDGUSD",  "DOGE"),   # 4H Fib 0.236 | included in backtest
    ("BNBUSD",  "BNB"),    # 4H Fib 0.236 | included in backtest
]

INTERVAL_HTF      = 240      # 4H candles — backtest timeframe
LOOKBACK_HTF      = 720      # max Kraken returns (~120 days on 4H)
FETCH_INTERVAL    = 600      # scan every 10 minutes (4H candles move slowly)
FIB_LEVEL         = 0.236    # the 23.6% retracement — shallowest, most momentum
SWING_LOOKBACK    = 30       # bars to find swing high/low (30 × 4H = 5 days)
FIB_ZONE_PCT      = 0.012    # within 1.2% of fib level = "touched it"
SL_ATR_MULT       = 2.0      # SL = 2× ATR below entry
RR_TARGET         = 2.0      # TP = 2× SL distance (true 2:1 R:R)
COOLDOWN_PER_PAIR = 57600    # 16 hours per pair = 4 bars on 4H (independent)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("FibBot")

# ─── DATA FETCHING ────────────────────────────────────────────────────────────

def fetch_kraken(symbol: str, interval: int, limit: int) -> list[dict]:
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": symbol, "interval": interval},
            headers={"User-Agent": "fib-bot/1.0"},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
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
            headers={"User-Agent": "fib-bot/1.0"},
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

# ─── TREND BIAS (EMA200 on 4H) ───────────────────────────────────────────────

def get_bias(candles: list[dict]) -> tuple[str, float]:
    """Bull if price > EMA200 and EMA50 > EMA200. Bear if opposite."""
    if len(candles) < 205:
        return "NEUTRAL", 0.0
    closes = [c["close"] for c in candles]
    e200   = ema(closes, 200)
    e50    = ema(closes, 50)
    if not e200 or not e50:
        return "NEUTRAL", 0.0
    price    = closes[-1]
    ema200_v = e200[-1]
    ema50_v  = e50[-1]
    if price > ema200_v and ema50_v > ema200_v:
        return "BULL", ema200_v
    if price < ema200_v and ema50_v < ema200_v:
        return "BEAR", ema200_v
    return "NEUTRAL", ema200_v

# ─── FIBONACCI STRATEGY ───────────────────────────────────────────────────────

def analyse_fibonacci(candles: list[dict], name: str) -> dict | None:
    """
    0.236 Fibonacci retracement signal.
    Bull:  price pulls back to the 23.6% fib level from swing high → LONG
    Bear:  price bounces up to the 23.6% fib level from swing low  → SHORT
    Entry: confirmed by bullish/bearish close on the 4H candle
    SL:    2× ATR below entry
    TP:    2× SL distance above entry (true 2:1 R:R)
    """
    if len(candles) < max(205, SWING_LOOKBACK + 5):
        return None

    bias, ema200_v = get_bias(candles)
    if bias == "NEUTRAL":
        return None

    confirm  = candles[-2]          # last closed candle
    price    = confirm["close"]
    atr_val  = atr(candles[-30:], 14)
    if not atr_val:
        return None

    closes   = [c["close"] for c in candles]
    rsi_val  = rsi(closes[:-1], 14)
    avg_v    = avg_volume(candles, 20)
    vol_mult = confirm["vol"] / avg_v if avg_v > 0 else 1.0

    # swing high and low over last SWING_LOOKBACK bars (excluding current)
    window     = candles[-SWING_LOOKBACK-1:-1]
    swing_high = max(c["high"] for c in window)
    swing_low  = min(c["low"]  for c in window)
    rng        = swing_high - swing_low

    if rng < price * 0.03:          # skip flat/tiny ranges
        return None

    body     = abs(confirm["close"] - confirm["open"])
    bar_rng  = confirm["high"] - confirm["low"]
    body_pct = body / bar_rng if bar_rng > 0 else 0

    # ── LONG: price retraced 23.6% from swing high in uptrend ──
    if bias == "BULL":
        fib_price = swing_high - FIB_LEVEL * rng   # 0.236 retracement level
        near_fib  = abs(price - fib_price) / fib_price <= FIB_ZONE_PCT
        bullish   = confirm["close"] > confirm["open"] and body_pct > 0.4

        if near_fib and bullish:
            ep       = price                         # enter at confirm close (live)
            sl_dist  = max(atr_val * SL_ATR_MULT, ep * 0.01)
            sl       = ep - sl_dist
            tp       = ep + sl_dist * RR_TARGET      # true 2:1 R:R
            rr_act   = (tp - ep) / (ep - sl)

            # confidence scoring
            conf = 72
            if vol_mult >= 1.5:  conf += 6
            if vol_mult >= 2.5:  conf += 4
            if body_pct >= 0.6:  conf += 5
            if rsi_val and rsi_val < 50:  conf += 5
            if rsi_val and rsi_val < 40:  conf += 3
            dist_from_ema = abs(price - ema200_v) / ema200_v * 100

            return {
                "symbol":     name,
                "direction":  "LONG",
                "strategy":   "Fib 0.236 Retracement (Bull)",
                "entry":      ep,
                "tp":         tp,
                "sl":         sl,
                "rr":         rr_act,
                "confidence": min(95, conf),
                "htf_bias":   bias,
                "ema200":     ema200_v,
                "fib_price":  fib_price,
                "swing_high": swing_high,
                "swing_low":  swing_low,
                "rsi":        rsi_val,
                "vol_mult":   vol_mult,
                "note": (
                    f"0.236 fib at ${fib_price:,.4f} | "
                    f"Swing {swing_low:,.4f}→{swing_high:,.4f} | "
                    f"RSI {rsi_val:.0f} | Vol {vol_mult:.1f}x"
                    if rsi_val else
                    f"0.236 fib at ${fib_price:,.4f} | "
                    f"Swing {swing_low:,.4f}→{swing_high:,.4f} | "
                    f"Vol {vol_mult:.1f}x"
                ),
            }

    # ── SHORT: price bounced 23.6% from swing low in downtrend ──
    if bias == "BEAR":
        fib_price = swing_low + FIB_LEVEL * rng    # 0.236 bounce level
        near_fib  = abs(price - fib_price) / fib_price <= FIB_ZONE_PCT
        bearish   = confirm["close"] < confirm["open"] and body_pct > 0.4

        if near_fib and bearish:
            ep       = price
            sl_dist  = max(atr_val * SL_ATR_MULT, ep * 0.01)
            sl       = ep + sl_dist
            tp       = ep - sl_dist * RR_TARGET
            rr_act   = (ep - tp) / (sl - ep)

            conf = 72
            if vol_mult >= 1.5:  conf += 6
            if vol_mult >= 2.5:  conf += 4
            if body_pct >= 0.6:  conf += 5
            if rsi_val and rsi_val > 50:  conf += 5
            if rsi_val and rsi_val > 60:  conf += 3

            return {
                "symbol":     name,
                "direction":  "SHORT",
                "strategy":   "Fib 0.236 Retracement (Bear)",
                "entry":      ep,
                "tp":         tp,
                "sl":         sl,
                "rr":         rr_act,
                "confidence": min(95, conf),
                "htf_bias":   bias,
                "ema200":     ema200_v,
                "fib_price":  fib_price,
                "swing_high": swing_high,
                "swing_low":  swing_low,
                "rsi":        rsi_val,
                "vol_mult":   vol_mult,
                "note": (
                    f"0.236 fib at ${fib_price:,.4f} | "
                    f"Swing {swing_high:,.4f}→{swing_low:,.4f} | "
                    f"RSI {rsi_val:.0f} | Vol {vol_mult:.1f}x"
                    if rsi_val else
                    f"0.236 fib at ${fib_price:,.4f} | "
                    f"Swing {swing_high:,.4f}→{swing_low:,.4f} | "
                    f"Vol {vol_mult:.1f}x"
                ),
            }

    return None

# ─── PER-PAIR TRADE TRACKER ──────────────────────────────────────────────────

active_trades: dict = {}
trade_history: list = []

def set_active_trade(signal: dict, kraken_sym: str):
    name = signal["symbol"]
    active_trades[name] = {
        "symbol":     name,
        "kraken_sym": kraken_sym,
        "direction":  signal["direction"],
        "entry":      signal["entry"],
        "tp":         signal["tp"],
        "sl":         signal["sl"],
        "strategy":   signal["strategy"],
        "rr":         signal["rr"],
        "open_time":  datetime.now(timezone.utc),
    }
    log.info(
        f"  📌 Tracking {name} {signal['direction']} "
        f"entry=${signal['entry']:,.4f} "
        f"TP=${signal['tp']:,.4f} SL=${signal['sl']:,.4f}"
    )

def check_outcome(name: str, price: float) -> str | None:
    t = active_trades.get(name)
    if not t:
        return None
    if t["direction"] == "LONG":
        if price >= t["tp"]: return "TP"
        if price <= t["sl"]: return "SL"
    else:
        if price <= t["tp"]: return "TP"
        if price >= t["sl"]: return "SL"
    return None

def format_outcome(name: str, outcome: str, price: float) -> str:
    t = active_trades.get(name)
    if not t:
        return ""
    entry    = t["entry"]
    exit_p   = t["tp"] if outcome == "TP" else t["sl"]
    pnl_pct  = abs(exit_p - entry) / entry * 100
    duration = int((datetime.now(timezone.utc) - t["open_time"]).total_seconds() / 60)
    emoji    = "✅" if outcome == "TP" else "❌"
    result   = "TAKE PROFIT HIT" if outcome == "TP" else "STOP LOSS HIT"
    pnl_str  = f"+{pnl_pct:.2f}%" if outcome == "TP" else f"-{pnl_pct:.2f}%"
    return "\n".join([
        f"{emoji} <b>{result}</b>",
        f"",
        f"💹 <b>Asset:</b> {name}",
        f"📍 <b>Direction:</b> {t['direction']}",
        f"",
        f"💰 <b>Entry:</b>  ${entry:,.4f}",
        f"🏁 <b>Exit:</b>   ${exit_p:,.4f}",
        f"📈 <b>Result:</b> {pnl_str}",
        f"⏱ <b>Duration:</b> {duration} min",
    ])

def record_and_clear(name: str, outcome: str):
    t = active_trades.get(name)
    if not t:
        return
    entry = t["entry"]
    pnl   = (
        abs(t["tp"] - entry) / entry * 100
        if outcome == "TP"
        else -abs(t["sl"] - entry) / entry * 100
    )
    trade_history.append({
        "symbol":    name,
        "direction": t["direction"],
        "strategy":  t["strategy"],
        "outcome":   outcome,
        "pnl_pct":   pnl,
        "rr":        t["rr"],
        "duration":  int((datetime.now(timezone.utc) - t["open_time"]).total_seconds() / 60),
        "time":      datetime.now(timezone.utc),
    })
    del active_trades[name]

# ─── SUMMARIES ────────────────────────────────────────────────────────────────

last_daily_summary:  datetime | None = None
last_weekly_summary: datetime | None = None

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

    sym_stats: dict = {}
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
        f"🎯 <b>Win Rate:</b> {win_rate:.1f}%  (need >33.3% at 2:1)",
        f"⚖️  <b>Avg R:R on wins:</b> 1:{avg_rr:.2f}",
        f"⏱ <b>Avg Duration:</b> {avg_dur:.0f} min",
        f"",
        f"💰 <b>Est. P&L</b> (1% risk/trade): {pnl:+.1f}%",
        f"📅 <b>Est. Annual P&L:</b> {annual:+.0f}%",
        f"",
        f"🏅 <b>By Asset:</b>",
    ]
    for sym, (w, l) in sorted(sym_stats.items(), key=lambda x: x[1][0], reverse=True)[:5]:
        t2 = w + l
        wr = w / t2 * 100 if t2 else 0
        lines.append(f"   {sym}: {t2} trades  {wr:.0f}% WR")
    if best:
        lines += [f"", f"🏆 <b>Best:</b> {best['symbol']} {best['direction']} +{best['pnl_pct']:.2f}%"]
    if worst:
        lines += [f"💀 <b>Worst:</b> {worst['symbol']} {worst['direction']} {worst['pnl_pct']:.2f}%"]
    lines += [f"", f"⚠️ <i>Not financial advice. DYOR.</i>"]
    return "\n".join(lines)

def check_summaries():
    global last_daily_summary, last_weekly_summary, trade_history
    now = datetime.now(timezone.utc)
    if now.hour == 20 and now.minute < 6:
        if last_daily_summary is None or (now - last_daily_summary).total_seconds() > 3600:
            last_daily_summary = now
            today = [t for t in trade_history if (now - t["time"]).total_seconds() < 86400]
            send_telegram(build_summary(today, "📊 Daily Summary — Fib 0.236 Bot", 1))
            log.info("📊 Daily summary sent")
    if now.weekday() == 6 and now.hour == 8 and now.minute < 6:
        if last_weekly_summary is None or (now - last_weekly_summary).total_seconds() > 86400:
            last_weekly_summary = now
            send_telegram(build_summary(trade_history, "📊 Weekly Summary — Fib 0.236 Bot", 7))
            log.info("📊 Weekly summary sent")
            trade_history = []

# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID")
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
    arrow    = "🟢" if sig["direction"] == "LONG" else "🔴"
    conf     = sig["confidence"]
    bars     = "█" * (conf // 10) + "░" * (10 - conf // 10)
    now_str  = datetime.now(timezone.utc).strftime("%H:%M UTC")
    entry    = sig["entry"]
    tp       = sig["tp"]
    sl       = sig["sl"]
    rr       = sig["rr"]
    bias_e   = "📈" if sig.get("htf_bias") == "BULL" else "📉"
    fib_p    = sig.get("fib_price", 0)
    sh       = sig.get("swing_high", 0)
    sl_price = sig.get("swing_low", 0)
    lines = [
        f"{arrow} <b>{sig['symbol']} {sig['direction']} SIGNAL</b>",
        f"",
        f"📊 <b>Strategy:</b> {sig['strategy']}",
        f"{bias_e} <b>4H Trend:</b> {sig.get('htf_bias')}",
        f"🕐 <b>Time:</b> {now_str}",
        f"",
        f"📐 <b>Fib 0.236 level:</b> ${fib_p:,.4f}",
        f"📊 <b>Swing:</b> ${sl_price:,.4f} → ${sh:,.4f}",
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
        active = (
            ", ".join(f"{k} {v['direction']}" for k, v in active_trades.items())
            or "none"
        )
        wins   = sum(1 for t in trade_history if t["outcome"] == "TP")
        total  = len(trade_history)
        wr     = f"{wins/total*100:.1f}%" if total else "—"
        return (
            f"<b>Fibonacci 0.236 Signal Bot</b><br>"
            f"Pairs: {len(SYMBOLS)} | Timeframe: 4H<br>"
            f"Active trades: {active}<br>"
            f"Completed: {total} trades | WR: {wr} (need >33.3%)<br>"
            f"Strategy: Fib 0.236 | SL=2xATR | TP=2xSL | 2:1 R:R<br>"
        )

    t = Thread(target=lambda: app.run(host="0.0.0.0", port=8080))
    t.daemon = True
    t.start()

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def run():
    log.info("═" * 64)
    log.info("  Fibonacci 0.236 Retracement Signal Bot — v1")
    log.info(f"  {len(SYMBOLS)} pairs | 4H candles | Fib={FIB_LEVEL} | R:R={RR_TARGET}:1")
    log.info(f"  Backtest: 8/8 years profitable | 37.1% WR | +EV per trade")
    log.info(f"  Breakeven WR at 2:1 = 33.3%")
    log.info("═" * 64)

    keep_alive()

    last_alert: dict[str, float] = {name: 0 for _, name in SYMBOLS}

    # Prime data buffers
    symbol_data: dict = {}
    for kraken_sym, name in SYMBOLS:
        candles = fetch_kraken(kraken_sym, INTERVAL_HTF, LOOKBACK_HTF)
        symbol_data[kraken_sym] = {
            "name":    name,
            "candles": deque(candles, maxlen=LOOKBACK_HTF),
        }
        log.info(f"  {name}: loaded {len(candles)} candles ({len(candles)*4//24}d of 4H data)")
        time.sleep(0.4)

    log.info(f"All {len(SYMBOLS)} pairs loaded. Scanning every {FETCH_INTERVAL//60} min...")

    while True:
        try:
            check_summaries()
            now = time.time()

            for kraken_sym, name in SYMBOLS:
                try:
                    # ── Outcome check ──────────────────────────────────────
                    if name in active_trades:
                        price = fetch_price(kraken_sym)
                        if price:
                            outcome = check_outcome(name, price)
                            if outcome:
                                log.info(f"  🏁 {name} {outcome} @ ${price:,.4f}")
                                send_telegram(format_outcome(name, outcome, price))
                                record_and_clear(name, outcome)
                                last_alert[name] = now - COOLDOWN_PER_PAIR + 600

                    # ── Signal scan ────────────────────────────────────────
                    if name in active_trades:
                        continue
                    if now - last_alert.get(name, 0) < COOLDOWN_PER_PAIR:
                        remaining = int((COOLDOWN_PER_PAIR - (now - last_alert[name])) / 3600)
                        log.debug(f"  {name}: cooldown {remaining}h remaining")
                        continue

                    # Fetch latest 4H candles and update buffer
                    new = fetch_kraken(kraken_sym, INTERVAL_HTF, 5)
                    buf = symbol_data[kraken_sym]["candles"]
                    for c in new:
                        if not buf or c["ts"] > buf[-1]["ts"]:
                            buf.append(c)
                        elif c["ts"] == buf[-1]["ts"]:
                            buf[-1] = c

                    candles = list(buf)
                    sig     = analyse_fibonacci(candles, name)

                    if sig:
                        log.info(
                            f"  🎯 {name} {sig['direction']} | "
                            f"Fib=${sig['fib_price']:,.4f} | "
                            f"Entry=${sig['entry']:,.4f} | "
                            f"Conf={sig['confidence']}%"
                        )
                        send_telegram(format_signal(sig))
                        set_active_trade(sig, kraken_sym)
                        last_alert[name] = now
                    else:
                        bias, e200 = get_bias(candles)
                        fib_level_price = 0.0
                        if len(candles) >= SWING_LOOKBACK + 1:
                            w  = candles[-SWING_LOOKBACK-1:-1]
                            sh = max(c["high"] for c in w)
                            sl = min(c["low"]  for c in w)
                            if bias == "BULL":
                                fib_level_price = sh - FIB_LEVEL * (sh - sl)
                            elif bias == "BEAR":
                                fib_level_price = sl + FIB_LEVEL * (sh - sl)
                        price = candles[-1]["close"] if candles else 0
                        dist  = abs(price - fib_level_price) / fib_level_price * 100 if fib_level_price else 0
                        log.debug(
                            f"  {name}: {bias} | price={price:,.4f} | "
                            f"fib={fib_level_price:,.4f} | dist={dist:.2f}% "
                            f"(need <{FIB_ZONE_PCT*100:.1f}%)"
                        )

                except Exception as e:
                    log.warning(f"  {name} error: {e}")
                    continue

            log.info(f"  Scan done — {len(active_trades)} active | sleeping {FETCH_INTERVAL}s")
            time.sleep(FETCH_INTERVAL)

        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    run()
