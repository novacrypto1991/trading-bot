#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
بوت تحليل وإشارات للذهب والكريبتو - سكالب + سوينج + ملخص السوق على تيليجرام
================================================================================
- ⚡ سكالب: فريم 15 دقيقة، بشرط اتجاه فريم الساعة معه. صفقات سريعة (ساعات).
- 🌊 سوينج: فريم 4 ساعات، بشرط اتجاه الفريم اليومي معه. صفقات أطول (أيام).
- ☀️ ملخص السوق: مرتين باليوم (9 الصبح و 4 العصر بتوقيت عمّان) وعند الطلب.
- الأسعار: Binance، وإذا ما زبط Kraken (بدون حسابات أو مفاتيح).
- الذهب: PAXG (مربوطة بأونصة ذهب، سعرها قريب جداً من XAU/USD).
- الاستراتيجية: تقاطع EMA 20/50 مع اتجاه EMA 200 + فلتر الفريم الأكبر
  + تأكيدات (RSI, MACD, فوليوم, مسافة عن الدعم/المقاومة). وقف حسب ATR وهدفين.
- اختياري: تحليل بالذكاء الاصطناعي (Claude) للإشارات والملخص إذا حطيت ANTHROPIC_API_KEY.
- ما بيحتاج أي مكتبات إضافية.

الأوامر:  --once | --summary | --test | --backtest | --chatid | (بدون = تشغيل دائم)

تنبيه: إشارات آلية مبنية على قواعد فنية، مش نصيحة مالية، وما في استراتيجية مضمونة.
"""

import argparse
import bisect
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================ الإعدادات ============================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")      # اختياري
AI_MODEL = os.getenv("AI_MODEL", "claude-haiku-5-5")

SYMBOLS = {   # رمز Binance -> الاسم + رمز Kraken (احتياط)
    "PAXGUSDT": {"name": "الذهب (PAXG)", "kraken": "PAXGUSD"},
    "BTCUSDT": {"name": "بيتكوين", "kraken": "XBTUSD"},
    "ETHUSDT": {"name": "إيثيريوم", "kraken": "ETHUSD"},
    "SOLUSDT": {"name": "سولانا", "kraken": "SOLUSD"},
}

# أنواع الصفقات. شيل أي نوع ما بدك ياه (أو خلي enabled: False)
STRATEGIES = {
    "scalp": {"enabled": True, "label": "⚡ سكالب", "tf": "15m", "htf": "1h",
              "sl_atr": 1.2, "min_score": 4, "duration": "دقايق لساعات"},
    "swing": {"enabled": True, "label": "🌊 سوينج", "tf": "4h", "htf": "1d",
              "sl_atr": 2.0, "min_score": 4, "duration": "أيام"},
    # سحب سيولة قمة/قاع اليوم السابق والأسبوع السابق (على شموع 15 دقيقة)
    "sweep": {"enabled": True, "label": "💧 سحب سيولة", "tf": "15m", "htf": "1d",
              "detector": "sweep", "sl_atr": 0.2, "min_score": 3, "duration": "ساعات ليوم",
              "levels": ["PDH", "PDL", "PWH", "PWL"]},   # شيل القيعان إذا بدك القمم بس
}

HTF_EMA = 50
EMA_FAST, EMA_SLOW, EMA_TREND = 20, 50, 200
RSI_LEN, ATR_LEN = 14, 14
SR_LOOKBACK = 50
SUMMARY_HOURS_UTC = [6, 13]   # 9 الصبح و 4 العصر بتوقيت عمّان
CHECK_EVERY_MIN = 5           # للتشغيل الدائم على جهازك فقط
BACKTEST_CANDLES = 3000

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")
BINANCE_URLS = ["https://data-api.binance.vision", "https://api.binance.com"]
INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
KRAKEN_MIN = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}
# ===================================================================


def name(sym):
    return SYMBOLS[sym]["name"]


def active():
    return {k: v for k, v in STRATEGIES.items() if v.get("enabled", True)}


# ---------------------------- الشبكة ----------------------------
def http_json(url, data=None, headers=None, timeout=25):
    h = {"User-Agent": "trading-bot/3.0"}
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h),
                                timeout=timeout) as r:
        return json.loads(r.read().decode())


def _binance(symbol, interval, limit, end_time):
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    if end_time:
        params["endTime"] = end_time
    last = None
    for base in BINANCE_URLS:
        try:
            raw = http_json(f"{base}/api/v3/klines?{urllib.parse.urlencode(params)}")
            return [{"open_time": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                     "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
                     "close_time": int(k[6])} for k in raw]
        except Exception as e:
            last = e
    raise RuntimeError(last)


def _kraken(symbol, interval):
    d = http_json("https://api.kraken.com/0/public/OHLC?pair="
                  f"{SYMBOLS[symbol]['kraken']}&interval={KRAKEN_MIN[interval]}")
    if d.get("error"):
        raise RuntimeError(d["error"])
    key = next(k for k in d["result"] if k != "last")
    step = INTERVAL_MS[interval]
    return [{"open_time": int(k[0]) * 1000, "open": float(k[1]), "high": float(k[2]),
             "low": float(k[3]), "close": float(k[4]), "volume": float(k[6]),
             "close_time": int(k[0]) * 1000 + step - 1} for k in d["result"][key]]


_cache = {}


def fetch_candles(symbol, interval, limit=500, end_time=None):
    key = (symbol, interval, limit, end_time)
    if key in _cache:
        return _cache[key]
    try:
        out = _binance(symbol, interval, limit, end_time)
    except Exception as e1:
        if end_time:
            return []
        try:
            out = _kraken(symbol, interval)[-limit:]
        except Exception as e2:
            raise RuntimeError(f"فشل جلب {symbol} {interval}: Binance={e1} | Kraken={e2}")
    _cache[key] = out
    return out


def fetch_history(symbol, interval, total):
    out, end = [], None
    while len(out) < total:
        batch = fetch_candles(symbol, interval, min(1000, total - len(out)), end)
        if not batch:
            break
        out = batch + out
        end = batch[0]["open_time"] - 1
        if len(batch) < 1000:
            break
    return out


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[تيليجرام غير مضبوط] الرسالة:\n" + text + "\n")
        return False
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text[:4090],
                                   "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        return http_json(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                         data=data).get("ok", False)
    except Exception as e:
        print(f"خطأ بإرسال تيليجرام: {e}")
        return False


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------- المؤشرات ----------------------------
def ema(values, n):
    out = [None] * len(values)
    idx = [i for i, v in enumerate(values) if v is not None]
    if len(idx) < n:
        return out
    start = idx[n - 1]
    out[start] = sum(values[i] for i in idx[:n]) / n
    k = 2 / (n + 1)
    for i in range(start + 1, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    g = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    l = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            ag = (ag * (n - 1) + g[i - 1]) / n
            al = (al * (n - 1) + l[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(c, n=14):
    out = [None] * len(c)
    trs = [c[0]["high"] - c[0]["low"]] + [
        max(c[i]["high"] - c[i]["low"], abs(c[i]["high"] - c[i - 1]["close"]),
            abs(c[i]["low"] - c[i - 1]["close"])) for i in range(1, len(c))]
    if len(trs) < n:
        return out
    a = sum(trs[:n]) / n
    out[n - 1] = a
    for i in range(n, len(trs)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def compute_indicators(candles):
    closes = [c["close"] for c in candles]
    e12, e26 = ema(closes, 12), ema(closes, 26)
    macd = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)]
    sig = ema(macd, 9)
    vols = [c["volume"] for c in candles]
    return {
        "fast": ema(closes, EMA_FAST), "slow": ema(closes, EMA_SLOW),
        "trend": ema(closes, EMA_TREND), "rsi": rsi(closes, RSI_LEN),
        "atr": atr(candles, ATR_LEN),
        "hist": [m - s if m is not None and s is not None else None for m, s in zip(macd, sig)],
        "vol_avg": [None if i < 20 else sum(vols[i - 20:i]) / 20 for i in range(len(vols))],
    }


class HTF:
    """اتجاه الفريم الأكبر: +1 صاعد، -1 هابط، 0 غير معروف"""
    def __init__(self, candles):
        self.closes = [c["close"] for c in candles]
        self.ends = [c["close_time"] for c in candles]
        self.e = ema(self.closes, HTF_EMA)

    def bias(self, t_ms):
        j = bisect.bisect_right(self.ends, t_ms) - 1
        if j < 0 or self.e[j] is None:
            return 0
        return 1 if self.closes[j] > self.e[j] else -1


def closed_only(candles, now_ms):
    return [c for c in candles if c["close_time"] < now_ms]


# ---------------------------- الاستراتيجية ----------------------------
def check_signal(c, ind, i, htf_bias, cfg):
    f, s, t = ind["fast"], ind["slow"], ind["trend"]
    if i < SR_LOOKBACK or any(v is None for v in (
            f[i], s[i], t[i], f[i - 1], s[i - 1], ind["rsi"][i], ind["atr"][i], ind["hist"][i])):
        return None
    price, r, a, h = c[i]["close"], ind["rsi"][i], ind["atr"][i], ind["hist"][i]
    vol_ok = ind["vol_avg"][i] is not None and c[i]["volume"] > ind["vol_avg"][i]
    risk = a * cfg["sl_atr"]
    window = c[i - SR_LOOKBACK:i]

    if f[i - 1] <= s[i - 1] and f[i] > s[i] and price > t[i] and htf_bias == 1:
        side, d = "BUY", 1
        level = max(x["high"] for x in window)
        room = level <= price or level >= price + risk
        score = 2 + (50 <= r <= 70) + (h > 0) + vol_ok + room
    elif f[i - 1] >= s[i - 1] and f[i] < s[i] and price < t[i] and htf_bias == -1:
        side, d = "SELL", -1
        level = min(x["low"] for x in window)
        room = level >= price or level <= price - risk
        score = 2 + (30 <= r <= 50) + (h < 0) + vol_ok + room
    else:
        return None
    if score < cfg["min_score"]:
        return None
    return {"side": side, "entry": price, "sl": price - d * risk, "tp1": price + d * risk,
            "tp2": price + d * 2 * risk, "score": score, "rsi": r, "level": level,
            "time": c[i]["open_time"]}


# ---------------------------- سحب السيولة ----------------------------
DAY, WEEK = 86_400_000, 604_800_000
LEVEL_TXT = {"PDH": "قمة اليوم السابق", "PDL": "قاع اليوم السابق",
             "PWH": "قمة الأسبوع السابق", "PWL": "قاع الأسبوع السابق"}


def week_idx(t_ms):            # أسابيع تبدأ الإثنين 00:00 UTC
    return (t_ms - 4 * DAY) // WEEK


class Levels:
    """قمم وقيعان اليوم السابق والأسبوع السابق من الشموع اليومية"""
    def __init__(self, daily):
        self.d = daily
        self.ends = [c["close_time"] for c in daily]
        self.weeks = {}
        for c in daily:
            w = week_idx(c["open_time"])
            hi, lo = self.weeks.get(w, (c["high"], c["low"]))
            self.weeks[w] = (max(hi, c["high"]), min(lo, c["low"]))

    def at(self, t_ms):
        """يرجع {اسم المستوى: (السعر, بداية الفترة الحالية)}"""
        out = {}
        j = bisect.bisect_right(self.ends, t_ms) - 1
        if j >= 0 and t_ms - self.d[j]["close_time"] <= DAY:
            start = self.d[j]["close_time"] + 1
            out["PDH"], out["PDL"] = (self.d[j]["high"], start), (self.d[j]["low"], start)
        w = week_idx(t_ms)
        if w - 1 in self.weeks:
            start = w * WEEK + 4 * DAY
            hi, lo = self.weeks[w - 1]
            out["PWH"], out["PWL"] = (hi, start), (lo, start)
        return out


def check_sweep(c, i, lv, ind, cfg):
    """شمعة طلعت فوق قمة (أو تحت قاع) لأول مرة بالفترة ورجعت سكّرت جوّا"""
    a, r = ind["atr"][i], ind["rsi"][i]
    if a is None or r is None:
        return None
    k = c[i]
    hits = {"H": [], "L": []}
    for lvl, (price, start) in lv.at(k["open_time"]).items():
        if lvl not in cfg["levels"]:
            continue
        hi = lvl.endswith("H")
        if hi and not (k["high"] > price and k["close"] < price):
            continue
        if not hi and not (k["low"] < price and k["close"] > price):
            continue
        taken = False                       # هل انضرب قبل بنفس الفترة؟
        for x in range(i - 1, -1, -1):
            if c[x]["open_time"] < start:
                break
            if (hi and c[x]["high"] > price) or (not hi and c[x]["low"] < price):
                taken = True
                break
        if not taken:
            hits["H" if hi else "L"].append((lvl, price))
    side_key = "H" if hits["H"] else "L" if hits["L"] else None
    if not side_key or (hits["H"] and hits["L"]):
        return None
    lvls = hits[side_key]
    sell = side_key == "H"
    rng = k["high"] - k["low"] or 1e-9
    wick = (k["high"] - max(k["open"], k["close"])) if sell else (min(k["open"], k["close"]) - k["low"])
    weekly = any(l.startswith("PW") for l, _ in lvls)
    score = (4 if weekly else 3) + (wick / rng >= 0.5) + ((r >= 60) if sell else (r <= 40))
    if score < cfg["min_score"]:
        return None
    entry = k["close"]
    sl = k["high"] + a * cfg["sl_atr"] if sell else k["low"] - a * cfg["sl_atr"]
    risk = max(abs(sl - entry), a * 0.5)      # ما نخلي الوقف ضيق زيادة
    d = -1 if sell else 1
    sl = entry - d * risk
    return {"side": "SELL" if sell else "BUY", "entry": entry, "sl": sl,
            "tp1": entry + d * risk, "tp2": entry + d * 2 * risk, "score": score, "rsi": r,
            "level": lvls[0][1], "levels": lvls, "wick": k["high"] if sell else k["low"],
            "time": k["open_time"]}


def update_trade(tr, high, low):
    """نص الصفقة يتسكّر عالهدف 1 (+0.5R) والوقف ينتقل للدخول،
    والباقي يا عالهدف 2 (+1R) يا عالدخول (0). الوقف قبل الهدف = -1R"""
    ev = []
    if tr.get("closed"):
        return ev
    buy = tr["side"] == "BUY"
    hit_sl = low <= tr["sl"] if buy else high >= tr["sl"]
    hit_tp1 = high >= tr["tp1"] if buy else low <= tr["tp1"]
    hit_tp2 = high >= tr["tp2"] if buy else low <= tr["tp2"]
    if not tr.get("tp1_hit"):
        if hit_sl:
            tr.update(closed=True, result=-1.0)
            ev.append("SL")
        elif hit_tp1:
            tr.update(tp1_hit=True, sl=tr["entry"])
            ev.append("TP1")
            if hit_tp2:
                tr.update(closed=True, result=1.5)
                ev.append("TP2")
    elif hit_sl:
        tr.update(closed=True, result=0.5)
        ev.append("BE")
    elif hit_tp2:
        tr.update(closed=True, result=1.5)
        ev.append("TP2")
    return ev


# ---------------------------- الذكاء الاصطناعي (اختياري) ----------------------------
def ask_ai(prompt, max_tokens=500):
    if not ANTHROPIC_API_KEY:
        return None
    body = {"model": AI_MODEL, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    for tools in ([{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}], None):
        try:
            b = dict(body, tools=tools) if tools else body
            d = http_json("https://api.anthropic.com/v1/messages",
                          data=json.dumps(b).encode(), headers=headers, timeout=120)
            texts = [x["text"] for x in d.get("content", []) if x.get("type") == "text"]
            if texts:
                return "".join(texts).strip()
        except Exception as e:
            print(f"AI: {e}")
    return None


def ai_signal_comment(sym, sig, cfg):
    side = "شراء" if sig["side"] == "BUY" else "بيع"
    txt = ask_ai(
        f"البوت الفني طلّع صفقة {cfg['label']} {side} على {name(sym)} ({sym}) فريم {cfg['tf']}.\n"
        f"الدخول {sig['entry']:.4f}، الوقف {sig['sl']:.4f}، الهدف1 {sig['tp1']:.4f}، "
        f"الهدف2 {sig['tp2']:.4f}، RSI {sig['rsi']:.0f}.\n"
        "ابحث عن آخر الأخبار والأحداث القادمة المؤثرة (وعالدولار والفائدة إذا ذهب)، وجاوب "
        "بالعربي بثلاث جمل كحد أقصى: هل الأخبار بتدعم الصفقة، وأهم خطر. "
        "ابدأ بكلمة وحدة: مؤيد / محايد / حذر. بدون مقدمات.")
    return txt[:700] if txt else None


def ai_market_brief(rows):
    data = "\n".join(f"- {r['name']}: {r['price']:.2f}, 24h {r['chg']:+.2f}%, "
                     f"اتجاه 1h/4h/يومي: {r['t1h']}/{r['t4h']}/{r['t1d']}" for r in rows)
    txt = ask_ai(
        "هاي بيانات السوق الحالية:\n" + data + "\n\n"
        "ابحث عن أهم أخبار اليوم للذهب والكريبتو وأهم الأحداث الاقتصادية بالـ 24 ساعة الجاية "
        "(بيانات أمريكية، الفيدرالي، إلخ). اكتب بالعربي ملخص قصير جداً: "
        "سطرين عن الذهب، سطرين عن الكريبتو، وسطر 'انتبه' لأهم حدث قادم مع وقته. بدون مقدمات.",
        max_tokens=700)
    return txt[:1500] if txt else None


# ---------------------------- الرسائل ----------------------------
def fmt(p):
    return f"{p:,.2f}" if p >= 1 else f"{p:.6f}"


TREND_TXT = {1: "صاعد 🟢", -1: "هابط 🔴", 0: "؟"}


def signal_message(sym, sig, cfg, ai=None):
    buy = sig["side"] == "BUY"
    head = "🟢 <b>شراء</b>" if buy else "🔴 <b>بيع</b>"
    stars = "⭐" * sig["score"] + "☆" * (6 - sig["score"])
    t = datetime.fromtimestamp(sig["time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = (
        f"<b>{cfg['label']}</b> | {head} — {name(sym)}\n"
        f"<code>{sym} · {cfg['tf']} · المدة المتوقعة: {cfg['duration']}</code>\n\n"
        f"💰 الدخول: <b>{fmt(sig['entry'])}</b>\n"
        f"🛑 وقف الخسارة: {fmt(sig['sl'])}\n"
        f"🎯 الهدف 1: {fmt(sig['tp1'])}\n"
        f"🎯 الهدف 2: {fmt(sig['tp2'])}\n\n"
        f"💪 القوة: {stars} ({sig['score']}/6)\n"
        f"🧭 اتجاه {cfg['htf']}: {'صاعد ✅' if buy else 'هابط ✅'}\n"
        f"{'🧱 أقرب مقاومة' if buy else '🧱 أقرب دعم'}: {fmt(sig['level'])}\n"
        f"📊 RSI: {sig['rsi']:.0f}\n🕐 {t}\n"
    )
    if ai:
        msg += f"\n🤖 <b>رأي الذكاء الاصطناعي:</b>\n{esc(ai)}\n"
    return msg + "\n<i>إشارة آلية وليست نصيحة مالية. خاطر بـ 1% بس.</i>"


def sweep_message(sym, sig, cfg, ai=None, note=""):
    sell = sig["side"] == "SELL"
    head = "🔴 <b>فكرة بيع</b>" if sell else "🟢 <b>فكرة شراء</b>"
    stars = "⭐" * sig["score"] + "☆" * (6 - sig["score"])
    lv = "\n".join(f"{'🏔' if l.endswith('H') else '🕳'} سحب <b>{LEVEL_TXT[l]}</b> ({l}): {fmt(p)}"
                   for l, p in sig["levels"])
    t = datetime.fromtimestamp(sig["time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    move = (f"↩️ طلع لـ {fmt(sig['wick'])} ورجع سكّر تحتها عند {fmt(sig['entry'])}" if sell else
            f"↩️ نزل لـ {fmt(sig['wick'])} ورجع سكّر فوقها عند {fmt(sig['entry'])}")
    msg = (
        f"<b>{cfg['label']}</b> | {head} — {name(sym)}\n<code>{sym} · {cfg['tf']}</code>\n\n"
        f"{lv}\n{move}\n\n"
        f"💰 الدخول: <b>{fmt(sig['entry'])}</b>\n"
        f"🛑 الوقف ({'فوق' if sell else 'تحت'} الذيل): {fmt(sig['sl'])}\n"
        f"🎯 الهدف 1: {fmt(sig['tp1'])}\n🎯 الهدف 2: {fmt(sig['tp2'])}\n\n"
        f"💪 القوة: {stars} ({sig['score']}/6)\n📊 RSI: {sig['rsi']:.0f}\n🕐 {t}\n"
    )
    if note:
        msg += f"\n{note}\n"
    if ai:
        msg += f"\n🤖 <b>رأي الذكاء الاصطناعي:</b>\n{esc(ai)}\n"
    return msg + ("\n<i>سحب السيولة يعني السعر ضرب أوامر الوقف عند المستوى ورجع. "
                  "ممكن يكون بداية انعكاس، والأفضل تستنى تأكيد. مش نصيحة مالية.</i>")


def event_message(tr, e):
    cfg = STRATEGIES.get(tr["style"], {"label": ""})
    side = "شراء" if tr["side"] == "BUY" else "بيع"
    n = f"{cfg['label']} {name(tr['symbol'])} ({side})"
    return {
        "TP1": f"✅ {n}: ضرب <b>الهدف 1</b> عند {fmt(tr['tp1'])}\n"
               f"يفضّل تسكير جزء ونقل الوقف لنقطة الدخول {fmt(tr['entry'])}",
        "TP2": f"🏆 {n}: ضرب <b>الهدف 2</b> عند {fmt(tr['tp2'])} — الصفقة انتهت",
        "SL": f"❌ {n}: ضرب <b>وقف الخسارة</b> عند {fmt(tr['sl'])} — الصفقة انتهت",
        "BE": f"➖ {n}: رجع لنقطة الدخول بعد الهدف 1 — انتهت بربح جزئي",
    }[e]


# ---------------------------- ملخص السوق ----------------------------
def market_rows(now_ms):
    rows = []
    for sym in SYMBOLS:
        try:
            h1 = closed_only(fetch_candles(sym, "1h", 500), now_ms)
            h4 = closed_only(fetch_candles(sym, "4h", 300), now_ms)
            d1 = closed_only(fetch_candles(sym, "1d", 300), now_ms)
            live = fetch_candles(sym, "15m", 2)[-1]["close"]
        except Exception as e:
            print(f"[{sym}] {e}")
            continue
        r4 = rsi([c["close"] for c in h4])[-1] or 50
        win = h4[-SR_LOOKBACK:]
        price = live
        res = [x["high"] for x in win if x["high"] > price]
        sup = [x["low"] for x in win if x["low"] < price]
        rows.append({
            "sym": sym, "name": name(sym), "price": price,
            "chg": (price / h1[-24]["close"] - 1) * 100,
            "t1h": TREND_TXT[HTF(h1).bias(h1[-1]["close_time"])],
            "t4h": TREND_TXT[HTF(h4).bias(h4[-1]["close_time"])],
            "t1d": TREND_TXT[HTF(d1).bias(d1[-1]["close_time"])],
            "rsi": r4, "res": min(res) if res else None, "sup": max(sup) if sup else None,
            "liq": {k: v[0] for k, v in Levels(d1).at(now_ms).items()},
        })
    return rows


def summary_message(rows, open_trades, ai=None):
    hr = datetime.now(timezone.utc).hour
    title = "☀️ <b>ملخص السوق الصباحي</b>" if hr < 10 else "📊 <b>ملخص السوق</b>"
    lines = [title + "\n"]
    for r in rows:
        arrow = "🟢" if r["chg"] >= 0 else "🔴"
        rs = "تشبّع شراء ⚠️" if r["rsi"] > 70 else "تشبّع بيع ⚠️" if r["rsi"] < 30 else f"{r['rsi']:.0f}"
        lines.append(
            f"{arrow} <b>{r['name']}</b>: {fmt(r['price'])} ({r['chg']:+.2f}%)\n"
            f"   الاتجاه — ساعة: {r['t1h']} · 4س: {r['t4h']} · يومي: {r['t1d']}\n"
            f"   مقاومة: {fmt(r['res']) if r['res'] else '—'} · دعم: {fmt(r['sup']) if r['sup'] else '—'}"
            f" · RSI(4س): {rs}")
        q = r["liq"]
        if q:
            lines.append("   💧 سيولة — "
                         + (f"يومي: {fmt(q['PDH'])} / {fmt(q['PDL'])}" if "PDH" in q else "")
                         + (f" · أسبوعي: {fmt(q['PWH'])} / {fmt(q['PWL'])}" if "PWH" in q else ""))
    if ai:
        lines.append(f"\n🤖 <b>الأخبار والتوقعات:</b>\n{esc(ai)}")
    lines.append(f"\n📂 صفقات مفتوحة: {len(open_trades)}")
    for t in open_trades:
        cfg = STRATEGIES.get(t["style"], {"label": ""})
        lines.append(f"  • {cfg['label']} {name(t['symbol'])} "
                     f"{'شراء' if t['side'] == 'BUY' else 'بيع'} من {fmt(t['entry'])}"
                     f"{' (الهدف 1 ✅)' if t.get('tp1_hit') else ''}")
    return "\n".join(lines)


def send_summary(state, now_ms):
    rows = market_rows(now_ms)
    if not rows:
        return False
    return send_telegram(summary_message(rows, state["open"], ai_market_brief(rows)))


# ---------------------------- الحالة ----------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("last_signal", {})
    st.setdefault("open", [])
    st.setdefault("last_summary", "")
    for t in st["open"]:          # توافق مع النسخ القديمة
        t.setdefault("style", "swing")
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ---------------------------- التشغيل ----------------------------
def run_once(state):
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    for style, cfg in active().items():
        is_sweep = cfg.get("detector") == "sweep"
        for sym in SYMBOLS:
            try:
                candles = fetch_candles(sym, cfg["tf"], 1000 if is_sweep else 500)
                htf_c = closed_only(fetch_candles(sym, cfg["htf"], 300), now_ms)
                htf = HTF(htf_c)
            except Exception as e:
                print(f"[{style} {sym}] {e}")
                continue
            closed = closed_only(candles, now_ms)
            live = candles[-1] if candles and candles[-1]["close_time"] >= now_ms else None

            # 1) متابعة الصفقات المفتوحة
            for tr in [t for t in state["open"] if t["symbol"] == sym and t["style"] == style]:
                for c in closed:
                    if c["open_time"] > tr["last_checked"]:
                        for e in update_trade(tr, c["high"], c["low"]):
                            send_telegram(event_message(tr, e))
                        tr["last_checked"] = c["open_time"]
                if live:
                    for e in update_trade(tr, live["high"], live["low"]):
                        send_telegram(event_message(tr, e))
            state["open"] = [t for t in state["open"] if not t.get("closed")]

            # 2أ) سحب السيولة: يفحص الشموع اللي سكّرت من آخر فحص (آخر 4 كحد أقصى)
            if is_sweep:
                if len(closed) < 30:
                    continue
                closes = [x["close"] for x in closed]
                ind = {"atr": atr(closed, ATR_LEN), "rsi": rsi(closes, RSI_LEN)}
                lv = Levels(htf_c)
                seen = state.setdefault("sweep_seen", {}).get(sym, 0)
                sig = None
                for k in range(max(1, len(closed) - 4), len(closed)):
                    if closed[k]["open_time"] > seen:
                        sig = check_sweep(closed, k, lv, ind, cfg) or sig
                state["sweep_seen"][sym] = closed[-1]["open_time"]
                if sig:
                    busy = any(t["symbol"] == sym and t["style"] == style for t in state["open"])
                    note = "ℹ️ في صفقة سحب مفتوحة على نفس الأصل، هاد تنبيه بس." if busy else ""
                    send_telegram(sweep_message(sym, sig, cfg,
                                                ai_signal_comment(sym, sig, cfg), note))
                    print(f"[sweep {sym}] {sig['side']} {[l for l, _ in sig['levels']]}")
                    if not busy:
                        state["open"].append(dict(sig, symbol=sym, style=style,
                                                  last_checked=sig["time"]))
                continue

            # 2ب) إشارة جديدة على آخر شمعة مسكّرة
            if len(closed) < EMA_TREND + 2:
                continue
            ind = compute_indicators(closed)
            i = len(closed) - 1
            sig = check_signal(closed, ind, i, htf.bias(closed[i]["close_time"]), cfg)
            key = f"{sym}|{style}"
            busy = any(t["symbol"] == sym and t["style"] == style for t in state["open"])
            if sig and state["last_signal"].get(key) != sig["time"] and not busy:
                send_telegram(signal_message(sym, sig, cfg, ai_signal_comment(sym, sig, cfg)))
                print(f"[{style} {sym}] {sig['side']} @ {fmt(sig['entry'])}")
                state["last_signal"][key] = sig["time"]
                state["open"].append(dict(sig, symbol=sym, style=style, last_checked=sig["time"]))

    # 3) ملخص السوق
    slot = f"{now:%Y-%m-%d}-{now.hour}"
    if now.hour in SUMMARY_HOURS_UTC and state["last_summary"] != slot:
        if send_summary(state, now_ms):
            state["last_summary"] = slot
    save_state(state)


def run_forever():
    print(f"البوت شغّال — فحص كل {CHECK_EVERY_MIN} دقايق")
    send_telegram("🤖 البوت اشتغل\n" + " · ".join(c["label"] for c in active().values()))
    state = load_state()
    while True:
        _cache.clear()
        try:
            run_once(state)
        except Exception as e:
            print(f"خطأ: {e}")
        time.sleep(CHECK_EVERY_MIN * 60)


def backtest(data=None):
    """data اختياري للتجربة: {(sym, tf): candles}"""
    out = ["🧪 <b>اختبار الاستراتيجيات</b> (بدون عمولات)"]
    for style, cfg in active().items():
        out.append(f"\n<b>{cfg['label']}</b> (فريم {cfg['tf']})")
        grand = []
        for sym in SYMBOLS:
            try:
                if data:
                    c, hc = data[(sym, cfg["tf"])], data[(sym, cfg["htf"])]
                else:
                    c = fetch_history(sym, cfg["tf"], BACKTEST_CANDLES)
                    ratio = INTERVAL_MS[cfg["tf"]] / INTERVAL_MS[cfg["htf"]]
                    hc = fetch_history(sym, cfg["htf"], int(BACKTEST_CANDLES * ratio) + 300)
            except Exception as e:
                out.append(f"{name(sym)}: ما قدرت أجيب البيانات ({e})")
                continue
            is_sweep = cfg.get("detector") == "sweep"
            if is_sweep:
                ind = {"atr": atr(c, ATR_LEN), "rsi": rsi([x["close"] for x in c], RSI_LEN)}
                lv = Levels(hc)
            else:
                ind, htf = compute_indicators(c), HTF(hc)
            res, tr = [], None
            for i in range(30 if is_sweep else EMA_TREND + 1, len(c)):
                if tr:
                    update_trade(tr, c[i]["high"], c[i]["low"])
                    if tr.get("closed"):
                        res.append(tr["result"])
                        tr = None
                    continue
                sig = (check_sweep(c, i, lv, ind, cfg) if is_sweep else
                       check_signal(c, ind, i, htf.bias(c[i]["close_time"]), cfg))
                if sig:
                    tr = dict(sig)
            grand += res
            n, w = len(res), sum(1 for r in res if r > 0)
            days = (c[-1]["open_time"] - c[0]["open_time"]) / 86_400_000 if c else 0
            out.append(f"  {name(sym)} ({days:.0f} يوم): {n} صفقة · نجاح "
                       f"{(w / n * 100 if n else 0):.0f}% · {sum(res):+.1f}R")
        if grand:
            n = len(grand)
            out.append(f"  <b>المجموع:</b> {n} صفقة · نجاح "
                       f"{sum(1 for r in grand if r > 0) / n * 100:.0f}% · {sum(grand):+.1f}R "
                       f"(متوسط {sum(grand) / n:+.2f}R)")
    out.append("\nR = المبلغ اللي بتخاطر فيه. لو بتخاطر بـ 10$ و +5R يعني ربح 50$.\n"
               "السكالب بتأثر عليه العمولات أكثر من السوينج.")
    text = "\n".join(out)
    print(text)
    send_telegram(text)


def show_chat_ids():
    if not TELEGRAM_BOT_TOKEN:
        sys.exit("حط TELEGRAM_BOT_TOKEN أول.")
    data = http_json(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates")
    chats = {}
    for u in data.get("result", []):
        ch = (u.get("message") or u.get("channel_post") or {}).get("chat", {})
        if ch:
            chats[ch["id"]] = ch.get("title") or ch.get("first_name") or ""
    print("\n".join(f"TELEGRAM_CHAT_ID = {k}  ({v})" for k, v in chats.items())
          or "ما لقيت رسائل. ابعت رسالة للبوت وجرّب مرة ثانية.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="بوت تحليل وإشارات الذهب والكريبتو")
    for flag in ("once", "summary", "backtest", "test", "chatid"):
        ap.add_argument(f"--{flag}", action="store_true")
    a = ap.parse_args()
    if a.chatid:
        show_chat_ids()
    elif a.test:
        ok = send_telegram("✅ تجربة: البوت موصول على تيليجرام بنجاح\n"
                           + " · ".join(c["label"] for c in active().values())
                           + ("\n🤖 الذكاء الاصطناعي: مفعّل" if ANTHROPIC_API_KEY else ""))
        print("انبعتت ✅" if ok else "ما انبعتت ❌ — تأكد من التوكن ورقم الشات")
        if not ok:
            sys.exit(1)
    elif a.summary:
        send_summary(load_state(), int(time.time() * 1000))
    elif a.backtest:
        backtest()
    elif a.once:
        run_once(load_state())
    else:
        run_forever()
