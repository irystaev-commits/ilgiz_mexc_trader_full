import os, time, hmac, hashlib, asyncio, re, requests
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

# ========= ENV =========
TG_TOKEN = (os.getenv("TELEGRAM_TOKEN") or os.getenv("TG_TOKEN") or "").strip()
ALLOWED_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
PAPER = os.getenv("PAPER_MODE", "true").lower() == "true"

# Сканер
WATCHLIST = [s.strip().upper() for s in os.getenv(
    "WATCHLIST",
    "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,AVAXUSDT,INJUSDT,OPUSDT,NEARUSDT,LINKUSDT,MATICUSDT,SEIUSDT"
).split(",") if s.strip()]
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))   # сек (например 300 = 5 мин)
TP1_PCT = float(os.getenv("TP1_PCT", "3.0"))             # +% к цене покупки
TP2_PCT = float(os.getenv("TP2_PCT", "6.0"))             # +%
SL_PCT  = float(os.getenv("SL_PCT",  "2.0"))             # -%

# ====== Внутреннее «псевдо-портфолио» (ручной учёт) ======
# /hold add SOL 10 @ 55   — добавить позицию
# /hold rm  SOL 3         — уменьшить кол-во (фиксировать)
# /hold report            — отчёт
HOLD = {}  # {"SOLUSDT": {"qty": 10.0, "avg": 55.0}}

# ========= Checks =========
if not TG_TOKEN or ":" not in TG_TOKEN:
    raise RuntimeError(f"Bad TELEGRAM_TOKEN: len={len(TG_TOKEN)}")

bot = Bot(token=TG_TOKEN, parse_mode="HTML")
dp  = Dispatcher(bot)

# ========= Helpers =========
def ensure(m: types.Message) -> bool:
    return ALLOWED_ID == 0 or m.from_user.id == ALLOWED_ID

def binance_price(symbol: str):
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": symbol}, timeout=10)
        if r.status_code == 200:
            return float(r.json()["price"])
    except Exception:
        pass
    return None

def fmt_pct(x): 
    s = f"{x:.1f}%"
    return s.replace(".0%","%")

def short_reason(side:str):
    return "объём растёт, пробой сопротивления" if side=="BUY" else "слабый импульс/риски снижения"

# ====== Команды ======
@dp.message_handler(commands=["start","help"])
async def start(m: types.Message):
    if not ensure(m): return
    text = (
        "🤖 Я готов.\n"
        "Команды:\n"
        "• /status — статус сканера 🔧\n"
        "• /advice — быстрый совет по портфелю 💡\n"
        "• /hold add SOL 10 @ 55 — добавить позицию\n"
        "• /hold rm SOL 3 — списать (продажа)\n"
        "• /hold report — отчёт по портфелю\n"
        "• /news — новости (кратко)\n\n"
        f"PAPER_MODE={'ON' if PAPER else 'OFF'}"
    )
    await m.answer(text)

@dp.message_handler(commands=["status"])
async def status(m: types.Message):
    if not ensure(m): return
    wl = ",".join(WATCHLIST)
    text = (
        "🛠️ <b>Статус сканера</b>\n"
        f"Watchlist: {wl}\n"
        f"Интервал: каждые {SCAN_INTERVAL//60} мин\n"
        f"Цели: TP1={fmt_pct(TP1_PCT)}, TP2={fmt_pct(TP2_PCT)}, SL=-{fmt_pct(SL_PCT)}"
    )
    await m.answer(text)

# ---- Псевдо‑портфолио ----
POS_RE = re.compile(r"^/hold\s+(add|rm)\s+([A-Za-z]{2,10})\s+(\d+(?:\.\d+)?)\s*(?:@\s*(\d+(?:\.\d+)?))?$")

@dp.message_handler(commands=["hold"])
async def hold_cmd(m: types.Message):
    if not ensure(m): return
    t = m.text.strip()
    if t == "/hold" or t.endswith("report"):
        return await hold_report(m)

    mt = POS_RE.match(t)
    if not mt:
        return await m.answer("Формат:\n/hold add SOL 10 @ 55\n/hold rm SOL 3\n/hold report")
    action, sym, qty, px = mt.groups()
    sym = sym.upper()
    if not sym.endswith("USDT"): sym += "USDT"
    qty = float(qty)
    if action == "add":
        if not px:
            live = binance_price(sym)
            if live is None: return await m.answer("Не смог получить цену.")
            px = live
        else:
            px = float(px)
        pos = HOLD.setdefault(sym, {"qty":0.0, "avg":0.0})
        new_qty = pos["qty"] + qty
        pos["avg"] = (pos["avg"]*pos["qty"] + px*qty)/new_qty if new_qty>0 else 0.0
        pos["qty"] = new_qty
        await m.answer(f"➕ Добавлено: <b>{sym}</b> {qty} @ {px}\nТекущая позиция: {pos}")
    else:
        pos = HOLD.get(sym)
        if not pos or pos["qty"]<=0:
            return await m.answer("Позиция не найдена.")
        sell_qty = min(qty, pos["qty"])
        pos["qty"] -= sell_qty
        await m.answer(f"➖ Списано: <b>{sym}</b> {sell_qty}\nТекущая позиция: {pos}")

@dp.message_handler(commands=["advice"])
async def advice(m: types.Message):
    if not ensure(m): return
    if not HOLD:
        return await m.answer("Пока позиций нет. Добавь: /hold add SOL 10 @ 55")
    lines = ["💡 <b>Совет по портфелю</b>"]
    for sym, pos in HOLD.items():
        live = binance_price(sym)
        if live is None: 
            lines.append(f"• {sym}: цена недоступна")
            continue
        change = (live/pos["avg"]-1)*100 if pos["avg"]>0 else 0
        if change >= TP2_PCT:
            lines.append(f"• {sym}: {live:.4f} — ✅ TP2: зафиксируй 80% (прибыль {change:.1f}%). Причина: сильный тренд, {short_reason('SELL')}.")
        elif change >= TP1_PCT:
            lines.append(f"• {sym}: {live:.4f} — ✅ TP1: зафиксируй 50% (прибыль {change:.1f}%). Причина: достижение первой цели.")
        elif change <= -SL_PCT:
            lines.append(f"• {sym}: {live:.4f} — 🛑 SL: продай всё (убыток {change:.1f}%). Причина: защита капитала.")
        else:
            lines.append(f"• {sym}: {live:.4f} — ⏳ Держать. Δ={change:.1f}%")

    await m.answer("\n".join(lines))

@dp.message_handler(commands=["news"])
async def news(m: types.Message):
    if not ensure(m): return
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=8)
        btc = float(r.json().get("price", 0))
        r2 = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT", timeout=8)
        eth = float(r2.json().get("price", 0))
        await m.answer(f"📰 <b>Коротко</b>\nBTC: <code>{btc}</code>\nETH: <code>{eth}</code>")
    except Exception:
        await m.answer("Новости/цены сейчас недоступны.")

# ========= Автосканер сигналов =========
async def scanner_loop():
    await bot.wait_until_ready() if hasattr(bot, "wait_until_ready") else asyncio.sleep(0)
    # Храним «последний совет» чтобы не спамить
    last_state = {}  # sym -> state: 'TP2','TP1','SL','HOLD'
    while True:
        try:
            for sym in WATCHLIST:
                live = binance_price(sym)
                if live is None: continue
                # Если есть позиция — совет по ней; если нет — просто мониторинг пробоя
                pos = HOLD.get(sym)
                state = "HOLD"
                explain = ""
                kb=None
                if pos and pos["qty"]>0 and pos["avg"]>0:
                    change = (live/pos["avg"]-1)*100
                    if change >= TP2_PCT:
                        state="TP2"; explain=f"🎯 {sym}: цена {live:.4f}. TP2 {fmt_pct(TP2_PCT)} достигнут — зафиксируй 80%. Причина: сильный импульс."
                        kb = InlineKeyboardMarkup().add(
                            InlineKeyboardButton("✅ Зафиксировать 80%", callback_data=f"fix|{sym}|0.8"))
                    elif change >= TP1_PCT:
                        state="TP1"; explain=f"🎯 {sym}: цена {live:.4f}. TP1 {fmt_pct(TP1_PCT)} — зафиксируй 50%."
                        kb = InlineKeyboardMarkup().add(
                            InlineKeyboardButton("✅ Зафиксировать 50%", callback_data=f"fix|{sym}|0.5"))
                    elif change <= -SL_PCT:
                        state="SL"; explain=f"🛑 {sym}: цена {live:.4f}. Стоп {fmt_pct(SL_PCT)} — продай всё для защиты."
                        kb = InlineKeyboardMarkup().add(
                            InlineKeyboardButton("❗ Продать всё", callback_data=f"fix|{sym}|1.0"))
                    else:
                        state="HOLD"

                # шлём только при смене состояния
                if state != last_state.get(sym) and state != "HOLD" and ALLOWED_ID:
                    try:
                        await bot.send_message(ALLOWED_ID, explain, reply_markup=kb)
                    except Exception:
                        pass
                    last_state[sym]=state
            await asyncio.sleep(SCAN_INTERVAL)
        except Exception:
            await asyncio.sleep(SCAN_INTERVAL)

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
    await c.message.edit_reply_markup()
    await c.message.answer(f"💰 Зафиксировано: {sym} {sell_qty}\nОстаток: {pos['qty']}")

async def on_startup(_):
    # запускаем фоновый сканер
    asyncio.create_task(scanner_loop())

# ====== Entry ======
if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
