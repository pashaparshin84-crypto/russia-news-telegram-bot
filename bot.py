import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import feedparser
from dotenv import load_dotenv

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "40"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
CHECK_INTERVAL = int(os.getenv("NEWS_CHECK_INTERVAL", "1800"))
NEWS_PER_DIGEST = int(os.getenv("NEWS_PER_DIGEST", "5"))
DB_FILE = os.getenv("SUBSCRIBERS_DB", "subscribers.db")

if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN в файле .env")


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("russia-news-bot")


RUSSIA_SOURCES = [
    ("РИА Новости", "https://ria.ru/export/rss2/index.xml"),
    ("ТАСС", "https://tass.ru/rss/v2.xml"),
    ("РБК", "https://rssexport.rbc.ru/rbcnews/news/30/full.rss"),
    ("Российская газета", "https://rg.ru/xml/index.xml"),
    ("МК", "https://www.mk.ru/rss/index.xml"),
    ("Коммерсантъ", "https://www.kommersant.ru/RSS/main.xml"),
    ("Интерфакс", "https://www.interfax.ru/rss.asp"),
]


def init_db() -> None:
    with sqlite3.connect(DB_FILE) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_news (
                link TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            )
            """
        )
        db.commit()


def add_subscriber(user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    with sqlite3.connect(DB_FILE) as db:
        db.execute(
            """
            INSERT INTO subscribers(user_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
            """,
            (
                user_id,
                username or "",
                first_name or "",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.commit()


def remove_subscriber(user_id: int) -> None:
    with sqlite3.connect(DB_FILE) as db:
        db.execute("DELETE FROM subscribers WHERE user_id = ?", (user_id,))
        db.commit()


def get_subscribers() -> list[int]:
    with sqlite3.connect(DB_FILE) as db:
        rows = db.execute("SELECT user_id FROM subscribers").fetchall()
    return [int(row[0]) for row in rows]


def is_subscriber(user_id: int) -> bool:
    with sqlite3.connect(DB_FILE) as db:
        row = db.execute(
            "SELECT 1 FROM subscribers WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row is not None


def mark_news_sent(link: str) -> None:
    with sqlite3.connect(DB_FILE) as db:
        db.execute(
            "INSERT OR IGNORE INTO sent_news(link, sent_at) VALUES (?, ?)",
            (link, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()


def was_news_sent(link: str) -> bool:
    with sqlite3.connect(DB_FILE) as db:
        row = db.execute(
            "SELECT 1 FROM sent_news WHERE link = ?",
            (link,),
        ).fetchone()
    return row is not None


def clean_text(value: str) -> str:
    value = value or ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def get_entry_image(entry) -> Optional[str]:
    """Возвращает URL изображения, если RSS-лента его предоставляет."""
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key, [])
        if isinstance(media, list):
            for item in media:
                url = item.get("url")
                if url and re.match(r"^https?://", url, re.I):
                    mime = (item.get("type") or "").lower()
                    if not mime or mime.startswith("image/"):
                        return url

    for item in entry.get("enclosures", []):
        url = item.get("href") or item.get("url")
        mime = (item.get("type") or "").lower()
        if url and re.match(r"^https?://", url, re.I):
            if mime.startswith("image/") or re.search(
                r"\.(jpg|jpeg|png|webp)(\?|$)", url, re.I
            ):
                return url

    for field in ("summary", "description"):
        value = entry.get(field, "")
        match = re.search(
            r'<img[^>]+(?:src|url)=["\'](https?://[^"\']+)["\']',
            str(value),
            re.I,
        )
        if match:
            return match.group(1)

    return None


def get_entry_video(entry) -> Optional[str]:
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key, [])
        if isinstance(media, list):
            for item in media:
                url = item.get("url")
                if url and re.search(r"\.(mp4|m3u8)(\?|$)", url, re.I):
                    return url

    for item in entry.get("enclosures", []):
        url = item.get("href") or item.get("url")
        mime = (item.get("type") or "").lower()
        if url and ("video" in mime or re.search(r"\.(mp4|m3u8)(\?|$)", url, re.I)):
            return url

    return None


def parse_feed(content: bytes, source: str) -> list[dict]:
    feed = feedparser.parse(content)
    result = []

    for entry in feed.entries:
        title = clean_text(entry.get("title", ""))
        link = entry.get("link", "")
        if not title or not link:
            continue

        summary = clean_text(
            entry.get("summary")
            or entry.get("description")
            or ""
        )

        published = clean_text(
            entry.get("published")
            or entry.get("updated")
            or ""
        )

        result.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "source": source,
                "published": published,
                "image_url": get_entry_image(entry),
                "video_url": get_entry_video(entry),
            }
        )

    return result


async def fetch_feed(
    session: aiohttp.ClientSession,
    source: str,
    url: str,
) -> list[dict]:
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            headers={"User-Agent": "Mozilla/5.0 RussiaNewsBot/1.0"},
        ) as response:
            if response.status != 200:
                logger.warning("%s: HTTP %s", source, response.status)
                return []

            return parse_feed(await response.read(), source)

    except Exception as exc:
        logger.warning("Ошибка RSS %s: %s", source, exc)
        return []


async def get_russia_news() -> list[dict]:
    connector = aiohttp.TCPConnector(limit=10, ssl=False)

    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(
            *[
                fetch_feed(session, source, url)
                for source, url in RUSSIA_SOURCES
            ]
        )

    news = []
    seen = set()

    for items in results:
        for item in items:
            link = item["link"].strip().lower()
            if not link or link in seen:
                continue
            seen.add(link)
            news.append(item)

    return news[:MAX_ITEMS]


def news_button(item: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "🔗 Читать оригинал",
                url=item["link"],
            )
        ]]
    )


def format_news(item: dict, number: int) -> str:
    title = html.escape(item["title"])
    source = html.escape(item["source"])

    text = f"<b>{number}. {title}</b>\n"
    text += f"📰 <b>Источник:</b> {source}\n"

    if item.get("published"):
        text += f"🕒 <b>Дата:</b> {html.escape(item['published'])}\n"

    summary = item.get("summary", "").strip()

    if summary:
        summary = summary[:1000]
        if len(item.get("summary", "")) > 1000:
            summary += "…"
        text += f"\n<b>Кратко:</b>\n{html.escape(summary)}\n"
    else:
        text += "\n<i>Описание отсутствует в RSS-ленте.</i>\n"

    return text


async def send_digest(bot, items: list[dict]) -> None:
    """Отправляет подборку: фотографии — альбомом, остальные новости — текстом."""
    subscribers = get_subscribers()

    if not subscribers or not items:
        return

    for user_id in subscribers:
        try:
            media_items = []
            text_items = []

            for number, item in enumerate(items, 1):
                body = format_news(item, number)
                body += (
                    f'\n🔗 <a href="{html.escape(item["link"])}">'
                    "Читать оригинал</a>"
                )

                if item.get("image_url"):
                    media_items.append(
                        InputMediaPhoto(
                            item["image_url"],
                            caption=body,
                            parse_mode="HTML",
                        )
                    )
                else:
                    text_items.append(body)

            if media_items:
                try:
                    await bot.send_media_group(
                        chat_id=user_id,
                        media=media_items,
                    )
                except Exception as exc:
                    logger.warning(
                        "Не удалось отправить фотоальбом пользователю %s: %s",
                        user_id,
                        exc,
                    )
                    for media in media_items:
                        await bot.send_message(
                            chat_id=user_id,
                            text=media.caption or "",
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )

            for body in text_items:
                await bot.send_message(
                    chat_id=user_id,
                    text=body,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

        except Exception as exc:
            error_text = str(exc).lower()

            if (
                "bot was blocked" in error_text
                or "chat not found" in error_text
                or "user is deactivated" in error_text
            ):
                remove_subscriber(user_id)
                logger.info("Удалён недоступный подписчик: %s", user_id)
            else:
                logger.warning(
                    "Не удалось отправить рассылку пользователю %s: %s",
                    user_id,
                    exc,
                )

        await asyncio.sleep(0.05)


async def news_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        news = await get_russia_news()

        new_items = [
            item for item in news
            if not was_news_sent(item["link"])
        ]

        if not new_items:
            logger.info("Новых новостей нет")
            return

        # В одну рассылку попадают только самые свежие материалы.
        new_items = new_items[:NEWS_PER_DIGEST]

        await send_digest(context.bot, new_items)

        for item in new_items:
            mark_news_sent(item["link"])

        logger.info(
            "Рассылка завершена: %s новостей, подписчиков: %s",
            len(new_items),
            len(get_subscribers()),
        )

    except Exception:
        logger.exception("Ошибка автоматической рассылки")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    add_subscriber(
        user.id,
        user.username,
        user.first_name,
    )

    subscribed = is_subscriber(user.id)

    text = (
        "👋 <b>Добро пожаловать в Russia News!</b>\n\n"
        "Здесь только 🇷🇺 новости России.\n\n"
        "Бот собирает новости из нескольких российских RSS-источников "
        "и может автоматически присылать подборку главных новостей.\n\n"
        "🔔 Сейчас вы подписаны на автоматическую рассылку."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇷🇺 Последние новости", callback_data="latest"),
        ],
        [
            InlineKeyboardButton(
                "🔕 Отписаться",
                callback_data="unsubscribe",
            )
        ],
        [
            InlineKeyboardButton("📰 Источники", callback_data="sources"),
            InlineKeyboardButton("❓ Помощь", callback_data="help"),
        ],
    ])

    if not subscribed:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🇷🇺 Последние новости", callback_data="latest"),
            ],
            [
                InlineKeyboardButton(
                    "🔔 Подписаться",
                    callback_data="subscribe",
                )
            ],
            [
                InlineKeyboardButton("📰 Источники", callback_data="sources"),
                InlineKeyboardButton("❓ Помощь", callback_data="help"),
            ],
        ])

    await update.effective_message.reply_text(
        text=text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    news = await get_russia_news()

    if not news:
        await update.effective_message.reply_text(
            "❌ Сейчас не удалось получить новости. Попробуйте ещё раз через несколько секунд."
        )
        return

    items = news[:NEWS_PER_DIGEST]
    media_items = []
    text_items = []

    for number, item in enumerate(items, 1):
        body = format_news(item, number)
        body += (
            f'\n🔗 <a href="{html.escape(item["link"])}">'
            "Читать оригинал</a>"
        )

        if item.get("image_url"):
            media_items.append(
                InputMediaPhoto(
                    item["image_url"],
                    caption=body,
                    parse_mode="HTML",
                )
            )
        else:
            text_items.append(body)

    if media_items:
        try:
            await update.effective_message.reply_media_group(media_items)
        except Exception as exc:
            logger.warning("Не удалось показать фотоальбом: %s", exc)
            for media in media_items:
                await update.effective_message.reply_text(
                    media.caption or "",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

    for body in text_items:
        await update.effective_message.reply_text(
            body,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    add_subscriber(user.id, user.username, user.first_name)

    await update.effective_message.reply_text(
        "🔔 <b>Вы подписались на рассылку.</b>\n\n"
        f"Бот будет проверять новые новости каждые "
        f"{max(1, CHECK_INTERVAL // 60)} минут и присылать подборку "
        f"до {NEWS_PER_DIGEST} новых материалов одним сообщением.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🇷🇺 Последние новости", callback_data="latest")],
            [InlineKeyboardButton("🔕 Отписаться", callback_data="unsubscribe")],
        ]),
    )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    remove_subscriber(user.id)

    await update.effective_message.reply_text(
        "🔕 <b>Вы отписались от автоматической рассылки.</b>\n\n"
        "Новости можно по-прежнему получать вручную.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🇷🇺 Последние новости", callback_data="latest")],
            [InlineKeyboardButton("🔔 Подписаться", callback_data="subscribe")],
        ]),
    )


async def sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    source_text = "\n".join(
        f"• {html.escape(name)}"
        for name, _ in RUSSIA_SOURCES
    )

    await update.effective_message.reply_text(
        "📰 <b>Источники российских новостей</b>\n\n"
        f"{source_text}\n\n"
        "Бот показывает заголовок, доступное описание, источник и "
        "ссылку на оригинальную публикацию. Полные статьи не копируются.",
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "❓ <b>Помощь</b>\n\n"
        "/start — главное меню\n"
        "/russia — последние новости\n"
        "/subscribe — подписаться на рассылку\n"
        "/unsubscribe — отписаться\n"
        "/sources — источники\n"
        "/help — помощь\n\n"
        f"Автоматическая проверка выполняется каждые "
        f"{max(1, CHECK_INTERVAL // 60)} минут.",
        parse_mode="HTML",
    )


async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "latest":
        news = await get_russia_news()
        items = news[:NEWS_PER_DIGEST]

        if not items:
            text = "❌ Сейчас новости получить не удалось."
        else:
            parts = ["🇷🇺 <b>Последние новости России</b>", ""]
            for number, item in enumerate(items, 1):
                parts.append(format_news(item, number))
                parts.append(
                    f'🔗 <a href="{html.escape(item["link"])}">Читать оригинал</a>'
                )
                parts.append("")
            text = "\n".join(parts)

        await query.message.reply_text(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    elif query.data == "subscribe":
        await subscribe(update, context)

    elif query.data == "unsubscribe":
        await unsubscribe(update, context)

    elif query.data == "sources":
        await sources(update, context)

    elif query.data == "help":
        await help_command(update, context)


def main() -> None:
    init_db()

    request = HTTPXRequest(
        httpx_kwargs={
            "trust_env": False
        }
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("russia", latest))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe))
    application.add_handler(CommandHandler("sources", sources))
    application.add_handler(CommandHandler("help", help_command))

    application.add_handler(CallbackQueryHandler(callback_handler))

    if application.job_queue is not None:
        application.job_queue.run_repeating(
            news_digest_job,
            interval=CHECK_INTERVAL,
            first=30,
            name="russia_news_digest",
        )
    else:
        logger.error(
            "JobQueue недоступен. Для автоматической рассылки "
            "нужна зависимость python-telegram-bot[job-queue]."
        )

    logger.info("Russia News Bot started")
    logger.info(
        "Проверка новостей каждые %s секунд; в рассылке до %s новостей",
        CHECK_INTERVAL,
        NEWS_PER_DIGEST,
    )

    application.run_polling()


if __name__ == "__main__":
    main()
