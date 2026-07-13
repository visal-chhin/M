import json
import logging
import os
import re
import threading
import time
from typing import Dict, Any, Tuple, Optional

from dotenv import load_dotenv
from curl_cffi import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# ─── Constants & Configuration ──────────────────────────────────────────────

STATS_FILE = "stats.json"
AJAX_ENDPOINT = "https://khdiamond.net/wp-admin/admin-ajax.php"
BASE_REFERER = "https://khdiamond.net"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive"
}

POST_ID_REGEX = re.compile(r"postid-(\d+)")
EPISODE_URL_REGEX = re.compile(r"(\d+)x(\d+)(/?)$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ─── Data Structures & State Management ─────────────────────────────────────

class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.total_requests = 0
        self.users: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def load(cls) -> "Stats":
        s = cls()
        if not os.path.exists(STATS_FILE):
            return s
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                s.total_requests = data.get("total_requests", 0)
                s.users = data.get("users", {})
        except Exception as e:
            logging.error(f"Failed to load user stats from storage: {e}")
        return s

    def save(self) -> None:
        with self._lock:
            try:
                data = {
                    "total_requests": self.total_requests,
                    "users": self.users
                }
                with open(STATS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
            except Exception as e:
                logging.error(f"Failed to write user stats to storage: {e}")

    def track_user(self, chat_id: int, username: str) -> None:
        with self._lock:
            key = str(chat_id)
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            self.total_requests += 1

            if key not in self.users:
                self.users[key] = {
                    "username": username,
                    "first_seen": now,
                    "last_seen": now,
                    "request_count": 0
                }
            
            self.users[key]["request_count"] += 1
            self.users[key]["last_seen"] = now
            if username:
                self.users[key]["username"] = username

    def report(self) -> str:
        with self._lock:
            entries = [(u.get("username", "unknown"), u.get("request_count", 0)) for u in self.users.values()]
            entries.sort(key=lambda x: x[1], reverse=True)
            top = entries[:5]

            lines = [f"{i}. @{username} — {count} reqs" for i, (username, count) in enumerate(top, start=1)]

            return (
                f"📊 Stats:\n"
                f"Total Users: {len(self.users)}\n"
                f"Total Requests: {self.total_requests}\n\n"
                f"Top 5 Users:\n" + "\n".join(lines)
            )


class PendingMap:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: Dict[int, dict] = {}

    def set(self, chat_id: int, url_str: str) -> None:
        initial_ep = 1
        match = EPISODE_URL_REGEX.search(url_str.rstrip("/"))
        if match:
            initial_ep = int(match.group(2))

        with self._lock:
            self._data[chat_id] = {
                "url": url_str,
                "type": "movie",
                "ep": initial_ep
            }

    def get(self, chat_id: int) -> Tuple[Optional[dict], bool]:
        with self._lock:
            val = self._data.get(chat_id)
            return val, val is not None

    def delete(self, chat_id: int) -> None:
        with self._lock:
            self._data.pop(chat_id, None)

# ─── Network Scraper Engine ──────────────────────────────────────────────────

def fetch_html(page_url: str, referer: str, proxy_url: Optional[str] = None) -> str:
    headers = DEFAULT_HEADERS.copy()
    headers["Referer"] = referer
    
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    try:
        response = requests.get(
            page_url, 
            headers=headers, 
            timeout=30, 
            impersonate="chrome", 
            proxies=proxies
        )
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestsError as ce:
        if "Unsupported proxy syntax" in str(ce) and proxies:
            logging.warning("Proxy syntax error caught. Dropping proxy configuration parameters and trying raw fallback connection...")
            response = requests.get(page_url, headers=headers, timeout=30, impersonate="chrome")
            response.raise_for_status()
            return response.text
        raise ce


def get_khdiamond_stream(page_url: str, media_type: str, episode: int = 1, proxy_url: Optional[str] = None) -> str:
    html = fetch_html(page_url, BASE_REFERER, proxy_url)

    match = POST_ID_REGEX.search(html)
    if not match:
        raise Exception("Failed to locate internal Post ID within server response.")
    post_id = match.group(1)

    payload = {
        "action": "doo_player_ajax",
        "post": post_id,
        "nume": str(episode) if media_type == "tv" else "1",
        "type": media_type
    }

    ajax_headers = DEFAULT_HEADERS.copy()
    ajax_headers["Referer"] = page_url
    ajax_headers["Origin"] = BASE_REFERER

    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    try:
        response = requests.post(
            AJAX_ENDPOINT, 
            data=payload, 
            headers=ajax_headers, 
            timeout=30, 
            impersonate="chrome", 
            proxies=proxies
        )
        response.raise_for_status()
    except requests.exceptions.RequestsError as ce:
        if "Unsupported proxy syntax" in str(ce) and proxies:
            response = requests.post(AJAX_ENDPOINT, data=payload, headers=ajax_headers, timeout=30, impersonate="chrome")
            response.raise_for_status()
        else:
            raise ce

    result = response.json()
    embed_url = result.get("embed_url")
    if not embed_url:
        raise Exception("Server returned empty embed stream URL layout configuration.")

    return embed_url

# ─── Bot Presentation Interface Handlers ─────────────────────────────────────

def get_username(user) -> str:
    if not user:
        return "unknown"
    return user.username if user.username else user.first_name

stats_manager = Stats.load()
pending_sessions = PendingMap()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("👋 Send a valid khdiamond.net link.")

async def count_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(stats_manager.report())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if text.startswith("/"):
        return

    if not text.startswith("http") or "khdiamond.net" not in text:
        await update.message.reply_text("⚠️ Invalid domain layout pattern.")
        return

    pending_sessions.set(chat_id, text)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Movie", callback_data="type_movie"),
            InlineKeyboardButton("📺 TV Series", callback_data="type_tv")
        ]
    ])
    await update.message.reply_text("Select Media Type:", reply_markup=keyboard)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    username = get_username(query.from_user)
    data = query.data

    state, exists = pending_sessions.get(chat_id)
    if not exists:
        await query.message.reply_text("❌ Session expired. Please send the link again.")
        return

    if data == "type_movie":
        state["type"] = "movie"
    elif data == "type_tv":
        state["type"] = "tv"
    elif data in ("tv_prev", "tv_next", "tv_refresh"):
        if data == "tv_prev" and state["ep"] > 1:
            state["ep"] -= 1
        elif data == "tv_next":
            state["ep"] += 1

        current_url = state["url"]
        match = EPISODE_URL_REGEX.search(current_url.rstrip("/"))
        if match:
            season = match.group(1)
            trailing_slash = match.group(3) or "/"
            new_pattern = f"{season}x{state['ep']}{trailing_slash}"
            state["url"] = EPISODE_URL_REGEX.sub(new_pattern, current_url.rstrip("/")) + (trailing_slash if current_url.endswith("/") else "")

    stats_manager.track_user(chat_id, username)
    threading.Thread(target=stats_manager.save, daemon=True).start()

    status_label = "Movie" if state["type"] == "movie" else f"EP {state['ep']}"
    loading_msg = await query.message.reply_text(f"⏳ Loading {status_label}...")

    try:
        proxy_url = os.getenv("PROXY_URL") or None
        embed_url = get_khdiamond_stream(state["url"], state["type"], state["ep"], proxy_url=proxy_url)

        if state["type"] == "movie":
            loop_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh Movie", callback_data="type_movie")]
            ])
            display_output = f"🎬 **MOVIE:**\n🔗 {state['url']}\n\n🚀 **WATCH:**\n{embed_url}"
        else:
            loop_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⏮️ Prev EP", callback_data="tv_prev"),
                    InlineKeyboardButton("🔄 Refresh", callback_data="tv_refresh"),
                    InlineKeyboardButton("⏭️ Next EP", callback_data="tv_next")
                ]
            ])
            display_output = f"📺 **TV SERIES:**\n🔗 {state['url']}\n🔢 Episode: {state['ep']}\n\n🚀 **WATCH:**\n{embed_url}"

        await query.message.reply_text(display_output, reply_markup=loop_keyboard, parse_mode="Markdown")

    except Exception as error:
        logging.error(f"Pipeline fault: {error}")
        err_msg = str(error)
        if "Unsupported proxy syntax" in err_msg:
            err_msg = "Proxy syntax configuration error on host variable. Please fix formatting."
            
        fallback_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Retry", callback_data="type_movie" if state["type"] == "movie" else "tv_refresh")]
        ])
        await query.message.reply_text(f"⚠️ **Error:** `{err_msg}`", reply_markup=fallback_keyboard, parse_mode="Markdown")

    try:
        await loading_msg.delete()
    except Exception:
        pass

# ─── Application Main Initialization Entrypoint ─────────────────────────────
def main() -> None:
    load_dotenv()

    TOKEN = "8648355227:AAHygQo9ud4VPnzSIj8uafzHIRYQIZSVSI8"

    if not TOKEN:

        TOKEN = "8648355227:AAHygQo9ud4VPnzSIj8uafzHIRYQIZSVSI8"


    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("count_process", count_process))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logging.info("Network listener active...")
    app.run_polling()


if __name__ == "__main__":
    main()