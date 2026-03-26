from __future__ import annotations

import asyncio
import html
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from xml.etree import ElementTree

import httpx
from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
from openai import AsyncOpenAI

from config import RSS_FEEDS

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "bot_data.db"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "google/gemini-2.0-flash-lite-preview-01"
SCAN_MINUTE = 5
SEND_DELAY_SECONDS = 0.05
SINGAPORE_TZ = ZoneInfo("Asia/Singapore")


@dataclass(frozen=True)
class NewsItem:
    source: str
    title: str
    link: str
    published: str
    item_id: str


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    async def init(self) -> None:
        async with self.lock:
            with self.connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS subscribers (
                        chat_id INTEGER PRIMARY KEY
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seen_posts (
                        account_name TEXT PRIMARY KEY,
                        tweet_id TEXT NOT NULL
                    )
                    """
                )
                connection.commit()

    async def add_subscriber(self, chat_id: int) -> bool:
        async with self.lock:
            with self.connect() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)",
                    (chat_id,),
                )
                connection.commit()
                return cursor.rowcount > 0

    async def remove_subscriber(self, chat_id: int) -> bool:
        async with self.lock:
            with self.connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM subscribers WHERE chat_id = ?",
                    (chat_id,),
                )
                connection.commit()
                return cursor.rowcount > 0

    async def list_subscribers(self) -> list[int]:
        async with self.lock:
            with self.connect() as connection:
                rows = connection.execute(
                    "SELECT chat_id FROM subscribers ORDER BY chat_id"
                ).fetchall()
                return [int(row["chat_id"]) for row in rows]

    async def get_last_seen_id(self, account_name: str) -> str | None:
        async with self.lock:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT tweet_id FROM seen_posts WHERE account_name = ?",
                    (account_name,),
                ).fetchone()
                if row is None:
                    return None
                return str(row["tweet_id"])

    async def set_last_seen_id(self, account_name: str, tweet_id: str) -> None:
        async with self.lock:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO seen_posts (account_name, tweet_id)
                    VALUES (?, ?)
                    ON CONFLICT(account_name)
                    DO UPDATE SET tweet_id = excluded.tweet_id
                    """,
                    (account_name, tweet_id),
                )
                connection.commit()


def load_settings() -> str:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env before running.")
    return token


def load_translation_settings() -> tuple[str, str]:
    load_dotenv()
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL).strip()
    return api_key, model or DEFAULT_OPENROUTER_MODEL


def parse_rss(xml_text: str, source: str) -> list[NewsItem]:
    root = ElementTree.fromstring(xml_text.lstrip())
    items: list[NewsItem] = []

    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        if not title or not link:
            continue
        items.append(
            NewsItem(
                source=source,
                title=title,
                link=link,
                published=published,
                item_id=guid or link,
            )
        )

    if items:
        return sorted(items, key=_sort_key, reverse=True)

    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("./atom:entry", namespace):
        title = (entry.findtext("atom:title", default="", namespaces=namespace)).strip()
        published = (
            entry.findtext("atom:published", default="", namespaces=namespace)
            or entry.findtext("atom:updated", default="", namespaces=namespace)
        ).strip()
        link = ""
        link_node = entry.find("atom:link", namespace)
        if link_node is not None:
            link = (link_node.attrib.get("href") or "").strip()
        entry_id = (entry.findtext("atom:id", default="", namespaces=namespace)).strip()
        if not title or not link:
            continue
        items.append(
            NewsItem(
                source=source,
                title=title,
                link=link,
                published=published,
                item_id=entry_id or link,
            )
        )

    return sorted(items, key=_sort_key, reverse=True)


def _sort_key(item: NewsItem) -> tuple[int, str]:
    if not item.published:
        return (0, item.item_id)
    try:
        published_at = parsedate_to_datetime(item.published)
    except (TypeError, ValueError):
        return (0, item.published)
    return (int(published_at.timestamp()), item.item_id)


async def fetch_feed(client: httpx.AsyncClient, source: str, url: str) -> list[NewsItem]:
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()
    return parse_rss(response.text, source)


def render_message(item: NewsItem) -> str:
    return item.title


async def translate_tweet(
    client: AsyncOpenAI | None,
    model: str,
    text: str,
) -> str:
    if client is None:
        raise RuntimeError("OPENROUTER_API_KEY is not configured.")

    response = await client.responses.create(
        model=model,
        input=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "You are a professional news translator. "
                            "Translate the following tweet into natural, fluent Chinese, "
                            "preserving technical AI terms."
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        ],
    )
    translated_text = (response.output_text or "").strip()
    if not translated_text:
        raise RuntimeError("Translation response is empty.")
    return translated_text


def format_published_time(published: str) -> str:
    if not published:
        return "Unknown"

    try:
        published_at = parsedate_to_datetime(published)
    except (TypeError, ValueError):
        return published

    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=ZoneInfo("UTC"))

    return published_at.astimezone(SINGAPORE_TZ).strftime("%Y-%m-%d %H:%M")


def format_broadcast_message(
    item: NewsItem,
    translated_text: str,
) -> str:
    author = html.escape(item.source)
    published_time = html.escape(format_published_time(item.published))
    content = html.escape(translated_text)
    link = html.escape(item.link)
    return (
        f"<b>👤 {author}</b>\n\n"
        f"<b>📅 {published_time}</b>\n\n"
        f"<b>中文内容：</b>\n{content}\n\n"
        f"<a href=\"{link}\">🔗 原始链接</a>"
    )


def should_remove_subscriber(exc: Exception) -> bool:
    if isinstance(exc, TelegramForbiddenError):
        return True
    if isinstance(exc, TelegramBadRequest):
        return "user is deactivated" in str(exc).lower()
    return False


async def broadcast_item(
    bot: Bot,
    db: Database,
    chat_ids: list[int],
    message_text: str,
) -> list[int]:
    if not chat_ids:
        return []

    active_chat_ids: list[int] = []
    for index, chat_id in enumerate(chat_ids):
        try:
            await bot.send_message(chat_id=chat_id, text=message_text, parse_mode="HTML")
            active_chat_ids.append(chat_id)
        except Exception as exc:
            print(f"Failed to send item to {chat_id}: {exc}")
            if should_remove_subscriber(exc):
                removed = await db.remove_subscriber(chat_id)
                if removed:
                    print(f"Removed inactive subscriber {chat_id}.")
            else:
                active_chat_ids.append(chat_id)

        if index < len(chat_ids) - 1:
            await asyncio.sleep(SEND_DELAY_SECONDS)

    return active_chat_ids


def collect_new_items(items: list[NewsItem], last_seen_id: str | None) -> list[NewsItem]:
    if not items:
        return []

    latest_item_id = items[0].item_id
    if latest_item_id == last_seen_id:
        return []

    if last_seen_id is None:
        return [items[0]]

    new_items: list[NewsItem] = []
    for item in items:
        if item.item_id == last_seen_id:
            break
        new_items.append(item)
    return list(reversed(new_items))


async def push_news(bot: Bot, db: Database) -> None:
    openrouter_api_key, openrouter_model = load_translation_settings()
    translation_client = (
        AsyncOpenAI(api_key=openrouter_api_key, base_url=OPENROUTER_BASE_URL)
        if openrouter_api_key
        else None
    )
    subscribers = await db.list_subscribers()
    print(f"Push cycle started. Subscribers: {len(subscribers)}")
    total_sent = 0

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0),
        headers={"User-Agent": "AI-News-Bot/0.1"},
    ) as client:
        for source, url in RSS_FEEDS.items():
            try:
                items = await fetch_feed(client, source, url)
            except Exception as exc:
                print(f"Skipping {source}: {exc}")
                continue

            if not items:
                continue

            last_seen_id = await db.get_last_seen_id(source)
            latest_item_id = items[0].item_id
            new_items = collect_new_items(items, last_seen_id)
            print(
                f"Source={source} latest_id={latest_item_id} "
                f"last_seen_id={last_seen_id} new_items={len(new_items)}"
            )

            if not new_items:
                if last_seen_id is None:
                    await db.set_last_seen_id(source, latest_item_id)
                del items
                continue

            if subscribers:
                for item in new_items:
                    try:
                        translated_text = await translate_tweet(
                            translation_client,
                            openrouter_model,
                            item.title,
                        )
                    except Exception as exc:
                        print(f"Translation failed for {source}: {exc}")
                        translated_text = item.title

                    message_text = format_broadcast_message(item, translated_text)
                    subscribers = await broadcast_item(
                        bot,
                        db,
                        subscribers,
                        message_text,
                    )
                    total_sent += len(subscribers)
                    del message_text
                    del translated_text
            else:
                print(f"No subscribers. Updated {source} to latest tweet only.")

            await db.set_last_seen_id(source, latest_item_id)
            del new_items
            del items

    if translation_client is not None:
        await translation_client.close()
    print(f"Push cycle completed. Active subscribers: {len(subscribers)} total_deliveries={total_sent}")


def seconds_until_next_run(now: datetime | None = None) -> float:
    current = now or datetime.now()
    next_run = current.replace(minute=SCAN_MINUTE, second=0, microsecond=0)
    if current >= next_run:
        next_run = (current + timedelta(hours=1)).replace(
            minute=SCAN_MINUTE,
            second=0,
            microsecond=0,
        )
    return max((next_run - current).total_seconds(), 0.0)


async def scraping_loop(bot: Bot, db: Database) -> None:
    while True:
        delay = seconds_until_next_run()
        print(f"Next scrape starts in {delay:.0f} seconds.")
        await asyncio.sleep(delay)

        try:
            await push_news(bot, db)
        except Exception as exc:
            print(f"Scraping loop failed: {exc}")


router = Router()
database: Database | None = None


def get_db() -> Database:
    if database is None:
        raise RuntimeError("Database is not initialized.")
    return database


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    added = await get_db().add_subscriber(message.chat.id)
    if added:
        await message.answer("欢迎订阅 AI 报童！")
        return
    await message.answer("欢迎订阅 AI 报童！")


@router.message(Command("stop"))
async def handle_stop(message: Message) -> None:
    removed = await get_db().remove_subscriber(message.chat.id)
    if removed:
        await message.answer("已取消订阅。")
        return
    await message.answer("你当前没有订阅。")


@router.message(Command("list"))
async def handle_list(message: Message) -> None:
    accounts = "\n".join(f"- {name}" for name in RSS_FEEDS)
    await message.answer(f"当前监控的 AI 账号：\n{accounts}")


@router.message(Command("run_now"))
async def handle_run_now(message: Message) -> None:
    await message.answer("开始立即执行一次抓取，请稍候查看日志和推送结果。")
    try:
        await push_news(message.bot, get_db())
        await message.answer("本次手动抓取已执行完成。")
    except Exception as exc:
        print(f"Manual run failed: {exc}")
        await message.answer(f"本次手动抓取失败：{html.escape(str(exc))}")


async def main() -> None:
    global database

    if not RSS_FEEDS:
        raise RuntimeError("Add at least one XCancel RSS feed URL to config.py.")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    database = Database(DB_PATH)
    await database.init()
    initial_subscribers = await database.list_subscribers()
    print(
        f"Bot starting. data_dir={DATA_DIR} db_path={DB_PATH} "
        f"subscribers={len(initial_subscribers)} rss_sources={len(RSS_FEEDS)}"
    )

    bot = Bot(load_settings())
    dp = Dispatcher()
    dp.include_router(router)

    scraping_task = asyncio.create_task(scraping_loop(bot, database))
    try:
        await dp.start_polling(bot)
    finally:
        scraping_task.cancel()
        await asyncio.gather(scraping_task, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
