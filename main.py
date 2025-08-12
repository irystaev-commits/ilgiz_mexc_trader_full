import os, re, time, hmac, hashlib, json, asyncio, requests
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ========= ENV =========
TG_TOKEN = (os.getenv("TELEGRAM_TOKEN") or os.getenv("TG_TOKEN") or "").strip()
ALLOWED_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
TZ = os.getenv("TZ", "Asia/Ho_Chi_Minh")
PAPER = os.getenv("PAPER_MODE", "true").lower() == "true"

# сигнал-сканер (дефолт и настраивается /set)
WATCHLIST = [s.strip().upper() for s in os.getenv(
    "WATCHLIST",
    "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,AVAXUSDT,INJUSDT,SUIUSDT,OPUSDT,NEARUSDT,LINKUSDT,MATICUSDT,SEIUSDT"
).split(",") if s.strip()]

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "5"))  # мин
TP1_PCT = float(os.getenv("TP1_PCT", "3.0"))
TP2_PCT = float(os.getenv("TP2_PCT", "6.0"))
SL_PCT  = float(os.getenv("SL_PCT", "-2.0"))

# расписание авто‑дайджестов
CRON_TIMES = [t.strip() for t in os.getenv("CRON_TIMES", "09:00,19:00,21:00,23:00").split(",") if t.strip()]

# (опционально) MEXC — оставим, чтобы потом включить реальную торговлю
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "").strip()
MEXC_SECRET  = os.getenv("MEXC_SECRET_KEY", "").strip()
BASE_MEXC = "https://api.mexc.com"

# ====== guards ======
if not TG_TOKEN or ":" not in TG_TOKEN:
    raise RuntimeError(f"Bad TELEGRAM_TOKEN: len={len(TG_TOKEN)}")

bot = Bot(token=TG_TOKEN, parse_mode="HTML")
dp  = Dispatcher(bot)
scheduler = AsyncIOScheduler(timezone=TZ)

# ====== utils ======
def ts() -> int: return int(time.time() * 1000)

def _fetch(url, timeout=15):
    try:
        return requests.get(url, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
    except Exception:
        return None

def binance_price(sym: str):
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={sym}"
    r = _fetch(url, 8)
    if not r or r.status_code != 200:
        return None
    try:
        return float(r.json().get("price", 0))
    except Exception:
        return None

def fmt_pct(x: float) -> str:
    s = f"{x:+.1f}%"
    return f"<b>{s}</b>"

def ensure(m: types.Message) -> bool:
    return (ALLOWED_ID == 0) or (m.from_user.id == ALLOWED_ID)

# ====== простые новости/рынок ======
NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.investing.com/rss/market_overview.rss"
]

def fetch_news(limit=6):
    items = []
    for feed in NEWS_FEEDS:
        r = _fetch(feed, 15)
        if not r or r.status_code != 200: 
            continue
        titles = re.findall(r"<title>(.*?)</title>", r.text, re.I|re.S)
        for t in titles[1:12]:
            t = re.sub("<.*?>", "", t).strip()
            if t and t not in items:
                items.append(t)
            if len(items) >= limit: break
        if len(items) >= limit: break
    return items

# ====== «бумажный» портфель (локально в памяти процесса) ======
PORTF = {}  # {'SOLUSDT': {'qty': 10.0, 'avg': 55.0}}

def hold_add(symbol: str, qty: float, price: float):
    s = symbol.upper()
    if s not in PORTF:
        PORTF[s] = {"qty": 0.0, "avg": price}
    pos = PORTF[s]
    new_qty = pos["qty"] + qty
    if new_qty <= 0:
        PORTF.pop(s, None)
        return {"removed": True}
    pos["avg"] = (pos["avg"] * pos["qty"] + price * qty) / new_qty
    pos["qty"] = new_qty
    return {"qty": pos["qty"], "avg": pos["avg"]}

def hold_rm(symbol: str, qty: float, price: float):
    s = symbol.upper()
    if s not in PORTF:
        return {"error": "Нет позиции"}
    pos = PORTF[s]
    sell_qty = min(qty, pos["qty"])
    pos["qty"] -= sell_qty
    pnl = (price - pos["avg"]) * sell_qty
    closed = False
    if pos["qty"] <= 0:
        PORTF.pop(s, None)
        closed = True
    return {"pnl": pnl, "left": pos["qty"] if not closed else 0.0, "closed": closed}

def portf_report():
    if not PORTF: 
        return "Портфель пуст."
    lines = []
    total_usdt = 0.0
    for s, p in PORTF.items():
        px = binance_price(s) or 0.0
        chg = 0.0 if p["avg"] == 0 else (px/p["avg"] - 1.0) * 100
        val = px * p["qty"]
        total_usdt += val
        lines.append(f"• {s}: {p['qty']:.4f} @ {p['avg']:.4f} → <code>{px:.4f}</code> ({fmt_pct(chg)}) ≈ <b>{val:.2f} USDT</b>")
    lines.append(f"\nИтого ≈ <b>{total_usdt:.2f} USDT</b>")
    return "\n".join(lines)

# ====== сканер сигналов ======
def scan_market():
    out = []
    for s in WATCHLIST:
        px = binance_price(s)
        if not px:
            continue
        # простая логика: берём изменение относительно 1-часовой свечи (приблизительно через 24h price)
        # тут для простоты возьмём ещё один быстрый источник — 24h change на binance
        r = _fetch(f"https://api.binance.com/api/v3/ticker/24hr?symbol={s}", 8)
        chg = 0.0
        if r and r.status_code == 200:
            try: chg = float(r.json().get("priceChangePercent", 0.0))
            except: chg = 0.0

        # рекомендация
        advice = "🤝 Держать"
        reason = "Тренд нейтральный"
        if chg >= TP2_PCT:
            advice = "✅ Зафиксировать часть"
            reason = f"Рост за 24ч {chg:.1f}% ≥ TP2={TP2_PCT:.1f}%"
        elif chg >= TP1_PCT:
            advice = "🟢 Можно фиксировать 30%"
            reason = f"Рост за 24ч {chg:.1f}% ≥ TP1={TP1_PCT:.1f}%"
        elif chg <= SL_PCT:
            advice = "🛑 Сократить позицию"
            reason = f"Падение {chg:.1f}% ≤ SL={SL_PCT:.1f}%"

        out.append({
            "symbol": s, "price": px, "chg": chg,
            "advice": advice, "reason": reason
        })
    return out

def make_digest():
    rows = scan_market()
    if not rows:
        return "⚠️ Данные рынка недоступны."
    lines = ["🧭 <b>Сканер рынка</b> (Binance 24h)"]
    for r in rows:
        lines.append(
            f"• <b>{r['symbol']}</b> <code>{r['price']:.4f}</code> "
            f"Δ24h={fmt_pct(r['chg'])} → {r['advice']} — {r['reason']}"
        )
    lines.append(f"\nЦели: TP1={TP1_PCT:.1f}%, TP2={TP2_PCT:.1f}%, SL={SL_PCT:.1f}%")
    lines.append(f"Watchlist: {', '.join(WATCHLIST)}")
    lines.append(f"PAPER_MODE={'ON' if PAPER else 'OFF'}")
    return "\n".join(lines)

# ====== Команды ======
@dp.message_handler(commands=["start","help"])
async def cmd_start(m: types.Message):
    if not ensure(m): return
    txt = (
        "🤖 Я готов.\n"
        "Команды:\n"
        "• /news — новости 🗞️\n"
        "• /market — цены BTC/ETH 📊\n"
        "• /status — статус сканера\n"
        "• /advice <SYMBOL> — совет по монете (напр. /advice SOL)\n"
        "• /hold add SOL 10 @ 55 — добавить позицию\n"
        "• /hold rm SOL 5 — списать (продажа)\n"
        "• /hold report — отчёт по портфелю\n"
        "• /set tp1=5 tp2=12 sl=-3 iv=5 wl=BTCUSDT,ETHUSDT,SOLUSDT — обновить параметры\n"
        f"PAPER_MODE={'ON' if PAPER else 'OFF'}"
    )
    await m.answer(txt)

@dp.message_handler(commands=["news"])
async def cmd_news(m: types.Message):
    if not ensure(m): return
    items = fetch_news(6)
    if not items:
        return await m.answer("⚠️ Новости недоступны.")
    await m.answer("🗞️ <b>Новости</b>\n" + "\n".join([f"• {t}" for t in items]))

@dp.message_handler(commands=["market"])
async def cmd_market(m: types.Message):
    if not ensure(m): return
    btc = binance_price("BTCUSDT"); eth = binance_price("ETHUSDT")
    await m.answer(f"📊 <b>Рынок</b>\nBTC: <code>{btc}</code>\nETH: <code>{eth}</code>")

@dp.message_handler(commands=["status"])
async def cmd_status(m: types.Message):
    if not ensure(m): return
    txt = (f"🛠️ <b>Статус сканера</b>\n"
           f"Watchlist: {', '.join(WATCHLIST)}\n"
           f"Интервал: каждые {SCAN_INTERVAL} мин\n"
           f"Цели: TP1={TP1_PCT:.1f}%, TP2={TP2_PCT:.1f}%, SL={SL_PCT:.1f}%")
    await m.answer(txt)

@dp.message_handler(commands=["advice"])
async def cmd_advice(m: types.Message):
    if not ensure(m): return
    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("Пример: <code>/advice SOL</code>")
    sym = parts[1].upper()
    if not sym.endswith("USDT"): sym += "USDT"
    px = binance_price(sym)
    if not px: return await m.answer("Цена недоступна.")
    r = scan_market()
    found = next((x for x in r if x["symbol"]==sym), None)
    if not found:
        found = {"chg": 0, "advice": "🤝 Держать", "reason": "Нет данных по 24h"}
    await m.answer(
        f"🧠 <b>Совет</b> по {sym}\n"
        f"Цена: <code>{px:.4f}</code>\n"
        f"Δ24h={fmt_pct(found['chg'])}\n"
        f"Рекомендация: {found['advice']}\n"
        f"Причина: {found['reason']}\n"
        f"Цели: TP1={TP1_PCT:.1f}% / TP2={TP2_PCT:.1f}% / SL={SL_PCT:.1f}%"
    )

# /hold блок
H_ADD_RE = re.compile(r"^/hold\s+add\s+([A-Z]{2,10})\s+(\d+(?:\.\d+)?)\s*@\s*(\d+(?:\.\d+)?)$", re.I)
H_RM_RE  = re.compile(r"^/hold\s+rm\s+([A-Z]{2,10})\s+(\d+(?:\.\d+)?)$", re.I)

@dp.message_handler(lambda m: m.text and m.text.lower().startswith("/hold"))
async def cmd_hold(m: types.Message):
    if not ensure(m): return
    t = m.text.strip()
    if t.endswith("report"):
        return await m.answer("📒 <b>Портфель</b>\n" + portf_report())

    mt = H_ADD_RE.match(t)
    if mt:
        sym, qty, price = mt.groups()
        sym = sym.upper()
        if not sym.endswith("USDT"): sym += "USDT"
        res = hold_add(sym, float(qty), float(price))
        if "removed" in res:
            return await m.answer(f"Позиция {sym} обнулена.")
        return await m.answer(f"➕ Добавлено: {sym} {float(qty):.4f} @ {float(price):.4f}\nТекущая позиция: {res}")

    mt = H_RM_RE.match(t)
    if mt:
        sym, qty = mt.groups()
        sym = sym.upper()
        if not sym.endswith("USDT"): sym += "USDT"
        px = binance_price(sym) or 0.0
        res = hold_rm(sym, float(qty), px)
        if "error" in res:
            return await m.answer("Нет такой позиции.")
        note = "закрыта" if res["closed"] else f"осталось {res['left']:.4f}"
        return await m.answer(f"➖ Продано: {sym} {float(qty):.4f} @ {px:.4f}\nP/L ≈ <b>{res['pnl']:.2f} USDT</b>, {note}")

    await m.answer("Примеры:\n• /hold add SOL 10 @ 55\n• /hold rm SOL 3\n• /hold report")

# /set — настройка параметров на лету
@dp.message_handler(commands=["set"])
async def cmd_set(m: types.Message):
    if not ensure(m): return
    global TP1_PCT, TP2_PCT, SL_PCT, SCAN_INTERVAL, WATCHLIST
    args = m.text.split()[1:]
    if not args:
        return await m.answer("Пример: /set tp1=5 tp2=12 sl=-3 iv=5 wl=BTCUSDT,ETHUSDT,SOLUSDT")
    changed = []
    for a in args:
        if "=" not in a: continue
        k, v = a.split("=", 1)
        k = k.lower()
        if k == "tp1":
            TP1_PCT = float(v); changed.append(f"TP1={TP1_PCT}%")
        elif k == "tp2":
            TP2_PCT = float(v); changed.append(f"TP2={TP2_PCT}%")
        elif k == "sl":
            SL_PCT = float(v); changed.append(f"SL={SL_PCT}%")
        elif k == "iv":
            SCAN_INTERVAL = max(1, int(v)); changed.append(f"интервал={SCAN_INTERVAL}м")
        elif k == "wl":
            WATCHLIST = [s.strip().upper() for s in v.split(",") if s.strip()]
            changed.append(f"watchlist={len(WATCHLIST)} пар")
    await m.answer("✅ Обновлено: " + ", ".join(changed))

# ========= SCHEDULES =========
def schedule_digest_jobs():
    for t in CRON_TIMES:
        try:
            hh, mm = map(int, t.split(":"))
            scheduler.add_job(
                lambda: asyncio.create_task(bot.send_message(ALLOWED_ID or 0, make_digest())),
                CronTrigger(hour=hh, minute=mm)
            )
        except Exception:
            pass

def schedule_scanner_ping():
    # фоновая отправка краткого сигнала каждые SCAN_INTERVAL минут (только в личку ALLOWED_ID)
    async def job():
        if not ALLOWED_ID:
            return
        rows = scan_market()
        if not rows: 
            return
        top = []
        for r in rows[:6]:
            top.append(f"{r['symbol']} {fmt_pct(r['chg'])} → {r['advice']}")
        await bot.send_message(ALLOWED_ID, "⏱️ <b>Мини‑скан</b>\n" + "\n".join(top))
    scheduler.add_job(lambda: asyncio.create_task(job()), f"interval", minutes=SCAN_INTERVAL)

# ========= run =========
if __name__ == "__main__":
    schedule_digest_jobs()
    schedule_scanner_ping()
    scheduler.start()
    executor.start_polling(dp, skip_updates=True)
