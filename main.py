import os, re, json, time, math, asyncio, requests
from datetime import datetime
from typing import Dict, Any

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


# ========= ENV =========
TG_TOKEN         = (os.getenv("TELEGRAM_TOKEN") or os.getenv("TG_TOKEN") or "").strip()
ALLOWED_ID       = int(os.getenv("ALLOWED_USER_ID", "0"))
TZ               = os.getenv("TZ", "Asia/Ho_Chi_Minh")
PAPER            = os.getenv("PAPER_MODE", "true").lower() == "true"
WATCHLIST        = [s.strip().upper() for s in os.getenv("WATCHLIST", "BTC,ETH,SOL,INJ,ENA,TIA,JUP,ZRO").split(",") if s.strip()]
SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL", "60"))  # сек, как часто обновлять /status-сканер
TP1_PCT          = float(os.getenv("TP1_PCT", "2"))       # цель 1 в %
TP2_PCT          = float(os.getenv("TP2_PCT", "5"))       # цель 2 в %
SL_PCT           = float(os.getenv("SL_PCT", "3"))        # стоп-лосс в %
MAX_USDT         = float(os.getenv("MAX_ORDER_USDT", "100"))
DIGEST_TIMES     = [t.strip() for t in os.getenv("DIGEST_TIMES", "09:00,19:00,21:00,23:00").split(",") if t.strip()]

if not TG_TOKEN or ":" not in TG_TOKEN:
    raise RuntimeError(f"Bad TELEGRAM_TOKEN: len={len(TG_TOKEN)}")

# ========= BOT/SCHED =========
bot = Bot(token=TG_TOKEN, parse_mode="HTML")
dp  = Dispatcher(bot)
scheduler = AsyncIOScheduler(timezone=TZ)

# ========= SIMPLE STORAGE (эпhemeral) =========
# Держим состояния портфеля/заметки в файле (переживает рестарт процесса, но не деплой).
STORE_PATH = "store.json"

def load_store() -> Dict[str, Any]:
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"hold": {}, "notes": {}}

def save_store(data: Dict[str, Any]):
    try:
        with open(STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

STORE = load_store()  # {"hold": {"SOL":{"qty":10,"avg":55.0}}, "notes":{}}


# ========= HELPERS =========
def ensure_access(m: types.Message) -> bool:
    return (ALLOWED_ID == 0) or (m.from_user.id == ALLOWED_ID)

def pct(a, b):
    try:
        return (a - b) / b * 100.0
    except Exception:
        return 0.0

def fmt_usd(x): return f"{x:,.2f}".replace(",", " ").replace(".00", "")
def now_hhmm():  return datetime.now().strftime("%H:%M")

# Price sources
def fetch_url(url, timeout=12):
    try:
        return requests.get(url, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
    except Exception:
        return None

def binance_price(sym):  # sym like 'BTCUSDT'
    r = fetch_url(f"https://api.binance.com/api/v3/ticker/price?symbol={sym}", 8)
    if r and r.status_code == 200:
        try:
            return float(r.json().get("price", 0))
        except Exception:
            return None
    return None

def spot_symbol(sym: str) -> str:
    s = sym.upper()
    return s if s.endswith("USDT") else s + "USDT"

# ========= NEWS =========
NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.investing.com/rss/market_overview.rss",
]

def fetch_news(limit=6):
    items = []
    import re
    for feed in NEWS_FEEDS:
        r = fetch_url(feed, 15)
        if not r or r.status_code != 200:
            continue
        titles = re.findall(r"<title>(.*?)</title>", r.text, re.I|re.S)
        for t in titles[1:15]:
            t = re.sub("<.*?>", "", t).strip()
            if t and t not in items:
                items.append(t)
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break
    return items


# ========= COMMANDS =========
@dp.message_handler(commands=["start","help"])
async def start(m: types.Message):
    if not ensure_access(m):
        return await m.answer("⛔️ Нет доступа.")
    txt = (
        "🤖 Я готов.\n"
        "Команды:\n"
        "• /news — новости 📰\n"
        "• /market — цены BTC/ETH 📊\n"
        "• /status — статус сканера\n"
        "• /hold add SOL 12.5 @ 56.2 — добавить позицию\n"
        "• /hold rm  SOL 5 — списать (продажа)\n"
        "• /hold report — отчёт по портфелю\n"
        "• /advice SOL — совет: зафиксировать/держать (с пояснением)\n"
        f"\nРежим: {'PAPER' if PAPER else 'LIVE'}  • TZ={TZ}\n"
        f"Дайджесты: {', '.join(DIGEST_TIMES)}"
    )
    await m.answer(txt)

@dp.message_handler(commands=["news"])
async def cmd_news(m: types.Message):
    if not ensure_access(m): return
    items = fetch_news(6)
    if not items:
        return await m.answer("⚠️ Новости недоступны.")
    await m.answer("📰 <b>Новости</b>\n" + "\n".join([f"• {t}" for t in items]))

@dp.message_handler(commands=["market"])
async def cmd_market(m: types.Message):
    if not ensure_access(m): return
    btc = binance_price("BTCUSDT")
    eth = binance_price("ETHUSDT")
    await m.answer(f"📊 <b>Рынок</b>\nBTC: <code>{btc}</code>\nETH: <code>{eth}</code>")

@dp.message_handler(commands=["status"])
async def cmd_status(m: types.Message):
    if not ensure_access(m): return
    await m.answer(
        "🛠 <b>Сканер</b>\n"
        f"Слежу за: {', '.join(WATCHLIST)}\n"
        f"Интервал: {SCAN_INTERVAL}s  | TP1={TP1_PCT}%  TP2={TP2_PCT}%  SL={SL_PCT}%\n"
        f"Время: {now_hhmm()}  • TZ={TZ}\n"
        f"Режим: {'PAPER' if PAPER else 'LIVE'}"
    )

# ---- HOLD SUBSYSTEM (ручной учёт)
HOLD_RE_ADD = re.compile(r"^/hold\s+add\s+([A-Z]{2,10})\s+([\d\.]+)\s*@\s*([\d\.]+)\s*$", re.I)
HOLD_RE_RM  = re.compile(r"^/hold\s+rm\s+([A-Z]{2,10})\s+([\d\.]+)\s*$", re.I)

@dp.message_handler(lambda m: m.text and HOLD_RE_ADD.match(m.text))
async def hold_add(m: types.Message):
    if not ensure_access(m): return
    sym, qty, price = HOLD_RE_ADD.match(m.text).groups()
    sym = sym.upper(); qty=float(qty); price=float(price)
    pos = STORE["hold"].get(sym, {"qty":0.0, "avg":0.0})
    new_qty = pos["qty"] + qty
    new_avg = (pos["avg"]*pos["qty"] + price*qty) / new_qty if new_qty>0 else 0.0
    STORE["hold"][sym] = {"qty": round(new_qty,6), "avg": round(new_avg,6)}
    save_store(STORE)
    await m.answer(f"➕ Добавлено: <b>{sym}</b> {qty} @ {price}\nТекущая позиция: {STORE['hold'][sym]}")

@dp.message_handler(lambda m: m.text and HOLD_RE_RM.match(m.text))
async def hold_rm(m: types.Message):
    if not ensure_access(m): return
    sym, qty = HOLD_RE_RM.match(m.text).groups()
    sym = sym.upper(); qty=float(qty)
    pos = STORE["hold"].get(sym)
    if not pos:
        return await m.answer("⚠️ Позиция не найдена.")
    new_qty = max(0.0, pos["qty"] - qty)
    if new_qty == 0:
        STORE["hold"].pop(sym, None)
        msg = f"➖ Продажа: {sym} {qty}. Позиция закрыта."
    else:
        STORE["hold"][sym]["qty"] = round(new_qty,6)
        msg = f"➖ Продажа: {sym} {qty}. Остаток: {STORE['hold'][sym]}"
    save_store(STORE)
    await m.answer(msg)

@dp.message_handler(commands=["hold"])
async def hold_report(m: types.Message):
    if not ensure_access(m): return
    parts = ["💼 <b>Отчёт по портфелю</b>"]
    total_val = 0.0
    for sym, pos in STORE["hold"].items():
        px = binance_price(spot_symbol(sym)) or 0.0
        val = pos["qty"] * px
        total_val += val
        p = pct(px, pos["avg"])
        parts.append(f"• {sym}: qty={pos['qty']}  avg={pos['avg']}  px={px:.4f}  PnL={p:+.2f}%")
    parts.append(f"\nИтого оценка: ≈ <b>{fmt_usd(total_val)}</b> USDT")
    await m.answer("\n".join(parts))

# ---- Advice
@dp.message_handler(commands=["advice"])
async def advice(m: types.Message):
    if not ensure_access(m): return
    args = m.get_args().strip().upper()
    if not args:
        return await m.answer("Формат: <code>/advice SOL</code>")
    sym = args
    pos = STORE["hold"].get(sym)
    px = binance_price(spot_symbol(sym))
    if not px:
        return await m.answer("⚠️ Цена недоступна сейчас.")
    if not pos:
        # нет позиции → совет по входу
        msg = (
            f"🧭 <b>{sym}</b> сейчас {px:.4f}.\n"
            f"Совет: ждать сигнала пробоя/объёма. Вход частями 25–30% при откате, SL {SL_PCT}%. "
            f"Цели: +{TP1_PCT}% и +{TP2_PCT}% от входа."
        )
        return await m.answer(msg)

    # есть позиция
    gain = pct(px, pos["avg"])
    explain = []
    decision = "Держать 🟢"
    if gain >= TP2_PCT:
        decision = "Зафиксировать 50% ✅"
        explain.append(f"Профит ≥ TP2 ({TP2_PCT}%) — частичная фиксация снижает риск.")
    elif gain >= TP1_PCT:
        decision = "Зафиксировать 25% ✅"
        explain.append(f"Профит ≥ TP1 ({TP1_PCT}%).")
    elif gain <= -SL_PCT:
        decision = "Стоп 100% ❌"
        explain.append(f"Уход ниже SL ({SL_PCT}%).")

    if not explain:
        explain.append("Тренд нейтральный/умеренно позитивный, поводов для фиксации нет.")

    await m.answer(
        f"🧭 <b>{sym}</b>\n"
        f"avg={pos['avg']} → px={px:.4f}  PnL={gain:+.2f}%\n"
        f"Решение: <b>{decision}</b>\n"
        "Причина: " + " ".join(explain)
    )


# ========= SCHEDULER =========
async def send_digest():
    if ALLOWED_ID == 0:
        return
    btc = binance_price("BTCUSDT")
    eth = binance_price("ETHUSDT")
    news = fetch_news(3)
    msg = [
        f"🗞 <b>Дайджест {now_hhmm()}</b>",
        f"• BTC: <code>{btc}</code>  • ETH: <code>{eth}</code>",
        "• Новости:",
    ] + [f"  – {t}" for t in news]
    msg.append("\nСовет: /advice SOL (пример) • Отчёт: /hold")
    try:
        await bot.send_message(ALLOWED_ID, "\n".join(msg))
    except Exception:
        pass

def schedule_jobs():
    # Дайджесты по времени из ENV
    for t in DIGEST_TIMES:
        try:
            hh, mm = t.split(":")
            scheduler.add_job(
                lambda: asyncio.create_task(send_digest()),
                CronTrigger(hour=int(hh), minute=int(mm))
            )
        except Exception:
            continue
    # Лёгкий «сканер» – просто пинг раз в SCAN_INTERVAL (можно расширять логикой)
    scheduler.add_job(
        lambda: None,  # место для фоновой логики
        "interval", seconds=max(30, SCAN_INTERVAL)
    )


# ========= START =========
if __name__ == "__main__":
    schedule_jobs()
    scheduler.start()
    executor.start_polling(dp, skip_updates=True)
