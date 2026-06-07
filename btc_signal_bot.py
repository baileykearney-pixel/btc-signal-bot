"""
Signal Bot v8 — EMA200 Touch Strategy
Optimised from 4.3yr backtest: +178.8%/yr | 34.5% WR | 890 trades
Changes vs v7:
  - ADX filter removed (was killing 85% of valid trades)
  - Per-pair trade tracking (no more global lock)
  - Per-pair cooldown (4hr per pair, independent)
  - All 10 pairs run simultaneously
"""

import time
import logging
import os
from datetime import datetime, timezone
from collections import deque
import requests

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOLS = [
    ("XBTUSD",   "BTC"),
    ("ETHUSD",   "ETH"),
    ("SOLUSD",   "SOL"),
    ("XRPUSD",   "XRP"),
    ("ADAUSD",   "ADA"),
    ("AVAXUSD",  "AVAX"),
    ("XLMUSD",   "XLM"),
    ("UNIUSD",   "UNI"),
    ("XDGUSD",   "DOGE"),
    ("BNBUSD",   "BNB"),
]

INTERVAL_HTF      = 60
LOOKBACK_HTF      = 250
FETCH_INTERVAL    = 300
MIN_VOL_MULT      = 1.5
MIN_RR            = 2.5
COOLDOWN_PER_PAIR = 14400
EMA200_ZONE_PCT   = 0.02

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("SignalBot")

def fetch_kraken(symbol, interval, limit):
    try:
        r = requests.get("https://api.kraken.com/0/public/OHLC",
                         params={"pair": symbol, "interval": interval},
                         headers={"User-Agent": "signal-bot/8.0"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("error"): return []
        result = data.get("result", {})
        pair_key = [k for k in result if k != "last"][0]
        return [{"ts": int(c[0])*1000, "open": float(c[1]), "high": float(c[2]),
                 "low": float(c[3]), "close": float(c[4]), "vol": float(c[6])}
                for c in result[pair_key][-limit:]]
    except Exception as e:
        log.debug(f"Fetch error ({symbol}): {e}")
        return []

def fetch_price(symbol):
    try:
        r = requests.get("https://api.kraken.com/0/public/Ticker",
                         params={"pair": symbol},
                         headers={"User-Agent": "signal-bot/8.0"}, timeout=5)
        result = r.json().get("result", {})
        return float(result[list(result.keys())[0]]["c"][0])
    except Exception:
        return None

def ema(values, period):
    if len(values) < period: return []
    k = 2/(period+1); r = [sum(values[:period])/period]
    for v in values[period:]: r.append(v*k + r[-1]*(1-k))
    return r

def rsi(closes, period=14):
    if len(closes) < period+1: return None
    d = [closes[i+1]-closes[i] for i in range(len(closes)-1)]
    g = [max(x,0) for x in d]; l = [max(-x,0) for x in d]
    ag = sum(g[:period])/period; al = sum(l[:period])/period
    for i in range(period, len(d)):
        ag = (ag*(period-1)+g[i])/period; al = (al*(period-1)+l[i])/period
    return 100 if al==0 else 100-100/(1+ag/al)

def atr(candles, period=14):
    if len(candles) < period+1: return None
    trs = [max(candles[i]["high"]-candles[i]["low"],
               abs(candles[i]["high"]-candles[i-1]["close"]),
               abs(candles[i]["low"]-candles[i-1]["close"]))
           for i in range(1, len(candles))]
    a = [sum(trs[:period])/period]
    for t in trs[period:]: a.append((a[-1]*(period-1)+t)/period)
    return a[-1]

def avg_volume(candles, period=20):
    v = [c["vol"] for c in candles[-period-1:-1]]
    return sum(v)/len(v) if v else 1.0

def get_bias(candles):
    if len(candles) < 205: return "NEUTRAL", 0, 0
    closes = [c["close"] for c in candles]
    e50 = ema(closes, 50); e200 = ema(closes, 200)
    if len(e50) < 5 or len(e200) < 5: return "NEUTRAL", 0, 0
    bull = sum(1 for i in range(-3,0) if e50[i]>e200[i] and closes[i]>e50[i])
    bear = sum(1 for i in range(-3,0) if e50[i]<e200[i] and closes[i]<e50[i])
    b = "BULL" if bull==3 else "BEAR" if bear==3 else "NEUTRAL"
    return b, e50[-1], e200[-1]

def get_key_levels(candles):
    h = [c["high"] for c in candles]; l = [c["low"] for c in candles]
    lvls = []; w = 5
    for i in range(w, len(h)-w):
        if h[i]==max(h[i-w:i+w+1]): lvls.append(h[i])
        if l[i]==min(l[i-w:i+w+1]): lvls.append(l[i])
    lvls.sort(); out = []
    for x in lvls:
        if not out or abs(x-out[-1])/out[-1]>0.002: out.append(x)
        else: out[-1] = (out[-1]+x)/2
    return out

def analyse_ema200_touch(candles, name):
    if len(candles) < 210: return None
    bias, e50_val, e200_val = get_bias(candles)
    if bias == "NEUTRAL": return None
    closes = [c["close"] for c in candles]
    price = closes[-1]
    confirm = candles[-2]
    avg_v = avg_volume(candles, 20)
    last_vol = confirm["vol"]
    atr_v = atr(candles, 14)
    rsi_val = rsi(closes[:-1], 14)
    if not atr_v: return None
    dist_pct = abs(price - e200_val) / e200_val
    if dist_pct > EMA200_ZONE_PCT: return None
    body = abs(confirm["close"]-confirm["open"])
    range_ = confirm["high"]-confirm["low"]
    if range_ == 0: return None
    body_pct = body/range_
    upper_w = confirm["high"] - max(confirm["open"], confirm["close"])
    lower_w = min(confirm["open"], confirm["close"]) - confirm["low"]
    min_sl_dist = price * 0.015

    if bias == "BULL" and price >= e200_val*0.99:
        if (confirm["close"]>confirm["open"] and body_pct>0.45 and
                last_vol>avg_v*MIN_VOL_MULT and (rsi_val is None or rsi_val<60)):
            conf = 78
            if last_vol > avg_v*2.5: conf += 5
            if rsi_val and rsi_val < 40: conf += 4
            if lower_w > body*0.5: conf += 3
            swing_low = min(c["low"] for c in candles[-20:-1])  # actual wicks
            sl = swing_low - atr_v*0.3
            if price-sl < min_sl_dist: sl = price-min_sl_dist
            sl_dist = price-sl
            tp = price + sl_dist*MIN_RR  # fixed 2.5R target
            rr = MIN_RR
            return {"symbol": name, "direction": "LONG", "strategy": "EMA200 Touch (Bull)",
                    "entry": price, "tp": tp, "sl": sl, "rr": rr,
                    "confidence": min(95, conf), "htf_bias": bias, "ema200": e200_val,
                    "dist_pct": dist_pct*100, "rsi": rsi_val, "vol_mult": last_vol/avg_v,
                    "note": f"EMA200 touch at ${e200_val:,.4f} | RSI {rsi_val:.0f} | Vol {last_vol/avg_v:.1f}x" if rsi_val else f"EMA200 touch at ${e200_val:,.4f}"}

    if bias == "BEAR" and price <= e200_val*1.01:
        if (confirm["close"]<confirm["open"] and body_pct>0.45 and
                last_vol>avg_v*MIN_VOL_MULT and (rsi_val is None or rsi_val>40)):
            conf = 78
            if last_vol > avg_v*2.5: conf += 5
            if rsi_val and rsi_val > 60: conf += 4
            if upper_w > body*0.5: conf += 3
            swing_high = max(c["high"] for c in candles[-20:-1])
            sl = swing_high + atr_v*0.3
            if sl-price < min_sl_dist: sl = price+min_sl_dist
            sl_dist = sl-price
            tp = price - sl_dist*MIN_RR  # fixed 2.5R target
            rr = MIN_RR
            return {"symbol": name, "direction": "SHORT", "strategy": "EMA200 Touch (Bear)",
                    "entry": price, "tp": tp, "sl": sl, "rr": rr,
                    "confidence": min(95, conf), "htf_bias": bias, "ema200": e200_val,
                    "dist_pct": dist_pct*100, "rsi": rsi_val, "vol_mult": last_vol/avg_v,
                    "note": f"EMA200 touch at ${e200_val:,.4f} | RSI {rsi_val:.0f} | Vol {last_vol/avg_v:.1f}x" if rsi_val else f"EMA200 touch at ${e200_val:,.4f}"}
    return None

# Per-pair trade tracker
active_trades = {}
trade_history = []

def set_active_trade(signal, kraken_sym):
    name = signal["symbol"]
    active_trades[name] = {
        "symbol": name, "kraken_sym": kraken_sym,
        "direction": signal["direction"], "entry": signal["entry"],
        "tp": signal["tp"], "sl": signal["sl"],
        "strategy": signal["strategy"], "rr": signal["rr"],
        "open_time": datetime.now(timezone.utc),
    }
    log.info(f"  📌 Tracking {name} {signal['direction']} entry=${signal['entry']:,.4f} TP=${signal['tp']:,.4f} SL=${signal['sl']:,.4f}")

def check_outcome(name, price):
    t = active_trades.get(name)
    if not t: return None
    if t["direction"] == "LONG":
        if price >= t["tp"]: return "TP"
        if price <= t["sl"]: return "SL"
    else:
        if price <= t["tp"]: return "TP"
        if price >= t["sl"]: return "SL"
    return None

def format_outcome(name, outcome, price):
    t = active_trades.get(name)
    if not t: return ""
    entry = t["entry"]; exit_p = t["tp"] if outcome=="TP" else t["sl"]
    pnl_pct = abs(exit_p-entry)/entry*100
    duration = int((datetime.now(timezone.utc)-t["open_time"]).total_seconds()/60)
    emoji = "✅" if outcome=="TP" else "❌"
    pnl_str = f"+{pnl_pct:.2f}%" if outcome=="TP" else f"-{pnl_pct:.2f}%"
    return "\n".join([f"{emoji} <b>{'TAKE PROFIT HIT' if outcome=='TP' else 'STOP LOSS HIT'}</b>",
                      f"", f"💹 <b>Asset:</b> {name}", f"📍 <b>Direction:</b> {t['direction']}",
                      f"", f"💰 <b>Entry:</b>  ${entry:,.4f}", f"🏁 <b>Exit:</b>   ${exit_p:,.4f}",
                      f"📈 <b>Result:</b> {pnl_str}", f"⏱ <b>Duration:</b> {duration} min"])

def record_and_clear(name, outcome):
    t = active_trades.get(name)
    if not t: return
    entry = t["entry"]
    pnl = abs(t["tp"]-entry)/entry*100 if outcome=="TP" else -abs(t["sl"]-entry)/entry*100
    trade_history.append({"symbol": name, "direction": t["direction"],
                           "strategy": t["strategy"], "outcome": outcome,
                           "pnl_pct": pnl, "rr": t["rr"],
                           "duration": int((datetime.now(timezone.utc)-t["open_time"]).total_seconds()/60),
                           "time": datetime.now(timezone.utc)})
    del active_trades[name]

last_daily_summary = None
last_weekly_summary = None

def build_summary(trades, title, days):
    if not trades:
        return f"📊 <b>{title}</b>\n\nNo completed trades this period."
    wins = [t for t in trades if t["outcome"]=="TP"]
    losses = [t for t in trades if t["outcome"]=="SL"]
    total = len(trades); wr = len(wins)/total*100
    pnl = sum(t["rr"] for t in wins) - len(losses)
    annual = pnl*(365/days)
    avg_rr = sum(t["rr"] for t in wins)/len(wins) if wins else 0
    avg_dur = sum(t["duration"] for t in trades)/total
    sym_stats = {}
    for t in trades:
        s = t["symbol"]
        if s not in sym_stats: sym_stats[s] = [0,0]
        if t["outcome"]=="TP": sym_stats[s][0]+=1
        else: sym_stats[s][1]+=1
    best = max(trades, key=lambda t: t["pnl_pct"]) if wins else None
    worst = min(trades, key=lambda t: t["pnl_pct"]) if losses else None
    lines = [f"📊 <b>{title}</b>", f"📅 {datetime.now(timezone.utc).strftime('%d %b %Y')}", f"",
             f"📈 <b>Total Trades:</b> {total}",
             f"✅ <b>Wins:</b> {len(wins)}  ❌ <b>Losses:</b> {len(losses)}",
             f"🎯 <b>Win Rate:</b> {wr:.1f}%", f"⚖️  <b>Avg R:R:</b> 1:{avg_rr:.1f}",
             f"⏱ <b>Avg Duration:</b> {avg_dur:.0f} min", f"",
             f"💰 <b>Est. P&L</b> (1% risk): {pnl:+.1f}%",
             f"📅 <b>Est. Annual:</b> {annual:+.0f}%", f"", f"🏅 <b>By Asset:</b>"]
    for sym,(w,l) in sorted(sym_stats.items(),key=lambda x:x[1][0],reverse=True)[:5]:
        t=w+l; lines.append(f"   {sym}: {t} trades  {w/t*100:.0f}% WR")
    if best: lines += [f"", f"🏆 <b>Best:</b> {best['symbol']} +{best['pnl_pct']:.2f}%"]
    if worst: lines += [f"💀 <b>Worst:</b> {worst['symbol']} {worst['pnl_pct']:.2f}%"]
    lines += [f"", f"⚠️ <i>Not financial advice. DYOR.</i>"]
    return "\n".join(lines)

def check_summaries():
    global last_daily_summary, last_weekly_summary, trade_history
    now = datetime.now(timezone.utc)
    if now.hour==20 and now.minute<3:
        if last_daily_summary is None or (now-last_daily_summary).total_seconds()>3600:
            last_daily_summary = now
            today = [t for t in trade_history if (now-t["time"]).total_seconds()<86400]
            send_telegram(build_summary(today, "📊 Daily Summary", 1))
            log.info("📊 Daily summary sent")
    if now.weekday()==6 and now.hour==8 and now.minute<3:
        if last_weekly_summary is None or (now-last_weekly_summary).total_seconds()>86400:
            last_weekly_summary = now
            send_telegram(build_summary(trade_history, "📊 Weekly Summary", 7))
            log.info("📊 Weekly summary sent")
            trade_history = []

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(text); return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                          timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.warning(f"Telegram error: {e}"); return False

def format_signal(sig):
    arrow = "🟢" if sig["direction"]=="LONG" else "🔴"
    rr = sig.get("rr",0); conf = sig["confidence"]
    bars = "█"*(conf//10) + "░"*(10-conf//10)
    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    entry=sig["entry"]; tp=sig["tp"]; sl=sig["sl"]
    bias_e = "📈" if sig.get("htf_bias")=="BULL" else "📉"
    lines = [f"{arrow} <b>{sig['symbol']} {sig['direction']} SIGNAL</b>", f"",
             f"📊 <b>Strategy:</b> {sig['strategy']}",
             f"{bias_e} <b>1H Trend:</b> {sig.get('htf_bias')}",
             f"🕐 <b>Time:</b> {now_str}", f"",
             f"💰 <b>Entry:</b>  ${entry:,.4f}",
             f"🎯 <b>TP:</b>     ${tp:,.4f}  ({(tp-entry)/entry*100:+.2f}%)",
             f"🛑 <b>SL:</b>     ${sl:,.4f}  ({(sl-entry)/entry*100:+.2f}%)", f"",
             f"⚖️  <b>R:R:</b> 1:{rr:.1f}", f"🔥 <b>Confidence:</b> {conf}%  {bars}"]
    if sig.get("note"): lines += [f"", f"📝 <i>{sig['note']}</i>"]
    lines += [f"", f"⚠️ <i>Not financial advice. DYOR.</i>"]
    return "\n".join(lines)

def keep_alive():
    from flask import Flask
    from threading import Thread
    app = Flask("")
    @app.route("/")
    def home():
        active = ", ".join(f"{k} {v['direction']}" for k,v in active_trades.items()) or "none"
        return f"Signal Bot v8 — {len(SYMBOLS)} pairs ✅<br>Active trades: {active}<br>History: {len(trade_history)} trades"
    Thread(target=lambda: app.run(host="0.0.0.0", port=8080), daemon=True).start()

def run():
    log.info("═"*62)
    log.info("  Signal Bot v8 — EMA200 Touch (optimised)")
    log.info(f"  {len(SYMBOLS)} pairs | 2% zone | no ADX | per-pair 4h cooldown")
    log.info(f"  Backtest: +178.8%/yr | 34.5% WR | 890 trades over 4.3yr")
    log.info("═"*62)
    keep_alive()
    last_alert = {name: 0 for _,name in SYMBOLS}
    symbol_data = {}
    for kraken_sym, name in SYMBOLS:
        candles = fetch_kraken(kraken_sym, INTERVAL_HTF, LOOKBACK_HTF)
        symbol_data[kraken_sym] = {"name": name, "candles": deque(candles, maxlen=LOOKBACK_HTF)}
        log.info(f"  {name}: loaded {len(candles)} candles")
        time.sleep(0.3)
    log.info(f"All {len(SYMBOLS)} pairs loaded. Monitoring...")

    while True:
        try:
            check_summaries()
            now = time.time()
            for kraken_sym, name in SYMBOLS:
                try:
                    if name in active_trades:
                        price = fetch_price(kraken_sym)
                        if price:
                            outcome = check_outcome(name, price)
                            if outcome:
                                log.info(f"  🏁 {name} {outcome} @ ${price:,.4f}")
                                send_telegram(format_outcome(name, outcome, price))
                                record_and_clear(name, outcome)
                                last_alert[name] = now - COOLDOWN_PER_PAIR + 300
                        continue

                    if now - last_alert.get(name,0) < COOLDOWN_PER_PAIR:
                        continue

                    new = fetch_kraken(kraken_sym, INTERVAL_HTF, 5)
                    buf = symbol_data[kraken_sym]["candles"]
                    for c in new:
                        if not buf or c["ts"]>buf[-1]["ts"]: buf.append(c)
                        elif c["ts"]==buf[-1]["ts"]: buf[-1]=c

                    candles = list(buf)
                    bias,_,e200 = get_bias(candles)
                    price = candles[-1]["close"] if candles else 0
                    log.info(f"  {name:6s} ${price:>12,.4f}  bias={bias:7s}  EMA200=${e200:,.4f}")

                    signal = analyse_ema200_touch(candles, name)
                    if signal:
                        log.info(f"  ✨ {name} {signal['direction']} | {signal['confidence']}% | R:R 1:{signal['rr']:.1f}")
                        if send_telegram(format_signal(signal)):
                            log.info(f"  📨 Sent — {name}")
                            set_active_trade(signal, kraken_sym)
                            last_alert[name] = now
                    time.sleep(0.3)
                except Exception as e:
                    log.debug(f"{name} error: {e}")
        except KeyboardInterrupt:
            log.info("Stopped."); break
        except Exception as e:
            log.error(f"Loop error: {e}")
        time.sleep(30 if active_trades else FETCH_INTERVAL)

if __name__ == "__main__":
    run()
