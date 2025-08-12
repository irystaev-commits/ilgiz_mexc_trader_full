import os, re, time, json, asyncio, requests
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

# ========= ENV (из Railway Variables) =========
TG_TOKEN   = (os.getenv("TELEGRAM_TOKEN") or os.getenv("TG_TOKEN") or "").strip()
ALLOWED_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"

# Часовой пояс и время автодайджестов (строго по твоему TZ)
TZ          = os.getenv("TZ", "Asia/Ho_Chi_Minh")
NEWS_TIMES  = [t.strip() for t in os.getenv("NEWS_TIMES", "09:00,19:00,21:00,23:00").split(",") if t.strip()]

# Сканер (watchlist и интервал проверки в секундах)
WATCHLIST     = [s.strip().upper() for s in os.getenv(
    "WATCHLIST",
    "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,AVAXUSDT,INJUSDT,OPUSDT,NEARUSDT,LINKUSDT,MATICUSDT,SEIUSDT"
).split(",") if s.strip()]
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))  # 300сек = 5мин по умолчанию

# Цели/стопы в процентах (для советов и сигналов)
TP1_PCT = float(os.getenv("TP1_PCT", "3.0"))   # +3%
TP2_PCT = float(os.getenv("TP2_PCT", "6.0"))   # +6%
SL_PCT  = float(os.getenv("SL_PCT",  "2.0"))   # -2%

# Применим TZ к процессу (на Linux)
try:
    os.environ["TZ"] = TZ
    time.tzset()
except Exception:
    pass

# ========= Checks =========
if not TG_TOKEN or ":" not in TG_TOKEN:
    raise RuntimeError("Bad TELEGRAM_TOKEN")

# ========= Bot =========
bot = Bot(token=TG_TOKEN, parse_mode="HTML")
dp  = Dispatcher(bot)

# ========= Локальный «бумажный» учёт позиций =========
# Пример команд:
# /hold add SOL 10 @ 55
# /hold rm  SOL 3
# /hold report
HOLD = {}  # {"SOLUSDT": {"qty": 10.0, "avg": 55.0}}

# ========= Helpers =========
def ensure(m: types.Message) -> bool:
    return (ALLOWED_ID == 0) or (m.from_user.id == ALLOWED_ID)

def http_get(url, params=None, timeout=10):
    try:
        return requests.get(url, params=params, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
    except Exception:
        return None

def binance_price(symbol: str):
    r = http_get("https://api.binance.com/api/v3/ticker/price", {"symbol": symbol})
    if r and r.status_code == 200:
        try: return float(r.json()["price"])
        except: return None
    return None

def binance_24h(symbol: str):
    r = http_get("https://api.binance.com/api/v3/ticker/24hr", {"symbol": symbol})
    if r and r.status_code == 200:
        try: return float(r.json().get("priceChangePercent", 0.0))
        except: return 0.0
    return 0.0

def fmt_pct(x):
    try: return f"{x:+.1f}%"
    except: return "+0%"

# ========= Новости (коротко) =========
FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.investing.com/rss/market_overview.rss",
]
def fetch_news(limit=4):
    import re
    items=[]
    for u in FEEDS:
        r=http_get(u, timeout=12)
        if not r or r.status_code!=200: continue
        titles=re.findall(r"<title>(.*?)</title>", r.text, re.I|re.S)
        for t in titles[1:12]:
            t=re.sub("<.*?>","",t).strip()
            if t and t not in items:
                items.append(t)
            if len(items)>=limit: break
        if len(items)>=limit: break
    return items

# ========= Команды =========
@dp.message_handler(commands=["start","help"])
async def cmd_start(m: types.Message):
    if not ensure(m): return await m.answer("⛔️ Нет доступа.")
    await m.answer(
        "🤖 Я готов.\n"
        "Команды:\n"
        "• /status — статус сканера\n"
        "• /advice — совет по текущим позициям\n"
        "• /hold add SOL 10 @ 55 — добавить позицию\n"
        "• /hold rm SOL 3 — списать (продажа)\n"
        "• /hold report — отчёт по портфелю\n"
        "• /news — краткие новости\n"
        f"\nPAPER_MODE={'ON' if PAPER_MODE else 'OFF'} • TZ={TZ}\n"
        f"Дайджесты: {', '.join(NEWS_TIMES)}"
    )

@dp.message_handler(commands=["status"])
async def cmd_status(m: types.Message):
    if not ensure(m): return
    await m.answer(
        "🛠️ <b>Статус</b>\n"
        f"Watchlist: {', '.join(WATCHLIST)}\n"
        f"Интервал: каждые {SCAN_INTERVAL//60} мин\n"
        f"Цели: TP1={TP1_PCT:.1f}%  TP2={TP2_PCT:.1f}%  SL={-SL_PCT:.1f}%\n"
        f"PAPER_MODE: {'ON' if PAPER_MODE else 'OFF'}"
    )

@dp.message_handler(commands=["news"])
async def cmd_news(m: types.Message):
    if not ensure(m): return
    items = fetch_news(4)
    if not items: return await m.answer("⚠️ Новости недоступны.")
    await m.answer("📰 <b>Новости</b>\n" + "\n".join([f"• {t}" for t in items]))

# ========= Учёт /hold =========
POS_RE = re.compile(r"^/hold\s+(add|rm)\s+([A-Za-z]{2,10})\s+(\d+(?:\.\d+)?)\s*(?:@\s*(\d+(?:\.\d+)?))?$")

@dp.message_handler(commands=["hold"])
async def cmd_hold(m: types.Message):
    if not ensure(m): return
    t = m.text.strip()
    if t == "/hold" or t.endswith("report"):
        return await m.answer("📒 <b>Портфель</b>\n" + build_portfolio_report())

    mt = POS_RE.match(t)
    if not mt:
        return await m.answer("Формат:\n/hold add SOL 10 @ 55\n/hold rm SOL 3\n/hold report")

    action, sym, qty, px = mt.groups()
    sym = sym.upper(); 
    if not sym.endswith("USDT"): sym += "USDT"
    qty = float(qty)

    if action.lower()=="add":
        price = float(px) if px else (binance_price(sym) or 0.0)
        pos = HOLD.setdefault(sym, {"qty":0.0, "avg":0.0})
        new_qty = pos["qty"] + qty
        pos["avg"] = (pos["avg"]*pos["qty"] + price*qty)/new_qty if new_qty>0 else 0.0
        pos["qty"] = new_qty
        return await m.answer(f"➕ Добавлено: <b>{sym}</b> {qty} @ {price}\nТекущая позиция: {pos}")

    # rm
    pos = HOLD.get(sym)
    if not pos or pos["qty"]<=0:
        return await m.answer("Позиция не найдена.")
    sell_qty = min(qty, pos["qty"])
    pos["qty"] -= sell_qty
    if pos["qty"]<=0: HOLD.pop(sym, None)
    return await m.answer(f"➖ Списано: <b>{sym}</b> {sell_qty}\nОстаток: {pos['qty'] if sym in HOLD else 0.0}")

def build_portfolio_report():
    if not HOLD:
        return "Портфель пуст. Пример: /hold add SOL 10 @ 55"
    lines=[]; total=0.0
    for sym, pos in HOLD.items():
        px = binance_price(sym) or 0.0
        pnl = (px/pos["avg"]-1)*100 if pos["avg"]>0 else 0.0
        val = px*pos["qty"]; total+=val
        lines.append(f"• {sym}: {pos['qty']:.4f} @ {pos['avg']:.4f} → {px:.4f} ({fmt_pct(pnl)}) ≈ <b>{val:.2f} USDT</b>")
    lines.append(f"\nИтого ≈ <b>{total:.2f} USDT</b>")
    return "\n".join(lines)

# ========= Советы по текущим позициям =========
def build_advice_text():
    if not HOLD:
        return "Пока позиций нет. Добавь: /hold add SOL 10 @ 55"
    lines = ["💡 <b>Совет по портфелю</b>"]
    for sym, pos in HOLD.items():
        px = binance_price(sym)
        if px is None:
            lines.append(f"• {sym}: цена недоступна"); continue
        change = (px/pos["avg"]-1)*100 if pos["avg"]>0 else 0.0
        if change >= TP2_PCT:
            lines.append(f"• {sym}: {px:.4f} — ✅ TP2: зафиксируй 80% (прибыль {change:.1f}%). ℹ️ Сильный импульс — снижаем риск.")
        elif change >= TP1_PCT:
            lines.append(f"• {sym}: {px:.4f} — ✅ TP1: зафиксируй 50% (прибыль {change:.1f}%). ℹ️ Достигнута первая цель.")
        elif change <= -SL_PCT:
            lines.append(f"• {sym}: {px:.4f} — 🛑 SL: закрой позицию (убыток {change:.1f}%). ℹ️ Защита капитала.")
        else:
            lines.append(f"• {sym}: {px:.4f} — ⏳ Держать. Δ={change:.1f}%. ℹ️ Сигналов на фиксацию нет.")
    return "\n".join(lines)

@dp.message_handler(commands=["advice"])
async def cmd_advice(m: types.Message):
    if not ensure(m): return
    await m.answer(build_advice_text())

# ========= Кнопки фиксации по сигналу сканера =========
@dp.callback_query_handler(lambda c: c.data.startswith("fix|"))
async def on_fix(c: types.CallbackQuery):
    if ALLOWED_ID and c.from_user.id != ALLOWED_ID:
        return await c.answer("Нет доступа", show_alert=True)
    _, sym, frac = c.data.split("|")
    frac = float(frac)
    pos = HOLD.get(sym)
    if not pos or pos["qty"]<=0:
        return await c.message.answer("Позиция не найдена.")
    sell_qty = round(pos["qty"]*frac, 6)
    pos["qty"] -= sell_qty
    if pos["qty"]<=0: HOLD.pop(sym, None)
    await c.message.edit_reply_markup()
    await c.message.answer(f"💰 Зафиксировано: {sym} {sell_qty}\nОстаток: {pos.get('qty', 0.0)}")

# ========= Фон: сканер (TP1/TP2/SL) каждые N секунд =========
async def scanner_loop():
    last_state = {}  # sym -> {'state':'TP1/TP2/SL/HOLD', 'ts': ...}
    while True:
        try:
            for sym, pos in list(HOLD.items()):
                if pos["qty"]<=0 or pos["avg"]<=0: continue
                px = binance_price(sym); 
                if px is None: continue
                change = (px/pos["avg"]-1)*100
                state="HOLD"; text=None; kb=None
                if change >= TP2_PCT:
                    state="TP2"
                    text = f"🎯 {sym}: {px:.4f}. Достигнут TP2 {TP2_PCT:.1f}% — зафиксируй 80%."
                    kb = InlineKeyboardMarkup().add(
                        InlineKeyboardButton("✅ Зафиксировать 80%", callback_data=f"fix|{sym}|0.8"))
                elif change >= TP1_PCT:
                    state="TP1"
                    text = f"🎯 {sym}: {px:.4f}. Достигнут TP1 {TP1_PCT:.1f}% — зафиксируй 50%."
                    kb = InlineKeyboardMarkup().add(
                        InlineKeyboardButton("✅ Зафиксировать 50%", callback_data=f"fix|{sym}|0.5"))
                elif change <= -SL_PCT:
                    state="SL"
                    text = f"🛑 {sym}: {px:.4f}. Стоп {SL_PCT:.1f}% — продай всё для защиты."
                    kb = InlineKeyboardMarkup().add(
                        InlineKeyboardButton("❗ Продать всё", callback_data=f"fix|{sym}|1.0"))
                # отправляем только при смене состояния
                prev = last_state.get(sym, {}).get("state")
                if text and state != prev and ALLOWED_ID:
                    try:
                        await bot.send_message(ALLOWED_ID, text, reply_markup=kb)
                    except Exception:
                        pass
                    last_state[sym] = {"state": state, "ts": time.time()}
            await asyncio.sleep(max(15, SCAN_INTERVAL))
        except Exception:
            await asyncio.sleep(max(15, SCAN_INTERVAL))

# ========= Фон: автодайджесты в заданные часы =========
async def daily_brief_loop():
    sent_today = set()  # "HH:MM"
    current_date = datetime.now().date()
    while True:
        try:
            now = datetime.now()
            if now.date() != current_date:
                sent_today.clear()
                current_date = now.date()
            hhmm = now.strftime("%H:%M")
            if ALLOWED_ID and hhmm in NEWS_TIMES and hhmm not in sent_today:
                btc = binance_price("BTCUSDT"); eth = binance_price("ETHUSDT")
                lines = [
                    f"🗓️ Дайджест {now.strftime('%d.%m %H:%M')} ({TZ})",
                    f"📊 BTC: <code>{btc}</code> • ETH: <code>{eth}</code>",
                    "📰 Новости:",
                ]
                news = fetch_news(3)
                lines += [f"• {t}" for t in news] if news else ["• (новости недоступны)"]
                lines.append("")
                lines.append(build_advice_text())
                lines.append("\n➡️ Для детального совета: /advice")
                try:
                    await bot.send_message(ALLOWED_ID, "\n".join(lines))
                except Exception:
                    pass
                sent_today.add(hhmm)
            await asyncio.sleep(20)
        except Exception:
            await asyncio.sleep(20)

# ========= Запуск =========
async def on_startup(_):
    # запускаем 2 фоновые задачи
    asyncio.create_task(scanner_loop())
    asyncio.create_task(daily_brief_loop())

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
