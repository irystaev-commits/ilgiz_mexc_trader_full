# Ilgis Telegram Advisor Bot (Railway-ready)

Функции:
- Команды: `/start`, `/help`, `/news`, `/market`, `/status`,  
  `/hold add SOL 10 @ 55`, `/hold rm SOL 3`, `/hold report`,  
  `/advice SOL`, `/signal BUY SOL 25 @MKT TP=212 SL=188`.
- Бумажный/реальный режим (PAPER_MODE).
- Ежедневные новости по расписанию (часы в `NEWS_HOURS`).
- Ведение портфеля вручную, советы по фиксации/докупке.
- Кнопочное подтверждение сигналов.

## Переменные окружения (Railway → Service → Variables)
Рекомендуется вставлять через **Raw Editor** одним блоком:
