import os, json, time, hmac, hashlib, asyncio, re, requests
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ----------------- ENV -----------------
TG_TOKEN       = (os.getenv("TELEGRAM_TOKEN") or os.getenv("TG_TOKEN") or "").strip()
ALLOWED_ID     = int(os.getenv("ALLOWED_USER_ID", "0"))
TZ             = os.getenv("TZ", "Asia/Ho_Chi_Minh")
WATCHLIST      = os.getenv("WATCHLIST", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,AVAXUSDT,INJUSDT,SUIUSDT,OPUSDT,NEARUSDT,LINKUSDT,MATICUSDT,SEIUSDT").replace(" ", "")
SCAN_INTERVAL  = int(os.getenv("SCAN_INTERVAL", "5"))  # мин
TP1_PCT        = float(os.getenv("TP1_PCT", "3"))
TP2_PCT        = float(os.getenv("TP2_PCT", "6"))
SL_PCT         = float(os.getenv("SL_PCT", "2"))
PAPER_MODE     = os.getenv("PAPER_MODE", "true").lower() == "true"
SCHEDULE_TIMES = os.getenv("SCHEDULE_TIMES", "09:00,19:00,21:00,23:00")

if not TG_TOKEN or ":" not in TG_TOKEN:
    raise RuntimeError(f"Bad TELEGRAM_TOKEN: len={len(TG_TOKEN)}")

bot = Bot(token=TG_TOKEN, parse_mode="HTML")
dp  = Dispatcher(bot)
scheduler = AsyncIOScheduler(timezone=TZ)

# ----------------- PERSISTENCE -----------------
STATE_FILE = "/app/holdings.json"

def _now_ts(): return int(time.time()*1000)

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"holdings":{}}   # {"SOLUSDT":{"qty":10.0,"avg":55.0}}

def save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

state = load_state()

# ----------------- MARKET HELPERS -----------------
BINANCE = "https://api.binance.com"

def http_get(url, params=None, timeout=12):
    headers={"User-Agent":"Mozilla/5.0"}
    return requests.get(url, params=params, timeout=timeout, headers=headers)

def price(symbol):
    r = http_get(BINANCE + "/api/v3/ticker/price", {"symbol": symbol})
    r.raise_for_status()
    return float(r.json()["price"])

def klines(symbol, interval="1h", limit=60):
    r = http_get(BINANCE + "/api/v3/klines", {"symbol":symbol, "interval":interval, "limit":limit})
    r.raise_for_status()
    return r.json()

def sma(values, n):
    if len(values) < n: return None
    return sum(values[-n:]) / n

def basic_signal(symbol):
    """
    Простейший анализ: SMA20/50 + импульс последней свечи + общий тренд.
    Возвращает (action, reason, tp1, tp2, sl)
    """
    try:
        ks = klines(symbol, "1h", 80)
        closes = [float(k[4]) for k in ks]
        c = closes[-1]
        s20 = sma(closes, 20)
        s50 = sma(closes, 50)
        body = (float(ks[-1][4]) - float(ks[-1][1])) / float(ks[-1][1]) * 100  # % тела свечи

        action = "HOLD"
        reason = []
        if s20 and s50:
            if s20 > s50 and body > 0.4:
                action = "BUY"
                reason.append("SMA20>SMA50, бычий импульс")
            elif s20 < s50 and body < -0.4:
                action = "SELL"
                reason.append("SMA20<SMA50, медвежий импульс")
            else:
                reason.append("Сигнал слабый — боковик/без импульса")

        tp1 = round(c * (1 + TP1_PCT/100), 6)
        tp2 = round(c * (1 + TP2_PCT/100), 6)
        sl  = round(c * (1 - SL_PCT/100), 6)
        if action == "SELL":
            # при продаже TP/SL информативны как цели обратной позиции
            pass
        return action, "; ".join(reason), c, tp1, tp2, sl
    except Exception as e:
        return "HOLD", f"Нет данных ({e})", None, None, None, None

# ----------------- UI BUILDERS -----------------
def act_kb(symbol, action, tp1, tp2, sl):
    data_prefix = f"{symbol}|{action}|{tp1 or 0}|{tp2 or 0}|{sl or 0}"
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(
        InlineKeyboardButton("✅ Зафиксировать", callback_data=f"dec|SELL|{data_prefix}"),
        InlineKeyboardButton("📈 Докупить",       callback_data=f"dec|BUY|{data_prefix}"),
        InlineKeyboardButton("⏳ Держать",        callback_data=f"dec|HOLD|{data_prefix}")
    )
    return kb

def ensure_access(m: types.Message) -> bool:
    return ALLOWED_ID == 0 or m.from_user.id == ALLOWED_ID

# ----------------- COMMANDS -----------------
@dp.message_handler(commands=["start","help"])
async def cmd_start(m: types.Message):
    if not ensure_access(m): return await m.answer("⛔️ Нет доступа.")
    txt = (
        "🤖 Я онлайн. PAPER_MODE=<b>{}</b>\n\n"
        "Команды:\n"
        "• /news — новости 📰\n"
        "• /market — цены BTC/ETH 📊\n"
        "• /status — статус сканера 🔧\n"
        "• /advice <SYMBOL> — совет по монете (пример: <code>/advice SOL</code>) 💡\n"
        "• /hold add SOL 10 @ 55 — добавить позицию ✚\n"
        "• /hold rm  SOL 5  — списать (продажа) ➖\n"
        "• /hold report — отчёт по портфелю 📒\n"
    ).format("ON" if PAPER_MODE else "OFF")
    await m.answer(txt)

@dp.message_handler(commands=["status"])
async def cmd_status(m: types.Message):
    if not ensure_access(m): return
    txt = (
        "🛠️ <b>Статус сканера</b>\n"
        f"Watchlist: {WATCHLIST}\n"
        f"Интервал: каждые {SCAN_INTERVAL} мин\n"
        f"Цели: TP1={TP1_PCT:.1f}%, TP2={TP2_PCT:.1f}%, SL={-SL_PCT:.1f}%"
    )
    await m.answer(txt)

# ---- NEWS (очень кратко, без внешних библиотек RSS) ----
FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.investing.com/rss/market_overview.rss"
]
def fetch_news(limit=6):
    items=[]
    for u in FEEDS:
        try:
            r=http_get(u, timeout=10)
            if r.status_code!=200: continue
            t=re.findall(r"<title>(.*?)</title>", r.text, re.I|re.S)
            for s in t[1:12]:
                s=re.sub("<.*?>","",s).strip()
                if s and s not in items: items.append(s)
                if len(items)>=limit: break
        except: pass
        if len(items)>=limit: break
    return items

@dp.message_handler(commands=["news"])
async def cmd_news(m: types.Message):
    if not ensure_access(m): return
    items = fetch_news(6)
    if not items: return await m.answer("⚠️ Новости недоступны.")
    await m.answer("📰 <b>Новости</b>\n" + "\n".join([f"• {x}" for x in items]))

@dp.message_handler(commands=["market"])
async def cmd_market(m: types.Message):
    if not ensure_access(m): return
    try:
        btc = price("BTCUSDT")
        eth = price("ETHUSDT")
        await m.answer(f"📊 <b>Рынок</b>\nBTC: <code>{btc:.2f}</code>\nETH: <code>{eth:.2f}</code>")
    except Exception as e:
        await m.answer(f"⚠️ Не удалось получить цены: {e}")

# ---- HOLDING BOOK ----
HRE = re.compile(r"^/hold\s+(add|rm)\s+([A-Z]{2,10})\s+(\d+(?:\.\d+)?)\s*(?:@\s*(\d+(?:\.\d+)?))?$", re.I)

@dp.message_handler(commands=["hold"])
async def cmd_hold(m: types.Message):
    if not ensure_access(m): return
    t = m.get_args()
    if t.strip().lower() == "report":
        if not state["holdings"]:
            return await m.answer("📒 Портфель пуст.")
        lines=[]
        total=0.0
        for s,info in state["holdings"].items():
            qty=float(info["qty"]); avg=float(info["avg"])
            try: px=price(s); pnl=(px-avg)/avg*100
            except: px=None; pnl=None
            line=f"• {s}: {qty} @ {avg}"
            if px: line+=f" | now {px:.4f} ({pnl:+.2f}%)"
            lines.append(line); total += qty*avg
        return await m.answer("📒 <b>Отчёт по портфелю</b>\n"+"\n".join(lines))

    mt = HRE.match(m.text)
    if not mt:
        return await m.answer("❗ Формат:\n/hold add SOL 10 @ 55\n/hold rm  SOL 5\n/hold report")
    act, sym, qty, avg = mt.groups()
    qty=float(qty); sym=sym.upper()
    key = sym if sym.endswith("USDT") else sym+"USDT"
    pos = state["holdings"].get(key, {"qty":0.0, "avg":0.0})

    if act.lower()=="add":
        if avg:  # ручной ввод цены
            new_qty = pos["qty"]+qty
            new_avg = (pos["avg"]*pos["qty"] + float(avg)*qty)/new_qty if new_qty>0 else 0.0
        else:
            px = price(key)
            new_qty = pos["qty"]+qty
            new_avg = (pos["avg"]*pos["qty"] + px*qty)/new_qty if new_qty>0 else 0.0
        state["holdings"][key]={"qty":round(new_qty,6),"avg":round(new_avg,6)}
        save_state(state)
        return await m.answer(f"✚ Добавлено: {sym} {qty}\nТекущая позиция: {state['holdings'][key]}")
    else:
        # rm
        new_qty = max(pos["qty"]-qty, 0.0)
        pos["qty"]=round(new_qty,6)
        state["holdings"][key]=pos
        if new_qty==0: state["holdings"].pop(key, None)
        save_state(state)
        return await m.answer(f"➖ Списано: {sym} {qty}\nТекущая позиция: {state['holdings'].get(key,'закрыта')}")

# ---- ADVICE ----
@dp.message_handler(commands=["advice"])
async def cmd_advice(m: types.Message):
    if not ensure_access(m): return
    arg = m.get_args().strip().upper()
    if not arg: return await m.answer("Укажи символ: пример <code>/advice SOL</code>")
    symbol = arg if arg.endswith("USDT") else arg+"USDT"

    action, reason, last, tp1, tp2, sl = basic_signal(symbol)
    if not last:
        return await m.answer(f"⚠️ Нет данных по {symbol}.")
    emoji = {"BUY":"🟢","SELL":"🔴","HOLD":"🟡"}[action]
    txt = (
        f"{emoji} <b>Совет по {symbol}</b>\n"
        f"Цена: <code>{last:.4f}</code>\n"
        f"TP1: <code>{tp1:.4f}</code> (+{TP1_PCT:.1f}%)\n"
        f"TP2: <code>{tp2:.4f}</code> (+{TP2_PCT:.1f}%)\n"
        f"SL:  <code>{sl:.4f}</code> (−{SL_PCT:.1f}%)\n"
        f"Причина: {reason}\n\n"
        f"Выбери действие:"
    )
    await m.answer(txt, reply_markup=act_kb(symbol, action, tp1, tp2, sl))

@dp.callback_query_handler(lambda c: c.data.startswith("dec|"))
async def decide(c: types.CallbackQuery):
    if ALLOWED_ID != 0 and c.from_user.id != ALLOWED_ID:
        return await c.answer("Нет доступа", show_alert=True)
    _, user_choice, symbol, action, tp1, tp2, sl = c.data.split("|", 6)
    msg = {
        "BUY":  "📈 Докупить (ручная торговля).",
        "SELL": "✅ Зафиксировать часть/всю позицию (ручная торговля).",
        "HOLD": "⏳ Держать, без действий."
    }[user_choice]
    await c.message.edit_reply_markup()
    await c.message.answer(f"📝 Решение по {symbol}: <b>{user_choice}</b>\n{msg}\nPAPER_MODE={'ON' if PAPER_MODE else 'OFF'}")

# ----------------- SCHEDULER -----------------
def schedule_reports():
    # четыре отправки обзора в день из переменной, по умолчанию 09:00,19:00,21:00,23:00
    for t in [x.strip() for x in SCHEDULE_TIMES.split(",") if x.strip()]:
        h, m = t.split(":")
        scheduler.add_job(
            lambda: asyncio.create_task(send_daily_overview()),
            CronTrigger(hour=int(h), minute=int(m))
        )
    # сканер-напоминалка по watchlist
    scheduler.add_job(lambda: asyncio.create_task(scan_watchlist()), "interval", minutes=SCAN_INTERVAL)

async def send_daily_overview():
    if ALLOWED_ID == 0: return
    try:
        btc = price("BTCUSDT"); eth = price("ETHUSDT")
        items = fetch_news(4)
        txt = (
            f"🗓️ Обзор\nBTC: <code>{btc:.2f}</code> | ETH: <code>{eth:.2f}</code>\n\n"
            "📰 Топ‑заголовки:\n" + "\n".join([f"• {x}" for x in items]) +
            "\n\nЧтобы получить совет по монете — напиши /advice SOL"
        )
        await bot.send_message(ALLOWED_ID, txt)
    except Exception as e:
        await bot.send_message(ALLOWED_ID, f"⚠️ Ошибка обзора: {e}")

async def scan_watchlist():
    if ALLOWED_ID == 0: return
    symbols = [s.strip().upper() for s in WATCHLIST.split(",") if s.strip()]
    lines=[]
    for s in symbols[:12]:  # не спамим
        act, reason, last, tp1, tp2, sl = basic_signal(s)
        if not last: continue
        emoji = {"BUY":"🟢","SELL":"🔴","HOLD":"🟡"}[act]
        lines.append(f"{emoji} {s} {last:.4f} • {act}")
    if lines:
        txt = "🔎 <b>Сканер (быстрый срез)</b>\n" + "\n".join(lines) + "\n\nЗапроси детально: /advice SOL"
        await bot.send_message(ALLOWED_ID, txt)

# ----------------- RUN -----------------
if __name__ == "__main__":
    schedule_reports()
    scheduler.start()
    executor.start_polling(dp, skip_updates=True)
