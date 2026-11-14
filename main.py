"""Семейный список покупок: API + раздача мини-аппа + Telegram-бот в одном процессе.

Запуск локально:   python main.py        (или: uvicorn main:app --reload --port 8000)
Запуск на Render:  uvicorn main:app --host 0.0.0.0 --port $PORT   (см. Procfile)
"""
from dotenv import load_dotenv

load_dotenv()

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Optional
from urllib.parse import parse_qsl

from aiogram.types import Update
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import bot as tgbot

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("SHOPPING_DB_PATH", os.path.join(BASE_DIR, "shopping.db"))

# Username, которому автоматически выдаётся админка
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "tune430hz")

# STRICT_AUTH=1 (по умолчанию): API принимает только запросы из Telegram с валидным initData.
# STRICT_AUTH=0: можно тестировать в обычном браузере (только для локальной разработки!).
STRICT_AUTH = os.getenv("STRICT_AUTH", "1") != "0"
INITDATA_MAX_AGE = 7 * 24 * 3600  # initData считается свежей 7 суток

# Файлы, которые можно отдавать по /static/<имя> (всё остальное — .env, .db, .py — закрыто)
STATIC_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".css", ".js"}


# ───────────────────────── Запуск / остановка ─────────────────────────
@asynccontextmanager
async def lifespan(_: FastAPI):
    polling_task = None
    try:
        if tgbot.PUBLIC_URL:
            await tgbot.bot.set_webhook(
                f"{tgbot.PUBLIC_URL}/webhook",
                secret_token=tgbot.WEBHOOK_SECRET,
                drop_pending_updates=True,
            )
            print(f"Вебхук установлен: {tgbot.PUBLIC_URL}/webhook")
        else:
            await tgbot.bot.delete_webhook(drop_pending_updates=True)
            polling_task = asyncio.create_task(
                tgbot.dp.start_polling(tgbot.bot, handle_signals=False)
            )
            print("Бот запущен через polling (локальный режим)")
    except Exception as exc:
        print(f"Не удалось инициализировать бота: {exc}")

    try:
        yield
    finally:
        if polling_task:
            polling_task.cancel()
            try:
                await polling_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await tgbot.bot.session.close()
        except Exception:
            pass


app = FastAPI(title="Family Shopping API", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# ───────────────────────── База данных ─────────────────────────
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE COLLATE NOCASE,
            first_name TEXT,
            is_admin INTEGER NOT NULL DEFAULT 0
        )""")
        # Миграция: если колонки is_admin ещё нет — добавляем
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "is_admin" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

        conn.execute("""CREATE TABLE IF NOT EXISTS friendships (
            user_id INTEGER NOT NULL,
            friend_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, friend_id),
            CHECK (user_id != friend_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            checked INTEGER NOT NULL DEFAULT 0,
            added_by INTEGER NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_owner ON items(list_owner_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            list_owner_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(sender_id, receiver_id, list_owner_id)
        )""")


init_db()


# ───────────────────────── Проверка Telegram initData ─────────────────────────
def verify_init_data(init_data: str) -> Optional[dict]:
    """Проверяет подпись initData по алгоритму Telegram. Возвращает dict пользователя или None."""
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", tgbot.BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            return None
        auth_date = int(parsed.get("auth_date", "0"))
        if time.time() - auth_date > INITDATA_MAX_AGE:
            return None
        user = json.loads(parsed.get("user", "{}"))
        return user if isinstance(user, dict) and "id" in user else None
    except Exception:
        return None


def auth(request: Request) -> Optional[dict]:
    """Зависимость: возвращает проверенного Telegram-пользователя (или None в dev-режиме)."""
    init_data = request.headers.get("X-Init-Data", "")
    if init_data:
        user = verify_init_data(init_data)
        if user:
            return user
        raise HTTPException(status_code=401, detail="Не удалось подтвердить сессию Telegram. Откройте приложение заново.")
    if STRICT_AUTH:
        raise HTTPException(status_code=401, detail="Откройте приложение из Telegram")
    return None


def check_user(tg_user: Optional[dict], claimed_id: int):
    """Запрещает действовать от имени чужого user_id."""
    if tg_user is not None and int(tg_user["id"]) != int(claimed_id):
        raise HTTPException(status_code=403, detail="Нельзя действовать от имени другого пользователя")


# ───────────────────────── Модели ─────────────────────────
class UserInit(BaseModel):
    id: int
    username: Optional[str] = Field(default=None, max_length=64)
    first_name: Optional[str] = Field(default=None, max_length=128)


class ItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    list_owner_id: int


class ItemUpdate(BaseModel):
    checked: bool


class InviteCreate(BaseModel):
    sender_id: int
    receiver_username: str = Field(min_length=1, max_length=64)


class FeedbackCreate(BaseModel):
    user_id: int
    text: str = Field(min_length=1, max_length=2000)


# ───────────────────────── Хелперы доступа ─────────────────────────
def ensure_user_exists(conn, user_id: int):
    if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Пользователь не найден")


def can_access_list(conn, user_id: int, owner_id: int) -> bool:
    if user_id == owner_id:
        return True
    return bool(conn.execute(
        "SELECT 1 FROM friendships WHERE user_id = ? AND friend_id = ?",
        (user_id, owner_id),
    ).fetchone())


def ensure_item_access(conn, user_id: int, item_id: int):
    item = conn.execute("SELECT id, list_owner_id FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        raise HTTPException(status_code=404, detail="Товар не найден")
    if not can_access_list(conn, user_id, item["list_owner_id"]):
        raise HTTPException(status_code=403, detail="Нет доступа к этому списку")
    return item


# ───────────────────────── Страницы и служебное ─────────────────────────
@app.get("/")
def get_index():
    return FileResponse(
        os.path.join(BASE_DIR, "index.html"),
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/static/{filename}")
def static_file(filename: str):
    # Отдаём только картинки/стили/скрипты из корня проекта. .env, .db, .py — недоступны.
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=404)
    if os.path.splitext(filename)[1].lower() not in STATIC_ALLOWED_EXT:
        raise HTTPException(status_code=404)
    path = os.path.join(BASE_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404)
    return FileResponse(path)


@app.post("/webhook")
async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != tgbot.WEBHOOK_SECRET:
        raise HTTPException(status_code=403)
    data = await request.json()
    update = Update.model_validate(data, context={"bot": tgbot.bot})
    try:
        await tgbot.dp.feed_update(tgbot.bot, update)
    except Exception as exc:
        print(f"Ошибка обработки апдейта: {exc}")
    return {"ok": True}


# ───────────────────────── API ─────────────────────────
@app.post("/api/login")
def login(user: UserInit, tg_user: Optional[dict] = Depends(auth)):
    check_user(tg_user, user.id)
    # Если есть проверенные данные из Telegram — доверяем только им
    raw_username = tg_user.get("username") if tg_user else user.username
    first_name = tg_user.get("first_name") if tg_user else user.first_name
    username = (raw_username or "").strip().lstrip("@") or None
    is_admin = 1 if (username and username.lower() == ADMIN_USERNAME.lower()) else 0

    with get_db() as conn:
        if username:
            # Username в Telegram уникален: если он числился за другим (устаревшим) аккаунтом — освобождаем
            conn.execute(
                "UPDATE users SET username = NULL WHERE username = ? COLLATE NOCASE AND id != ?",
                (username, user.id),
            )
        existing = conn.execute("SELECT id, is_admin FROM users WHERE id = ?", (user.id,)).fetchone()
        if existing:
            # Админку не снимаем никогда, но повышаем, если username совпал
            new_admin = 1 if (existing["is_admin"] or is_admin) else 0
            conn.execute(
                "UPDATE users SET username = ?, first_name = ?, is_admin = ? WHERE id = ?",
                (username, first_name, new_admin, user.id),
            )
            is_admin = new_admin
        else:
            conn.execute(
                "INSERT INTO users (id, username, first_name, is_admin) VALUES (?, ?, ?, ?)",
                (user.id, username, first_name, is_admin),
            )
    return {"status": "ok", "is_admin": bool(is_admin)}


@app.get("/api/friends")
def get_friends(user_id: int, tg_user: Optional[dict] = Depends(auth)):
    check_user(tg_user, user_id)
    with get_db() as conn:
        ensure_user_exists(conn, user_id)
        rows = conn.execute("""
            SELECT DISTINCT u.id,
                COALESCE(NULLIF(u.first_name, ''), u.username, 'Пользователь') AS name
            FROM friendships f
            JOIN users u ON u.id = CASE WHEN f.user_id = ? THEN f.friend_id ELSE f.user_id END
            WHERE f.user_id = ? OR f.friend_id = ?
            ORDER BY name COLLATE NOCASE
        """, (user_id, user_id, user_id)).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/items")
def get_items(user_id: int, list_owner_id: int, tg_user: Optional[dict] = Depends(auth)):
    check_user(tg_user, user_id)
    with get_db() as conn:
        ensure_user_exists(conn, user_id)
        if not can_access_list(conn, user_id, list_owner_id):
            raise HTTPException(status_code=403, detail="Нет доступа к этому списку")
        rows = conn.execute(
            "SELECT id, list_owner_id, name, checked, added_by FROM items "
            "WHERE list_owner_id = ? ORDER BY checked ASC, id DESC",
            (list_owner_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/items")
def add_item(user_id: int, item: ItemCreate, tg_user: Optional[dict] = Depends(auth)):
    check_user(tg_user, user_id)
    name = item.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Название товара не может быть пустым")
    with get_db() as conn:
        ensure_user_exists(conn, user_id)
        if not can_access_list(conn, user_id, item.list_owner_id):
            raise HTTPException(status_code=403, detail="Нет доступа к этому списку")
        cur = conn.execute(
            "INSERT INTO items (list_owner_id, name, checked, added_by) VALUES (?, ?, 0, ?)",
            (item.list_owner_id, name, user_id),
        )
        item_id = cur.lastrowid
    return {"status": "ok", "id": item_id}


@app.patch("/api/items/{item_id}")
def toggle_item(item_id: int, item: ItemUpdate, user_id: int = Query(...),
                tg_user: Optional[dict] = Depends(auth)):
    check_user(tg_user, user_id)
    with get_db() as conn:
        record = ensure_item_access(conn, user_id, item_id)
        conn.execute("UPDATE items SET checked = ? WHERE id = ?", (int(item.checked), record["id"]))
    return {"status": "ok"}


@app.delete("/api/items/{item_id}")
def delete_item(item_id: int, user_id: int = Query(...), tg_user: Optional[dict] = Depends(auth)):
    check_user(tg_user, user_id)
    with get_db() as conn:
        record = ensure_item_access(conn, user_id, item_id)
        conn.execute("DELETE FROM items WHERE id = ?", (record["id"],))
    return {"status": "ok"}


@app.delete("/api/items")
def clear_items(user_id: int, list_owner_id: int, tg_user: Optional[dict] = Depends(auth)):
    check_user(tg_user, user_id)
    with get_db() as conn:
        ensure_user_exists(conn, user_id)
        if not can_access_list(conn, user_id, list_owner_id):
            raise HTTPException(status_code=403, detail="Нет доступа к этому списку")
        conn.execute("DELETE FROM items WHERE list_owner_id = ?", (list_owner_id,))
    return {"status": "ok"}


@app.post("/api/invites/send")
async def send_invite(data: InviteCreate, tg_user: Optional[dict] = Depends(auth)):
    check_user(tg_user, data.sender_id)
    username = data.receiver_username.strip().lstrip("@")
    if not username:
        raise HTTPException(status_code=422, detail="Укажите username")
    with get_db() as conn:
        ensure_user_exists(conn, data.sender_id)
        receiver = conn.execute(
            "SELECT id, first_name FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if not receiver:
            raise HTTPException(
                status_code=404,
                detail="Пользователь не найден. Пусть он сначала запустит бота.",
            )
        receiver_id = receiver["id"]
        if receiver_id == data.sender_id:
            raise HTTPException(status_code=400, detail="Нельзя пригласить самого себя")
        already = conn.execute(
            "SELECT 1 FROM friendships WHERE user_id = ? AND friend_id = ?",
            (data.sender_id, receiver_id),
        ).fetchone()
        if already:
            raise HTTPException(status_code=409, detail="Вы уже делитесь списками")
        conn.execute("""
            INSERT INTO invites (sender_id, receiver_id, list_owner_id, status)
            VALUES (?, ?, ?, 'pending')
            ON CONFLICT(sender_id, receiver_id, list_owner_id) DO UPDATE SET status = 'pending'
        """, (data.sender_id, receiver_id, data.sender_id))
        sender = conn.execute(
            "SELECT first_name, username FROM users WHERE id = ?", (data.sender_id,)
        ).fetchone()
        sender_name = (
            (sender["first_name"] if sender else None)
            or (sender["username"] if sender else None)
            or "Пользователь"
        )

    notified = await tgbot.notify_invite(receiver_id, sender_name)
    return {
        "status": "ok",
        "notified": notified,
        "message": "Приглашение отправлено" if notified
        else "Приглашение создано — друг увидит его в приложении",
    }


@app.get("/api/invites")
def get_invites(user_id: int, tg_user: Optional[dict] = Depends(auth)):
    check_user(tg_user, user_id)
    with get_db() as conn:
        ensure_user_exists(conn, user_id)
        rows = conn.execute("""
            SELECT i.id, i.sender_id, i.list_owner_id,
                COALESCE(NULLIF(u.first_name, ''), u.username, 'Пользователь') AS sender_name
            FROM invites i JOIN users u ON u.id = i.sender_id
            WHERE i.receiver_id = ? AND i.status = 'pending'
            ORDER BY i.id DESC
        """, (user_id,)).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/invites/{invite_id}/accept")
def accept_invite(invite_id: int, user_id: int = Query(...), tg_user: Optional[dict] = Depends(auth)):
    check_user(tg_user, user_id)
    with get_db() as conn:
        invite = conn.execute(
            "SELECT * FROM invites WHERE id = ? AND receiver_id = ? AND status = 'pending'",
            (invite_id, user_id),
        ).fetchone()
        if not invite:
            raise HTTPException(status_code=404, detail="Приглашение не найдено или уже обработано")
        conn.execute("INSERT OR IGNORE INTO friendships (user_id, friend_id) VALUES (?, ?)",
                     (user_id, invite["sender_id"]))
        conn.execute("INSERT OR IGNORE INTO friendships (user_id, friend_id) VALUES (?, ?)",
                     (invite["sender_id"], user_id))
        conn.execute("UPDATE invites SET status = 'accepted' WHERE id = ?", (invite_id,))
    return {"status": "ok"}


@app.post("/api/invites/{invite_id}/decline")
def decline_invite(invite_id: int, user_id: int = Query(...), tg_user: Optional[dict] = Depends(auth)):
    check_user(tg_user, user_id)
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE invites SET status = 'declined' WHERE id = ? AND receiver_id = ? AND status = 'pending'",
            (invite_id, user_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Приглашение не найдено или уже обработано")
    return {"status": "ok"}


@app.post("/api/feedback")
async def send_feedback(data: FeedbackCreate, tg_user: Optional[dict] = Depends(auth)):
    check_user(tg_user, data.user_id)
    text = data.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Отзыв не может быть пустым")
    with get_db() as conn:
        user = conn.execute(
            "SELECT username, first_name FROM users WHERE id = ?", (data.user_id,)
        ).fetchone()
    username = user["username"] if user else None
    first_name = user["first_name"] if user else None

    ok = await tgbot.notify_feedback(data.user_id, username, first_name, text)
    if not ok:
        raise HTTPException(status_code=502, detail="Не удалось доставить отзыв, попробуйте позже")
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
