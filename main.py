"""
ربات تحلیل بیت‌کوین (تایم ۵ و ۱۵ دقیقه) برای تلگرام
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

TIMEFRAMES = [5, 15]
TZ = timezone(timedelta(hours=3, minutes=30))  # وقت ایران
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 (btc-bot)"})


# ---------------------------------------------------------------- داده‌ها
# هر کندل: (open, high, low, close, volume) - فقط کندل‌های بسته‌شده
def from_kraken(tf):
    r = HTTP.get("https://api.kraken.com/0/public/OHLC",
                 params={"pair": "XBTUSD", "interval": tf}, timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(str(j["error"]))
    rows = next(v for k, v in j["result"].items() if k != "last")[:-1]
    return [(float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[6])) for x in rows]


def from_okx(tf):
    r = HTTP.get("https://www.okx.com/api/v5/market/candles",
                 params={"instId": "BTC-USDT", "bar": f"{tf}m", "limit": 300}, timeout=15)
    r.raise_for_status()
    data = [d for d in r.json()["data"] if d[8] == "1"]
    data.reverse()
    return [(float(d[1]), float(d[2]), float(d[3]), float(d[4]), float(d[5])) for d in data]


def from_binance(tf):
    r = HTTP.get("https://api.binance.com/api/v3/klines",
                 params={"symbol": "BTCUSDT", "interval": f"{tf}m", "limit": 300}, timeout=15)
    r.raise_for_status()
    return [(float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in r.json()[:-1]]


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
    s = f"⏱ <b>تایم {tf} دقیقه</b>\n"
    s += f"{SIDE_TXT[a['side']]} | اطمینان: {a['conf']}%\n"
    s += f"RSI: {a['rsi']:.0f} | ATR: {a['atr']:.0f}\n"
    s += f"حمایت: {fmt(a['support'])} | مقاومت: {fmt(a['resistance'])}\n"
    if a["plan"]:
        e, sl, t1, t2 = a["plan"]
        s += f"🎯 ورود: {fmt(e)}\n❌ حد ضرر: {fmt(sl)}\n✅ هدف۱: {fmt(t1)} | هدف۲: {fmt(t2)}\n"
    s += "• " + "\n• ".join(html.escape(w) for w in a["why"][:4])
    return s


def verdict(a5, a15):
    if a5["side"] == a15["side"] and a5["side"] != "flat":
        return "🔥 هر دو تایم هم‌جهت‌اند: " + SIDE_TXT[a5["side"]]
    if "bull" in (a5["side"], a15["side"]) and "bear" in (a5["side"], a15["side"]):
        return "⚠️ تضاد بین تایم‌ها؛ بهتره وارد نشی"
    if a15["side"] != "flat":
        return f"📌 جهت اصلی (۱۵ دقیقه): {SIDE_TXT[a15['side']]}؛ ۵ دقیقه هنوز تایید نکرده"
    return "😴 سیگنال واضحی نیست؛ صبر کن"


def ai_comment(a5, a15):
    if not GEMINI_KEY:
        return ""
    prompt = (
        "تو تحلیلگر تکنیکال بیت‌کوین هستی. بر اساس داده‌های زیر یک جمع‌بندی خیلی کوتاه "
        "(حداکثر ۳ جمله) به فارسی بنویس، بدون توصیه مالی قطعی.\n"
        f"تایم ۵ دقیقه: جهت={a5['side']} امتیاز={a5['score']:.1f} RSI={a5['rsi']:.0f} MACD={a5['macd']:.2f}\n"
        f"تایم ۱۵ دقیقه: جهت={a15['side']} امتیاز={a15['score']:.1f} RSI={a15['rsi']:.0f} MACD={a15['macd']:.2f}\n"
        f"قیمت={a5['price']:.0f} حمایت={a15['support']:.0f} مقاومت={a15['resistance']:.0f}"
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


def build_message():
    res, src = {}, ""
    for tf in TIMEFRAMES:
        candles, src = get_candles(tf)
        res[tf] = analyze(candles)
    a5, a15 = res[5], res[15]
    now = datetime.now(TZ).strftime("%H:%M:%S")
    msg = (f"🟠 <b>تحلیل بیت‌کوین (BTC)</b>\n🕒 {now} | منبع: {src}\n"
           f"💰 قیمت: <b>${fmt(a5['price'])}</b>\n\n"
           f"{tf_block(5, a5)}\n\n{tf_block(15, a15)}\n\n"
           f"<b>{verdict(a5, a15)}</b>")
    ai = ai_comment(a5, a15)
    if ai:
        msg += f"\n\n🤖 {html.escape(ai)}"
    msg += "\n\n<i>تحلیل الگوریتمی و آموزشی است، توصیه مالی نیست.</i>"
    return msg


# ---------------------------------------------------------------- تلگرام
def send(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        r = HTTP.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                                 "disable_web_page_preview": True}, timeout=20)
        if r.status_code == 400 and "parse entities" in r.text:
            plain = html.unescape(re.sub(r"<[^>]+>", "", text))
            r = HTTP.post(url, json={"chat_id": CHAT_ID, "text": plain,
                                     "disable_web_page_preview": True}, timeout=20)
        if not r.ok:
            log.error("Telegram error: %s", r.text[:200])
    except Exception as e:
        log.error("Telegram send failed: %s", type(e).__name__)


def bot_loop():
    send("✅ ربات تحلیل بیت‌کوین روشن شد.")
    while True:
        started = time.time()
        try:
            send(build_message())
            log.info("analysis sent")
        except Exception as e:
            log.error("cycle failed: %s", e)
        time.sleep(max(5, INTERVAL_MIN * 60 - (time.time() - started)))


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
    if not TOKEN or not CHAT_ID:
        raise SystemExit("TELEGRAM_TOKEN و TELEGRAM_CHAT_ID باید در Environment ست شوند")
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    log.info("listening on %s", PORT)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
