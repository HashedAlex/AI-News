from __future__ import annotations

import asyncio
import html
import os
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from xml.etree import ElementTree

import httpx
from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
from openai import AsyncOpenAI

from config import ACCOUNTS, PROVIDER, RSSHUB_PLACEHOLDER_URL, XCANCEL_BASE_URL

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "bot_data.db"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "google/gemini-2.5-flash-lite"
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


@dataclass(frozen=True)
class PushStats:
    subscribers: int
    sources_checked: int
    sources_with_updates: int
    new_items: int
    deliveries: int
    translation_failures: int
    blocked_sources: int


class FeedAccessError(RuntimeError):
    pass


class FeedProvider(ABC):
    name: str

    @abstractmethod
    def build_feed_url(self, username: str) -> str:
        raise NotImplementedError

    def validate_feed(self, root: ElementTree.Element, source: str) -> None:
        return None

    def get_feeds(self) -> dict[str, str]:
        return {
            source: self.build_feed_url(username)
            for source, username in ACCOUNTS.items()
        }


class XCancelProvider(FeedProvider):
    name = "xcancel"

    def __init__(self, base_url: str = XCANCEL_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")

    def build_feed_url(self, username: str) -> str:
        return f"{self.base_url}/{username}/rss"

    def validate_feed(self, root: ElementTree.Element, source: str) -> None:
        channel_title = (root.findtext("./channel/title") or "").strip()
        channel_description = (root.findtext("./channel/description") or "").strip()
        if (
            "RSS reader not yet whitelist" in channel_title
            or "RSS reader not yet whitelist" in channel_description
        ):
            raise FeedAccessError(f"{source} feed is not whitelisted by XCancel yet.")


class RSSHubProvider(FeedProvider):
    name = "rsshub"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def build_feed_url(self, username: str) -> str:
        return f"{self.base_url}/twitter/user/{username}"


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


def get_provider() -> FeedProvider:
    provider_name = PROVIDER.strip().lower()
    if provider_name == "xcancel":
        return XCancelProvider()

    if provider_name == "rsshub":
        rsshub_url = os.getenv("RSSHUB_URL", "").strip()
        if not rsshub_url:
            print(
                f"RSSHUB_URL is missing. Falling back to placeholder {RSSHUB_PLACEHOLDER_URL}",
                flush=True,
            )
            rsshub_url = RSSHUB_PLACEHOLDER_URL
        return RSSHubProvider(rsshub_url)

    raise RuntimeError(f"Unsupported provider configured: {PROVIDER}")


def canonical_item_id(*candidates: str) -> str:
    for candidate in candidates:
        value = (candidate or "").strip()
        if not value:
            continue

        status_id = extract_status_id(value)
        if status_id:
            return status_id

    for candidate in candidates:
        value = (candidate or "").strip()
        if value:
            return value

    return ""


def extract_status_id(value: str) -> str | None:
    try:
        parsed = urlparse(value)
    except ValueError:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part == "status":
            return parts[index + 1]

    return None


def parse_rss(xml_text: str, source: str, provider: FeedProvider) -> list[NewsItem]:
    root = ElementTree.fromstring(xml_text.lstrip())
    provider.validate_feed(root, source)

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
                item_id=canonical_item_id(guid, link),
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
                item_id=canonical_item_id(entry_id, link),
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


async def fetch_feed(
    client: httpx.AsyncClient,
    source: str,
    url: str,
    provider: FeedProvider,
) -> list[NewsItem]:
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()
    return parse_rss(response.text, source, provider)


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


MAX_NEW_ITEMS_PER_SOURCE = 5


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

    # Cap to avoid flooding after provider switch or first run
    if len(new_items) > MAX_NEW_ITEMS_PER_SOURCE:
        print(
            f"Capping new items from {len(new_items)} to {MAX_NEW_ITEMS_PER_SOURCE}",
            flush=True,
        )
        new_items = new_items[:MAX_NEW_ITEMS_PER_SOURCE]

    return list(reversed(new_items))


async def push_news(bot: Bot, db: Database) -> PushStats:
    provider = get_provider()
    feeds = provider.get_feeds()

    openrouter_api_key, openrouter_model = load_translation_settings()
    translation_client = (
        AsyncOpenAI(api_key=openrouter_api_key, base_url=OPENROUTER_BASE_URL)
        if openrouter_api_key
        else None
    )
    subscribers = await db.list_subscribers()
    print(f"Push cycle started. Subscribers: {len(subscribers)}", flush=True)
    total_sent = 0
    sources_checked = 0
    sources_with_updates = 0
    total_new_items = 0
    translation_failures = 0
    blocked_sources = 0

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0),
        headers={"User-Agent": "AI-News-Bot/0.1"},
    ) as client:
        for source, url in feeds.items():
            sources_checked += 1
            try:
                items = await fetch_feed(client, source, url, provider)
            except FeedAccessError as exc:
                blocked_sources += 1
                print(f"Blocked feed for {source}: {exc}", flush=True)
                continue
            except Exception as exc:
                print(f"Skipping {source}: {exc}", flush=True)
                continue

            if not items:
                continue

            last_seen_id = await db.get_last_seen_id(source)
            latest_item_id = items[0].item_id
            new_items = collect_new_items(items, last_seen_id)
            print(
                f"Source={source} latest_id={latest_item_id} "
                f"last_seen_id={last_seen_id} new_items={len(new_items)}"
                ,
                flush=True,
            )

            if not new_items:
                if last_seen_id is None:
                    await db.set_last_seen_id(source, latest_item_id)
                del items
                continue

            sources_with_updates += 1
            total_new_items += len(new_items)

            if subscribers:
                for item in new_items:
                    try:
                        translated_text = await translate_tweet(
                            translation_client,
                            openrouter_model,
                            item.title,
                        )
                    except Exception as exc:
                        print(f"Translation failed for {source}: {exc}", flush=True)
                        translated_text = item.title
                        translation_failures += 1

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
                print(f"No subscribers. Updated {source} to latest tweet only.", flush=True)

            await db.set_last_seen_id(source, latest_item_id)
            del new_items
            del items

    if translation_client is not None:
        await translation_client.close()
    print(
        f"Push cycle completed. Active subscribers: {len(subscribers)} "
        f"sources_checked={sources_checked} sources_with_updates={sources_with_updates} "
        f"new_items={total_new_items} total_deliveries={total_sent} "
        f"translation_failures={translation_failures} blocked_sources={blocked_sources}",
        flush=True,
    )
    return PushStats(
        subscribers=len(subscribers),
        sources_checked=sources_checked,
        sources_with_updates=sources_with_updates,
        new_items=total_new_items,
        deliveries=total_sent,
        translation_failures=translation_failures,
        blocked_sources=blocked_sources,
    )


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
            print(f"Scraping loop failed: {exc}", flush=True)


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
    provider = get_provider()
    accounts = "\n".join(f"- {name}" for name in ACCOUNTS)
    await message.answer(
        f"当前 provider：{provider.name}\n当前监控的 AI 账号：\n{accounts}"
    )


@router.message(Command("run_now"))
async def handle_run_now(message: Message) -> None:
    await message.answer("开始立即执行一次抓取，请稍候查看日志和推送结果。")
    try:
        stats = await push_news(message.bot, get_db())
        await message.answer(
            "本次手动抓取已执行完成。\n"
            f"订阅人数：{stats.subscribers}\n"
            f"检查源数：{stats.sources_checked}\n"
            f"有更新源数：{stats.sources_with_updates}\n"
            f"新内容数：{stats.new_items}\n"
            f"发送总数：{stats.deliveries}\n"
            f"翻译失败数：{stats.translation_failures}\n"
            f"被源站拦截数：{stats.blocked_sources}"
        )
    except Exception as exc:
        print(f"Manual run failed: {exc}", flush=True)
        await message.answer(f"本次手动抓取失败：{html.escape(str(exc))}")


async def main() -> None:
    global database

    provider = get_provider()
    feeds = provider.get_feeds()
    if not feeds:
        raise RuntimeError("Add at least one account to config.py.")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    database = Database(DB_PATH)
    await database.init()
    initial_subscribers = await database.list_subscribers()
    print(
        f"Bot starting. data_dir={DATA_DIR} db_path={DB_PATH} "
        f"subscribers={len(initial_subscribers)} rss_sources={len(feeds)} provider={provider.name}"
        ,
        flush=True,
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
