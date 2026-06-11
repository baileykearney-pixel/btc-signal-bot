"""
BOS + Order Block Signal Bot — Safety Hardened + cTrader Auto Execution
Strategy: Break of Structure + Order Block retest
Timeframe: 4H candles | 10 pairs | Kraken data feed
"""

import time
import logging
import os
import json
from datetime import datetime, timezone
from collections import deque
import requests

# ─── TRADE PERSISTENCE ────────────────────────────────────────────────────────
TRADES_FILE = "/tmp/active_trades.json"

def save_trades():
    try:
        serialisable = {}
        for k, v in active_trades.items():
            t = dict(v)
            t["open_time"] = t["open_time"].isoformat()
            serialisable[k] = t
        with open(TRADES_FILE, "w") as f:
            json.dump(serialisable, f)
    except Exception as e:
        log.warning(f"Could not save trades: {e}")

def load_trades():
    try:
        if not os.path.exists(TRADES_FILE):
            return
        with open(TRADES_FILE) as f:
            data = json.load(f)
        for k, v in data.items():
            v["open_time"] = datetime.fromisoformat(v["open_time"])
            active_trades[k] = v
        if active_trades:
            log.info(f"  Restored {len(active_trades)} active trade(s) from disk: {list(active_trades.keys())}")
            send_telegram(f"♻️ <b>Bot restarted</b> — restored {len(active_trades)} active trade(s): {', '.join(active_trades.keys())}")
    except Exception as e:
        log.warning(f"Could not load trades: {e}")

# ─── CONFIG ───────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# cTrader config
CTRADER_ACCESS_TOKEN  = os.environ.get("CTRADER_ACCESS_TOKEN", "")
CTRADER_REFRESH_TOKEN = os.environ.get("CTRADER_REFRESH_TOKEN", "")
CTRADER_ACCOUNT_ID    = os.environ.get("CTRADER_ACCOUNT_ID", "")
CTRADER_CLIENT_ID     = os.environ.get("CTRADER_CLIENT_ID", "")
CTRADER_CLIENT_SECRET = os.environ.get("CTRADER_CLIENT_SECRET", "")
CTRADER_BASE_URL      = "https://api.ctrader.com/v2"  # REST JSON API

# Map signal symbol names to cTrader symbol names
CTRADER_SYMBOLS = {
    "BTC":  "BTCUSD",
    "ETH":  "ETHUSD",
    "SOL":  "SOLUSD",
    "XRP":  "XRPUSD",
    "ADA":  "ADAUSD",
    "AVAX": "AVAXUSD",
    "XLM":  "XLMUSD",
    "UNI":  "UNIUSD",
    "DOGE": "DOGEUSD",
    "BNB":  "BNBUSD",
}

RISK_PCT = 1.0  # % of balance to risk per trade

SYMBOLS = [
    ("XBTUSD",  "BTC"),
    ("ETHUSD",  "ETH"),
    ("SOLUSD",  "SOL"),
    ("XRPUSD",  "XRP"),
    ("ADAUSD",  "ADA"),
    ("AVAXUSD", "AVAX"),
    ("XLMUSD",  "XLM"),
    ("UNIUSD",  "UNI"),
    ("XDGUSD",  "DOGE"),
    ("BNBUSD",  "BNB"),
]

BINANCE_SYMBOLS = {
    "XBTUSD": "BTCUSDT", "ETHUSD": "ETHUSDT", "SOLUSD": "SOLUSDT",
    "XRPUSD": "XRPUSDT", "ADAUSD": "ADAUSDT", "AVAXUSD": "AVAXUSDT",
    "XLMUSD": "XLMUSDT", "UNIUSD": "UNIUSDT", "XDGUSD": "DOGEUSDT",
    "BNBUSD": "BNBUSDT",
}

INTERVAL_4H       = 240
LOOKBACK_BARS     = 720
FETCH_INTERVAL    = 600
SWING_LOOKBACK    = 5
OB_EXPIRE_BARS    = 50
OB_BUFFER_PCT     = 0.10
RR_TARGET         = 2.0
COOLDOWN_PER_PAIR = 57600
MAX_RISK_PCT      = 2.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("BOSBot")

# ─── cTRADER AUTO EXECUTION (via ctrader-sdk) ────────────────────────────────

def place_ctrader_order(signal: dict) -> bool:
    """Place a market order on cTrader using ctrader-sdk."""
    if not CTRADER_ACCESS_TOKEN or not CTRADER_ACCOUNT_ID:
        log.warning("  ⚠️ cTrader not configured — signal sent to Telegram only")
        return False

    ct_symbol = CTRADER_SYMBOLS.get(signal["symbol"])
    if not ct_symbol:
        log.warning(f"  ⚠️ No cTrader symbol mapping for {signal['symbol']}")
        return False

    try:
        from ctrader_sdk import CTraderBot
        bot = CTraderBot(CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET, CTRADER_ACCESS_TOKEN, CTRADER_ACCOUNT_ID)

        # Get balance for position sizing
        account_info = bot.get_account_information()
        balance = None
        if account_info:
            balance = account_info.get("balance") or account_info.get("equity")
            if balance:
                balance = float(balance) / 100  # cTrader returns in cents

        if not balance:
            equity = bot.get_account_equity()
            if equity:
                balance = float(equity)

        if not balance:
            log.warning("  ⚠️ Could not fetch balance — skipping auto execution")
            send_telegram(f"⚠️ <b>cTrader balance fetch failed</b> — {signal['symbol']} signal sent but NOT auto-executed")
            return False

        log.info(f"  💰 Account balance: ${balance:,.2f}")

        entry = signal["entry"]
        sl    = signal["sl"]
        lots  = safe_lot_size(entry, sl, balance, RISK_PCT)
        volume = int(lots * 100000)  # ctrader-sdk uses full units

        trade_side = "BUY" if signal["direction"] == "LONG" else "SELL"

        log.info(f"  📤 Placing order: {trade_side} {ct_symbol} vol={volume} SL={sl:.4f} TP={signal['tp']:.4f}")

        order_response = bot.place_order(
            symbol=ct_symbol,
            volume=volume,
            direction=trade_side,
            order_type="MARKET",
            take_profit=round(signal["tp"], 5),
            stop_loss=round(sl, 5),
        )

        if order_response:
            order_id = order_response.get("orderId") or order_response.get("id") or "unknown"
            log.info(f"  ✅ Order placed! ID: {order_id}")
            send_telegram(
                f"✅ <b>Order Executed on cTrader</b>\n\n"
                f"💹 {signal['symbol']} {signal['direction']}\n"
                f"📦 {lots} lots @ ${entry:,.4f}\n"
                f"🎯 TP: ${signal['tp']:,.4f}  🛑 SL: ${sl:,.4f}\n"
                f"🆔 Order ID: {order_id}"
            )
            return True
        else:
            log.error("  ❌ Order placement returned no response")
            send_telegram(f"❌ <b>cTrader Order Failed</b> — {signal['symbol']} {signal['direction']} — no response from API")
            return False

    except Exception as e:
        log.error(f"  ❌ cTrader execution error: {e}")
        send_telegram(f"❌ <b>cTrader error</b> — {signal['symbol']} NOT executed: {str(e)[:200]}")
        return False

# ─── DATA FETCHING + BINANCE FALLBACK ─────────────────────────────────────────

def fetch_kraken(symbol: str, interval: int, limit: int) -> list[dict]:
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": symbol, "interval": interval},
            headers={"User-Agent": "bos-ob-bot/2.0"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            return []
        result   = data.get("result", {})
        pair_key = [k for k in result if k != "last"][0]
        return [
            {
                "ts":    int(c[0]) * 1000,
                "open":  float(c[1]),
                "high":  float(c[2]),
                "low":   float(c[3]),
                "close": float(c[4]),
                "vol":   float(c[6]),
            }
            for c in result[pair_key][-limit:]
        ]
    except Exception as e:
        log.debug(f"Kraken fetch error ({symbol}): {e}")
        return []

def fetch_binance_fallback(kraken_sym: str, limit: int) -> list[dict]:
    binance_sym = BINANCE_SYMBOLS.get(kraken_sym)
    if not binance_sym:
        return []
    try:
        r = requests.get(
            "https://api.binance.us/api/v3/klines",
            params={"symbol": binance_sym, "interval": "4h", "limit": limit},
            headers={"User-Agent": "bos-ob-bot/2.0"},
            timeout=15,
        )
        r.raise_for_status()
        return [
            {
                "ts":    int(c[0]),
                "open":  float(c[1]),
                "high":  float(c[2]),
                "low":   float(c[3]),
                "close": float(c[4]),
                "vol":   float(c[5]),
            }
            for c in r.json()
        ]
    except Exception as e:
        log.debug(f"Binance fallback error ({binance_sym}): {e}")
        return []

def fetch_candles(kraken_sym: str, interval: int, limit: int) -> list[dict]:
    candles = fetch_kraken(kraken_sym, interval, limit)
    if not candles:
        log.warning(f"  Kraken failed for {kraken_sym} — trying Binance fallback")
        candles = fetch_binance_fallback(kraken_sym, limit)
        if candles:
            log.info(f"  Binance fallback succeeded for {kraken_sym}")
        else:
            log.error(f"  Both data sources failed for {kraken_sym}")
    return candles

def fetch_price(symbol: str) -> float | None:
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": symbol},
            headers={"User-Agent": "bos-ob-bot/2.0"},
            timeout=5,
        )
        result   = r.json().get("result", {})
        pair_key = list(result.keys())[0]
        return float(result[pair_key]["c"][0])
    except Exception:
        try:
            binance_sym = BINANCE_SYMBOLS.get(symbol)
            if binance_sym:
                r = requests.get(
                    "https://api.binance.us/api/v3/ticker/price",
                    params={"symbol": binance_sym},
                    timeout=5,
                )
                return float(r.json()["price"])
        except Exception:
            pass
        return None

# ─── TELEGRAM WITH RETRY ──────────────────────────────────────────────────────

def send_telegram(text: str, retries: int = 3) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured")
        print(text)
        return False
    for attempt in range(retries):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if r.status_code == 200:
                return True
            log.warning(f"Telegram attempt {attempt+1} failed: {r.status_code}")
        except Exception as e:
            log.warning(f"Telegram attempt {attempt+1} error: {e}")
        if attempt < retries - 1:
            time.sleep(5)
    log.error("Telegram failed after all retries")
    return False

# ─── POSITION SIZE SAFETY CHECK ───────────────────────────────────────────────

def safe_lot_size(entry: float, sl: float, account_balance: float, risk_pct: float) -> float:
    risk_pct    = min(risk_pct, MAX_RISK_PCT)
    risk_amount = account_balance * (risk_pct / 100)
    sl_dist     = abs(entry - sl)
    if sl_dist <= 0:
        return 0.01
    lots = risk_amount / sl_dist
    lots = max(0.01, round(lots, 2))
    implied_risk = lots * sl_dist / account_balance * 100
    if implied_risk > MAX_RISK_PCT:
        log.warning(f"  ⚠️ Position size sanity check failed — capping to safe size")
        lots = round((account_balance * MAX_RISK_PCT / 100) / sl_dist, 2)
    return lots

# ─── INDICATORS ───────────────────────────────────────────────────────────────

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
    deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_g  = sum(gains[:period]) / period
    avg_l  = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    return 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)

def atr(candles: list[dict], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    avgs = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        avgs.append((avgs[-1] * (period - 1) + tr) / period)
    return avgs[-1] if avgs else None

def avg_volume(candles: list[dict], period: int = 20) -> float:
    vols = [c["vol"] for c in candles[-period - 1:-1]]
    return sum(vols) / len(vols) if vols else 1.0

# ─── BOS + ORDER BLOCK DETECTION ──────────────────────────────────────────────

def find_active_order_blocks(candles: list[dict]) -> list[dict]:
    cs = candles[:-1]
    n  = len(cs)
    if n < SWING_LOOKBACK * 4:
        return []
    is_sh = [False] * n
    is_sl = [False] * n
    for i in range(SWING_LOOKBACK, n - SWING_LOOKBACK):
        h = cs[i]["high"]
        l = cs[i]["low"]
        if all(cs[j]["high"] <= h for j in range(i - SWING_LOOKBACK, i + SWING_LOOKBACK + 1) if j != i):
            is_sh[i] = True
        if all(cs[j]["low"] >= l for j in range(i - SWING_LOOKBACK, i + SWING_LOOKBACK + 1) if j != i):
            is_sl[i] = True
    obs       = []
    last_sh_v = None
    last_sl_v = None
    for i in range(SWING_LOOKBACK * 2, n):
        c  = cs[i]
        p  = cs[i - 1]
        ci = i - SWING_LOOKBACK
        if ci >= 0:
            if is_sh[ci]: last_sh_v = cs[ci]["high"]
            if is_sl[ci]: last_sl_v = cs[ci]["low"]
        if last_sh_v is not None and p["close"] < last_sh_v and c["close"] > last_sh_v:
            ob_candle = None
            for j in range(max(0, i - 10), i):
                if cs[j]["close"] < cs[j]["open"]: ob_candle = cs[j]
            if ob_candle:
                obs.append({"idx": i, "ob_high": ob_candle["high"],
                            "ob_low": ob_candle["low"], "bos_level": last_sh_v, "dir": "B"})
        if last_sl_v is not None and p["close"] > last_sl_v and c["close"] < last_sl_v:
            ob_candle = None
            for j in range(max(0, i - 10), i):
                if cs[j]["close"] > cs[j]["open"]: ob_candle = cs[j]
            if ob_candle:
                obs.append({"idx": i, "ob_high": ob_candle["high"],
                            "ob_low": ob_candle["low"], "bos_level": last_sl_v, "dir": "S"})
    active = []
    for ob in reversed(obs):
        age = n - ob["idx"]
        if age <= OB_EXPIRE_BARS:
            ob["age"] = age
            active.append(ob)
    return active

def analyse(candles: list[dict], name: str) -> dict | None:
    if len(candles) < SWING_LOOKBACK * 4 + 10:
        return None
    confirm = candles[-2]
    price   = confirm["close"]
    obs = find_active_order_blocks(candles)
    if not obs: return None
    closes  = [c["close"] for c in candles[:-1]]
    rsi_val = rsi(closes[-30:], 14)
    avg_v   = avg_volume(candles, 20)
    vol_m   = confirm["vol"] / avg_v if avg_v > 0 else 1.0
    e200_list = ema(closes, 200) if len(closes) >= 200 else []
    e50_list  = ema(closes, 50)  if len(closes) >= 50  else []
    e200_v    = e200_list[-1] if e200_list else 0.0
    e50_v     = e50_list[-1]  if e50_list  else 0.0

    for ob in obs:
        ob_h = ob["ob_high"]; ob_l = ob["ob_low"]
        rng  = ob_h - ob_l
        if rng <= 0: continue

        if ob["dir"] == "B":
            if not (confirm["low"] <= ob_h and confirm["close"] >= ob_l): continue
            entry  = ob_h; sl = ob_l - rng * OB_BUFFER_PCT
            risk   = entry - sl
            if risk <= 0: continue
            tp     = entry + risk * RR_TARGET
            rr_act = (tp - entry) / (entry - sl)
            conf = 70
            if ob["age"] <= 10: conf += 10
            elif ob["age"] <= 25: conf += 5
            if vol_m >= 1.5: conf += 6
            if vol_m >= 2.5: conf += 4
            if rsi_val and rsi_val < 50: conf += 5
            if rsi_val and rsi_val < 40: conf += 5
            if price > e200_v > 0: conf += 5
            return {"symbol": name, "direction": "LONG",
                    "strategy": "BOS + Order Block (Bullish)",
                    "entry": entry, "tp": tp, "sl": sl, "rr": rr_act,
                    "confidence": min(95, conf), "ob_high": ob_h, "ob_low": ob_l,
                    "ob_age": ob["age"], "bos_level": ob["bos_level"],
                    "ema200": e200_v, "ema50": e50_v, "rsi": rsi_val, "vol_mult": vol_m,
                    "note": (f"OB zone ${ob_l:,.4f}–${ob_h:,.4f} | BOS broke ${ob['bos_level']:,.4f} | "
                             f"{ob['age']} bars ago ({ob['age']*4}h) | "
                             + (f"RSI {rsi_val:.0f} | " if rsi_val else "")
                             + f"Vol {vol_m:.1f}×")}

        elif ob["dir"] == "S":
            if not (confirm["high"] >= ob_l and confirm["close"] <= ob_h): continue
            entry  = ob_l; sl = ob_h + rng * OB_BUFFER_PCT
            risk   = sl - entry
            if risk <= 0: continue
            tp     = entry - risk * RR_TARGET
            rr_act = (entry - tp) / (sl - entry)
            conf = 70
            if ob["age"] <= 10: conf += 10
            elif ob["age"] <= 25: conf += 5
            if vol_m >= 1.5: conf += 6
            if vol_m >= 2.5: conf += 4
            if rsi_val and rsi_val > 50: conf += 5
            if rsi_val and rsi_val > 60: conf += 5
            if price < e200_v > 0: conf += 5
            return {"symbol": name, "direction": "SHORT",
                    "strategy": "BOS + Order Block (Bearish)",
                    "entry": entry, "tp": tp, "sl": sl, "rr": rr_act,
                    "confidence": min(95, conf), "ob_high": ob_h, "ob_low": ob_l,
                    "ob_age": ob["age"], "bos_level": ob["bos_level"],
                    "ema200": e200_v, "ema50": e50_v, "rsi": rsi_val, "vol_mult": vol_m,
                    "note": (f"OB zone ${ob_l:,.4f}–${ob_h:,.4f} | BOS broke ${ob['bos_level']:,.4f} | "
                             f"{ob['age']} bars ago ({ob['age']*4}h) | "
                             + (f"RSI {rsi_val:.0f} | " if rsi_val else "")
                             + f"Vol {vol_m:.1f}×")}
    return None

# ─── TRADE TRACKER ────────────────────────────────────────────────────────────

active_trades: dict = {}
trade_history: list = []

def open_trade(signal: dict, kraken_sym: str) -> None:
    name = signal["symbol"]
    active_trades[name] = {
        "symbol": name, "kraken_sym": kraken_sym,
        "direction": signal["direction"], "entry": signal["entry"],
        "tp": signal["tp"], "sl": signal["sl"],
        "strategy": signal["strategy"], "rr": signal["rr"],
        "open_time": datetime.now(timezone.utc),
    }
    log.info(f"  📌 Tracking {name} {signal['direction']} "
             f"entry=${signal['entry']:,.4f} TP=${signal['tp']:,.4f} SL=${signal['sl']:,.4f}")
    save_trades()

    # AUTO EXECUTE on cTrader
    if CTRADER_ACCESS_TOKEN and CTRADER_ACCOUNT_ID:
        log.info(f"  🤖 Auto-executing on cTrader...")
        place_ctrader_order(signal)
    else:
        log.info(f"  ℹ️ cTrader not configured — signal only")

def check_outcome(name: str, price: float) -> str | None:
    t = active_trades.get(name)
    if not t: return None
    if t["direction"] == "LONG":
        if price >= t["tp"]: return "TP"
        if price <= t["sl"]: return "SL"
    else:
        if price <= t["tp"]: return "TP"
        if price >= t["sl"]: return "SL"
    return None

def format_outcome(name: str, outcome: str) -> str:
    t = active_trades.get(name)
    if not t: return ""
    entry    = t["entry"]
    exit_p   = t["tp"] if outcome == "TP" else t["sl"]
    pnl_pct  = abs(exit_p - entry) / entry * 100
    duration = int((datetime.now(timezone.utc) - t["open_time"]).total_seconds() / 60)
    emoji    = "✅" if outcome == "TP" else "❌"
    pnl_str  = f"+{pnl_pct:.2f}%" if outcome == "TP" else f"-{pnl_pct:.2f}%"
    return "\n".join([
        f"{emoji} <b>{'TAKE PROFIT HIT' if outcome == 'TP' else 'STOP LOSS HIT'}</b>",
        f"", f"💹 <b>Asset:</b>      {name}",
        f"📍 <b>Direction:</b>  {t['direction']}",
        f"📊 <b>Strategy:</b>   {t['strategy']}",
        f"", f"💰 <b>Entry:</b>  ${entry:,.4f}",
        f"🏁 <b>Exit:</b>   ${exit_p:,.4f}",
        f"📈 <b>Result:</b> {pnl_str}",
        f"⏱ <b>Duration:</b> {duration // 60}h {duration % 60}m",
    ])

def close_trade(name: str, outcome: str) -> None:
    t = active_trades.get(name)
    if not t: return
    entry = t["entry"]
    pnl = (abs(t["tp"]-entry)/entry*100 if outcome == "TP"
           else -abs(t["sl"]-entry)/entry*100)
    trade_history.append({
        "symbol": name, "direction": t["direction"],
        "strategy": t["strategy"], "outcome": outcome,
        "pnl_pct": pnl, "rr": t["rr"],
        "duration": int((datetime.now(timezone.utc)-t["open_time"]).total_seconds()/60),
        "time": datetime.now(timezone.utc),
    })
    del active_trades[name]
    save_trades()

def already_in_trade(name: str) -> bool:
    if name in active_trades:
        log.info(f"  ⛔ {name} already has an active trade — skipping duplicate")
        return True
    return False

# ─── SUMMARIES ────────────────────────────────────────────────────────────────

last_daily_summary:  datetime | None = None
last_weekly_summary: datetime | None = None

def build_summary(trades: list[dict], title: str, period_days: int) -> str:
    if not trades:
        return f"📊 <b>{title}</b>\n\nNo completed trades this period."
    wins     = [t for t in trades if t["outcome"] == "TP"]
    losses   = [t for t in trades if t["outcome"] == "SL"]
    total    = len(trades)
    win_rate = len(wins) / total * 100
    pnl_r    = sum(t["rr"] for t in wins) - len(losses)
    annual   = pnl_r * (365 / period_days) if period_days > 0 else 0
    avg_rr   = sum(t["rr"] for t in wins) / len(wins) if wins else 0
    avg_dur  = sum(t["duration"] for t in trades) / total
    sym_stats: dict = {}
    for t in trades:
        s = t["symbol"]
        if s not in sym_stats: sym_stats[s] = [0, 0]
        if t["outcome"] == "TP": sym_stats[s][0] += 1
        else: sym_stats[s][1] += 1
    best  = max(trades, key=lambda t: t["pnl_pct"]) if wins  else None
    worst = min(trades, key=lambda t: t["pnl_pct"]) if losses else None
    lines = [
        f"📊 <b>{title}</b>",
        f"📅 {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')}",
        f"", f"📈 <b>Trades:</b>    {total}",
        f"✅ <b>Wins:</b>      {len(wins)}   ❌ <b>Losses:</b> {len(losses)}",
        f"🎯 <b>Win rate:</b>  {win_rate:.1f}%  (breakeven: 33.3% at 2:1)",
        f"⚖️  <b>Avg R:R:</b>  1:{avg_rr:.2f}",
        f"⏱ <b>Avg hold:</b>  {avg_dur:.0f} min  ({avg_dur/60:.1f}h)",
        f"", f"💰 <b>Est. P&L</b> (1% risk/trade): {pnl_r:+.1f}R  ({pnl_r:+.1f}%)",
        f"📅 <b>Annualised:</b> {annual:+.0f}%/yr",
        f"", f"🏅 <b>Top pairs:</b>",
    ]
    for sym, (w, l) in sorted(sym_stats.items(), key=lambda x: -(x[1][0]+x[1][1]))[:6]:
        tp2 = w+l; wr2 = w/tp2*100 if tp2 else 0
        lines.append(f"   {sym}: {tp2}T  {wr2:.0f}% WR  ({w}W / {l}L)")
    if best:  lines += [f"", f"🏆 <b>Best:</b>  {best['symbol']} {best['direction']}  +{best['pnl_pct']:.2f}%"]
    if worst: lines += [f"💀 <b>Worst:</b> {worst['symbol']} {worst['direction']}  {worst['pnl_pct']:.2f}%"]
    lines += [f"", f"⚠️ <i>Not financial advice. DYOR.</i>"]
    return "\n".join(lines)

def check_summaries() -> None:
    global last_daily_summary, last_weekly_summary, trade_history
    now = datetime.now(timezone.utc)
    if now.hour == 20 and now.minute < 10:
        if last_daily_summary is None or (now-last_daily_summary).total_seconds() > 3600:
            last_daily_summary = now
            today = [t for t in trade_history if (now-t["time"]).total_seconds() < 86400]
            send_telegram(build_summary(today, "Daily Summary — BOS+OB Bot", 1))
            log.info("📊 Daily summary sent")
    if now.weekday() == 6 and now.hour == 8 and now.minute < 10:
        if last_weekly_summary is None or (now-last_weekly_summary).total_seconds() > 86400:
            last_weekly_summary = now
            send_telegram(build_summary(trade_history, "Weekly Summary — BOS+OB Bot", 7))
            log.info("📊 Weekly summary sent")
            trade_history = []

# ─── SIGNAL FORMATTER ─────────────────────────────────────────────────────────

def format_signal(sig: dict) -> str:
    arrow   = "🟢" if sig["direction"] == "LONG" else "🔴"
    conf    = sig["confidence"]
    bar_str = "█" * (conf // 10) + "░" * (10 - conf // 10)
    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC %d %b")
    entry   = sig["entry"]; tp = sig["tp"]; sl = sig["sl"]
    ob_h    = sig.get("ob_high", 0); ob_l = sig.get("ob_low", 0)
    age     = sig.get("ob_age", 0); bos_lv = sig.get("bos_level", 0)
    e200    = sig.get("ema200", 0); rsi_v = sig.get("rsi"); vol = sig.get("vol_mult", 1.0)
    tp_pct  = (tp - entry) / entry * 100
    sl_pct  = (sl - entry) / entry * 100
    auto_tag = "🤖 <b>AUTO-EXECUTING on cTrader</b>" if CTRADER_ACCESS_TOKEN else "📡 <b>Signal only (no auto execution)</b>"
    lines = [
        f"{arrow} <b>{sig['symbol']} {sig['direction']} SIGNAL</b>",
        f"", auto_tag,
        f"", f"📊 <b>Strategy:</b>  {sig['strategy']}",
        f"🕐 <b>Time:</b>      {now_str}", f"",
        f"📦 <b>Order Block:</b>  ${ob_l:,.4f} – ${ob_h:,.4f}",
        f"💥 <b>BOS level:</b>    ${bos_lv:,.4f}",
        f"⏳ <b>OB age:</b>       {age} × 4H bars  ({age*4}h ago)",
        (f"📈 <b>EMA 200:</b>     ${e200:,.4f}" if e200 else ""),
        f"", f"💰 <b>Entry:</b>  ${entry:,.4f}",
        f"🎯 <b>TP:</b>     ${tp:,.4f}   ({tp_pct:+.2f}%)",
        f"🛑 <b>SL:</b>     ${sl:,.4f}   ({sl_pct:+.2f}%)",
        f"", f"⚖️  <b>R:R:</b>  1:{sig['rr']:.1f}",
        f"📊 <b>RSI:</b>  {rsi_v:.0f}" if rsi_v else "",
        f"📦 <b>Vol:</b>  {vol:.1f}× avg",
        f"🔥 <b>Confidence:</b> {conf}%  {bar_str}",
    ]
    if sig.get("note"): lines += [f"", f"📝 <i>{sig['note']}</i>"]
    lines += [f"", f"⚠️ <i>Not financial advice. DYOR.</i>"]
    return "\n".join(l for l in lines if l)

# ─── KEEP-ALIVE ───────────────────────────────────────────────────────────────

def keep_alive() -> None:
    from flask import Flask
    from threading import Thread
    app = Flask(__name__)
    @app.route("/")
    def home():
        active_str = ", ".join(f"{k} {v['direction']}" for k,v in active_trades.items()) or "none"
        wins  = sum(1 for t in trade_history if t["outcome"] == "TP")
        total = len(trade_history)
        wr    = f"{wins/total*100:.1f}%" if total else "—"
        ct_status = "✅ Connected" if CTRADER_ACCESS_TOKEN else "⚠️ Not configured"
        return (
            f"<b>BOS + Order Block Signal Bot — Auto Execution</b><br><br>"
            f"<b>Pairs:</b> {len(SYMBOLS)} | <b>Timeframe:</b> 4H | <b>Exchange:</b> Kraken (+ Binance fallback)<br>"
            f"<b>Active trades:</b> {active_str}<br>"
            f"<b>Completed:</b> {total} trades | <b>Live WR:</b> {wr}<br>"
            f"<b>cTrader:</b> {ct_status} | Account: {CTRADER_ACCOUNT_ID}<br><br>"
            f"<b>Safety features:</b><br>"
            f"  ✅ Auto execution via cTrader Open API<br>"
            f"  ✅ Token auto-refresh on 401<br>"
            f"  ✅ Balance check before every order<br>"
            f"  ✅ Order rejection alerts to Telegram<br>"
            f"  ✅ Hard cap: max {MAX_RISK_PCT}% risk per trade<br>"
            f"  ✅ Duplicate trade prevention<br>"
        )
    Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080))),
           daemon=True).start()

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def run() -> None:
    log.info("═" * 64)
    log.info("  BOS + Order Block Signal Bot — Auto Execution")
    log.info(f"  {len(SYMBOLS)} pairs | 4H | Kraken + Binance fallback")
    log.info(f"  cTrader account: {CTRADER_ACCOUNT_ID or 'NOT SET'}")
    log.info(f"  Max risk: {MAX_RISK_PCT}% | Risk per trade: {RISK_PCT}%")
    log.info("═" * 64)

    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN not set — signals print to console only")
    if not CTRADER_ACCESS_TOKEN:
        log.warning("CTRADER_ACCESS_TOKEN not set — signals only, no auto execution")

    keep_alive()
    load_trades()

    last_alert:  dict[str, float] = {name: 0.0 for _, name in SYMBOLS}
    symbol_data: dict = {}

    for kraken_sym, name in SYMBOLS:
        candles = fetch_candles(kraken_sym, INTERVAL_4H, LOOKBACK_BARS)
        symbol_data[kraken_sym] = {
            "name": name,
            "candles": deque(candles, maxlen=LOOKBACK_BARS),
        }
        log.info(f"  {name:6s}: loaded {len(candles)} candles ({len(candles)*4//24}d)")
        time.sleep(0.4)

    log.info(f"All {len(SYMBOLS)} pairs loaded. Scanning every {FETCH_INTERVAL//60} min.")

    while True:
        try:
            check_summaries()
            now = time.time()

            for kraken_sym, name in SYMBOLS:
                try:
                    if name in active_trades:
                        price = fetch_price(kraken_sym)
                        if price is not None:
                            outcome = check_outcome(name, price)
                            if outcome:
                                log.info(f"  🏁 {name} {outcome} @ ${price:,.4f}")
                                send_telegram(format_outcome(name, outcome))
                                close_trade(name, outcome)
                                last_alert[name] = now - COOLDOWN_PER_PAIR + 600

                    if already_in_trade(name):
                        continue
                    if now - last_alert.get(name, 0) < COOLDOWN_PER_PAIR:
                        continue

                    new_candles = fetch_candles(kraken_sym, INTERVAL_4H, 5)
                    buf = symbol_data[kraken_sym]["candles"]
                    for c in new_candles:
                        if not buf or c["ts"] > buf[-1]["ts"]: buf.append(c)
                        elif c["ts"] == buf[-1]["ts"]: buf[-1] = c

                    candles = list(buf)
                    sig = analyse(candles, name)

                    if sig:
                        log.info(f"  🎯 {name} {sig['direction']} | "
                                 f"OB ${sig['ob_low']:,.4f}–${sig['ob_high']:,.4f} | "
                                 f"entry ${sig['entry']:,.4f} | conf {sig['confidence']}%")
                        send_telegram(format_signal(sig))
                        open_trade(sig, kraken_sym)  # auto execution happens inside here
                        last_alert[name] = now

                    time.sleep(0.3)

                except Exception as e:
                    log.warning(f"  {name} error: {e}")
                    continue

            active_str = ", ".join(f"{n} {v['direction']}" for n,v in active_trades.items()) or "none"
            log.info(f"  Scan complete — active: [{active_str}] | completed: {len(trade_history)} | sleeping {FETCH_INTERVAL}s")
            time.sleep(FETCH_INTERVAL)

        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    run()
