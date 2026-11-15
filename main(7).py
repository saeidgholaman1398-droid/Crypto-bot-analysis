"""
ربات تحلیل بیت‌کوین (تایم ۵ و ۱۵ دقیقه، ۱ و ۴ ساعت) برای تلگرام
- داده: APIهای عمومی صرافی‌ها (بدون API Key)
- تحلیل: موتور امتیازدهی اندیکاتوری (EMA, RSI, MACD, Bollinger, ATR, حجم)
- هوش مصنوعی اختیاری: اگر GEMINI_API_KEY ست باشد، یک جمع‌بندی هم اضافه می‌شود
"""
import os
import re
import time
import html
import logging
import threading
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("btc-bot")

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
INTERVAL_MIN = float(os.getenv("INTERVAL_MIN", "5"))  # هر چند دقیقه ارسال شود (2 یا 5)
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()  # اختیاری
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
PORT = int(os.getenv("PORT", "10000"))
SELF_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()  # رندر خودش ست می‌کند
DATABASE_PATH = os.getenv("DATABASE_PATH", "subscribers.db").strip()

TIMEFRAMES = [5, 15, 60, 240]
TF_LABELS = {5: "۵ دقیقه", 15: "۱۵ دقیقه", 60: "۱ ساعت", 240: "۴ ساعت"}
OKX_BARS = {5: "5m", 15: "15m", 60: "1H", 240: "4H"}
BINANCE_INTERVALS = {5: "5m", 15: "15m", 60: "1h", 240: "4h"}
TZ = timezone(timedelta(hours=3, minutes=30))  # وقت ایران
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 (btc-bot)"})


# ---------------------------------------------------------------- داده‌ها
# هر کندل: (open, high, low, close, volume, timestamp) - فقط کندل‌های بسته‌شده
def from_kraken(tf):
    r = HTTP.get("https://api.kraken.com/0/public/OHLC",
                 params={"pair": "XBTUSD", "interval": tf}, timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(str(j["error"]))
    rows = next(v for k, v in j["result"].items() if k != "last")[:-1]
    return [(float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[6]), int(float(x[0]))) for x in rows]


def from_okx(tf):
    r = HTTP.get("https://www.okx.com/api/v5/market/candles",
                 params={"instId": "BTC-USDT", "bar": OKX_BARS[tf], "limit": 300}, timeout=15)
    r.raise_for_status()
    data = [d for d in r.json()["data"] if d[8] == "1"]
    data.reverse()
    return [(float(d[1]), float(d[2]), float(d[3]), float(d[4]), float(d[5]), int(d[0]) // 1000) for d in data]


def from_binance(tf):
    r = HTTP.get("https://api.binance.com/api/v3/klines",
                 params={"symbol": "BTCUSDT", "interval": BINANCE_INTERVALS[tf], "limit": 300}, timeout=15)
    r.raise_for_status()
    return [(float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), int(x[0]) // 1000) for x in r.json()[:-1]]


SOURCES = [("Kraken", from_kraken), ("OKX", from_okx), ("Binance", from_binance)]


def get_candles(tf):
    for name, fn in SOURCES:
        try:
            c = fn(tf)
            if len(c) >= 100:
                return c, name
        except Exception as e:
            log.warning("%s failed (%s): %s", name, tf, type(e).__name__)
    raise RuntimeError("هیچ منبع داده‌ای در دسترس نبود")


# ---------------------------------------------------------------- اندیکاتورها
def ema(vals, n):
    k = 2 / (n + 1)
    out, e = [], vals[0]
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    ag, al = g / n, l / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(c, n=14):
    trs = []
    for i in range(1, len(c)):
        h, lo, pc = c[i][1], c[i][2], c[i - 1][3]
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def bollinger(closes, n=20, k=2):
    w = closes[-n:]
    m = sum(w) / n
    sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
    return m - k * sd, m, m + k * sd


# ---------------------------------------------------------------- تحلیل
def analyze(c):
    closes = [x[3] for x in c]
    price = closes[-1]
    e9, e21, e50 = ema(closes, 9)[-1], ema(closes, 21)[-1], ema(closes, 50)[-1]
    macd_line = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    hist = [m - s for m, s in zip(macd_line, ema(macd_line, 9))]
    h_now, h_prev = hist[-1], hist[-2]
    r = rsi(closes)
    lo, _, up = bollinger(closes)
    a = atr(c)
    vols = [x[4] for x in c]
    vol_ratio = vols[-1] / ((sum(vols[-21:-1]) / 20) or 1)

    score, why = 0.0, []
    strong_up = price > e9 > e21 > e50
    strong_down = price < e9 < e21 < e50

    if strong_up:
        score += 2; why.append("روند صعودی قوی (EMA 9>21>50)")
    elif strong_down:
        score -= 2; why.append("روند نزولی قوی (EMA 9<21<50)")
    elif e9 > e21:
        score += 0.75; why.append("EMA9 بالای EMA21")
    else:
        score -= 0.75; why.append("EMA9 پایین EMA21")

    if r < 30:
        if strong_down:
            why.append(f"RSI اشباع فروش ({r:.0f}) - احتمال برگشت، احتیاط")
        else:
            score += 1.5; why.append(f"RSI اشباع فروش ({r:.0f})")
    elif r > 70:
        if strong_up:
            why.append(f"RSI اشباع خرید ({r:.0f}) - احتمال اصلاح، احتیاط")
        else:
            score -= 1.5; why.append(f"RSI اشباع خرید ({r:.0f})")
    elif r >= 50:
        score += 0.5; why.append(f"RSI مثبت ({r:.0f})")
    else:
        score -= 0.5; why.append(f"RSI منفی ({r:.0f})")

    if h_now > 0 and h_now > h_prev:
        score += 1; why.append("MACD مثبت و در حال تقویت")
    elif h_now > 0:
        score += 0.4; why.append("MACD مثبت ولی ضعیف‌شونده")
    elif h_now < 0 and h_now < h_prev:
        score -= 1; why.append("MACD منفی و در حال تقویت")
    else:
        score -= 0.4; why.append("MACD منفی ولی ضعیف‌شونده")

    if price < lo:
        if strong_down:
            score -= 0.5; why.append("شکست باند پایین بولینگر در روند نزولی")
        else:
            score += 1; why.append("قیمت زیر باند پایین بولینگر")
    elif price > up:
        if strong_up:
            score += 0.5; why.append("شکست باند بالای بولینگر در روند صعودی")
        else:
            score -= 1; why.append("قیمت بالای باند بالای بولینگر")

    if vol_ratio > 1.5:
        d = 1 if c[-1][3] > c[-1][0] else -1
        score += 0.5 * d
        why.append(f"حجم {vol_ratio:.1f}× میانگین ({'خریدار' if d > 0 else 'فروشنده'})")

    if score >= 2.5:
        side = "bull"
    elif score <= -2.5:
        side = "bear"
    else:
        side = "flat"

    plan = None
    if side == "bull":
        plan = (price, price - 1.5 * a, price + 1.5 * a, price + 3 * a)
    elif side == "bear":
        plan = (price, price + 1.5 * a, price - 1.5 * a, price - 3 * a)

    return {
        "price": price, "score": score, "side": side,
        "conf": min(95, int(abs(score) / 5 * 100)),
        "rsi": r, "atr": a, "macd": h_now,
        "support": min(x[2] for x in c[-30:]),
        "resistance": max(x[1] for x in c[-30:]),
        "plan": plan, "why": why,
    }


SIDE_TXT = {"bull": "🟢 صعودی (خرید)", "bear": "🔴 نزولی (فروش)", "flat": "⚪️ خنثی (صبر)"}


def fmt(x):
    return f"{x:,.1f}"


def tf_block(tf, a):
    s = f"⏱ <b>تایم {TF_LABELS.get(tf, f'{tf} دقیقه')}</b>\n"
    s += f"{SIDE_TXT[a['side']]} | اطمینان: {a['conf']}%\n"
    s += f"RSI: {a['rsi']:.0f} | ATR: {a['atr']:.0f}\n"
    s += f"حمایت: {fmt(a['support'])} | مقاومت: {fmt(a['resistance'])}\n"
    if a["plan"]:
        e, sl, t1, t2 = a["plan"]
        s += f"🎯 ورود: {fmt(e)}\n❌ حد ضرر: {fmt(sl)}\n✅ هدف۱: {fmt(t1)} | هدف۲: {fmt(t2)}\n"
    s += "• " + "\n• ".join(html.escape(w) for w in a["why"][:4])
    return s


def aggregate_signal(analyses):
    counts = {
        "bull": sum(a["side"] == "bull" for a in analyses.values()),
        "bear": sum(a["side"] == "bear" for a in analyses.values()),
    }
    side = max(counts, key=counts.get)
    return (side, counts[side]) if counts[side] >= 3 else ("flat", counts[side])


def verdict(analyses):
    side, votes = aggregate_signal(analyses)
    if side != "flat":
        return f"🔥 برآیند ۴ تایم‌فریم ({fa(votes)} از ۴): {SIDE_TXT[side]}"
    bull = sum(a["side"] == "bull" for a in analyses.values())
    bear = sum(a["side"] == "bear" for a in analyses.values())
    if bull and bear:
        return "⚠️ تضاد بین تایم‌فریم‌ها؛ بهتره وارد نشی"
    return "😴 حداقل ۳ تایم از ۴ تایم هم‌جهت نیستند؛ فعلاً سیگنال برآیندی نداریم"


def ai_comment(analyses):
    if not GEMINI_KEY:
        return ""
    timeframe_lines = "\n".join(
        f"تایم {TF_LABELS[tf]}: جهت={analyses[tf]['side']} امتیاز={analyses[tf]['score']:.1f} "
        f"RSI={analyses[tf]['rsi']:.0f} MACD={analyses[tf]['macd']:.2f}"
        for tf in TIMEFRAMES
    )
    a5, a240 = analyses[5], analyses[240]
    prompt = (
        "تو تحلیلگر تکنیکال بیت‌کوین هستی. بر اساس داده‌های زیر یک جمع‌بندی خیلی کوتاه "
        "(حداکثر ۳ جمله) به فارسی بنویس، بدون توصیه مالی قطعی.\n"
        f"{timeframe_lines}\n"
        f"قیمت={a5['price']:.0f} حمایت={a240['support']:.0f} مقاومت={a240['resistance']:.0f}"
    )
    try:
        r = HTTP.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        log.warning("Gemini failed: %s", type(e).__name__)
        return ""


def update_signal_outcomes(candles):
    """نتیجه سیگنال‌های برآیندی باز را با کندل‌های بسته‌شده ۵ دقیقه بررسی می‌کند."""
    with db_connect() as db:
        pending = db.execute(
            "SELECT id, side, stop_loss, target, entry_time, last_checked "
            "FROM signal_history WHERE status='open'"
        ).fetchall()

        for signal_id, side, stop_loss, target, entry_time, last_checked in pending:
            checked_to = max(int(entry_time), int(last_checked or entry_time))
            final_status = None
            resolved_at = None

            for candle in candles:
                candle_time = int(candle[5])
                if candle_time <= checked_to:
                    continue

                high, low = candle[1], candle[2]
                if side == "bull":
                    hit_stop, hit_target = low <= stop_loss, high >= target
                else:
                    hit_stop, hit_target = high >= stop_loss, low <= target

                # اگر هر دو سطح در یک کندل لمس شوند، نتیجه با فرض محافظه‌کارانه باخت است.
                if hit_stop and hit_target:
                    final_status = "lost"
                elif hit_stop:
                    final_status = "lost"
                elif hit_target:
                    final_status = "won"

                checked_to = candle_time
                if final_status:
                    resolved_at = datetime.fromtimestamp(candle_time, timezone.utc).isoformat()
                    break

                # سیگنال بدون برخورد به حد سود/ضرر پس از ۲۴ ساعت منقضی می‌شود.
                if candle_time - int(entry_time) >= 24 * 60 * 60:
                    final_status = "expired"
                    resolved_at = datetime.fromtimestamp(candle_time, timezone.utc).isoformat()
                    break

            if final_status:
                db.execute(
                    "UPDATE signal_history SET status=?, last_checked=?, resolved_at=? WHERE id=?",
                    (final_status, checked_to, resolved_at, signal_id),
                )
            elif checked_to > int(last_checked or entry_time):
                db.execute(
                    "UPDATE signal_history SET last_checked=? WHERE id=?",
                    (checked_to, signal_id),
                )


def register_aggregate_signal(analyses, candles_5m):
    """فقط در صورت هم‌جهتی حداقل ۳ تایم‌فریم از ۴ تایم‌فریم، سیگنال را ثبت می‌کند."""
    side, _ = aggregate_signal(analyses)
    if side == "flat" or not candles_5m:
        return

    entry_time = int(candles_5m[-1][5])
    entry = analyses[5]["price"]
    risk = analyses[5]["atr"] * 1.5
    if risk <= 0:
        return

    if side == "bull":
        stop_loss, target = entry - risk, entry + risk
    else:
        stop_loss, target = entry + risk, entry - risk

    with db_connect() as db:
        # فقط یک سیگنال باز و حداکثر یک سیگنال جدید در هر کندل ۵ دقیقه ثبت می‌شود.
        if db.execute("SELECT 1 FROM signal_history WHERE status='open' LIMIT 1").fetchone():
            return
        last_entry = db.execute("SELECT MAX(entry_time) FROM signal_history").fetchone()[0]
        if last_entry is not None and entry_time <= int(last_entry):
            return
        db.execute(
            "INSERT INTO signal_history(side, entry, stop_loss, target, entry_time, "
            "last_checked, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
            (side, entry, stop_loss, target, entry_time, entry_time,
             datetime.now(timezone.utc).isoformat()),
        )


def win_rate_text():
    with db_connect() as db:
        wins, losses, pending = db.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status='won' THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN status='open' THEN 1 ELSE 0 END), 0) "
            "FROM signal_history"
        ).fetchone()

    resolved = wins + losses
    if not resolved:
        return (f"📊 وین‌ریت سیگنال‌های برآیندی: هنوز نتیجه‌ای ثبت نشده "
                f"(سیگنال باز: {fa(pending)})\n"
                "<i>معیار نتیجه: هدف اول در برابر حد ضرر؛ سیگنال‌های منقضی‌شده در وین‌ریت حساب نمی‌شوند.</i>")
    rate = wins / resolved * 100
    return (f"📈 وین‌ریت سیگنال‌های برآیندی: <b>{fa(round(rate, 1))}٪</b> "
            f"({fa(wins)} برد از {fa(resolved)} نتیجه؛ سیگنال باز: {fa(pending)})\n"
            "<i>معیار نتیجه: هدف اول در برابر حد ضرر؛ سیگنال‌های منقضی‌شده در وین‌ریت حساب نمی‌شوند.</i>")


def build_message():
    res, candles_by_tf, src = {}, {}, ""
    for tf in TIMEFRAMES:
        candles, src = get_candles(tf)
        candles_by_tf[tf] = candles
        res[tf] = analyze(candles)
    a5 = res[5]
    update_signal_outcomes(candles_by_tf[5])
    register_aggregate_signal(res, candles_by_tf[5])
    now = datetime.now(TZ).strftime("%H:%M:%S")
    msg = (f"🟠 <b>تحلیل بیت‌کوین (BTC)</b>\n🕒 {now} | منبع: {src}\n"
           f"💰 قیمت: <b>${fmt(a5['price'])}</b>\n\n"
           + "\n\n".join(tf_block(tf, res[tf]) for tf in TIMEFRAMES)
           + f"\n\n<b>{verdict(res)}</b>\n\n{win_rate_text()}")
    ai = ai_comment(res)
    if ai:
        msg += f"\n\n🤖 {html.escape(ai)}"
    msg += "\n\n<i>تحلیل الگوریتمی و آموزشی است، توصیه مالی نیست.</i>"
    return msg


# ---------------------------------------------------------------- تلگرام
API = f"https://api.telegram.org/bot{TOKEN}"
state = {"auto": True, "interval": INTERVAL_MIN}
INTERVAL_CHOICES = [2, 5, 10]
_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

HELP_TEXT = (
    "ℹ️ <b>راهنما</b>\n\n"
    "📊 تحلیل کامل: بررسی هم‌زمان تایم ۵ و ۱۵ دقیقه و ۱ و ۴ ساعت\n"
    "⏱ تحلیل جداگانه: ۵ دقیقه، ۱۵ دقیقه، ۱ ساعت یا ۴ ساعت\n"
    "📈 وین‌ریت: عملکرد تاریخی سیگنال‌های برآیندی\n"
    "💰 قیمت لحظه‌ای بیت‌کوین\n"
    "⏸/▶️ توقف یا شروع ارسال خودکار\n"
    "🕒 تغییر فاصله ارسال خودکار\n"
    "🔄 زیر هر تحلیل می‌تونی همون پیام رو تازه‌سازی کنی\n\n"
    "دستورها: /menu  /analyze  /price  /help\n\n"
    "<i>تحلیل الگوریتمی و آموزشی است، توصیه مالی نیست.</i>"
)


def fa(x):
    return f"{x:g}".translate(_FA)


def now_str():
    return datetime.now(TZ).strftime("%H:%M:%S")


def plain(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def call(method, **payload):
    try:
        return HTTP.post(f"{API}/{method}", json=payload, timeout=20)
    except Exception as e:
        log.error("Telegram %s failed: %s", method, type(e).__name__)
        return None


def _deliver(method, payload):
    r = call(method, **payload)
    if r is not None and r.status_code == 400 and "parse entities" in r.text:
        payload = dict(payload)
        payload.pop("parse_mode", None)
        payload["text"] = plain(payload["text"])
        r = call(method, **payload)
    if r is not None and not r.ok and "not modified" not in r.text:
        log.error("Telegram error: %s", r.text[:200])
    return r


def send(text, markup=None, chat_id=None):
    p = {"chat_id": chat_id or CHAT_ID, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    if markup:
        p["reply_markup"] = markup
    return _deliver("sendMessage", p)


def edit(chat_id, message_id, text, markup=None):
    p = {"chat_id": chat_id, "message_id": message_id, "text": text,
         "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        p["reply_markup"] = markup
    _deliver("editMessageText", p)


def answer(cb_id, text=""):
    call("answerCallbackQuery", callback_query_id=cb_id, text=text)


# ---------------------------------------------------------------- عضویت و ارسال گروهی
def db_connect():
    return sqlite3.connect(DATABASE_PATH, timeout=20)


def init_subscribers_db():
    parent = Path(DATABASE_PATH).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    with db_connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS subscribers (
            chat_id TEXT PRIMARY KEY, username TEXT, first_name TEXT,
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            side TEXT NOT NULL,
            entry REAL NOT NULL,
            stop_loss REAL NOT NULL,
            target REAL NOT NULL,
            entry_time INTEGER NOT NULL,
            last_checked INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )""")
        db.commit()


def subscribe(chat_id, user=None):
    user = user or {}
    with db_connect() as db:
        db.execute(
            "INSERT INTO subscribers(chat_id, username, first_name, active, created_at) "
            "VALUES (?, ?, ?, 1, ?) ON CONFLICT(chat_id) DO UPDATE SET "
            "username=excluded.username, first_name=excluded.first_name, active=1",
            (str(chat_id), user.get("username"), user.get("first_name"),
             datetime.now(timezone.utc).isoformat())
        )
        db.commit()


def unsubscribe(chat_id):
    with db_connect() as db:
        db.execute("UPDATE subscribers SET active=0 WHERE chat_id=?", (str(chat_id),))
        db.commit()


def active_subscribers():
    with db_connect() as db:
        return [row[0] for row in db.execute(
            "SELECT chat_id FROM subscribers WHERE active=1").fetchall()]


def deactivate_subscriber(chat_id):
    unsubscribe(chat_id)


def broadcast(text, markup=None):
    recipients = active_subscribers()
    sent = 0
    for recipient in recipients:
        response = send(text, markup, chat_id=recipient)
        if response is not None and response.ok:
            sent += 1
        elif response is not None and response.status_code == 403:
            # کاربر ربات را مسدود کرده یا دسترسی چت لغو شده است
            deactivate_subscriber(recipient)
        time.sleep(0.05)  # جلوگیری از ارسال فشرده به API تلگرام
    log.info("broadcast completed: %s/%s", sent, len(recipients))
    return sent, len(recipients)


# ---------------------------------------------------------------- منوی شیشه‌ای
def menu_text():
    on = "✅ فعال" if state["auto"] else "⏸ متوقف"
    return ("📋 <b>منوی ربات تحلیل بیت‌کوین</b>\n\n"
            f"ارسال خودکار: {on}\n"
            f"فاصله ارسال: هر {fa(state['interval'])} دقیقه\n\n"
            "یکی از گزینه‌ها رو انتخاب کن 👇")


def menu_markup(chat_id=None):
    keyboard = [
        [{"text": "📊 تحلیل کامل (۵ و ۱۵ دقیقه + ۱ و ۴ ساعت)", "callback_data": "full"}],
        [{"text": "⏱ فقط ۵ دقیقه", "callback_data": "tf5"},
         {"text": "⏱ فقط ۱۵ دقیقه", "callback_data": "tf15"}],
        [{"text": "⏱ فقط ۱ ساعت", "callback_data": "tf60"},
         {"text": "⏱ فقط ۴ ساعت", "callback_data": "tf240"}],
        [{"text": "💰 قیمت لحظه‌ای", "callback_data": "price"}],
        [{"text": "ℹ️ راهنما", "callback_data": "help"}],
    ]
    # کنترل ارسال خودکار فقط برای مدیر اصلی نمایش داده می‌شود.
    if CHAT_ID and str(chat_id) == CHAT_ID:
        auto_btn = "⏸ توقف ارسال خودکار" if state["auto"] else "▶️ شروع ارسال خودکار"
        row = []
        for m in INTERVAL_CHOICES:
            mark = " ✅" if float(state["interval"]) == m else ""
            row.append({"text": f"🕒 {fa(m)} دقیقه{mark}", "callback_data": f"int{m}"})
        keyboard.insert(3, [{"text": auto_btn, "callback_data": "toggle"}])
        keyboard.insert(4, row)
    return {"inline_keyboard": keyboard}


def result_markup(kind):
    return {"inline_keyboard": [[
        {"text": "🔄 تازه‌سازی", "callback_data": f"re_{kind}"},
        {"text": "📋 منو", "callback_data": "menu"},
    ]]}


# ---------------------------------------------------------------- محتوا
def get_price():
    try:
        r = HTTP.get("https://api.kraken.com/0/public/Ticker", params={"pair": "XBTUSD"}, timeout=10)
        r.raise_for_status()
        return float(next(iter(r.json()["result"].values()))["c"][0]), "Kraken"
    except Exception:
        candles, src = get_candles(5)
        return candles[-1][3], src


def build_price_message():
    p, src = get_price()
    return (f"💰 <b>قیمت لحظه‌ای بیت‌کوین</b>\n\n<b>${fmt(p)}</b>\n"
            f"🕒 {now_str()} | منبع: {src}")


def build_tf_message(tf):
    candles, src = get_candles(tf)
    a = analyze(candles)
    return (f"🟠 <b>تحلیل بیت‌کوین (BTC)</b>\n🕒 {now_str()} | منبع: {src}\n"
            f"💰 قیمت: <b>${fmt(a['price'])}</b>\n\n{tf_block(tf, a)}\n\n"
            "<i>تحلیل الگوریتمی و آموزشی است، توصیه مالی نیست.</i>")


def safe_content(kind):
    try:
        if kind == "price":
            return build_price_message()
        if kind == "full":
            return build_message()
        return build_tf_message(int(kind))
    except Exception as e:
        log.error("content failed: %s", e)
        return "⚠️ دریافت داده با مشکل روبه‌رو شد؛ چند ثانیه بعد دوباره تلاش کن."


# ---------------------------------------------------------------- مدیریت دکمه‌ها و دستورها
def handle_callback(cq):
    cid, data = cq["id"], cq.get("data", "")
    msg = cq.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    mid = msg.get("message_id")
    if chat_id is None:
        answer(cid, "خطا در تشخیص چت")
        return

    is_admin = bool(CHAT_ID) and str(chat_id) == CHAT_ID

    if data in ("full", "tf5", "tf15", "tf60", "tf240", "price"):
        answer(cid, "⏳ در حال دریافت...")
        kind = {"tf5": "5", "tf15": "15", "tf60": "60", "tf240": "240"}.get(data, data)
        send(safe_content(kind), result_markup(kind), chat_id=chat_id)
    elif data.startswith("re_"):
        answer(cid, "🔄 در حال تازه‌سازی...")
        kind = data[3:]
        edit(chat_id, mid, safe_content(kind), result_markup(kind))
    elif data == "menu":
        answer(cid)
        send(menu_text(), menu_markup(chat_id), chat_id=chat_id)
    elif data == "toggle":
        if not is_admin:
            answer(cid, "این تنظیم فقط برای مدیر ربات فعال است")
            return
        state["auto"] = not state["auto"]
        answer(cid, "✅ ارسال خودکار فعال شد" if state["auto"] else "⏸ ارسال خودکار متوقف شد")
        edit(chat_id, mid, menu_text(), menu_markup(chat_id))
    elif data.startswith("int"):
        if not is_admin:
            answer(cid, "این تنظیم فقط برای مدیر ربات فعال است")
            return
        state["interval"] = float(data[3:])
        answer(cid, f"🕒 از این به بعد هر {fa(state['interval'])} دقیقه")
        edit(chat_id, mid, menu_text(), menu_markup(chat_id))
    elif data == "help":
        answer(cid)
        send(HELP_TEXT, chat_id=chat_id)
    else:
        answer(cid)


def handle_message(m):
    chat_id = m.get("chat", {}).get("id")
    if chat_id is None:
        return
    t = (m.get("text") or "").strip().lower()
    chat_type = m.get("chat", {}).get("type", "")
    if t.startswith("/start"):
        if chat_type == "private":
            subscribe(chat_id, m.get("from", {}))
            send("✅ عضویت شما فعال شد. از این به بعد سیگنال‌های جدید برایتان ارسال می‌شود.\nبرای لغو عضویت: /stop", chat_id=chat_id)
        send(menu_text(), menu_markup(chat_id), chat_id=chat_id)
    elif t.startswith("/stop"):
        unsubscribe(chat_id)
        send("🔕 عضویت شما لغو شد و دیگر سیگنال خودکار دریافت نمی‌کنید. هر زمان خواستید با /start دوباره عضو شوید.", chat_id=chat_id)
    elif t.startswith("/menu"):
        send(menu_text(), menu_markup(chat_id), chat_id=chat_id)
    elif t.startswith("/analyze"):
        send(safe_content("full"), result_markup("full"), chat_id=chat_id)
    elif t.startswith("/price"):
        send(safe_content("price"), result_markup("price"), chat_id=chat_id)
    elif t.startswith("/help"):
        send(HELP_TEXT, chat_id=chat_id)


def poll_loop():
    offset = None
    while True:
        try:
            r = HTTP.get(f"{API}/getUpdates", timeout=45,
                         params={"timeout": 30, "offset": offset,
                                 "allowed_updates": '["message","callback_query"]'})
            if r.status_code == 409:  # دو نسخه هم‌زمان (هنگام دیپلوی)؛ کمی صبر کن
                time.sleep(10)
                continue
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                try:
                    if "callback_query" in u:
                        handle_callback(u["callback_query"])
                    elif "message" in u:
                        handle_message(u["message"])
                except Exception as e:
                    log.error("update failed: %s", e)
        except Exception as e:
            log.warning("polling error: %s", type(e).__name__)
            time.sleep(5)


def bot_loop():
    call("setMyCommands", commands=[
        {"command": "menu", "description": "📋 نمایش منو"},
        {"command": "analyze", "description": "📊 تحلیل همین الان"},
        {"command": "price", "description": "💰 قیمت لحظه‌ای"},
        {"command": "help", "description": "ℹ️ راهنما"},
        {"command": "stop", "description": "🔕 لغو دریافت سیگنال خودکار"},
    ])
    if CHAT_ID:
        send(menu_text(), menu_markup(CHAT_ID), chat_id=CHAT_ID)
    last = 0.0
    while True:
        if state["auto"] and time.time() - last >= float(state["interval"]) * 60:
            last = time.time()
            try:
                message = build_message()
                broadcast(message, result_markup("full"))
            except Exception as e:
                log.error("cycle failed: %s", e)
        time.sleep(3)


# ---------------------------------------------------------------- وب‌سرور (برای رندر)
class Handler(BaseHTTPRequestHandler):
    def _ok(self, body=True):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        if body:
            self.wfile.write(b"BTC bot is running")

    def do_GET(self):
        self._ok()

    def do_HEAD(self):
        self._ok(False)

    def log_message(self, *args):
        pass


def keep_alive():
    # رندر رایگان بعد از ۱۵ دقیقه بدون ترافیک ورودی می‌خوابد؛ این پینگ جلوی آن را می‌گیرد
    if not SELF_URL:
        return
    while True:
        time.sleep(8 * 60)
        try:
            HTTP.get(SELF_URL, timeout=15)
        except Exception:
            pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("TELEGRAM_TOKEN باید در Environment ست شود")
    init_subscribers_db()
    if not CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID تنظیم نشده؛ ارسال خودکار اختصاصی مدیر غیرفعال است")
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    log.info("listening on %s", PORT)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
