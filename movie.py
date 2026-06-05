import os
import re
import json
import time
import threading
import requests

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# -------------------------
# CONFIG & STATE
# -------------------------
load_dotenv()

# Exposed Token Config
TOKEN = "8648355227:AAHcQQySFDT3EZvWRJ4rEh7nK7rTQXOp8qk"
AJAX_ENDPOINT = "https://khdiamond.net/wp-admin/admin-ajax.php"
BASE_REFERER = "https://khdiamond.net"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    )
}

POST_ID_REGEX = re.compile(r"postid-(\d+)")
stats_file = "stats.json"
lock = threading.Lock()

# Persistent state mapping: chat_id -> {"url": str, "type": str, "ep": int}
session_state = {}

# Initialize Stats Data
stats = {"total_requests": 0, "users": {}}
if os.path.exists(stats_file):
    try:
        with open(stats_file, "r", encoding="utf-8") as f:
            stats = json.load(f)
    except Exception:
        pass

# -------------------------
# COMMAND HANDLERS
# -------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a khdiamond.net URL.")


async def count_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Inline generation of the stats summary report
    users = list(stats["users"].values())
    users.sort(key=lambda x: x["request_count"], reverse=True)
    top = users[:5]
    
    lines = [f"{i}. @{user['username']} — {user['request_count']} requests" for i, user in enumerate(top, start=1)]
    
    report_text = (
        f"Stats:\n\n"
        f"Total Users: {len(stats['users'])}\n"
        f"Total Requests: {stats['total_requests']}\n\n"
        f"Top 5 Users:\n" + "\n".join(lines)
    )
    await update.message.reply_text(report_text)

# -------------------------
# MESSAGE HANDLER
# -------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if not text.startswith("http"):
        await update.message.reply_text("Please send a valid URL.")
        return

    if "khdiamond.net" not in text:
        await update.message.reply_text("Only khdiamond.net URLs are supported.")
        return

    # Store initial target context; Default to Episode index 1 for potential TV loops
    session_state[chat_id] = {
        "url": text,
        "type": "movie",
        "ep": 1
    }

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📽️ Movie ", callback_data="set_movie"),
            InlineKeyboardButton("📺 EP  ", callback_data="set_tv")
        ]
    ])

    await update.message.reply_text(
        f"Choose type:",
        reply_markup=keyboard
    )

# -------------------------
# CALLBACK QUERY INTERACTION (LOOP ENGINE)
# -------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat.id

    if chat_id not in session_state:
        await query.message.reply_text("Session data missing. Please drop the link again.")
        return

    data = query.data
    state = session_state[chat_id]

    # Handle State Transitions / Navigation Loops
    if data == "set_movie":
        state["type"] = "movie"
    elif data == "set_tv":
        state["type"] = "tv"
    elif data == "tv_prev":
        if state["ep"] > 1:
            state["ep"] -= 1
    elif data == "tv_next":
        state["ep"] += 1
    # Note: "refresh" or "tv_refresh" actions do not modify state variables, they re-trigger the engine.

    # Inline Analytics Logging Execution
    username = query.from_user.username or query.from_user.first_name
    with lock:
        key = str(chat_id)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        stats["total_requests"] += 1
        if key not in stats["users"]:
            stats["users"][key] = {
                "username": username,
                "first_seen": now,
                "last_seen": now,
                "request_count": 0
            }
        stats["users"][key]["request_count"] += 1
        stats["users"][key]["last_seen"] = now
        if username:
            stats["users"][key]["username"] = username
        
        with open(stats_file, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=4)

    # Visual Update Notification
    status_label = "Movie" if state["type"] == "movie" else f"TV Show (EP {state['ep']})"
    loading_msg = await query.message.reply_text(f"Processing backend handshake for {status_label}...")

    # Core Streaming Scraper Logic Integration
    try:
        # Step 1: Resource Fetch
        headers = HEADERS.copy()
        headers["Referer"] = BASE_REFERER
        r = requests.get(state["url"], headers=headers, timeout=30)
        r.raise_for_status()
        html = r.text

        # Step 2: DOM Parsing via Regex
        match = POST_ID_REGEX.search(html)
        if not match:
            raise Exception("No post ID found in source markup.")
        post_id = match.group(1)

        # Step 3: Asynchronous Gateway Handshake Payload
        # For TV mode, the API parameter matches the numeric progression loop
        payload = {
            "action": "doo_player_ajax",
            "post": post_id,
            "nume": str(state["ep"]) if state["type"] == "tv" else "1",
            "type": state["type"]
        }

        ajax_headers = HEADERS.copy()
        ajax_headers["Referer"] = state["url"]

        res = requests.post(AJAX_ENDPOINT, data=payload, headers=ajax_headers, timeout=30)
        res.raise_for_status()
        data_json = res.json()

        embed_url = data_json.get("embed_url")
        if not embed_url:
            raise Exception("Resource target stream link empty for this index parameter selection.")

        # Build Contextual Loop Interface Control Panels
        if state["type"] == "movie":
            loop_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh Movie ", callback_data="refresh")]
            ])
            display_output = (
                f"📺 MOVIE:\n"
                f"{state['url']}\n\n"
                f"🎥 WATCH:\n"
                f"{embed_url}\n\n"
            )
            # display_output = f"🎬 **Movie **\n\n text \n\nTarget URL:\n{embed_url}"
        else:
            loop_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⏮️ Prev EP", callback_data="tv_prev"),
                    InlineKeyboardButton("🔄 Refresh EP", callback_data="tv_refresh"),
                    InlineKeyboardButton("⏭️ Next EP", callback_data="tv_next")
                ]
            ])
            display_output = (
                f"📺 TV SHOW (Episode {state['ep']}):\n"
                f"{state['url']}\n\n"
                f"🎥 WATCH:\n"
                f"{embed_url}\n\n"
            )

        await query.message.reply_text(display_output, reply_markup=loop_keyboard, parse_mode="Markdown")

    except Exception as error:
        # Fallback UI rendering with control panels persistent to allow retries or navigation out of dead endpoints
        fallback_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Retry Operation", callback_data="refresh")]
        ]) if state["type"] == "movie" else InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏮️ Prev EP", callback_data="tv_prev"),
                InlineKeyboardButton("🔄 Retry EP", callback_data="tv_refresh"),
                InlineKeyboardButton("⏭️ Next EP", callback_data="tv_next")
            ]
        ])
        await query.message.reply_text(f"⚠️ **Extraction Alert:** {str(error)}", reply_markup=fallback_keyboard, parse_mode="Markdown")

    try:
        await loading_msg.delete()
    except Exception:
        pass

# -------------------------
# RUNTIME INITIALIZATION
# -------------------------

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("count_process", count_process))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()