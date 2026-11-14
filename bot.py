"""Телеграм-бот (aiogram 3).

Отдельно запускать не нужно: бот подключается из main.py и работает в одном процессе с API.
Локально (без WEBHOOK_URL / RENDER_EXTERNAL_URL) бот работает через polling,
на хостинге (Render) — через вебхук.
"""
import hashlib
import html
import os

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Токен не указан: добавь BOT_TOKEN в .env или в переменные окружения")

ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)

# Публичный адрес сервиса. На Render RENDER_EXTERNAL_URL задаётся автоматически.
# Если пусто (локальный запуск) — бот работает через polling.
PUBLIC_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

# Адрес, который открывается по кнопке в Telegram
WEBAPP_URL = os.getenv("WEBAPP_URL") or PUBLIC_URL or "https://familyshopingbot.onrender.com"

# Секрет, по которому main.py проверяет, что вебхук дёргает именно Telegram
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(BOT_TOKEN.encode()).hexdigest()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def _open_app_keyboard(text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, web_app=WebAppInfo(url=WEBAPP_URL))
    ]])


@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    first_name = message.from_user.first_name or "друг"
    await message.answer(
        f"Привет, {first_name}! 👋\n\nНажми кнопку ниже, чтобы открыть семейный список покупок.",
        reply_markup=_open_app_keyboard("🛒 Открыть список покупок"),
    )


async def notify_invite(receiver_id: int, sender_name: str) -> bool:
    """Шлёт получателю уведомление о приглашении. True — если доставлено."""
    try:
        await bot.send_message(
            receiver_id,
            f"👋 {sender_name} приглашает тебя в совместную корзину!",
            reply_markup=_open_app_keyboard("Посмотреть приглашение"),
        )
        return True
    except Exception as exc:
        print(f"Не удалось отправить уведомление {receiver_id}: {exc}")
        return False


async def notify_feedback(user_id: int, username: str | None, first_name: str | None, text: str) -> bool:
    """Пересылает отзыв админу. True — если доставлено."""
    if not ADMIN_ID:
        print("ADMIN_ID не задан — отзыв некуда отправить")
        return False
    username_line = f"@{html.escape(username)}" if username else "—"
    message = (
        "📝 <b>Новый отзыв</b>\n\n"
        f"👤 Имя: {html.escape(first_name) if first_name else '—'}\n"
        f"🔗 Username: {username_line}\n"
        f"🆔 ID: <code>{user_id}</code>\n\n"
        f"💬 Текст:\n{html.escape(text)}"
    )
    try:
        await bot.send_message(ADMIN_ID, message, parse_mode="HTML")
        return True
    except Exception as exc:
        print(f"Не удалось отправить отзыв админу: {exc}")
        return False


if __name__ == "__main__":
    print("Бот запускается вместе с API. Запускай:  python main.py  (или  uvicorn main:app --port 8000)")
