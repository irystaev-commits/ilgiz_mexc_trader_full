# Ilgis MEXC Trader Bot (Telegram, Railway-ready)

Функции:
- Команды: /start, /balance, /news, /market, /signal (шаблон), /help
- Утро/вечер авто-дайджест: новости + тренд BTC/ETH (SMA20/50)
- Сигнал с подтверждением ✅ Да / ❌ Нет
- По подтверждению — реальная покупка/продажа на MEXC (если PAPER_MODE=false)
- Автоустановка TP (limit) и SL (stop-loss) после покупки

Переменные окружения в Railway → Variables:
- TELEGRAM_TOKEN — токен бота от @BotFather
- ALLOWED_USER_ID — твой Telegram ID (чтобы только ты управлял)
- MEXC_API_KEY — API Key на MEXC (только Trade, без Withdraw)
- MEXC_SECRET_KEY — Secret Key MEXC
- PAPER_MODE — true/false (бумажный режим по умолчанию true)
- MAX_ORDER_USDT — лимит на одну сделку, например 300
- TZ — часовой пояс, например Asia/Almaty или Asia/Ho_Chi_Minh

Примеры:
/signal BUY SOL 25 @MKT TP=212 SL=188
/signal BUY BTC 50 @LIM=59000 TP=60500 SL=57500
