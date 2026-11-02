from dotenv import load_dotenv
load_dotenv()
import asyncio
import os

import uvicorn
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from fastapi import FastAPI
from pydantic import BaseModel

BOT_TOKEN = "8423951970:AAHvjrXMbdbwgipFvZJ1xURCnMc8BcSvJlU"
WEBAPP_URL = "https://dazzler-shelving-capillary.ngrok-free.dev"

# Telegram ID админа. Узнай свой ID (например, через @userinfobot) и впиши сюда.
ADMIN_ID = int(os.getenv("ADMIN_ID", "784580949"))  # 0 = не задан

if not BOT_TOKEN:
    raise RuntimeError("Токен не указан")
if not WEBAPP_URL:
    raise RuntimeError("Ссылка на WebApp не указана")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
notify_app = FastAPI(title="Family Shopping Bot Notifications")


class NotifyData(BaseModel):
    receiver_id: int
    sender_name: str
    is_group: bool = False


class FeedbackData(BaseModel):
    user_id: int
    username: str | None = None
    first_name: str | None = None
    text: str


@notify_app.post("/api/notify_invite")
async def notify_invite(data: NotifyData):
    if data.is_group:
        text = f"👋 {data.sender_name} приглашает тебя в групповую совместную корзину!"
    else:
        text = f"👋 {data.sender_name} приглашает тебя в совместную корзину!"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Посмотреть приглашение",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )
    ]])
    try:
        await bot.send_message(data.receiver_id, text, reply_markup=keyboard)
        return {"status": "ok"}
    except Exception as exc:
        print(f"Не удалось отправить уведомление пользователю {data.receiver_id}: {exc}")
        return {"status": "error", "detail": "Не удалось отправить сообщение"}


@notify_app.post("/api/notify_feedback")
async def notify_feedback(data: FeedbackData):
    if not ADMIN_ID:
        print("ADMIN_ID не задан — отзыв не отправлен.")
        return {"status": "error", "detail": "ADMIN_ID не задан"}

    username_line = f"@{data.username}" if data.username else "—"
    text = (
        "📝 <b>Новый отзыв</b>\n\n"
        f"👤 Имя: {data.first_name or '—'}\n"
        f"🔗 Username: {username_line}\n"
        f"🆔 ID: <code>{data.user_id}</code>\n\n"
        f"💬 Текст:\n{data.text}"
    )
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
        return {"status": "ok"}
    except Exception as exc:
        print(f"Не удалось отправить отзыв админу {ADMIN_ID}: {exc}")
        return {"status": "error", "detail": str(exc)}


@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🛒 Открыть список покупок",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )
    ]])
    first_name = message.from_user.first_name or "друг"
    await message.answer(
        f"Привет, {first_name}! 👋\n\nНажми кнопку ниже, чтобы открыть семейный список покупок.",
        reply_markup=keyboard,
    )


async def main():
    config = uvicorn.Config(
        notify_app,
        host="127.0.0.1",
        port=8080,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        await dp.start_polling(bot)
    finally:
        server.should_exit = True
        await server_task
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())