import asyncio
import base64
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import socket
import uuid
import hashlib
import hmac
import urllib.parse
import json
import http.cookiejar
import urllib.error
import urllib.request
from collections import defaultdict, deque
from pathlib import Path

# ====================== bothost: папка данных и .env ======================
# На bothost постоянный том — /app/data (платформа задаёт DATA_DIR). Всё остальное стирается при деплое.
DATA_DIR = Path(os.getenv("DATA_DIR") or "/app/data")
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    DATA_DIR = Path(".")  # запуск на своём компьютере


def _load_env_file(path: Path) -> None:
    """Читает KEY=VALUE из файла. Нужен, если на тарифе нет раздела «Переменные окружения»."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_env_file(DATA_DIR / ".env")  # /app/data/.env — переживает деплой, в Git не попадает
_load_env_file(Path(".env"))


def _update_ytdlp() -> bool:
    """Ставит свежую версию yt-dlp (Instagram и YouTube часто меняют защиту)."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "--quiet", "--no-cache-dir", "yt-dlp[default]"],
            timeout=240,
            capture_output=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def _installed_ytdlp_version() -> str:
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import yt_dlp.version as v; print(v.__version__)"],
            capture_output=True, text=True, timeout=60,
        )
        return r.stdout.strip() or "?"
    except Exception:
        return "?"


# при каждом запуске бота подтягиваем свежий yt-dlp (отключить: AUTO_UPDATE_YTDLP=0)
if os.getenv("AUTO_UPDATE_YTDLP", "1") == "1":
    _update_ytdlp()

import yt_dlp  # noqa: E402
from aiohttp import web  # noqa: E402
from aiogram import Bot, Dispatcher, F  # noqa: E402
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    ReactionTypeEmoji,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
    Message,
)

# ====================== настройки ======================
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL = "@horizonmarkettj"  # канал для обязательной подписки
CHANNEL_URL = "https://t.me/horizonmarkettj"
CREATOR_URL = "https://t.me/IDIEVW"
SHOP_URL = "https://korvontj.shop"  # ссылка для кнопки «Посмотреть» (магазин)
BOT_VERSION = "2026-11-20 two-bots+instagram v2"
BOT_NAME = "Taj Saver"  # так будет называться любая присланная музыка
HERE = Path(__file__).resolve().parent
COVER = HERE / "cover.jpg"  # обложка внутри mp3 (640x640)
COVER_THUMB = HERE / "cover_thumb.jpg"  # превью для Telegram (320x320, до 200 КБ)
CAPTION = "❤️ @IDIEVW"  # подпись под видео, музыкой и сторис
# ID администратора(ов) через запятую. Свой ID можно узнать командой /id
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_ID", "").replace(" ", "").split(",") if x.isdigit()}
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "2"))  # сколько загрузок одновременно
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "3"))  # загрузок в минуту на человека
REF_NEEDED = 3  # сколько друзей нужно пригласить, чтобы не требовалась подписка
TZ_OFFSET = 5 * 3600  # Таджикистан, UTC+5 (для статистики «за сегодня»)
# Прокси для скачивания (нужен, если Instagram блокирует IP сервера).
# Формат: http://логин:пароль@хост:порт или socks5://логин:пароль@хост:порт
PROXY_URL = os.getenv("PROXY_URL")
PROXY_FOR = os.getenv("PROXY_FOR", "instagram").lower()  # instagram — только Instagram, all — всё
DB_PATH = os.getenv("DB_PATH") or str(DATA_DIR / "bot.db")

MAX_SIZE = 50 * 1024 * 1024  # лимит Telegram Bot API на отправку файлов
MAX_PHOTO_SIZE = 10 * 1024 * 1024  # лимит Telegram на фото (больше -> как документ)
QUALITIES = ["360", "480", "720", "1080"]
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MEDIA_EXT = {".mp3", ".mp4", ".mkv", ".webm", ".m4a", ".mov"}

URL_RE = re.compile(
    r"https?://(?:[\w-]+\.)?(?:youtube\.com|youtu\.be|instagram\.com|tiktok\.com)/\S+",
    re.IGNORECASE,
)

LANG_PROMPT = "Забонро интихоб кунед 👇\nВыберите язык 👇"

TEXTS = {
    "ru": {
        "welcome": "Привет! 👋\nОтправь ссылку на видео из YouTube, Instagram или TikTok.\n📸 Сторис Instagram: ссылка на сторис или /stories логин\n🎵 Какая песня? Отправь аудио, голосовое или видео — найду название\n🎧 Видео-файл можно превратить в MP3 или аудио",
        "subscribe": "📢 Чтобы пользоваться ботом, подпишись на каналы ниже и нажми «Проверить».\n\n"
        "Или пригласи друзей: /invite",
        "btn_sub": "📢 Подписаться",
        "btn_check": "✅ Проверить",
        "not_sub": "Ты ещё не подписался ❌",
        "ready": "✅ Готово!\nЧто скачать?",
        "video": "🎬 Видео",
        "mp3": "🎵 MP3",
        "voice": "🎧 Аудио",
        "photo": "🖼 Фото",
        "quality": "Выбери качество:",
        "expired": "Ссылка устарела, отправь её заново",
        "downloading": "⏳ Скачиваю...",
        "processing": "🔄 Обрабатываю...",
        "sending": "📤 Отправляю...",
        "queued": "⏳ Сейчас много загрузок. Ты в очереди (перед тобой: {n}). "
        "Начну, как только освободится место.",
        "compressing": "🗜 Файл больше 50 МБ — сжимаю видео, чтобы его можно было отправить. "
        "Это займёт немного времени...",
        "too_big": "❌ Файл больше 50 МБ — Telegram не позволяет боту его отправить. "
        "Попробуй меньшее качество или MP3.",
        "too_long": "❌ Видео слишком длинное: даже после сжатия оно не поместится в 50 МБ. Попробуй MP3.",
        "busy": "⏳ Дождись окончания предыдущей загрузки.",
        "rate": "🚦 Слишком часто. Подожди {s} сек.",
        "fail": "❌ Не удалось скачать: ",
        "no_photo": "❌ Фото по этой ссылке не найдено",
        "no_audio": "❌ В этом видео нет звука (или Instagram не отдаёт его боту), поэтому MP3 сделать нельзя.",
        "ask_no_audio": "⚠️ У этого видео нет аудио.\nВсё равно скачать без звука?",
        "yes": "✅ Да",
        "no": "❌ Нет",
        "cancelled": "Отменено. Можешь отправить другую ссылку.",
        "other": "Пришли ссылку на YouTube, Instagram или TikTok 🙂",
        "auto_on": "⚡ Авто-режим включён: буду скачивать так же, как в прошлый раз ({fmt}), "
        "без вопросов.\nВыключить: /auto",
        "auto_off": "Авто-режим выключен. Теперь буду спрашивать формат.",
        "auto_need": "Сначала скачай что-нибудь один раз, потом включи /auto.",
        "invite": "👥 Пригласи друзей!\nПриведи {need} друзей: когда они скачают хотя бы по одному "
        "файлу, подписка на канал тебе больше не понадобится.\n\n"
        "Твоя ссылка:\n{link}\n\nПриглашено: {n}/{need}",
        "invite_done": "🎉 Ты уже пригласил достаточно друзей — подписка не нужна!",
        "ref_new": "🎉 Твой друг начал пользоваться ботом! Приглашено: {n}/{need}",
        "ref_unlocked": "🎁 Ты пригласил {need} друзей — теперь подписка на канал не нужна!",
        "ad": "💛 <b>Наш магазин техники KORVONTJ.SHOP</b>\n"
        "<blockquote>Ноутбуки 💻\nПринтери 🖨️\nХолодильник ❄️\nТелефонные аксессуары 🎧</blockquote>",
        "btn_creator": "👉 Посмотреть",
        "stories_usage": "Отправь ссылку на сторис или команду так: /stories логин",
        "no_stories": "❌ Сторис не найдены: их нет, прошло больше 24 часов или аккаунт закрытый.",
        "no_cookies": "❌ Для сторис бот должен быть авторизован в Instagram. Сообщите админу.",
        "ig_denied": "❌ Instagram не пустил бота (cookies устарели или IP заблокирован). Сообщите админу.",
        "rec_start": "🎧 Слушаю...",
        "rec_not_found": "😔 Не удалось найти эту музыку. Отправь фрагмент подлиннее (10–20 секунд) и почище, без шума.",
        "rec_big": "❌ Файл слишком большой для распознавания (до 20 МБ). Отправь короткий фрагмент.",
        "rec_err": "❌ Ошибка распознавания, попробуй позже.",
        "btn_dl_mp3": "⬇️ Скачать MP3",
        "btn_shazam": "🔗 Shazam",
        "send_again": "✅ Готово! Отправь файл ещё раз.",
        "btn_music": "🎵 Поиск музыки",
        "btn_invite": "👥 Пригласить друга",
        "btn_share": "📤 Поделиться ботом",
        "menu_hint": "Выбери действие 👇",
        "share_text": "Скачивай видео, музыку и сторис из YouTube, Instagram и TikTok и находи любую песню — бесплатно!",
        "music_no_app": "🎵 Чтобы найти песню, отправь мне аудио, голосовое или видео с музыкой — я найду название.",
        "conv_ask": "Что сделать с этим файлом?",
        "btn_find": "🔎 Найти песню",
        "btn_to_mp3": "🎵 В MP3 (музыка)",
        "btn_to_voice": "🎧 В аудио (голосовое)",
        "conv_start": "🔄 Конвертирую...",
        "conv_big": "❌ Файл слишком большой (Telegram даёт боту скачать до 20 МБ). Отправь файл поменьше или ссылку.",
        "conv_fail": "❌ Не удалось конвертировать: ",
    },
    "tg": {
        "welcome": "Салом! 👋\nПайванди видеоро аз YouTube, Instagram ё TikTok фиристед.\n📸 Сторисҳои Instagram: пайванд ё /stories ном\n🎵 Кадом суруд? Аудио, овоз ё видео фиристед — ном меёбам\n🎧 Файли видеоро ба MP3 ё аудио табдил додан мумкин",
        "subscribe": "📢 Барои истифодаи бот ба каналҳои зерин обуна шавед ва тугмаи «Санҷиш»-ро пахш кунед.\n\n"
        "Ё дӯстонро даъват кунед: /invite",
        "btn_sub": "📢 Обуна шудан",
        "btn_check": "✅ Санҷиш",
        "not_sub": "Шумо ҳанӯз обуна нашудаед ❌",
        "ready": "✅ Тайёр!\nЧӣ боргирӣ кунем?",
        "video": "🎬 Видео",
        "mp3": "🎵 MP3",
        "voice": "🎧 Аудио",
        "photo": "🖼 Акс",
        "quality": "Сифатро интихоб кунед:",
        "expired": "Пайванд кӯҳна шуд, онро дубора фиристед",
        "downloading": "⏳ Боргирӣ шуда истодааст...",
        "processing": "🔄 Коркард шуда истодааст...",
        "sending": "📤 Фиристода истодаам...",
        "queued": "⏳ Ҳоло боргириҳо бисёранд. Шумо дар навбатед (пеш аз шумо: {n}). "
        "Ҳамин ки ҷой холӣ шавад, оғоз мекунам.",
        "compressing": "🗜 Ҳаҷми файл аз 50 МБ зиёд аст — видеоро кӯчак карда истодаам, "
        "то фиристодан мумкин шавад. Каме вақт мегирад...",
        "too_big": "❌ Ҳаҷми файл аз 50 МБ зиёд аст — Telegram ба бот фиристодани онро иҷозат намедиҳад. "
        "Сифати пасттар ё MP3-ро санҷед.",
        "too_long": "❌ Видео хеле дароз аст: ҳатто пас аз кӯчак кардан ба 50 МБ намеғунҷад. MP3-ро санҷед.",
        "busy": "⏳ Интизор шавед, боргирии қаблӣ ба охир расад.",
        "rate": "🚦 Хеле зуд-зуд. {s} сония интизор шавед.",
        "fail": "❌ Боргирӣ нашуд: ",
        "no_photo": "❌ Аксе бо ин пайванд ёфт нашуд",
        "no_audio": "❌ Дар ин видео садо нест (ё Instagram онро ба бот намедиҳад), барои ҳамин MP3 сохтан мумкин нест.",
        "ask_no_audio": "⚠️ Ин видео аудио надорад.\nБо вуҷуди ин бе садо боргирӣ шавад?",
        "yes": "✅ Ҳа",
        "no": "❌ Не",
        "cancelled": "Бекор карда шуд. Метавонед пайванди дигар фиристед.",
        "other": "Пайванди YouTube, Instagram ё TikTok-ро фиристед 🙂",
        "auto_on": "⚡ Реҷаи худкор фаъол шуд: бе савол мисли дафъаи қаблӣ ({fmt}) боргирӣ мекунам.\n"
        "Хомӯш кардан: /auto",
        "auto_off": "Реҷаи худкор хомӯш шуд. Акнун формат мепурсам.",
        "auto_need": "Аввал як маротиба чизе боргирӣ кунед, баъд /auto-ро фаъол кунед.",
        "invite": "👥 Дӯстонро даъват кунед!\n{need} дӯстро биёред: вақте онҳо ақаллан як файл "
        "боргирӣ кунанд, ба шумо обуна ба канал лозим нест.\n\n"
        "Пайванди шумо:\n{link}\n\nДаъват шуд: {n}/{need}",
        "invite_done": "🎉 Шумо дӯстони кофӣ даъват кардед — обуна лозим нест!",
        "ref_new": "🎉 Дӯсти шумо ба истифодаи бот оғоз кард! Даъват шуд: {n}/{need}",
        "ref_unlocked": "🎁 Шумо {need} дӯстро даъват кардед — акнун обуна ба канал лозим нест!",
        "ad": "💛 <b>Мағозаи техникаи мо KORVONTJ.SHOP</b>\n"
        "<blockquote>Ноутбукҳо 💻\nПринтерҳо 🖨️\nЯхдон ❄️\nАксессуарҳои телефон 🎧</blockquote>",
        "btn_creator": "👉 Дидан",
        "stories_usage": "Пайванди сторис ё фармонро чунин фиристед: /stories ном",
        "no_stories": "❌ Сторис ёфт нашуд: нест, зиёда аз 24 соат гузашт ё ҳисоб пӯшида аст.",
        "no_cookies": "❌ Барои сторис бот бояд ба Instagram ворид шуда бошад. Ба админ хабар диҳед.",
        "ig_denied": "❌ Instagram бот-ро роҳ надод (cookies кӯҳна шудаанд ё IP баста аст). Ба админ хабар диҳед.",
        "rec_start": "🎧 Гӯш карда истодаам...",
        "rec_not_found": "😔 Ин мусиқиро ёфта натавонистам. Порчаи дарозтар (10–20 сония) ва тозаро фиристед.",
        "rec_big": "❌ Ҳаҷми файл барои шинохтан хеле калон аст (то 20 МБ). Порчаи кӯтоҳ фиристед.",
        "rec_err": "❌ Хатогии шинохтан, баъдтар кӯшиш кунед.",
        "btn_dl_mp3": "⬇️ MP3 боргирӣ",
        "btn_shazam": "🔗 Shazam",
        "send_again": "✅ Тайёр! Файлро дубора фиристед.",
        "btn_music": "🎵 Ҷустуҷӯи мусиқӣ",
        "btn_invite": "👥 Даъвати дӯст",
        "btn_share": "📤 Бот-ро мубодила кардан",
        "menu_hint": "Амалро интихоб кунед 👇",
        "share_text": "Видео, мусиқӣ ва сторисҳоро аз YouTube, Instagram ва TikTok боргирӣ кунед ва ҳар сурудро ёбед — ройгон!",
        "music_no_app": "🎵 Барои ёфтани суруд, аудио, овоз ё видеои мусиқидорро фиристед — ном меёбам.",
        "conv_ask": "Бо ин файл чӣ кор кунем?",
        "btn_find": "🔎 Ёфтани суруд",
        "btn_to_mp3": "🎵 Ба MP3 (мусиқӣ)",
        "btn_to_voice": "🎧 Ба аудио (овоз)",
        "conv_start": "🔄 Табдил дода истодаам...",
        "conv_big": "❌ Файл хеле калон аст (Telegram ба бот то 20 МБ боргирӣ карданро иҷозат медиҳад). Файли хурдтар ё пайванд фиристед.",
        "conv_fail": "❌ Табдил нашуд: ",
    },
}

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()

# ====================== база данных (SQLite) ======================
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    lang TEXT,
    joined INTEGER,
    referrer INTEGER,
    ref_credited INTEGER DEFAULT 0,
    ref_count INTEGER DEFAULT 0,
    last_mode TEXT,
    last_quality TEXT,
    auto INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS downloads(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    mode TEXT,
    ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_dl_ts ON downloads(ts);
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER UNIQUE,
    title TEXT,
    url TEXT
);
"""
)
db.commit()

USER_FIELDS = {"lang", "ref_credited", "ref_count", "last_mode", "last_quality", "auto"}


def get_user(uid: int):
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def ensure_user(uid: int, referrer: int | None = None) -> bool:
    """Создаёт пользователя при первой встрече. Возвращает True, если он новый."""
    if get_user(uid):
        return False
    if referrer and (referrer == uid or not get_user(referrer)):
        referrer = None
    db.execute(
        "INSERT INTO users(id, joined, referrer) VALUES(?,?,?)",
        (uid, int(time.time()), referrer),
    )
    db.commit()
    return True


def set_field(uid: int, field: str, value):
    assert field in USER_FIELDS
    db.execute(f"UPDATE users SET {field}=? WHERE id=?", (value, uid))
    db.commit()


def add_download(uid: int, mode: str):
    db.execute("INSERT INTO downloads(user_id, mode, ts) VALUES(?,?,?)", (uid, mode, int(time.time())))
    db.commit()


def list_channels():
    return db.execute("SELECT * FROM channels ORDER BY id").fetchall()


def add_channel(chat_id: int, title: str, url: str) -> bool:
    cur = db.execute(
        "INSERT OR IGNORE INTO channels(chat_id, title, url) VALUES(?,?,?)",
        (chat_id, title, url),
    )
    db.commit()
    return cur.rowcount > 0


def delete_channel(channel_id: int):
    db.execute("DELETE FROM channels WHERE id=?", (channel_id,))
    db.commit()


def parse_channel_input(text: str):
    """Из текста админа достаёт канал (@username / t.me/ссылка / -100ID) и ссылку-приглашение."""
    chat, url = None, None
    for p in text.split():
        if re.fullmatch(r"-100\d+", p):
            chat = int(p)
        elif re.fullmatch(r"@[A-Za-z0-9_]{4,}", p):
            if chat is None:
                chat = p
            if url is None:
                url = f"https://t.me/{p[1:]}"
        elif re.match(r"(?:https?://)?t\.me/(\+|joinchat/)\S+", p):
            url = p if p.startswith("http") else "https://" + p
        else:
            m = re.fullmatch(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})/?", p)
            if m:
                if chat is None:
                    chat = "@" + m.group(1)
                url = f"https://t.me/{m.group(1)}"
    return chat, url


user_lang: dict[int, str] = {}  # кэш языков


def get_lang(uid: int) -> str | None:
    if uid in user_lang:
        return user_lang[uid]
    row = get_user(uid)
    if row and row["lang"]:
        user_lang[uid] = row["lang"]
        return row["lang"]
    return None


def t(uid: int, key: str) -> str:
    return TEXTS[get_lang(uid) or "ru"][key]


def fmt_name(uid: int, mode: str, quality: str | None) -> str:
    if mode == "video":
        return f"{t(uid, 'video')} {quality or ''}p"
    return t(uid, mode)


def day_start() -> int:
    now = int(time.time())
    return (now + TZ_OFFSET) // 86400 * 86400 - TZ_OFFSET


def stats_text() -> str:
    ds = day_start()
    wk = ds - 6 * 86400

    def q(sql, *args):
        return db.execute(sql, args).fetchone()[0]

    by_mode = db.execute(
        "SELECT mode, COUNT(*) c FROM downloads WHERE ts>=? GROUP BY mode", (ds,)
    ).fetchall()
    modes = ", ".join(f"{r['mode']}: {r['c']}" for r in by_mode) or "—"
    return (
        "📊 Статистика\n\n"
        f"👥 Всего пользователей: {q('SELECT COUNT(*) FROM users')}\n"
        f"🆕 Новых сегодня: {q('SELECT COUNT(*) FROM users WHERE joined>=?', ds)}\n"
        f"🔥 Активных сегодня: {q('SELECT COUNT(DISTINCT user_id) FROM downloads WHERE ts>=?', ds)}\n\n"
        f"⬇️ Загрузок сегодня: {q('SELECT COUNT(*) FROM downloads WHERE ts>=?', ds)}\n"
        f"   ({modes})\n"
        f"⬇️ За 7 дней: {q('SELECT COUNT(*) FROM downloads WHERE ts>=?', wk)}\n"
        f"⬇️ Всего: {q('SELECT COUNT(*) FROM downloads')}\n\n"
        f"🔗 Пришли по приглашениям: {q('SELECT COUNT(*) FROM users WHERE referrer IS NOT NULL')}\n"
        f"🎁 Открыли доступ без подписки: {q('SELECT COUNT(*) FROM users WHERE ref_count>=?', REF_NEEDED)}"
    )


# ====================== состояние и лимиты ======================
pending: dict[str, str] = {}  # id -> ссылка (callback_data ограничена 64 байтами)
held: dict[str, dict] = {}  # видео без звука, ждут ответа «да/нет»
bc_pending: dict[str, dict] = {}  # рассылка, ждёт подтверждения
bg_tasks: set[asyncio.Task] = set()
download_sem = asyncio.Semaphore(MAX_PARALLEL)
queue = {"waiting": 0}
active_users: set[int] = set()
recent: dict[int, deque] = defaultdict(deque)
BOT_USERNAME = ""


def limit_problem(uid: int) -> str | None:
    """Возвращает текст проблемы, если человеку пока нельзя качать."""
    if uid in ADMIN_IDS:
        return None
    if uid in active_users:
        return t(uid, "busy")
    now = time.time()
    dq = recent[uid]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= RATE_LIMIT:
        return t(uid, "rate").format(s=int(60 - (now - dq[0])) + 1)
    return None


def proxy_for(url: str) -> str | None:
    if not PROXY_URL:
        return None
    if PROXY_FOR == "all" or "instagram.com" in url.lower():
        return PROXY_URL
    return None


def setup_cookies() -> str | None:
    """Cookies нужны для Instagram и YouTube на серверах. Берём из COOKIES_B64 или COOKIES_FILE."""
    b64 = os.getenv("COOKIES_B64")
    if b64:
        path = "/tmp/cookies.txt"
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return path
    if os.getenv("COOKIES_FILE"):
        return os.getenv("COOKIES_FILE")
    f = DATA_DIR / "cookies.txt"  # можно просто положить файл в /app/data
    return str(f) if f.is_file() else None


COOKIES = setup_cookies()


# ====================== клавиатуры ======================
def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇹🇯 Тоҷикӣ", callback_data="lang:tg"),
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
            ]
        ]
    )


def format_keyboard(uid: int, key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t(uid, "video"), callback_data=f"choose:{key}"),
                InlineKeyboardButton(text=t(uid, "mp3"), callback_data=f"mp3:0:{key}"),
            ],
            [
                InlineKeyboardButton(text=t(uid, "voice"), callback_data=f"voice:0:{key}"),
                InlineKeyboardButton(text=t(uid, "photo"), callback_data=f"photo:0:{key}"),
            ],
        ]
    )


def subscribe_keyboard(uid: int, key: str, channels) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📢 {c['title'][:40]}", url=c["url"])] for c in channels
    ]
    rows.append([InlineKeyboardButton(text=t(uid, "btn_check"), callback_data=f"check:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def yes_no_keyboard(uid: int, token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t(uid, "yes"), callback_data=f"noaudio:yes:{token}"),
                InlineKeyboardButton(text=t(uid, "no"), callback_data=f"noaudio:no:{token}"),
            ]
        ]
    )


def creator_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t(uid, "btn_creator"), url=SHOP_URL)]]
    )


async def send_ad(msg: Message, uid: int):
    """Рекламный блок магазина после отправки файла."""
    try:
        await msg.answer(t(uid, "ad"), reply_markup=creator_keyboard(uid), parse_mode="HTML")
    except Exception:
        logging.exception("Не удалось отправить рекламу")


# ====================== доступ: подписка и приглашения ======================
_warned: dict = {}


async def notify_admins(bot: Bot, text: str, key: str | None = None, every: int = 3600):
    """Пишет админам. С key — не чаще раза в `every` секунд, чтобы не спамить."""
    now = time.time()
    if key:
        if now - _warned.get(key, 0) < every:
            return
        _warned[key] = now
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception:
            pass


async def bot_admin_status(bot: Bot, chat_id) -> tuple[bool, str]:
    """Админ ли бот в канале. Без этого подписку проверить нельзя."""
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
    except Exception as e:
        return False, str(e)[:150]
    if me.status in ("administrator", "creator"):
        return True, ""
    return False, f"статус бота в канале: {me.status}"


async def is_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as e:
        # бота нет в админах канала или канал удалён — людей не блокируем, но админу сообщаем
        logging.exception("Не удалось проверить подписку на %s (бот админ канала?)", chat_id)
        await notify_admins(
            bot,
            f"⚠️ Не могу проверить подписку на канал {chat_id}: {str(e)[:150]}\n\n"
            "Пока это не исправлено, подписка НЕ проверяется и бот открыт всем.\n"
            "Добавьте этого бота в администраторы канала (Канал → Администраторы → Добавить), "
            "затем проверьте: /channels",
            key=f"m{chat_id}",
        )
        return True
    if member.status in ("member", "administrator", "creator"):
        return True
    return member.status == "restricted" and getattr(member, "is_member", False)


async def missing_channels(bot: Bot, uid: int) -> list:
    """Каналы, на которые человек ещё не подписан. Пустой список — доступ открыт."""
    if uid in ADMIN_IDS:
        return []
    row = get_user(uid)
    if row and row["ref_count"] >= REF_NEEDED:
        return []  # пригласил достаточно друзей
    missing = []
    for c in list_channels():
        if not await is_member(bot, c["chat_id"], uid):
            missing.append(c)
    return missing


async def credit_referral(bot: Bot, uid: int):
    """Засчитываем приглашение, когда друг скачал свой первый файл."""
    row = get_user(uid)
    if not row or not row["referrer"] or row["ref_credited"]:
        return
    set_field(uid, "ref_credited", 1)
    ref = row["referrer"]
    ref_row = get_user(ref)
    if not ref_row:
        return
    n = (ref_row["ref_count"] or 0) + 1
    set_field(ref, "ref_count", n)
    key = "ref_unlocked" if n == REF_NEEDED else "ref_new"
    try:
        await bot.send_message(ref, t(ref, key).format(n=min(n, REF_NEEDED), need=REF_NEEDED))
    except Exception:
        pass


# ====================== прогресс ======================
def bar(pct: int) -> str:
    filled = pct // 10
    return "▓" * filled + "░" * (10 - filled)


class ProgressFile(FSInputFile):
    """Файл, который считает, сколько байт уже ушло в Telegram."""

    def __init__(self, path, state: dict, **kwargs):
        super().__init__(path, **kwargs)
        self.state = state

    async def read(self, bot):
        async for chunk in super().read(bot):
            self.state["sent"] += len(chunk)
            yield chunk


async def progress_loop(status: Message, uid: int, state: dict):
    """Раз в 3 секунды обновляет сообщение с процентами."""
    last = ""
    while True:
        await asyncio.sleep(3)
        stage = state["stage"]
        if stage == "queued":
            text = t(uid, "queued").format(n=state["ahead"])
        elif stage == "download":
            pct = state["pct"]
            text = f"{t(uid, 'downloading')}\n{bar(pct)} {pct}%"
        elif stage == "processing":
            text = t(uid, "processing")
        elif stage == "compressing":
            text = t(uid, "compressing")
        else:
            total = state["total"] or 1
            pct = min(99, int(state["sent"] * 100 / total))
            text = f"{t(uid, 'sending')}\n{bar(pct)} {pct}%"
        if text == last:
            continue
        try:
            await status.edit_text(text)
            last = text
        except Exception:
            pass  # слишком частые правки или то же самое сообщение — не страшно


# ====================== ffmpeg: проверка звука и сжатие ======================
async def has_audio(path: Path) -> bool:
    """Есть ли в файле звуковая дорожка (через ffprobe)."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return True  # не смогли проверить — не мешаем отправке
    return bool(out.strip())


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def compress(path: Path, out_dir: str) -> Path:
    """Сжимает видео так, чтобы оно уложилось в лимит Telegram (50 МБ)."""
    dur = probe_duration(path)
    if dur <= 0:
        raise RuntimeError("TOO_LONG")
    audio_bits = 96_000
    video_bits = int(MAX_SIZE * 0.9 * 8 / dur) - audio_bits
    if video_bits < 120_000:  # слишком длинное — картинка превратится в кашу
        raise RuntimeError("TOO_LONG")
    out = Path(out_dir) / "compressed.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", str(path),
        "-c:v", "libx264", "-b:v", str(video_bits),
        "-maxrate", str(video_bits), "-bufsize", str(video_bits * 2),
        "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
    ]
    cmd += ["-pix_fmt", "yuv420p"]
    if video_bits < 700_000:
        cmd += ["-vf", "scale=-2:480"]
    cmd.append(str(out))
    subprocess.run(cmd, check=True, capture_output=True, timeout=1800)
    if out.stat().st_size > MAX_SIZE:
        raise RuntimeError("TOO_LONG")
    return out


# ====================== видео, которое сохраняется в галерею ======================
# Instagram/YouTube часто отдают HEVC, VP9 или AV1, видео без метаданных или с нечётными размерами.
# Такое видео Telegram проигрывает, но «Сохранить в галерею» на телефоне не работает.
# Поэтому каждое видео приводим к H.264 + yuv420p + AAC в mp4 с faststart и передаём
# в Telegram ширину, высоту, длительность и превью.
def _probe_json(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(r.stdout or "{}")
    except ValueError:
        return {}


def _video_meta(path: Path) -> dict:
    """Ширина, высота (с учётом поворота) и длительность готового mp4."""
    info = _probe_json(path)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    rot = 0
    try:
        rot = int(float((v.get("tags") or {}).get("rotate", 0)))
    except (TypeError, ValueError):
        pass
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(float(sd["rotation"]))
            except (TypeError, ValueError):
                pass
    if abs(rot) % 180 == 90:
        w, h = h, w
    try:
        dur = int(float((info.get("format") or {}).get("duration") or 0))
    except (TypeError, ValueError):
        dur = 0
    meta = {}
    if w and h:
        meta["width"], meta["height"] = w, h
    if dur:
        meta["duration"] = dur
    return meta


def make_telegram_video(path: Path, out_dir: str) -> tuple[Path, dict]:
    """Делает из любого видео mp4, которое Telegram сохранит в галерею. Возвращает (файл, параметры отправки)."""
    info = _probe_json(path)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    out = Path(out_dir) / f"tg_{path.stem}.mp4"
    if out == path:
        out = Path(out_dir) / f"tg2_{path.stem}.mp4"

    w, h = int((v or {}).get("width") or 0), int((v or {}).get("height") or 0)
    video_ok = bool(v) and v.get("codec_name") == "h264" and v.get("pix_fmt") == "yuv420p" \
        and w % 2 == 0 and h % 2 == 0
    audio_ok = a is None or a.get("codec_name") == "aac"

    cmd = ["ffmpeg", "-y", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
    if video_ok:
        cmd += ["-c:v", "copy"]  # картинка уже подходит — не пережимаем, быстро и без потери качества
    else:
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        ]
    cmd += ["-c:a", "copy"] if audio_ok else ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-movflags", "+faststart", "-map_metadata", "-1", str(out)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=1800)

    kwargs = _video_meta(out)
    thumb = Path(out_dir) / f"{out.stem}_thumb.jpg"
    try:
        ss = "1" if kwargs.get("duration", 0) > 2 else "0"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", ss, "-i", str(out), "-frames:v", "1",
             "-vf", "scale='min(320,iw)':-2", "-q:v", "5", str(thumb)],
            check=True, capture_output=True, timeout=60,
        )
        if thumb.exists() and thumb.stat().st_size > 0:
            kwargs["thumbnail"] = FSInputFile(thumb)
    except Exception:
        logging.warning("Не удалось сделать превью видео")
    return out, kwargs


# ====================== конвертация видео/аудио в MP3 и голосовое ======================
def convert_audio(src: Path, out_dir: str, kind: str) -> tuple[Path, int]:
    """kind: 'mp3' — музыка (MP3 192k), 'voice' — аудио-сообщение (OGG/Opus). Возвращает (файл, секунд)."""
    if kind == "voice":
        out = Path(out_dir) / "audio.ogg"
        enc = ["-c:a", "libopus", "-b:a", "96k", "-application", "audio", "-ar", "48000"]
    else:
        out = Path(out_dir) / "audio.mp3"
        enc = ["-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100"]
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-vn", "-map", "0:a:0", "-map_metadata", "-1", *enc, str(out)],
        check=True, capture_output=True, timeout=1800,
    )
    if kind != "voice":
        out = brand_mp3(out, out_dir)
    return out, int(probe_duration(out))


def brand_mp3(path: Path, out_dir: str) -> Path:
    """Вшивает в mp3 название бота и обложку (видно и в Telegram, и в любом плеере после скачивания)."""
    out = Path(out_dir) / "Taj_Saver.mp3"
    if not COVER.exists():
        return path
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-i", str(COVER), "-map", "0:a", "-map", "1:v",
             "-c", "copy", "-id3v2_version", "3",
             "-metadata", f"title={BOT_NAME}", "-metadata", f"artist={BOT_NAME}",
             "-metadata:s:v", "title=Cover", "-metadata:s:v", "comment=Cover (front)", str(out)],
            check=True, capture_output=True, timeout=300,
        )
        return out
    except Exception:
        logging.warning("Не удалось вшить обложку в mp3")
        return path


def audio_kwargs() -> dict:
    """Название = имя бота, обложка = наше фото."""
    kw = {"title": BOT_NAME, "performer": f"@{BOT_USERNAME}" if BOT_USERNAME else BOT_NAME}
    if COVER_THUMB.exists():
        kw["thumbnail"] = FSInputFile(COVER_THUMB)
    return kw


async def react_heart(message: Message):
    """Ставит ❤ на сообщение пользователя (нужен aiogram >= 3.13)."""
    try:
        await message.react([ReactionTypeEmoji(emoji="❤")])
    except Exception:
        logging.debug("Не удалось поставить реакцию", exc_info=True)


# ====================== загрузка yt-dlp ======================
def download(url: str, mode: str, quality: str, out_dir: str, state: dict) -> list[Path]:
    def hook(d):
        if d["status"] == "downloading":
            state["stage"] = "download"
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                state["pct"] = min(99, int(d.get("downloaded_bytes", 0) * 100 / total))
        elif d["status"] == "finished":
            state["pct"] = 100
            state["stage"] = "processing"

    opts = {
        "outtmpl": f"{out_dir}/%(title).60s_%(autonumber)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    proxy = proxy_for(url)
    if proxy:
        opts["proxy"] = proxy
    if COOKIES:
        # копия, т.к. yt-dlp может перезаписывать файл, а /app может быть read-only
        tmp_cookies = os.path.join(out_dir, "cookies.txt")
        shutil.copy(COOKIES, tmp_cookies)
        opts["cookiefile"] = tmp_cookies

    if mode in ("mp3", "voice"):  # «аудио» скачиваем как mp3, а потом переделываем в OGG/Opus
        opts.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )
    elif mode == "photo":
        # Фото: берём картинки (обложки/слайды поста) без скачивания видео
        opts.update(
            {
                "skip_download": True,
                "writethumbnail": True,
                "noplaylist": False,
                "playlistend": 10,
                "postprocessors": [
                    {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}
                ],
            }
        )
    else:
        h = quality
        opts.update(
            {
                # ba[ext=m4a] даёт AAC-звук, а [acodec!=none] не даёт выбрать видео без звука
                "format": (
                    f"bv*[height<={h}]+ba[ext=m4a]/bv*[height<={h}]+ba/"
                    f"b[height<={h}][acodec!=none]/b[acodec!=none]/b"
                ),
                # при равном разрешении предпочитаем H.264 — его не надо перекодировать
                "format_sort": ["res", "vcodec:h264", "acodec:m4a"],
                "merge_output_format": "mp4",
                # при склейке перекодируем звук в AAC, его проигрывают все телефоны
                "postprocessor_args": {"merger": ["-c:v", "copy", "-c:a", "aac"]},
            }
        )

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    allowed = IMAGE_EXT if mode == "photo" else MEDIA_EXT
    files = sorted(
        (p for p in Path(out_dir).iterdir() if p.is_file() and p.suffix.lower() in allowed),
        key=lambda p: p.name,
    )
    if not files:
        raise RuntimeError("NO_FILES")
    if mode == "photo":
        return files[:10]
    return [max(files, key=lambda p: p.stat().st_size)]


# ====================== сторис Instagram ======================
STORY_RE = re.compile(r"instagram\.com/stories/", re.IGNORECASE)
IG_APP_ID = "936619743392459"
IG_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _ig_cookie_info() -> tuple[str, str]:
    """Заголовок Cookie для Instagram и csrftoken из cookies.txt (формат Netscape)."""
    if not COOKIES:
        return "", ""
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(COOKIES, ignore_discard=True, ignore_expires=True)
    except Exception:
        return "", ""
    items = {c.name: c.value for c in jar if "instagram.com" in c.domain}
    return "; ".join(f"{k}={v}" for k, v in items.items()), items.get("csrftoken", "")


def _ig_open(url: str, cookie: str = "", csrf: str = "", binary: bool = False):
    headers = {"User-Agent": IG_UA, "Referer": "https://www.instagram.com/"}
    if not binary:
        headers.update(
            {"X-IG-App-ID": IG_APP_ID, "X-ASBD-ID": "198387", "X-IG-WWW-Claim": "0",
             "X-CSRFToken": csrf, "Cookie": cookie, "Origin": "https://www.instagram.com",
             "Accept": "*/*", "X-Requested-With": "XMLHttpRequest"}
        )
    proxy = proxy_for("instagram.com")
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    try:
        return opener.open(urllib.request.Request(url, headers=headers), timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("NO_FILES")
        try:
            body = e.read(300).decode("utf-8", "replace").replace("\n", " ")
        except Exception:
            body = ""
        raise RuntimeError(f"IG_DENIED HTTP {e.code} {body}")


def download_stories(url: str, out_dir: str, state: dict) -> list[Path]:
    """Сначала API Instagram; если отказал — для одной сторис пробуем yt-dlp."""
    try:
        return _download_stories_api(url, out_dir, state)
    except RuntimeError as e:
        if "IG_DENIED" not in str(e):
            raise
        api_err = e
    if re.search(r"/stories/[^/]+/\d+", url) and "/highlights/" not in url:
        try:
            return download(url, "video", "1080", out_dir, state)
        except Exception as e2:
            logging.warning("yt-dlp для сторис тоже не сработал: %s", e2)
    raise api_err


def _download_stories_api(url: str, out_dir: str, state: dict) -> list[Path]:
    """Скачивает сторис (одну, все у пользователя или хайлайт) через API Instagram с cookies."""
    cookie, csrf = _ig_cookie_info()
    if "sessionid" not in cookie:
        raise RuntimeError("NO_COOKIES")
    story_id = None
    m = re.search(r"instagram\.com/stories/highlights/(\d+)", url, re.IGNORECASE)
    if m:
        reel = f"highlight:{m.group(1)}"
    else:
        m = re.search(r"instagram\.com/stories/([A-Za-z0-9_.]+)/?(\d+)?", url, re.IGNORECASE)
        if not m:
            raise RuntimeError("NO_FILES")
        username, story_id = m.group(1), m.group(2)
        info = json.load(
            _ig_open(
                f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}",
                cookie, csrf,
            )
        )
        user = (info.get("data") or {}).get("user")
        if not user:
            raise RuntimeError("NO_FILES")
        reel = user["id"]
    data = json.load(
        _ig_open(f"https://i.instagram.com/api/v1/feed/reels_media/?reel_ids={reel}", cookie, csrf)
    )
    node = (data.get("reels") or {}).get(reel)
    if not node:
        node = next(iter(data.get("reels_media") or []), None)
    items = (node or {}).get("items") or []
    if story_id:
        items = [
            it for it in items
            if str(it.get("pk")) == story_id or str(it.get("id", "")).split("_")[0] == story_id
        ]
    items = items[:30]
    if not items:
        raise RuntimeError("NO_FILES")

    out: list[Path] = []
    for i, it in enumerate(items, 1):
        if it.get("video_versions"):
            src, ext = it["video_versions"][0]["url"], ".mp4"
        else:
            cands = (it.get("image_versions2") or {}).get("candidates") or []
            if not cands:
                continue
            src, ext = cands[0]["url"], ".jpg"
        path = Path(out_dir) / f"story_{i:02d}{ext}"
        with _ig_open(src, binary=True) as r, open(path, "wb") as f:
            shutil.copyfileobj(r, f)
        state["pct"] = min(99, int(i * 100 / len(items)))
        out.append(path)
    if not out:
        raise RuntimeError("NO_FILES")
    return out


def _story_media(p: Path, caption: str | None = None, kw: dict | None = None):
    if p.suffix == ".mp4":
        kw = {k: v for k, v in (kw or {}).items() if k != "thumbnail"}
        return InputMediaVideo(media=FSInputFile(p), supports_streaming=True, caption=caption, **kw)
    return InputMediaPhoto(media=FSInputFile(p), caption=caption)


async def process_story(bot: Bot, base: Message, status: Message, uid: int, url: str):
    state = {"stage": "download", "pct": 0, "sent": 0, "total": 0, "ahead": 0}
    active_users.add(uid)
    recent[uid].append(time.time())
    task = asyncio.create_task(progress_loop(status, uid, state))
    tmp = tempfile.mkdtemp()
    try:
        async with download_sem:
            paths = await asyncio.to_thread(download_stories, url, tmp, state)
        state["stage"] = "processing"
        vmeta: dict[Path, dict] = {}
        fixed = []
        for p in paths:  # видео сторис тоже делаем совместимым с галереей
            if p.suffix == ".mp4":
                try:
                    p, kw = await asyncio.to_thread(make_telegram_video, p, tmp)
                    vmeta[p] = kw
                except Exception:
                    logging.warning("Не удалось перекодировать сторис, шлю как есть")
            fixed.append(p)
        files = [p for p in fixed if p.stat().st_size <= MAX_SIZE]
        for i in range(0, len(files), 10):
            chunk = files[i : i + 10]
            if len(chunk) == 1:
                p = chunk[0]
                if p.suffix == ".mp4":
                    await base.answer_video(
                        FSInputFile(p), supports_streaming=True, caption=CAPTION, **vmeta.get(p, {})
                    )
                else:
                    await base.answer_photo(FSInputFile(p), caption=CAPTION)
            else:
                await base.answer_media_group(
                    [_story_media(p, CAPTION if j == 0 else None, vmeta.get(p)) for j, p in enumerate(chunk)]
                )
        task.cancel()
        await status.delete()
        add_download(uid, "story")
        await credit_referral(bot, uid)
        await send_ad(base, uid)
    except Exception as e:
        task.cancel()
        logging.exception("Ошибка сторис")
        msg = str(e)
        if "NO_COOKIES" in msg or "IG_DENIED" in msg:
            await status.edit_text(t(uid, "no_cookies" if "NO_COOKIES" in msg else "ig_denied"))
            await notify_admins(
                bot,
                "⚠️ Сторис Instagram не скачиваются: " + msg[:300] + "\n"
                "Обновите cookies Instagram (COOKIES_B64) или задайте PROXY_URL.",
                key="ig_denied",
            )
        elif "NO_FILES" in msg:
            await status.edit_text(t(uid, "no_stories"))
        else:
            await status.edit_text(f"{t(uid, 'fail')}{msg[:200]}")
    finally:
        task.cancel()
        active_users.discard(uid)
        shutil.rmtree(tmp, ignore_errors=True)


async def start_story(bot: Bot, message: Message, uid: int, url: str):
    problem = limit_problem(uid)
    if problem:
        await message.answer(problem)
        return
    status = await message.answer(f"{t(uid, 'downloading')}\n{bar(0)} 0%")
    await process_story(bot, message, status, uid, url)


@dp.message(Command("stories"))
async def stories_cmd(message: Message, command: CommandObject, bot: Bot):
    uid = message.from_user.id
    ensure_user(uid)
    if get_lang(uid) is None:
        await message.answer(LANG_PROMPT, reply_markup=lang_keyboard())
        return
    name = (command.args or "").strip().rstrip("/").split("/")[-1].lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", name):
        await message.answer(t(uid, "stories_usage"))
        return
    url = f"https://www.instagram.com/stories/{name}/"
    missing = await missing_channels(bot, uid)
    if missing:
        key = uuid.uuid4().hex[:10]
        pending[key] = url
        await message.answer(t(uid, "subscribe"), reply_markup=subscribe_keyboard(uid, key, missing))
        return
    await start_story(bot, message, uid, url)


# ====================== распознавание музыки (как Shazam) ======================
REC_MAX = 20 * 1024 * 1024  # бот может скачать из Telegram файл не больше 20 МБ
_shazam = None


def _doc_is_media(m: Message) -> bool:
    d = m.document
    return bool(d and d.mime_type and d.mime_type.startswith(("audio/", "video/")))


def _recognize_prepare(src: Path, out_dir: str) -> Path:
    """Вырезает ~25 секунд звука (из середины, если файл длинный) в wav для распознавания."""
    dur = probe_duration(src)
    start = dur / 3 if dur > 40 else 0
    out = Path(out_dir) / "rec.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{start:.1f}", "-i", str(src), "-t", "25",
         "-vn", "-ac", "1", "-ar", "44100", str(out)],
        check=True, capture_output=True, timeout=120,
    )
    return out


async def shazam_lookup(path: Path) -> dict | None:
    global _shazam
    from shazamio import Shazam  # если пакет не установился — ImportError, обработаем выше

    if _shazam is None:
        _shazam = Shazam()
    fn = getattr(_shazam, "recognize", None) or _shazam.recognize_song
    result = await fn(str(path))
    return (result or {}).get("track")


file_jobs: dict[str, dict] = {}  # файлы, ждущие выбора: найти песню / MP3 / аудио


def _file_kb(uid: int, key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(uid, "btn_to_mp3"), callback_data=f"conv:mp3:{key}")],
            [InlineKeyboardButton(text=t(uid, "btn_to_voice"), callback_data=f"conv:voice:{key}")],
            [InlineKeyboardButton(text=t(uid, "btn_find"), callback_data=f"conv:find:{key}")],
        ]
    )


@dp.message(F.audio | F.voice | F.video | F.video_note | F.func(_doc_is_media))
async def offer_convert(message: Message, bot: Bot):
    """Пришёл файл: предлагаем конвертировать в MP3 / аудио или найти песню."""
    uid = message.from_user.id
    ensure_user(uid)
    if get_lang(uid) is None:
        await message.answer(LANG_PROMPT, reply_markup=lang_keyboard())
        return
    missing = await missing_channels(bot, uid)
    if missing:
        key = uuid.uuid4().hex[:10]
        pending[key] = "__resend__"
        await message.answer(t(uid, "subscribe"), reply_markup=subscribe_keyboard(uid, key, missing))
        return
    asyncio.create_task(react_heart(message))
    media = message.audio or message.voice or message.video or message.video_note or message.document
    if (media.file_size or 0) > REC_MAX:
        await message.answer(t(uid, "conv_big"))
        return
    key = uuid.uuid4().hex[:10]
    file_jobs[key] = {"uid": uid, "file_id": media.file_id, "time": time.time()}
    while len(file_jobs) > 300:
        file_jobs.pop(next(iter(file_jobs)))
    await message.answer(t(uid, "conv_ask"), reply_markup=_file_kb(uid, key))


@dp.callback_query(F.data.startswith("conv:"))
async def on_convert(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    _, action, key = call.data.split(":", 2)
    job = file_jobs.get(key)
    if not job or job["uid"] != uid:
        await call.answer(t(uid, "expired"), show_alert=True)
        return
    missing = await missing_channels(bot, uid)
    if missing:
        await call.answer()
        rkey = uuid.uuid4().hex[:10]
        pending[rkey] = "__resend__"
        await edit_or_answer(
            call.message, t(uid, "subscribe"), reply_markup=subscribe_keyboard(uid, rkey, missing)
        )
        return
    problem = limit_problem(uid)
    if problem:
        await call.answer(problem, show_alert=True)
        return
    file_jobs.pop(key, None)
    await call.answer()
    if action == "find":
        status = await edit_or_answer(call.message, t(uid, "rec_start"))
        await run_recognition(call.message, bot, uid, job["file_id"], status)
    else:
        status = await edit_or_answer(call.message, t(uid, "conv_start"))
        await run_convert(call.message, bot, uid, job["file_id"], action, status)


async def run_convert(base: Message, bot: Bot, uid: int, file_id: str, kind: str, status: Message):
    """Скачивает присланный файл, вытаскивает звук и отправляет MP3 или голосовое."""
    active_users.add(uid)
    recent[uid].append(time.time())
    tmp = tempfile.mkdtemp()
    try:
        src = Path(tmp) / "in"
        await bot.download(file_id, destination=src)
        if not await has_audio(src):
            await status.edit_text(t(uid, "no_audio"))
            return
        async with download_sem:
            out, secs = await asyncio.to_thread(convert_audio, src, tmp, kind)
        if out.stat().st_size > MAX_SIZE:
            await status.edit_text(t(uid, "too_big"))
            return
        if kind == "voice":
            await base.answer_voice(FSInputFile(out), caption=CAPTION, duration=secs)
        else:
            await base.answer_audio(
                FSInputFile(out, filename=f"{BOT_NAME}.mp3"), caption=CAPTION, duration=secs, **audio_kwargs()
            )
        await status.delete()
        add_download(uid, "voice" if kind == "voice" else "mp3")
        await credit_referral(bot, uid)
        await send_ad(base, uid)
    except Exception as e:
        logging.exception("Ошибка конвертации")
        await status.edit_text(f"{t(uid, 'conv_fail')}{str(e)[:200]}")
    finally:
        active_users.discard(uid)
        shutil.rmtree(tmp, ignore_errors=True)


async def run_recognition(message: Message, bot: Bot, uid: int, media, status: Message):
    active_users.add(uid)
    recent[uid].append(time.time())
    tmp = tempfile.mkdtemp()
    try:
        src = Path(tmp) / "in"
        await bot.download(media, destination=src)
        async with download_sem:
            wav = await asyncio.to_thread(_recognize_prepare, src, tmp)
        track = await asyncio.wait_for(shazam_lookup(wav), timeout=60)
        if not track:
            await status.edit_text(t(uid, "rec_not_found"))
            return

        title = track.get("title") or "?"
        artist = track.get("subtitle") or ""
        lines = [f"🎵 {title}"]
        if artist:
            lines.append(f"👤 {artist}")
        for sec in track.get("sections") or []:
            if sec.get("type") == "SONG":
                for md in sec.get("metadata") or []:
                    if md.get("title") == "Album" and md.get("text"):
                        lines.append(f"💿 {md['text']}")
        genre = (track.get("genres") or {}).get("primary")
        if genre:
            lines.append(f"🎼 {genre}")
        text = "\n".join(lines)

        key = uuid.uuid4().hex[:10]
        pending[key] = f"ytsearch1:{artist} - {title}".strip()
        rows = [[InlineKeyboardButton(text=t(uid, "btn_dl_mp3"), callback_data=f"mp3:0:{key}")]]
        link = track.get("url") or ""
        if link.startswith("http"):
            rows.append([InlineKeyboardButton(text=t(uid, "btn_shazam"), url=link)])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

        images = track.get("images") or {}
        cover = images.get("coverarthq") or images.get("coverart")
        sent = False
        if cover:
            try:
                await message.answer_photo(cover, caption=text, reply_markup=kb)
                sent = True
            except Exception:
                logging.warning("Не удалось отправить обложку")
        if not sent:
            await message.answer(text, reply_markup=kb)
        await status.delete()
        add_download(uid, "recognize")
        await credit_referral(bot, uid)
    except ImportError:
        logging.exception("shazamio не установлен")
        await status.edit_text(t(uid, "rec_err"))
        await notify_admins(bot, "⚠️ Распознавание музыки не работает: пакет shazamio не установился.", key="shz")
    except Exception:
        logging.exception("Ошибка распознавания")
        await status.edit_text(t(uid, "rec_err"))
    finally:
        active_users.discard(uid)
        shutil.rmtree(tmp, ignore_errors=True)


async def edit_or_answer(msg: Message, text: str, **kw) -> Message:
    """Редактирует сообщение; если это фото (текст править нельзя) — присылает новое."""
    try:
        return await msg.edit_text(text, **kw)
    except Exception:
        return await msg.answer(text, **kw)


# ====================== Mini App: поиск музыки ======================
WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"
web_tracks: dict[str, dict] = {}  # token -> найденный трек (до перезапуска бота)


def _webapp_base() -> str:
    raw = (os.getenv("WEBAPP_URL") or os.getenv("DOMAIN") or "").strip().rstrip("/")
    if not raw:
        return ""
    return raw if raw.startswith("http") else "https://" + raw


WEBAPP_URL = _webapp_base()


def webapp_link(uid: int) -> str:
    return f"{WEBAPP_URL}/?lang={get_lang(uid) or 'ru'}"


def check_init_data(init_data: str) -> int | None:
    """Проверяет подпись Telegram (initData Mini App). Возвращает id пользователя или None."""
    try:
        data = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        got = data.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", (TOKEN or "").encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(data.get("auth_date", "0")) > 86400:
            return None
        return int(json.loads(data["user"])["id"])
    except Exception:
        return None


def track_summary(track: dict) -> dict:
    album = genre = ""
    for sec in track.get("sections") or []:
        if sec.get("type") == "SONG":
            for md in sec.get("metadata") or []:
                if md.get("title") == "Album" and md.get("text"):
                    album = md["text"]
    genre = (track.get("genres") or {}).get("primary") or ""
    images = track.get("images") or {}
    link = track.get("url") or ""
    return {
        "title": track.get("title") or "?",
        "artist": track.get("subtitle") or "",
        "album": album,
        "genre": genre,
        "cover": images.get("coverarthq") or images.get("coverart") or "",
        "url": link if link.startswith("http") else "",
    }


async def web_index(request: web.Request):
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-store"})


async def api_recognize(request: web.Request):
    bot: Bot = request.app["bot"]
    uid = check_init_data(request.headers.get("X-Init-Data", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=401)
    ensure_user(uid)
    if await missing_channels(bot, uid):
        return web.json_response({"ok": False, "error": "subscribe"})
    if limit_problem(uid):
        return web.json_response({"ok": False, "error": "busy"})
    try:
        form = await request.post()
        field = form.get("audio")
        data = field.file.read() if field is not None and hasattr(field, "file") else b""
    except Exception:
        data = b""
    if not data:
        return web.json_response({"ok": False, "error": "fail"})
    if len(data) > REC_MAX:
        return web.json_response({"ok": False, "error": "big"})

    active_users.add(uid)
    recent[uid].append(time.time())
    tmp = tempfile.mkdtemp()
    try:
        src = Path(tmp) / "in"
        src.write_bytes(data)
        async with download_sem:
            wav = await asyncio.to_thread(_recognize_prepare, src, tmp)
        track = await asyncio.wait_for(shazam_lookup(wav), timeout=60)
        if not track:
            return web.json_response({"ok": True, "found": False})
        info = track_summary(track)
        token = uuid.uuid4().hex
        web_tracks[token] = {
            "uid": uid,
            "query": f"ytsearch1:{info['artist']} - {info['title']}".strip(),
            "title": info["title"],
            "artist": info["artist"],
        }
        while len(web_tracks) > 500:
            web_tracks.pop(next(iter(web_tracks)))
        add_download(uid, "recognize")
        return web.json_response({"ok": True, "found": True, "token": token, **info})
    except ImportError:
        logging.exception("shazamio не установлен")
        await notify_admins(bot, "⚠️ Поиск музыки (Mini App): пакет shazamio не установился.", key="shz")
        return web.json_response({"ok": False, "error": "fail"})
    except Exception:
        logging.exception("Ошибка распознавания (Mini App)")
        return web.json_response({"ok": False, "error": "fail"})
    finally:
        active_users.discard(uid)
        shutil.rmtree(tmp, ignore_errors=True)


async def api_download(request: web.Request):
    bot: Bot = request.app["bot"]
    uid = check_init_data(request.headers.get("X-Init-Data", ""))
    if not uid:
        return web.json_response({"ok": False, "error": "auth"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    item = web_tracks.get(str(body.get("token", "")))
    if not item or item["uid"] != uid:
        return web.json_response({"ok": False, "error": "expired"})
    if await missing_channels(bot, uid):
        return web.json_response({"ok": False, "error": "subscribe"})
    if uid in active_users:
        return web.json_response({"ok": False, "error": "busy"})

    active_users.add(uid)
    tmp = tempfile.mkdtemp()
    try:
        state = {"stage": "download", "pct": 0}
        async with download_sem:
            paths = await asyncio.to_thread(download, item["query"], "mp3", "", tmp, state)
        p = paths[0]
        if p.stat().st_size > MAX_SIZE:
            return web.json_response({"ok": False, "error": "big"})
        p = await asyncio.to_thread(brand_mp3, p, tmp)
        sent = await bot.send_audio(
            uid, FSInputFile(p, filename=f"{BOT_NAME}.mp3"),
            caption=f"🎵 {item['artist']} — {item['title']}\n{CAPTION}".replace("🎵  — ", "🎵 "),
            **audio_kwargs(),
        )
        add_download(uid, "mp3")
        await credit_referral(bot, uid)
        await send_ad(sent, uid)
        return web.json_response({"ok": True})
    except Exception:
        logging.exception("Ошибка скачивания (Mini App)")
        return web.json_response({"ok": False, "error": "fail"})
    finally:
        active_users.discard(uid)
        shutil.rmtree(tmp, ignore_errors=True)


async def start_web_server(bot: Bot, bot2: "Bot | None" = None):
    """Сайт Mini App: отдаёт страницу и принимает запись с микрофона. Порт даёт bothost (PORT)."""
    try:
        app = web.Application(client_max_size=24 * 1024 * 1024)
        app["bot"] = bot
        app.router.add_get("/", web_index)
        app.router.add_get("/health", lambda r: web.Response(text="ok"))
        app.router.add_post("/api/recognize", api_recognize)
        app.router.add_post("/api/download", api_download)
        if bot2 is not None:
            ig_register_routes(app)  # вебхук Instagram, вход через Instagram, клики, картинки
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()
        logging.info("Mini App: %s", WEBAPP_URL or "адрес не задан (нужен домен)")
    except Exception:
        logging.exception("Не удалось запустить сервер Mini App")


# ====================== кнопки: поиск музыки и пригласить друга ======================
BTN_MUSIC = {"🎵 Поиск музыки", "🎵 Ҷустуҷӯи мусиқӣ"}
BTN_INVITE = {"👥 Пригласить друга", "👥 Даъвати дӯст"}


def main_keyboard(uid: int) -> ReplyKeyboardMarkup:
    music = t(uid, "btn_music")
    # обычная кнопка: Mini App из reply-клавиатуры не получает initData (ошибка авторизации),
    # поэтому Mini App открываем inline-кнопкой в music_button
    first = KeyboardButton(text=music)
    return ReplyKeyboardMarkup(
        keyboard=[[first], [KeyboardButton(text=t(uid, "btn_invite"))]],
        resize_keyboard=True,
        is_persistent=True,
    )


async def send_invite(message: Message, uid: int):
    row = get_user(uid)
    n = row["ref_count"] or 0
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    text = t(uid, "invite").format(link=link, n=min(n, REF_NEEDED), need=REF_NEEDED)
    if n >= REF_NEEDED:
        text += "\n\n" + t(uid, "invite_done")
    share = (
        "https://t.me/share/url?url=" + urllib.parse.quote(link, safe="")
        + "&text=" + urllib.parse.quote(t(uid, "share_text"), safe="")
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t(uid, "btn_share"), url=share)]]
    )
    await message.answer(text, reply_markup=kb)


@dp.message(F.text.in_(BTN_INVITE))
async def invite_button(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    await send_invite(message, uid)


@dp.message(F.text.in_(BTN_MUSIC))
async def music_button(message: Message):
    """Срабатывает, только если Mini App не настроен (нет домена): объясняем, как искать музыку."""
    uid = message.from_user.id
    ensure_user(uid)
    if not WEBAPP_URL:
        await message.answer(t(uid, "music_no_app"))
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=t(uid, "btn_music"), web_app=WebAppInfo(url=webapp_link(uid)))
        ]]
    )
    await message.answer(t(uid, "btn_music"), reply_markup=kb)


@dp.message(Command("menu"))
async def menu_cmd(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    await message.answer(t(uid, "menu_hint"), reply_markup=main_keyboard(uid))


# ====================== команды: язык, старт, id ======================
@dp.message(CommandStart())
async def start(message: Message, command: CommandObject):
    uid = message.from_user.id
    referrer = None
    if command.args and command.args.startswith("ref_") and command.args[4:].isdigit():
        referrer = int(command.args[4:])
    ensure_user(uid, referrer)
    await message.answer(LANG_PROMPT, reply_markup=lang_keyboard())


@dp.message(Command("lang"))
async def lang_cmd(message: Message):
    ensure_user(message.from_user.id)
    await message.answer(LANG_PROMPT, reply_markup=lang_keyboard())


@dp.message(Command("id"))
async def id_cmd(message: Message):
    await message.answer(f"ID: {message.from_user.id}")


@dp.callback_query(F.data.startswith("lang:"))
async def set_lang(call: CallbackQuery):
    lang = call.data.split(":", 1)[1]
    if lang not in TEXTS:
        await call.answer()
        return
    uid = call.from_user.id
    ensure_user(uid)
    set_field(uid, "lang", lang)
    user_lang[uid] = lang
    await call.answer()
    await call.message.edit_text(TEXTS[lang]["welcome"])
    await call.message.answer(TEXTS[lang]["menu_hint"], reply_markup=main_keyboard(uid))


# ====================== авто-режим и приглашения ======================
@dp.message(Command("auto"))
async def auto_cmd(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    row = get_user(uid)
    if row["auto"]:
        set_field(uid, "auto", 0)
        await message.answer(t(uid, "auto_off"))
        return
    if not row["last_mode"]:
        await message.answer(t(uid, "auto_need"))
        return
    set_field(uid, "auto", 1)
    await message.answer(
        t(uid, "auto_on").format(fmt=fmt_name(uid, row["last_mode"], row["last_quality"]))
    )


@dp.message(Command("invite"))
async def invite_cmd(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    await send_invite(message, uid)


# ====================== админ: /stats и /broadcast ======================
@dp.message(Command("stats"))
async def stats_cmd(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(stats_text())


@dp.message(Command("version"))
async def version_cmd(message: Message):
    """Админ: какая версия кода работает и на какой машине (помогает найти второй экземпляр бота)."""
    if message.from_user.id not in ADMIN_IDS:
        return
    cookie, _ = _ig_cookie_info()
    await message.answer(
        f"Версия: {BOT_VERSION}\n"
        f"Машина: {socket.gethostname()}\n"
        f"yt-dlp: {_installed_ytdlp_version()}\n"
        f"Cookies Instagram: {'есть' if 'sessionid' in cookie else 'НЕТ'}\n"
        f"Прокси: {'есть' if PROXY_URL else 'нет'}\n"
        f"Админов: {len(ADMIN_IDS)}"
    )


@dp.message(Command("update"))
async def update_cmd(message: Message):
    """Админ: обновить yt-dlp и перезапустить бота (без пересборки)."""
    if message.from_user.id not in ADMIN_IDS:
        return
    old = _installed_ytdlp_version()
    msg = await message.answer(f"🔄 Обновляю yt-dlp (сейчас {old})...")
    ok = await asyncio.to_thread(_update_ytdlp)
    new = await asyncio.to_thread(_installed_ytdlp_version)
    if not ok:
        await msg.edit_text(f"❌ Не удалось обновить (версия {new}). Смотрите логи на bothost.")
        return
    await msg.edit_text(f"✅ yt-dlp: {old} → {new}\nПерезапускаю бота...")
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)  # перезапуск процесса на месте


@dp.message(Command("broadcast"))
async def broadcast_cmd(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    if message.reply_to_message:
        payload = {
            "kind": "copy",
            "chat": message.chat.id,
            "msg": message.reply_to_message.message_id,
        }
    elif command.args:
        payload = {"kind": "text", "text": command.args}
    else:
        await message.answer(
            "Как пользоваться:\n"
            "• /broadcast текст — разослать текст\n"
            "• ответьте командой /broadcast на любое сообщение (фото, видео, пост) — "
            "и оно уйдёт всем"
        )
        return
    token = uuid.uuid4().hex[:8]
    bc_pending[token] = payload
    n = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправить", callback_data=f"bc:yes:{token}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"bc:no:{token}"),
            ]
        ]
    )
    await message.answer(f"Отправить это сообщение {n} пользователям?", reply_markup=kb)


async def send_one(bot: Bot, uid: int, payload: dict):
    if payload["kind"] == "copy":
        await bot.copy_message(uid, payload["chat"], payload["msg"])
    else:
        await bot.send_message(uid, payload["text"])


async def run_broadcast(bot: Bot, payload: dict, report: Message):
    ids = [r[0] for r in db.execute("SELECT id FROM users").fetchall()]
    ok = blocked = failed = 0
    for uid in ids:
        try:
            await send_one(bot, uid, payload)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await send_one(bot, uid, payload)
                ok += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            blocked += 1  # пользователь заблокировал бота
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # ~20 сообщений в секунду, в пределах лимитов Telegram
    await report.answer(
        f"✅ Рассылка завершена\nДоставлено: {ok}\nЗаблокировали бота: {blocked}\nОшибок: {failed}"
    )


@dp.callback_query(F.data.startswith("bc:"))
async def broadcast_confirm(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer()
        return
    _, answer, token = call.data.split(":", 2)
    payload = bc_pending.pop(token, None)
    await call.answer()
    if not payload or answer != "yes":
        await call.message.edit_text("Отменено.")
        return
    await call.message.edit_text("📣 Рассылка началась...")
    task = asyncio.create_task(run_broadcast(bot, payload, call.message))
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)


# ====================== админ: каналы для подписки ======================
awaiting_channel: set[int] = set()


async def channels_view(bot: Bot):
    chans = list_channels()
    if chans:
        lines = []
        for i, c in enumerate(chans, 1):
            ok, err = await bot_admin_status(bot, c["chat_id"])
            lines.append(f"{i}. {c['title']} — " + ("✅ бот админ" if ok else f"⚠️ бот НЕ админ ({err})"))
        text = "📢 Каналы для обязательной подписки:\n\n" + "\n".join(lines)
        if any("НЕ админ" in l for l in lines):
            text += "\n\n⚠️ Добавьте бота в администраторы канала, иначе подписка не проверяется."
    else:
        text = "Каналов нет — подписка сейчас не требуется."
    rows = [
        [InlineKeyboardButton(text=f"🗑 Удалить: {c['title'][:30]}", callback_data=f"chdel:{c['id']}")]
        for c in chans
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="chadd")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("channels"))
async def channels_cmd(message: Message, bot: Bot):
    if message.from_user.id not in ADMIN_IDS:
        return
    text, kb = await channels_view(bot)
    await message.answer(text, reply_markup=kb)


@dp.message(Command("cancel"))
async def cancel_cmd(message: Message):
    if message.from_user.id in awaiting_channel:
        awaiting_channel.discard(message.from_user.id)
        await message.answer("Отменено.")


@dp.callback_query(F.data == "chadd")
async def channel_add_start(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer()
        return
    awaiting_channel.add(call.from_user.id)
    await call.answer()
    await call.message.answer(
        "Отправь канал одним из способов:\n"
        "• @username или ссылку https://t.me/username\n"
        "• приватный канал: ID и ссылка-приглашение через пробел, например:\n"
        "-1001234567890 https://t.me/+abcdef\n\n"
        "⚠️ Бот должен быть администратором этого канала.\n"
        "Отмена: /cancel"
    )


@dp.callback_query(F.data.startswith("chdel:"))
async def channel_delete(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer()
        return
    delete_channel(int(call.data.split(":", 1)[1]))
    await call.answer("Удалено")
    text, kb = await channels_view(bot)
    await call.message.edit_text(text, reply_markup=kb)


@dp.message(F.text, lambda m: m.from_user.id in awaiting_channel and not m.text.startswith("/"))
async def channel_input(message: Message, bot: Bot):
    chat, url = parse_channel_input(message.text)
    if chat is None:
        await message.answer("Не понял. Отправь @username или ссылку на канал (или /cancel).")
        return
    try:
        info = await bot.get_chat(chat)
    except Exception:
        await message.answer("❌ Не нашёл такой канал. Проверь ссылку и попробуй снова (или /cancel).")
        return
    try:
        me = await bot.get_chat_member(info.id, bot.id)
        is_admin = me.status in ("administrator", "creator")
    except Exception:
        is_admin = False
    if not is_admin:
        await message.answer(
            "❌ Бот не администратор этого канала, поэтому не сможет проверять подписку.\n"
            "Добавьте бота в администраторы и отправьте канал ещё раз (или /cancel)."
        )
        return
    if not url:
        if info.username:
            url = f"https://t.me/{info.username}"
        else:
            await message.answer(
                "Это приватный канал: добавьте ссылку-приглашение через пробел, например:\n"
                f"{info.id} https://t.me/+abcdef"
            )
            return
    awaiting_channel.discard(message.from_user.id)
    if add_channel(info.id, info.title or str(info.id), url):
        await message.answer(f"✅ Канал добавлен: {info.title}")
    else:
        await message.answer("Этот канал уже есть в списке.")
    text, kb = await channels_view(bot)
    await message.answer(text, reply_markup=kb)


# ====================== ссылка, подписка, формат ======================
@dp.message(F.text.regexp(URL_RE))
async def got_link(message: Message, bot: Bot):
    uid = message.from_user.id
    ensure_user(uid)
    if get_lang(uid) is None:
        await message.answer(LANG_PROMPT, reply_markup=lang_keyboard())
        return

    url = URL_RE.search(message.text).group(0)
    asyncio.create_task(react_heart(message))

    missing = await missing_channels(bot, uid)
    if missing:
        key = uuid.uuid4().hex[:10]
        pending[key] = url
        await message.answer(
            t(uid, "subscribe"), reply_markup=subscribe_keyboard(uid, key, missing)
        )
        return

    if STORY_RE.search(url):  # сторис качаем сразу, без вопроса о формате
        await start_story(bot, message, uid, url)
        return

    row = get_user(uid)
    if row["auto"] and row["last_mode"]:
        # авто-режим: качаем сразу в прошлом формате
        problem = limit_problem(uid)
        if problem:
            await message.answer(problem)
            return
        status = await message.answer(f"{t(uid, 'downloading')}\n{bar(0)} 0%")
        await process_download(
            bot, message, status, uid, url, row["last_mode"], row["last_quality"] or "720"
        )
        return

    key = uuid.uuid4().hex[:10]
    pending[key] = url
    await message.answer(t(uid, "ready"), reply_markup=format_keyboard(uid, key))


@dp.callback_query(F.data.startswith("check:"))
async def check_sub(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    key = call.data.split(":", 1)[1]
    if key not in pending:
        await call.answer(t(uid, "expired"), show_alert=True)
        return
    missing = await missing_channels(bot, uid)
    if missing:
        await call.answer(t(uid, "not_sub"), show_alert=True)
        try:  # оставляем кнопки только для каналов, на которые ещё не подписался
            await call.message.edit_reply_markup(reply_markup=subscribe_keyboard(uid, key, missing))
        except Exception:
            pass
        return
    await call.answer()
    if pending[key] == "__resend__":
        pending.pop(key, None)
        await call.message.edit_text(t(uid, "send_again"))
        return
    if STORY_RE.search(pending[key]):
        url = pending.pop(key)
        problem = limit_problem(uid)
        if problem:
            await call.message.edit_text(problem)
            return
        status = await call.message.edit_text(f"{t(uid, 'downloading')}\n{bar(0)} 0%")
        await process_story(bot, call.message, status, uid, url)
        return
    await call.message.edit_text(t(uid, "ready"), reply_markup=format_keyboard(uid, key))


@dp.callback_query(F.data.startswith("choose:"))
async def choose_quality(call: CallbackQuery):
    uid = call.from_user.id
    key = call.data.split(":", 1)[1]
    if key not in pending:
        await call.answer(t(uid, "expired"), show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"{q}p", callback_data=f"video:{q}:{key}")
                for q in QUALITIES
            ]
        ]
    )
    await call.message.edit_text(t(uid, "quality"), reply_markup=kb)
    await call.answer()


# ====================== загрузка и отправка ======================
async def send_photos(message: Message, paths: list[Path]):
    if len(paths) == 1:
        p = paths[0]
        if p.stat().st_size <= MAX_PHOTO_SIZE:
            await message.answer_photo(FSInputFile(p))
        else:
            await message.answer_document(FSInputFile(p))
        return
    media = []
    for p in paths:
        if p.stat().st_size <= MAX_PHOTO_SIZE:
            media.append(InputMediaPhoto(media=FSInputFile(p)))
        else:
            await message.answer_document(FSInputFile(p))
    if len(media) == 1:
        await message.answer_photo(media[0].media)
    elif media:
        await message.answer_media_group(media)


async def process_download(
    bot: Bot, base: Message, status: Message, uid: int, url: str, mode: str, quality: str
):
    """Вся загрузка: очередь -> скачивание -> (сжатие) -> проверка звука -> отправка."""
    state = {"stage": "download", "pct": 0, "sent": 0, "total": 0, "ahead": 0}
    active_users.add(uid)
    recent[uid].append(time.time())
    set_field(uid, "last_mode", mode)
    set_field(uid, "last_quality", quality)
    task = asyncio.create_task(progress_loop(status, uid, state))
    tmp = tempfile.mkdtemp()
    keep_tmp = False
    try:
        # очередь: одновременно работают только MAX_PARALLEL загрузок
        queued = download_sem.locked()
        if queued:
            state["stage"] = "queued"
            state["ahead"] = MAX_PARALLEL + queue["waiting"]
            queue["waiting"] += 1
        try:
            await download_sem.acquire()
        finally:
            if queued:
                queue["waiting"] -= 1
        try:
            state["stage"] = "download"
            dl_mode = "mp3" if mode == "voice" else mode
            paths = await asyncio.to_thread(download, url, dl_mode, quality, tmp, state)
            vkw: dict = {}
            if mode == "video":
                state["stage"] = "processing"
                # приводим к mp4 H.264/AAC, чтобы работало «Сохранить в галерею»
                video_path, vkw = await asyncio.to_thread(make_telegram_video, paths[0], tmp)
                paths = [video_path]
                if paths[0].stat().st_size > MAX_SIZE:
                    state["stage"] = "compressing"
                    paths = [await asyncio.to_thread(compress, paths[0], tmp)]
                    vkw = await asyncio.to_thread(_video_meta, paths[0])
            elif mode == "mp3":
                paths = [await asyncio.to_thread(brand_mp3, paths[0], tmp)]
            elif mode == "voice":
                state["stage"] = "processing"
                voice_path, secs = await asyncio.to_thread(convert_audio, paths[0], tmp, "voice")
                paths, vkw = [voice_path], {"duration": secs}
        finally:
            download_sem.release()

        if mode in ("mp3", "voice") and paths[0].stat().st_size > MAX_SIZE:
            task.cancel()
            await status.edit_text(t(uid, "too_big"))
            return

        if mode == "video" and not await has_audio(paths[0]):
            task.cancel()
            token = uuid.uuid4().hex[:10]
            held[token] = {"path": paths[0], "tmp": tmp, "time": time.time(), "kw": vkw}
            keep_tmp = True
            await status.edit_text(
                t(uid, "ask_no_audio"), reply_markup=yes_no_keyboard(uid, token)
            )
            return

        state["stage"] = "sending"
        if mode == "photo":
            await status.edit_text(t(uid, "sending"))
            await send_photos(base, paths)
        else:
            state["total"] = paths[0].stat().st_size
            file = ProgressFile(paths[0], state, **({"filename": f"{BOT_NAME}.mp3"} if mode == "mp3" else {}))
            if mode == "mp3":
                await base.answer_audio(file, caption=CAPTION, **audio_kwargs())
            elif mode == "voice":
                await base.answer_voice(file, caption=CAPTION, **vkw)
            else:
                await base.answer_video(file, supports_streaming=True, caption=CAPTION, **vkw)

        task.cancel()
        await status.delete()
        add_download(uid, mode)
        await credit_referral(bot, uid)
        # реклама создателя после отправки файла
        await send_ad(base, uid)
    except Exception as e:
        task.cancel()
        logging.exception("Ошибка загрузки")
        msg = str(e)
        if "NO_FILES" in msg and mode == "photo":
            await status.edit_text(t(uid, "no_photo"))
        elif "TOO_LONG" in msg:
            await status.edit_text(t(uid, "too_long"))
        elif mode in ("mp3", "voice") and ("audio codec" in msg or "does not contain any stream" in msg or "Stream map" in msg):
            await status.edit_text(t(uid, "no_audio"))
        else:
            await status.edit_text(f"{t(uid, 'fail')}{msg[:200]}")
    finally:
        task.cancel()
        active_users.discard(uid)
        if not keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)


@dp.callback_query(F.data.regexp(r"^(video|mp3|voice|photo):\d+:"))
async def on_download(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    mode, quality, key = call.data.split(":", 2)

    # повторная проверка, чтобы нельзя было обойти подписку старыми кнопками
    missing = await missing_channels(bot, uid)
    if missing:
        await call.answer()
        await edit_or_answer(
            call.message, t(uid, "subscribe"), reply_markup=subscribe_keyboard(uid, key, missing)
        )
        return

    problem = limit_problem(uid)
    if problem:
        await call.answer(problem, show_alert=True)
        return

    url = pending.pop(key, None)
    if not url:
        await call.answer(t(uid, "expired"), show_alert=True)
        return

    await call.answer()
    status = await edit_or_answer(call.message, f"{t(uid, 'downloading')}\n{bar(0)} 0%")
    await process_download(bot, call.message, status, uid, url, mode, quality)


@dp.callback_query(F.data.startswith("noaudio:"))
async def on_no_audio(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    _, answer, token = call.data.split(":", 2)
    item = held.pop(token, None)
    if not item:
        await call.answer(t(uid, "expired"), show_alert=True)
        return
    await call.answer()

    if answer != "yes":
        shutil.rmtree(item["tmp"], ignore_errors=True)
        await call.message.edit_text(t(uid, "cancelled"))
        return

    path: Path = item["path"]
    state = {"stage": "sending", "pct": 0, "sent": 0, "total": path.stat().st_size}
    status = await call.message.edit_text(f"{t(uid, 'sending')}\n{bar(0)} 0%")
    task = asyncio.create_task(progress_loop(status, uid, state))
    try:
        await call.message.answer_video(
            ProgressFile(path, state), supports_streaming=True, caption=CAPTION, **item.get("kw", {})
        )
        task.cancel()
        await status.delete()
        add_download(uid, "video")
        await credit_referral(bot, uid)
        await send_ad(call.message, uid)
    except Exception as e:
        task.cancel()
        logging.exception("Ошибка отправки")
        await status.edit_text(f"{t(uid, 'fail')}{str(e)[:200]}")
    finally:
        task.cancel()
        shutil.rmtree(item["tmp"], ignore_errors=True)


@dp.message()
async def other(message: Message):
    await message.answer(t(message.from_user.id, "other"))


# =====================================================================================
#  БОТ №2 — Instagram-автоматизация (как ManyChat)
#  Работает в этом же файле. Включается, если задана переменная BOT2_TOKEN.
#  • комментарий под постом → ответ в комментарии + личное сообщение с кнопками
#  • автоответчик в Direct (приветствие, ключевые слова, ответ по умолчанию)
#  • «сначала подпишись» (проверка подписки) • клики по ссылкам • статистика
#  • динамика подписчиков за 30 дней, кто подписался / отписался среди контактов
# =====================================================================================
import random  # noqa: E402

TOKEN2 = os.getenv("BOT2_TOKEN")
IG_APP_ID = (os.getenv("IG_APP_ID") or "").strip()
IG_APP_SECRET = (os.getenv("IG_APP_SECRET") or "").strip()
IG_VERIFY = os.getenv("IG_VERIFY_TOKEN") or hashlib.sha256(("ig" + (TOKEN2 or "")).encode()).hexdigest()[:16]
IG_API = "https://graph.instagram.com/v21.0"
IG_SCOPES = "instagram_business_basic,instagram_business_manage_messages,instagram_business_manage_comments"
IG_AUTO_COOLDOWN = 12 * 3600  # ответ по умолчанию — не чаще раза в 12 часов на человека

# ---------- тарифы и оплата ----------
FIREBASE_URL = (os.getenv("FIREBASE_DB_URL") or "").strip().rstrip("/")  # https://проект-default-rtdb.firebaseio.com
FIREBASE_AUTH = (os.getenv("FIREBASE_AUTH") or "").strip()  # старый способ: «секрет базы данных» (необязательно)


def _load_fb_key():
    """JSON-ключ сервисного аккаунта Firebase: FIREBASE_KEY_B64 / FIREBASE_KEY / файл /app/data/firebase.json."""
    try:
        if os.getenv("FIREBASE_KEY_B64"):
            return json.loads(base64.b64decode(os.environ["FIREBASE_KEY_B64"]))
        if os.getenv("FIREBASE_KEY"):
            return json.loads(os.environ["FIREBASE_KEY"])
        f = DATA_DIR / "firebase.json"
        if f.is_file():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        logging.warning("Не удалось прочитать ключ Firebase (JSON повреждён?)")
    return None


FIREBASE_KEY = _load_fb_key()
SUPPORT_USER = "@IDIEVW"
SUPPORT_URL = "https://t.me/IDIEVW"
PAY_DC = os.getenv("PAY_DC", "978117811")  # DC City
PAY_ALIF = os.getenv("PAY_ALIF", "978117811")  # Alif Mobi
FREE_DAYS = 3  # Free: сколько дней работает ответ под одним постом
PRO_MAX_ACCOUNTS = 5  # Business Pro: сколько Instagram-аккаунтов можно подключить
PRO_INTRO_ON = os.getenv("PRO_INTRO", "1") == "1"  # акция для первых пользователей (PRO_INTRO=0 — выключить)
PLANS = {
    "intro": {"title": "Business Pro", "days": 5, "label": "5 дней", "price": int(os.getenv("PRICE_INTRO", "15"))},
    "month": {"title": "Business Pro", "days": 30, "label": "1 месяц", "price": int(os.getenv("PRICE_MONTH", "30"))},
}

dp2 = Dispatcher()
BOT2: "Bot | None" = None
ig_wait: dict[int, dict] = {}  # что бот ждёт от человека (токен, ключевое слово, текст…)

db.executescript(
    """
CREATE TABLE IF NOT EXISTS ig_accounts(
    ig_id TEXT PRIMARY KEY, alt_id TEXT, owner INTEGER, username TEXT, token TEXT, token_at INTEGER,
    welcome TEXT, welcome_on INTEGER DEFAULT 0, dflt TEXT, dflt_on INTEGER DEFAULT 0, created INTEGER
);
CREATE TABLE IF NOT EXISTS ig_rules(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ig_id TEXT, kind TEXT, keyword TEXT, media_id TEXT DEFAULT '',
    message TEXT, buttons TEXT DEFAULT '[]', public_reply TEXT DEFAULT '', need_follow INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1, sent INTEGER DEFAULT 0, created INTEGER, catalog INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ig_contacts(
    ig_id TEXT, user_id TEXT, username TEXT, first_seen INTEGER, last_seen INTEGER,
    last_auto INTEGER DEFAULT 0, follows INTEGER DEFAULT -1, follow_checked INTEGER DEFAULT 0,
    PRIMARY KEY(ig_id, user_id)
);
CREATE TABLE IF NOT EXISTS ig_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ig_id TEXT, user_id TEXT, username TEXT, kind TEXT, ts INTEGER
);
CREATE TABLE IF NOT EXISTS ig_followers(ig_id TEXT, day TEXT, cnt INTEGER, PRIMARY KEY(ig_id, day));
CREATE TABLE IF NOT EXISTS ig_links(
    id TEXT PRIMARY KEY, url TEXT, ig_id TEXT, rule_id INTEGER, clicks INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ig_products(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ig_id TEXT, title TEXT, subtitle TEXT, img TEXT DEFAULT '',
    url TEXT, btn TEXT DEFAULT 'Купить', link_id TEXT, created INTEGER
);
CREATE TABLE IF NOT EXISTS ig_done(key TEXT PRIMARY KEY, ts INTEGER);
CREATE TABLE IF NOT EXISTS ig_log(ig_id TEXT, ts INTEGER, kind TEXT);
CREATE TABLE IF NOT EXISTS ig_sel(owner INTEGER PRIMARY KEY, ig_id TEXT);
CREATE TABLE IF NOT EXISTS kv(path TEXT PRIMARY KEY, val TEXT);
"""
)
db.commit()
try:  # на случай, если таблица создана старой версией
    db.execute("ALTER TABLE ig_rules ADD COLUMN catalog INTEGER DEFAULT 0")
    db.commit()
except sqlite3.OperationalError:
    pass
IG_IMG_DIR = DATA_DIR / "ig_img"  # фото товаров (отдаются по /img/…)


# ---------- хранилище клиентов: Firebase (запасной вариант — локальная база) ----------
class PlanLimit(Exception):
    """Действие недоступно на текущем тарифе."""


def bg(coro) -> None:
    task = asyncio.create_task(coro)
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)


def _kv_get(path: str):
    row = db.execute("SELECT val FROM kv WHERE path=?", (path,)).fetchone()
    try:
        return json.loads(row[0]) if row else None
    except ValueError:
        return None


def _kv_set(path: str, value) -> None:
    db.execute("INSERT OR REPLACE INTO kv(path, val) VALUES(?,?)", (path, json.dumps(value, ensure_ascii=False)))
    db.commit()


_fb_tok = {"v": "", "exp": 0.0}


def _fb_token() -> str:
    """Токен доступа из JSON-ключа сервисного аккаунта (нужен пакет google-auth). Обновляется раз в ~50 минут."""
    if time.time() < _fb_tok["exp"]:
        return _fb_tok["v"]
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as GoogleRequest

    creds = service_account.Credentials.from_service_account_info(
        FIREBASE_KEY,
        scopes=["https://www.googleapis.com/auth/firebase.database",
                "https://www.googleapis.com/auth/userinfo.email"],
    )
    creds.refresh(GoogleRequest())
    _fb_tok["v"], _fb_tok["exp"] = creds.token, time.time() + 3000
    return creds.token


def _fb_sync(method: str, path: str, body=None):
    """Запрос к Firebase Realtime Database через REST."""
    url = f"{FIREBASE_URL}/{path}.json"
    if FIREBASE_KEY:
        url += "?access_token=" + urllib.parse.quote(_fb_token(), safe="")
    elif FIREBASE_AUTH:
        url += "?auth=" + urllib.parse.quote(FIREBASE_AUTH, safe="")
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Firebase HTTP {e.code}: {e.read(200).decode('utf-8', 'ignore')}")
    return json.loads(raw.decode("utf-8") or "null")


async def fb_alarm(err: str):
    if BOT2:
        await notify_admins(BOT2, "⚠️ Firebase недоступен, данные пока пишутся локально:\n" + err[:200],
                            key="fb", every=3600)


async def store_get(path: str):
    if FIREBASE_URL:
        try:
            return await asyncio.to_thread(_fb_sync, "GET", path)
        except Exception as e:
            logging.warning("Firebase GET %s: %s", path, e)
            await fb_alarm(str(e))
    return _kv_get(path)


async def store_set(path: str, value) -> None:
    _kv_set(path, value)  # локальная копия на случай сбоя Firebase
    if FIREBASE_URL:
        try:
            await asyncio.to_thread(_fb_sync, "PUT", path, value)
        except Exception as e:
            logging.warning("Firebase PUT %s: %s", path, e)
            await fb_alarm(str(e))


async def store_update(path: str, fields: dict) -> None:
    cur = _kv_get(path)
    cur = dict(cur) if isinstance(cur, dict) else {}
    cur.update(fields)
    _kv_set(path, cur)
    if FIREBASE_URL:
        try:
            await asyncio.to_thread(_fb_sync, "PATCH", path, fields)
        except Exception as e:
            logging.warning("Firebase PATCH %s: %s", path, e)
            await fb_alarm(str(e))


async def store_all(path: str) -> dict:
    if FIREBASE_URL:
        try:
            return await asyncio.to_thread(_fb_sync, "GET", path) or {}
        except Exception as e:
            logging.warning("Firebase GET %s: %s", path, e)
    out = {}
    for r in db.execute("SELECT path, val FROM kv WHERE path LIKE ?", (path + "/%",)).fetchall():
        try:
            out[r["path"].split("/", 1)[1]] = json.loads(r["val"])
        except ValueError:
            pass
    return out


SUB_TTL = 30
sub_cache: dict[int, tuple[float, dict]] = {}


async def get_sub(uid: int, fresh: bool = False) -> dict:
    """Подписка пользователя: {plan, until, free_started, intro_used, pending…}. Кэш 30 секунд."""
    hit = sub_cache.get(uid)
    if hit and not fresh and time.time() - hit[0] < SUB_TTL:
        return hit[1]
    data = await store_get(f"subs/{uid}")
    if not isinstance(data, dict):
        data = {}
    sub_cache[uid] = (time.time(), data)
    return data


def sub_cached(uid: int) -> dict:
    hit = sub_cache.get(uid)
    return hit[1] if hit else {}


async def sub_update(uid: int, **fields) -> dict:
    await store_update(f"subs/{uid}", fields)
    return await get_sub(uid, fresh=True)


async def client_update(uid: int, **fields) -> None:
    fields["updated"] = int(time.time())
    if not _kv_get(f"clients/{uid}"):
        fields["joined"] = fields["updated"]
    await store_update(f"clients/{uid}", fields)


async def client_touch(user) -> None:
    await client_update(user.id, name=user.full_name, username=user.username or "")


def tier_of(sub: dict) -> str:
    """pro — Business Pro оплачен; free — бесплатный; expired — бесплатные 3 дня закончились."""
    now = time.time()
    if sub.get("plan") == "pro" and (sub.get("until") or 0) > now:
        return "pro"
    fs = sub.get("free_started")
    if fs and now > fs + FREE_DAYS * 86400:
        return "expired"
    return "free"


async def acc_tier(acc) -> str:
    return tier_of(await get_sub(acc["owner"]))


def intro_available(sub: dict) -> bool:
    return PRO_INTRO_ON and not sub.get("intro_used")


def fdate(ts) -> str:
    return time.strftime("%d.%m.%Y", time.gmtime(int(ts) + TZ_OFFSET))


MSG_FREE_ENDED = ("⏸ Бесплатный период (3 дня) закончился — ответы в комментариях остановлены.\n"
                  "Подключите Business Pro: /tariff")


def plan_line(sub: dict) -> str:
    t = tier_of(sub)
    if t == "pro":
        return f"💎 Тариф: Business Pro до {fdate(sub['until'])}"
    if t == "expired":
        return f"⏸ Тариф Free закончился ({FREE_DAYS} дня). Подключите Business Pro: /tariff"
    fs = sub.get("free_started")
    if fs:
        return f"🆓 Тариф: Free — работает до {fdate(fs + FREE_DAYS * 86400)}"
    return f"🆓 Тариф: Free (1 аккаунт, ответ в комментариях, 1 пост, {FREE_DAYS} дня). Больше: /tariff"


# ---------- мелкие помощники ----------
def dbx(sql: str, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def ig_today(offset_days: int = 0) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + TZ_OFFSET - offset_days * 86400))


def ig_acc(any_id) -> "sqlite3.Row | None":
    return db.execute(
        "SELECT * FROM ig_accounts WHERE ig_id=? OR alt_id=?", (str(any_id), str(any_id))
    ).fetchone()


def ig_accs_of(owner: int) -> list:
    return db.execute("SELECT * FROM ig_accounts WHERE owner=? ORDER BY created", (owner,)).fetchall()


def ig_acc_of(owner: int):
    """Выбранный аккаунт владельца (если не выбран — последний подключённый)."""
    sel = db.execute("SELECT ig_id FROM ig_sel WHERE owner=?", (owner,)).fetchone()
    if sel:
        row = db.execute("SELECT * FROM ig_accounts WHERE ig_id=? AND owner=?", (sel[0], owner)).fetchone()
        if row:
            return row
    return db.execute(
        "SELECT * FROM ig_accounts WHERE owner=? ORDER BY created DESC LIMIT 1", (owner,)
    ).fetchone()


def ig_seen(key: str) -> bool:
    """True, если событие новое (защита от повторной доставки вебхука)."""
    return dbx("INSERT OR IGNORE INTO ig_done(key, ts) VALUES(?,?)", (key, int(time.time()))).rowcount == 1


def ig_log(ig_id: str, kind: str):
    dbx("INSERT INTO ig_log(ig_id, ts, kind) VALUES(?,?,?)", (ig_id, int(time.time()), kind))


def ig_touch(ig_id: str, uid: str, username: str | None = None) -> bool:
    """Запоминает контакт. True — если человек новый."""
    now = int(time.time())
    row = db.execute("SELECT 1 FROM ig_contacts WHERE ig_id=? AND user_id=?", (ig_id, uid)).fetchone()
    if row:
        if username:
            dbx("UPDATE ig_contacts SET last_seen=?, username=? WHERE ig_id=? AND user_id=?", (now, username, ig_id, uid))
        else:
            dbx("UPDATE ig_contacts SET last_seen=? WHERE ig_id=? AND user_id=?", (now, ig_id, uid))
        return False
    dbx(
        "INSERT INTO ig_contacts(ig_id, user_id, username, first_seen, last_seen) VALUES(?,?,?,?,?)",
        (ig_id, uid, username or "", now, now),
    )
    return True


def ig_match(rule, text: str) -> bool:
    kws = [k.strip().casefold() for k in re.split(r"[,;\n]", rule["keyword"] or "") if k.strip()]
    if not kws or "*" in kws:
        return True
    low = (text or "").casefold()
    return any(k in low for k in kws)


def ig_rules_for(ig_id: str, kind: str) -> list:
    rows = db.execute(
        "SELECT * FROM ig_rules WHERE ig_id=? AND kind=? AND active=1", (ig_id, kind)
    ).fetchall()
    # сначала правила для конкретного поста и с конкретным словом, «любое слово» — в конце
    return sorted(rows, key=lambda r: (not r["media_id"], "*" in (r["keyword"] or "*"), r["id"]))


def ig_parse_buttons(text: str) -> list[dict]:
    """Строки вида «Купить | https://сайт.com». Не больше 3 кнопок."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line == "-":
            continue
        if "|" in line:
            title, url = [x.strip() for x in line.split("|", 1)]
        else:
            title, url = "Открыть", line
        if not url.startswith("http"):
            if "." in url and " " not in url:
                url = "https://" + url
            else:
                continue
        out.append({"t": (title or "Открыть")[:20], "u": url})
        if len(out) == 3:
            break
    return out


# ---------- запросы к Instagram API ----------
class IGError(Exception):
    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


def _ig_http_sync(method: str, url: str, params=None, body=None, form=None) -> dict:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data, headers = None, {"User-Agent": "Mozilla/5.0"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
    try:
        js = json.loads(raw.decode("utf-8", "ignore") or "{}")
    except ValueError:
        js = {"error": {"message": raw[:200].decode("utf-8", "ignore")}}
    if isinstance(js, dict):
        er = js.get("error")
        if er:
            if isinstance(er, dict):
                raise IGError(str(er.get("message", "error")), int(er.get("code") or 0))
            raise IGError(str(er))
        if js.get("error_message"):
            raise IGError(str(js["error_message"]), int(js.get("code") or 0))
    return js


async def ig_call(method: str, path: str, token: str | None = None, params=None, body=None, form=None) -> dict:
    url = path if path.startswith("http") else IG_API + path
    params = dict(params or {})
    if token:
        params["access_token"] = token
    return await asyncio.to_thread(_ig_http_sync, method, url, params, body, form)


async def ig_tell_owner(acc, text: str):
    """Пишет владельцу аккаунта в Telegram (не чаще раза в час на один текст)."""
    if not BOT2 or not acc:
        return
    key = f"ig_note:{acc['ig_id']}:{text[:30]}"
    now = time.time()
    if now - notified.get(key, 0) < 3600:
        return
    notified[key] = now
    try:
        await BOT2.send_message(acc["owner"], text)
    except Exception:
        pass


notified: dict[str, float] = {}


def ig_button_url(b: dict) -> str:
    return f"{WEBAPP_URL}/r/{b['l']}" if WEBAPP_URL and b.get("l") else b["u"]


async def ig_send(acc, text: str, buttons: list[dict] | None = None, user_id: str | None = None,
                  comment_id: str | None = None, postback: tuple[str, str] | None = None):
    """Личное сообщение. Если указан comment_id — это «ответ в личку» на комментарий."""
    rec = {"comment_id": comment_id} if comment_id else {"id": user_id}
    buttons = buttons or []
    btns = [{"type": "web_url", "url": ig_button_url(b), "title": b["t"][:20]} for b in buttons[:3]]
    if postback and len(btns) < 3:
        btns.append({"type": "postback", "title": postback[0][:20], "payload": postback[1]})
    if btns:
        msg = {"attachment": {"type": "template", "payload": {
            "template_type": "button", "text": text[:640], "buttons": btns}}}
    else:
        msg = {"text": text[:1000]}
    try:
        await ig_call("POST", "/me/messages", acc["token"], body={"recipient": rec, "message": msg})
    except IGError as e:
        if e.code == 190:
            await ig_tell_owner(acc, "⚠️ Instagram отключил доступ (токен устарел). Подключите аккаунт заново: /menu → Подключить.")
            raise
        if not btns:
            raise
        # запасной вариант: обычный текст со ссылками
        plain = text + "\n\n" + "\n".join(f"{b['t']}: {ig_button_url(b)}" for b in buttons[:3])
        await ig_call("POST", "/me/messages", acc["token"],
                      body={"recipient": rec, "message": {"text": plain[:1000]}})


async def ig_check_follow(acc, uid: str) -> bool | None:
    """Подписан ли человек на аккаунт. None — Instagram не ответил."""
    try:
        r = await ig_call("GET", f"/{uid}", acc["token"], params={"fields": "username,is_user_follow_business"})
    except IGError:
        return None
    f = r.get("is_user_follow_business")
    if f is None:
        return None
    f = bool(f)
    row = db.execute("SELECT follows, username FROM ig_contacts WHERE ig_id=? AND user_id=?",
                     (acc["ig_id"], uid)).fetchone()
    old = row["follows"] if row else -1
    uname = r.get("username") or (row["username"] if row else "")
    if not row:
        ig_touch(acc["ig_id"], uid, uname)
    dbx("UPDATE ig_contacts SET follows=?, follow_checked=?, username=? WHERE ig_id=? AND user_id=?",
        (int(f), int(time.time()), uname, acc["ig_id"], uid))
    if old == 0 and f:
        dbx("INSERT INTO ig_events(ig_id,user_id,username,kind,ts) VALUES(?,?,?,?,?)",
            (acc["ig_id"], uid, uname, "follow", int(time.time())))
    elif old == 1 and not f:
        dbx("INSERT INTO ig_events(ig_id,user_id,username,kind,ts) VALUES(?,?,?,?,?)",
            (acc["ig_id"], uid, uname, "unfollow", int(time.time())))
    return f


def ig_follow_buttons(acc) -> list[dict]:
    return [{"t": "Подписаться", "u": f"https://www.instagram.com/{acc['username']}/"}]


def ig_products(acc) -> list:
    return db.execute("SELECT * FROM ig_products WHERE ig_id=? ORDER BY id LIMIT 10", (acc["ig_id"],)).fetchall()


async def ig_send_catalog(acc, user_id: str):
    """Карусель товаров: фото, название, цена, кнопка со ссылкой."""
    elements = []
    for p in ig_products(acc):
        el = {"title": p["title"][:80], "subtitle": (p["subtitle"] or "")[:80] or "—",
              "buttons": [{"type": "web_url", "url": ig_button_url({"u": p["url"], "l": p["link_id"]}),
                           "title": (p["btn"] or "Купить")[:20]}]}
        if p["img"] and WEBAPP_URL:
            el["image_url"] = f"{WEBAPP_URL}/img/{p['img']}"
        elements.append(el)
    if not elements:
        return
    msg = {"attachment": {"type": "template", "payload": {"template_type": "generic", "elements": elements}}}
    await ig_call("POST", "/me/messages", acc["token"], body={"recipient": {"id": user_id}, "message": msg})


async def ig_sync_menu(acc) -> str:
    """Кнопки меню чата (ice breakers) — до 4 штук. Возвращает текст ошибки или пустую строку."""
    rules = db.execute("SELECT * FROM ig_rules WHERE ig_id=? AND kind='menu' AND active=1 ORDER BY id LIMIT 4",
                       (acc["ig_id"],)).fetchall()
    try:
        if rules:
            await ig_call("POST", "/me/messenger_profile", acc["token"], body={"ice_breakers": [{
                "call_to_actions": [{"question": r["keyword"][:80], "payload": f"ib:{r['id']}"} for r in rules],
                "locale": "default"}]})
        else:
            await ig_call("DELETE", "/me/messenger_profile", acc["token"], body={"fields": ["ice_breakers"]})
    except IGError as e:
        return str(e)
    return ""


async def ig_deliver(acc, rule, user_id: str | None = None, comment_id: str | None = None, verified: bool = False):
    """Отправляет сообщение по правилу (с проверкой подписки и каталогом, если они включены)."""
    if rule["need_follow"] and not verified:
        await ig_send(acc, f"Чтобы получить это, подпишитесь на @{acc['username']}: нажмите «Подписаться», "
                           "потом вернитесь сюда и нажмите «Проверить» 👇",
                      ig_follow_buttons(acc), user_id=user_id, comment_id=comment_id,
                      postback=("✅ Проверить", f"chk:{rule['id']}"))
    else:
        buttons = json.loads(rule["buttons"] or "[]")
        has_cat = bool(rule["catalog"]) and bool(ig_products(acc))
        if has_cat and comment_id:  # на комментарий можно ответить одним сообщением — каталог по кнопке
            await ig_send(acc, rule["message"], buttons[:2], user_id=user_id, comment_id=comment_id,
                          postback=("🛍 Каталог", f"cat:{rule['id']}"))
        else:
            await ig_send(acc, rule["message"], buttons, user_id=user_id, comment_id=comment_id)
            if has_cat and user_id:
                await ig_send_catalog(acc, user_id)
        dbx("UPDATE ig_rules SET sent=sent+1 WHERE id=?", (rule["id"],))
        ig_log(acc["ig_id"], "dm")
    if user_id:
        dbx("UPDATE ig_contacts SET last_auto=? WHERE ig_id=? AND user_id=?",
            (int(time.time()), acc["ig_id"], user_id))


# ---------- обработка событий Instagram (вебхук) ----------
async def ig_on_comment(acc, v: dict):
    cid = str(v.get("id") or "")
    frm = v.get("from") or {}
    uid = str(frm.get("id") or "")
    text = v.get("text") or ""
    media = str((v.get("media") or {}).get("id") or "")
    if not cid or not uid or uid in (acc["ig_id"], str(acc["alt_id"])):
        return  # свои же ответы не трогаем
    if not ig_seen("c:" + cid):
        return
    sub = await get_sub(acc["owner"])
    tier = tier_of(sub)
    if tier == "expired":
        await ig_tell_owner(acc, MSG_FREE_ENDED)
        return
    rules = ig_rules_for(acc["ig_id"], "comment")
    if tier == "free":  # Free: одно правило на один пост
        own = sorted((r for r in rules if r["media_id"]), key=lambda r: r["id"])
        rules = own[:1]
        if rules and not sub.get("free_started"):
            await sub_update(acc["owner"], free_started=int(time.time()))
    rule = next((r for r in rules
                 if (not r["media_id"] or r["media_id"] == media) and ig_match(r, text)), None)
    if not rule:
        return
    ig_touch(acc["ig_id"], uid, frm.get("username"))
    ig_log(acc["ig_id"], "comment")
    variants = [x.strip() for x in (rule["public_reply"] or "").splitlines() if x.strip()]
    if variants:
        try:
            await ig_call("POST", f"/{cid}/replies", acc["token"], params={"message": random.choice(variants)})
        except IGError:
            logging.warning("IG: не удалось ответить в комментарии")
    if tier != "pro":
        return  # Free: только ответ в комментарии, без сообщения в Direct
    try:
        await ig_deliver(acc, rule, user_id=uid, comment_id=cid)
    except IGError as e:
        logging.warning("IG: личное сообщение на комментарий не ушло: %s", e)


async def ig_on_messaging(acc, m: dict):
    uid = str((m.get("sender") or {}).get("id") or "")
    if not uid or uid in (acc["ig_id"], str(acc["alt_id"])):
        return
    if await acc_tier(acc) != "pro":
        return  # автоматизация Direct — только Business Pro
    msg = m.get("message") or {}
    if msg.get("is_echo"):
        return
    pb = m.get("postback") or {}
    mid = msg.get("mid") or pb.get("mid") or ""
    if mid and not ig_seen("m:" + mid):
        return
    ig_touch(acc["ig_id"], uid)
    payload = pb.get("payload") or (msg.get("quick_reply") or {}).get("payload") or ""
    kind, _, rid = payload.partition(":")
    if kind in ("chk", "ib", "cat") and rid.isdigit():
        rule = db.execute("SELECT * FROM ig_rules WHERE id=? AND ig_id=?", (int(rid), acc["ig_id"])).fetchone()
        if not rule:
            return
        if kind == "cat":  # «Каталог» из сообщения-ответа на комментарий
            await ig_send_catalog(acc, uid)
        elif kind == "ib":  # кнопка меню чата
            await ig_deliver(acc, rule, user_id=uid)
        else:  # «Проверить подписку»
            f = await ig_check_follow(acc, uid)
            if f is False:
                await ig_send(acc, "Пока не вижу подписки 🙈 Подпишитесь и нажмите «Проверить» ещё раз.",
                              ig_follow_buttons(acc), user_id=uid, postback=("✅ Проверить", f"chk:{rule['id']}"))
            else:  # подписан (или Instagram не смог ответить — не мучаем человека)
                await ig_deliver(acc, rule, user_id=uid, verified=True)
        return
    # упоминание аккаунта в сторис
    if any(a.get("type") == "story_mention" for a in (msg.get("attachments") or [])):
        rule = next(iter(ig_rules_for(acc["ig_id"], "story_mention")), None)
        if rule:
            await ig_deliver(acc, rule, user_id=uid)
        return
    text = msg.get("text") or ""
    # ответ на сторис
    if (msg.get("reply_to") or {}).get("story"):
        rule = next((r for r in ig_rules_for(acc["ig_id"], "story_reply") if ig_match(r, text)), None)
        if rule:
            await ig_deliver(acc, rule, user_id=uid)
            return
    if not text:
        return
    rule = next((r for r in ig_rules_for(acc["ig_id"], "dm") if ig_match(r, text)), None)
    if rule:
        await ig_deliver(acc, rule, user_id=uid)
        return
    last = db.execute("SELECT last_auto FROM ig_contacts WHERE ig_id=? AND user_id=?",
                      (acc["ig_id"], uid)).fetchone()["last_auto"]
    reply = None
    if last == 0 and acc["welcome_on"] and acc["welcome"]:
        reply = acc["welcome"]
    elif acc["dflt_on"] and acc["dflt"] and time.time() - last > IG_AUTO_COOLDOWN:
        reply = acc["dflt"]
    if reply:
        await ig_send(acc, reply, user_id=uid)
        dbx("UPDATE ig_contacts SET last_auto=? WHERE ig_id=? AND user_id=?",
            (int(time.time()), acc["ig_id"], uid))
        ig_log(acc["ig_id"], "dm")


async def ig_handle_payload(data: dict):
    for entry in data.get("entry", []) or []:
        acc = ig_acc(entry.get("id"))
        if not acc:
            continue
        for ch in entry.get("changes", []) or []:
            if ch.get("field") == "comments":
                try:
                    await ig_on_comment(acc, ch.get("value") or {})
                except Exception:
                    logging.exception("IG: ошибка обработки комментария")
        for m in entry.get("messaging", []) or []:
            try:
                await ig_on_messaging(acc, m)
            except Exception:
                logging.exception("IG: ошибка обработки сообщения")


async def ig_webhook_get(request: web.Request):
    q = request.query
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == IG_VERIFY:
        return web.Response(text=q.get("hub.challenge", ""))
    return web.Response(status=403, text="forbidden")


async def ig_webhook_post(request: web.Request):
    raw = await request.read()
    if IG_APP_SECRET:
        want = "sha256=" + hmac.new(IG_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("X-Hub-Signature-256", ""), want):
            return web.Response(status=403, text="bad signature")
    try:
        data = json.loads(raw)
    except ValueError:
        return web.Response(status=400, text="bad json")
    task = asyncio.create_task(ig_handle_payload(data))  # Instagram ждёт ответ быстро
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    return web.Response(text="ok")


async def ig_redirect(request: web.Request):
    row = db.execute("SELECT * FROM ig_links WHERE id=?", (request.match_info["lid"],)).fetchone()
    if not row:
        return web.Response(status=404, text="not found")
    dbx("UPDATE ig_links SET clicks=clicks+1 WHERE id=?", (row["id"],))
    ig_log(row["ig_id"], "click")
    raise web.HTTPFound(row["url"])


async def ig_image(request: web.Request):
    name = request.match_info["name"]
    path = IG_IMG_DIR / name
    if not re.fullmatch(r"[0-9a-f]{12}\.jpg", name) or not path.exists():
        return web.Response(status=404, text="not found")
    return web.FileResponse(path)


def ig_state(uid: int) -> str:
    key = (IG_APP_SECRET or TOKEN2 or "x").encode()
    return f"{uid}.{hmac.new(key, str(uid).encode(), hashlib.sha256).hexdigest()[:16]}"


def ig_state_uid(state: str) -> int | None:
    head, _, sig = (state or "").partition(".")
    if head.isdigit() and hmac.compare_digest(ig_state(int(head)), f"{head}.{sig}"):
        return int(head)
    return None


def ig_oauth_url(uid: int) -> str | None:
    if not (IG_APP_ID and IG_APP_SECRET and WEBAPP_URL):
        return None
    q = urllib.parse.urlencode({
        "client_id": IG_APP_ID, "redirect_uri": WEBAPP_URL + "/ig/oauth", "response_type": "code",
        "scope": IG_SCOPES, "state": ig_state(uid), "force_reauth": "true",
    })
    return "https://www.instagram.com/oauth/authorize?" + q


def _page(text: str) -> web.Response:
    html = ("<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<body style='font-family:sans-serif;text-align:center;padding:40px'>"
            f"<h2>{text}</h2></body>")
    return web.Response(text=html, content_type="text/html")


async def ig_oauth_cb(request: web.Request):
    code = (request.query.get("code") or "").split("#")[0]
    uid = ig_state_uid(request.query.get("state", ""))
    if not code or not uid:
        return _page("❌ Не получилось. Вернитесь в Telegram и нажмите «Подключить Instagram» снова.")
    try:
        r = await ig_call("POST", "https://api.instagram.com/oauth/access_token", form={
            "client_id": IG_APP_ID, "client_secret": IG_APP_SECRET, "grant_type": "authorization_code",
            "redirect_uri": WEBAPP_URL + "/ig/oauth", "code": code})
        item = (r.get("data") or [r])[0]
        token = item["access_token"]
        try:  # долгий токен (60 дней)
            r2 = await ig_call("GET", "https://graph.instagram.com/access_token", params={
                "grant_type": "ig_exchange_token", "client_secret": IG_APP_SECRET, "access_token": token})
            token = r2.get("access_token") or token
        except IGError:
            pass
        acc, warn = await ig_connect(uid, token)
    except PlanLimit as e:
        return _page("🔒 " + str(e))
    except Exception as e:
        logging.exception("IG OAuth")
        return _page("❌ Ошибка подключения: " + str(e)[:120])
    if BOT2:
        try:
            await BOT2.send_message(uid, f"✅ Instagram @{acc['username']} подключён!\n" + (f"⚠️ {warn}\n" if warn else "")
                                    + "Теперь создайте первую автоматизацию 👇", reply_markup=ig_menu_kb(uid))
        except Exception:
            pass
    return _page("✅ Готово! Вернитесь в Telegram.")


def ig_register_routes(app: web.Application):
    app.router.add_get("/ig/webhook", ig_webhook_get)
    app.router.add_post("/ig/webhook", ig_webhook_post)
    app.router.add_get("/ig/oauth", ig_oauth_cb)
    app.router.add_get("/r/{lid}", ig_redirect)
    app.router.add_get("/img/{name}", ig_image)


async def ig_snapshot(acc):
    r = await ig_call("GET", "/me", acc["token"], params={"fields": "followers_count"})
    if r.get("followers_count") is not None:
        dbx("INSERT OR REPLACE INTO ig_followers(ig_id, day, cnt) VALUES(?,?,?)",
            (acc["ig_id"], ig_today(), int(r["followers_count"])))
        return int(r["followers_count"])
    return None


async def ig_connect(owner: int, token: str):
    me = await ig_call("GET", "/me", token, params={"fields": "user_id,username,followers_count"})
    ig_id = str(me.get("user_id") or me.get("id") or "")
    if not ig_id:
        raise IGError("Instagram не вернул ID аккаунта")
    sub = await get_sub(owner, fresh=True)
    mine = [a["ig_id"] for a in ig_accs_of(owner)]
    if ig_id not in mine:
        if tier_of(sub) == "pro":
            if len(mine) >= PRO_MAX_ACCOUNTS:
                raise PlanLimit(f"Лимит Business Pro — {PRO_MAX_ACCOUNTS} аккаунтов.")
        elif mine:
            raise PlanLimit("На Free можно подключить только 1 аккаунт. Подключите Business Pro: /tariff")
    dbx(
        "INSERT INTO ig_accounts(ig_id, alt_id, owner, username, token, token_at, created) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(ig_id) DO UPDATE SET alt_id=excluded.alt_id, owner=excluded.owner, "
        "username=excluded.username, token=excluded.token, token_at=excluded.token_at",
        (ig_id, str(me.get("id") or ""), owner, me.get("username") or "", token, int(time.time()), int(time.time())),
    )
    acc = ig_acc(ig_id)
    dbx("INSERT OR REPLACE INTO ig_sel(owner, ig_id) VALUES(?,?)", (owner, ig_id))
    warn = ""
    try:
        await ig_call("POST", "/me/subscribed_apps", token,
                      params={"subscribed_fields": "comments,messages,messaging_postbacks"})
    except IGError as e:
        warn = f"Не удалось включить вебхук: {e}"
    if me.get("followers_count") is not None:
        dbx("INSERT OR REPLACE INTO ig_followers(ig_id, day, cnt) VALUES(?,?,?)",
            (ig_id, ig_today(), int(me["followers_count"])))
    await client_update(owner, ig=[a["username"] for a in ig_accs_of(owner)])
    return acc, warn


async def ig_daily_once():
    now = int(time.time())
    dbx("DELETE FROM ig_done WHERE ts<?", (now - 3 * 86400,))
    dbx("DELETE FROM ig_log WHERE ts<?", (now - 95 * 86400,))
    for acc in db.execute("SELECT * FROM ig_accounts").fetchall():
        try:
            if not db.execute("SELECT 1 FROM ig_followers WHERE ig_id=? AND day=?", (acc["ig_id"], ig_today())).fetchone():
                await ig_snapshot(acc)
            if now - (acc["token_at"] or 0) > 30 * 86400:  # продлеваем токен
                r = await ig_call("GET", "https://graph.instagram.com/refresh_access_token",
                                  params={"grant_type": "ig_refresh_token", "access_token": acc["token"]})
                if r.get("access_token"):
                    dbx("UPDATE ig_accounts SET token=?, token_at=? WHERE ig_id=?", (r["access_token"], now, acc["ig_id"]))
        except IGError as e:
            if e.code == 190:
                await ig_tell_owner(acc, "⚠️ Токен Instagram устарел. Подключите аккаунт заново: /menu")
            logging.warning("IG daily: %s", e)
            continue
        if tier_of(await get_sub(acc["owner"])) != "pro":
            continue  # динамика подписок по контактам — только Business Pro
        # проверка подписки у контактов (кто подписался / отписался)
        rows = db.execute(
            "SELECT user_id FROM ig_contacts WHERE ig_id=? AND last_seen>? AND follow_checked<? "
            "ORDER BY follow_checked LIMIT 60", (acc["ig_id"], now - 30 * 86400, now - 86400)).fetchall()
        for r in rows:
            await ig_check_follow(acc, r["user_id"])
            dbx("UPDATE ig_contacts SET follow_checked=? WHERE ig_id=? AND user_id=?",
                (now, acc["ig_id"], r["user_id"]))
            await asyncio.sleep(1)


async def ig_daily_loop():
    while True:
        try:
            await ig_daily_once()
        except Exception:
            logging.exception("IG daily")
        await asyncio.sleep(3600)


# ---------- отчёты ----------
def ig_names(rows, limit=15) -> str:
    names = [("@" + r["username"]) if r["username"] else f"id{r['user_id']}" for r in rows]
    s = ", ".join(names[:limit])
    return s + (f" и ещё {len(names) - limit}" if len(names) > limit else "")


def ig_subs_text(acc) -> str:
    rows = db.execute("SELECT day, cnt FROM ig_followers WHERE ig_id=? AND day>=? ORDER BY day",
                      (acc["ig_id"], ig_today(30))).fetchall()
    out = [f"👥 Подписчики @{acc['username']} — 30 дней\n"]
    if not rows:
        out.append("Данных пока нет — учёт начнётся автоматически, загляните завтра.")
    else:
        first, last = rows[0], rows[-1]
        diff = last["cnt"] - first["cnt"]
        out.append(f"Сейчас: {last['cnt']}")
        out.append(f"С {first['day']}: {first['cnt']} → {last['cnt']} ({'+' if diff >= 0 else ''}{diff})")
        if len(rows) > 1:
            out.append("\nПо дням:")
            for a, b in list(zip(rows, rows[1:]))[-10:]:
                d = b["cnt"] - a["cnt"]
                out.append(f"{b['day'][5:]}: {b['cnt']} ({'+' if d >= 0 else ''}{d})")
    since = int(time.time()) - 30 * 86400
    ev = lambda k: db.execute("SELECT * FROM ig_events WHERE ig_id=? AND kind=? AND ts>? ORDER BY ts DESC",
                              (acc["ig_id"], k, since)).fetchall()
    fol, unf = ev("follow"), ev("unfollow")
    out.append("\nСреди тех, кто писал или комментировал:")
    out.append(f"✅ Подписались ({len(fol)}): {ig_names(fol) if fol else '—'}")
    out.append(f"❌ Отписались ({len(unf)}): {ig_names(unf) if unf else '—'}")
    out.append("\nℹ️ Instagram не отдаёт ни одному сервису (и ManyChat тоже) полный список подписчиков — "
               "поэтому по именам видно только контактов бота, а по остальным — число.")
    return "\n".join(out)


def ig_stats_text(acc) -> str:
    since = int(time.time()) - 30 * 86400
    cnt = lambda k: db.execute("SELECT COUNT(*) FROM ig_log WHERE ig_id=? AND kind=? AND ts>?",
                               (acc["ig_id"], k, since)).fetchone()[0]
    contacts = db.execute("SELECT COUNT(*) FROM ig_contacts WHERE ig_id=?", (acc["ig_id"],)).fetchone()[0]
    out = [f"📊 Статистика @{acc['username']} — 30 дней\n",
           f"💬 Сработало на комментарии: {cnt('comment')}",
           f"📩 Отправлено сообщений: {cnt('dm')}",
           f"🔗 Кликов по ссылкам: {cnt('click')}",
           f"👤 Контактов всего: {contacts}"]
    rules = db.execute("SELECT r.*, COALESCE((SELECT SUM(clicks) FROM ig_links WHERE rule_id=r.id),0) AS cl "
                       "FROM ig_rules r WHERE ig_id=? ORDER BY sent DESC LIMIT 8", (acc["ig_id"],)).fetchall()
    if rules:
        out.append("\nПо автоматизациям:")
        for r in rules:
            out.append(f"{IG_ICON.get(r['kind'], '•')} «{r['keyword']}» — отправлено {r['sent']}, кликов {r['cl']}")
    prods = db.execute("SELECT p.title, COALESCE(l.clicks,0) AS cl FROM ig_products p "
                       "LEFT JOIN ig_links l ON l.id=p.link_id WHERE p.ig_id=? ORDER BY cl DESC LIMIT 5",
                       (acc["ig_id"],)).fetchall()
    if prods:
        out.append("\nТовары (клики по «Купить»):")
        out += [f"🛍 {p['title'][:30]} — {p['cl']}" for p in prods]
    return "\n".join(out)


# ---------- Telegram-бот №2: кнопки и мастер настройки ----------
def _b(text: str, data: str | None = None, url: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data, url=url) if not url else InlineKeyboardButton(text=text, url=url)


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ig_menu_kb(uid: int) -> InlineKeyboardMarkup:
    lock = "" if tier_of(sub_cached(uid)) == "pro" else "🔒 "
    if not ig_acc_of(uid):
        return _kb([[_b("🔗 Подключить Instagram", "g:connect")],
                    [_b("💎 Тарифы", "t:menu"), _b("❓ Как это работает", "g:help")]])
    return _kb([
        [_b("➕ Новая автоматизация", "g:new")],
        [_b("📋 Мои автоматизации", "g:list")],
        [_b(lock + "💬 Автоответчик в Direct", "g:dm"), _b(lock + "🛍 Каталог", "g:cat")],
        [_b(lock + "👥 Подписчики (30 дней)", "g:subs"), _b(lock + "📊 Статистика", "g:stats")],
        [_b("📱 Аккаунты", "g:accs"), _b("💎 Тарифы", "t:menu")],
        [_b("❓ Помощь", "g:help"), _b("🔌 Отключить", "g:disc")],
    ])


IG_WELCOME = (
    "👋 Это бот для автоматизации Instagram — как ManyChat.\n\n"
    "• Человек пишет под постом ключевое слово → бот отвечает в комментарии и присылает сообщение с кнопками в Direct\n"
    "• Автоответчик в Direct: приветствие и ответы по словам\n"
    "• Упоминание и ответ на сторис → сообщение в Direct\n• Меню в чате: кнопки «Цены», «Заказать», «Связаться»\n• Каталог товаров каруселью с фото и кнопкой «Купить»\n• «Сначала подпишись» → потом ссылка или товар\n"
    "• Динамика подписчиков и статистика кликов\n\n"
    "Нужен Instagram-аккаунт типа Business или Creator.\n\n"
    "🆓 Free — 1 аккаунт, ответ в комментариях под одним постом, 3 дня.\n"
    "💎 Business Pro — все функции. Тарифы: /tariff"
)


async def ig_show_menu(msg: Message, uid: int, text: str | None = None, edit: bool = False):
    acc = ig_acc_of(uid)
    sub = await get_sub(uid)
    head = text or (IG_WELCOME if not acc else f"📱 Аккаунт: @{acc['username']}\nВыберите действие 👇")
    if not text:
        head += "\n\n" + plan_line(sub)
    if edit:
        await edit_or_answer(msg, head, reply_markup=ig_menu_kb(uid))
    else:
        await msg.answer(head, reply_markup=ig_menu_kb(uid))


def ig_is_owner(uid: int) -> bool:
    return not ADMIN_IDS or uid in ADMIN_IDS


def ig_help_text(uid: int) -> str:
    base = (
        "❓ Как пользоваться\n\n"
        "1. «Подключить Instagram» → войдите и разрешите доступ.\n"
        "2. «Новая автоматизация» → выберите пост, ключевое слово («ХОЧУ»), текст и кнопки со ссылкой.\n"
        "3. Готово: кто напишет слово в комментариях — получит сообщение в Direct.\n\n"
        "Правила Instagram: на комментарий можно ответить в личку один раз и в течение 7 дней; "
        "в Direct бот отвечает в течение 24 часов после сообщения человека."
    )
    if not ig_is_owner(uid):
        return base
    return base + (
        "\n\n⚙️ Настройка владельца (один раз):\n"
        "1) developers.facebook.com → Создать приложение (Business) → добавить продукт Instagram → «API с входом через Instagram».\n"
        f"2) Webhooks → Callback URL:\n{(WEBAPP_URL or 'https://ВАШ-ДОМЕН') + '/ig/webhook'}\n"
        f"Verify token:\n{IG_VERIFY}\n"
        "Подписки: comments, messages, messaging_postbacks.\n"
        f"3) Business login → Redirect URL:\n{(WEBAPP_URL or 'https://ВАШ-ДОМЕН') + '/ig/oauth'}\n"
        "4) В bothost в переменные: BOT2_TOKEN, IG_APP_ID, IG_APP_SECRET, DOMAIN (адрес вашего бота на bothost).\n"
        "5) Пока приложение Meta в режиме разработки, добавляйте Instagram-аккаунты в «Роли → Тестировщики Instagram».\n"
        "6) Клиенты и тарифы: Firebase → Realtime Database → скопируйте адрес базы в FIREBASE_DB_URL "
        "и положите JSON-ключ сервисного аккаунта в /app/data/firebase.json (или FIREBASE_KEY_B64). "
        "Без них данные хранятся локально.\n"
        "7) Чеки об оплате приходят админам из ADMIN_ID в этого бота."
    )


@dp2.message(CommandStart())
@dp2.message(Command("menu"))
async def ig_start(message: Message):
    ig_wait.pop(message.from_user.id, None)
    adm_wait.pop(message.from_user.id, None)
    bg(client_touch(message.from_user))
    await ig_show_menu(message, message.from_user.id)


@dp2.message(Command("cancel"))
async def ig_cancel(message: Message):
    uid = message.from_user.id
    ig_wait.pop(uid, None)
    adm_wait.pop(uid, None)
    pay = await pay_awaiting(uid)
    if pay:
        await pay_cancel_do(pay)
    await ig_show_menu(message, uid, "Отменено.")


IG_ICON = {"comment": "💬", "dm": "✉️", "story_mention": "📸", "story_reply": "↩️", "menu": "📋"}
IG_KIND_NAME = {
    "comment": "💬 Комментарии → Direct", "dm": "✉️ Сообщение в Direct", "story_mention": "📸 Упоминание в сторис",
    "story_reply": "↩️ Ответ на сторис", "menu": "📋 Кнопка меню чата",
}


def ig_rule_title(r) -> str:
    on = "✅" if r["active"] else "⏸"
    return f"{on} {IG_ICON.get(r['kind'], '•')} «{(r['keyword'] or '*')[:20]}» · {r['sent']}"


def ig_rule_card(r) -> str:
    btns = json.loads(r["buttons"] or "[]")
    clicks = db.execute("SELECT COALESCE(SUM(clicks),0) FROM ig_links WHERE rule_id=?", (r["id"],)).fetchone()[0]
    kw_label = "Название кнопки меню" if r["kind"] == "menu" else "Слово"
    lines = [IG_KIND_NAME.get(r["kind"], r["kind"]), f"{kw_label}: {r['keyword'] if r['keyword'] != '*' else 'любое'}"]
    if r["kind"] == "comment":
        lines.append("Пост: " + (r["media_id"] and "выбранный" or "все посты"))
        if r["public_reply"]:
            lines.append("Ответ в комментарии: " + r["public_reply"].splitlines()[0][:60])
    lines += [f"Сообщение: {r['message'][:200]}"]
    if btns:
        lines.append("Кнопки: " + ", ".join(b["t"] for b in btns))
    lines.append("Только для подписчиков: " + ("да" if r["need_follow"] else "нет"))
    lines.append("Каталог товаров: " + ("да" if r["catalog"] else "нет"))
    lines.append(f"\nОтправлено: {r['sent']} · Кликов: {clicks}")
    lines.append("Статус: " + ("включено ✅" if r["active"] else "выключено ⏸"))
    return "\n".join(lines)


async def ig_finish_rule(call_or_msg: Message, uid: int):
    w = ig_wait.pop(uid, None)
    acc = ig_acc_of(uid)
    if not w or not acc:
        return
    d = w["data"]
    buttons = []
    for b in d.get("buttons", []):
        lid = uuid.uuid4().hex[:8]
        dbx("INSERT INTO ig_links(id, url, ig_id, rule_id) VALUES(?,?,?,0)", (lid, b["u"], acc["ig_id"]))
        buttons.append({**b, "l": lid})
    cur = dbx(
        "INSERT INTO ig_rules(ig_id, kind, keyword, media_id, message, buttons, public_reply, need_follow, catalog, created) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (acc["ig_id"], d["kind"], d["kw"], d.get("media", ""), d.get("msg", ""), json.dumps(buttons, ensure_ascii=False),
         d.get("pub", ""), d.get("follow", 0), d.get("catalog", 0), int(time.time())))
    for b in buttons:
        dbx("UPDATE ig_links SET rule_id=? WHERE id=?", (cur.lastrowid, b["l"]))
    tips = {
        "comment": "Проверьте: с другого аккаунта напишите комментарий со словом под нужным постом.",
        "dm": "Проверьте: с другого аккаунта напишите вашему аккаунту это слово в Direct.",
        "story_mention": "Проверьте: с другого аккаунта упомяните ваш аккаунт в сторис.",
        "story_reply": "Проверьте: с другого аккаунта ответьте на вашу сторис.",
        "menu": "Кнопка появится в новом чате с вашим аккаунтом (у тех, кто ещё не писал вам).",
    }
    extra = ""
    if d["kind"] == "menu":
        err = await ig_sync_menu(acc)
        extra = f"\n⚠️ Меню в Instagram не обновилось: {err[:150]}" if err else ""
    sub = await get_sub(uid)
    if tier_of(sub) != "pro" and d["kind"] == "comment":
        fs = sub.get("free_started")
        if not fs:
            fs = int(time.time())
            await sub_update(uid, free_started=fs)
        extra += (f"\n🆓 Free: работает до {fdate(fs + FREE_DAYS * 86400)} "
                  "(только публичный ответ под комментарием).")
    await call_or_msg.answer(f"✅ Автоматизация создана!\n{tips[d['kind']]}{extra}", reply_markup=ig_menu_kb(uid))


async def ig_ask_follow(msg: Message):
    await msg.answer(
        "Отправлять только тем, кто подписан на аккаунт?\n"
        "(Бот пришлёт кнопку «Подписаться» — она откроет ваш профиль, — и кнопку «Проверить»)",
        reply_markup=_kb([[_b("✅ Да, только подписчикам", "g:f:1")], [_b("Нет, всем", "g:f:0")]]))


@dp2.callback_query(F.data.startswith("g:"))
async def ig_cb(call: CallbackQuery):
    uid = call.from_user.id
    parts = call.data.split(":")
    act = parts[1]
    msg = call.message
    acc = ig_acc_of(uid)
    sub = await get_sub(uid)
    pro = tier_of(sub) == "pro"
    await call.answer()
    if act == "menu":
        ig_wait.pop(uid, None)
        return await ig_show_menu(msg, uid, edit=True)
    if act == "help":
        return await edit_or_answer(msg, ig_help_text(uid), reply_markup=_kb([[_b("⬅️ Меню", "g:menu")]]))
    if act in ("connect", "token") and acc:
        if not pro:
            return await ig_paywall(msg, "Второй аккаунт Instagram (на Free — один)")
        if len(ig_accs_of(uid)) >= PRO_MAX_ACCOUNTS:
            return await edit_or_answer(msg, f"Достигнут лимит: {PRO_MAX_ACCOUNTS} аккаунтов.",
                                        reply_markup=_kb([[_b("⬅️ Меню", "g:menu")]]))
    if act == "connect":
        url = ig_oauth_url(uid)
        rows = []
        if url:
            rows.append([_b("📲 Войти через Instagram", url=url)])
        rows += [[_b("🔑 Вставить токен вручную", "g:token")], [_b("⬅️ Меню", "g:menu")]]
        txt = ("Нажмите кнопку, войдите в Instagram и разрешите доступ 👇" if url else
               "Автовход ещё не настроен владельцем бота (нужны IG_APP_ID, IG_APP_SECRET и DOMAIN).\n"
               "Можно подключить вручную по токену — см. «Помощь».")
        return await edit_or_answer(msg, txt, reply_markup=_kb(rows))
    if act == "token":
        ig_wait[uid] = {"step": "token"}
        return await msg.answer("Пришлите токен доступа Instagram одним сообщением (отмена: /cancel).")
    if not acc:
        return await ig_show_menu(msg, uid, "Сначала подключите Instagram 👇", edit=True)

    if act in ("subs", "stats", "dm", "w", "df", "cat", "pa", "pd") and not pro:
        names = {"subs": "Динамика подписчиков", "stats": "Статистика", "dm": "Автоответчик в Direct",
                 "w": "Автоответчик в Direct", "df": "Автоответчик в Direct"}
        return await ig_paywall(msg, names.get(act, "Каталог товаров"))

    if act == "accs":
        rows = [[_b(("✅ " if a["ig_id"] == acc["ig_id"] else "📱 ") + "@" + (a["username"] or a["ig_id"]),
                    f"g:sa:{a['ig_id']}")] for a in ig_accs_of(uid)]
        rows.append([_b("➕ Добавить аккаунт" + ("" if pro else " 🔒"), "g:connect")])
        rows.append([_b("⬅️ Меню", "g:menu")])
        lim = PRO_MAX_ACCOUNTS if pro else 1
        return await edit_or_answer(
            msg, f"📱 Ваши аккаунты Instagram ({len(ig_accs_of(uid))}/{lim})\nВыбран: @{acc['username']}",
            reply_markup=_kb(rows))
    if act == "sa":
        target = ig_acc(parts[2])
        if target and target["owner"] == uid:
            dbx("INSERT OR REPLACE INTO ig_sel(owner, ig_id) VALUES(?,?)", (uid, target["ig_id"]))
        return await ig_show_menu(msg, uid, edit=True)

    if act == "disc":
        return await edit_or_answer(msg, f"Отключить @{acc['username']}? Все автоматизации и данные будут удалены.",
                                    reply_markup=_kb([[_b("Да, отключить", "g:disc2")], [_b("Отмена", "g:menu")]]))
    if act == "disc2":
        i = acc["ig_id"]
        for tbl in ("ig_rules", "ig_contacts", "ig_events", "ig_followers", "ig_links", "ig_log", "ig_products"):
            dbx(f"DELETE FROM {tbl} WHERE ig_id=?", (i,))
        dbx("DELETE FROM ig_accounts WHERE ig_id=?", (i,))
        return await ig_show_menu(msg, uid, "🔌 Аккаунт отключён.", edit=True)

    if act == "subs":
        return await edit_or_answer(msg, ig_subs_text(acc), reply_markup=_kb([[_b("⬅️ Меню", "g:menu")]]))
    if act == "stats":
        return await edit_or_answer(msg, ig_stats_text(acc), reply_markup=_kb([[_b("⬅️ Меню", "g:menu")]]))

    # --- создание автоматизации ---
    if act == "new":
        lk = "" if pro else "🔒 "
        first = "💬 Комментарии под постом → Direct" if pro else "💬 Ответ в комментариях (Free)"
        return await edit_or_answer(msg, "Что автоматизируем?", reply_markup=_kb([
            [_b(first, "g:k:comment")],
            [_b(lk + "✉️ Сообщение в Direct по слову", "g:k:dm")],
            [_b(lk + "📸 Упоминание в сторис", "g:k:story_mention")],
            [_b(lk + "↩️ Ответ на сторис", "g:k:story_reply")],
            [_b(lk + "📋 Кнопка меню в чате", "g:k:menu")],
            [_b("⬅️ Меню", "g:menu")]]))
    if act == "k":
        kind = parts[2]
        if not pro and kind != "comment":
            return await ig_paywall(msg, "Эта автоматизация")
        if not pro:
            if tier_of(sub) == "expired":
                return await ig_paywall(msg, f"Free-период ({FREE_DAYS} дня) закончился, новые автоматизации")
            if db.execute("SELECT COUNT(*) FROM ig_rules WHERE ig_id=? AND kind='comment'",
                          (acc["ig_id"],)).fetchone()[0] >= 1:
                return await edit_or_answer(
                    msg, "На Free доступна одна автоматизация: ответ в комментариях под одним постом на "
                         f"{FREE_DAYS} дня. Удалите старую в «Мои автоматизации» или подключите Business Pro.",
                    reply_markup=_kb([[_b("💎 Тарифы", "t:menu")], [_b("📋 Мои автоматизации", "g:list")],
                                      [_b("⬅️ Меню", "g:menu")]]))
        if kind == "menu" and db.execute("SELECT COUNT(*) FROM ig_rules WHERE ig_id=? AND kind='menu'",
                                         (acc["ig_id"],)).fetchone()[0] >= 4:
            return await msg.answer("Instagram разрешает максимум 4 кнопки меню. Удалите одну в «Мои автоматизации».")
        ig_wait[uid] = {"step": "kw", "data": {"kind": kind}}
        if kind == "comment":
            try:
                r = await ig_call("GET", "/me/media", acc["token"], params={"fields": "id,caption", "limit": 8})
                posts = r.get("data") or []
            except IGError:
                posts = []
            rows = [[_b("📌 Все посты и рилсы", "g:p:all")]] if pro else []
            for p in posts:
                cap = (p.get("caption") or "без подписи").replace("\n", " ")[:35]
                rows.append([_b("📷 " + cap, f"g:p:{p['id']}")])
            ask = "Под каким постом ловим комментарии?" + ("" if pro else " (на Free — один пост)")
            return await edit_or_answer(msg, ask, reply_markup=_kb(rows + [[_b("⬅️ Меню", "g:menu")]]))
        if kind == "story_mention":
            ig_wait[uid] = {"step": "msg", "data": {"kind": kind, "kw": "*"}}
            return await msg.answer("Кто упомянет ваш аккаунт в сторис — получит сообщение в Direct. Напишите его текст (до 640 символов).")
        if kind == "story_reply":
            return await msg.answer("На какое слово в ответе на сторис реагируем? Можно несколько через запятую.\n«*» — на любой ответ.")
        if kind == "menu":
            return await msg.answer("Название кнопки меню (до 80 символов), например: 💰 Цены")
        return await msg.answer("Какое слово ждём в Direct? Можно несколько через запятую.\n«*» — на любое сообщение.")
    if act == "p":
        w = ig_wait.get(uid)
        if not w:
            return
        if parts[2] == "all" and not pro:
            return await ig_paywall(msg, "Все посты сразу")
        w["data"]["media"] = "" if parts[2] == "all" else parts[2]
        return await msg.answer("Какое слово ждём в комментариях? Например: ХОЧУ\nМожно несколько через запятую. «*» — любой комментарий.")
    if act == "f":
        w = ig_wait.get(uid)
        if not w:
            return
        w["data"]["follow"] = int(parts[2])
        if ig_products(acc):
            return await msg.answer("Добавить к сообщению каталог товаров (карусель)?", reply_markup=_kb([
                [_b("🛍 Да, с каталогом", "g:c:1")], [_b("Нет", "g:c:0")]]))
        return await ig_finish_rule(msg, uid)
    if act == "c":
        w = ig_wait.get(uid)
        if not w:
            return
        w["data"]["catalog"] = int(parts[2])
        return await ig_finish_rule(msg, uid)

    # --- каталог товаров ---
    if act == "cat":
        prods = ig_products(acc)
        txt = "🛍 Каталог товаров (до 10)\n\n" + (
            "\n".join(f"{i}. {p['title']} — {p['subtitle']}" for i, p in enumerate(prods, 1)) if prods else "Пока пусто.")
        txt += "\n\nКаталог можно приложить к любой автоматизации — он придёт каруселью."
        if not WEBAPP_URL:
            txt += "\n⚠️ Не задан DOMAIN — фото в карточках не покажутся."
        rows = [[_b("➕ Добавить товар", "g:pa")]] + [[_b("🗑 " + p["title"][:30], f"g:pd:{p['id']}")] for p in prods]
        return await edit_or_answer(msg, txt, reply_markup=_kb(rows + [[_b("⬅️ Меню", "g:menu")]]))
    if act == "pa":
        if len(ig_products(acc)) >= 10:
            return await msg.answer("Максимум 10 товаров в карусели Instagram. Удалите один.")
        ig_wait[uid] = {"step": "p_title", "data": {}}
        return await msg.answer("Название товара (до 80 символов):")
    if act == "pd":
        p = db.execute("SELECT * FROM ig_products WHERE id=? AND ig_id=?", (int(parts[2]), acc["ig_id"])).fetchone()
        if p:
            dbx("DELETE FROM ig_products WHERE id=?", (p["id"],))
            dbx("DELETE FROM ig_links WHERE id=?", (p["link_id"],))
            if p["img"]:
                try:
                    (IG_IMG_DIR / p["img"]).unlink()
                except OSError:
                    pass
        call.data = "g:cat"
        return await ig_cb(call)

    # --- список и карточка ---
    if act == "list":
        rules = db.execute("SELECT * FROM ig_rules WHERE ig_id=? ORDER BY id DESC LIMIT 30", (acc["ig_id"],)).fetchall()
        if not rules:
            return await edit_or_answer(msg, "Пока пусто. Создайте первую автоматизацию 👇", reply_markup=ig_menu_kb(uid))
        rows = [[_b(ig_rule_title(r), f"g:r:{r['id']}")] for r in rules] + [[_b("⬅️ Меню", "g:menu")]]
        return await edit_or_answer(msg, "📋 Ваши автоматизации:", reply_markup=_kb(rows))
    if act in ("r", "t", "d", "d2"):
        r = db.execute("SELECT * FROM ig_rules WHERE id=? AND ig_id=?", (int(parts[2]), acc["ig_id"])).fetchone()
        if not r:
            return await ig_show_menu(msg, uid, edit=True)
        if act == "t":
            dbx("UPDATE ig_rules SET active=1-active WHERE id=?", (r["id"],))
            r = db.execute("SELECT * FROM ig_rules WHERE id=?", (r["id"],)).fetchone()
            if r["kind"] == "menu":
                err = await ig_sync_menu(acc)
                if err:
                    await msg.answer(f"⚠️ Меню в Instagram не обновилось: {err[:150]}")
        if act == "d":
            return await edit_or_answer(msg, "Удалить эту автоматизацию?", reply_markup=_kb([
                [_b("Да, удалить", f"g:d2:{r['id']}")], [_b("Отмена", f"g:r:{r['id']}")]]))
        if act == "d2":
            dbx("DELETE FROM ig_rules WHERE id=?", (r["id"],))
            dbx("DELETE FROM ig_links WHERE rule_id=?", (r["id"],))
            if r["kind"] == "menu":
                err = await ig_sync_menu(acc)
                if err:
                    await msg.answer(f"⚠️ Меню в Instagram не обновилось: {err[:150]}")
            call.data = "g:list"
            return await ig_cb(call)
        return await edit_or_answer(msg, ig_rule_card(r), reply_markup=_kb([
            [_b("⏯ Вкл/выкл", f"g:t:{r['id']}"), _b("🗑 Удалить", f"g:d:{r['id']}")],
            [_b("⬅️ К списку", "g:list")]]))

    # --- автоответчик в Direct ---
    if act in ("dm", "w", "df"):
        if act == "w" and parts[2] == "tog":
            dbx("UPDATE ig_accounts SET welcome_on=1-welcome_on WHERE ig_id=?", (acc["ig_id"],))
        elif act == "w" and parts[2] == "edit":
            ig_wait[uid] = {"step": "welcome"}
            return await msg.answer("Пришлите текст приветствия — его получит человек, который пишет впервые.")
        elif act == "df" and parts[2] == "tog":
            dbx("UPDATE ig_accounts SET dflt_on=1-dflt_on WHERE ig_id=?", (acc["ig_id"],))
        elif act == "df" and parts[2] == "edit":
            ig_wait[uid] = {"step": "dflt"}
            return await msg.answer("Пришлите текст ответа по умолчанию — для сообщений, где нет ключевых слов (не чаще раза в 12 часов).")
        acc = ig_acc_of(uid)
        txt = (f"💬 Автоответчик в Direct\n\n"
               f"👋 Приветствие ({'вкл ✅' if acc['welcome_on'] else 'выкл ⏸'}):\n{acc['welcome'] or '— не задано —'}\n\n"
               f"↩️ Ответ по умолчанию ({'вкл ✅' if acc['dflt_on'] else 'выкл ⏸'}):\n{acc['dflt'] or '— не задано —'}\n\n"
               "Ответы по ключевым словам создаются в «Новая автоматизация».")
        return await edit_or_answer(msg, txt, reply_markup=_kb([
            [_b("✏️ Приветствие", "g:w:edit"), _b("⏯ Вкл/выкл", "g:w:tog")],
            [_b("✏️ Ответ по умолчанию", "g:df:edit"), _b("⏯ Вкл/выкл", "g:df:tog")],
            [_b("⬅️ Меню", "g:menu")]]))


# ====================== тарифы и оплата ======================
adm_wait: dict[int, str] = {}  # админ вводит причину отказа: id админа -> id платежа
pay_busy: set[str] = set()  # платежи, которые прямо сейчас обрабатываются админом
PAY_REASONS = [
    "Деньги не поступили",
    "Неверная сумма",
    "Чек нечитаемый или не тот",
    "Этот чек уже использован",
    "✏️ Другая причина",
]
PRO_FEATURES = (
    "• Ответ в комментариях + сообщения в Direct с кнопками\n"
    "• «Сначала подпишись», автоответчик, сторис, меню чата, каталог\n"
    f"• Статистика, динамика подписчиков, до {PRO_MAX_ACCOUNTS} аккаунтов"
)


def support_kb() -> InlineKeyboardMarkup:
    return _kb([[_b("💬 Поддержка " + SUPPORT_USER, url=SUPPORT_URL)]])


def pay_cancel_kb(pid: str) -> InlineKeyboardMarkup:
    return _kb([[_b("❌ Отмена", f"t:cancel:{pid}")]])


def pay_admin_kb(pid: str) -> InlineKeyboardMarkup:
    return _kb([[_b("✅ Подтвердить", f"pa:ok:{pid}"), _b("❌ Отклонить", f"pa:no:{pid}")]])


async def ig_paywall(msg: Message, what: str):
    return await edit_or_answer(
        msg, f"🔒 {what} — доступно на тарифе Business Pro.\n\nПодключите Business Pro и пользуйтесь всеми функциями.",
        reply_markup=_kb([[_b("💎 Тарифы и оплата", "t:menu")], [_b("⬅️ Меню", "g:menu")]]))


def plans_text(uid: int) -> str:
    sub = sub_cached(uid)
    pi, pm = PLANS["intro"], PLANS["month"]
    lines = [
        "💎 Тарифы", "",
        "🆓 Free — бесплатно",
        "• 1 аккаунт Instagram",
        "• Только ответ в комментариях",
        f"• 1 пост, {FREE_DAYS} дня", "",
        "💎 Business Pro — все функции",
        PRO_FEATURES,
    ]
    if intro_available(sub):
        lines.append(f"🔥 Для первых пользователей: {pi['label']} — {pi['price']} TJS")
    lines.append(f"💰 {pm['label']} — {pm['price']} TJS")
    lines += ["", plan_line(sub)]
    return "\n".join(lines)


def plans_kb(uid: int) -> InlineKeyboardMarkup:
    sub = sub_cached(uid)
    rows = [[_b("🆓 Free", "t:sel:free")]]
    if intro_available(sub):
        p = PLANS["intro"]
        rows.append([_b(f"🔥 Business Pro · {p['label']} · {p['price']} TJS", "t:sel:intro")])
    p = PLANS["month"]
    rows.append([_b(f"💎 Business Pro · {p['label']} · {p['price']} TJS", "t:sel:month")])
    rows.append([_b("⬅️ Меню", "g:menu")])
    return _kb(rows)


@dp2.message(Command("tariff"))
async def tariff_cmd(message: Message):
    uid = message.from_user.id
    ig_wait.pop(uid, None)
    await get_sub(uid)
    await message.answer(plans_text(uid), reply_markup=plans_kb(uid))


@dp2.message(Command("grant"))
async def grant_cmd(message: Message, command: CommandObject):
    """Админ: /grant ID дни — выдать Business Pro вручную (0 — отключить)."""
    if message.from_user.id not in ADMIN_IDS:
        return
    a = (command.args or "").split()
    if len(a) != 2 or not a[0].isdigit() or not a[1].isdigit():
        return await message.answer("Формат: /grant ID_пользователя дни\nПример: /grant 123456789 30\n0 дней — отключить Pro.")
    uid, days = int(a[0]), int(a[1])
    sub = await get_sub(uid, fresh=True)
    now = int(time.time())
    if days == 0:
        await sub_update(uid, plan="free", until=0)
        await client_update(uid, plan="free", until=0)
        return await message.answer("✅ Business Pro отключён.")
    base = max(now, int(sub.get("until") or 0)) if tier_of(sub) == "pro" else now
    until = base + days * 86400
    await sub_update(uid, plan="pro", until=until, intro_used=True, pending=None)
    await client_update(uid, plan="pro", until=until)
    await message.answer(f"✅ Business Pro выдан до {fdate(until)}")
    try:
        await BOT2.send_message(uid, f"🎁 Вам подключён Business Pro до {fdate(until)}", reply_markup=ig_menu_kb(uid))
    except Exception:
        pass


@dp2.callback_query(F.data.startswith("t:"))
async def tariff_cb(call: CallbackQuery):
    uid = call.from_user.id
    parts = call.data.split(":")
    act = parts[1]
    msg = call.message
    sub = await get_sub(uid)
    await call.answer()
    if act == "menu":
        return await edit_or_answer(msg, plans_text(uid), reply_markup=plans_kb(uid))
    if act == "sel":
        key = parts[2]
        if key == "free":
            return await edit_or_answer(
                msg, f"🆓 Тариф Free\n\n• 1 аккаунт Instagram\n• Только ответ в комментариях\n• 1 пост, {FREE_DAYS} дня\n\n"
                     "Оплата не нужна. Больше возможностей — на Business Pro.",
                reply_markup=_kb([[_b("⬅️ Тарифы", "t:menu")], [_b("📋 Меню", "g:menu")]]))
        plan = PLANS.get(key)
        if not plan or (key == "intro" and not intro_available(sub)):
            return await edit_or_answer(msg, "Это предложение недоступно.", reply_markup=plans_kb(uid))
        text = (f"💎 {plan['title']} — {plan['label']}\n💰 Сумма: {plan['price']} TJS\n\n"
                f"Все функции:\n{PRO_FEATURES}")
        if tier_of(sub) == "pro":
            text += f"\n\nСейчас активен до {fdate(sub['until'])} — оплата продлит срок."
        return await edit_or_answer(msg, text, reply_markup=_kb([
            [_b("💳 Оплатить", f"t:pay:{key}")], [_b("⬅️ Назад", "t:menu")]]))
    if act == "pay":
        return await pay_start(call, uid, parts[2], sub)
    if act == "cancel":
        pay = await store_get(f"payments/{parts[2]}")
        if isinstance(pay, dict) and pay.get("uid") == uid and pay.get("status") == "awaiting":
            await pay_cancel_do(pay)
        return await edit_or_answer(msg, "❌ Запрос на оплату отменён.", reply_markup=_kb([
            [_b("💎 Тарифы", "t:menu")], [_b("📋 Меню", "g:menu")]]))


async def pay_start(call: CallbackQuery, uid: int, key: str, sub: dict):
    msg = call.message
    plan = PLANS.get(key)
    if not plan or (key == "intro" and not intro_available(sub)):
        return await edit_or_answer(msg, "Это предложение недоступно.", reply_markup=plans_kb(uid))
    if not ADMIN_IDS:
        return await edit_or_answer(msg, f"Оплата временно недоступна. Напишите в поддержку {SUPPORT_USER}.",
                                    reply_markup=support_kb())
    ig_wait.pop(uid, None)
    old_id = sub.get("pending")
    if old_id:
        old = await store_get(f"payments/{old_id}")
        if isinstance(old, dict):
            if old.get("status") == "review":
                return await edit_or_answer(
                    msg, "⏳ Ваш предыдущий платёж ещё проверяется. Подождите до 5 минут.\n\n"
                         f"Если тариф не подключится — напишите в службу поддержки {SUPPORT_USER}",
                    reply_markup=support_kb())
            if old.get("status") == "awaiting":
                await store_update(f"payments/{old_id}", {"status": "cancelled"})
    pid = uuid.uuid4().hex[:10]
    text = (f"💳 Оплата тарифа\n\n📦 Тариф: {plan['title']}\n⏱ Срок: {plan['label']}\n"
            f"💰 Сумма: {plan['price']} TJS\n\n"
            "Переведите сумму по номеру телефона:\n"
            f"• DC City: {PAY_DC}\n• Alif Mobi: {PAY_ALIF}\n\n"
            "📸 После оплаты отправьте чек (скриншот) сюда, в этот чат.")
    m = await edit_or_answer(msg, text, reply_markup=pay_cancel_kb(pid))
    u = call.from_user
    await store_set(f"payments/{pid}", {
        "id": pid, "uid": uid, "plan": key, "days": plan["days"], "amount": plan["price"],
        "status": "awaiting", "created": int(time.time()), "name": u.full_name, "username": u.username or "",
        "chat": m.chat.id, "msg_id": m.message_id,
    })
    await sub_update(uid, pending=pid)


async def pay_awaiting(uid: int):
    """Платёж, по которому бот ждёт чек от этого пользователя."""
    pid = (await get_sub(uid)).get("pending")
    if not pid:
        return None
    p = await store_get(f"payments/{pid}")
    if isinstance(p, dict) and p.get("status") == "awaiting" and p.get("uid") == uid:
        return p
    return None


async def pay_cancel_do(pay: dict):
    await store_update(f"payments/{pay['id']}", {"status": "cancelled"})
    await sub_update(pay["uid"], pending=None)


async def pay_receipt(message: Message, pay: dict):
    """Пользователь прислал чек: чистим старые сообщения, говорим «ждите», отправляем чек админам."""
    uid = message.from_user.id
    if message.photo:
        kind, fid = "photo", message.photo[-1].file_id
    else:
        kind, fid = "document", message.document.file_id
    if pay.get("msg_id"):  # старое сообщение с реквизитами
        try:
            await BOT2.delete_message(pay.get("chat") or uid, pay["msg_id"])
        except Exception:
            pass
    wait = await message.answer(
        "⏳ Ваш платёж обрабатывается, пожалуйста, подождите до 5 минут.\n\n"
        f"Если тариф не подключится — напишите в службу поддержки {SUPPORT_USER}",
        reply_markup=support_kb())
    plan = PLANS.get(pay["plan"], {})
    acc = ig_acc_of(uid)
    card = (f"💳 Новый платёж #{pay['id']}\n\n"
            f"👤 {pay.get('name') or '—'}" + (f" (@{pay['username']})" if pay.get("username") else "") + f"\n🆔 {uid}\n"
            f"📦 Business Pro · {plan.get('label', '')}\n💰 {pay['amount']} TJS\n"
            "📱 Instagram: " + (f"@{acc['username']}" if acc else "не подключён") +
            "\n🕐 " + time.strftime("%d.%m %H:%M", time.gmtime(time.time() + TZ_OFFSET)))
    adm: dict[str, int] = {}
    for aid in ADMIN_IDS:
        try:
            if kind == "photo":
                m = await BOT2.send_photo(aid, fid, caption=card, reply_markup=pay_admin_kb(pay["id"]))
            else:
                m = await BOT2.send_document(aid, fid, caption=card, reply_markup=pay_admin_kb(pay["id"]))
            adm[str(aid)] = m.message_id
        except Exception:
            logging.warning("Не удалось отправить чек админу %s", aid)
    if not adm:
        try:
            await wait.delete()
        except Exception:
            pass
        return await message.answer(
            f"❌ Не удалось передать чек. Отправьте его ещё раз или напишите в поддержку {SUPPORT_USER}.",
            reply_markup=support_kb())
    await store_update(f"payments/{pay['id']}", {
        "status": "review", "file_id": fid, "kind": kind, "card": card, "adm": adm,
        "wait_msg": wait.message_id, "wait_chat": wait.chat.id, "submitted": int(time.time())})


@dp2.message(F.document)
async def ig_doc(message: Message):
    pay = await pay_awaiting(message.from_user.id)
    mime = (message.document.mime_type or "")
    if pay and (mime.startswith("image/") or mime == "application/pdf"):
        return await pay_receipt(message, pay)
    await ig_show_menu(message, message.from_user.id)


async def pay_mark_cards(pay: dict, status: str):
    """Убирает кнопки у карточек платежа у всех админов и дописывает итог."""
    for aid, mid in (pay.get("adm") or {}).items():
        try:
            await BOT2.edit_message_caption(chat_id=int(aid), message_id=int(mid),
                                            caption=(pay.get("card") or "")[:900] + "\n\n" + status,
                                            reply_markup=None)
        except Exception:
            logging.warning("Не удалось обновить карточку платежа у админа %s", aid)


async def pay_clear_wait(pay: dict):
    if pay.get("wait_msg"):
        try:
            await BOT2.delete_message(int(pay.get("wait_chat") or pay["uid"]), int(pay["wait_msg"]))
        except Exception:
            pass


async def pay_approve(pay: dict, admin):
    uid = pay["uid"]
    sub = await get_sub(uid, fresh=True)
    now = int(time.time())
    base = max(now, int(sub.get("until") or 0)) if tier_of(sub) == "pro" else now
    until = base + int(pay["days"]) * 86400
    await store_update(f"payments/{pay['id']}", {"status": "approved", "decided": now, "admin": admin.id})
    await sub_update(uid, plan="pro", until=until, intro_used=True, pending=None, last_pay=pay["id"],
                     rem_for=None, exp_for=None)
    await client_update(uid, plan="pro", until=until)
    await pay_mark_cards(pay, f"✅ Подтверждён ({admin.full_name}) · до {fdate(until)}")
    await pay_clear_wait(pay)
    try:
        await BOT2.send_message(
            uid, f"✅ Оплата подтверждена!\n\n💎 Business Pro активен до {fdate(until)}.\nВсе функции доступны.",
            reply_markup=ig_menu_kb(uid))
    except Exception:
        logging.warning("Не удалось написать пользователю %s об оплате", uid)


async def pay_reject(pay: dict, reason: str, admin):
    uid = pay["uid"]
    await store_update(f"payments/{pay['id']}", {"status": "rejected", "reason": reason,
                                                 "decided": int(time.time()), "admin": admin.id})
    await sub_update(uid, pending=None)
    await pay_mark_cards(pay, f"❌ Отклонён ({admin.full_name})\nПричина: {reason}")
    await pay_clear_wait(pay)
    try:
        await BOT2.send_message(
            uid, f"❌ Платёж отклонён\n\nПричина: {reason}\n\nЕсли вы оплатили — отправьте чек снова "
                 f"через «Тарифы» или напишите в службу поддержки {SUPPORT_USER}",
            reply_markup=_kb([[_b("💎 Тарифы", "t:menu")], [_b("💬 Поддержка " + SUPPORT_USER, url=SUPPORT_URL)]]))
    except Exception:
        logging.warning("Не удалось написать пользователю %s об отказе", uid)


@dp2.callback_query(F.data.startswith("pa:"))
async def pay_admin_cb(call: CallbackQuery):
    admin = call.from_user
    if admin.id not in ADMIN_IDS:
        return await call.answer()
    parts = call.data.split(":")
    act, pid = parts[1], parts[2]
    pay = await store_get(f"payments/{pid}")
    if not isinstance(pay, dict) or pay.get("status") != "review" or pid in pay_busy:
        await call.answer("Этот платёж уже обработан", show_alert=True)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if act == "no":  # выбор причины отказа
        await call.answer()
        rows = [[_b(r, f"pa:r:{pid}:{i}")] for i, r in enumerate(PAY_REASONS)] + [[_b("⬅️ Назад", f"pa:b:{pid}")]]
        return await call.message.edit_reply_markup(reply_markup=_kb(rows))
    if act == "b":
        await call.answer()
        return await call.message.edit_reply_markup(reply_markup=pay_admin_kb(pid))
    if act == "r" and int(parts[3]) == len(PAY_REASONS) - 1:  # «Другая причина» — админ пишет текстом
        await call.answer()
        adm_wait[admin.id] = pid
        return await call.message.answer("✏️ Напишите причину отказа одним сообщением (отмена: /cancel).")
    pay_busy.add(pid)
    try:
        await call.answer()
        if act == "ok":
            await pay_approve(pay, admin)
        elif act == "r":
            await pay_reject(pay, PAY_REASONS[int(parts[3])], admin)
    finally:
        pay_busy.discard(pid)


async def admin_reason_text(message: Message, text: str):
    pid = adm_wait.pop(message.from_user.id, None)
    pay = await store_get(f"payments/{pid}") if pid else None
    if not isinstance(pay, dict) or pay.get("status") != "review" or pid in pay_busy:
        return await message.answer("Этот платёж уже обработан.")
    pay_busy.add(pid)
    try:
        await pay_reject(pay, text[:300], message.from_user)
    finally:
        pay_busy.discard(pid)
    await message.answer("✅ Отказ отправлен пользователю.")


async def ig_user_note(uid: int, text: str):
    try:
        await BOT2.send_message(uid, text, reply_markup=_kb([[_b("💎 Тарифы", "t:menu")]]))
    except Exception:
        pass


async def sub_watch_once():
    """Напоминание за 2 дня до конца Business Pro и сообщение об окончании."""
    now = int(time.time())
    for k, s in (await store_all("subs")).items():
        if not isinstance(s, dict) or s.get("plan") != "pro" or not str(k).isdigit():
            continue
        uid, until = int(k), int(s.get("until") or 0)
        if until <= now and s.get("exp_for") != until:
            await sub_update(uid, exp_for=until)
            await ig_user_note(uid, "⏸ Тариф Business Pro закончился. Автоматизации Direct остановлены.\n"
                                    "Продлите тариф, чтобы вернуть все функции.")
        elif 0 < until - now < 2 * 86400 and s.get("rem_for") != until:
            await sub_update(uid, rem_for=until)
            await ig_user_note(uid, f"⏰ Business Pro заканчивается {fdate(until)}. Продлить можно в «Тарифах».")


async def sub_watch_loop():
    while True:
        try:
            await sub_watch_once()
        except Exception:
            logging.exception("sub watch")
        await asyncio.sleep(3600)


@dp2.message(F.text)
async def ig_text(message: Message):
    uid = message.from_user.id
    w = ig_wait.get(uid)
    text = (message.text or "").strip()
    sub = await get_sub(uid)
    pro = tier_of(sub) == "pro"
    if uid in adm_wait:
        return await admin_reason_text(message, text)
    if not w:
        pay = await pay_awaiting(uid)
        if pay:
            return await message.answer("📸 Отправьте фото чека (скриншот оплаты) в этот чат.",
                                        reply_markup=pay_cancel_kb(pay["id"]))
        return await ig_show_menu(message, uid)
    step = w["step"]
    acc = ig_acc_of(uid)
    if step == "token":
        ig_wait.pop(uid, None)
        try:
            await message.delete()  # токен лучше не оставлять в чате
        except Exception:
            pass
        try:
            acc, warn = await ig_connect(uid, text)
        except PlanLimit as e:
            return await message.answer("🔒 " + str(e), reply_markup=ig_menu_kb(uid))
        except Exception as e:
            return await message.answer(f"❌ Токен не подошёл: {str(e)[:150]}", reply_markup=ig_menu_kb(uid))
        return await message.answer(f"✅ Подключён @{acc['username']}\n" + (f"⚠️ {warn}" if warn else ""),
                                    reply_markup=ig_menu_kb(uid))
    if not acc:
        ig_wait.pop(uid, None)
        return await ig_show_menu(message, uid)
    if step in ("welcome", "dflt"):
        ig_wait.pop(uid, None)
        if step == "welcome":
            dbx("UPDATE ig_accounts SET welcome=?, welcome_on=1 WHERE ig_id=?", (text[:1000], acc["ig_id"]))
        else:
            dbx("UPDATE ig_accounts SET dflt=?, dflt_on=1 WHERE ig_id=?", (text[:1000], acc["ig_id"]))
        return await message.answer("✅ Сохранено и включено.", reply_markup=_kb([[_b("💬 Автоответчик", "g:dm")]]))
    d = w["data"]
    if step == "kw":
        d["kw"] = text[:80 if d["kind"] == "menu" else 100]
        if d["kind"] == "comment" and not pro:  # Free: только публичный ответ под комментарием
            d["msg"], d["buttons"] = "", []
            w["step"] = "pub"
            return await message.answer(
                "Что ответить публично под комментарием? Можно несколько вариантов — каждый с новой строки "
                "(бот выберет случайный).\n\nНапример:\nСпасибо! 🙏\nПишите, отвечу 📩")
        w["step"] = "msg"
        return await message.answer("Напишите сообщение, которое получит человек в Direct (до 640 символов).")
    if step == "msg":
        if len(text) > 640:
            return await message.answer(f"Слишком длинно ({len(text)}). Максимум 640 символов — сократите.")
        d["msg"] = text
        w["step"] = "btns"
        return await message.answer(
            "Добавьте кнопки-ссылки (до 3), каждая с новой строки:\nНазвание | ссылка\n\n"
            "Например:\nКупить | https://mysite.com/buy\n\nБез кнопок — отправьте «-»")
    if step == "btns":
        btns = ig_parse_buttons(text)
        if text != "-" and not btns:
            return await message.answer("Не нашёл ссылок. Формат: Название | https://сайт.com (или «-» без кнопок).")
        d["buttons"] = btns
        if d["kind"] == "comment":
            w["step"] = "pub"
            return await message.answer(
                "Что ответить публично под комментарием? Можно несколько вариантов — каждый с новой строки "
                "(бот выберет случайный).\n\nНапример:\nОтправил в Direct ✅\nПроверь личные сообщения 📩\n\nБез ответа — «-»")
        w["step"] = "follow"
        return await ig_ask_follow(message)
    if step == "pub":
        if not pro:
            if text == "-":
                return await message.answer("На Free нужен текст ответа под комментарием. Напишите его (отмена: /cancel).")
            d["pub"] = text[:600]
            return await ig_finish_rule(message, uid)
        d["pub"] = "" if text == "-" else text[:600]
        w["step"] = "follow"
        return await ig_ask_follow(message)
    # --- добавление товара ---
    if step == "p_title":
        d["title"] = text[:80]
        w["step"] = "p_sub"
        return await message.answer("Цена и короткое описание (до 80 символов), например: 120 сомони · размеры M–XL")
    if step == "p_sub":
        d["sub"] = text[:80]
        if WEBAPP_URL:
            w["step"] = "p_photo"
            return await message.answer("Пришлите фото товара (лучше квадратное) или «-» без фото.")
        d["img"] = ""
        w["step"] = "p_url"
        return await message.answer("Ссылка для кнопки (куда ведёт «Купить»):")
    if step == "p_photo":
        if text != "-":
            return await message.answer("Пришлите именно фото (как картинку) или «-» без фото.")
        d["img"] = ""
        w["step"] = "p_url"
        return await message.answer("Ссылка для кнопки (куда ведёт «Купить»):")
    if step == "p_url":
        found = ig_parse_buttons(text)
        if not found:
            return await message.answer("Не нашёл ссылку. Пример: https://mysite.com/product1")
        d["url"] = found[0]["u"]
        w["step"] = "p_btn"
        return await message.answer("Название кнопки (до 20 символов), например «Купить» или «Заказать». «-» — «Купить».")
    if step == "p_btn":
        ig_wait.pop(uid, None)
        lid = uuid.uuid4().hex[:8]
        cur = dbx("INSERT INTO ig_products(ig_id, title, subtitle, img, url, btn, link_id, created) VALUES(?,?,?,?,?,?,?,?)",
                  (acc["ig_id"], d["title"], d["sub"], d.get("img", ""), d["url"],
                   "Купить" if text == "-" else text[:20], lid, int(time.time())))
        dbx("INSERT INTO ig_links(id, url, ig_id, rule_id) VALUES(?,?,?,?)", (lid, d["url"], acc["ig_id"], -cur.lastrowid))
        return await message.answer("✅ Товар добавлен.", reply_markup=_kb([
            [_b("➕ Ещё товар", "g:pa")], [_b("🛍 Каталог", "g:cat")]]))
    await ig_show_menu(message, uid)


@dp2.message(F.photo)
async def ig_photo(message: Message):
    uid = message.from_user.id
    w = ig_wait.get(uid)
    pay = await pay_awaiting(uid)
    if pay and not (w and w.get("step") == "p_photo"):
        return await pay_receipt(message, pay)  # фото чека
    if not w or w.get("step") != "p_photo":
        return await ig_show_menu(message, uid)
    IG_IMG_DIR.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex[:12] + ".jpg"
    await BOT2.download(message.photo[-1], destination=IG_IMG_DIR / name)
    w["data"]["img"] = name
    w["step"] = "p_url"
    await message.answer("Ссылка для кнопки (куда ведёт «Купить»):")


async def ig_setup_commands(bot: Bot):
    cmds = [
        BotCommand(command="start", description="🚀 Начать"),
        BotCommand(command="menu", description="📋 Меню"),
        BotCommand(command="tariff", description="💎 Тарифы и оплата"),
        BotCommand(command="cancel", description="✖️ Отмена"),
    ]
    try:
        await bot.set_my_commands(cmds)
    except Exception:
        pass
    for aid in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                cmds + [BotCommand(command="grant", description="🎁 Выдать Pro: /grant ID дни")],
                scope=BotCommandScopeChat(chat_id=aid))
        except Exception:
            pass


# ====================== запуск ======================
async def cleanup_loop():
    """Удаляет файлы, на вопрос «да/нет» по которым не ответили 15 минут."""
    while True:
        await asyncio.sleep(300)
        now = time.time()
        for k, v in list(held.items()):
            if now - v["time"] > 900:
                held.pop(k, None)
                shutil.rmtree(v["tmp"], ignore_errors=True)


async def setup_commands(bot: Bot):
    ru = [
        BotCommand(command="start", description="🚀 Начать"),
        BotCommand(command="lang", description="🌐 Сменить язык / Иваз кардани забон"),
        BotCommand(command="menu", description="📋 Кнопки"),
        BotCommand(command="stories", description="📸 Сторис Instagram"),
        BotCommand(command="auto", description="⚡ Авто-режим формата"),
        BotCommand(command="invite", description="👥 Пригласить друзей"),
    ]
    tg = [
        BotCommand(command="start", description="🚀 Оғоз"),
        BotCommand(command="lang", description="🌐 Иваз кардани забон / Сменить язык"),
        BotCommand(command="menu", description="📋 Тугмаҳо"),
        BotCommand(command="stories", description="📸 Сторисҳои Instagram"),
        BotCommand(command="auto", description="⚡ Реҷаи худкори формат"),
        BotCommand(command="invite", description="👥 Даъвати дӯстон"),
    ]
    await bot.set_my_commands(ru)
    await bot.set_my_commands(tg, language_code="tg")
    admin_cmds = ru + [
        BotCommand(command="stats", description="📊 Статистика"),
        BotCommand(command="channels", description="📢 Каналы подписки"),
        BotCommand(command="broadcast", description="📣 Рассылка"),
        BotCommand(command="update", description="🔄 Обновить yt-dlp"),
        BotCommand(command="id", description="🆔 Мой ID"),
    ]
    for aid in ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=aid))
        except Exception:
            logging.warning("Не удалось задать меню команд для админа %s", aid)


async def main():
    global BOT_USERNAME
    if not TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")
    logging.info("Версия кода: %s, машина: %s", BOT_VERSION, socket.gethostname())
    bot = Bot(TOKEN)
    BOT_USERNAME = (await bot.get_me()).username
    await setup_commands(bot)
    if not list_channels():  # первый запуск: добавляем канал по умолчанию
        try:
            info = await bot.get_chat(CHANNEL)
            add_channel(info.id, info.title or CHANNEL, CHANNEL_URL)
        except Exception:
            logging.warning("Не удалось добавить канал по умолчанию — добавьте через /channels")
            await notify_admins(
                bot, f"⚠️ Не удалось добавить канал {CHANNEL}. Добавьте его вручную: /channels"
            )
    bad = []
    for c in list_channels():
        ok, err = await bot_admin_status(bot, c["chat_id"])
        if not ok:
            bad.append(f"• {c['title']}: {err}")
    if bad:
        await notify_admins(
            bot,
            "⚠️ Бот запущен, но подписка на эти каналы не проверяется (бот не админ):\n"
            + "\n".join(bad)
            + "\n\nДобавьте бота в администраторы этих каналов. Проверка: /channels",
        )
    cleanup = asyncio.create_task(cleanup_loop())
    bg_tasks.add(cleanup)
    global BOT2
    if TOKEN2 and TOKEN2 == TOKEN:
        logging.warning("BOT2_TOKEN совпадает с BOT_TOKEN — второй бот не запущен, нужен отдельный токен")
    elif TOKEN2:
        BOT2 = Bot(TOKEN2)
        await ig_setup_commands(BOT2)
        ig_task = asyncio.create_task(ig_daily_loop())
        bg_tasks.add(ig_task)
        bg(sub_watch_loop())
        logging.info("Бот №2 (Instagram): @%s", (await BOT2.get_me()).username)
    await start_web_server(bot, BOT2)
    await bot.delete_webhook(drop_pending_updates=True)
    if BOT2:
        await BOT2.delete_webhook(drop_pending_updates=True)
        await asyncio.gather(
            dp.start_polling(bot),
            dp2.start_polling(BOT2, handle_signals=False),
        )
    else:
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
