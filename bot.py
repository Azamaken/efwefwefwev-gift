import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
import sqlite3
from datetime import datetime

# ===== optional account mode =====
USE_ACCOUNT_MODE = os.getenv("USE_ACCOUNT_MODE", "1") == "1"
if USE_ACCOUNT_MODE:
    from telethon import TelegramClient, functions, types

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8398522812:AAHzw5HLSBQIBVFIMWv2kvE0nFaKz-O6q2A")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7528568061"))

# для режима "от аккаунта"
API_ID = int(os.getenv("API_ID", "28563482"))
API_HASH = os.getenv("API_HASH", "914e598bf5ca977a5f53d3c3b4f6f148")
ACCOUNT_SESSION = os.getenv("ACCOUNT_SESSION", "gift_user")
ACCOUNT_USERNAME = os.getenv("ACCOUNT_USERNAME", "nolyktg")

BOT_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ShellGifts")

router = Router()

user_states: Dict[int, Dict[str, Any]] = {}
gift_cache: List[Dict[str, Any]] = []
gift_map: Dict[str, Dict[str, Any]] = {}
pending_invoices: Dict[str, Dict[str, Any]] = {}

DB_PATH = "gifts.db"


def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS gift_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        full_name TEXT,
        sender_type TEXT NOT NULL,
        gift_id TEXT NOT NULL,
        gift_title TEXT NOT NULL,
        target_user_id INTEGER NOT NULL,
        gift_text TEXT,
        price_stars INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS custom_gifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gift_id TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        star_count INTEGER NOT NULL DEFAULT 0,
        emoji_id TEXT,
        is_pinned INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS required_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        invite_link TEXT,
        title TEXT,
        is_main INTEGER DEFAULT 0,
        is_enabled INTEGER DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS bot_users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS required_channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT,
    title TEXT,
    link TEXT,
    is_main INTEGER DEFAULT 0
    )
    """)
    
    conn.commit()
    conn.close()

account_client = None

# =========================
# LEGACY / PINNED GIFTS
# =========================
LEGACY_GIFTS: List[Dict[str, Any]] = [
    {
        "id": "5801108895304779062",
        "title": "💝 Старое сердечко",
        "star_count": 50,
        "emoji_id": "5224628072619216265",
        "pinned": True,
    },
    {
        "id": "5800655655995968830",
        "title": "🧸 Старый белый мишка",
        "star_count": 50,
        "emoji_id": "5226661632259691727",
        "pinned": True,
    },
]

# =========================
# BOT API RAW HELPERS
# =========================
async def bot_api_call(method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{BOT_API_BASE}/{method}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload or {}) as resp:
            data = await resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"{method} failed: {data}")
            return data["result"]


async def get_my_star_balance() -> Dict[str, Any]:
    return await bot_api_call("getMyStarBalance")


async def get_available_gifts() -> List[Dict[str, Any]]:
    result = await bot_api_call("getAvailableGifts")
    return result.get("gifts", [])


async def send_gift_via_bot(
    *,
    user_id: Optional[int] = None,
    chat_id: Optional[str | int] = None,
    gift_id: str,
    text: str = "",
    pay_for_upgrade: bool = False,
) -> bool:
    payload: Dict[str, Any] = {"gift_id": gift_id}

    if user_id is not None:
        payload["user_id"] = user_id
    elif chat_id is not None:
        payload["chat_id"] = chat_id
    else:
        raise ValueError("Нужен user_id или chat_id")

    if text:
        payload["text"] = text[:128]

    if pay_for_upgrade:
        payload["pay_for_upgrade"] = True

    result = await bot_api_call("sendGift", payload)
    return bool(result)

# =========================
# ACCOUNT MODE HELPERS
# =========================
async def start_account_client() -> None:
    global account_client
    if not USE_ACCOUNT_MODE:
        return
    if not API_ID or not API_HASH:
        raise RuntimeError("Для режима аккаунта нужны API_ID и API_HASH")
    account_client = TelegramClient(ACCOUNT_SESSION, API_ID, API_HASH)
    await account_client.start()


async def resolve_account_peer(identifier: int | str):
    if account_client is None:
        raise RuntimeError("account_client не запущен")

    if isinstance(identifier, int):
        return await account_client.get_input_entity(identifier)

    text = str(identifier).strip()
    if text.startswith("@"):
        text = text[1:]
    return await account_client.get_input_entity(text)


async def send_gift_via_account(
    *,
    user_id: int,
    gift_id: str,
    text: str = "",
) -> None:
    if account_client is None:
        raise RuntimeError("Режим аккаунта не включён")

    peer = await resolve_account_peer(user_id)

    invoice = types.InputInvoiceStarGift(
        peer=peer,
        gift_id=int(gift_id),
        hide_name=False,
        message=types.TextWithEntities(
            text=text[:128] if text else "",
            entities=[],
        ),
    )

    form = await account_client(functions.payments.GetPaymentFormRequest(invoice=invoice))
    await account_client(functions.payments.SendStarsFormRequest(
        form_id=form.form_id,
        invoice=invoice,
    ))

async def check_subs(bot,user_id):

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT chat_id, invite_link FROM required_channels WHERE is_enabled=1")

    channels = cur.fetchall()

    conn.close()

    not_sub = []

    for cid, invite_link in channels:

        try:

            member = await bot.get_chat_member(cid,user_id)

            if member.status not in ["member","administrator","creator"]:

                not_sub.append((cid, invite_link))

        except:

            not_sub.append((cid, invite_link))

    return not_sub

async def broadcast_copy(bot,message):

    conn=db_connect()
    cur=conn.cursor()

    cur.execute("SELECT user_id FROM bot_users")

    users=cur.fetchall()

    conn.close()

    for (uid,) in users:

        try:

            await bot.copy_message(
                chat_id=uid,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )

        except:
            pass

# =========================
# DATA BASE
# =========================

def add_history_record(
    *,
    user_id: int,
    username: str | None,
    full_name: str,
    sender_type: str,
    gift_id: str,
    gift_title: str,
    target_user_id: int,
    gift_text: str = "",
    price_stars: int = 0,
):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO gift_history (
        user_id, username, full_name, sender_type,
        gift_id, gift_title, target_user_id,
        gift_text, price_stars, created_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        username,
        full_name,
        sender_type,
        gift_id,
        gift_title,
        target_user_id,
        gift_text,
        price_stars,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ))

    conn.commit()
    conn.close()

def get_user_stats(user_id: int) -> dict:
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    SELECT COUNT(*), COALESCE(SUM(price_stars), 0)
    FROM gift_history
    WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()

    conn.close()

    return {
        "total_sent": row[0] or 0,
        "total_spent": row[1] or 0,
    }

def get_user_history(user_id: int, limit: int = 5, offset: int = 0) -> list[dict]:
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    SELECT sender_type, gift_title, target_user_id, gift_text, price_stars, created_at
    FROM gift_history
    WHERE user_id = ?
    ORDER BY id DESC
    LIMIT ? OFFSET ?
    """, (user_id, limit, offset))

    rows = cur.fetchall()
    conn.close()

    result = []
    for row in rows:
        result.append({
            "sender_type": row[0],
            "gift_title": row[1],
            "target_user_id": row[2],
            "gift_text": row[3],
            "price_stars": row[4],
            "created_at": row[5],
        })
    return result

def get_user_history_count(user_id: int) -> int:
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    SELECT COUNT(*)
    FROM gift_history
    WHERE user_id = ?
    """, (user_id,))

    total = cur.fetchone()[0]
    conn.close()
    return total

def add_custom_gift(
    *,
    gift_id: str,
    title: str,
    star_count: int,
    emoji_id: str = "",
    is_pinned: bool = False,
):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO custom_gifts (gift_id, title, star_count, emoji_id, is_pinned, created_at)
    VALUES (?, ?, ?, ?, ?, datetime('now'))
    """, (
        str(gift_id),
        title,
        int(star_count),
        emoji_id or None,
        1 if is_pinned else 0,
    ))

    conn.commit()
    conn.close()


def get_custom_gifts() -> list[dict]:
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    SELECT gift_id, title, star_count, emoji_id, is_pinned
    FROM custom_gifts
    ORDER BY is_pinned DESC, id DESC
    """)

    rows = cur.fetchall()
    conn.close()

    result = []
    for row in rows:
        result.append({
            "gift_id": row[0],
            "title": row[1],
            "star_count": row[2],
            "emoji_id": row[3],
            "is_pinned": bool(row[4]),
        })
    return result


def get_custom_gift(gift_id: str) -> dict | None:
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    SELECT gift_id, title, star_count, emoji_id, is_pinned
    FROM custom_gifts
    WHERE gift_id = ?
    """, (str(gift_id),))

    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "gift_id": row[0],
        "title": row[1],
        "star_count": row[2],
        "emoji_id": row[3],
        "is_pinned": bool(row[4]),
    }


def delete_custom_gift(gift_id: str):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("DELETE FROM custom_gifts WHERE gift_id = ?", (str(gift_id),))

    conn.commit()
    conn.close()


def toggle_custom_gift_pin(gift_id: str):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    UPDATE custom_gifts
    SET is_pinned = CASE WHEN is_pinned = 1 THEN 0 ELSE 1 END
    WHERE gift_id = ?
    """, (str(gift_id),))

    conn.commit()
    conn.close()

def save_user(user):

    conn=db_connect()
    cur=conn.cursor()

    cur.execute(
        "INSERT OR IGNORE INTO bot_users (user_id,username,full_name,created_at) VALUES (?,?,?,?)",
        (
            user.id,
            user.username,
            f"{user.first_name or ''} {user.last_name or ''}",
            datetime.now().isoformat()
        )
    )

    conn.commit()
    conn.close()

# =========================
# UI HELPERS
# =========================
def btn(
    text: str,
    callback_data: Optional[str] = None,
    url: Optional[str] = None,
    *,
    emoji_id: Optional[str] = None,
    style: Optional[str] = None,
) -> InlineKeyboardButton:

    kwargs: Dict[str, Any] = {"text": text}

    if callback_data is not None:
        kwargs["callback_data"] = callback_data

    if url is not None:
        kwargs["url"] = url

    if emoji_id is not None:
        kwargs["icon_custom_emoji_id"] = emoji_id

    if style is not None:
        kwargs["style"] = style

    return InlineKeyboardButton(**kwargs)


def sender_label(selected: str, current: str, base_text: str) -> str:
    return f"• {base_text}" if selected == current else base_text

def subs_keyboard(channels):

    rows=[]

    for cid,link in channels:

        rows.append([InlineKeyboardButton(
            text="📢 Подписаться",
            url=link
        )])

    rows.append([btn("✅ Проверить","check_subs")])

    return InlineKeyboardMarkup(inline_keyboard=rows)

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn(" Подарки", "menu_gifts", style="success", emoji_id = '5280615440928758599')],
            [btn(" Профиль", "profile", style="primary", emoji_id = '5364052602357044385')],
            [btn(" Информация", "information", style="default", emoji_id = '5220197908342648622')],
        ]
    )


def recipient_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn("⌵ Себе", "to_self", style="success"),
                btn("⌵ Другому", "to_other", style="success"),
            ],
            [btn(" Назад", "menu_gifts", style="danger", emoji_id = '5416113713428057601')],
        ]
    )


def comment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn("✦ Без текста", "comment:none", style="success"),
                btn("✦ лови подарок", "comment:preset1", style="success"),
            ],
            [
                btn("✦ отправлено через бота", "comment:preset2", style="success"),
                btn("✎ Свой текст", "comment:custom", style="primary"),
            ],
            [btn(" Назад", "gift:", style="danger", emoji_id = '5416113713428057601')],
        ]
    )


def confirm_keyboard(selected_sender: str) -> InlineKeyboardMarkup:
    pay_text = "Оплатить звёздами" if selected_sender == "bot" else f"<tg-emoji emoji-id='5280615440928758599'>🎁</tg-emoji> Отправить от @{ACCOUNT_USERNAME}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn(pay_text, "pay_now", style="primary", emoji_id = '5310224206732996002')],
            [btn("Назад", "back_comment", style="danger", emoji_id = '5416113713428057601')],
            [btn("✖ Отмена", "cancel", style="danger")],
        ]
    )

def information_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Назад", "back_main", style="default", emoji_id = '5416113713428057601')],
        ]
    )

def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("История", "profile_history", style="primary", emoji_id = '5220046725493828505')],
            [btn("Назад", "back_main", style="danger", emoji_id = '5416113713428057601')],
        ]
    )


def history_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Назад в профиль", "profile", style="primary", emoji_id = '5416113713428057601')],
            [btn("В меню", "back_main", style="danger", emoji_id = '5395831812704452001')],
        ]
    )

def history_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav_row = []

    if page > 1:
        nav_row.append(btn("⬅️", f"profile_history:{page-1}", style="primary"))

    nav_row.append(btn(f"{page}/{total_pages}", "noop", style="primary"))

    if page < total_pages:
        nav_row.append(btn("➡️", f"profile_history:{page+1}", style="primary"))

    rows = [nav_row] if nav_row else []

    rows.append([btn("Назад в профиль", "profile", style="primary", emoji_id = '5416113713428057601')])
    rows.append([btn("В меню", "back_main", style="danger", emoji_id = '5395831812704452001')])

    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Отправить подарок", "admin_send_gift", style="success", emoji_id = '5280615440928758599')],
            [btn("Отправить подарок без оплаты", "admin_send_free", style="success", emoji_id = '5280615440928758599')],
            [btn("Добавить подарок", "admin_add_gift", style="primary", emoji_id = '5397916757333654639')],
            [btn("Мои подарки", "admin_list_gifts", style="primary", emoji_id = '5258500400918587241')],
            [btn("Каналы подписки", "admin_channels", style="primary", emoji_id = '5395831812704452001')],
            [btn("Рассылка", "admin_broadcast", style="primary", emoji_id = '5370599459661045441')],
            [btn("Назад", "back_main", style="danger", emoji_id = '5416113713428057601')],
        ]
    )

def admin_send_comment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✦ Без текста", "admin_send_comment_none", style="success")],
            [btn("✎ Свой текст", "admin_send_comment_custom", style="primary")],
            [btn("⬅️ Назад", "admin_back", style="danger")],
        ]
    )

def admin_channels_keyboard():

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT id,title,is_main FROM required_channels")
    channels = cur.fetchall()

    conn.close()

    rows = []

    for cid,title,is_main in channels:

        icon = "⭐️" if is_main else "📢"

        rows.append([
            btn(f"{icon} {title}",f"channel_edit:{cid}"),
            btn("🗑",f"channel_delete:{cid}")
        ])

    rows.append([btn("Добавить канал","channel_add", emoji_id = '5397916757333654639')])
    rows.append([btn("Назад","admin_panel", emoji_id = '5416113713428057601')])

    return InlineKeyboardMarkup(inline_keyboard=rows)

def channel_edit_keyboard(channel_id):

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [btn("⭐️ Сделать основным",f"channel_main:{channel_id}")],

            [btn("✏️ Изменить название",f"channel_title:{channel_id}")],

            [btn("🔗 Изменить ссылку",f"channel_link:{channel_id}")],

            [btn("🗑 Удалить",f"channel_delete:{channel_id}")],

            [btn("Назад","admin_channels", emoji_id = '5416113713428057601')]
        ]
    )

def admin_sender_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn("От бота", "admin_sender:bot", style="success", emoji_id = '5355051922862653659'),
                btn(f"От @{ACCOUNT_USERNAME}", "admin_sender:account", style="primary", emoji_id = '5364052602357044385'),
            ],
            [btn("Назад", "admin_back", style="danger", emoji_id = '5416113713428057601')]
        ]
    )

def broadcast_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [btn("📄 Copy пост","bc_copy")],

            [btn("↪️ Forward пост","bc_forward")],

            [btn("✏️ Своя рассылка","broadcast_custom")],

            [btn("📊 Статистика","bc_stats")],

            [btn("Назад","admin_panel", emoji_id = '5416113713428057601')]
        ]
    )

def broadcast_builder_keyboard(state: dict) -> InlineKeyboardMarkup:
    text_status = "✅" if state.get("bc_text") else "❌"
    photo_status = "✅" if state.get("bc_photo_file_id") else "❌"
    button_status = "✅" if state.get("bc_button_text") and state.get("bc_button_url") else "❌"
    preview_status = "✅" if state.get("bc_preview", False) else "❌"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn(f"{text_status} 📝 Текст", "bc_edit_text", style="primary")],
            [btn(f"{photo_status} 🖼 Фото", "bc_edit_photo", style="primary")],
            [btn(f"{button_status} 🔘 Кнопка", "bc_edit_button", style="primary")],
            [btn(f"{preview_status} 👁 Превью ссылок", "bc_toggle_preview", style="success")],
            [btn("📨 Отправить рассылку", "bc_send_custom", style="success")],
            [btn("⬅️ Назад", "admin_broadcast", style="danger")],
        ]
    )

def build_broadcast_builder_text(state: dict) -> str:
    text_value = state.get("bc_text") or "не задан"
    photo_value = "добавлено" if state.get("bc_photo_file_id") else "нет"
    button_text = state.get("bc_button_text") or "нет"
    button_url = state.get("bc_button_url") or "нет"
    preview_value = "вкл" if state.get("bc_preview", False) else "выкл"

    return (
        "<b>✏️ Конструктор рассылки</b>\n\n"
        f"<b>Текст:</b> {text_value[:120] if text_value else 'не задан'}\n"
        f"<b>Фото:</b> {photo_value}\n"
        f"<b>Кнопка:</b> {button_text}\n"
        f"<b>Ссылка кнопки:</b> {button_url}\n"
        f"<b>Превью ссылок:</b> {preview_value}\n\n"
        "<blockquote>⌵ Выбери поле ниже, чтобы изменить его.</blockquote>"
    )

def admin_add_gift_keyboard(state: dict) -> InlineKeyboardMarkup:
    gift_id = state.get("new_gift_id", "не задан")
    title = state.get("new_gift_title", "не задано")
    price = state.get("new_gift_price", "не задана")
    emoji_id = state.get("new_gift_emoji", "не задан")
    pinned = "Да" if state.get("new_gift_pinned", False) else "Нет"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn(f"Gift ID: {gift_id}", "admin_edit_gift_id", style="primary", emoji_id = '5258466217273871977')],
            [btn(f"Название: {title}", "admin_edit_title", style="primary", emoji_id = '5364265065799239497')],
            [btn(f"Цена: {price}", "admin_edit_price", style="primary", emoji_id = '5310224206732996002')],
            [btn(f"Emoji ID: {emoji_id}", "admin_edit_emoji", style="primary", emoji_id = '5451694541063074067')],
            [btn(f"Закреп: {pinned}", "admin_toggle_pinned", style="success", emoji_id = '5397782960512444700')],
            [btn("Сохранить", "admin_save_gift", style="success", emoji_id = '5206607081334906820')],
            [btn("Отмена", "admin_back", style="danger", emoji_id = '5416113713428057601')],
        ]
    )

def admin_save_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Да, добавить", "admin_confirm_save_gift", style="success", emoji_id = '5206607081334906820')],
            [btn("Назад", "admin_add_gift", style="danger", emoji_id = '5416113713428057601')],
        ]
    )

def admin_gifts_keyboard() -> InlineKeyboardMarkup:
    rows = []

    for g in get_custom_gifts():
        pin_mark = "📌 " if g["is_pinned"] else ""
        rows.append([
            btn(f"{pin_mark}{g['title']}", f"admin_gift:{g['gift_id']}", style="success")
        ])

    rows.append([btn("➕ Добавить подарок", "admin_add_gift", style="primary")])
    rows.append([btn("Назад", "admin_back", style="danger", emoji_id = '5416113713428057601')])

    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_one_gift_keyboard(gift_id: str, is_pinned: bool) -> InlineKeyboardMarkup:
    pin_text = "📌 Открепить" if is_pinned else "📌 Закрепить"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn(pin_text, f"admin_pin:{gift_id}", style="primary")],
            [btn("🗑 Удалить", f"admin_delete:{gift_id}", style="danger")],
            [btn("⬅️ Назад к списку", "admin_list_gifts", style="primary")],
        ]
    )

def admin_comment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✎ Ввести комментарий", "admin_comment_custom", style="primary")],
            [btn("✦ Без текста", "admin_comment_none", style="success")],
            [btn("Назад", "admin_back", style="danger", emoji_id = '5416113713428057601')],
        ]
    )

def build_information_text() -> str:
    return (
        "<b>ℹ️ Информация о боте</b>\n\n"
        "<blockquote>"
        " <b><tg-emoji emoji-id='5280615440928758599'>🎁</tg-emoji> ShellGifts</b> — бот для покупки и отправки подарков в Telegram.\n\n"
        "⌵ Через этого бота можно выбрать подарок,\n"
        "⌵ указать получателя,\n"
        "⌵ добавить текст к подарку\n"
        "⌵ и отправить подарок от лица <b>бота</b>."
        "</blockquote>\n\n"
        "<b>📌 Возможности:</b>\n"
        "• выбор отправителя\n"
        "• список доступных подарков\n"
        "• отправка себе или другому человеку\n"
        "• оплата звёздами <tg-emoji emoji-id='5310224206732996002'>⭐</tg-emoji>\n\n"
        "<b>💻 Кодер:</b> <a href='https://t.me/nolyktg'><b>nolyk</b></a>\n"
        "<b>🛠 Версия:</b> <i>ShellGifts 1.0</i>\n\n"
        "<i>⌵ Используйте кнопки ниже для навигации.</i>"
    )

def build_admin_add_gift_text(state: dict) -> str:
    return (
        "<b>➕ Добавление подарка</b>\n\n"
        f"<b>Gift ID:</b> <code>{state.get('new_gift_id', 'не задан')}</code>\n"
        f"<b>Название:</b> {state.get('new_gift_title', 'не задано')}\n"
        f"<b>Цена:</b> {state.get('new_gift_price', 'не задана')} <tg-emoji emoji-id='5310224206732996002'>⭐</tg-emoji>\n"
        f"<b>Emoji ID:</b> <code>{state.get('new_gift_emoji', 'не задан')}</code>\n"
        f"<b>Закреплён:</b> {'Да' if state.get('new_gift_pinned', False) else 'Нет'}\n\n"
        "<blockquote>⌵ Нажми на нужное поле ниже, чтобы изменить его.</blockquote>"
    )

def get_api_gift_name(g: Dict[str, Any]) -> str:
    sticker = g.get("sticker", {}) or {}
    emoji = sticker.get("emoji", "")
    return f"{emoji} Подарок"


def merge_gifts() -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen_ids = set()

    for g in get_custom_gifts():
        merged.append({
            "id": str(g["gift_id"]),
            "title": g["title"],
            "star_count": g["star_count"],
            "emoji_id": g.get("emoji_id"),
            "source": "custom",
            "pinned": g["is_pinned"],
        })
        seen_ids.add(str(g["gift_id"]))

    # потом обычные из API
    for g in gift_cache:
        gid = str(g["id"])
        if gid in seen_ids:
            continue

        sticker = g.get("sticker", {}) or {}
        merged.append({
            "id": gid,
            "title": g.get("title") or "Подарок",
            "star_count": g.get("star_count", 0),
            "emoji_id": sticker.get("custom_emoji_id"),
            "source": "api",
            "pinned": False,
        })
        seen_ids.add(gid)

    return merged

def get_final_price(state: Dict[str, Any]) -> int:
    gift = gift_map.get(str(state["gift_id"]), {})
    base_price = int(gift.get("star_count", 0))

    is_admin_send = state.get("admin_send_mode", False)
    is_custom_comment = state.get("is_custom_comment", False)
    is_self = state.get("target_user_id") == state.get("from_user_id")

    extra = 0
    if not is_admin_send and is_custom_comment and not is_self:
        extra = 5

    return base_price + extra

def gifts_keyboard() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []

    custom_gifts = [g for g in merge_gifts() if g.get("source") == "custom"]
    api_gifts = [g for g in merge_gifts() if g.get("source") == "api"]

    def build_two_col_rows(items: List[Dict[str, Any]]):
        local_rows: List[List[InlineKeyboardButton]] = []
        row: List[InlineKeyboardButton] = []

        for g in items:
            button_text = f"{g['title']} · {g['star_count']} ★"
            row.append(
                btn(
                    text=button_text,
                    callback_data=f"gift:{g['id']}",
                    emoji_id=g.get("emoji_id"),
                    style="success",
                )
            )

            if len(row) == 2:
                local_rows.append(row)
                row = []

        if row:
            local_rows.append([row[0]])

        return local_rows

    if custom_gifts:
        rows.append([btn("Старые подарки", "noop", style="primary", emoji_id = '5397782960512444700')])
        rows.extend(build_two_col_rows(custom_gifts))

    if api_gifts:
        rows.append([btn("Обычные подарки", "noop", style="primary", emoji_id = '5280615440928758599')])
        rows.extend(build_two_col_rows(api_gifts))

    rows.append([btn("Обновить подарки", "refresh_gifts", style="primary", emoji_id = '5292226786229236118')])
    rows.append([btn("Назад", "back_main", style="danger", emoji_id = '5416113713428057601')])

    return InlineKeyboardMarkup(inline_keyboard=rows)

def build_profile_text(user: Message | CallbackQuery, stats: dict) -> str:
    u = user.from_user
    full_name = (u.full_name or "Неизвестно").strip()
    username = f"@{u.username}" if u.username else "нет username"

    return (
        "<b><tg-emoji emoji-id='5364052602357044385'>👤</tg-emoji> Профиль пользователя</b>\n\n"
        f"<b>ID:</b> <code>{u.id}</code>\n"
        f"<b>Username:</b> {username}\n"
        f"<b>Имя:</b> {full_name}\n\n"
        f"<b><tg-emoji emoji-id='5280615440928758599'>🎁</tg-emoji> Отправлено подарков:</b> {stats['total_sent']}\n"
        f"<b><tg-emoji emoji-id='5310224206732996002'><tg-emoji emoji-id='5310224206732996002'>⭐</tg-emoji></tg-emoji> Потрачено звёзд:</b> {stats['total_spent']}\n\n"
        "<blockquote>⌵ Ниже можно открыть историю отправок.</blockquote>"
    )

def build_history_text(user_id: int, page: int = 1, per_page: int = 5) -> str:
    total = get_user_history_count(user_id)

    if total == 0:
        return (
            "<b><tg-emoji emoji-id='5220046725493828505'>✍️</tg-emoji> История отправок</b>\n\n"
            "<i>История пока пустая.</i>"
        )

    offset = (page - 1) * per_page
    history = get_user_history(user_id, limit=per_page, offset=offset)

    lines = [
        "<b><tg-emoji emoji-id='5220046725493828505'>✍️</tg-emoji> История отправок</b>\n\n"
        f"<blockquote>⌵ Страница {page}</blockquote>"
    ]

    for i, item in enumerate(history, start=offset + 1):
        sender_name = "<tg-emoji emoji-id='5355051922862653659'>🤖</tg-emoji> Бот" if item["sender_type"] == "bot" else f"👤 @{ACCOUNT_USERNAME}"
        gift_text = item["gift_text"] if item["gift_text"] else "—"

        lines.append(
            f"\n<b>{i}.</b> {item['gift_title']}\n"
            f"├ <b>От:</b> {sender_name}\n"
            f"├ <b>Кому:</b> <code>{item['target_user_id']}</code>\n"
            f"├ <b>Цена:</b> {item['price_stars']} <tg-emoji emoji-id='5310224206732996002'>⭐</tg-emoji>\n"
            f"├ <b>Текст:</b> {gift_text}\n"
            f"└ <b>Дата:</b> {item['created_at']}\n"
        )

    return "".join(lines)

# =========================
# DATA HELPERS
# =========================

async def refresh_gifts_cache() -> int:
    global gift_cache, gift_map
    gifts = await get_available_gifts()
    gift_cache = gifts
    gift_map = {}

    for g in gifts:
        gid = str(g["id"])
        sticker = g.get("sticker", {}) or {}
        gift_map[gid] = {
            "id": gid,
            "title": get_api_gift_name(g),
            "star_count": g.get("star_count", 0),
            "emoji_id": sticker.get("custom_emoji_id"),
            "source": "api",
        }

    for g in get_custom_gifts():
        gid = str(g["gift_id"])
        gift_map[gid] = {
            "id": gid,
            "title": g["title"],
            "star_count": g["star_count"],
            "emoji_id": g.get("emoji_id"),
            "source": "custom",
            "pinned": g["is_pinned"],
        }

    return len(gifts)


def build_main_text() -> str:
    return (
        "<b><tg-emoji emoji-id='5280615440928758599'>🎁</tg-emoji> Добро пожаловать в ShellGifts!</b>\n\n"
        f"<tg-emoji emoji-id='5449683594425410231'>🔼</tg-emoji> <i>Этот бот предназначен для покупки подарков.</i>\n\n"
        "⌵ <b>Выберите отправителя выше,</b>\n"
        "⌵ затем нажмите <b>«Подарки»</b> и выберите нужный подарок.\n\n"
        "<tg-emoji emoji-id='5258500400918587241'>✍️</tg-emoji> <i>Дальше просто выберите получателя и подтвердите заказ.</i>"
    )


def build_summary(state: Dict[str, Any]) -> str:
    gift = gift_map.get(str(state["gift_id"]), {})
    title = gift.get("title", state["gift_id"])
    stars = get_final_price(state)

    sender_type = state.get("sender_type", "bot")
    sender_line =  "<tg-emoji emoji-id='5355051922862653659'>🤖</tg-emoji> <b>Бот</b>" if sender_type == "bot" else f"👤 <b>@{ACCOUNT_USERNAME}</b>"

    target = "себе" if state.get("target_user_id") == state.get("from_user_id") else str(state.get("target_user_id"))
    text = state.get("gift_text") or "—"

    extra_note = ""
    if state.get("is_custom_comment") and state.get("target_user_id") != state.get("from_user_id") and not state.get("admin_send_mode", False):
        extra_note = "\n<b>Надбавка за свой текст:</b> 5 <tg-emoji emoji-id='5310224206732996002'>"

    if state.get("admin_send_mode"):
       price_line = "<b>Оплата:</b> без invoice, с баланса бота"
    else:
       price_line = f"<b>Цена:</b> {stars} ⭐️" if sender_type == "bot" else "<b>Оплата:</b> со Stars аккаунта"

    return (
        "<b><tg-emoji emoji-id='5280615440928758599'>🎁</tg-emoji> Подтверждение заказа</b>\n\n"
        f"<b>Отправитель:</b> {sender_line}\n"
        f"<b>Подарок:</b> {title}\n"
        f"{price_line}\n"
        f"<b>Кому:</b> <code>{target}</code>\n"
        f"<b>Текст:</b> {text}\n\n"
        "<blockquote>⌵ Проверьте всё внимательно перед отправкой.</blockquote>"
    )
# =========================
# PAYMENTS
# =========================
async def create_stars_invoice(chat_id: int, state: Dict[str, Any], bot: Bot) -> None:
    gift = gift_map.get(str(state["gift_id"]))
    if not gift:
        raise RuntimeError("Подарок не найден")

    stars = get_final_price(state)
    if stars <= 0:
        raise RuntimeError("У подарка не найдена цена в звёздах")

    payload = f"gift:{chat_id}:{state['gift_id']}:{state['target_user_id']}"
    pending_invoices[payload] = {
        "chat_id": chat_id,
        "gift_id": str(state["gift_id"]),
        "target_user_id": int(state["target_user_id"]),
        "gift_text": state.get("gift_text", ""),
        "from_user_id": int(state["from_user_id"]),
        "sender_type": "bot",
        "is_custom_comment": state.get("is_custom_comment", False),
    }

    await bot.send_invoice(
        chat_id=chat_id,
        title=gift["title"][:32],
        description=f"Покупка подарка для {state['target_user_id']} ({stars} ⭐)",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=gift["title"][:32], amount=stars)],
    )

# =========================
# COMMANDS
# =========================
@router.message(CommandStart())
async def cmd_start(message: Message):
    save_user(message.from_user)

    not_subs = await check_subs(message.bot, message.from_user.id)

    if not_subs:
        await message.answer(
            "<b>⚠️ Подпишитесь на каналы чтобы пользоваться ботом</b>",
            parse_mode="HTML",
            reply_markup=subs_keyboard(not_subs)
        )
        return

    user_states[message.chat.id] = {
        "step": "main",
        "from_user_id": message.from_user.id,
        "sender_type": "bot",
    }

    await message.answer(
        build_main_text(),
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    user_states[message.chat.id] = {
        "step": "admin",
        "from_user_id": message.from_user.id,
        "sender_type": user_states.get(message.chat.id, {}).get("sender_type", "bot"),
    }

    await message.answer(
        "<b>🛠 Админ-панель</b>\n\n"
        "⌵ Здесь можно добавлять, закреплять и удалять подарки.",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )

@router.message(Command("reload_gifts"))
async def cmd_reload_gifts(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    count = await refresh_gifts_cache()
    await message.answer(f"✅ Обновил список подарков: {count}")

@router.message(F.photo)
async def photo_handler(message: Message):
    chat_id = message.chat.id
    state = user_states.get(chat_id)

    if not state:
        return

    step = state.get("step")

    if step == "broadcast_input_photo":
        photo = message.photo[-1]
        state["bc_photo_file_id"] = photo.file_id
        state["step"] = "broadcast_builder"

        await message.answer(
            build_broadcast_builder_text(state),
            parse_mode="HTML",
            reply_markup=broadcast_builder_keyboard(state),
            disable_web_page_preview=not state.get("bc_preview", False),
        )
        return

@router.message(Command("balance"))
async def cmd_balance(message: Message):
    try:
        bal = await get_my_star_balance()
        amount = bal.get("amount", 0)
        nano = bal.get("nanostar_amount", 0)
        await message.answer(
            f"<b><tg-emoji emoji-id='5310224206732996002'>⭐</tg-emoji> Баланс бота</b>\n\nStars: <b>{amount}</b>\nNanoStars: <b>{nano}</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка баланса: {e}")

# =========================
# CALLBACKS
# =========================
@router.callback_query()
async def callbacks(q: CallbackQuery, bot: Bot):
    chat_id = q.message.chat.id
    user_id = q.from_user.id
    data = q.data or ""
    state = user_states.setdefault(chat_id, {
        "step": "main",
        "from_user_id": user_id,
        "sender_type": "bot",
    })

    if data == "cancel":
        selected = state.get("sender_type", "bot")
        user_states[chat_id] = {
            "step": "main",
            "from_user_id": user_id,
            "sender_type": selected,
        }
        await q.message.edit_text(
            build_main_text(),
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        await q.answer()
        return

    if data == "back_main":
        selected = state.get("sender_type", "bot")
        user_states[chat_id] = {
            "step": "main",
            "from_user_id": user_id,
            "sender_type": selected,
        }
        await q.message.edit_text(
            build_main_text(),
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        await q.answer()
        return
    
    if data == "admin_channels":

        await q.message.edit_text(
           "<b>📡 Управление каналами</b>",
            parse_mode="HTML",
            reply_markup=admin_channels_keyboard()
        )
        return
    
    if data=="admin_broadcast":

        await q.message.edit_text(
            "<b>📢 Рассылка</b>",
            parse_mode="HTML",
            reply_markup=broadcast_keyboard()
        )

        return
    
    if data == "channel_add":
        state["step"] = "add_channel_chat_id"

        await q.message.edit_text(
            "<b>➕ Добавление канала</b>\n\n"
            "⌵ Отправь chat_id канала",
            parse_mode="HTML"
        )
        await q.answer()
        return

    if data == "menu_balance":
        try:
            bal = await get_my_star_balance()
            amount = bal.get("amount", 0)
            nano = bal.get("nanostar_amount", 0)
            await q.message.edit_text(
                f"<b><tg-emoji emoji-id='5310224206732996002'>⭐</tg-emoji> Баланс бота</b>\n\n"
                f"Stars: <b>{amount}</b>\n"
                f"NanoStars: <b>{nano}</b>\n\n"
                f"<blockquote>⌵ Баланс аккаунта @{ACCOUNT_USERNAME} ботом не читается.</blockquote>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[btn(" Назад", "back_main", style="danger", emoji_id = '5416113713428057601')]]
                ),
            )
        except Exception as e:
            await q.message.edit_text(
                f"❌ Ошибка баланса: {e}",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[btn(" Назад", "back_main", style="danger", emoji_id = '5416113713428057601')]]
                ),
            )
        await q.answer()
        return

    if data == "menu_gifts":
        try:
            if not gift_cache:
                await refresh_gifts_cache()
            await q.message.edit_text(
                "<b>🎁 Выберите подарок</b>\n\n<i>⌵ Ниже доступны закреплённые и текущие подарки.</i>",
                parse_mode="HTML",
                reply_markup=gifts_keyboard(),
            )
        except Exception as e:
            await q.message.edit_text(f"❌ Не удалось загрузить подарки: {e}")
        await q.answer()
        return
    
    if data == "information":
        try:
            await q.message.edit_text(
                build_information_text(),
                parse_mode="HTML",
                reply_markup=information_keyboard(),
            )
        except Exception as e:
            await q.message.edit_text(
                f"❌ Ошибка открытия информации: {e}",
                reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[btn(" Назад", "back_main", style="danger", emoji_id = '5416113713428057601')]]
            ),
            disable_web_page_preview=True
        )
        await q.answer()
        return
    
    if data == "profile":
        stats = get_user_stats(user_id)
        await q.message.edit_text(
            build_profile_text(q, stats),
            parse_mode="HTML",
            reply_markup=profile_keyboard(),
            disable_web_page_preview=True,
        )
        await q.answer()
        return

    if data == "profile_history":
        page = 1
        total = get_user_history_count(user_id)
        total_pages = max(1, (total + 5 - 1) // 5)

        await q.message.edit_text(
            build_history_text(user_id, page=page, per_page=5),
            parse_mode="HTML",
            reply_markup=history_keyboard(page, total_pages),
            disable_web_page_preview=True,
        )
        await q.answer()
        return

    if data.startswith("profile_history:"):
        page = int(data.split(":")[1])
        total = get_user_history_count(user_id)
        total_pages = max(1, (total + 5 - 1) // 5)

        if page < 1:
            page = 1
        if page > total_pages:
            page = total_pages

        await q.message.edit_text(
            build_history_text(user_id, page=page, per_page=5),
            parse_mode="HTML",
            reply_markup=history_keyboard(page, total_pages),
            disable_web_page_preview=True,
        ) 
        await q.answer()
        return
    
    if data.startswith("channel_delete"):

        cid = data.split(":")[1]

        conn = db_connect()
        cur = conn.cursor()

        cur.execute("DELETE FROM required_channels WHERE id=?",(cid,))
        conn.commit()
        conn.close()

        await q.message.edit_text(
            "🗑 Канал удалён",
            reply_markup=admin_channels_keyboard()
        )

        return

    if data == "refresh_gifts":
        try:
            count = await refresh_gifts_cache()
            await q.message.edit_text(
                f"<b>🎁 Подарки обновлены</b>\n\n"
                f"⌵ Текущих подарков из API: <b>{count}</b>\n"
                f"⌵ Старые закреплённые подарки находятся сверху.",
                parse_mode="HTML",
                reply_markup=gifts_keyboard(),
            )
        except Exception as e:
            await q.message.edit_text(f"❌ Ошибка обновления: {e}")
        await q.answer()
        return

    if data.startswith("gift:"):
        gift_id = data.split(":", 1)[1]
        state["gift_id"] = gift_id

        if state.get("admin_send_mode"):
            state["step"] = "admin_send_recipient"

            await q.message.edit_text(
                "<b>👤 Введите numeric user_id получателя</b>\n\n"
                "Пример: <code>123456789</code>",
                parse_mode="HTML",
            )
            await q.answer()
            return

        state["step"] = "recipient"
        await q.message.edit_text(
            "<b>👤 Кому отправить подарок?</b>\n\n"
            "⌵ <i>Можно отправить себе или другому человеку.</i>\n"
            "⌵ Для другого человека нужно будет ввести <b>numeric user_id</b>.",
            parse_mode="HTML",
            reply_markup=recipient_keyboard(),
        )
        await q.answer()
        return

    if data == "to_self":
        state["target_user_id"] = user_id
        state["step"] = "comment"
        await q.message.edit_text(
            "<b>💬 Выберите текст к подарку</b>",
            parse_mode="HTML",
            reply_markup=comment_keyboard(),
        )
        await q.answer()
        return

    if data == "to_other":
        state["step"] = "await_user_id"
        await q.message.edit_text(
            "<b>✍️ Введите numeric user_id получателя</b>\n\n"
            "Пример: <code>123456789</code>",
            parse_mode="HTML",
        )
        await q.answer()
        return
    
    if data == "broadcast_custom":
        state["step"] = "broadcast_builder"
        state["bc_text"] = state.get("bc_text", "")
        state["bc_photo_file_id"] = state.get("bc_photo_file_id", "")
        state["bc_button_text"] = state.get("bc_button_text", "")
        state["bc_button_url"] = state.get("bc_button_url", "")
        state["bc_preview"] = state.get("bc_preview", False)

        await q.message.edit_text(
            build_broadcast_builder_text(state),
            parse_mode="HTML",
            reply_markup=broadcast_builder_keyboard(state),
            disable_web_page_preview=not state.get("bc_preview", False),
        )
        await q.answer()
        return

    if data == "bc_edit_text":
        state["step"] = "broadcast_input_text"
        await q.message.edit_text(
            "<b>📝 Введи текст рассылки</b>\n\n"
            "<i>HTML поддерживается.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[btn("⬅️ Назад", "broadcast_custom", style="danger")]]
            ),
        )
        await q.answer()
        return

    if data == "bc_edit_photo":
        state["step"] = "broadcast_input_photo"
        await q.message.edit_text(
            "<b>🖼 Отправь фото для рассылки</b>\n\n"
            "<i>Или отправь <code>-</code>, чтобы убрать фото.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[btn("⬅️ Назад", "broadcast_custom", style="danger")]]
            ),
        )
        await q.answer()
        return

    if data == "bc_edit_button":
        state["step"] = "broadcast_input_button_text"
        await q.message.edit_text(
            "<b>🔘 Введи текст кнопки</b>\n\n"
            "<i>Или отправь <code>-</code>, чтобы убрать кнопку.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[btn("⬅️ Назад", "broadcast_custom", style="danger")]]
            ),
        )
        await q.answer()
        return

    if data == "bc_toggle_preview":
        state["bc_preview"] = not state.get("bc_preview", False)

        await q.message.edit_text(
            build_broadcast_builder_text(state),
            parse_mode="HTML",
            reply_markup=broadcast_builder_keyboard(state),
            disable_web_page_preview=not state.get("bc_preview", False),
        )
        await q.answer("Изменено")
        return


    if data == "admin_send_gift":
        if user_id != ADMIN_ID:
            await q.answer()
            return

        state["admin_send_mode"] = True
        state["step"] = "admin_choose_sender"
        state["sender_type"] = "bot"

        await q.message.edit_text(
        "<b>🎁 Админ-отправка</b>\n\n"
        "⌵ Выберите, от кого отправлять подарок.",
        parse_mode="HTML",
        reply_markup=admin_sender_keyboard(),
        )
        await q.answer()
        return

    if data.startswith("admin_sender:"):
        if user_id != ADMIN_ID:
            await q.answer()
            return

        sender = data.split(":", 1)[1]
        state["sender_type"] = sender
        state["admin_send_mode"] = True
        state["step"] = "admin_choose_gift"

        await q.message.edit_text(
            "<b>🎁 Выберите подарок</b>\n\n"
            f"⌵ Текущий отправитель: <b>{'бот' if sender == 'bot' else '@' + ACCOUNT_USERNAME}</b>",
            parse_mode="HTML",
            reply_markup=gifts_keyboard(),
        )
        await q.answer()
        return

    if data == "back_recipient":
        state["step"] = "recipient"
        await q.message.edit_text(
            "<b>👤 Кому отправить подарок?</b>",
            parse_mode="HTML",
            reply_markup=recipient_keyboard(),
        )
        await q.answer()
        return
    
    if data.startswith("gift:"):
        gift_id = data.split(":", 1)[1]
        state["gift_id"] = gift_id

        if state.get("admin_send_mode"):
            state["step"] = "admin_recipient"
            await q.message.edit_text(
                "<b>👤 Кому отправить подарок?</b>\n\n"
                "⌵ Введите numeric user_id получателя.",
                parse_mode="HTML",
            )
            await q.answer()
            return

        state["step"] = "recipient"
        await q.message.edit_text(
            "<b>👤 Кому отправить подарок?</b>\n\n"
            "⌵ <i>Можно отправить себе или другому человеку.</i>\n"
            "⌵ Для другого человека нужно будет ввести <b>numeric user_id</b>.",
            parse_mode="HTML",
            reply_markup=recipient_keyboard(),
        )
        await q.answer()
        return

    if data == "back_comment":
        state["step"] = "comment"
        await q.message.edit_text(
            "<b>💬 Выберите текст к подарку</b>",
            parse_mode="HTML",
            reply_markup=comment_keyboard(),
        )
        await q.answer()
        return

    if data == "comment:none":
        state["gift_text"] = ""
        state["is_custom_comment"] = False
        state["step"] = "confirm"
        await q.message.edit_text(
            build_summary(state),
            parse_mode="HTML",
            reply_markup=confirm_keyboard(state.get("sender_type", "bot")),
        )
        await q.answer()
        return

    if data == "comment:preset1":
        state["gift_text"] = "лови подарок"
        state["step"] = "confirm"
        state["is_custom_comment"] = False
        await q.message.edit_text(
            build_summary(state),
            parse_mode="HTML",
            reply_markup=confirm_keyboard(state.get("sender_type", "bot")),
        )
        await q.answer()
        return

    if data == "comment:preset2":
        state["gift_text"] = "отправлено через бота"
        state["is_custom_comment"] = False
        state["step"] = "confirm"
        await q.message.edit_text(
            build_summary(state),
            parse_mode="HTML",
            reply_markup=confirm_keyboard(state.get("sender_type", "bot")),
        )
        await q.answer()
        return

    if data == "comment:custom":
        state["step"] = "await_custom_text"
        state["is_custom_comment"] = True
        await q.message.edit_text(
            "<b>✍️ Напишите свой текст к подарку</b>\n\n"
            "<i>Максимум: 128 символов. За свой текст будет +5 <tg-emoji emoji-id='5310224206732996002'>⭐</tg-emoji>️.</i>",
            parse_mode="HTML",
        )
        await q.answer()
        return
    
    if data == "noop":
        await q.answer()
        return

    if data == "pay_now":
        try:
            sender_type = state.get("sender_type", "bot")
            is_admin_send = state.get("admin_send_mode", False)

            # АДМИН + БОТ => создаём счёт, после оплаты бот отправляет подарок
            if is_admin_send and sender_type == "bot":
                await create_stars_invoice(chat_id, state, bot)
                user_states[chat_id]["step"] = "waiting_payment"
                await q.answer("Счёт создан")
                return

            # АДМИН + АККАУНТ => сразу отправляем с аккаунта, без счёта
            if is_admin_send and sender_type == "account":
                if not USE_ACCOUNT_MODE:
                    raise RuntimeError("Режим аккаунта не включён")

                await q.message.edit_text(
                    f"<b>⏳ Выполняю заказ...</b>\n\n"
                    f"<blockquote>⌵ Отправляю подарок от аккаунта @{ACCOUNT_USERNAME}.</blockquote>",
                    parse_mode="HTML",
                )
 
                await send_gift_via_account(
                    user_id=int(state["target_user_id"]),
                    gift_id=str(state["gift_id"]),
                    text=state.get("gift_text", ""),
                )

                sent_gift = gift_map.get(str(state["gift_id"]), {})
                await q.message.edit_text(
                    f"<b>✅ Заказ выполнен</b>\n\n"
                    f"<blockquote>🎁 Подарок успешно отправлен от аккаунта @{ACCOUNT_USERNAME}.</blockquote>\n\n"
                    f"<b>Подарок:</b> {sent_gift.get('title', state['gift_id'])}\n"
                    f"<b>Кому:</b> <code>{state['target_user_id']}</code>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[btn("⬅️ В меню", "back_main", style="primary")]]
                    ),
                )
                await q.answer("Отправлено")
                return

            # Обычный пользователь или другой случай => создаём счёт
            await create_stars_invoice(chat_id, state, bot)
            user_states[chat_id]["step"] = "waiting_payment"
            await q.answer("Счёт создан")
            return

        except Exception as e:
            await q.message.edit_text(
                f"❌ <b>Ошибка</b>\n\n<code>{str(e)[:3500]}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[btn("⬅️ В меню", "back_main", style="danger")]]
                ),
            )
            await q.answer()
            return
        
    await q.answer()
    if data == "admin_back":
        if user_id != ADMIN_ID:
            await q.answer()
            return

        await q.message.edit_text(
            "<b>🛠 Админ-панель</b>\n\n⌵ Выберите действие.",
             parse_mode="HTML",
             reply_markup=admin_keyboard(),
        )
        await q.answer()
        return

    if data == "admin_add_gift":
        if user_id != ADMIN_ID:
            await q.answer()
            return

        state["step"] = "admin_add_gift_menu"
        state["new_gift_id"] = ""
        state["new_gift_title"] = ""
        state["new_gift_price"] = ""
        state["new_gift_emoji"] = ""
        state["new_gift_pinned"] = False

        await q.message.edit_text(
            build_admin_add_gift_text(state),
            parse_mode="HTML",
            reply_markup=admin_add_gift_keyboard(state),
        )
        await q.answer()
        return

    if data == "admin_list_gifts":
        if user_id != ADMIN_ID:
            await q.answer()
            return

        await q.message.edit_text(
            "<b>📋 Мои добавленные подарки</b>",
            parse_mode="HTML",
            reply_markup=admin_gifts_keyboard(),
        )
        await q.answer()
        return

    if data.startswith("admin_gift:"):
        if user_id != ADMIN_ID:
            await q.answer()
            return

        gift_id = data.split(":", 1)[1]
        g = get_custom_gift(gift_id)

        if not g:
            await q.answer("Подарок не найден")
            return

        pin_text = "Да" if g["is_pinned"] else "Нет"
        emoji_text = g["emoji_id"] or "—"

        await q.message.edit_text(
            "<b>🎁 Подарок</b>\n\n"
            f"<b>Название:</b> {g['title']}\n"
            f"<b>Gift ID:</b> <code>{g['gift_id']}</code>\n"
            f"<b>Цена:</b> {g['star_count']} <tg-emoji emoji-id='5310224206732996002'>⭐</tg-emoji>\n"
            f"<b>Emoji ID:</b> <code>{emoji_text}</code>\n"
            f"<b>Закреплён:</b> {pin_text}",
            parse_mode="HTML",
            reply_markup=admin_one_gift_keyboard(gift_id, g["is_pinned"]),
        )
        await q.answer()
        return

    if data.startswith("admin_pin:"):
        if user_id != ADMIN_ID:
            await q.answer()
            return

        gift_id = data.split(":", 1)[1]
        toggle_custom_gift_pin(gift_id)
        await refresh_gifts_cache()

        g = get_custom_gift(gift_id)
        await q.message.edit_text(
            "<b>🎁 Подарок обновлён</b>\n\n"
            f"<b>Название:</b> {g['title']}\n"
            f"<b>Gift ID:</b> <code>{g['gift_id']}</code>\n"
            f"<b>Цена:</b> {g['star_count']} <tg-emoji emoji-id='5310224206732996002'>⭐</tg-emoji>\n"
            f"<b>Закреплён:</b> {'Да' if g['is_pinned'] else 'Нет'}",
            parse_mode="HTML",
            reply_markup=admin_one_gift_keyboard(gift_id, g["is_pinned"]),
        )
        await q.answer("Статус закрепа изменён")
        return

    if data.startswith("admin_delete:"):
        if user_id != ADMIN_ID:
            await q.answer()
            return

        gift_id = data.split(":", 1)[1]
        delete_custom_gift(gift_id)
        await refresh_gifts_cache()

        await q.message.edit_text(
            "<b>🗑 Подарок удалён</b>",
            parse_mode="HTML",
             reply_markup=admin_gifts_keyboard(),
        )
        await q.answer("Удалено")
        return
    
    if data == "admin_edit_gift_id":
        state["step"] = "admin_input_gift_id"
        await q.message.edit_text(
            "<b>🆔 Введи Gift ID</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[btn("Назад", "admin_add_gift", style="danger", emoji_id = '5416113713428057601')]]
            ),
        )
        await q.answer()
        return

    if data == "admin_edit_title":
        state["step"] = "admin_input_title"
        await q.message.edit_text(
            "<b>🏷 Введи название подарка</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[btn("Назад", "admin_add_gift", style="danger", emoji_id = '5416113713428057601')]]
            ),
        )
        await q.answer()
        return

    if data == "admin_edit_price":
        state["step"] = "admin_input_price"
        await q.message.edit_text(
            "<b><tg-emoji emoji-id='5310224206732996002'>⭐</tg-emoji> Введи цену в звёздах</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[btn("Назад", "admin_add_gift", style="danger", emoji_id = '5416113713428057601')]]
            ),
        )
        await q.answer()
        return

    if data == "admin_edit_emoji":
        state["step"] = "admin_input_emoji"
        await q.message.edit_text(
            "<b>😀 Введи Emoji ID</b>\n\n<i>Если не нужен — отправь -</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[btn(" Назад", "admin_add_gift", style="danger", emoji_id = '5416113713428057601')]]
            ),
        )
        await q.answer()
        return

    if data == "admin_toggle_pinned":
        state["new_gift_pinned"] = not state.get("new_gift_pinned", False)

        await q.message.edit_text(
            build_admin_add_gift_text(state),
            parse_mode="HTML",
            reply_markup=admin_add_gift_keyboard(state),
        )
        await q.answer("Статус закрепа изменён")
        return    
    
    if data == "admin_save_gift":
        await q.message.edit_text(
            "<b>✅ Подтвердить добавление подарка?</b>\n\n"
            f"<b>Gift ID:</b> <code>{state.get('new_gift_id', '')}</code>\n"
            f"<b>Название:</b> {state.get('new_gift_title', '')}\n"
            f"<b>Цена:</b> {state.get('new_gift_price', '')} <tg-emoji emoji-id='5310224206732996002'>⭐</tg-emoji>\n"
            f"<b>Emoji ID:</b> <code>{state.get('new_gift_emoji', '') or '-'}</code>\n"
            f"<b>Закреплён:</b> {'Да' if state.get('new_gift_pinned', False) else 'Нет'}",
            parse_mode="HTML",
            reply_markup=admin_save_confirm_keyboard(),
        )
        await q.answer()
        return

    if data == "admin_confirm_save_gift":
        try:
            add_custom_gift(
                gift_id=state["new_gift_id"],
                title=state["new_gift_title"],
                star_count=int(state["new_gift_price"]),
                emoji_id=state["new_gift_emoji"],
                is_pinned=state.get("new_gift_pinned", False),
            )

            await refresh_gifts_cache()

            await q.message.edit_text(
                "<b>✅ Подарок добавлен</b>\n\n"
                f"<b>Название:</b> {state['new_gift_title']}\n"
                f"<b>Gift ID:</b> <code>{state['new_gift_id']}</code>\n"
                f"<b>Цена:</b> {state['new_gift_price']} <tg-emoji emoji-id='5310224206732996002'>⭐</tg-emoji>\n"
                f"<b>Закреплён:</b> {'Да' if state.get('new_gift_pinned', False) else 'Нет'}",
                parse_mode="HTML",
                reply_markup=admin_keyboard(),
            )

            user_states[chat_id]["step"] = "admin"

        except Exception as e:
            await q.message.edit_text(
                f"❌ Ошибка добавления:\n<code>{e}</code>",
                parse_mode="HTML",
                reply_markup=admin_keyboard(),
            )

        await q.answer()
        return    
    
    if data == "bc_send_custom":
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM bot_users")
        users = cur.fetchall()
        conn.close()

        sent = 0

        keyboard = None
        if state.get("bc_button_text") and state.get("bc_button_url"):
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=state["bc_button_text"], url=state["bc_button_url"])]
                ]
            )

        for (uid,) in users:
            try:
                if state.get("bc_photo_file_id"):
                    await bot.send_photo(
                        chat_id=uid,
                        photo=state["bc_photo_file_id"],
                        caption=state.get("bc_text", "") or " ",
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                else:
                    await bot.send_message(
                        chat_id=uid,
                        text=state.get("bc_text", "") or " ",
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        disable_web_page_preview=not state.get("bc_preview", False),
                    )
                sent += 1
            except:
                pass

        state["step"] = "main"

        await q.message.edit_text(
            f"<b>✅ Рассылка завершена</b>\n\n"
            f"<b>Отправлено:</b> {sent}",
            parse_mode="HTML",
            reply_markup=broadcast_keyboard(),
        )
        await q.answer()
        return

    if data == "check_subs":
        not_subs = await check_subs(bot, user_id)

        if not not_subs:
            user_states[chat_id] = {
                "step": "main",
                "from_user_id": user_id,
                "sender_type": "bot",
            }

            await q.message.edit_text(
                build_main_text(),
                parse_mode="HTML",
                reply_markup=main_keyboard()
            )
            await q.answer("✅ Подписка подтверждена")
            return

        await q.message.edit_text(
            "<b>⚠️ Вы ещё не подписались на все каналы</b>\n\n"
            "⌵ Подпишитесь и нажмите кнопку проверки ещё раз.",
            parse_mode="HTML",
            reply_markup=subs_keyboard(not_subs)
        )
        await q.answer("❌ Подписка не найдена", show_alert=True)
        return


    if data == "admin_comment_none":
       state["gift_text"] = ""
       state["step"] = "admin_confirm"

       await q.message.edit_text(
           build_summary(state),
           parse_mode="HTML",
           reply_markup=confirm_keyboard(state.get("sender_type", "bot")),
        )
       await q.answer()
       return

    if data == "admin_comment_custom":
       state["step"] = "admin_comment_input"
       await q.message.edit_text(
           "<b>✍️ Введите комментарий</b>\n\n"
           "<i>Для админа без наценки.</i>",
           parse_mode="HTML",
       )
       await q.answer()
       return
    
    if data == "bc_copy":
        state["step"] = "broadcast_copy_post"

        await q.message.edit_text(
            "<b>📄 Скопировать пост</b>\n\n"
            "⌵ Перешли сюда пост или сообщение, которое нужно разослать.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[btn("⬅️ Назад", "admin_broadcast", style="danger")]]
            ),
        )
        await q.answer()
        return

    if data == "bc_forward":
        state["step"] = "broadcast_forward_post"

        await q.message.edit_text(
            "<b>↪️ Переслать пост</b>\n\n"
            "⌵ Перешли сюда пост или сообщение, которое нужно разослать.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[btn("⬅️ Назад", "admin_broadcast", style="danger")]]
            ),
        )
        await q.answer()
        return
    
    if data == "admin_send_comment_none":
        state["gift_text"] = ""
        state["step"] = "admin_send_confirm"

        await q.message.edit_text(
            build_summary(state),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                 [btn("🎁 Отправить сейчас", "admin_send_now", style="success")],
                    [btn("⬅️ Назад", "admin_back", style="danger")],
                ]
         ),
     )
        await q.answer()
        return

    if data == "admin_send_comment_custom":
        state["step"] = "admin_send_comment_input"

        await q.message.edit_text(
            "<b>✍️ Напишите комментарий</b>\n\n"
            "<i>Максимум 128 символов</i>",
            parse_mode="HTML",
        )
        await q.answer()
        return

    if data == "bc_custom":
        user_states[chat_id]["step"] = "broadcast_text"
        await q.message.edit_text(
            "<b>✏️ Своя рассылка</b>\n\n"
            "⌵ Напиши текст\n\n"
            "HTML поддерживается",
            parse_mode="HTML"
        )
        return
    
    if data == "broadcast_send":
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM bot_users")
        users = cur.fetchall()
        conn.close()

        sent = 0
        text_to_send = state.get("broadcast_text", "")

        for (uid,) in users:
            try:
                await bot.send_message(
                    uid,
                    text_to_send,
                    parse_mode="HTML"
                )
                sent += 1
            except Exception:
                pass

        state["step"] = "main"

        await q.message.edit_text(
            f"✅ Рассылка завершена\n\nОтправлено: <b>{sent}</b>",
            parse_mode="HTML"
        )
        await q.answer()
        return
    
    if data == "admin_send_now":
        if user_id != ADMIN_ID:
            await q.answer()
            return

        try:
            await q.message.edit_text(
                "<b>⏳ Отправляю подарок...</b>",
                parse_mode="HTML",
            )

            await send_gift_via_bot(
                user_id=int(state["target_user_id"]),
                gift_id=str(state["gift_id"]),
                text=state.get("gift_text", ""),
                pay_for_upgrade=False,
            )

            sent_gift = gift_map.get(str(state["gift_id"]), {})

            await q.message.edit_text(
                f"<b>✅ Подарок отправлен</b>\n\n"
                f"<b>Подарок:</b> {sent_gift.get('title', state['gift_id'])}\n"
                f"<b>Кому:</b> <code>{state['target_user_id']}</code>\n"
                f"<b>Текст:</b> {state.get('gift_text') or '—'}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[btn("⬅️ В админку", "admin_back", style="primary")]]
                ),
            )

            add_history_record(
                user_id=int(state["from_user_id"]),
                username=q.from_user.username,
                full_name=q.from_user.full_name,
                sender_type="bot",
                gift_id=str(state["gift_id"]),
                gift_title=sent_gift.get("title", state["gift_id"]),
                target_user_id=int(state["target_user_id"]),
                gift_text=state.get("gift_text", ""),
                price_stars=int(sent_gift.get("star_count", 0)),
            )

            user_states[chat_id] = {
                "step": "admin",
                "from_user_id": user_id,
                "sender_type": "bot",
            }

            await q.answer("Отправлено")
            return

        except Exception as e:
            await q.message.edit_text(
                f"❌ <b>Ошибка отправки</b>\n\n<code>{str(e)[:3500]}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[btn("⬅️ В админку", "admin_back", style="danger")]]
                ),
            )
            await q.answer()
            return 
        

    if data == "admin_send_free":
        if user_id != ADMIN_ID:
            await q.answer()
            return

        state["admin_send_mode"] = True
        state["step"] = "admin_send_choose_gift"

        await q.message.edit_text(
            "<b>🎁 Отправка подарка без оплаты</b>\n\n"
            "⌵ Выберите подарок",
            parse_mode="HTML",
            reply_markup=gifts_keyboard(),
        )
        await q.answer()
        return 
    
# =========================
# PAYMENTS HANDLERS
# =========================
@router.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@router.message(lambda m: m.successful_payment is not None)
async def successful_payment_handler(message: Message):
    sp = message.successful_payment
    if not sp:
        return

    payload = sp.invoice_payload
    pending = pending_invoices.get(payload)
    if not pending:
        await message.answer("❌ Оплата прошла, но заказ не найден.")
        return

    try:
        await send_gift_via_bot(
            user_id=int(pending["target_user_id"]),
            gift_id=str(pending["gift_id"]),
            text=pending.get("gift_text", ""),
            pay_for_upgrade=False,
        )

        sent_gift = gift_map.get(str(pending["gift_id"]), {})
        await message.answer(
            f"<b>✅ Заказ выполнен</b>\n\n"
            f"<blockquote>🎁 Подарок успешно отправлен от бота.</blockquote>\n\n"
            f"<b>Подарок:</b> {sent_gift.get('title', pending['gift_id'])}\n"
            f"<b>Кому:</b> <code>{pending['target_user_id']}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[btn("⬅️ В меню", "back_main", style="primary")]]
            ),
        )

        gift_title = sent_gift.get("title", pending["gift_id"])
        price_stars = int(sent_gift.get("star_count", 0))

        add_history_record(
            user_id=int(pending["from_user_id"]),
            username=message.from_user.username,
            full_name=message.from_user.full_name,
            sender_type="bot",
            gift_id=str(pending["gift_id"]),
            gift_title=gift_title,
            target_user_id=int(pending["target_user_id"]),
            gift_text=pending.get("gift_text", ""),
            price_stars=price_stars,
        )
    except Exception as e:
        await message.answer(
            f"❌ Оплата прошла, но gift не отправился:\n<code>{str(e)[:3500]}</code>",
            parse_mode="HTML",
        )
    finally:
        pending_invoices.pop(payload, None)
        old_sender = user_states.get(message.chat.id, {}).get("sender_type", "bot")
        user_states[message.chat.id] = {
            "step": "main",
            "from_user_id": message.from_user.id,
            "sender_type": old_sender,
        }

# =========================
# MESSAGES
# =========================
@router.message()
async def any_message(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    state = user_states.get(chat_id)

    if not state:
        return

    step = state.get("step")
    text = (message.text or "").strip()
    print("STEP =", step, "| TEXT =", text)

    # =========================
    # USER ID
    # =========================
    if step == "await_user_id":
        if not text.isdigit():
            await message.answer(
                "❌ Нужен именно numeric user_id.\nПример: <code>123456789</code>",
                parse_mode="HTML",
            )
            return

        state["target_user_id"] = int(text)
        state["step"] = "comment"

        await message.answer(
            "<b>💬 Выберите текст к подарку</b>",
            parse_mode="HTML",
            reply_markup=comment_keyboard(),
        )
        return

    # =========================
    # CUSTOM TEXT
    # =========================
    if step == "await_custom_text":
        if len(text) > 128:
            await message.answer(
                "❌ Текст слишком длинный. Максимум 128 символов."
            )
            return

        state["gift_text"] = text
        state["step"] = "confirm"

        await message.answer(
            build_summary(state),
            parse_mode="HTML",
            reply_markup=confirm_keyboard(state.get("sender_type", "bot")),
        )
        return

    # =========================
    # ADMIN ADD GIFT
    # =========================
    if user_id == ADMIN_ID and step == "admin_add_gift_gift_id":
        state["new_gift_id"] = text
        state["step"] = "admin_add_gift_title"

        await message.answer(
            "<b>⌵ Введи название подарка</b>",
            parse_mode="HTML",
        )
        return
    
    if user_id == ADMIN_ID and step == "admin_send_recipient":
        if not text.isdigit():
            await message.answer(
                "❌ Нужен numeric user_id.\nПример: <code>123456789</code>",
                parse_mode="HTML",
            )
            return

        state["target_user_id"] = int(text)
        state["step"] = "admin_send_comment"

        await message.answer(
            "<b>💬 Комментарий к подарку</b>",
            parse_mode="HTML",
            reply_markup=admin_send_comment_keyboard(),
        )
        return
    
    if user_id == ADMIN_ID and step == "admin_send_comment_input":
        if len(text) > 128:
            await message.answer("❌ Текст слишком длинный. Максимум 128 символов.")
            return

        state["gift_text"] = text
        state["step"] = "admin_send_confirm"

        await message.answer(
            build_summary(state),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [btn("🎁 Отправить сейчас", "admin_send_now", style="success")],
                    [btn("⬅️ Назад", "admin_back", style="danger")],
                ]
            ),
        )
        return

    if step == "broadcast_input_photo" and text == "-":
        state["bc_photo_file_id"] = ""
        state["step"] = "broadcast_builder"

        await message.answer(
           build_broadcast_builder_text(state),
           parse_mode="HTML",
           reply_markup=broadcast_builder_keyboard(state),
           disable_web_page_preview=not state.get("bc_preview", False),
        )
        return
    
    if step == "broadcast_text":
        state["broadcast_text"] = text
        state["step"] = "broadcast_confirm"

        await message.answer(
            "<b>✅ Подтвердить рассылку?</b>\n\n"
            f"{text}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [btn("📢 Отправить", "broadcast_send", style="success")],
                    [btn("⬅️ Назад", "admin_broadcast", style="danger")]
                ]
            )
        )
        return
    
    if step == "broadcast_input_text":
        state["bc_text"] = text
        state["step"] = "broadcast_builder"

        await message.answer(
            build_broadcast_builder_text(state),
            parse_mode="HTML",
            reply_markup=broadcast_builder_keyboard(state),
            disable_web_page_preview=not state.get("bc_preview", False),
        )
        return

    if step == "broadcast_input_button_text":
        if text == "-":
            state["bc_button_text"] = ""
            state["bc_button_url"] = ""
            state["step"] = "broadcast_builder"

            await message.answer(
                build_broadcast_builder_text(state),
                parse_mode="HTML",
                reply_markup=broadcast_builder_keyboard(state),
                disable_web_page_preview=not state.get("bc_preview", False),
            )
            return

        state["bc_button_text"] = text
        state["step"] = "broadcast_input_button_url"

        await message.answer(
            "<b>🔗 Введи ссылку кнопки</b>\n\n"
            "Пример: <code>https://t.me/yourbot</code>",
            parse_mode="HTML",
        )
        return

    if step == "broadcast_input_button_url":
        state["bc_button_url"] = text
        state["step"] = "broadcast_builder"

        await message.answer(
            build_broadcast_builder_text(state),
            parse_mode="HTML",
            reply_markup=broadcast_builder_keyboard(state),
            disable_web_page_preview=not state.get("bc_preview", False),
        )
        return
    
    if step == "broadcast_copy_post":
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM bot_users")
        users = cur.fetchall()
        conn.close()

        sent = 0

        for (uid,) in users:
            try:
                await message.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
                sent += 1
            except:
                pass

        state["step"] = "main"

        await message.answer(
            f"<b>✅ Copy-рассылка завершена</b>\n\n"
            f"<b>Отправлено:</b> {sent}",
            parse_mode="HTML",
            reply_markup=broadcast_keyboard(),
        )
        return

    if step == "broadcast_forward_post":
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM bot_users")
        users = cur.fetchall()
        conn.close()

        sent = 0

        for (uid,) in users:
            try:
                await message.bot.forward_message(
                    chat_id=uid,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
                sent += 1
            except:
                pass

        state["step"] = "main"

        await message.answer(
            f"<b>✅ Forward-рассылка завершена</b>\n\n"
            f"<b>Отправлено:</b> {sent}",
            parse_mode="HTML",
            reply_markup=broadcast_keyboard(),
        )
        return

    if step == "add_channel_chat_id":
        state["channel_chat_id"] = text
        state["step"] = "add_channel_title"
        await message.answer(
            "<b>⌵ Введи название канала</b>",
            parse_mode="HTML"
        )
        return

    if step == "add_channel_title":
        state["channel_title"] = text
        state["step"] = "add_channel_link"
        await message.answer(
            "<b>⌵ Отправь ссылку канала</b>\n\n"
            "Пример: <code>https://t.me/testchannel</code>",
            parse_mode="HTML"
        )
        return

    if step == "add_channel_link":
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO required_channels (chat_id, title, invite_link, is_enabled)
            VALUES (?, ?, ?, 1)
            """,
            (
                state["channel_chat_id"],
                state["channel_title"],
                text
            )
        )
        conn.commit()
        conn.close()
        state["step"] = "main"
        await message.answer(
            "✅ Канал добавлен",
            parse_mode="HTML",
            reply_markup=admin_channels_keyboard()
        )
        return      
    
    if step == "broadcast_copy":
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users")
        users = cur.fetchall()
        conn.close()

        sent = 0
        for (uid,) in users:
            try:
                await message.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
                sent += 1
            except Exception:
                pass

        await message.answer(f"📢 Отправлено: {sent}")
        user_states[chat_id]["step"] = "main"
        return

    if user_id == ADMIN_ID and step == "admin_add_gift_title":
        state["new_gift_title"] = text
        state["step"] = "admin_add_gift_price"

        await message.answer(
            "<b>⌵ Введи цену в звёздах</b>\n\nПример: <code>50</code>",
            parse_mode="HTML",
        )
        return

    if user_id == ADMIN_ID and step == "admin_add_gift_price":
        if not text.isdigit():
            await message.answer("❌ Цена должна быть числом.")
            return

        state["new_gift_price"] = int(text)
        state["step"] = "admin_add_gift_emoji"

        await message.answer(
            "<b>⌵ Введи emoji_id</b>\n\n"
            "Если не нужен — отправь <code>-</code>",
            parse_mode="HTML",
        )
        return

    if user_id == ADMIN_ID and step == "admin_add_gift_emoji":
        state["new_gift_emoji"] = "" if text == "-" else text
        state["step"] = "admin_add_gift_pinned"

        await message.answer(
            "<b>⌵ Закрепить подарок сверху?</b>\n\n"
            "Напиши: <code>да</code> или <code>нет</code>",
            parse_mode="HTML",
        )
        return

    if user_id == ADMIN_ID and step == "admin_add_gift_pinned":
        is_pinned = text.lower() in ("да", "yes", "1", "true")

        try:
            add_custom_gift(
                gift_id=state["new_gift_id"],
                title=state["new_gift_title"],
                star_count=state["new_gift_price"],
                emoji_id=state["new_gift_emoji"],
                is_pinned=is_pinned,
            )

            await refresh_gifts_cache()

            await message.answer(
                "<b>✅ Подарок добавлен</b>\n\n"
                f"<b>Название:</b> {state['new_gift_title']}\n"
                f"<b>Gift ID:</b> <code>{state['new_gift_id']}</code>\n"
                f"<b>Цена:</b> {state['new_gift_price']} <tg-emoji emoji-id='5310224206732996002'>⭐</tg-emoji>\n"
                f"<b>Закреплён:</b> {'Да' if is_pinned else 'Нет'}",
                parse_mode="HTML",
                reply_markup=admin_keyboard(),
            )

        except Exception as e:
            await message.answer(f"❌ Ошибка добавления: {e}")

        user_states[chat_id]["step"] = "admin"
        return
    
    if user_id == ADMIN_ID and step == "admin_input_gift_id":
        state["new_gift_id"] = text
        state["step"] = "admin_add_gift_menu"
        await message.answer(
            build_admin_add_gift_text(state),
            parse_mode="HTML",
            reply_markup=admin_add_gift_keyboard(state),
        )
        return

    if user_id == ADMIN_ID and step == "admin_input_title":
        state["new_gift_title"] = text
        state["step"] = "admin_add_gift_menu"
        await message.answer(
            build_admin_add_gift_text(state),
            parse_mode="HTML",
            reply_markup=admin_add_gift_keyboard(state),
        )
        return

    if user_id == ADMIN_ID and step == "admin_input_price":
        if not text.isdigit():
            await message.answer("❌ Цена должна быть числом.")
            return

        state["new_gift_price"] = text
        state["step"] = "admin_add_gift_menu"
        await message.answer(
            build_admin_add_gift_text(state),
            parse_mode="HTML",
            reply_markup=admin_add_gift_keyboard(state),
        )
        return

    if user_id == ADMIN_ID and step == "admin_input_emoji":
        state["new_gift_emoji"] = "" if text == "-" else text
        state["step"] = "admin_add_gift_menu"
        await message.answer(
            build_admin_add_gift_text(state),
            parse_mode="HTML",
            reply_markup=admin_add_gift_keyboard(state),
        )
        return
    
    if user_id == ADMIN_ID and step == "admin_comment_input":
        if len(text) > 128:
            await message.answer("❌ Текст слишком длинный. Максимум 128 символов.")
            return

        state["gift_text"] = text
        state["step"] = "admin_confirm"

        await message.answer(
            build_summary(state),
            parse_mode="HTML",
            reply_markup=confirm_keyboard(state.get("sender_type", "bot")),
        )
        return
    


# =========================
# STARTUP
# =========================
async def on_startup(bot: Bot):
    try:
        count = await refresh_gifts_cache()
        logger.info("Loaded gifts from API: %s", count)
    except Exception as e:
        logger.warning("Could not preload gifts: %s", e)

    me = await bot.get_me()
    logger.info("Bot started: @%s (%s)", me.username, me.id)

    if USE_ACCOUNT_MODE:
        logger.info("Account mode enabled: @%s", ACCOUNT_USERNAME)


async def main():
    if BOT_TOKEN == "PUT_NEW_BOT_TOKEN_HERE":
        raise RuntimeError("Поставь новый BOT_TOKEN в env или в код.")
    
    init_db()

    if USE_ACCOUNT_MODE:
        await start_account_client()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    await on_startup(bot)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
