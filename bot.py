# -*- coding: utf-8 -*-
"""
🎌 ANIME BOT — bitta fayl (aiogram 3.7+)

ISHGA TUSHIRISH
    pip install "aiogram>=3.7"
    1) Token va admin ID ni pastdagi SOZLAMALAR bo'limiga yozing
       (yoki hosting panelida BOT_TOKEN va ADMIN_IDS environment variable qo'ying — xavfsizroq)
    2) python bot.py
    3) Botda /admin yozing

HOSTING UCHUN MUHIM
    Ma'lumotlar bazasi (DB_NAME) doimiy diskda turishi kerak, aks holda har deploy'da
    foydalanuvchilar va premiumlar o'chib ketadi.
    Masalan: DB_NAME=/data/anime_bot.db  (hostingdagi Volume/Disk yo'li)

ESLATMA
    Bot majburiy obuna kanallarida va post kanalida ADMIN bo'lishi shart.
"""
import asyncio
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from html import escape as esc

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ChatJoinRequest,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

log = logging.getLogger("anime_bot")

# =====================================================================
#                            SOZLAMALAR
# =====================================================================
# Token: @BotFather dan olinadi. Environment variable BOT_TOKEN bo'lsa, o'sha ishlatiladi.
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Adminlar (Telegram ID). Environment: ADMIN_IDS="123456789,987654321"
_env_admins = [int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.isdigit()]
ADMIN_IDS = _env_admins or [7112151149]  # <- o'zingizning Telegram ID raqamingizni qo'ying

# Ma'lumotlar bazasi fayli (hostingda doimiy disk yo'lini ko'rsating)
DB_NAME = os.getenv("DB_NAME", "anime_bot.db")

# Standart premium narxlari [kun, so'm] — admin paneldan ham o'zgartiriladi
DEFAULT_PREMIUM_DAYS_PRICES = [[7, 5000], [30, 15000], [90, 35000]]

DEFAULT_TEXTS = {
    "start_text": "🎌 <b>Anime Bot</b>ga xush kelibsiz!\n\n🔑 Shifrli kod orqali qismlarni oching yoki 💎 Premium sotib oling!",
    "premium_text": "💎 <b>Premium obuna</b>\n\nPremium orqali barcha 🔒 animelarni cheksiz tomosha qiling!",
    "help_text": "📞 Admin bilan bog'lanish: @admin_username",
    "btn_codes": "🔑 Kod kiritish",
    "btn_premium": "💎 Premium",
    "btn_animes": "🎬 Animelar",
    "btn_profile": "👤 Profil",
    "prices": json.dumps(DEFAULT_PREMIUM_DAYS_PRICES),
}


# =====================================================================
#                        MA'LUMOTLAR BAZASI (SQLite)
# =====================================================================
class db:
    """Barcha ma'lumotlar bazasi funksiyalari (db.nomi(...) ko'rinishida chaqiriladi)"""

    # ---------- past darajadagi yordamchilar ----------
    @staticmethod
    def conn():
        c = sqlite3.connect(DB_NAME, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def run(sql, params=()):
        c = db.conn()
        try:
            cur = c.execute(sql, params)
            c.commit()
            return cur.lastrowid, cur.rowcount
        finally:
            c.close()

    @staticmethod
    def one(sql, params=()):
        c = db.conn()
        try:
            return c.execute(sql, params).fetchone()
        finally:
            c.close()

    @staticmethod
    def many(sql, params=()):
        c = db.conn()
        try:
            return c.execute(sql, params).fetchall()
        finally:
            c.close()

    # ---------- yaratish ----------
    @staticmethod
    def init():
        c = db.conn()
        try:
            try:
                c.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            c.executescript("""
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                joined_at TEXT,
                is_banned INTEGER DEFAULT 0,
                is_premium INTEGER DEFAULT 0,
                premium_until TEXT,
                codes_used INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS animes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                is_premium INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS episodes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anime_id INTEGER,
                episode_number INTEGER,
                code TEXT UNIQUE,
                file_id TEXT,
                file_type TEXT
            );
            CREATE TABLE IF NOT EXISTS wallets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                card_number TEXT,
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS channels(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT UNIQUE,
                channel_url TEXT
            );
            CREATE TABLE IF NOT EXISTS texts(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS payments(
                rid TEXT PRIMARY KEY,
                user_id INTEGER,
                days INTEGER,
                amount INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            );
            """)
            c.executescript("""
            CREATE TABLE IF NOT EXISTS join_requests(
                channel_id TEXT,
                user_id INTEGER,
                PRIMARY KEY(channel_id, user_id)
            );
            """)
            # eski bazalarga yangi ustunlarni xavfsiz qo'shish
            for table, col, ddl in [
                ("channels", "kind", "TEXT DEFAULT 'channel'"),
                ("animes", "poster", "TEXT DEFAULT ''"),
                ("animes", "fandub", "TEXT DEFAULT ''"),
            ]:
                cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
                if col not in cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            for k, v in DEFAULT_TEXTS.items():
                c.execute("INSERT OR IGNORE INTO texts(key, value) VALUES(?, ?)", (k, v))
            c.commit()
        finally:
            c.close()

    # ---------- foydalanuvchilar ----------
    @staticmethod
    def add_user(user_id, username, full_name):
        db.run("INSERT OR IGNORE INTO users(user_id, username, full_name, joined_at) VALUES(?,?,?,?)",
               (user_id, username, full_name, datetime.now().isoformat()))

    @staticmethod
    def get_user(user_id):
        return db.one("SELECT * FROM users WHERE user_id=?", (user_id,))

    @staticmethod
    def all_user_ids():
        return [r[0] for r in db.many("SELECT user_id FROM users WHERE is_banned=0")]

    @staticmethod
    def ban_user(user_id):
        db.run("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))

    @staticmethod
    def unban_user(user_id):
        db.run("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))

    @staticmethod
    def set_premium(user_id, days):
        """Premium beradi. Agar premium hali tugamagan bo'lsa — qolgan kunlarga QO'SHIB uzaytiradi."""
        base = datetime.now()
        u = db.get_user(user_id)
        if u and u["is_premium"] and u["premium_until"]:
            try:
                cur = datetime.fromisoformat(u["premium_until"])
                if cur > base:
                    base = cur
            except ValueError:
                pass
        until = (base + timedelta(days=int(days))).isoformat()
        db.run("UPDATE users SET is_premium=1, premium_until=? WHERE user_id=?", (until, user_id))
        return until

    @staticmethod
    def has_premium(user_id):
        u = db.get_user(user_id)
        if not u or not u["is_premium"]:
            return False
        if u["premium_until"] and u["premium_until"] < datetime.now().isoformat():
            db.run("UPDATE users SET is_premium=0 WHERE user_id=?", (user_id,))
            return False
        return True

    @staticmethod
    def use_code(user_id):
        db.run("UPDATE users SET codes_used=codes_used+1 WHERE user_id=?", (user_id,))

    @staticmethod
    def counts():
        today = datetime.now().date().isoformat()
        return {
            "users": db.one("SELECT COUNT(*) FROM users")[0],
            "premium": db.one("SELECT COUNT(*) FROM users WHERE is_premium=1")[0],
            "today": db.one("SELECT COUNT(*) FROM users WHERE joined_at >= ?", (today,))[0],
            "animes": db.one("SELECT COUNT(*) FROM animes")[0],
            "episodes": db.one("SELECT COUNT(*) FROM episodes")[0],
        }

    # ---------- animelar ----------
    @staticmethod
    def add_anime(title, desc, is_premium):
        return db.run("INSERT INTO animes(title, description, is_premium) VALUES(?,?,?)",
                      (title, desc, is_premium))[0]

    @staticmethod
    def get_animes():
        return db.many("SELECT * FROM animes ORDER BY id")

    @staticmethod
    def get_anime(aid):
        return db.one("SELECT * FROM animes WHERE id=?", (aid,))

    @staticmethod
    def delete_anime(aid):
        db.run("DELETE FROM episodes WHERE anime_id=?", (aid,))
        db.run("DELETE FROM animes WHERE id=?", (aid,))

    # ---------- qismlar ----------
    @staticmethod
    def add_episode(anime_id, number, code, file_id, file_type):
        db.run("INSERT INTO episodes(anime_id, episode_number, code, file_id, file_type) VALUES(?,?,?,?,?)",
               (anime_id, number, code, file_id, file_type))

    @staticmethod
    def get_episode_by_code(code):
        return db.one("SELECT * FROM episodes WHERE code=?", (code,))

    @staticmethod
    def get_episodes(anime_id):
        return db.many("SELECT * FROM episodes WHERE anime_id=? ORDER BY episode_number", (anime_id,))

    @staticmethod
    def delete_episode_by_code(code):
        return db.run("DELETE FROM episodes WHERE code=?", (code,))[1] > 0

    # ---------- hamyonlar ----------
    @staticmethod
    def add_wallet(name, card):
        db.run("INSERT INTO wallets(name, card_number) VALUES(?,?)", (name, card))

    @staticmethod
    def get_wallets(active_only=True):
        return db.many("SELECT * FROM wallets" + (" WHERE is_active=1" if active_only else ""))

    @staticmethod
    def delete_wallet(wid):
        db.run("DELETE FROM wallets WHERE id=?", (wid,))

    # ---------- anime tahrirlash ----------
    @staticmethod
    def update_anime(aid, field, value):
        if field not in ("title", "description", "poster", "fandub", "is_premium"):
            raise ValueError("noto'g'ri maydon")
        db.run(f"UPDATE animes SET {field}=? WHERE id=?", (value, aid))

    # ---------- zayafka (join request) ----------
    @staticmethod
    def add_join_request(channel_id, user_id):
        db.run("INSERT OR IGNORE INTO join_requests(channel_id, user_id) VALUES(?,?)", (str(channel_id), user_id))

    @staticmethod
    def has_join_request(channel_id, user_id):
        return db.one("SELECT 1 FROM join_requests WHERE channel_id=? AND user_id=?",
                      (str(channel_id), user_id)) is not None

    # ---------- majburiy obuna kanallari ----------
    @staticmethod
    def add_channel(cid, url, kind="channel"):
        db.run("INSERT OR IGNORE INTO channels(channel_id, channel_url, kind) VALUES(?,?,?)", (cid, url, kind))

    @staticmethod
    def get_channels():
        return db.many("SELECT * FROM channels")

    @staticmethod
    def delete_channel(cid):
        db.run("DELETE FROM channels WHERE id=?", (cid,))

    # ---------- matnlar / sozlamalar ----------
    @staticmethod
    def get_text(key, default=""):
        r = db.one("SELECT value FROM texts WHERE key=?", (key,))
        return r["value"] if r else default

    @staticmethod
    def set_text(key, value):
        db.run("INSERT OR REPLACE INTO texts(key, value) VALUES(?,?)", (key, value))

    @staticmethod
    def get_setting(key, default=None):
        r = db.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else default

    @staticmethod
    def set_setting(key, value):
        db.run("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, value))

    # ---------- to'lov so'rovlari ----------
    @staticmethod
    def create_payment(rid, user_id, days, amount):
        db.run("INSERT INTO payments(rid, user_id, days, amount, created_at) VALUES(?,?,?,?,?)",
               (rid, user_id, days, amount, datetime.now().isoformat()))

    @staticmethod
    def get_payment(rid):
        return db.one("SELECT * FROM payments WHERE rid=?", (rid,))

    @staticmethod
    def resolve_payment(rid, status):
        """So'rovni faqat BIR marta ko'rib chiqishga ruxsat beradi (ikki marta premium berilmasligi uchun)"""
        return db.run("UPDATE payments SET status=? WHERE rid=? AND status='pending'", (status, rid))[1] > 0


def load_admins():
    """Admin paneldan qo'shilgan adminlarni bazadan yuklaydi (qayta ishga tushganda yo'qolmaydi)"""
    try:
        for a in json.loads(db.get_setting("extra_admins", "[]")):
            if int(a) not in ADMIN_IDS:
                ADMIN_IDS.append(int(a))
    except Exception:
        log.exception("extra_admins o'qib bo'lmadi")


def save_admins():
    extra = [a for a in ADMIN_IDS if a not in _env_admins and a != 123456789]
    db.set_setting("extra_admins", json.dumps(extra))


# =====================================================================
#                  YORDAMCHI FUNKSIYALAR VA KLAVIATURALAR
# =====================================================================
def get_prices():
    """Premium narxlari: [[kun, narx], ...]"""
    try:
        return [[int(d), int(p)] for d, p in json.loads(db.get_text("prices", "[]"))]
    except Exception:
        return [list(x) for x in DEFAULT_PREMIUM_DAYS_PRICES]


def post_channels() -> list:
    """Post yuboriladigan kanallar (bir nechta bo'lishi mumkin, vergul bilan saqlanadi)"""
    raw = db.get_setting("post_channel") or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


def get_socials() -> list:
    """Majburiy obunadagi ijtimoiy tarmoq havolalari: [[nom, url], ...] (eng ko'pi 2 ta)"""
    try:
        return [list(x) for x in json.loads(db.get_setting("socials", "[]"))][:2]
    except Exception:
        return []


def protect_on() -> bool:
    """Kontent cheklash (forward/saqlash taqiqi)"""
    return db.get_setting("protect", "0") == "1"


def bot_is_on() -> bool:
    return db.get_setting("bot_on", "1") == "1"


async def send_to_post_channels(bot: Bot, fn):
    """fn(chat_id) ni har bir post kanaliga bajaradi. (muvaffaqiyatli, xatolar) qaytaradi"""
    ok, errs = 0, []
    for ch in post_channels():
        try:
            await fn(ch)
            ok += 1
        except Exception as e:
            errs.append(f"{ch}: {e}")
    return ok, errs


def menu_texts() -> set:
    return {
        db.get_text("btn_codes", "🔑 Kod kiritish"),
        db.get_text("btn_premium", "💎 Premium"),
        db.get_text("btn_animes", "🎬 Animelar"),
        db.get_text("btn_profile", "👤 Profil"),
    }


def admin_panel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="⚙️ Birlamchi sozlamalar", callback_data="admin:settings"))
    b.row(InlineKeyboardButton(text="📊 Statistika", callback_data="admin:stats"),
          InlineKeyboardButton(text="📢 Xabar yuborish", callback_data="admin:broadcast"))
    b.row(InlineKeyboardButton(text="📬 Post tayyorlash", callback_data="admin:post"),
          InlineKeyboardButton(text="📌 Konkurs", callback_data="admin:contest"),
          InlineKeyboardButton(text="📩 TEXT POST", callback_data="admin:textpost"))
    b.row(InlineKeyboardButton(text="🎬 Animelar sozlash", callback_data="admin:animes"),
          InlineKeyboardButton(text="💳 Hamyonlar", callback_data="admin:wallets"))
    b.row(InlineKeyboardButton(text="🔍 Foydalanuvchini boshqarish", callback_data="admin:user_manage"))
    b.row(InlineKeyboardButton(text="📣 Kanallar", callback_data="admin:channels"),
          InlineKeyboardButton(text="🔘 Tugmalar", callback_data="admin:buttons"),
          InlineKeyboardButton(text="📄 Matnlar", callback_data="admin:texts"))
    return b.as_markup()


def back_kb(data: str = "admin:panel") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data=data))
    return b.as_markup()


def user_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=db.get_text("btn_codes", "🔑 Kod kiritish")),
         KeyboardButton(text=db.get_text("btn_premium", "💎 Premium"))],
        [KeyboardButton(text=db.get_text("btn_animes", "🎬 Animelar")),
         KeyboardButton(text=db.get_text("btn_profile", "👤 Profil"))],
    ], resize_keyboard=True)


def premium_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for days, price in get_prices():
        b.row(InlineKeyboardButton(text=f"💎 {days} kun — {price:,} so'm", callback_data=f"buy:{days}"))
    return b.as_markup()


async def mark_message(msg: Message, suffix: str):
    """Xabar (matn yoki rasm/fayl sarlavhasi) oxiriga belgi qo'shadi va tugmalarni olib tashlaydi"""
    try:
        base = msg.html_text or ""
        if msg.text is not None:
            await msg.edit_text(base + suffix)
        else:
            await msg.edit_caption(caption=base + suffix)
    except Exception:
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


# =====================================================================
#                           ADMIN PANEL
# =====================================================================
def _is_admin(event) -> bool:
    return bool(event.from_user and event.from_user.id in ADMIN_IDS)


admin_router = Router()
admin_router.message.filter(_is_admin)
admin_router.callback_query.filter(_is_admin)


class AdminStateReset(BaseMiddleware):
    """Admin menyusidagi har bir 'admin:...' tugmasi eski holatni (FSM) tozalaydi"""

    async def __call__(self, handler, event, data):
        if (isinstance(event, CallbackQuery) and (event.data or "").startswith("admin:")
                and event.from_user.id in ADMIN_IDS):
            st = data.get("state")
            if st:
                await st.clear()
        return await handler(event, data)


admin_router.callback_query.outer_middleware(AdminStateReset())


class AdminFSM(StatesGroup):
    broadcast = State()
    post_content = State()
    post_button = State()
    textpost = State()
    contest = State()
    anime_title = State()
    anime_desc = State()
    ep_number = State()
    ep_file = State()
    ep_code = State()
    ep_del_code = State()
    wallet_add = State()
    channel_add = State()
    user_id = State()
    text_value = State()
    btn_value = State()
    setting_channel = State()
    setting_admin = State()
    setting_prices = State()
    anime_edit_value = State()
    social_add = State()


TXT_LABELS = {
    "start_text": "🎬 Start xabari",
    "premium_text": "💎 Premium matni",
    "help_text": "📞 Yordam matni",
}
BTN_LABELS = {
    "btn_codes": "🔑 Kod tugmasi",
    "btn_premium": "💎 Premium tugmasi",
    "btn_animes": "🎬 Animelar tugmasi",
    "btn_profile": "👤 Profil tugmasi",
}


# ================= PANEL =================
@admin_router.message(Command("admin"))
async def open_panel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⚙️ <b>Admin panel</b>", reply_markup=admin_panel_kb())


@admin_router.callback_query(F.data == "admin:panel")
async def panel(cl: CallbackQuery):
    await cl.message.edit_text("⚙️ <b>Admin panel</b>", reply_markup=admin_panel_kb())


# ================= STATISTIKA =================
@admin_router.callback_query(F.data == "admin:stats")
async def stats(cl: CallbackQuery):
    c = db.counts()
    text = (f"📊 <b>Statistika</b>\n\n"
            f"👥 Jami foydalanuvchilar: {c['users']}\n"
            f"🆕 Bugun qo'shilgan: {c['today']}\n"
            f"💎 Premium a'zolar: {c['premium']}\n"
            f"🎬 Animelar: {c['animes']}\n"
            f"📼 Qismlar: {c['episodes']}")
    await cl.message.edit_text(text, reply_markup=back_kb())


# ================= XABAR YUBORISH =================
@admin_router.callback_query(F.data == "admin:broadcast")
async def bc_start(cl: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.broadcast)
    await cl.message.edit_text("📢 Barcha foydalanuvchilarga yuboriladigan xabarni yuboring:",
                               reply_markup=back_kb())


@admin_router.message(AdminFSM.broadcast)
async def bc_go(m: Message, state: FSMContext):
    await state.clear()
    ids = db.all_user_ids()
    status = await m.answer(f"📤 Yuborilmoqda... 0/{len(ids)}")
    ok, fail = 0, 0
    for i, uid in enumerate(ids, 1):
        try:
            await m.copy_to(uid)
            ok += 1
        except TelegramRetryAfter as e:      # Telegram limiti — kutib, qayta urinamiz
            await asyncio.sleep(e.retry_after + 1)
            try:
                await m.copy_to(uid)
                ok += 1
            except Exception:
                fail += 1
        except Exception:                    # bot bloklangan / akkaunt o'chgan
            fail += 1
        await asyncio.sleep(0.05)            # sekundiga ~20 ta (Telegram limiti ~30)
        if i % 200 == 0:
            try:
                await status.edit_text(f"📤 Yuborilmoqda... {i}/{len(ids)}")
            except Exception:
                pass
    await m.answer(f"✅ Yuborildi: {ok}\n❌ Yetkazib bo'lmadi: {fail}", reply_markup=back_kb())


# ================= POST TAYYORLASH =================
@admin_router.callback_query(F.data == "admin:post")
async def post_start(cl: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.post_content)
    await cl.message.edit_text("📬 Kanalga yuboriladigan postni yuboring (matn/rasm/video):",
                               reply_markup=back_kb())


@admin_router.message(AdminFSM.post_content)
async def post_got(m: Message, state: FSMContext):
    await state.update_data(post_chat=m.chat.id, post_mid=m.message_id)
    await state.set_state(AdminFSM.post_button)
    await m.answer("🔘 Tugma qo'shish: <code>Matn | https://link</code> yuboring yoki /skip deb o'tkazing:")


@admin_router.message(AdminFSM.post_button)
async def post_button(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    ch = post_channels()
    if not ch:
        await state.clear()
        return await m.answer("❌ Post kanali o'rnatilmagan! (⚙️ Birlamchi sozlamalar → 📢 Post kanalini o'rnatish)",
                              reply_markup=back_kb())
    markup = None
    if not (m.text and m.text.strip() == "/skip"):
        parts = (m.text or "").split("|")
        if len(parts) != 2 or not parts[1].strip().startswith(("http://", "https://", "tg://")):
            return await m.answer("❌ Format xato. <code>Matn | https://link</code> yoki /skip:")
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=parts[0].strip(), url=parts[1].strip())]])
    ok, errs = await send_to_post_channels(
        bot, lambda c: bot.copy_message(chat_id=c, from_chat_id=data["post_chat"],
                                        message_id=data["post_mid"], reply_markup=markup))
    await state.clear()
    msg = f"✅ Post {ok} ta kanalga yuborildi!"
    if errs:
        msg += "\n\n❌ Xatolar (bot kanalda admin ekanini tekshiring):\n" + "\n".join(esc(e) for e in errs)
    await m.answer(msg, reply_markup=back_kb())


# ================= TEXT POST =================
@admin_router.callback_query(F.data == "admin:textpost")
async def tp_start(cl: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.textpost)
    await cl.message.edit_text("📩 Kanalga yuboriladigan matnni yuboring:", reply_markup=back_kb())


@admin_router.message(AdminFSM.textpost)
async def tp_go(m: Message, state: FSMContext, bot: Bot):
    if not m.text:
        return await m.answer("❌ Faqat matn yuboring:")
    ch = post_channels()
    if not ch:
        await state.clear()
        return await m.answer("❌ Post kanali o'rnatilmagan! (⚙️ Birlamchi sozlamalar)", reply_markup=back_kb())
    ok, errs = await send_to_post_channels(bot, lambda c: bot.send_message(c, m.html_text))
    await state.clear()
    msg = f"✅ Matn {ok} ta kanalga yuborildi!"
    if errs:
        msg += "\n\n❌ Xatolar:\n" + "\n".join(esc(e) for e in errs)
    await m.answer(msg, reply_markup=back_kb())


# ================= KONKURS =================
@admin_router.callback_query(F.data == "admin:contest")
async def ct_start(cl: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.contest)
    await cl.message.edit_text("📌 G'oliblar sonini kiriting:", reply_markup=back_kb())


@admin_router.message(AdminFSM.contest)
async def ct_go(m: Message, state: FSMContext):
    if not m.text or not m.text.strip().isdigit() or int(m.text.strip()) < 1:
        return await m.answer("❌ 1 yoki undan katta son kiriting:")
    n = int(m.text.strip())
    ids = db.all_user_ids()
    await state.clear()
    if not ids:
        return await m.answer("Foydalanuvchilar yo'q.", reply_markup=back_kb())
    winners = random.sample(ids, min(n, len(ids)))
    await m.answer("🏆 <b>G'oliblar:</b>\n" + "\n".join(f"• <code>{w}</code>" for w in winners),
                   reply_markup=back_kb())


# ================= ANIMELAR SOZLASH =================
@admin_router.callback_query(F.data == "admin:animes")
async def animes_menu(cl: CallbackQuery):
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Anime qo'shish", callback_data="admin:anime_add"),
          InlineKeyboardButton(text="➕ Qism qo'shish", callback_data="admin:ep_add"))
    b.row(InlineKeyboardButton(text="📋 Ro'yxat", callback_data="admin:anime_list"),
          InlineKeyboardButton(text="❌ Anime o'chirish", callback_data="admin:anime_del"),
          InlineKeyboardButton(text="❌ Qism o'chirish", callback_data="admin:ep_del"))
    b.row(InlineKeyboardButton(text="✏️ Anime tahrirlash (nom, tavsif, poster, fanadub)", callback_data="admin:anime_edit"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:panel"))
    await cl.message.edit_text("🎬 <b>Animelar sozlash</b>", reply_markup=b.as_markup())


@admin_router.callback_query(F.data == "admin:anime_add")
async def aa_start(cl: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.anime_title)
    await cl.message.edit_text("🎬 Anime nomini kiriting:", reply_markup=back_kb("admin:animes"))


@admin_router.message(AdminFSM.anime_title)
async def aa_title(m: Message, state: FSMContext):
    if not m.text:
        return await m.answer("❌ Anime nomini matn ko'rinishida yuboring:")
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminFSM.anime_desc)
    await m.answer("📝 Qisqacha tavsif kiriting (yo'q bo'lsa /skip):")


@admin_router.message(AdminFSM.anime_desc)
async def aa_desc(m: Message, state: FSMContext):
    if not m.text:
        return await m.answer("❌ Tavsifni matn ko'rinishida yuboring yoki /skip:")
    desc = "" if m.text.strip() == "/skip" else m.text.strip()
    await state.update_data(desc=desc)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔒 Ha, premium", callback_data="an_pr:1"),
          InlineKeyboardButton(text="🆓 Yo'q", callback_data="an_pr:0"))
    await m.answer("Bu anime premium bo'lsinmi?", reply_markup=b.as_markup())


@admin_router.callback_query(F.data.startswith("an_pr:"), AdminFSM.anime_desc)
async def aa_save(cl: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    aid = db.add_anime(data["title"], data.get("desc", ""), int(cl.data.split(":")[1]))
    await state.clear()
    await cl.message.edit_text(f"✅ Anime qo'shildi! ID: <code>{aid}</code>\n\n"
                               f"🖼 Poster va 🎙 fanadub nomini qo'shish uchun ✏️ Anime tahrirlash bo'limidan foydalaning.",
                               reply_markup=back_kb("admin:animes"))


@admin_router.callback_query(F.data == "admin:ep_add")
async def ea_start(cl: CallbackQuery):
    animes = db.get_animes()
    if not animes:
        return await cl.answer("❌ Avval anime qo'shing!", show_alert=True)
    b = InlineKeyboardBuilder()
    for a in animes:
        lock = "🔒" if a["is_premium"] else "🆓"
        b.row(InlineKeyboardButton(text=f"{lock} {a['title']}", callback_data=f"ep_an:{a['id']}"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:animes"))
    await cl.message.edit_text("🎬 Qaysi animega qism qo'shilsin?", reply_markup=b.as_markup())


@admin_router.callback_query(F.data.startswith("ep_an:"))
async def ea_pick(cl: CallbackQuery, state: FSMContext):
    await state.update_data(anime_id=int(cl.data.split(":")[1]))
    await state.set_state(AdminFSM.ep_number)
    await cl.message.edit_text("🔢 Qism raqamini kiriting:", reply_markup=back_kb("admin:ep_add"))


@admin_router.message(AdminFSM.ep_number)
async def ea_num(m: Message, state: FSMContext):
    if not m.text or not m.text.isdigit():
        return await m.answer("❌ Raqam kiriting:")
    await state.update_data(number=int(m.text))
    await state.set_state(AdminFSM.ep_file)
    await m.answer("📼 Qism videosini/faylini yuboring yoki /skip:")


@admin_router.message(AdminFSM.ep_file)
async def ea_file(m: Message, state: FSMContext):
    if m.text and m.text.strip() == "/skip":
        await state.update_data(file_id=None, file_type=None)
    elif m.video:
        await state.update_data(file_id=m.video.file_id, file_type="video")
    elif m.document:
        await state.update_data(file_id=m.document.file_id, file_type="document")
    else:
        return await m.answer("❌ Video/fayl yuboring yoki /skip:")
    await state.set_state(AdminFSM.ep_code)
    await m.answer("🔑 Shifrli kodni kiriting yoki /skip (avtomatik yaratiladi):")


@admin_router.message(AdminFSM.ep_code)
async def ea_code(m: Message, state: FSMContext):
    data = await state.get_data()
    if m.text and m.text.strip() != "/skip":
        code = m.text.strip().upper()
        if len(code) < 3 or " " in code:
            return await m.answer("❌ Kod kamida 3 belgi bo'lsin va bo'sh joysiz yozing:")
        if db.get_episode_by_code(code):
            return await m.answer("❌ Bu kod band! Boshqa kod kiriting yoki /skip:")
    else:
        while True:
            code = secrets.token_hex(3).upper()
            if not db.get_episode_by_code(code):
                break
    db.add_episode(data["anime_id"], data["number"], code, data.get("file_id"), data.get("file_type"))
    await state.clear()
    await m.answer(f"✅ Qism qo'shildi!\n🔑 Kodi: <code>{esc(code)}</code>", reply_markup=back_kb("admin:animes"))


@admin_router.callback_query(F.data == "admin:anime_list")
async def anime_list(cl: CallbackQuery):
    animes = db.get_animes()
    if not animes:
        return await cl.answer("❌ Animelar yo'q!", show_alert=True)
    text = "📋 <b>Animelar:</b>\n\n"
    for a in animes:
        lock = "🔒" if a["is_premium"] else "🆓"
        text += f"{lock} <b>{esc(a['title'])}</b>\n"
        for e in db.get_episodes(a["id"]):
            text += f"   📼 {e['episode_number']}-qism | kod: <code>{esc(e['code'])}</code>\n"
        text += "\n"
    if len(text) > 4000:  # Telegram xabar limiti 4096 belgi
        text = text[:3990] + "\n…(ro'yxat uzun, qisqartirildi)"
    await cl.message.edit_text(text, reply_markup=back_kb("admin:animes"))


# ================= ANIME TAHRIRLASH =================
def _ae_view(aid: int):
    a = db.get_anime(aid)
    if not a:
        return None, None
    text = (f"✏️ <b>{esc(a['title'])}</b>\n\n"
            f"📝 Tavsif: {esc(a['description'] or '-')}\n"
            f"🎙 Fanadub: {esc(a['fandub'] or '-')}\n"
            f"🖼 Poster: {'✅ bor' if a['poster'] else '❌ yo`q'}\n"
            f"🔐 Turi: {'🔒 Premium' if a['is_premium'] else '🆓 Bepul'}")
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✏️ Nomi", callback_data=f"ae_f:{aid}:title"),
          InlineKeyboardButton(text="📝 Tavsif", callback_data=f"ae_f:{aid}:description"))
    b.row(InlineKeyboardButton(text="🎙 Fanadub", callback_data=f"ae_f:{aid}:fandub"),
          InlineKeyboardButton(text="🖼 Poster", callback_data=f"ae_f:{aid}:poster"))
    b.row(InlineKeyboardButton(text="🔁 Premium/Bepul", callback_data=f"ae_prem:{aid}"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:anime_edit"))
    return text, b.as_markup()


@admin_router.callback_query(F.data == "admin:anime_edit")
async def ae_menu(cl: CallbackQuery):
    animes = db.get_animes()
    if not animes:
        return await cl.answer("❌ Animelar yo'q!", show_alert=True)
    b = InlineKeyboardBuilder()
    for a in animes:
        b.row(InlineKeyboardButton(text=f"✏️ {a['title']}", callback_data=f"ae_pick:{a['id']}"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:animes"))
    await cl.message.edit_text("Qaysi animeni tahrirlaysiz?", reply_markup=b.as_markup())


@admin_router.callback_query(F.data.startswith("ae_pick:"))
async def ae_pick(cl: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = _ae_view(int(cl.data.split(":")[1]))
    if not text:
        return await cl.answer("❌ Anime topilmadi!", show_alert=True)
    await cl.message.edit_text(text, reply_markup=kb)


@admin_router.callback_query(F.data.startswith("ae_prem:"))
async def ae_prem(cl: CallbackQuery):
    aid = int(cl.data.split(":")[1])
    a = db.get_anime(aid)
    if not a:
        return await cl.answer("❌ Anime topilmadi!", show_alert=True)
    db.update_anime(aid, "is_premium", 0 if a["is_premium"] else 1)
    text, kb = _ae_view(aid)
    await cl.message.edit_text(text, reply_markup=kb)
    await cl.answer("Saqlandi")


@admin_router.callback_query(F.data.startswith("ae_f:"))
async def ae_field(cl: CallbackQuery, state: FSMContext):
    _, aid, field = cl.data.split(":")
    await state.set_state(AdminFSM.anime_edit_value)
    await state.update_data(ae_id=int(aid), ae_field=field)
    prompts = {
        "title": "✏️ Yangi nomni yuboring:",
        "description": "📝 Yangi tavsifni yuboring (o'chirish uchun <code>-</code>):",
        "fandub": "🎙 Fanadub nomini yuboring (o'chirish uchun <code>-</code>):",
        "poster": "🖼 Poster rasmini yuboring (rasm sifatida):",
    }
    await cl.message.edit_text(prompts.get(field, "Yangi qiymatni yuboring:"),
                               reply_markup=back_kb(f"ae_pick:{aid}"))


@admin_router.message(AdminFSM.anime_edit_value)
async def ae_save(m: Message, state: FSMContext):
    d = await state.get_data()
    aid, field = d.get("ae_id"), d.get("ae_field")
    if not aid or not field:
        await state.clear()
        return await m.answer("❌ Xatolik. Qaytadan urinib ko'ring.", reply_markup=back_kb("admin:anime_edit"))
    if field == "poster":
        if not m.photo:
            return await m.answer("❌ Rasmni <b>rasm</b> ko'rinishida yuboring (fayl emas):")
        value = m.photo[-1].file_id
    else:
        if not m.text:
            return await m.answer("❌ Matn yuboring:")
        value = m.text.strip()
        if value == "-" and field in ("description", "fandub"):
            value = ""
        if field == "title" and not value:
            return await m.answer("❌ Nom bo'sh bo'lmasin:")
    db.update_anime(aid, field, value)
    await state.clear()
    text, kb = _ae_view(aid)
    await m.answer("✅ Yangilandi!\n\n" + text, reply_markup=kb)


@admin_router.callback_query(F.data == "admin:anime_del")
async def anime_del_menu(cl: CallbackQuery):
    animes = db.get_animes()
    if not animes:
        return await cl.answer("❌ Animelar yo'q!", show_alert=True)
    b = InlineKeyboardBuilder()
    for a in animes:
        b.row(InlineKeyboardButton(text=f"❌ {a['title']}", callback_data=f"an_del:{a['id']}"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:animes"))
    await cl.message.edit_text("O'chiriladigan animeni tanlang:", reply_markup=b.as_markup())


@admin_router.callback_query(F.data.startswith("an_del:"))
async def anime_del(cl: CallbackQuery):
    db.delete_anime(int(cl.data.split(":")[1]))
    await cl.message.edit_text("✅ Anime va uning qismlari o'chirildi!", reply_markup=back_kb("admin:animes"))


@admin_router.callback_query(F.data == "admin:ep_del")
async def ep_del_start(cl: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.ep_del_code)
    await cl.message.edit_text("❌ O'chiriladigan qismning kodini yuboring:", reply_markup=back_kb("admin:animes"))


@admin_router.message(AdminFSM.ep_del_code)
async def ep_del_go(m: Message, state: FSMContext):
    ok = db.delete_episode_by_code((m.text or "").strip().upper())
    await state.clear()
    await m.answer("✅ Qism o'chirildi!" if ok else "❌ Kod topilmadi.", reply_markup=back_kb("admin:animes"))


# ================= HAMYONLAR =================
@admin_router.callback_query(F.data == "admin:wallets")
async def wallets_menu(cl: CallbackQuery):
    ws = db.get_wallets(active_only=False)
    text = "💳 <b>Hamyonlar:</b>\n\n"
    if ws:
        text += "\n".join(f"• {esc(w['name'])}: <code>{esc(w['card_number'])}</code>" +
                          (" ✅" if w["is_active"] else " ❌") for w in ws)
    else:
        text += "Hozircha hamyon qo'shilmagan."
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Hamyon qo'shish", callback_data="admin:wallet_add"))
    for w in ws:
        b.row(InlineKeyboardButton(text=f"❌ {w['name']}", callback_data=f"wal_del:{w['id']}"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:panel"))
    await cl.message.edit_text(text, reply_markup=b.as_markup())


@admin_router.callback_query(F.data == "admin:wallet_add")
async def wallet_add_start(cl: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.wallet_add)
    await cl.message.edit_text("💳 Format: <code>Nomi | karta raqami</code>", reply_markup=back_kb("admin:wallets"))


@admin_router.message(AdminFSM.wallet_add)
async def wallet_add(m: Message, state: FSMContext):
    parts = (m.text or "").split("|")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return await m.answer("❌ Format xato. Qayta kiriting:")
    db.add_wallet(parts[0].strip(), parts[1].strip())
    await state.clear()
    await m.answer("✅ Hamyon qo'shildi!", reply_markup=back_kb("admin:wallets"))


@admin_router.callback_query(F.data.startswith("wal_del:"))
async def wallet_del(cl: CallbackQuery):
    db.delete_wallet(int(cl.data.split(":")[1]))
    await cl.answer("✅ O'chirildi!")
    await wallets_menu(cl)


# ================= FOYDALANUVCHINI BOSHQARISH =================
@admin_router.callback_query(F.data == "admin:user_manage")
async def um_start(cl: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.user_id)
    await cl.message.edit_text("🔍 Foydalanuvchi ID sini yuboring:", reply_markup=back_kb())


@admin_router.message(AdminFSM.user_id)
async def um_go(m: Message, state: FSMContext):
    if not m.text or not m.text.strip().isdigit():
        return await m.answer("❌ ID raqam bo'lishi kerak:")
    uid = int(m.text.strip())
    u = db.get_user(uid)
    await state.clear()
    if not u:
        return await m.answer("❌ Foydalanuvchi topilmadi.", reply_markup=back_kb())
    prem = ("💎 " + (u["premium_until"] or "-")[:10]) if u["is_premium"] else "❌"
    ban = "ha" if u["is_banned"] else "yo'q"
    text = (f"👤 <b>{esc(u['full_name'] or '-')}</b> (@{esc(u['username'] or '-')})\n"
            f"🆔 <code>{uid}</code>\n"
            f"⭐ Premium: {prem}\n"
            f"🚫 Ban: {ban}\n"
            f"🔑 Ishlatilgan kodlar: {u['codes_used']}")
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="💎 7 kun", callback_data=f"um_prem:{uid}:7"),
          InlineKeyboardButton(text="💎 30 kun", callback_data=f"um_prem:{uid}:30"),
          InlineKeyboardButton(text="💎 90 kun", callback_data=f"um_prem:{uid}:90"))
    act = "🚫 Banlash" if not u["is_banned"] else "✅ Bandan chiqarish"
    b.row(InlineKeyboardButton(text=act, callback_data=f"um_ban:{uid}:{u['is_banned']}"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:panel"))
    await m.answer(text, reply_markup=b.as_markup())


@admin_router.callback_query(F.data.startswith("um_prem:"))
async def um_prem(cl: CallbackQuery, bot: Bot):
    _, uid, days = cl.data.split(":")
    until = db.set_premium(int(uid), int(days))
    await cl.answer("✅ Premium berildi!")
    try:
        await bot.send_message(int(uid), f"🎉 Sizga <b>{days} kunlik Premium</b> berildi!\n"
                                         f"⏳ Amal qilish muddati: {until[:10]} gacha")
    except Exception:
        pass
    await cl.message.edit_text(f"✅ Premium faollashtirildi ({until[:10]} gacha).", reply_markup=back_kb())


@admin_router.callback_query(F.data.startswith("um_ban:"))
async def um_ban(cl: CallbackQuery):
    _, uid, cur = cl.data.split(":")
    if cur == "1":
        db.unban_user(int(uid))
    else:
        db.ban_user(int(uid))
    await cl.answer("✅ Bajarildi!")
    await cl.message.edit_text("✅ Bajarildi.", reply_markup=back_kb())


# ================= PREMIUM TASDIQLASH (foydalanuvchi cheki) =================
@admin_router.callback_query(F.data.startswith("prem:ok:"))
async def prem_ok(cl: CallbackQuery, bot: Bot):
    rid = cl.data.split(":")[2]
    p = db.get_payment(rid)
    if not p:
        return await cl.answer("❌ So'rov topilmadi.", show_alert=True)
    if not db.resolve_payment(rid, "ok"):
        await cl.answer("ℹ️ Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
        try:
            await cl.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    until = db.set_premium(p["user_id"], p["days"])
    await cl.answer("✅ Premium berildi!")
    await mark_message(cl.message, f"\n\n✅ <b>TASDIQLANDI</b> ({esc(cl.from_user.full_name)})")
    try:
        await bot.send_message(p["user_id"],
                               f"🎉 To'lovingiz tasdiqlandi! <b>{p['days']} kunlik Premium</b> faollashtirildi.\n"
                               f"⏳ Amal qilish muddati: {until[:10]} gacha")
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("prem:no:"))
async def prem_no(cl: CallbackQuery, bot: Bot):
    rid = cl.data.split(":")[2]
    p = db.get_payment(rid)
    if not p:
        return await cl.answer("❌ So'rov topilmadi.", show_alert=True)
    if not db.resolve_payment(rid, "no"):
        await cl.answer("ℹ️ Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
        try:
            await cl.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    await cl.answer("❌ Rad etildi!")
    await mark_message(cl.message, f"\n\n❌ <b>RAD ETILDI</b> ({esc(cl.from_user.full_name)})")
    try:
        await bot.send_message(p["user_id"], "❌ To'lovingiz tasdiqlanmadi. Admin bilan bog'laning.")
    except Exception:
        pass


# ================= KANALLAR (majburiy obuna) =================
@admin_router.callback_query(F.data == "admin:channels")
async def channels_menu(cl: CallbackQuery):
    chs = db.get_channels()
    socials = get_socials()
    text = "📣 <b>Majburiy obuna kanallari:</b>\n\n"
    if chs:
        text += "\n".join(f"• {'📨' if c['kind'] == 'request' else '📢'} <code>{esc(c['channel_id'])}</code> — {esc(c['channel_url'])}"
                          for c in chs)
    else:
        text += "Hozircha kanal qo'shilmagan."
    if socials:
        text += "\n\n🌐 <b>Ijtimoiy tarmoqlar:</b>\n" + "\n".join(f"• {esc(n)} — {esc(u)}" for n, u in socials)
    text += "\n\n📨 — zayafka (so'rov) kanali\n⚠️ Bot har bir kanalda admin bo'lishi shart!"
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="admin:channel_add"))
    if len(socials) < 2:
        b.row(InlineKeyboardButton(text="🌐 Instagram/YouTube qo'shish", callback_data="admin:social_add"))
    for c in chs:
        b.row(InlineKeyboardButton(text=f"❌ {c['channel_id']}", callback_data=f"ch_del:{c['id']}"))
    for i, (n, _) in enumerate(socials):
        b.row(InlineKeyboardButton(text=f"❌ {n}", callback_data=f"soc_del:{i}"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:panel"))
    await cl.message.edit_text(text, reply_markup=b.as_markup())


@admin_router.callback_query(F.data == "admin:channel_add")
async def ch_add_start(cl: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.channel_add)
    await cl.message.edit_text("📣 Oddiy kanal: <code>@kanal | https://t.me/kanal</code>\n\n"
                               "📨 Zayafka (so'rov bilan qo'shiladigan) kanal:\n"
                               "<code>-100123456789 | https://t.me/+AbCdEf | zayafka</code>",
                               reply_markup=back_kb("admin:channels"))


@admin_router.message(AdminFSM.channel_add)
async def ch_add(m: Message, state: FSMContext, bot: Bot):
    parts = [x.strip() for x in (m.text or "").split("|")]
    if len(parts) not in (2, 3):
        return await m.answer("❌ Format xato. Qayta kiriting:")
    cid, url = parts[0], parts[1]
    kind = "request" if len(parts) == 3 and parts[2].lower() in ("zayafka", "request", "sorov", "so'rov") else "channel"
    if not url.startswith(("http://", "https://")):
        return await m.answer("❌ Havola https:// bilan boshlanishi kerak. Qayta kiriting:")
    try:
        await bot.get_chat(cid)
        me = await bot.get_me()
        member = await bot.get_chat_member(cid, me.id)
        if member.status not in ("administrator", "creator"):
            return await m.answer("❌ Bot bu kanalda ADMIN emas! Avval botni kanalga admin qiling va qayta kiriting:")
    except Exception as e:
        return await m.answer(f"❌ Kanal topilmadi! Bot kanalga qo'shilganmi? Qayta kiriting:\n\n<code>{esc(str(e))}</code>")
    db.add_channel(cid, url, kind)
    await state.clear()
    await m.answer("✅ Kanal qo'shildi!" + (" (📨 zayafka)" if kind == "request" else ""),
                   reply_markup=back_kb("admin:channels"))


@admin_router.callback_query(F.data == "admin:social_add")
async def social_add_start(cl: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.social_add)
    await cl.message.edit_text("🌐 Format: <code>Instagram | https://instagram.com/sahifa</code>\n"
                               "yoki <code>YouTube | https://youtube.com/@kanal</code>\n\n"
                               "ℹ️ Ijtimoiy tarmoq obunasi tekshirilmaydi, faqat tugma sifatida ko'rsatiladi.",
                               reply_markup=back_kb("admin:channels"))


@admin_router.message(AdminFSM.social_add)
async def social_add(m: Message, state: FSMContext):
    parts = [x.strip() for x in (m.text or "").split("|")]
    if len(parts) != 2 or not parts[0] or not parts[1].startswith(("http://", "https://")):
        return await m.answer("❌ Format xato. <code>Nom | https://havola</code>")
    socials = get_socials()
    if len(socials) >= 2:
        await state.clear()
        return await m.answer("❌ Eng ko'pi 2 ta ijtimoiy tarmoq qo'shish mumkin.", reply_markup=back_kb("admin:channels"))
    socials.append([parts[0], parts[1]])
    db.set_setting("socials", json.dumps(socials))
    await state.clear()
    await m.answer("✅ Qo'shildi!", reply_markup=back_kb("admin:channels"))


@admin_router.callback_query(F.data.startswith("soc_del:"))
async def social_del(cl: CallbackQuery):
    socials = get_socials()
    idx = int(cl.data.split(":")[1])
    if 0 <= idx < len(socials):
        socials.pop(idx)
        db.set_setting("socials", json.dumps(socials))
    await cl.answer("✅ O'chirildi!")
    await channels_menu(cl)


@admin_router.callback_query(F.data.startswith("ch_del:"))
async def ch_del(cl: CallbackQuery):
    db.delete_channel(int(cl.data.split(":")[1]))
    await cl.answer("✅ O'chirildi!")
    await channels_menu(cl)


# ================= TUGMALAR =================
@admin_router.callback_query(F.data == "admin:buttons")
async def buttons_menu(cl: CallbackQuery):
    b = InlineKeyboardBuilder()
    for k, label in BTN_LABELS.items():
        b.row(InlineKeyboardButton(text=f"{label}: {db.get_text(k)}", callback_data=f"btn:{k}"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:panel"))
    await cl.message.edit_text("🔘 <b>Foydalanuvchi tugmalarini sozlash:</b>", reply_markup=b.as_markup())


@admin_router.callback_query(F.data.startswith("btn:"))
async def btn_pick(cl: CallbackQuery, state: FSMContext):
    key = cl.data.split(":", 1)[1]
    await state.update_data(bkey=key)
    await state.set_state(AdminFSM.btn_value)
    await cl.message.edit_text(f"🔘 {BTN_LABELS.get(key, key)} uchun yangi nom yuboring:",
                               reply_markup=back_kb("admin:buttons"))


@admin_router.message(AdminFSM.btn_value)
async def btn_save(m: Message, state: FSMContext):
    if not m.text or not m.text.strip() or m.text.startswith("/"):
        return await m.answer("❌ Tugma nomini oddiy matn ko'rinishida yuboring:")
    data = await state.get_data()
    db.set_text(data["bkey"], m.text.strip())
    await state.clear()
    await m.answer("✅ Tugma nomi saqlandi! (Foydalanuvchilarda /start bosilgach yangilanadi)",
                   reply_markup=back_kb("admin:buttons"))


# ================= MATNLAR =================
@admin_router.callback_query(F.data == "admin:texts")
async def texts_menu(cl: CallbackQuery):
    b = InlineKeyboardBuilder()
    for k, label in TXT_LABELS.items():
        b.row(InlineKeyboardButton(text=label, callback_data=f"txt:{k}"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:panel"))
    await cl.message.edit_text("📄 <b>Matnlarni sozlash</b> — o'zgartiriladiganini tanlang:",
                               reply_markup=b.as_markup())


@admin_router.callback_query(F.data.startswith("txt:"))
async def txt_pick(cl: CallbackQuery, state: FSMContext):
    key = cl.data.split(":", 1)[1]
    await state.update_data(tkey=key)
    await state.set_state(AdminFSM.text_value)
    await cl.message.edit_text(f"📝 {TXT_LABELS.get(key, key)} uchun yangi matn yuboring:\n\n"
                               f"Joriy:\n<code>{esc(db.get_text(key))}</code>",
                               reply_markup=back_kb("admin:texts"))


@admin_router.message(AdminFSM.text_value)
async def txt_save(m: Message, state: FSMContext):
    if not m.text:
        return await m.answer("❌ Matn ko'rinishida yuboring:")
    data = await state.get_data()
    db.set_text(data["tkey"], m.html_text)  # formatlash (qalin, kursiv...) saqlanadi
    await state.clear()
    await m.answer("✅ Matn saqlandi!", reply_markup=back_kb("admin:texts"))


# ================= BIRLAMCHI SOZLAMALAR =================
@admin_router.callback_query(F.data == "admin:settings")
async def settings_menu(cl: CallbackQuery):
    chs = post_channels()
    text = (f"⚙️ <b>Birlamchi sozlamalar</b>\n\n"
            f"📢 Post kanallari: <code>{esc(', '.join(chs) if chs else '❌ o`rnatilmagan')}</code>\n"
            f"🔒 Kontent cheklash: {'✅ yoqilgan' if protect_on() else '❌ o`chiq'}\n"
            f"🤖 Bot holati: {'🟢 yoqilgan' if bot_is_on() else '🔴 o`chirilgan'}\n"
            f"👑 Adminlar: {', '.join(str(a) for a in ADMIN_IDS)}")
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📢 Post kanali qo'shish", callback_data="admin:set_channel"),
          InlineKeyboardButton(text="🗑 Post kanallarini tozalash", callback_data="admin:post_clear"))
    b.row(InlineKeyboardButton(text="🔒 Kontent cheklashni almashtirish", callback_data="admin:toggle_protect"))
    b.row(InlineKeyboardButton(text="🤖 Botni yoqish/o'chirish", callback_data="admin:toggle_bot"))
    b.row(InlineKeyboardButton(text="💰 Premium narxlarini o'zgartirish", callback_data="admin:set_prices"))
    b.row(InlineKeyboardButton(text="➕ Admin qo'shish", callback_data="admin:add_admin"))
    b.row(InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:panel"))
    await cl.message.edit_text(text, reply_markup=b.as_markup())


@admin_router.callback_query(F.data == "admin:post_clear")
async def post_clear(cl: CallbackQuery):
    db.set_setting("post_channel", "")
    await cl.answer("🗑 Post kanallari tozalandi")
    await settings_menu(cl)


@admin_router.callback_query(F.data == "admin:toggle_protect")
async def toggle_protect(cl: CallbackQuery):
    db.set_setting("protect", "0" if protect_on() else "1")
    await cl.answer("Saqlandi")
    await settings_menu(cl)


@admin_router.callback_query(F.data == "admin:toggle_bot")
async def toggle_bot(cl: CallbackQuery):
    db.set_setting("bot_on", "0" if bot_is_on() else "1")
    await cl.answer("Saqlandi")
    await settings_menu(cl)


@admin_router.callback_query(F.data == "admin:set_channel")
async def set_ch_start(cl: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.setting_channel)
    await cl.message.edit_text("📢 Post kanalini qo'shish (@kanal yoki -100... ID). Xohlagancha kanal qo'shsangiz bo'ladi, post hammasiga bir vaqtda ketadi:\n\n"
                               "⚠️ Bot kanalda admin bo'lishi shart!",
                               reply_markup=back_kb("admin:settings"))


@admin_router.message(AdminFSM.setting_channel)
async def set_ch(m: Message, state: FSMContext, bot: Bot):
    cid = (m.text or "").strip()
    try:
        await bot.get_chat(cid)
    except Exception as e:
        return await m.answer(f"❌ Kanal topilmadi! Qayta kiriting:\n\n<code>{esc(str(e))}</code>")
    chs = post_channels()
    if cid not in chs:
        chs.append(cid)
    db.set_setting("post_channel", ",".join(chs))
    await state.clear()
    await m.answer(f"✅ Post kanali qo'shildi! Jami: {len(chs)} ta", reply_markup=back_kb("admin:settings"))


@admin_router.callback_query(F.data == "admin:set_prices")
async def set_pr_start(cl: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.setting_prices)
    await cl.message.edit_text("💰 Format: <code>kun:narx, kun:narx</code>\n"
                               "Masalan: <code>7:5000, 30:15000, 90:35000</code>",
                               reply_markup=back_kb("admin:settings"))


@admin_router.message(AdminFSM.setting_prices)
async def set_pr_save(m: Message, state: FSMContext):
    try:
        pairs = [p.split(":") for p in (m.text or "").split(",")]
        prices = sorted([[int(a.strip()), int(b.strip())] for a, b in pairs])
        if not prices or any(d < 1 or p < 1 for d, p in prices):
            raise ValueError
        db.set_text("prices", json.dumps(prices))
    except Exception:
        return await m.answer("❌ Format xato. Qayta kiriting:")
    await state.clear()
    await m.answer("✅ Narxlar saqlandi!", reply_markup=back_kb("admin:settings"))


@admin_router.callback_query(F.data == "admin:add_admin")
async def add_adm_start(cl: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.setting_admin)
    await cl.message.edit_text("👑 Yangi adminning Telegram ID sini yuboring:",
                               reply_markup=back_kb("admin:settings"))


@admin_router.message(AdminFSM.setting_admin)
async def add_adm_save(m: Message, state: FSMContext):
    if not m.text or not m.text.strip().isdigit():
        return await m.answer("❌ ID raqam kiriting:")
    uid = int(m.text.strip())
    if uid not in ADMIN_IDS:
        ADMIN_IDS.append(uid)
        save_admins()  # bazaga yoziladi — bot qayta ishga tushsa ham saqlanadi
    await state.clear()
    await m.answer("✅ Admin qo'shildi!", reply_markup=back_kb("admin:settings"))


# =====================================================================
#                        FOYDALANUVCHI QISMI
# =====================================================================
user_router = Router()


class UserFSM(StatesGroup):
    code = State()
    receipt = State()


# ---------------- majburiy obuna ----------------
_SUB_CACHE: dict = {}   # user_id -> obuna tasdig'i amal qiladigan vaqt
_SUB_TTL = 60           # soniya (har xabarda Telegram API ni urmaslik uchun)


async def is_subscribed(bot: Bot, user_id: int, use_cache: bool = True) -> bool:
    """Foydalanuvchi barcha majburiy kanallarga obuna bo'lganmi"""
    now = time.time()
    if use_cache and _SUB_CACHE.get(user_id, 0) > now:
        return True
    for ch in db.get_channels():
        try:
            member = await bot.get_chat_member(ch["channel_id"], user_id)
            left = member.status in ("left", "kicked")
            restricted_out = member.status == "restricted" and not getattr(member, "is_member", True)
            if left or restricted_out:
                # zayafka kanalida so'rov yuborgan bo'lsa — obuna hisoblanadi
                if ch["kind"] == "request" and db.has_join_request(ch["channel_id"], user_id):
                    continue
                _SUB_CACHE.pop(user_id, None)
                return False
        except Exception as e:
            # Odatda: bot kanalda admin emas. Foydalanuvchini to'smaymiz, lekin logga yozamiz.
            log.warning("Obunani tekshirib bo'lmadi (%s): %s — bot kanalda admin ekanini tekshiring",
                        ch["channel_id"], e)
            continue
    if len(_SUB_CACHE) > 10000:
        _SUB_CACHE.clear()
    _SUB_CACHE[user_id] = now + _SUB_TTL
    return True


async def send_sub_prompt(target: Message):
    b = InlineKeyboardBuilder()
    has_request = False
    for ch in db.get_channels():
        if ch["kind"] == "request":
            has_request = True
            b.row(InlineKeyboardButton(text=f"📨 {ch['channel_id']} (so'rov yuborish)", url=ch["channel_url"]))
        else:
            b.row(InlineKeyboardButton(text=f"📢 {ch['channel_id']}", url=ch["channel_url"]))
    for name, url in get_socials():
        b.row(InlineKeyboardButton(text=f"🌐 {name}", url=url))
    b.row(InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_sub"))
    text = "❗ <b>Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:</b>"
    if has_request:
        text += "\n\n📨 belgili kanallarda «Qo'shilish so'rovi»ni yuboring, keyin ✅ tugmasini bosing."
    await target.answer(text, reply_markup=b.as_markup())


@user_router.chat_join_request()
async def on_join_request(ev: ChatJoinRequest):
    """Zayafka kanaliga so'rov yuborgan foydalanuvchini eslab qolamiz"""
    for ch in db.get_channels():
        cid = ch["channel_id"]
        if cid == str(ev.chat.id) or (ev.chat.username and cid.lower() == "@" + ev.chat.username.lower()):
            db.add_join_request(cid, ev.from_user.id)
            _SUB_CACHE.pop(ev.from_user.id, None)


class GuardMiddleware(BaseMiddleware):
    """
    Har bir xabar/tugma oldidan:
      1) foydalanuvchini bazaga yozadi,
      2) bloklanganlarni to'xtatadi,
      3) majburiy obuna bo'lmaganlarni to'xtatadi (HAR QANDAY amalda, faqat /start da emas).
    """

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        if isinstance(event, Message) and event.chat.type != "private":
            return  # guruhlarda ishlamaydi

        u = db.get_user(user.id)
        if not u:
            db.add_user(user.id, user.username, user.full_name)
            u = db.get_user(user.id)

        if user.id in ADMIN_IDS:
            return await handler(event, data)

        if not bot_is_on():
            if isinstance(event, CallbackQuery):
                return await event.answer("⏸ Bot vaqtincha o'chirilgan.", show_alert=True)
            return await event.answer("⏸ Bot vaqtincha o'chirilgan. Keyinroq urinib ko'ring.")

        if u and u["is_banned"]:
            if isinstance(event, CallbackQuery):
                return await event.answer("🚫 Siz botdan bloklangansiz.", show_alert=True)
            return await event.answer("🚫 Siz botdan bloklangansiz.")

        if not db.get_channels():
            return await handler(event, data)
        # /start va "Obunani tekshirish" tugmasi o'z tekshiruvini o'zi qiladi
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)

        if await is_subscribed(data["bot"], user.id):
            return await handler(event, data)

        state = data.get("state")
        if state:
            await state.clear()
        if isinstance(event, CallbackQuery):
            await event.answer("❗ Avval kanallarga obuna bo'ling!", show_alert=True)
            if event.message:
                await send_sub_prompt(event.message)
        else:
            await send_sub_prompt(event)


user_router.message.outer_middleware(GuardMiddleware())
user_router.callback_query.outer_middleware(GuardMiddleware())


def _is(key: str, default: str):
    """Menyu tugmasi matniga mos keladimi (matnlar admin paneldan o'zgartirilishi mumkin)"""
    return lambda m: m.text == db.get_text(key, default)


def _not_menu(m) -> bool:
    """Menyu tugmasi bosilgan bo'lsa — kod/chek holati uni 'yutib yubormasin'"""
    return (m.text or "") not in menu_texts()


# ---------------- start / bekor ----------------
@user_router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    if not await is_subscribed(bot, m.from_user.id, use_cache=False):
        return await send_sub_prompt(m)
    await m.answer(db.get_text("start_text"), reply_markup=user_menu_kb())


@user_router.message(Command("bekor"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=user_menu_kb())


@user_router.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(db.get_text("help_text"))


@user_router.callback_query(F.data == "check_sub")
async def check_sub(cl: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, cl.from_user.id, use_cache=False):
        await cl.answer("✅ Obuna tasdiqlandi!")
        try:
            await cl.message.delete()
        except Exception:
            pass
        await cl.message.answer(db.get_text("start_text"), reply_markup=user_menu_kb())
    else:
        await cl.answer("❌ Hali hamma kanallarga obuna bo'lmagansiz!", show_alert=True)


# ================= KOD KIRITISH =================
@user_router.message(_is("btn_codes", "🔑 Kod kiritish"))
async def ask_code(m: Message, state: FSMContext):
    await state.set_state(UserFSM.code)
    await m.answer("🔑 Qismni ochish uchun shifrli kodni kiriting (bekor qilish: /bekor):")


@user_router.message(UserFSM.code, _not_menu)
async def use_code(m: Message, state: FSMContext, bot: Bot):
    code = (m.text or "").strip().upper()
    if not code:
        return await m.answer("❌ Kodni matn ko'rinishida yuboring yoki /bekor:")
    ep = db.get_episode_by_code(code)
    anime = db.get_anime(ep["anime_id"]) if ep else None
    if not ep or not anime:
        return await m.answer("❌ Bunday kod topilmadi. Qayta urinib ko'ring yoki /bekor:")
    if anime["is_premium"] and not db.has_premium(m.from_user.id):
        await state.clear()
        return await m.answer("🔒 Bu qism faqat 💎 <b>premium</b> foydalanuvchilar uchun!\n\n" +
                              db.get_text("premium_text"), reply_markup=premium_kb())
    caption = f"🎬 <b>{esc(anime['title'])}</b> — {ep['episode_number']}-qism"
    if ep["file_id"]:
        try:
            if ep["file_type"] == "video":
                await bot.send_video(m.chat.id, ep["file_id"], caption=caption, protect_content=protect_on())
            else:
                await bot.send_document(m.chat.id, ep["file_id"], caption=caption, protect_content=protect_on())
        except Exception as e:
            log.error("Qismni yuborib bo'lmadi (kod %s): %s", code, e)
            await state.clear()
            return await m.answer("⚠️ Qismni yuborishda xatolik. Iltimos, admin bilan bog'laning.")
    else:
        await m.answer(caption + "\n⚠️ <i>(Bu qism hali yuklanmagan)</i>")
    db.use_code(m.from_user.id)
    await state.clear()


# ================= PREMIUM =================
@user_router.message(_is("btn_premium", "💎 Premium"))
async def show_premium(m: Message, state: FSMContext):
    await state.clear()
    prices = get_prices()
    wallets = db.get_wallets()
    text = db.get_text("premium_text") + "\n\n💰 <b>Narxlar:</b>\n"
    text += "\n".join(f"• 💎 {d} kun — {p:,} so'm" for d, p in prices)
    if wallets:
        text += "\n\n💳 <b>To'lov kartalari:</b>\n"
        text += "\n".join(f"• {esc(w['name'])}: <code>{esc(w['card_number'])}</code>" for w in wallets)
    text += "\n\nTo'lovdan so'ng chekni botga yuboring — admin tasdiqlagach 💎 premium faollashtiriladi."
    await m.answer(text, reply_markup=premium_kb())


@user_router.callback_query(F.data.startswith("buy:"))
async def buy(cl: CallbackQuery, state: FSMContext):
    try:
        days = int(cl.data.split(":")[1])
    except ValueError:
        return await cl.answer("❌ Xato.", show_alert=True)
    amount = next((p for d, p in get_prices() if d == days), None)
    if amount is None:
        return await cl.answer("❌ Bu tarif endi mavjud emas. Premium bo'limini qayta oching.", show_alert=True)
    await state.update_data(days=days, amount=amount)
    await state.set_state(UserFSM.receipt)
    await cl.message.edit_text(f"💎 Siz <b>{days} kunlik premium</b> ({amount:,} so'm) sotib olmoqchisiz.\n\n"
                               f"To'lovni kartalarga amalga oshirib, chekni (rasm yoki skrinshot) shu yerga yuboring "
                               f"yoki /bekor:")
    await cl.answer()


@user_router.message(UserFSM.receipt, _not_menu)
async def receipt(m: Message, state: FSMContext, bot: Bot):
    if not (m.photo or m.document):
        return await m.answer("❌ Iltimos, to'lov chekini <b>rasm (skrinshot) yoki fayl</b> ko'rinishida yuboring.\n"
                              "Bekor qilish: /bekor")
    data = await state.get_data()
    days, amount = data.get("days"), data.get("amount")
    if not days:
        await state.clear()
        return await m.answer("❌ Xatolik. Iltimos, 💎 Premium bo'limidan qaytadan boshlang.")
    rid = secrets.token_hex(4)
    db.create_payment(rid, m.from_user.id, days, amount)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✅ Premium berish", callback_data=f"prem:ok:{rid}"),
          InlineKeyboardButton(text="❌ Rad etish", callback_data=f"prem:no:{rid}"))
    uname = f"@{esc(m.from_user.username)}" if m.from_user.username else "-"
    caption = (f"💎 <b>Premium so'rovi</b>\n"
               f"👤 {esc(m.from_user.full_name)} ({uname})\n"
               f"🆔 <code>{m.from_user.id}</code>\n"
               f"📦 {days} kun — {amount:,} so'm")
    sent = 0
    for adm in ADMIN_IDS:
        try:
            await m.copy_to(adm, caption=caption, reply_markup=b.as_markup())
            sent += 1
        except Exception as e:
            log.warning("Adminga (%s) chek yuborilmadi: %s", adm, e)
    await state.clear()
    if sent:
        await m.answer("✅ Chekingiz adminga yuborildi. Tasdiqlangach premium faollashtiriladi.")
    else:
        db.resolve_payment(rid, "failed")
        await m.answer("⚠️ Chekni adminga yetkazib bo'lmadi. Iltimos, keyinroq urinib ko'ring.")


# ================= ANIMELAR =================
def _animes_list_kb():
    b = InlineKeyboardBuilder()
    for a in db.get_animes():
        lock = "🔒" if a["is_premium"] else "🆓"
        b.row(InlineKeyboardButton(text=f"{lock} {a['title']}", callback_data=f"u_an:{a['id']}"))
    return b.as_markup()


@user_router.message(_is("btn_animes", "🎬 Animelar"))
async def animes(m: Message, state: FSMContext):
    await state.clear()
    if not db.get_animes():
        return await m.answer("🎬 Hozircha animelar qo'shilmagan.")
    await m.answer("🎬 <b>Animelar ro'yxati:</b>\n(qismlarni 🔑 kod orqali ochasiz)", reply_markup=_animes_list_kb())


@user_router.callback_query(F.data.startswith("u_an:"))
async def anime_info(cl: CallbackQuery):
    a = db.get_anime(int(cl.data.split(":")[1]))
    if not a:
        return await cl.answer("❌ Anime topilmadi!", show_alert=True)
    eps = db.get_episodes(a["id"])
    lock = "🔒 Premium" if a["is_premium"] else "🆓 Bepul"
    desc = a["description"] or "-"
    if len(desc) > 600:
        desc = desc[:600] + "…"
    text = f"🎬 <b>{esc(a['title'])}</b>\n📝 {esc(desc)}\n"
    if a["fandub"]:
        text += f"🎙 Fanadub: {esc(a['fandub'])}\n"
    text += (f"🔐 {lock}\n\n📼 Qismlar: {len(eps)} ta\n"
             f"Qism ochish uchun asosiy menyudan 🔑 Kod kiritish tugmasidan foydalaning.")
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔙 Animelar ro'yxati", callback_data="u_back"))
    if a["poster"]:
        try:
            await cl.message.delete()
        except Exception:
            pass
        await cl.message.answer_photo(a["poster"], caption=text, reply_markup=b.as_markup())
    else:
        await cl.message.edit_text(text, reply_markup=b.as_markup())
    await cl.answer()


@user_router.callback_query(F.data == "u_back")
async def back_to_animes(cl: CallbackQuery):
    try:
        await cl.message.delete()
    except Exception:
        pass
    await cl.message.answer("🎬 <b>Animelar ro'yxati:</b>\n(qismlarni 🔑 kod orqali ochasiz)",
                            reply_markup=_animes_list_kb())
    await cl.answer()


# ================= PROFIL =================
@user_router.message(_is("btn_profile", "👤 Profil"))
async def profile(m: Message, state: FSMContext):
    await state.clear()
    u = db.get_user(m.from_user.id)
    if db.has_premium(m.from_user.id):
        prem = f"💎 {(u['premium_until'] or '')[:10]} gacha"
    else:
        prem = "❌"
    await m.answer(f"👤 <b>Sizning profilingiz</b>\n\n"
                   f"🆔 <code>{m.from_user.id}</code>\n"
                   f"⭐ Premium: {prem}\n"
                   f"🔑 Ishlatilgan kodlar: {u['codes_used']}")


# =====================================================================
#                              ISHGA TUSHIRISH
# =====================================================================
async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    if ":" not in BOT_TOKEN or BOT_TOKEN.startswith("BU_YERGA"):
        print("❌ BOT_TOKEN kiritilmagan! Fayl boshidagi SOZLAMALAR bo'limiga @BotFather tokenini yozing "
              "(yoki BOT_TOKEN environment variable qo'ying).")
        return

    db.init()
    load_admins()
    if ADMIN_IDS == [123456789]:
        log.warning("⚠️ ADMIN_IDS hali o'zgartirilmagan! O'z Telegram ID raqamingizni yozing.")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    dp.include_router(user_router)

    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    log.info("✅ Bot ishga tushdi: @%s | adminlar: %s | baza: %s", me.username, ADMIN_IDS, DB_NAME)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
