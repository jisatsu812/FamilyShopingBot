# Семейный список покупок — Telegram Mini App

FastAPI (API + раздача `index.html`) и aiogram-бот работают в **одном процессе**.

## Файлы

- `main.py` — API, проверка Telegram initData, вебхук/polling бота
- `bot.py` — бот: `/start`, уведомления о приглашениях и отзывах
- `index.html` — мини-приложение
- `Procfile`, `requirements.txt` — запуск и зависимости для Render

## Локальный запуск

```bash
pip install -r requirements.txt
cp .env.example .env      # заполни BOT_TOKEN, ADMIN_ID, WEBAPP_URL (ссылка ngrok)
ngrok http 8000
python main.py
```

Без `WEBHOOK_URL` бот работает через polling. Не запускай локально тот же токен, пока сервис живёт на Render:
при старте локальная версия сбросит вебхук. Для тестов удобнее отдельный тестовый бот.

## Деплой на Render

- Start command берётся из `Procfile`.
- Переменные: `BOT_TOKEN`, `ADMIN_ID` (остальное подхватывается само).
- Не задавай `STRICT_AUTH=0` на проде.
- На бесплатном плане файловая система сбрасывается при перезапуске — подключи persistent disk
  и укажи `SHOPPING_DB_PATH=/data/shopping.db`, иначе база обнулится.
