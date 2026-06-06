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

# ─── Constants ──────────────────────────────────────────────────────────────

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

# Setup logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ─── Stats ──────────────────────────────────────────────────────────────────

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
            logging.error(f"Failed to load stats: {e}")
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
                logging.error(f"Failed to write stats: {e}")

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
            # Sort slice logic mirroring Go style
            entries = [(u.get("username", "unknown"), u.get("request_count", 0)) for u in self.users.values()]
            entries.sort(key=lambda x: x[1], reverse=True)
            top = entries[:5]

            lines = [f"{i}. @{username} — {count} requests" for i, (username, count) in enumerate(top, start=1)]

            return (
                f"Stats:\n\n"
                f"Total Users: {len(self.users)}\n"
                f"Total Requests: {self.total_requests}\n\n"
                f"Top 5 Users:\n" + "\n".join(lines)
            )

# ─── HTTP Engine / Client ───────────────────────────────────────────────────

def fetch_html(page_url: str, referer: str) -> str:
    headers = DEFAULT_HEADERS.copy()
    headers["Referer"] = referer
    
    response = requests.get(page_url, headers=headers, timeout=30, impersonate="chrome")
    response.raise_for_status()
    return response.text

def get_khdiamond_stream(page_url: str, media_type: str, episode: int = 1) -> str:
    html = fetch_html(page_url, BASE_REFERER)

    match = POST_ID_REGEX.search(html)
    if not match:
        raise Exception("No post ID found on page source.")
    post_id = match.group(1)

    payload = {
        "action": "doo_player_ajax",
        "post": post_id,
        "nume": str(episode) if media_type == "tv" else "1",
        "type": media_type
    }

    ajax_headers = DEFAULT_HEADERS.copy()
    ajax_headers["Referer"] = page_url

    response = requests.post(AJAX_ENDPOINT, data=payload, headers=ajax_headers, timeout=30, impersonate="chrome")
    response.raise_for_status()
    
    result = response.json()
    embed_url = result.get("embed_url")
    if not embed_url:
        raise Exception("Target stream link empty for this election index.")
        
    return embed_url

# ─── Pending Sessions ───────────────────────────────────────────────────────

class PendingMap:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: Dict[int, dict] = {}

    def set(self, chat_id: int, url_str: str) -> None:
        with self._lock:
            self._data[chat_id] = {
                "url": url_str,
                "type": "movie",
                "ep": 1
            }

    def get(self, chat_id: int) -> Tuple[Optional[dict], bool]:
        with self._lock:
            val = self._data.get(chat_id)
            return val, val is not None

    def delete(self, chat_id: int) -> None:
        with self._lock:
            self._data.pop(chat_id, None)

# ─── Bot Handlers ───────────────────────────────────────────────────────────

def get_username(user) -> str:
    if not user:
        return "unknown"
    return user.username if user.username else user.first_name

# Global Thread-safe instances
stats_manager = Stats.load()
pending_sessions = PendingMap()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Send me a khdiamond.net URL and I will get the stream for you.")

async def count_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(stats_manager.report())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if text.startswith("/"):
        return

    if not text.startswith("http"):
        await update.message.reply_text("Please send a valid URL.")
        return

    if "khdiamond.net" not in text:
        await update.message.reply_text("Only khdiamond.net URLs are supported.")
        return

    pending_sessions.set(chat_id, text)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Movie 📽️", callback_data="type_movie"),
            InlineKeyboardButton("TV Show 📺", callback_data="type_tv")
        ]
    ])
    await update.message.reply_text("Is this a Movie or TV Show?", reply_markup=keyboard)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    username = get_username(query.from_user)
    data = query.data

    state, exists = pending_sessions.get(chat_id)
    if not exists:
        await query.message.reply_text("Session expired. Please send the URL again.")
        return

    # Handle transitions / state machine modifications
    if data == "type_movie":
        state["type"] = "movie"
    elif data == "type_tv":
        state["type"] = "tv"
    elif data == "tv_prev":
        if state["ep"] > 1:
            state["ep"] -= 1
    elif data == "tv_next":
        state["ep"] += 1
    # 'refresh' and 'tv_refresh' actions maintain current data block mutations safely

    # Track structural transaction metric
    stats_manager.track_user(chat_id, username)
    
    # Asynchronous file writing task out-of-band via background thread pool
    threading.Thread(target=stats_manager.save, daemon=True).start()

    status_label = "Movie" if state["type"] == "movie" else f"TV Show (EP {state['ep']})"
    loading_msg = await query.message.reply_text(f"Fetching {status_label} stream...")

    try:
        embed_url = get_khdiamond_stream(state["url"], state["type"], state["ep"])

        if state["type"] == "movie":
            loop_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh Movie", callback_data="type_movie")]
            ])
            display_output = f"📺 MOVIE:\n{state['url']}\n\n🎥 WATCH:\n{embed_url}"
        else:
            loop_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⏮️ Prev EP", callback_data="tv_prev"),
                    InlineKeyboardButton("🔄 Refresh EP", callback_data="tv_refresh"),
                    InlineKeyboardButton("⏭️ Next EP", callback_data="tv_next")
                ]
            ])
            display_output = f"📺 TV SHOW (Episode {state['ep']}):\n{state['url']}\n\n🎥 WATCH:\n{embed_url}"

        await query.message.reply_text(display_output, reply_markup=loop_keyboard)

    except Exception as error:
        fallback_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Retry Operation", callback_data="type_movie" if state["type"] == "movie" else "tv_refresh")]
        ])
        await query.message.reply_text(f"⚠️ Extraction Alert: {str(error)}", reply_markup=fallback_keyboard)

    try:
        await loading_msg.delete()
    except Exception:
        pass

# ─── Runtime Initialization ─────────────────────────────────────────────────

def main() -> None:
    load_dotenv()

    token = os.getenv("BOT_TOKEN")
    if not token:
        # Fallback to the code token definition for seamless runtime drop-in
        token = "8648355227:AAHcQQySFDT3EZvWRJ4rEh7nK7rTQXOp8qk"

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("count_process", count_process))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logging.info("Bot started and listening for network traffic updates...")
    app.run_polling()

if __name__ == "__main__":
    main()