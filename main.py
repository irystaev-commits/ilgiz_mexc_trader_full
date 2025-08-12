import os, time, hmac, hashlib, requests, re, asyncio, json
from pathlib import Path
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ========= ENV =========
TG_TOKEN   = (os.getenv("TELEGRAM_TOKEN") or os.getenv("TG_TOKEN") or "").strip()
ALLOWED_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
API_KEY    = os.getenv("MEXC_API_KEY", "").strip()
SECRET     = os.getenv("MEXC_SECRET_KEY", "").strip()
PAPER      = os.getenv("PAPER_MODE", "true").lower() == "true"
MAX_USDT   = float(os.getenv("MAX_ORDER_USDT", "300"))
TZ         = os.getenv("TZ", "Asia/Ho_Chi_Minh")
BASE       = "https://api.mexc.com"

# Сканер сигналов (аналитика 24/7)
WATCHLIST      = [s.strip().upper() for s in os.getenv("WATCHLIST", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,AVAXUSDT,INJUSDT,SUIUSDT,OPUSDT,NEARUSDT,LINKUSDT,MATICUSDT,SEIUSDT").split(",") if s.strip()]
SCAN_INTERVAL  = int(os.getenv("SCAN_INTERVAL", "300"))     # сек (по умолчанию 5 мин)
TP1_PCT        = float(os.getenv("TP1_PCT", "0.03"))        # +3%
TP2_PCT        = float(os.getenv("TP2_PCT", "0.06"))        # +6%
SL_PCT         = float(os.getenv("SL_PCT",  "0.02"))        # -2%

if not TG_TOKEN or ":" not in TG_TOKEN:
    raise RuntimeError(f"Bad TELEGRAM_TOKEN: len={len(TG_TOKEN)}")

bot = Bot(token=TG_TOKEN, parse_mode="HTML")
dp  = Dispatcher(bot)
scheduler = AsyncIOScheduler(timezone=TZ)

# ========= UTILS =========
def ts() -> int: return int(time.time()*1000)
def sign(query: str) -> str: return hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

def mexc(method, path, params=None, signed=False):
    headers = {"X-MEXC-APIKEY": API_KEY}
    params = params or {}
    if signed:
        if not API_KEY or not SECRET:
            raise RuntimeError("MEXC API keys not set")
        params["timestamp"] = ts()
        params["recvWindow"] = 50000
        q = "&".join([f"{k}={params[k]}" for k in sorted(params)])
        params["signature"] = sign(q)
    if method == "GET":
        r = requests.get(BASE + path, params=params, headers=headers, timeout=20)
    else:
        r = requests.post(BASE + path, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

def fetch_url(url, timeout=15):
    try:
        return requests.get(url, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
    except Exception:
        return None

def binance_price(sym):
    u = f"https://api.binance.com/api/v3/ticker/price?symbol={sym}"
    r = fetch_url(u, 10)
    if not r or r.status_code != 200: return None
    try: return float(r.json().get("price", 0))
    except: return None

def price(symbol: str) -> float:
    data = mexc("GET", "/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"])

def pair(sym: str) -> str:
    sym = sym.upper()
    if not sym.endswith("USDT"): sym += "USDT"
    return sym

# ========= NEWS =========
NEWS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.investing.com/rss/market_overview.rss"
]
def fetch_news(limit=6):
    items = []
    import re as _re
    for feed in NEWS:
        r = fetch_url(feed, 15)
        if not r or r.status_code != 200: continue
        titles = _re.findall(r"<title>(.*?)</title>", r.text, _re.I|_re.S)
        for t in titles[1:10]:
            t = _re.sub("<.*?>", "", t).strip()
            if t and t not in items: items.append(t)
            if len(items) >= limit: break
        if len(items) >= limit: break
    return items

# ========= ORDERS (spot) =========
def place_spot_order(symbol, side, qty=None, quote_usdt=None, order_type="MARKET", limit_price=None):
    if quote_usdt is not None and qty is None:
        px = price(symbol)
        qty = max(round(float(quote_usdt)/px, 6), 0.000001)
    payload = {"symbol": symbol, "side": side, "type": order_type, "quantity": qty}
    if order_type == "LIMIT":
        payload["price"] = f"{float(limit_price):.8f}"
        payload["timeInForce"] = "GTC"
    if PAPER:
        return {"paper": True, "order": payload}
    return mexc("POST", "/api/v3/order", payload, signed=True)

def place_tp_limit(symbol, qty, tp_px):
    payload = {"symbol": symbol, "side": "SELL", "type": "LIMIT",
               "timeInForce": "GTC", "quantity": qty, "price": f"{tp_px:.8f}"}
    if PAPER: return {"paper": True, "tp": payload}
    return mexc("POST", "/api/v3/order", payload, signed=True)

def place_sl_stoplimit(symbol, qty, stop_px, lim_px):
    payload = {"symbol":symbol,"side":"SELL","type":"STOP_LOSS_LIMIT","timeInForce":"GTC",
               "quantity":qty,"stopPrice":f"{stop_px:.8f}","price":f"{lim_px:.8f}"}
    if PAPER: return {"paper": True, "sl": payload}
    return mexc("POST","/api/v3/order",payload, signed=True)

# ========= MANUAL PORTFOLIO (без API) =========
# На Railway лучше хранить на Volume: поменяй путь на "/app/data/portfolio.json", если подключишь volume.
PORTF_PATH = Path("portfolio.json")

def load_portfolio():
    if PORTF_PATH.exists():
        try: return json.loads(PORTF_PATH.read_text())
        except: return {}
    return {}

def save_portfolio(p): 
    try: PORTF_PATH.write_text(json.dumps(p, ensure_ascii=False, indent=2))
    except Exception as e: print("save_portfolio fail:", e)

portfolio = load_portfolio()  # {"SOLUSDT":{"qty":12.5,"avg":56.2}, ...}

def add_hold(symbol: str, qty: float, price_in: float):
    s = pair(symbol)
    cur = portfolio.get(s, {"qty":0.0, "avg":0.0})
    new_qty = cur["qty"] + qty
    if new_qty <= 0:
        portfolio.pop(s, None)
    else:
        new_avg = (cur["qty"]*cur["avg"] + qty*price_in) / new_qty if cur["qty"]>0 else price_in
        portfolio[s] = {"qty": round(new_qty, 8), "avg": float(new_avg)}
    save_portfolio(portfolio); 
    return portfolio.get(s)

def remove_hold(symbol: str, qty: float):
    s = pair(symbol)
    cur = portfolio.get(s)
    if not cur: return None
    left = round(cur["qty"] - qty, 8)
    if left <= 0: portfolio.pop(s, None)
    else: portfolio[s]["qty"] = left
    save_portfolio(portfolio); 
    return portfolio.get(s)

def holding_report():
    lines, total_cost, total_now = [], 0.0, 0.0
    for s, pos in portfolio.items():
        qty, avg = pos["qty"], pos["avg"]
        px = binance_price(s)
        if px is None:
            lines.append(f"{s}: {qty} @ {avg} — цена n/a"); 
            continue
        cost, now = qty*avg, qty*px
        pnl = ((px-avg)/avg)*100 if avg>0 else 0
        total_cost += cost; total_now += now
        lines.append(f"{s}: {qty} @ {avg:.4f} → {px:.4f}  PnL: {pnl:+.2f}%  (вал:{now:.2f} USDT)")
    d = total_now - total_cost
    tot = f"\nИтого портфель: {total_now:.2f} USDT  (PnL: {d:+.2f} USDT, {((total_now/total_cost-1)*100 if total_cost>0 else 0):+.2f}%)"
    return "\n".join(lines) + (tot if lines else "Пусто")

def advice_for_position(symbol: str):
    s = pair(symbol)
    pos = portfolio.get(s)
    if not pos: return None, "позиции нет"
    px = binance_price(s)
    if px is None: return None, "нет цены"
    avg, qty = pos["avg"], pos["qty"]
    pnl_pct = ((px-avg)/avg)*100 if avg>0 else 0
    if pnl_pct >= 12: note = f"PnL {pnl_pct:.1f}% — зафиксируй 30–50%, остальное держи по тренду."
    elif pnl_pct >= 4: note = f"PnL {pnl_pct:.1f}% — держи; можно зафиксировать 20–30%, стоп в безубыток."
    elif pnl_pct > -3: note = f"PnL {pnl_pct:.1f}% — нейтрально; держи без фиксации."
    else: note = f"PnL {pnl_pct:.1f}% — просадка; держи/докупай по плану, стоп под лоу."
    return {"symbol": s, "qty": qty, "avg": avg, "px": px, "pnl_pct": pnl_pct, "advice": note}, None

# ========= SIGNALS SCANNER =========
def get_binance_klines(symbol: str, interval="1h", limit=120):
    u = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    r = fetch_url(u, 12)
    if not r or r.status_code != 200: return []
    return r.json()

def sma(vals, n):
    if len(vals) < n: return None
    return sum(vals[-n:]) / n

def rsi(values, period=14):
    if len(values) <= period: return None
    gains, losses = 0.0, 0.0
    for i in range(1, period+1):
        ch = values[i] - values[i-1]
        gains += max(ch, 0); losses += max(-ch, 0)
    avg_gain, avg_loss = gains/period, losses/period
    for i in range(period+1, len(values)):
        ch = values[i] - values[i-1]
        gain, loss = max(ch, 0.0), max(-ch, 0.0)
        avg_gain = (avg_gain*(period-1) + gain)/period
        avg_loss = (avg_loss*(period-1) + loss)/period
    if avg_loss == 0: return 100.0
    rs = avg_gain/avg_loss
    return 100 - (100/(1+rs))

def analyze_symbol(symbol: str):
    kl = get_binance_klines(symbol, "1h", 120)
    if len(kl) < 60: return {"symbol": symbol, "ok": False, "why": "мало данных"}
    closes = [float(k[4]) for k in kl]
    price_now = closes[-1]
    s20_prev, s50_prev = sma(closes[:-1], 20), sma(closes[:-1], 50)
    s20, s50         = sma(closes, 20), sma(closes, 50)
    rsi_now, rsi_prev= rsi(closes, 14), rsi(closes[:-1], 14)
    if None in (s20, s50, s20_prev, s50_prev, rsi_now, rsi_prev):
        return {"symbol": symbol, "ok": False, "why": "нет индикаторов"}
    cross_up = (s20_prev <= s50_prev and s20 > s50)
    trend_up = s20 > s50
    rsi_rising = rsi_now > rsi_prev if rsi_prev is not None else False
    rsi_ok = 50 <= rsi_now <= 70
    if (cross_up or (trend_up and rsi_rising)) and rsi_ok:
        tp1 = price_now * (1 + TP1_PCT)
        tp2 = price_now * (1 + TP2_PCT)
        sl  = price_now * (1 - SL_PCT)
        reason = []
        if cross_up: reason.append("пересечение SMA20↑SMA50")
        if trend_up: reason.append("SMA20>SMA50 (тренд ↑)")
        if rsi_rising: reason.append(f"RSI растёт ({rsi_now:.1f})")
        return {"symbol": symbol, "ok": True, "action": "BUY", "price": price_now,
                "tp1": tp1, "tp2": tp2, "sl": sl,
                "tp1_pct": TP1_PCT*100, "tp2_pct": TP2_PCT*100, "sl_pct": SL_PCT*100,
                "why": "; ".join(reason) or "сигнал по тренду"}
    if s20 < s50 and rsi_now < 45:
        return {"symbol": symbol, "ok": True, "action": "EXIT", "price": price_now,
                "why": f"SMA20<SMA50 и RSI {rsi_now:.1f}<45 (слабость)"}
    return {"symbol": symbol, "ok": True, "action": "HOLD", "price": price_now, "why": "сигнала нет"}

_last_alert_at = {}  # (symbol, action) -> ts

async def scan_and_alert():
    if not ALLOWED_ID: return
    now = time.time()
    min_realert_sec = 60*60*2
    for sym in WATCHLIST:
        try:
            sig = analyze_symbol(sym)
        except Exception as e:
            print("scan error", sym, e); continue
        if not sig.get("ok"): continue
        act = sig.get("action")
        if act in ("BUY", "EXIT"):
            last = _last_alert_at.get((sym, act), 0)
            if now - last < min_realert_sec: continue
            _last_alert_at[(sym, act)] = now
            if act == "BUY":
                msg = (f"📣 <b>Сигнал</b> {sym}\n"
                       f"Вход: <b>{sig['price']:.4f}</b>\n"
                       f"TP1: <b>{sig['tp1']:.4f}</b> (+{sig['tp1_pct']:.1f}%)\n"
                       f"TP2: <b>{sig['tp2']:.4f}</b> (+{sig['tp2_pct']:.1f}%)\n"
                       f"SL: <b>{sig['sl']:.4f}</b> (−{sig['sl_pct']:.1f}%)\n"
                       f"Причина: {sig['why']}")
            else:
                msg = (f"📉 <b>Сигнал фиксации</b> {sym}\n"
                       f"Цена: <b>{sig['price']:.4f}</b>\n"
                       f"Причина: {sig['why']}\n"
                       f"💡 Идея: зафиксировать часть/всё вручную.")
            try: await bot.send_message(ALLOWED_ID, msg)
            except Exception as e: print("send fail:", e)

# ========= ACCESS =========
def ensure_access(m: types.Message) -> bool:
    return m.from_user.id == ALLOWED_ID or ALLOWED_ID == 0

# ========= HANDLERS =========
@dp.message_handler(commands=["start","help"])
async def start(m: types.Message):
    if not ensure_access(m): return await m.answer("⛔️ Нет доступа.")
    txt = ("🤖 Я готов.\n"
           "Команды:\n"
           "• /news — новости 📰\n"
           "• /market — цены BTC/ETH 📊\n"
           "• /status — статус сканера\n"
           "• /hold add SOL 12.5 @ 56.2 — добавить позицию\n"
           "• /hold rm SOL 5 — списать (продажа)\n"
           "• /hold report — отчёт по портфелю\n"
           "• /advice SOL — совет: зафиксировать/держать\n"
           "• /signal BUY SOL 25 @MKT TP=212 SL=188\n"
           f"PAPER_MODE={'ON' if PAPER else 'OFF'}")
    await m.answer(txt)

@dp.message_handler(commands=["news"])
async def news(m: types.Message):
    if not ensure_access(m): return
    items = fetch_news(6)
    if not items: return await m.answer("⚠️ Новости недоступны.")
    await m.answer("📰 <b>Новости</b>\n" + "\n".join([f"• {t}" for t in items]))

@dp.message_handler(commands=["market"])
async def market(m: types.Message):
    if not ensure_access(m): return
    btc = binance_price("BTCUSDT"); eth = binance_price("ETHUSDT")
    await m.answer(f"📊 <b>Рынок</b>\nBTC: <code>{btc}</code>\nETH: <code>{eth}</code>")

@dp.message_handler(commands=["status"])
async def status_cmd(m: types.Message):
    if not ensure_access(m): return
    mins = max(1, SCAN_INTERVAL // 60)
    await m.answer(
        "🛠️ <b>Статус сканера</b>\n"
        f"Watchlist: {', '.join(WATCHLIST)}\n"
        f"Интервал: каждые {mins} мин\n"
        f"Цели: TP1=+{TP1_PCT*100:.1f}%, TP2=+{TP2_PCT*100:.1f}%, SL=−{SL_PCT*100:.1f}%"
    )

@dp.message_handler(commands=["hold"])
async def hold_cmd(m: types.Message):
    if not ensure_access(m): return
    t = m.text.strip()
    try:
        parts = t.split()
        if len(parts) == 1 or parts[1].lower() == "report":
            rep = holding_report()
            return await m.answer("📒 <b>Портфель</b>\n" + rep)
        action = parts[1].lower()
        sym = parts[2].upper()
        if action == "add":
            qty = float(parts[3])
            if parts[4] != "@": raise ValueError
            price_in = float(parts[5])
            pos = add_hold(sym, qty, price_in)
            return await m.answer(f"➕ Добавлено: {sym} {qty} @ {price_in}\nТекущая позиция: {pos}")
        elif action in ("rm","remove","sell"):
            qty = float(parts[3])
            pos = remove_hold(sym, qty)
            return await m.answer(f"➖ Списано: {sym} {qty}\nОстаток: {pos if pos else 'нет'}")
        else:
            return await m.answer("Формат:\n/hold add SOL 12.5 @ 56.2\n/hold rm SOL 5\n/hold report")
    except Exception:
        return await m.answer("Формат:\n/hold add SOL 12.5 @ 56.2\n/hold rm SOL 5\n/hold report")

@dp.message_handler(commands=["advice"])
async def advice_cmd(m: types.Message):
    if not ensure_access(m): return
    parts = m.text.split()
    if len(parts) < 2:
        return await m.answer("Формат: /advice SOL  (для всех: /hold report)")
    sym = parts[1]
    data, err = advice_for_position(sym)
    if err: return await m.answer(f"⚠️ {err}")
    s = data["symbol"]; px=data["px"]; avg=data["avg"]; qty=data["qty"]; pnl=data["pnl_pct"]; note=data["advice"]
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("✅ Зафиксировать 25%", callback_data=f"fx|{s}|25"),
        InlineKeyboardButton("✅ Зафиксировать 50%", callback_data=f"fx|{s}|50")
    ).add(
        InlineKeyboardButton("🔒 Стоп = безубыток", callback_data=f"slbe|{s}|{avg:.6f}"),
        InlineKeyboardButton("⏸ Держать", callback_data=f"hold|{s}")
    )
    msg = (f"🧭 <b>Совет по {s}</b>\n"
           f"Кол-во: {qty}\nСредняя: {avg:.4f}\nЦена: {px:.4f}\nPnL: {pnl:+.2f}%\n"
           f"Рекомендация: {note}\n\nЧто делаем?")
    await m.answer(msg, reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith(("fx|","slbe|","hold|")))
async def cb_advice(c: types.CallbackQuery):
    if c.from_user.id != ALLOWED_ID and ALLOWED_ID != 0:
        return await c.answer("Нет доступа", show_alert=True)
    kind, s, arg = (c.data.split("|")+[""])[:3]
    await c.message.edit_reply_markup()
    if kind == "fx":
        pct = int(arg)
        pos = portfolio.get(s)
        if not pos: return await c.message.answer("Позиции нет.")
        fix_qty = round(pos["qty"] * (pct/100.0), 8)
        if fix_qty <= 0: return await c.message.answer("Нечего фиксировать.")
        await c.message.answer(f"✅ Совет: зафиксируй {pct}% ({fix_qty} {s.replace('USDT','')}). "
                               f"После продажи введи:\n/hold rm {s.replace('USDT','')} {fix_qty}")
    elif kind == "slbe":
        be = float(arg)
        await c.message.answer(f"🔒 Совет: перенести стоп в безубыток ~ {be:.4f}. (Поставь вручную на бирже)")
    else:
        await c.message.answer("⏸ Ок, держим без изменений.")

# ========= SIGNAL (ручной триггер с подтверждением) =========
SIG_RE = re.compile(
 r"^/signal\s+(BUY|SELL)\s+([A-Z]{2,10})\s+(\d+(?:\.\d+)?)\s+@(?:(MKT)|LIM=(\d+(?:\.\d+)?))\s+TP=(\d+(?:\.\d+)?)\s+SL=(\d+(?:\.\d+)?)\s*(?:\nR:\s*(.+))?$",
 re.IGNORECASE
)

@dp.message_handler(commands=["signal"])
async def signal_cmd(m: types.Message):
    if not ensure_access(m): return
    t = m.text.strip()
    mt = SIG_RE.match(t)
    if not mt:
        return await m.answer("❗ Формат:\n/signal BUY SOL 25 @MKT TP=212 SL=188\nR: причина")
    side, sym, usdt, mkt, lim, tp, sl, reason = mt.groups()
    usdt = float(usdt); tp=float(tp); sl=float(sl)
    if usdt > MAX_USDT: return await m.answer(f"❗ {usdt}USDT > лимита {MAX_USDT}USDT.")
    order_type = "MARKET" if mkt else "LIMIT"
    lim = float(lim) if lim else None
    symbol = pair(sym)
    px = binance_price(symbol)  # чтобы не падало из-за MEXC
    explain = reason or ("SMA/новости: положительно" if side.upper()=="BUY" else "Фиксация/ослабление импульса")
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("✅ Да", callback_data=f"ok|{side}|{symbol}|{usdt}|{order_type}|{lim or 0}|{tp}|{sl}|{explain}"),
        InlineKeyboardButton("❌ Нет", callback_data="cancel")
    )
    await m.answer(
        f"📣 <b>Сигнал</b>\n"
        f"• {side.upper()} <b>{symbol}</b>\n• Сумма: <b>{usdt} USDT</b>\n• Тип: <b>{order_type}{' @ '+str(lim) if lim else ''}</b>\n"
        f"• TP: <b>{tp}</b> • SL: <b>{sl}</b>\n"
        f"Текущая цена: <code>{px if px else 'n/a'}</code>\n"
        f"💬 Причина: {explain}\n\nПодтвердить?", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("ok|"))
async def approve(c: types.CallbackQuery):
    if c.from_user.id != ALLOWED_ID and ALLOWED_ID != 0:
        return await c.answer("Нет доступа", show_alert=True)
    _, side, symbol, usdt, order_type, lim, tp, sl, reason = c.data.split("|", 8)
    usdt=float(usdt); lim=float(lim); tp=float(tp); sl=float(sl)
    # В аналитическом режиме просто подтверждаем план (без отправки ордера на биржу)
    msg = (f"✅ План подтверждён (ручное исполнение)\n"
           f"{side} {symbol} на {usdt} USDT\n"
           f"Тип: {order_type}{' @ '+str(lim) if order_type=='LIMIT' else ''}\n"
           f"🎯 TP: {tp} • 🛡️ SL: {sl}\n"
           f"💬 Причина: {reason}\n"
           f"✍️ Выполни вручную на бирже и при необходимости обнови портфель: "
           f"/hold add {symbol.replace('USDT','')} QTY @ PRICE")
    await c.message.edit_reply_markup()
    await c.message.answer(msg)

@dp.callback_query_handler(lambda c: c.data=="cancel")
async def cancel(c: types.CallbackQuery):
    await c.message.edit_reply_markup()
    await c.message.answer("Отменено.")

# ========= SCHEDULES =========
def schedule_reports():
    # Утро 09:00
    scheduler.add_job(lambda: asyncio.create_task(bot.send_message(ALLOWED_ID, "🌅 Утренний обзор: /news, /market")),
                      CronTrigger(hour=9, minute=0))
    # Вечер 18:00 — отчёт по портфелю
    async def evening():
        rep = holding_report()
        await bot.send_message(ALLOWED_ID, "🌆 <b>Вечерний отчёт</b>\n" + rep + "\n\nКоманда: /advice SOL — совет по монете")
    scheduler.add_job(lambda: asyncio.create_task(evening()), CronTrigger(hour=18, minute=0))

def schedule_scanner():
    step = max(1, SCAN_INTERVAL // 60)
    scheduler.add_job(lambda: asyncio.create_task(scan_and_alert()),
                      CronTrigger(minute=f"*/{step}"))

# ========= START =========
if __name__ == "__main__":
    schedule_reports()
    schedule_scanner()
    scheduler.start()
    executor.start_polling(dp, skip_updates=True)
