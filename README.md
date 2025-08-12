Ilgis MEXC Trader Bot (Telegram, Railway)

Команды:
• /start, /help
• /news — новости
• /market — цены BTC/ETH
• /balance — баланс MEXC
• /signal BUY SOL 25 @MKT TP=212 SL=188
  R: краткая причина
  -> Подтверждение кнопками ✅/❌ (после "Да" создаются ордер + TP + SL)

Переменные окружения (Railway -> Variables):
TELEGRAM_TOKEN  — токен из @BotFather
ALLOWED_USER_ID — ваш Telegram ID (число) (можно 0, чтобы тестить без ограничения)
MEXC_API_KEY    — API Key MEXC
MEXC_SECRET_KEY — Secret Key MEXC
PAPER_MODE      — true/false (бумажный режим)
MAX_ORDER_USDT  — лимит на одну сделку (например 300)
TZ              — часовой пояс, по умолчанию Asia/Ho_Chi_Minh

Деплой:
1) Залей файлы в GitHub.
2) Railway -> New Project -> Deploy from GitHub.
3) Добавь все переменные выше. Нажми Restart/Deploy.
4) В Telegram: /start, /news, /market, /balance, /signal ...
