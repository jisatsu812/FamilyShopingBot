import asyncio
import os

import uvicorn
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Update
from fastapi import FastAPI, Request
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://familyshopingbot.onrender.com/")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("Токен не указан")

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
    text = f"👋 {data.sender_name} приглашает тебя в совместную корзину!"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Посмотреть приглашение", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    try:
        await bot.send_message(data.receiver_id, text, reply_markup=keyboard)
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}

@notify_app.post("/api/notify_feedback")
async def notify_feedback(data: FeedbackData):
    if not ADMIN_ID:
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
        return {"status": "error", "detail": str(exc)}

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 Открыть список покупок", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    first_name = message.from_user.first_name or "друг"
    await message.answer(
        f"Привет, {first_name}! 👋\n\nНажми кнопку ниже, чтобы открыть семейный список покупок.",
        reply_markup=keyboard,
    )

@notify_app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

async def main():
    port = int(os.getenv("PORT", 8080))
    webhook_url = os.getenv("WEBHOOK_URL", "https://familyshopingbot.onrender.com")
    
    # Сначала удаляем старый вебхук
    await bot.delete_webhook(drop_pending_updates=True)
    # Устанавливаем новый
    await bot.set_webhook(f"{webhook_url}/webhook")
    
    config = uvicorn.Config(
        notify_app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())