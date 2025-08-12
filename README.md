# Ilgis Telegram Advisor Bot (Railway-ready)

## Переменные окружения (Railway → Variables → Raw Editor)
Вставь и сохрани, значения под себя:

ALLOWED_USER_ID=6409945468
TELEGRAM_TOKEN=8394255634:AAFjJrWI8Dpka-G4wR514ItKYkWU1FXWtOY
TZ=Asia/Ho_Chi_Minh
PAPER_MODE=true
WATCHLIST=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,AVAXUSDT,INJUSDT,SUIUSDT,OPUSDT,NEARUSDT,LINKUSDT,MATICUSDT,SEIUSDT
SCAN_INTERVAL=5
TP1_PCT=3
TP2_PCT=6
SL_PCT=2
SCHEDULE_TIMES=09:00,19:00,21:00,23:00

## Деплой
1) Закинуть файлы (main.py, requirements.txt, Procfile) в репозиторий.
2) На Railway → New Project → Deploy from GitHub.
3) Заполнить Variables как выше → Restart/Deploy.
4) В Telegram: /start, /status, /advice SOL, /hold report.
