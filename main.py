
import os, time, hmac, hashlib, requests, re, asyncio
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ========= ENV =========
TG_TOKEN = (os.getenv("TELEGRAM_TOKEN") or os.getenv("TG_TOKEN") or "").strip()
ALLOWED_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
API_KEY = os.getenv("MEXC_API_KEY", "").strip()
SECRET  = os.getenv("MEXC_SECRET_KEY", "").strip()
PAPER   = os.getenv("PAPER_MODE", "true").lower() == "true"
MAX_USDT = float(os.getenv("MAX_ORDER_USDT", "300"))
TZ = os.getenv("TZ", "Asia/Ho_Chi_Minh")
BASE = "https://api.mexc.com"

if not TG_TOKEN or ":" not in TG_TOKEN:
    raise RuntimeError(f"Bad TELEGRAM_TOKEN: len={len(TG_TOKEN)}")

bot = Bot(token=TG_TOKEN, parse_mode="HTML")
dp  = Dispatcher(bot)
scheduler = AsyncIOScheduler(timezone=TZ)

def ts(): return int(time.time()*1000)
def sign(query: str) -> str:
    return hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

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

def price(symbol: str) -> float:
    data = mexc("GET", "/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"])

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

def pair(sym: str) -> str:
    sym = sym.upper()
    if not sym.endswith("USDT"): sym += "USDT"
    return sym

SIG_RE = re.compile(
 r"^/signal\s+(BUY|SELL)\s+([A-Z]{2,10})\s+(\d+(?:\.\d+)?)\s+@(?:(MKT)|LIM=(\d+(?:\.\d+)?))\s+TP=(\d+(?:\.\d+)?)\s+SL=(\d+(?:\.\d+)?)\s*(?:\nR:\s*(.+))?$",
 re.IGNORECASE
)

# ====== News & Market (simple) ======
NEWS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://www.investing.com/rss/market_overview.rss"
]

def fetch_url(url, timeout=15):
    try:
        return requests.get(url, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
    except Exception:
        return None

def fetch_news(limit=6):
    items = []
    import re
    for feed in NEWS:
        r = fetch_url(feed, timeout=15)
        if not r or r.status_code != 200: continue
        titles = re.findall(r"<title>(.*?)</title>", r.text, re.I|re.S)
        for t in titles[1:10]:
            t = re.sub("<.*?>", "", t).strip()
            if t and t not in items: items.append(t)
            if len(items)>=limit: break
        if len(items)>=limit: break
    return items

def binance_price(sym):
    u = f"https://api.binance.com/api/v3/ticker/price?symbol={sym}"
    r = fetch_url(u, 8)
    if not r or r.status_code!=200: return None
    try: return float(r.json().get("price",0))
    except: return None

# ====== Bot Handlers ======
def ensure_access(m: types.Message) -> bool:
    return m.from_user.id == ALLOWED_ID or ALLOWED_ID == 0

@dp.message_handler(commands=["start","help"])
async def start(m: types.Message):
    if not ensure_access(m): return await m.answer("⛔️ Нет доступа.")
    txt = ("🤖 Я готов.\n"
           "Команды:\n"
           "• /news — новости 📰\n"
           "• /market — цены BTC/ETH 📊\n"
           "• /balance — баланс на MEXC 💼\n"
           "• /signal BUY SOL 25 @MKT TP=212 SL=188\\nR: Брейкаут 4h  — создать заявку с подтверждением ✅/❌\n"
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

@dp.message_handler(commands=["balance"])
async def balance(m: types.Message):
    if not ensure_access(m): return
    try:
        data = mexc("GET","/api/v3/account", signed=True)
        bals = {b['asset']: float(b['free']) for b in data.get('balances',[]) if float(b['free'])>0}
        top = [f"{k}: {v:.4f}" for k,v in sorted(bals.items(), key=lambda x:-x[1])[:12]]
        await m.answer("💼 Баланс:\n" + ("\n".join(top) if top else "Пусто"))
    except Exception as e:
        await m.answer(f"⚠️ Ошибка баланса: {e}")

@dp.message_handler(commands=["signal"])
async def signal_cmd(m: types.Message):
    if not ensure_access(m): return
    t = m.text.strip()
    mt = SIG_RE.match(t)
    if not mt:
        return await m.answer("❗ Формат:\n/signal BUY SOL 25 @MKT TP=212 SL=188\nR: причина")
    side, sym, usdt, mkt, lim, tp, sl, reason = mt.groups()
    usdt = float(usdt); tp=float(tp); sl=float(sl)
    if usdt > MAX_USDT:
        return await m.answer(f"❗ {usdt}USDT > лимита {MAX_USDT}USDT.")
    order_type = "MARKET" if mkt else "LIMIT"
    lim = float(lim) if lim else None
    symbol = pair(sym)
    px = None
    try: px = price(symbol)
    except: pass
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
    try:
        res = place_spot_order(symbol, side, quote_usdt=usdt,
                               order_type=order_type, limit_price=lim if order_type=="LIMIT" else None)
        msg = f"✅ Ордер отправлен{' (PAPER)' if PAPER else ''}\n{side} {symbol} на {usdt} USDT\n"
        if side.upper()=="BUY":
            # Оценим количество
            if "order" in res and "quantity" in res["order"]:
                qty = float(res["order"]["quantity"])
            else:
                try:
                    px = price(symbol)
                    qty = round(usdt/px, 6)
                except:
                    qty = None
            if qty:
                tp_res = place_tp_limit(symbol, qty, tp)
                try:
                    sl_res = place_sl_stoplimit(symbol, qty, sl, sl*0.997)
                except Exception as e:
                    sl_res = f"SL не создан: {e}"
                msg += f"🎯 TP: {tp_res}\n🛡️ SL: {sl_res}\n"
        await c.message.edit_reply_markup()
        await c.message.answer(msg + f"💬 Причина: {reason}")
    except Exception as e:
        await c.message.answer(f"⚠️ Ошибка: {e}")

@dp.callback_query_handler(lambda c: c.data=="cancel")
async def cancel(c: types.CallbackQuery):
    await c.message.edit_reply_markup()
    await c.message.answer("Отменено.")

# ===== Schedules =====
def schedule_reports():
    scheduler.add_job(lambda: asyncio.create_task(bot.send_message(ALLOWED_ID, "🌅 Утренний обзор доступен: /news, /market")), CronTrigger(hour=9, minute=0))
    scheduler.add_job(lambda: asyncio.create_task(bot.send_message(ALLOWED_ID, "🌇 Вечерний обзор доступен: /news, /market")), CronTrigger(hour=20, minute=0))

if __name__ == "__main__":
    schedule_reports()
    scheduler.start()
    executor.start_polling(dp, skip_updates=True)
