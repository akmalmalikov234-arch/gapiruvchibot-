import os
import re
import math
import time
import html
import asyncio
import sqlite3
import tempfile
import shutil
import logging
import secrets
import threading
import subprocess
import sys
from pathlib import Path
from functools import wraps

# ============================================================
# AUTO INSTALL - Render uchun
# ============================================================

PACKAGES = {
    "telegram": "python-telegram-bot==21.6",
    "edge_tts": "edge-tts",
    "speech_recognition": "SpeechRecognition",
    "imageio_ffmpeg": "imageio-ffmpeg",
    "flask": "flask",
}

for module, package in PACKAGES.items():
    try:
        __import__(module)
    except ImportError:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", package]
        )

import edge_tts
import speech_recognition as sr
import imageio_ffmpeg

from flask import Flask, request, jsonify, send_from_directory

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import RetryAfter, TimedOut, NetworkError

# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

CARD_NUMBER = os.getenv("CARD_NUMBER", "8600000000000000")
CARD_OWNER = os.getenv("CARD_OWNER", "ISM FAMILIYA")

PORT = int(os.getenv("PORT", "10000"))
WEB_URL = os.getenv("WEB_URL", "")

DB_PATH = os.getenv("DB_PATH", "bot.db")

START_BONUS = int(os.getenv("START_BONUS", "2000"))
REF_BONUS = int(os.getenv("REF_BONUS", "500"))

TTS_PRICE = float(os.getenv("TTS_PRICE", "0.3"))
STT_PRICE = float(os.getenv("STT_PRICE", "5"))

MAX_TTS = int(os.getenv("MAX_TTS", "3000"))
MAX_STT_SECONDS = int(os.getenv("MAX_STT_SECONDS", "120"))

MIN_TOPUP = int(os.getenv("MIN_TOPUP", "1000"))

BOT_ON = os.getenv("BOT_ON", "1") == "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("voicebot")

# ============================================================
# APP
# ============================================================

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

# ============================================================
# DATABASE
# ============================================================

db_lock = threading.RLock()

db = sqlite3.connect(
    DB_PATH,
    check_same_thread=False,
    timeout=30
)

db.row_factory = sqlite3.Row

with db_lock:
    db.executescript("""
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        name TEXT DEFAULT '',
        balance INTEGER DEFAULT 0,
        lang TEXT DEFAULT 'uz-UZ',
        voice TEXT DEFAULT 'uz-UZ-MadinaNeural',
        stt_lang TEXT DEFAULT 'uz-UZ',
        banned INTEGER DEFAULT 0,
        refs INTEGER DEFAULT 0,
        ref_by INTEGER,
        created_at INTEGER DEFAULT 0,
        last_seen INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid INTEGER,
        amount INTEGER,
        status TEXT DEFAULT 'wait',
        created_at INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid INTEGER,
        kind TEXT,
        amount REAL DEFAULT 0,
        cost INTEGER DEFAULT 0,
        created_at INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS channels (
        chat_id INTEGER PRIMARY KEY,
        title TEXT,
        link TEXT
    );

    CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY
    );
    """)

# ============================================================
# HELPERS
# ============================================================

def now():
    return int(time.time())


def fmt(n):
    return f"{int(n):,}".replace(",", " ")


def execute(sql, args=()):
    with db_lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur


def fetchone(sql, args=()):
    with db_lock:
        return db.execute(sql, args).fetchone()


def fetchall(sql, args=()):
    with db_lock:
        return db.execute(sql, args).fetchall()


def is_admin(uid):
    if uid == ADMIN_ID:
        return True

    row = fetchone(
        "SELECT 1 FROM admins WHERE id=?",
        (uid,)
    )

    return bool(row)


def user(uid):
    row = fetchone(
        "SELECT * FROM users WHERE id=?",
        (uid,)
    )

    return row


def ensure_user(tg_user, ref=None):
    uid = tg_user.id

    row = user(uid)

    if row:
        execute(
            """
            UPDATE users
            SET username=?, name=?, last_seen=?
            WHERE id=?
            """,
            (
                tg_user.username or "",
                tg_user.full_name or "",
                now(),
                uid
            )
        )
        return user(uid)

    balance = START_BONUS

    valid_ref = (
        ref
        and ref != uid
        and user(ref)
    )

    if valid_ref:
        balance += REF_BONUS

    execute(
        """
        INSERT INTO users
        (id,username,name,balance,ref_by,created_at,last_seen)
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            uid,
            tg_user.username or "",
            tg_user.full_name or "",
            balance,
            ref if valid_ref else None,
            now(),
            now()
        )
    )

    if valid_ref:
        execute(
            """
            UPDATE users
            SET balance=balance+?, refs=refs+1
            WHERE id=?
            """,
            (REF_BONUS, ref)
        )

    return user(uid)


def add_balance(uid, amount):
    execute(
        "UPDATE users SET balance=balance+? WHERE id=?",
        (amount, uid)
    )


def spend(uid, amount):
    with db_lock:
        row = db.execute(
            "SELECT balance FROM users WHERE id=?",
            (uid,)
        ).fetchone()

        if not row or row["balance"] < amount:
            return False

        db.execute(
            "UPDATE users SET balance=balance-? WHERE id=?",
            (amount, uid)
        )

        db.commit()

    return True


def usage(uid, kind, amount, cost):
    execute(
        """
        INSERT INTO usage(uid,kind,amount,cost,created_at)
        VALUES(?,?,?,?,?)
        """,
        (uid, kind, amount, cost, now())
    )


# ============================================================
# VOICES
# ============================================================

VOICES = {
    "uz": {
        "name": "🇺🇿 O'zbek",
        "stt": "uz-UZ",
        "voices": [
            ("Madina", "uz-UZ-MadinaNeural"),
            ("Sardor", "uz-UZ-SardorNeural"),
        ],
    },

    "ru": {
        "name": "🇷🇺 Rus",
        "stt": "ru-RU",
        "voices": [
            ("Svetlana", "ru-RU-SvetlanaNeural"),
            ("Dmitry", "ru-RU-DmitryNeural"),
        ],
    },

    "en": {
        "name": "🇺🇸 English",
        "stt": "en-US",
        "voices": [
            ("Jenny", "en-US-JennyNeural"),
            ("Guy", "en-US-GuyNeural"),
            ("Aria", "en-US-AriaNeural"),
        ],
    },

    "tr": {
        "name": "🇹🇷 Turk",
        "stt": "tr-TR",
        "voices": [
            ("Emel", "tr-TR-EmelNeural"),
            ("Ahmet", "tr-TR-AhmetNeural"),
        ],
    },

    "kk": {
        "name": "🇰🇿 Qozoq",
        "stt": "kk-KZ",
        "voices": [
            ("Aigul", "kk-KZ-AigulNeural"),
            ("Daulet", "kk-KZ-DauletNeural"),
        ],
    },

    "az": {
        "name": "🇦🇿 Ozarbayjon",
        "stt": "az-AZ",
        "voices": [
            ("Banu", "az-AZ-BanuNeural"),
            ("Babek", "az-AZ-BabekNeural"),
        ],
    },

    "de": {
        "name": "🇩🇪 Nemis",
        "stt": "de-DE",
        "voices": [
            ("Katja", "de-DE-KatjaNeural"),
            ("Conrad", "de-DE-ConradNeural"),
        ],
    },

    "fr": {
        "name": "🇫🇷 Fransuz",
        "stt": "fr-FR",
        "voices": [
            ("Denise", "fr-FR-DeniseNeural"),
            ("Henri", "fr-FR-HenriNeural"),
        ],
    },

    "es": {
        "name": "🇪🇸 Ispan",
        "stt": "es-ES",
        "voices": [
            ("Elvira", "es-ES-ElviraNeural"),
            ("Alvaro", "es-ES-AlvaroNeural"),
        ],
    },

    "it": {
        "name": "🇮🇹 Italyan",
        "stt": "it-IT",
        "voices": [
            ("Elsa", "it-IT-ElsaNeural"),
            ("Diego", "it-IT-DiegoNeural"),
        ],
    },

    "ar": {
        "name": "🇸🇦 Arab",
        "stt": "ar-SA",
        "voices": [
            ("Zariyah", "ar-SA-ZariyahNeural"),
            ("Hamed", "ar-SA-HamedNeural"),
        ],
    },

    "fa": {
        "name": "🇮🇷 Fors",
        "stt": "fa-IR",
        "voices": [
            ("Dilara", "fa-IR-DilaraNeural"),
            ("Farid", "fa-IR-FaridNeural"),
        ],
    },

    "hi": {
        "name": "🇮🇳 Hind",
        "stt": "hi-IN",
        "voices": [
            ("Swara", "hi-IN-SwaraNeural"),
            ("Madhur", "hi-IN-MadhurNeural"),
        ],
    },

    "ur": {
        "name": "🇵🇰 Urdu",
        "stt": "ur-PK",
        "voices": [
            ("Uzma", "ur-PK-UzmaNeural"),
            ("Asad", "ur-PK-AsadNeural"),
        ],
    },

    "zh": {
        "name": "🇨🇳 Xitoy",
        "stt": "zh-CN",
        "voices": [
            ("Xiaoxiao", "zh-CN-XiaoxiaoNeural"),
            ("Yunxi", "zh-CN-YunxiNeural"),
        ],
    },

    "ja": {
        "name": "🇯🇵 Yapon",
        "stt": "ja-JP",
        "voices": [
            ("Nanami", "ja-JP-NanamiNeural"),
            ("Keita", "ja-JP-KeitaNeural"),
        ],
    },

    "ko": {
        "name": "🇰🇷 Koreys",
        "stt": "ko-KR",
        "voices": [
            ("SunHi", "ko-KR-SunHiNeural"),
            ("InJoon", "ko-KR-InJoonNeural"),
        ],
    },

    "pt": {
        "name": "🇵🇹 Portugal",
        "stt": "pt-PT",
        "voices": [
            ("Raquel", "pt-PT-RaquelNeural"),
            ("Duarte", "pt-PT-DuarteNeural"),
        ],
    },

    "nl": {
        "name": "🇳🇱 Golland",
        "stt": "nl-NL",
        "voices": [
            ("Colette", "nl-NL-ColetteNeural"),
            ("Maarten", "nl-NL-MaartenNeural"),
        ],
    },

    "pl": {
        "name": "🇵🇱 Polyak",
        "stt": "pl-PL",
        "voices": [
            ("Zofia", "pl-PL-ZofiaNeural"),
            ("Marek", "pl-PL-MarekNeural"),
        ],
    },
}

# ============================================================
# TTS
# ============================================================

def tts_cost(text):
    return max(
        1,
        math.ceil(len(text) * TTS_PRICE)
    )


async def make_tts(text, voice, rate="+0%", pitch="+0Hz"):
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "voice.mp3")

    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            pitch=pitch
        )

        await communicate.save(path)

        return tmp, path

    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


# ============================================================
# STT
# ============================================================

def convert_and_recognize(src, lang):
    tmp = tempfile.mkdtemp()

    wav = os.path.join(tmp, "voice.wav")

    try:
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                src,
                "-ar",
                "16000",
                "-ac",
                "1",
                wav
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        recognizer = sr.Recognizer()

        with sr.AudioFile(wav) as source:
            audio = recognizer.record(source)

        return recognizer.recognize_google(
            audio,
            language=lang
        )

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# UI
# ============================================================

def menu():
    return ReplyKeyboardMarkup(
        [
            ["🎙 Ovoz → Matn"],
            ["🔊 Matn → Ovoz"],
            ["💰 Balans", "💳 To'ldirish"],
            ["👥 Referal", "📱 Mini App"],
        ],
        resize_keyboard=True
    )


def main_buttons():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎙 STT",
                callback_data="stt"
            ),
            InlineKeyboardButton(
                "🔊 TTS",
                callback_data="tts"
            )
        ],
        [
            InlineKeyboardButton(
                "💰 Balans",
                callback_data="balance"
            ),
            InlineKeyboardButton(
                "💳 To'ldirish",
                callback_data="topup"
            )
        ],
        [
            InlineKeyboardButton(
                "🌐 Til",
                callback_data="language"
            ),
            InlineKeyboardButton(
                "👥 Referal",
                callback_data="ref"
            )
        ]
    ])


# ============================================================
# SUBSCRIPTION
# ============================================================

async def check_subscription(bot, uid):
    rows = fetchall(
        "SELECT chat_id,title,link FROM channels"
    )

    missing = []

    for row in rows:
        try:
            member = await bot.get_chat_member(
                row["chat_id"],
                uid
            )

            if member.status in ("left", "kicked"):
                missing.append(row)

        except Exception as e:
            log.warning(
                "Subscription check error: %s",
                e
            )

    return missing


async def gate(update, context):
    uid = update.effective_user.id

    if is_admin(uid):
        return True

    u = user(uid)

    if u and u["banned"]:
        await update.effective_message.reply_text(
            "🚫 Siz bloklangansiz."
        )
        return False

    if not BOT_ON:
        await update.effective_message.reply_text(
            "🛠 Bot vaqtincha texnik xizmatda."
        )
        return False

    missing = await check_subscription(
        context.bot,
        uid
    )

    if missing:
        buttons = []

        for ch in missing:
            buttons.append([
                InlineKeyboardButton(
                    "📢 " + (ch["title"] or "Kanal"),
                    url=ch["link"]
                )
            ])

        buttons.append([
            InlineKeyboardButton(
                "✅ Tekshirish",
                callback_data="check_sub"
            )
        ])

        await update.effective_message.reply_text(
            "📢 Botdan foydalanish uchun kanallarga obuna bo'ling.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

        return False

    return True


# ============================================================
# START
# ============================================================

async def start(update, context):
    tg = update.effective_user

    ref = None

    if context.args:
        arg = context.args[0]

        if arg.startswith("ref_"):
            try:
                ref = int(arg[4:])
            except:
                pass

    ensure_user(tg, ref)

    if not await gate(update, context):
        return

    await update.message.reply_text(
        f"👋 Assalomu alaykum, {tg.first_name}!\n\n"
        "🎙 Men ovozni matnga aylantiraman.\n"
        "🔊 Matnni tabiiy neural ovozga aylantiraman.\n\n"
        "Pastdagi menyudan foydalaning.",
        reply_markup=menu()
    )

# ============================================================
# BALANCE
# ============================================================

async def balance(update, context):
    u = user(update.effective_user.id)

    await update.effective_message.reply_text(
        "💰 BALANS\n\n"
        f"Balans: {fmt(u['balance'])} so'm\n\n"
        f"🎙 STT: {STT_PRICE:g} so'm/soniya\n"
        f"🔊 TTS: {TTS_PRICE:g} so'm/belgi"
    )


# ============================================================
# TOPUP
# ============================================================

async def topup(update, context):
    await update.effective_message.reply_text(
        "💳 BALANSNI TO'LDIRISH\n\n"
        f"Minimal summa: {fmt(MIN_TOPUP)} so'm\n\n"
        "Kerakli summani yozing:"
    )

    context.user_data["state"] = "amount"


async def handle_amount(update, context):
    text = update.message.text.strip()

    digits = re.sub(
        r"\D",
        "",
        text
    )

    if not digits:
        await update.message.reply_text(
            "❗ Summani raqamda yozing."
        )
        return

    amount = int(digits)

    if amount < MIN_TOPUP:
        await update.message.reply_text(
            f"❗ Minimal summa: {fmt(MIN_TOPUP)} so'm"
        )
        return

    context.user_data["amount"] = amount
    context.user_data["state"] = "receipt"

    await update.message.reply_text(
        "💳 TO'LOV MA'LUMOTI\n\n"
        f"Summa: {fmt(amount)} so'm\n\n"
        f"Karta:\n"
        f"<code>{html.escape(CARD_NUMBER)}</code>\n\n"
        f"👤 {html.escape(CARD_OWNER)}\n\n"
        "To'lovni amalga oshiring va "
        "chek rasmini shu yerga yuboring.",
        parse_mode="HTML"
    )


# ============================================================
# RECEIPT
# ============================================================

async def receipt(update, context):
    uid = update.effective_user.id

    amount = context.user_data.get("amount")

    if not amount:
        await update.message.reply_text(
            "Avval balans to'ldirish summasini tanlang."
        )
        return

    cur = execute(
        """
        INSERT INTO payments(uid,amount,status,created_at)
        VALUES(?,?,?,?)
        """,
        (
            uid,
            amount,
            "wait",
            now()
        )
    )

    pid = cur.lastrowid

    caption = (
        f"💳 YANGI TO'LOV #{pid}\n\n"
        f"👤 {update.effective_user.full_name}\n"
        f"🆔 {uid}\n"
        f"💰 {fmt(amount)} so'm"
    )

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Tasdiqlash",
                callback_data=f"pay_ok:{pid}"
            ),
            InlineKeyboardButton(
                "❌ Rad etish",
                callback_data=f"pay_no:{pid}"
            )
        ]
    ])

    sent = False

    for aid in {ADMIN_ID} | {
        r["id"]
        for r in fetchall("SELECT id FROM admins")
    }:
        try:
            if update.message.photo:
                await context.bot.send_photo(
                    aid,
                    update.message.photo[-1].file_id,
                    caption=caption,
                    reply_markup=buttons
                )
            else:
                await context.bot.send_document(
                    aid,
                    update.message.document.file_id,
                    caption=caption,
                    reply_markup=buttons
                )

            sent = True

        except Exception as e:
            log.warning(
                "Admin receipt error: %s",
                e
            )

    context.user_data.clear()

    if sent:
        await update.message.reply_text(
            "✅ Chek adminga yuborildi.\n"
            "Tasdiqlangandan keyin balansingiz to'ldiriladi.",
            reply_markup=menu()
        )
    else:
        execute(
            "DELETE FROM payments WHERE id=?",
            (pid,)
        )

        await update.message.reply_text(
            "❗ Chekni yuborishda xatolik."
        )


# ============================================================
# LANGUAGE
# ============================================================

async def language_menu(update, context):
    buttons = []

    for key, data in VOICES.items():
        buttons.append([
            InlineKeyboardButton(
                data["name"],
                callback_data=f"lang:{key}"
            )
        ])

    await update.effective_message.reply_text(
        "🌐 Tilni tanlang:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def voice_menu(update, context, lang):
    data = VOICES.get(lang)

    if not data:
        return

    buttons = []

    for name, voice in data["voices"]:
        buttons.append([
            InlineKeyboardButton(
                name,
                callback_data=f"voice:{lang}:{voice}"
            )
        ])

    await update.effective_message.reply_text(
        f"{data['name']}\n\nOvozni tanlang:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# STT
# ============================================================

async def stt_start(update, context):
    uid = update.effective_user.id

    context.user_data["state"] = "stt"

    u = user(uid)

    await update.effective_message.reply_text(
        "🎙 Ovozli xabar yuboring.\n\n"
        f"🌐 Til: {u['stt_lang']}"
    )


async def handle_voice(update, context):
    uid = update.effective_user.id

    u = user(uid)

    if not u:
        ensure_user(update.effective_user)
        u = user(uid)

    if context.user_data.get("state") != "stt":
        await update.message.reply_text(
            "Avval 🎙 Ovoz → Matn tugmasini bosing."
        )
        return

    voice = update.message.voice

    if not voice:
        return

    duration = int(voice.duration or 1)

    if duration > MAX_STT_SECONDS:
        await update.message.reply_text(
            f"❗ Maksimal audio: {MAX_STT_SECONDS} soniya."
        )
        return

    cost = max(
        1,
        math.ceil(duration * STT_PRICE)
    )

    if u["balance"] < cost:
        await update.message.reply_text(
            f"❗ Balans yetarli emas.\n"
            f"Kerak: {fmt(cost)} so'm\n"
            f"Sizda: {fmt(u['balance'])} so'm"
        )
        return

    if not spend(uid, cost):
        await update.message.reply_text(
            "❗ Balans yetarli emas."
        )
        return

    msg = await update.message.reply_text(
        "⏳ Ovoz tahlil qilinmoqda..."
    )

    tmp = tempfile.mkdtemp()

    try:
        ogg = os.path.join(
            tmp,
            "audio.ogg"
        )

        tg_file = await context.bot.get_file(
            voice.file_id
        )

        await tg_file.download_to_drive(
            ogg
        )

        loop = asyncio.get_running_loop()

        text = await loop.run_in_executor(
            None,
            convert_and_recognize,
            ogg,
            u["stt_lang"]
        )

        usage(
            uid,
            "stt",
            duration,
            cost
        )

        new_balance = user(uid)["balance"]

        await msg.edit_text(
            "📝 NATIJA\n\n"
            f"{text}\n\n"
            f"━━━━━━━━━━━━\n"
            f"⏱ {duration} soniya\n"
            f"💸 {fmt(cost)} so'm\n"
            f"💰 Balans: {fmt(new_balance)} so'm"
        )

    except sr.UnknownValueError:

        add_balance(uid, cost)

        await msg.edit_text(
            "❗ Ovoz tushunilmadi.\n\n"
            "Pul yechilmadi."
        )

    except Exception as e:

        log.exception(e)

        add_balance(uid, cost)

        await msg.edit_text(
            "❗ STT xatoligi.\n"
            "Pul balansga qaytarildi."
        )

    finally:
        shutil.rmtree(
            tmp,
            ignore_errors=True
        )


# ============================================================
# TTS
# ============================================================

async def tts_start(update, context):
    uid = update.effective_user.id

    context.user_data["state"] = "tts"

    u = user(uid)

    await update.effective_message.reply_text(
        "🔊 Matn yuboring.\n\n"
        f"👤 Ovoz: {u['voice']}\n"
        f"🌐 Til: {u['lang']}"
    )


async def handle_tts(update, context):
    uid = update.effective_user.id

    text = update.message.text.strip()

    if not text:
        return

    if len(text) > MAX_TTS:
        await update.message.reply_text(
            f"❗ Maksimal: {MAX_TTS} belgi."
        )
        return

    u = user(uid)

    cost = tts_cost(text)

    if u["balance"] < cost:
        await update.message.reply_text(
            "❗ Balans yetarli emas.\n\n"
            f"Kerak: {fmt(cost)} so'm\n"
            f"Sizda: {fmt(u['balance'])} so'm"
        )
        return

    if not spend(uid, cost):
        await update.message.reply_text(
            "❗ Balans yetarli emas."
        )
        return

    wait = await update.message.reply_text(
        "🔊 Neural ovoz tayyorlanmoqda..."
    )

    tmp = None

    try:
        tmp, path = await make_tts(
            text,
            u["voice"],
            rate=context.user_data.get(
                "rate",
                "+0%"
            ),
            pitch=context.user_data.get(
                "pitch",
                "+0Hz"
            )
        )

        new_balance = user(uid)["balance"]

        with open(path, "rb") as audio:

            await update.message.reply_voice(
                audio,
                caption=(
                    f"🔊 Tayyor\n\n"
                    f"👤 {u['voice']}\n"
                    f"📝 {len(text)} belgi\n"
                    f"💸 {fmt(cost)} so'm\n"
                    f"💰 Balans: {fmt(new_balance)} so'm"
                )
            )

        usage(
            uid,
            "tts",
            len(text),
            cost
        )

        await wait.delete()

    except Exception as e:

        log.exception(e)

        add_balance(uid, cost)

        await wait.edit_text(
            "❗ TTS xatoligi.\n"
            "Pul balansga qaytarildi."
        )

    finally:

        if tmp:
            shutil.rmtree(
                tmp,
                ignore_errors=True
            )


# ============================================================
# CALLBACK
# ============================================================

async def callback(update, context):
    q = update.callback_query

    await q.answer()

    uid = q.from_user.id

    data = q.data

    # -----------------------------------------
    # subscription
    # -----------------------------------------

    if data == "check_sub":

        missing = await check_subscription(
            context.bot,
            uid
        )

        if missing:
            await q.answer(
                "Hali barcha kanallarga obuna bo'lmagansiz.",
                show_alert=True
            )
            return

        await q.message.edit_text(
            "✅ Obuna tasdiqlandi."
        )

        return

    # -----------------------------------------
    # main
    # -----------------------------------------

    if data == "stt":

        context.user_data["state"] = "stt"

        u = user(uid)

        await q.message.reply_text(
            "🎙 Ovozli xabar yuboring.\n\n"
            f"🌐 Til: {u['stt_lang']}"
        )

        return

    if data == "tts":

        context.user_data["state"] = "tts"

        u = user(uid)

        await q.message.reply_text(
            "🔊 Matn yuboring.\n\n"
            f"👤 Ovoz: {u['voice']}"
        )

        return

    if data == "balance":

        u = user(uid)

        await q.message.reply_text(
            f"💰 Balans: {fmt(u['balance'])} so'm"
        )

        return

    if data == "topup":

        await topup(update, context)

        return

    if data == "language":

        await language_menu(
            update,
            context
        )

        return

    if data == "ref":

        bot = await context.bot.get_me()

        u = user(uid)

        link = (
            f"https://t.me/{bot.username}"
            f"?start=ref_{uid}"
        )

        await q.message.reply_text(
            "👥 REFERAL\n\n"
            f"Har bir do'st: {fmt(REF_BONUS)} so'm\n\n"
            f"🔗 {link}\n\n"
            f"Takliflar: {u['refs']}"
        )

        return

    # -----------------------------------------
    # language
    # -----------------------------------------

    if data.startswith("lang:"):

        lang = data.split(":")[1]

        await voice_menu(
            update,
            context,
            lang
        )

        return

    # -----------------------------------------
    # voice
    # -----------------------------------------

    if data.startswith("voice:"):

        _, lang, voice = data.split(
            ":",
            2
        )

        if lang not in VOICES:
            return

        execute(
            """
            UPDATE users
            SET lang=?, voice=?, stt_lang=?
            WHERE id=?
            """,
            (
                VOICES[lang]["name"],
                voice,
                VOICES[lang]["stt"],
                uid
            )
        )

        await q.message.reply_text(
            f"✅ Ovoz tanlandi:\n{voice}"
        )

        return

    # -----------------------------------------
    # payments
    # -----------------------------------------

    if data.startswith("pay_ok:"):

        if not is_admin(uid):
            return

        pid = int(data.split(":")[1])

        payment = fetchone(
            """
            SELECT *
            FROM payments
            WHERE id=? AND status='wait'
            """,
            (pid,)
        )

        if not payment:
            await q.message.reply_text(
                "❗ Bu to'lov allaqachon ko'rilgan."
            )
            return

        execute(
            """
            UPDATE payments
            SET status='ok'
            WHERE id=?
            """,
            (pid,)
        )

        add_balance(
            payment["uid"],
            payment["amount"]
        )

        await context.bot.send_message(
            payment["uid"],
            "✅ To'lov tasdiqlandi!\n\n"
            f"+{fmt(payment['amount'])} so'm\n"
            f"Balans: {fmt(user(payment['uid'])['balance'])} so'm"
        )

        await q.edit_message_caption(
            caption=(
                (q.message.caption or "")
                + "\n\n✅ TASDIQLANDI"
            )
        )

        return

    if data.startswith("pay_no:"):

        if not is_admin(uid):
            return

        pid = int(data.split(":")[1])

        payment = fetchone(
            """
            SELECT *
            FROM payments
            WHERE id=? AND status='wait'
            """,
            (pid,)
        )

        if not payment:
            return

        execute(
            """
            UPDATE payments
            SET status='no'
            WHERE id=?
            """,
            (pid,)
        )

        await context.bot.send_message(
            payment["uid"],
            "❌ To'lov rad etildi."
        )

        await q.edit_message_caption(
            caption=(
                (q.message.caption or "")
                + "\n\n❌ RAD ETILDI"
            )
        )


# ============================================================
# ADMIN
# ============================================================

async def admin(update, context):

    uid = update.effective_user.id

    if not is_admin(uid):
        return

    users = fetchone(
        "SELECT COUNT(*) AS c FROM users"
    )["c"]

    balance = fetchone(
        "SELECT COALESCE(SUM(balance),0) AS c FROM users"
    )["c"]

    payments = fetchone(
        """
        SELECT COALESCE(SUM(amount),0) AS c
        FROM payments
        WHERE status='ok'
        """
    )["c"]

    await update.message.reply_text(
        "🛠 ADMIN PANEL\n\n"
        f"👥 Users: {users}\n"
        f"💰 User balanslari: {fmt(balance)} so'm\n"
        f"💳 Tasdiqlangan to'lovlar: {fmt(payments)} so'm\n\n"
        "Admin funksiyalarini keyinchalik Mini App orqali ham boshqarish mumkin."
    )


# ============================================================
# TEXT ROUTER
# ============================================================

async def text_router(update, context):

    if not update.message:
        return

    uid = update.effective_user.id

    ensure_user(
        update.effective_user
    )

    if not await gate(
        update,
        context
    ):
        return

    text = update.message.text.strip()

    state = context.user_data.get(
        "state"
    )

    if state == "amount":

        await handle_amount(
            update,
            context
        )

        return

    if state == "tts":

        await handle_tts(
            update,
            context
        )

        return

    if text == "🎙 Ovoz → Matn":

        await stt_start(
            update,
            context
        )

        return

    if text == "🔊 Matn → Ovoz":

        await tts_start(
            update,
            context
        )

        return

    if text == "💰 Balans":

        await balance(
            update,
            context
        )

        return

    if text == "💳 To'ldirish":

        await topup(
            update,
            context
        )

        return

    if text == "👥 Referal":

        bot = await context.bot.get_me()

        u = user(uid)

        link = (
            f"https://t.me/{bot.username}"
            f"?start=ref_{uid}"
        )

        await update.message.reply_text(
            "👥 REFERAL\n\n"
            f"Bonus: {fmt(REF_BONUS)} so'm\n\n"
            f"{link}\n\n"
            f"Takliflar: {u['refs']}"
        )

        return

    if text == "📱 Mini App":

        url = WEB_URL

        if not url:

            await update.message.reply_text(
                "📱 Mini App hali URL bilan sozlanmagan.\n\n"
                "Render deploy qilingandan keyin WEB_URL "
                "ga Render URL'ini qo'yasiz."
            )

            return

        await update.message.reply_text(
            "📱 Mini App:",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🚀 Ochish",
                        web_app={
                            "url": url
                        }
                    )
                ]
            ])
        )

        return

    await update.message.reply_text(
        "Menyudan foydalaning 👇",
        reply_markup=menu()
    )


# ============================================================
# API AUTH
# ============================================================

def api_user():

    data = request.headers.get(
        "X-Telegram-User"
    )

    if not data:
        return None

    try:
        uid = int(data)

        return user(uid)

    except:
        return None


# ============================================================
# MINI APP API
# ============================================================

@app.route("/")
def index():

    return send_from_directory(
        WEB_DIR,
        "index.html"
    )


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "service": "voicebot"
    })


@app.route("/api/me")
def api_me():

    u = api_user()

    if not u:
        return jsonify({
            "error": "unauthorized"
        }), 401

    return jsonify({
        "id": u["id"],
        "name": u["name"],
        "username": u["username"],
        "balance": u["balance"],
        "voice": u["voice"],
        "lang": u["lang"],
        "refs": u["refs"]
    })


@app.route("/api/voices")
def api_voices():

    result = []

    for key, data in VOICES.items():

        result.append({
            "code": key,
            "name": data["name"],
            "stt": data["stt"],
            "voices": [
                {
                    "name": name,
                    "id": voice
                }
                for name, voice in data["voices"]
            ]
        })

    return jsonify(result)


# ============================================================
# WEB SERVER
# ============================================================

def run_web():

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True
    )


# ============================================================
# TELEGRAM
# ============================================================

def build_bot():

    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    application = (
        Application
        .builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .build()
    )

    private = filters.ChatType.PRIVATE

    application.add_handler(
        CommandHandler(
            "start",
            start,
            private
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin,
            private
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback
        )
    )

    application.add_handler(
        MessageHandler(
            private & filters.VOICE,
            handle_voice
        )
    )

    application.add_handler(
        MessageHandler(
            private & filters.PHOTO,
            receipt
        )
    )

    application.add_handler(
        MessageHandler(
            private & filters.Document.ALL,
            receipt
        )
    )

    application.add_handler(
        MessageHandler(
            private & filters.TEXT & ~filters.COMMAND,
            text_router
        )
    )

    return application


def run_bot():

    while True:

        try:

            bot = build_bot()

            log.info(
                "Telegram bot starting..."
            )

            bot.run_polling(
                drop_pending_updates=True
            )

            break

        except (
            TimedOut,
            NetworkError
        ) as e:

            log.error(
                "Telegram network error: %s",
                e
            )

            time.sleep(10)

        except Exception as e:

            log.exception(e)

            time.sleep(10)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    run_bot()
