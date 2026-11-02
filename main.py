from dotenv import load_dotenv
load_dotenv()
import os
import sqlite3
from contextlib import contextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("SHOPPING_DB_PATH", os.path.join(BASE_DIR, "shopping.db"))
BOT_NOTIFY_URL = os.getenv("BOT_NOTIFY_URL", "http://127.0.0.1:8080/api/notify_invite")
BOT_FEEDBACK_URL = os.getenv("BOT_FEEDBACK_URL", "http://127.0.0.1:8080/api/notify_feedback")

# Username, которому автоматически выдаётся админка
ADMIN_USERNAME = "tune430hz"

app = FastAPI(title="Family Shopping API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

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

@app.get("/")
async def get_index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"), media_type="text/html")

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/api/login")
async def login(user: UserInit):
    username = (user.username or "").strip().lstrip("@") or None
    is_admin = 1 if (username and username.lower() == ADMIN_USERNAME.lower()) else 0
    with get_db() as conn:
        existing = conn.execute("SELECT id, is_admin FROM users WHERE id = ?", (user.id,)).fetchone()
        if existing:
            # Админку не снимаем никогда, но повышаем, если username совпал
            new_admin = 1 if (existing["is_admin"] or is_admin) else 0
            conn.execute(
                "UPDATE users SET username = ?, first_name = ?, is_admin = ? WHERE id = ?",
                (username, user.first_name, new_admin, user.id),
            )
            is_admin = new_admin
        else:
            try:
                conn.execute(
                    "INSERT INTO users (id, username, first_name, is_admin) VALUES (?, ?, ?, ?)",
                    (user.id, username, user.first_name, is_admin),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail="Этот username уже используется")
    return {"status": "ok", "is_admin": bool(is_admin)}

@app.get("/api/friends")
async def get_friends(user_id: int):
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
async def get_items(user_id: int, list_owner_id: int):
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
async def add_item(user_id: int, item: ItemCreate):
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
async def toggle_item(item_id: int, item: ItemUpdate, user_id: int = Query(...)):
    with get_db() as conn:
        record = ensure_item_access(conn, user_id, item_id)
        conn.execute("UPDATE items SET checked = ? WHERE id = ?", (int(item.checked), record["id"]))
    return {"status": "ok"}

@app.delete("/api/items/{item_id}")
async def delete_item(item_id: int, user_id: int = Query(...)):
    with get_db() as conn:
        record = ensure_item_access(conn, user_id, item_id)
        conn.execute("DELETE FROM items WHERE id = ?", (record["id"],))
    return {"status": "ok"}

@app.delete("/api/items")
async def clear_items(user_id: int, list_owner_id: int):
    with get_db() as conn:
        ensure_user_exists(conn, user_id)
        if not can_access_list(conn, user_id, list_owner_id):
            raise HTTPException(status_code=403, detail="Нет доступа к этому списку")
        conn.execute("DELETE FROM items WHERE list_owner_id = ?", (list_owner_id,))
    return {"status": "ok"}

@app.post("/api/invites/send")
async def send_invite(data: InviteCreate):
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
        access_count = conn.execute(
            "SELECT COUNT(*) FROM friendships WHERE user_id = ?", (data.sender_id,)
        ).fetchone()[0]

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(BOT_NOTIFY_URL, json={
                "receiver_id": receiver_id,
                "sender_name": sender_name,
                "is_group": access_count >= 1,
            })
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Не удалось отправить уведомление боту: {exc}")
    return {"status": "ok", "message": "Приглашение отправлено"}

@app.get("/api/invites")
async def get_invites(user_id: int):
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
async def accept_invite(invite_id: int, user_id: int = Query(...)):
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
async def decline_invite(invite_id: int, user_id: int = Query(...)):
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE invites SET status = 'declined' WHERE id = ? AND receiver_id = ? AND status = 'pending'",
            (invite_id, user_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Приглашение не найдено или уже обработано")
    return {"status": "ok"}

@app.post("/api/feedback")
async def send_feedback(data: FeedbackCreate):
    text = data.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Отзыв не может быть пустым")
    with get_db() as conn:
        user = conn.execute(
            "SELECT username, first_name FROM users WHERE id = ?", (data.user_id,)
        ).fetchone()
    username = user["username"] if user else None
    first_name = user["first_name"] if user else None

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(BOT_FEEDBACK_URL, json={
                "user_id": data.user_id,
                "username": username,
                "first_name": first_name,
                "text": text,
            })
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Не удалось отправить отзыв боту: {exc}")
        raise HTTPException(status_code=502, detail="Не удалось доставить отзыв, попробуйте позже")
    return {"status": "ok"}

app.mount("/static", StaticFiles(directory=BASE_DIR), name="static")