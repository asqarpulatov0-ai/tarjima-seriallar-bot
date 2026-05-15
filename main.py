import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, Optional
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType
from aiogram.types import (
    Message, CallbackQuery, BotCommand,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
import aiosqlite

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN not set!")
    sys.exit(1)

DB_PATH = os.path.join(os.path.dirname(__file__), "serials.db")
PER_PAGE = 8

# ─── Database ─────────────────────────────────────────────────────────────────

CREATE_TABLES = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS serials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    serial_id INTEGER NOT NULL REFERENCES serials(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    channel_id INTEGER DEFAULT NULL,
    message_id INTEGER DEFAULT NULL,
    UNIQUE(serial_id, number)
);
CREATE TABLE IF NOT EXISTS fsm_storage (
    key TEXT PRIMARY KEY,
    state TEXT DEFAULT NULL,
    data TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_serials_name ON serials(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_episodes_serial ON episodes(serial_id);
"""

# ─── SQLite FSM Storage ──────────────────────────────────────────────────────

class SQLiteStorage(BaseStorage):
    def __init__(self, db_path: str):
        self._db_path = db_path

    def _key(self, key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}"

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        k = self._key(key)
        state_str = state.state if hasattr(state, "state") else (state or None)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO fsm_storage(key, state) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET state=excluded.state",
                (k, state_str)
            )
            await db.commit()

    async def get_state(self, key: StorageKey) -> Optional[str]:
        k = self._key(key)
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT state FROM fsm_storage WHERE key=?", (k,)) as cur:
                row = await cur.fetchone()
                return row[0] if row else None

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        k = self._key(key)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO fsm_storage(key, data) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (k, json.dumps(data))
            )
            await db.commit()

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        k = self._key(key)
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT data FROM fsm_storage WHERE key=?", (k,)) as cur:
                row = await cur.fetchone()
                return json.loads(row[0]) if row and row[0] else {}

    async def close(self) -> None:
        pass

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES)
        await db.commit()

async def get_or_create_serial(name: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO serials(name) VALUES(?)", (name,))
        await db.commit()
        async with db.execute("SELECT id FROM serials WHERE name=?", (name,)) as cur:
            row = await cur.fetchone()
            return row[0]

async def get_serial(serial_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, name FROM serials WHERE id=?", (serial_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def delete_serial(serial_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM serials WHERE id=?", (serial_id,))
        await db.commit()

async def get_serials_count():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM serials") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def get_serials_page(offset=0, limit=8):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT s.id, s.name,
               (SELECT COUNT(*) FROM episodes WHERE serial_id=s.id) as ep_count
               FROM serials s ORDER BY s.name LIMIT ? OFFSET ?""",
            (limit, offset)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def search_serials(query: str, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT s.id, s.name,
               (SELECT COUNT(*) FROM episodes WHERE serial_id=s.id) as ep_count
               FROM serials s WHERE s.name LIKE ? COLLATE NOCASE ORDER BY s.name LIMIT ?""",
            (f"%{query}%", limit)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def get_episodes(serial_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, serial_id, number, file_id, channel_id, message_id FROM episodes WHERE serial_id=? ORDER BY number",
            (serial_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def get_episode(episode_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT e.id, e.number, e.file_id, e.channel_id, e.message_id,
                      e.serial_id, s.name as serial_name
               FROM episodes e
               JOIN serials s ON e.serial_id = s.id
               WHERE e.id=?""",
            (episode_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def add_episode(serial_id: int, number: int, file_id: str,
                      channel_id=None, message_id=None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR REPLACE INTO episodes(serial_id, number, file_id, channel_id, message_id) VALUES(?,?,?,?,?)",
            (serial_id, number, file_id, channel_id, message_id)
        )
        await db.commit()
        return cur.lastrowid

async def add_next_episode(serial_id: int, file_id: str,
                           channel_id=None, message_id=None) -> int:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM episodes WHERE serial_id=?",
                (serial_id,)
            ) as cur:
                next_num = (await cur.fetchone())[0]
            await db.execute(
                "INSERT INTO episodes(serial_id, number, file_id, channel_id, message_id) VALUES(?,?,?,?,?)",
                (serial_id, next_num, file_id, channel_id, message_id)
            )
            await db.commit()
            return next_num
        except Exception as e:
            await db.rollback()
            raise e

async def delete_episode(episode_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM episodes WHERE id=?", (episode_id,))
        await db.commit()

async def delete_all_serials():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM serials")
        await db.commit()

async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM serials") as cur:
            serials = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM episodes") as cur:
            episodes = (await cur.fetchone())[0]
        return {"serials": serials, "episodes": episodes}

# ─── Keyboards ────────────────────────────────────────────────────────────────

def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔎 Qidiruv"), KeyboardButton(text="📺 Seriallar")]],
        resize_keyboard=True, persistent=True
    )

def serials_kb(serials, page, total):
    buttons = []
    for s in serials:
        ep = s['ep_count']
        buttons.append([
            InlineKeyboardButton(
                text=f"📺 {s['name']} ({ep} qism)",
                callback_data=f"serial:{s['id']}"
            )
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Oldingi", callback_data=f"pg:{page-1}"))
    total_pages = (total + PER_PAGE - 1) // PER_PAGE
    nav.append(InlineKeyboardButton(text=f"📄 {page+1}/{total_pages}", callback_data="noop"))
    if (page + 1) * PER_PAGE < total:
        nav.append(InlineKeyboardButton(text="Keyingi ▶️", callback_data=f"pg:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔎 Qidiruv", callback_data="search")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def episodes_kb(serial, episodes):
    buttons = []
    row = []
    for e in episodes:
        row.append(InlineKeyboardButton(
            text=f"{e['number']}",
            callback_data=f"ep:{e['id']}"
        ))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="pg:0")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def search_results_kb(serials):
    buttons = []
    for s in serials:
        ep = s['ep_count']
        buttons.append([
            InlineKeyboardButton(
                text=f"📺 {s['name']} ({ep} qism)",
                callback_data=f"serial:{s['id']}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="◀️ Bosh menu", callback_data="close")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def back_to_serial_kb(serial_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Qismlarga", callback_data=f"serial:{serial_id}")],
        [InlineKeyboardButton(text="🏠 Seriallar", callback_data="pg:0")]
    ])

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Bekor qilish", callback_data="close")
    ]])

def confirm_delete_kb(serial_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Ha, o'chirish", callback_data=f"del_confirm:{serial_id}")],
        [InlineKeyboardButton(text="❌ Yo'q", callback_data="close")]
    ])

def del_episodes_kb(episodes, serial_id):
    buttons = []
    row = []
    for e in episodes:
        row.append(InlineKeyboardButton(
            text=f"🗑 {e['number']}",
            callback_data=f"delep:{e['id']}"
        ))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="close")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def confirm_clearall_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Ha, hammasini o'chir", callback_data="clearall_confirm")],
        [InlineKeyboardButton(text="❌ Yo'q", callback_data="close")]
    ])

# ─── Bot Setup ────────────────────────────────────────────────────────────────

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=SQLiteStorage(DB_PATH))

_upload_locks: dict[int, asyncio.Lock] = {}

def get_upload_lock(user_id: int) -> asyncio.Lock:
    if user_id not in _upload_locks:
        _upload_locks[user_id] = asyncio.Lock()
    return _upload_locks[user_id]

def is_admin(user_id: int) -> bool:
    env = os.environ.get("ADMIN_IDS", "")
    ids = {int(x) for x in env.split(",") if x.strip().isdigit()}
    return user_id in ids

async def safe_edit(msg, text, markup=None):
    try:
        await msg.edit_text(text, reply_markup=markup)
    except Exception:
        await msg.answer(text, reply_markup=markup)

# ─── States ───────────────────────────────────────────────────────────────────

class Search(StatesGroup):
    waiting = State()

class Upload(StatesGroup):
    serial_name = State()
    videos = State()

# ─── User Handlers ────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    name = message.from_user.first_name or "Do'st"
    await message.answer(
        f"👋 Salom, <b>{name}</b>!\n\n"
        f"Bu bot orqali sevimli seriallaringizni tomosha qilishingiz mumkin.\n\n"
        f"📌 <b>Nima qilmoqchisiz?</b>",
        reply_markup=main_menu()
    )

async def show_page(target, page=0):
    total = await get_serials_count()
    if total == 0:
        text = "📭 Hozircha serial yo'q."
        markup = None
    else:
        serials = await get_serials_page(offset=page * PER_PAGE, limit=PER_PAGE)
        text = f"📺 <b>Seriallar ro'yxati</b> — jami <b>{total} ta</b>"
        markup = serials_kb(serials, page, total)
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        await safe_edit(target.message, text, markup)

@dp.message(F.text == "📺 Seriallar")
async def btn_serials(message: Message, state: FSMContext):
    await state.clear()
    await show_page(message)

@dp.callback_query(F.data.startswith("pg:"))
async def cb_page(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_page(call, int(call.data.split(":")[1]))
    await call.answer()

async def send_episodes_task(chat_id: int, serial: dict, episodes: list):
    failed = 0
    for ep in episodes:
        caption = f"📺 <b>{serial['name']}</b> — {ep['number']}-qism"
        for attempt in range(3):
            try:
                if ep.get('channel_id') and ep.get('message_id'):
                    await bot.forward_message(
                        chat_id=chat_id,
                        from_chat_id=ep['channel_id'],
                        message_id=ep['message_id']
                    )
                else:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=ep['file_id'],
                        caption=caption,
                        supports_streaming=True
                    )
                break
            except TelegramRetryAfter as e:
                wait = e.retry_after + 1
                logger.warning(f"Flood control: {wait}s kutilmoqda ({ep['number']}-qism)")
                await asyncio.sleep(wait)
            except TelegramForbiddenError:
                logger.warning(f"Foydalanuvchi botni bloklagan, yuborish to'xtatildi")
                return
            except Exception as e:
                logger.error(f"Video yuborishda xato ({ep['number']}-qism): {e}")
                failed += 1
                break
        await asyncio.sleep(0.4)

    try:
        if failed:
            await bot.send_message(
                chat_id,
                f"⚠️ {len(episodes) - failed} ta qism yuborildi, {failed} ta xatolik yuz berdi.",
                reply_markup=main_menu()
            )
        else:
            await bot.send_message(
                chat_id,
                f"✅ <b>{serial['name']}</b> — barcha <b>{len(episodes)} ta qism</b> yuborildi!",
                reply_markup=main_menu()
            )
    except Exception as e:
        logger.error(f"Yakuniy xabar yuborishda xato: {e}")

@dp.callback_query(F.data.startswith("serial:"))
async def cb_serial(call: CallbackQuery):
    serial_id = int(call.data.split(":")[1])
    serial = await get_serial(serial_id)
    if not serial:
        await call.answer("❌ Topilmadi!", show_alert=True)
        return
    episodes = await get_episodes(serial_id)
    if not episodes:
        await safe_edit(
            call.message,
            f"📺 <b>{serial['name']}</b>\n\n⚠️ Hali qismlar qo'shilmagan.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Orqaga", callback_data="pg:0")]
            ])
        )
        await call.answer()
        return

    await call.answer("⏳ Yuklanmoqda...")
    await safe_edit(
        call.message,
        f"📺 <b>{serial['name']}</b>\n\n⏳ <b>{len(episodes)} ta qism</b> yuborilmoqda...\n"
        f"<i>Iltimos kuting, videolar ketma-ket yuboriladi.</i>"
    )
    asyncio.create_task(send_episodes_task(call.message.chat.id, serial, episodes))

@dp.callback_query(F.data.startswith("ep:"))
async def cb_episode(call: CallbackQuery):
    ep = await get_episode(int(call.data.split(":")[1]))
    if not ep:
        await call.answer("❌ Topilmadi!", show_alert=True)
        return
    caption = f"📺 <b>{ep['serial_name']}</b> — {ep['number']}-qism"
    await call.answer("⏳ Yuklanmoqda...")
    try:
        if ep.get('channel_id') and ep.get('message_id'):
            await bot.forward_message(
                chat_id=call.message.chat.id,
                from_chat_id=ep['channel_id'],
                message_id=ep['message_id']
            )
        else:
            await bot.send_video(
                chat_id=call.message.chat.id,
                video=ep['file_id'],
                caption=caption,
                supports_streaming=True,
                reply_markup=back_to_serial_kb(ep['serial_id'])
            )
    except Exception as e:
        logger.error(f"Video yuborishda xato: {e}")
        await call.message.answer(
            f"❌ Video yuborishda xato:\n<code>{e}</code>",
            reply_markup=back_to_serial_kb(ep['serial_id'])
        )

@dp.message(F.text == "🔎 Qidiruv")
async def btn_search(message: Message, state: FSMContext):
    await state.set_state(Search.waiting)
    await message.answer(
        "🔎 <b>Serial nomini kiriting:</b>\n"
        "<i>/cancel — bekor qilish</i>",
        reply_markup=cancel_kb()
    )

@dp.callback_query(F.data == "search")
async def cb_search(call: CallbackQuery, state: FSMContext):
    await state.set_state(Search.waiting)
    await call.message.answer(
        "🔎 <b>Serial nomini kiriting:</b>\n"
        "<i>/cancel — bekor qilish</i>",
        reply_markup=cancel_kb()
    )
    await call.answer()

@dp.message(StateFilter(Search.waiting))
async def do_search(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    if not query:
        return
    await state.clear()
    results = await search_serials(query)
    if not results:
        await message.answer(
            f"🔍 <b>«{query}»</b> topilmadi.",
            reply_markup=main_menu()
        )
        return
    await message.answer(
        f"🔍 <b>{len(results)} ta natija:</b>",
        reply_markup=search_results_kb(results)
    )

@dp.callback_query(F.data == "close")
async def cb_close(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()

@dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()

# ─── Admin: Serial qo'shish (/add) ────────────────────────────────────────────

@dp.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q.")
        return
    await state.set_state(Upload.serial_name)
    await message.answer(
        "📝 <b>Serial nomini kiriting:</b>\n"
        "<i>/cancel — bekor qilish</i>"
    )

@dp.message(StateFilter(Upload.serial_name))
async def upload_serial_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Nom bo'sh bo'lmasin!")
        return
    serial_id = await get_or_create_serial(name)
    await state.set_state(Upload.videos)
    await state.update_data(serial_id=serial_id, serial_name=name, count=0)
    existing = await get_episodes(serial_id)
    next_num = (max(e['number'] for e in existing) + 1) if existing else 1
    await message.answer(
        f"✅ <b>{name}</b>\n\n"
        f"📹 Endi videolarni birin-ketin yuboring!\n"
        f"▶️ Birinchi qism raqami: <b>{next_num}</b>\n\n"
        f"<i>Tugatish uchun /done yozing</i>"
    )

@dp.message(StateFilter(Upload.videos), F.video)
async def upload_video(message: Message, state: FSMContext):
    lock = get_upload_lock(message.from_user.id)
    async with lock:
        try:
            data = await state.get_data()
            file_id = message.video.file_id
            size_mb = (message.video.file_size or 0) / 1024 / 1024
            num = await add_next_episode(data['serial_id'], file_id)
            await state.update_data(count=data.get('count', 0) + 1)
            await message.answer(
                f"✅ <b>{num}-qism</b> saqlandi ({size_mb:.1f} MB)\n"
                f"<i>Tugatish: /done</i>"
            )
        except Exception as e:
            logger.error(f"Video saqlashda xato: {e}")
            await message.answer(f"❌ Xato: {str(e)[:100]}\nQaytadan urinib ko'ring.")

@dp.message(StateFilter(Upload.videos), Command("done"))
async def upload_done(message: Message, state: FSMContext):
    data = await state.get_data()
    serial_id = data['serial_id']
    name = data['serial_name']
    await state.clear()
    total = len(await get_episodes(serial_id))
    await message.answer(
        f"🎉 <b>Yuklash tugadi!</b>\n\n"
        f"📺 <b>{name}</b>\n"
        f"✅ Jami <b>{total} ta qism</b> saqlandi!\n\n"
        f"Yana qo'shish: /add",
        reply_markup=main_menu()
    )

@dp.message(StateFilter(Upload.videos))
async def upload_wrong(message: Message):
    if message.text and message.text.startswith("/"):
        return
    await message.answer("❌ Faqat <b>video</b> yuboring!\n<i>Tugatish: /done</i>")

# ─── Admin: Serial o'chirish (/delete) ────────────────────────────────────────

@dp.message(Command("delete"))
async def cmd_delete(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q.")
        return
    args = message.text.split(None, 1)
    if len(args) < 2:
        await message.answer("❌ Foydalanish: /delete <serial nomi>")
        return
    query = args[1].strip()
    results = await search_serials(query, limit=5)
    if not results:
        await message.answer(f"❌ <b>«{query}»</b> topilmadi.")
        return
    buttons = []
    for s in results:
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {s['name']} ({s['ep_count']} qism)",
            callback_data=f"del_ask:{s['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="❌ Bekor", callback_data="close")])
    await message.answer("O'chirmoqchi bo'lgan serialni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("del_ask:"))
async def cb_del_ask(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    serial_id = int(call.data.split(":")[1])
    serial = await get_serial(serial_id)
    if not serial:
        await call.answer("❌ Topilmadi!", show_alert=True)
        return
    await safe_edit(
        call.message,
        f"⚠️ <b>{serial['name']}</b> ni barcha qismlari bilan o'chirishni tasdiqlaysizmi?",
        confirm_delete_kb(serial_id)
    )
    await call.answer()

@dp.callback_query(F.data.startswith("del_confirm:"))
async def cb_del_confirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    serial_id = int(call.data.split(":")[1])
    serial = await get_serial(serial_id)
    if not serial:
        await call.answer("❌ Topilmadi!", show_alert=True)
        return
    await delete_serial(serial_id)
    await safe_edit(call.message, f"🗑 <b>{serial['name']}</b> o'chirildi.")
    await call.answer("O'chirildi!")

# ─── Admin: Qism o'chirish (/deleteep) ────────────────────────────────────────

@dp.message(Command("deleteep"))
async def cmd_deleteep(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q.")
        return
    args = message.text.split(None, 1)
    if len(args) < 2:
        await message.answer("❌ Foydalanish: /deleteep <serial nomi>")
        return
    query = args[1].strip()
    results = await search_serials(query, limit=5)
    if not results:
        await message.answer(f"❌ <b>«{query}»</b> topilmadi.")
        return
    buttons = [[InlineKeyboardButton(
        text=f"📺 {s['name']} ({s['ep_count']} qism)",
        callback_data=f"delep_serial:{s['id']}"
    )] for s in results]
    buttons.append([InlineKeyboardButton(text="❌ Bekor", callback_data="close")])
    await message.answer("Qaysi serialning qismini o'chirish kerak?",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("delep_serial:"))
async def cb_delep_serial(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    serial_id = int(call.data.split(":")[1])
    serial = await get_serial(serial_id)
    if not serial:
        await call.answer("❌ Topilmadi!", show_alert=True)
        return
    episodes = await get_episodes(serial_id)
    if not episodes:
        await safe_edit(call.message, f"⚠️ <b>{serial['name']}</b> da qismlar yo'q.")
        await call.answer()
        return
    await safe_edit(
        call.message,
        f"📺 <b>{serial['name']}</b>\n\nO'chirmoqchi bo'lgan qism raqamini tanlang:",
        del_episodes_kb(episodes, serial_id)
    )
    await call.answer()

@dp.callback_query(F.data.startswith("delep:"))
async def cb_delep(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    ep_id = int(call.data.split(":")[1])
    ep = await get_episode(ep_id)
    if not ep:
        await call.answer("❌ Topilmadi!", show_alert=True)
        return
    await delete_episode(ep_id)
    episodes = await get_episodes(ep['serial_id'])
    if episodes:
        await safe_edit(
            call.message,
            f"🗑 <b>{ep['number']}-qism</b> o'chirildi.\n\n"
            f"📺 <b>{ep['serial_name']}</b> — qolgan qismlar:",
            del_episodes_kb(episodes, ep['serial_id'])
        )
    else:
        await safe_edit(
            call.message,
            f"🗑 <b>{ep['number']}-qism</b> o'chirildi.\n\n"
            f"📺 <b>{ep['serial_name']}</b> da endi qism qolmadi."
        )
    await call.answer(f"{ep['number']}-qism o'chirildi!")

# ─── Admin: Hammasini o'chirish (/clearall) ───────────────────────────────────

@dp.message(Command("clearall"))
async def cmd_clearall(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q.")
        return
    s = await get_stats()
    await message.answer(
        f"⚠️ <b>Diqqat!</b>\n\n"
        f"Hozir bazada:\n"
        f"📺 <b>{s['serials']} ta serial</b>\n"
        f"▶️ <b>{s['episodes']} ta qism</b>\n\n"
        f"Barchasini o'chirishni tasdiqlaysizmi?",
        reply_markup=confirm_clearall_kb()
    )

@dp.callback_query(F.data == "clearall_confirm")
async def cb_clearall_confirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    await delete_all_serials()
    await safe_edit(call.message, "🗑 Barcha seriallar va qismlar o'chirildi.")
    await call.answer("Hammasi o'chirildi!")

# ─── Admin: Boshqa buyruqlar ──────────────────────────────────────────────────

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q.")
        return
    await message.answer(
        "⚙️ <b>Admin Panel</b>\n\n"
        "➕ <b>Qo'shish:</b>\n"
        "• /add — Serial qo'shish\n\n"
        "🗑 <b>O'chirish:</b>\n"
        "• /delete <nom> — Butun serialni o'chirish\n"
        "• /deleteep <nom> — Serialning bitta qismini o'chirish\n"
        "• /clearall — Hammasini o'chirish\n\n"
        "📊 <b>Boshqa:</b>\n"
        "• /stats — Statistika\n"
        "• /cancel — Bekor qilish\n\n"
        "<b>Qo'shish tartibi:</b>\n"
        "1️⃣ /add yozing\n"
        "2️⃣ Serial nomini kiriting\n"
        "3️⃣ Videolarni birin-ketin yuboring\n"
        "4️⃣ /done yozing — tugadi!"
    )

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q.")
        return
    s = await get_stats()
    await message.answer(
        f"📊 <b>Statistika</b>\n\n"
        f"📺 Seriallar: <b>{s['serials']}</b>\n"
        f"▶️ Qismlar: <b>{s['episodes']}</b>"
    )

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Bekor qilindi.", reply_markup=main_menu())

@dp.message(F.video)
async def unknown_video(message: Message, state: FSMContext):
    if await state.get_state():
        return
    if is_admin(message.from_user.id):
        await message.answer(
            "⚠️ Sessiya tugagan.\n\n"
            "Video qo'shish uchun avval /add buyrug'ini yozing, "
            "keyin serial nomini kiriting, so'ng videolarni yuboring."
        )

@dp.message()
async def unknown(message: Message, state: FSMContext):
    if await state.get_state():
        return
    if message.video or message.photo or message.document or message.audio:
        return
    await message.answer(
        "📌 <b>Nima qilmoqchisiz?</b>\n\n"
        "🔎 Serial qidirish — <b>Qidiruv</b> tugmasi\n"
        "📺 Ro'yxatni ko'rish — <b>Seriallar</b> tugmasi",
        reply_markup=main_menu()
    )

# ─── Webhook (Render) ─────────────────────────────────────────────────────────

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "tarjima_seriallar_secret_2024")
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"

async def health(request):
    return web.Response(text="OK")

async def on_startup():
    await init_db()
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Bosh menyu"),
        BotCommand(command="add", description="➕ Serial qo'shish"),
        BotCommand(command="delete", description="🗑 Serialni o'chirish"),
        BotCommand(command="deleteep", description="🗑 Bitta qismni o'chirish"),
        BotCommand(command="clearall", description="🗑 Hammasini o'chirish"),
        BotCommand(command="stats", description="📊 Statistika"),
        BotCommand(command="admin", description="⚙️ Admin panel"),
        BotCommand(command="cancel", description="❌ Bekor qilish"),
    ])
    logger.info("Bot ishga tayyor!")

async def main():
    await on_startup()
    
    port = int(os.environ.get("PORT", 10000))
    render_hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    
    if render_hostname:
        base_url = f"https://{render_hostname}"
        webhook_url = f"{base_url}{WEBHOOK_PATH}"
        
        await bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True
        )
        logger.info(f"Webhook o'rnatildi: {webhook_url}")
        
        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        
        SimpleRequestHandler(dp, bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        
        logger.info(f"Server {port} portda ishga tushdi")
        await asyncio.Event().wait()
    else:
        logger.info("Polling rejimi")
        await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
