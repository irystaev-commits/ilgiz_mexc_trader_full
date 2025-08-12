# Ilgis Telegram Advisor Bot (Railway-ready)

Команды:
- /news — новости
- /market — цены BTC/ETH
- /status — статус сканера
- /advice <SYMBOL> — совет по монете (например, /advice SOL)
- /hold add SOL 10 @ 55 — добавить позицию
- /hold rm SOL 3 — списать (продажа)
- /hold report — отчёт по портфелю
- /set tp1=5 tp2=12 sl=-3 iv=5 wl=BTCUSDT,ETHUSDT,SOLUSDT — изменить параметры «на лету»

Авто‑дайджесты идут по CRON_TIMES (по умолчанию 09:00,19:00,21:00,23:00 по TZ).

## Переменные окружения (Railway → Variables → Raw Editor)
Вставьте блок ниже и сохраните, затем перезапустите:

TELEGRAM_TOKEN=Поставь_свой_токен_бота
ALLOWED_USER_ID=Твой_Telegram_ID
TZ=Asia/Ho_Chi_Minh
PAPER_MODE=true
WATCHLIST=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,AVAXUSDT,INJUSDT,SUIUSDT,OPUSDT,NEARUSDT,LINKUSDT,MATICUSDT,SEIUSDT
SCAN_INTERVAL=5
TP1_PCT=3.0
TP2_PCT=6.0
SL_PCT=-2.0
CRON_TIMES=09:00,19:00,21:00,23:00

# (опционально для реальных ордеров на будущее)
MEXC_API_KEY=
MEXC_SECRET_KEY=
