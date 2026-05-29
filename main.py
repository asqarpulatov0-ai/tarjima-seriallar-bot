import asyncio
import logging
import os
import sys
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, BotCommand,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
import aiosqlite

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    raise ValueError("TOKEN topilmadi! TELEGRAM_BOT_TOKEN ni environmentga qo'shing.")

DB_PATH = os.path.join(os.path.dirname(__file__), "serials.db")
PER_PAGE = 8

# ========== DATABASE FUNCTIONS ==========
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS serials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    serial_id INTEGER NOT NULL REFERENCES serials(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    name TEXT DEFAULT '',
    UNIQUE(serial_id, number)
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    name TEXT DEFAULT '',
    file_id TEXT NOT NULL,
    UNIQUE(season_id, number)
);
CREATE INDEX IF NOT EXISTS idx_serials_name ON serials(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_seasons_serial ON seasons(serial_id);
CREATE INDEX IF NOT EXISTS idx_episodes_season ON episodes(season_id);
        """)
        await db.commit()

async def get_serials_count():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM serials") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def get_serials_page(offset=0, limit=8):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, name, description FROM serials ORDER BY name LIMIT ? OFFSET ?", (limit, offset)) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def search_serials(query, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, name FROM serials WHERE name LIKE ? COLLATE NOCASE ORDER BY name LIMIT ?", (f"%{query}%", limit)) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def get_serial(serial_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, name, description FROM serials WHERE id=?", (serial_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def get_seasons(serial_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, serial_id, number, name FROM seasons WHERE serial_id=? ORDER BY number", (serial_id,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def get_season(season_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, serial_id, number, name FROM seasons WHERE id=?", (season_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def get_episodes(season_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, season_id, number, name, file_id FROM episodes WHERE season_id=? ORDER BY number", (season_id,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def get_episode(episode_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""SELECT e.id, e.number, e.name, e.file_id,
                      s.number as season_number, s.id as season_id, s.serial_id,
                      sr.name as serial_name
               FROM episodes e
               JOIN seasons s ON e.season_id = s.id
               JOIN serials sr ON s.serial_id = sr.id
               WHERE e.id=?""", (episode_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM serials") as cur:
            serials = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM seasons") as cur:
            seasons = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM episodes") as cur:
            episodes = (await cur.fetchone())[0]
        return {"serials": serials, "seasons": seasons, "episodes": episodes}

async def add_serial(name, description=""):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT OR IGNORE INTO serials(name,description) VALUES(?,?)", (name, description))
        await db.commit()
        return cur.lastrowid

async def add_season(serial_id, number, name=""):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT OR IGNORE INTO seasons(serial_id,number,name) VALUES(?,?,?)", (serial_id, number, name))
        await db.commit()
        return cur.lastrowid

async def add_episode(season_id, number, name, file_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT OR REPLACE INTO episodes(season_id,number,name,file_id) VALUES(?,?,?,?)", (season_id, number, name, file_id))
        await db.commit()
        return cur.lastrowid

# ========== KEYBOARDS ==========
def main_menu():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔎 Qidiruv"), KeyboardButton(text="📺 Seriallar")]], resize_keyboard=True, persistent=True)

def serials_kb(serials, page, total):
    buttons = [[InlineKeyboardButton(text=f"📺 {s['name']}", callback_data=f"serial:{s['id']}")] for s in serials]
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

def seasons_kb(serial, seasons):
    buttons = []
    for s in seasons:
        label = s['name'] if s['name'] else f"Fasl {s['number']}"
        buttons.append([InlineKeyboardButton(text=f"🎬 {label}", callback_data=f"season:{s['id']}")])
    buttons.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="pg:0")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def episodes_kb(season, episodes, serial_id):
    buttons = []
    for e in episodes:
        label = e['name'] if e['name'] else f"{e['number']}-qism"
        if e['file_id'] == "PENDING":
            label = f"⏳ {label} (video kutilmoqda)"
        buttons.append([InlineKeyboardButton(text=f"▶️ {label}", callback_data=f"ep:{e['id']}")])
    buttons.append([InlineKeyboardButton(text="◀️ Fasllar", callback_data=f"serial:{serial_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def search_results_kb(serials):
    buttons = [[InlineKeyboardButton(text=f"📺 {s['name']}", callback_data=f"serial:{s['id']}")] for s in serials]
    buttons.append([InlineKeyboardButton(text="◀️ Bosh menu", callback_data="close")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def back_kb(serial_id, season_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Qismlarga", callback_data=f"season:{season_id}")],
        [InlineKeyboardButton(text="🏠 Seriallar", callback_data="pg:0")]
    ])

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="close")]])

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Serial qo'shish", callback_data="admin_addserial")],
        [InlineKeyboardButton(text="⚡ Tez yuklash", callback_data="admin_quickadd")],
        [InlineKeyboardButton(text="📦 Ommaviy yuklash", callback_data="admin_bulkadd")]
    ])

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# ========== ADMIN FUNKSIYASI ==========
def is_admin(user_id):
    logger.info(f"ADMIN TEKSHIRUV: {user_id}")
    admin_ids = {
        1885056636,
        8168417164
    }
    return int(user_id) in admin_ids

async def safe_edit(msg, text, markup=None):
    try:
        await msg.edit_text(text, reply_markup=markup)
    except Exception:
        await msg.answer(text, reply_markup=markup)

class Search(StatesGroup):
    waiting = State()

class Admin(StatesGroup):
    serial_name = State()
    serial_desc = State()
    serial_qismlar_soni = State()
    season_serial = State()
    season_num = State()
    season_name = State()
    ep_season = State()
    ep_num = State()
    ep_name = State()
    ep_file = State()
    bulk_season = State()
    bulk_video = State()

# ========== HANDLERS ==========
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    name = message.from_user.first_name or "Do'st"
    await message.answer(f"👋 Salom, <b>{name}</b>!\n\n🎬 <b>Serial Bot</b>ga xush kelibsiz!\n\n📌 <b>Nima qilmoqchisiz?</b>", reply_markup=main_menu())

async def show_page(target, page=0):
    total = await get_serials_count()
    if total == 0:
        text, markup = "📭 Hozircha serial yo'q.", None
    else:
        serials = await get_serials_page(offset=page * PER_PAGE, limit=PER_PAGE)
        text = f"📺 <b>Seriallar</b> — jami <b>{total} ta</b>"
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

@dp.callback_query(F.data.startswith("serial:"))
async def cb_serial(call: CallbackQuery):
    serial_id = int(call.data.split(":")[1])
    serial = await get_serial(serial_id)
    if not serial:
        await call.answer("❌ Topilmadi!", show_alert=True)
        return
    seasons = await get_seasons(serial_id)
    desc = f"\n📝 {serial['description']}" if serial.get('description') else ""
    text = (f"📺 <b>{serial['name']}</b>{desc}\n\n🎬 <b>{len(seasons)} ta fasl</b>" if seasons else f"📺 <b>{serial['name']}</b>\n\n⚠️ Fasllar yo'q.")
    await safe_edit(call.message, text, seasons_kb(serial, seasons))
    await call.answer()

@dp.callback_query(F.data.startswith("season:"))
async def cb_season(call: CallbackQuery):
    season_id = int(call.data.split(":")[1])
    season = await get_season(season_id)
    if not season:
        await call.answer("❌ Topilmadi!", show_alert=True)
        return
    serial = await get_serial(season['serial_id'])
    episodes = await get_episodes(season_id)
    label = season['name'] if season['name'] else f"Fasl {season['number']}"
    text = (f"📺 <b>{serial['name']}</b>\n🎬 <b>{label}</b>\n\n▶️ <b>{len(episodes)} ta qism</b>" if episodes else f"📺 <b>{serial['name']}</b>\n🎬 <b>{label}</b>\n\n⚠️ Qismlar yo'q.")
    await safe_edit(call.message, text, episodes_kb(season, episodes, season['serial_id']))
    await call.answer()

@dp.callback_query(F.data.startswith("ep:"))
async def cb_episode(call: CallbackQuery):
    ep = await get_episode(int(call.data.split(":")[1]))
    if not ep:
        await call.answer("❌ Topilmadi!", show_alert=True)
        return
    if ep['file_id'] == "PENDING":
        await call.answer("⏳ Bu qismga video hali yuklanmagan!", show_alert=True)
        return
    caption = (f"📺 <b>{ep['serial_name']}</b>\n🎬 Fasl {ep['season_number']}\n▶️ {ep['number']}-qism" + (f" — {ep['name']}" if ep['name'] else ""))
    await call.answer("⏳ Yuklanmoqda...")
    try:
        await bot.send_video(call.message.chat.id, ep['file_id'], caption=caption, reply_markup=back_kb(ep['serial_id'], ep['season_id']), supports_streaming=True)
    except Exception as e:
        logger.error(f"Video error: {e}")
        await call.message.answer(f"❌ Video yuborishda xato.\n<code>{e}</code>")

@dp.message(F.text == "🔎 Qidiruv")
async def btn_search(message: Message, state: FSMContext):
    await state.set_state(Search.waiting)
    await message.answer("🔎 <b>Qidiruv</b>\n\nSerial nomini kiriting:", reply_markup=cancel_kb())

@dp.callback_query(F.data == "search")
async def cb_search(call: CallbackQuery, state: FSMContext):
    await state.set_state(Search.waiting)
    await call.message.answer("🔎 <b>Qidiruv</b>\n\nSerial nomini kiriting:", reply_markup=cancel_kb())
    await call.answer()

@dp.message(StateFilter(Search.waiting))
async def do_search(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    if not query:
        return
    await state.clear()
    results = await search_serials(query)
    if not results:
        await message.answer(f"🔍 <b>«{query}»</b> topilmadi.", reply_markup=cancel_kb())
        return
    await message.answer(f"🔍 <b>{len(results)} ta natija:</b>", reply_markup=search_results_kb(results))

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

# ========== ADMIN HANDLERS ==========
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q! Siz admin emassiz.")
        return
    await message.answer("⚙️ <b>Admin Panel</b>\n\nQuyidagi tugmalardan foydalaning:", reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_addserial")
async def admin_addserial_cb(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q!", show_alert=True)
        return
    await state.set_state(Admin.serial_name)
    await call.message.answer("📝 Serial nomini kiriting:")
    await call.answer()

@dp.callback_query(F.data == "admin_quickadd")
async def admin_quickadd_cb(call: CallbackQuery, state: FSMContext):
    await state.set_state("quickadd_serial")
    await call.message.answer("⚡ <b>TEZ YUKLASH</b>\n\nSerial nomini yoki ID sini kiriting:")

@dp.callback_query(F.data == "admin_bulkadd")
async def admin_bulkadd_cb(call: CallbackQuery, state: FSMContext):
    await state.set_state(Admin.bulk_season)
    await call.message.answer("⚡ <b>Ommaviy yuklash rejimi</b>\n\n🆔 Fasl ID sini kiriting:\n<i>/cancel — bekor qilish</i>")
    await call.answer()

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Bekor qilindi.", reply_markup=main_menu())

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q.")
        return
    s = await get_stats()
    await message.answer(f"📊 <b>Statistika</b>\n\n📺 Seriallar: <b>{s['serials']}</b>\n🎬 Fasllar: <b>{s['seasons']}</b>\n▶️ Qismlar: <b>{s['episodes']}</b>")

@dp.message(Command("addserial"))
async def cmd_addserial(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q.")
        return
    await state.set_state(Admin.serial_name)
    await message.answer("📝 Serial nomini kiriting:")

@dp.message(StateFilter(Admin.serial_name))
async def a_serial_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(Admin.serial_desc)
    await message.answer("📝 Tavsif (yoki — ):")

@dp.message(StateFilter(Admin.serial_desc))
async def a_serial_desc(message: Message, state: FSMContext):
    data = await state.get_data()
    desc = "" if message.text.strip() == "-" else message.text.strip()
    sid = await add_serial(data['name'], desc)
    await state.update_data(serial_id=sid, serial_name=data['name'])
    await state.set_state(Admin.serial_qismlar_soni)
    await message.answer(
        f"✅ <b>{data['name']}</b> qo'shildi!\n\n"
        f"📊 <b>Qismlar sonini kiriting:</b>\n"
        f"Masalan: <code>50</code> — 1 dan 50 gacha avtomatik yaratiladi\n\n"
        f"<i>0 yoki — bo'sh qoldirsangiz, keyin qo'shasiz</i>"
    )

@dp.message(StateFilter(Admin.serial_qismlar_soni))
async def a_serial_qismlar_soni(message: Message, state: FSMContext):
    text = message.text.strip()
    data = await state.get_data()
    if text == "-" or text == "0" or text == "":
        await state.clear()
        await message.answer(f"✅ <b>{data['serial_name']}</b> saqlandi!\n\n➕ Qismlar: /addepisode")
        return
    if not text.isdigit():
        await message.answer("❌ Faqat raqam kiriting (masalan: 50):")
        return
    qismlar_soni = int(text)
    seasons = await get_seasons(data['serial_id'])
    if not seasons:
        season_id = await add_season(data['serial_id'], 1, "1-fasl")
    else:
        season_id = seasons[0]['id']
    yaratilgan = 0
    for qism_raqam in range(1, qismlar_soni + 1):
        episodes = await get_episodes(season_id)
        mavjud = any(e['number'] == qism_raqam for e in episodes)
        if not mavjud:
            await add_episode(season_id, qism_raqam, f"{qism_raqam}-qism", "PENDING")
            yaratilgan += 1
    await state.clear()
    await message.answer(
        f"🎉 <b>YAKUNLANDI!</b>\n\n"
        f"📺 <b>{data['serial_name']}</b>\n"
        f"✅ 1-fasl yaratildi\n"
        f"✅ <b>{yaratilgan} ta qism</b> tayyorlandi (1...{qismlar_soni})\n\n"
        f"📹 <b>Videolarni yuklash:</b>\n"
        f"• /addepisode - har bir qism uchun\n"
        f"• /bulkadd - ommaviy yuklash"
    )

@dp.message(Command("bulkadd"))
async def cmd_bulkadd(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q. Siz admin emassiz!")
        return
    await state.set_state(Admin.bulk_season)
    await message.answer("⚡ <b>Ommaviy yuklash rejimi</b>\n\n🆔 Fasl ID sini kiriting:\n<i>/cancel — bekor qilish</i>")

@dp.message(StateFilter(Admin.bulk_season))
async def bulk_season(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q!")
        await state.clear()
        return
    if not message.text.strip().isdigit():
        await message.answer("❌ Raqam kiriting:")
        return
    sid = int(message.text.strip())
    season = await get_season(sid)
    if not season:
        await message.answer(f"❌ ID={sid} fasl topilmadi!")
        return
    serial = await get_serial(season['serial_id'])
    label = season['name'] if season['name'] else f"Fasl {season['number']}"
    existing = await get_episodes(sid)
    existing_numbers = [e['number'] for e in existing]
    next_num = 1
    while next_num in existing_numbers:
        next_num += 1
    await state.update_data(bulk_season_id=sid, bulk_season_label=label, bulk_serial_name=serial['name'], bulk_next=next_num, bulk_count=0)
    await state.set_state(Admin.bulk_video)
    await message.answer(f"✅ <b>{serial['name']}</b> — {label}\n\n📹 Videolarni yuboring!\n▶️ Boshlang'ich qism: <b>{next_num}</b>\n\n<i>Tugatish: /done</i>")

@dp.message(StateFilter(Admin.bulk_video), F.video | F.document)
async def bulk_video(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.video:
        file_id = message.video.file_id
        size_mb = (message.video.file_size or 0) / 1024 / 1024
    else:
        file_id = message.document.file_id
        size_mb = (message.document.file_size or 0) / 1024 / 1024
    num = data['bulk_next']
    await add_episode(data['bulk_season_id'], num, f"{num}-qism", file_id)
    await state.update_data(bulk_next=num + 1, bulk_count=data['bulk_count'] + 1)
    await message.answer(f"✅ <b>{num}-qism</b> qo'shildi! ({size_mb:.1f} MB)\n📹 Keyingisi: <b>{num+1}-qism</b>\n<i>Tugatish: /done</i>")

@dp.message(StateFilter(Admin.bulk_video), Command("done"))
async def bulk_done(message: Message, state: FSMContext):
    data = await state.get_data()
    count = data['bulk_count']
    await state.clear()
    await message.answer(f"🎉 <b>Yuklash tugadi!</b>\n\n📺 {data['bulk_serial_name']} — {data['bulk_season_label']}\n✅ Jami <b>{count} ta qism</b> qo'shildi!\n\nYana yuklash: /bulkadd")

@dp.message(StateFilter(Admin.bulk_video))
async def bulk_wrong(message: Message):
    if message.text and message.text.startswith("/"):
        return
    await message.answer("❌ Faqat <b>video</b> yuboring!\n<i>Tugatish: /done</i>")

@dp.message(StateFilter("quickadd_serial"))
async def quickadd_serial(message: Message, state: FSMContext):
    text = message.text.strip()
    if text.isdigit():
        serial = await get_serial(int(text))
    else:
        results = await search_serials(text, limit=1)
        serial = results[0] if results else None
    if not serial:
        await message.answer(f"❌ «{text}» topilmadi!\n\nQaytadan kiriting:")
        return
    seasons = await get_seasons(serial['id'])
    if not seasons:
        season_id = await add_season(serial['id'], 1, "1-fasl")
    else:
        season_id = seasons[0]['id']
    episodes = await get_episodes(season_id)
    existing_numbers = [e['number'] for e in episodes]
    next_num = 1
    while next_num in existing_numbers:
        next_num += 1
    await state.update_data(quick_serial=serial, quick_season=season_id, quick_next=next_num, quick_count=0)
    await state.set_state("quickadd_video")
    await message.answer(
        f"✅ <b>{serial['name']}</b>\n"
        f"🎬 1-fasl\n\n"
        f"📹 <b>Videolarni yuboring!</b>\n\n"
        f"▶️ Boshlanish: <b>{next_num}-qism</b>\n\n"
        f"1-video → {next_num}-qism\n"
        f"2-video → {next_num + 1}-qism\n"
        f"3-video → {next_num + 2}-qism\n"
        f"...\n\n"
        f"<i>Tugatish: /done</i>"
    )

# ========== QUYIDAGI 3 TA HANDLER QO'SHILDI (TUZATISH) ==========

@dp.message(StateFilter("quickadd_video"), F.video | F.document)
async def quickadd_video(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    if message.video:
        file_id = message.video.file_id
        size_mb = (message.video.file_size or 0) / 1024 / 1024
    else:
        file_id = message.document.file_id
        size_mb = (message.document.file_size or 0) / 1024 / 1024
    num = data['quick_next']
    season_id = data['quick_season']
    serial = data['quick_serial']
    await add_episode(season_id, num, f"{num}-qism", file_id)
    await state.update_data(quick_next=num + 1, quick_count=data['quick_count'] + 1)
    await message.answer(
        f"✅ <b>{num}-qism</b> qo'shildi! ({size_mb:.1f} MB)\n"
        f"📹 Keyingisi: <b>{num+1}-qism</b>\n"
        f"<i>Tugatish: /done</i>"
    )

@dp.message(StateFilter("quickadd_video"), Command("done"))
async def quickadd_done(message: Message, state: FSMContext):
    data = await state.get_data()
    count = data.get('quick_count', 0)
    serial = data.get('quick_serial', {})
    await state.clear()
    await message.answer(
        f"🎉 <b>Yuklash tugadi!</b>\n\n"
        f"📺 {serial.get('name', '')}\n"
        f"✅ Jami <b>{count} ta qism</b> qo'shildi!\n\n"
        f"Yana: /admin"
    )

@dp.message(StateFilter("quickadd_video"))
async def quickadd_wrong(message: Message):
    if message.text and message.text.startswith("/"):
        return
    await message.answer("❌ Faqat <b>video</b> yuboring!\n<i>Tugatish: /done</i>")

# ========== TUZATISH TUGADI ==========

@dp.message(Command("addseason"))
async def cmd_addseason(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q.")
        return
    await state.set_state(Admin.season_serial)
    await message.answer("🆔 Serial ID:")

@dp.message(StateFilter(Admin.season_serial))
async def a_season_serial(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("❌ Raqam kiriting:")
        return
    sid = int(message.text.strip())
    serial = await get_serial(sid)
    if not serial:
        await message.answer(f"❌ ID={sid} topilmadi!")
        return
    await state.update_data(serial_id=sid, serial_name=serial['name'])
    await state.set_state(Admin.season_num)
    await message.answer(f"✅ <b>{serial['name']}</b>\n\nFasl raqami (1, 2, ...):")

@dp.message(StateFilter(Admin.season_num))
async def a_season_num(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("❌ Raqam kiriting:")
        return
    await state.update_data(season_num=int(message.text.strip()))
    await state.set_state(Admin.season_name)
    await message.answer("📝 Fasl nomi (yoki — ):")

@dp.message(StateFilter(Admin.season_name))
async def a_season_name(message: Message, state: FSMContext):
    data = await state.get_data()
    name = "" if message.text.strip() == "-" else message.text.strip()
    fid = await add_season(data['serial_id'], data['season_num'], name)
    await state.clear()
    await message.answer(f"✅ {data['season_num']}-fasl qo'shildi!\n🆔 Fasl ID: <code>{fid}</code>\n\nQism: /addepisode")

@dp.message(Command("addepisode"))
async def cmd_addepisode(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q.")
        return
    await state.set_state(Admin.ep_season)
    await message.answer("🆔 Fasl ID:")

@dp.message(StateFilter(Admin.ep_season))
async def a_ep_season(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("❌ Raqam kiriting:")
        return
    sid = int(message.text.strip())
    season = await get_season(sid)
    if not season:
        await message.answer(f"❌ ID={sid} topilmadi!")
        return
    serial = await get_serial(season['serial_id'])
    label = season['name'] if season['name'] else f"Fasl {season['number']}"
    await state.update_data(season_id=sid, season_label=label, serial_name=serial['name'])
    await state.set_state(Admin.ep_num)
    await message.answer(f"✅ <b>{serial['name']}</b> — {label}\n\nQism raqami:")

@dp.message(StateFilter(Admin.ep_num))
async def a_ep_num(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("❌ Raqam kiriting:")
        return
    await state.update_data(ep_num=int(message.text.strip()))
    await state.set_state(Admin.ep_name)
    await message.answer("📝 Qism nomi (yoki — ):")

@dp.message(StateFilter(Admin.ep_name))
async def a_ep_name(message: Message, state: FSMContext):
    name = "" if message.text.strip() == "-" else message.text.strip()
    await state.update_data(ep_name=name)
    await state.set_state(Admin.ep_file)
    await message.answer("🎬 Videoni yuboring!")

@dp.message(StateFilter(Admin.ep_file), F.video | F.document)
async def a_ep_file(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.video:
        file_id = message.video.file_id
        size_mb = (message.video.file_size or 0) / 1024 / 1024
    else:
        file_id = message.document.file_id
        size_mb = (message.document.file_size or 0) / 1024 / 1024
    eid = await add_episode(data['season_id'], data['ep_num'], data['ep_name'], file_id)
    await state.clear()
    label = data['ep_name'] if data['ep_name'] else f"{data['ep_num']}-qism"
    await message.answer(f"✅ <b>{data['serial_name']}</b> — {data['season_label']}\n▶️ <b>{label}</b> qo'shildi!\n📦 {size_mb:.1f} MB\n🆔 ID: <code>{eid}</code>\n\nKeyingi: /addepisode")

@dp.message(StateFilter(Admin.ep_file))
async def a_ep_wrong(message: Message):
    await message.answer("❌ Faqat video yuboring!")

@dp.message()
async def unknown(message: Message, state: FSMContext):
    if await state.get_state():
        return
    await message.answer("❓ /start — boshlash", reply_markup=main_menu())

# ========== WEBHOOK ==========
async def handle_webhook(request):
    from aiogram.types import Update
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return web.Response(status=200)

async def health(request):
    return web.Response(text="OK")

async def on_startup():
    await init_db()
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Bosh menyu"),
        BotCommand(command="admin", description="⚙️ Admin panel"),
        BotCommand(command="addserial", description="➕ Serial"),
        BotCommand(command="addseason", description="➕ Fasl"),
        BotCommand(command="addepisode", description="➕ Qism"),
        BotCommand(command="bulkadd", description="📦 Ommaviy yuklash"),
        BotCommand(command="quickadd", description="⚡ Tez yuklash"),
        BotCommand(command="stats", description="📊 Statistika"),
        BotCommand(command="cancel", description="❌ Bekor"),
    ])
    logger.info("✅ Bot ishga tayyor!")

async def main():
    port = int(os.environ.get("PORT", 10000))
    webhook_path = "/webhook"
    if os.environ.get("RENDER"):
        render_hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        if not render_hostname:
            logger.error("RENDER_EXTERNAL_HOSTNAME topilmadi!")
            return
        webhook_url = f"https://{render_hostname}{webhook_path}"
    else:
        webhook_url = f"http://localhost:{port}{webhook_path}"
    await on_startup()
    await bot.set_webhook(webhook_url, drop_pending_updates=True)
    logger.info(f"Webhook sozlandi: {webhook_url}")
    app = web.Application()
    app.router.add_post(webhook_path, handle_webhook)
    app.router.add_get("/health", health)
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🚀 Bot ishga tushdi! Port: {port}")
    try:
        await asyncio.Event().wait()
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
