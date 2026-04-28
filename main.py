import os
import sys
import asyncio
import logging
import threading
import time
import json
import base64
import datetime
import io
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
from PIL import Image, ImageDraw, ImageFont
import google_play_scraper as gps
from telegram import (
    Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.error import TelegramError
import firebase_admin
from firebase_admin import credentials, firestore
from groq import Groq

# ─── Logging ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("ReviewBot")

# ─── Environment Variables ────────────────────────────────
TELEGRAM_BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY         = os.environ["GROQ_API_KEY"]
FIREBASE_CRED_JSON   = os.environ["FIREBASE_CREDENTIALS_JSON"]  # Full JSON string
ADMIN_IDS            = list(map(int, os.environ.get("ADMIN_IDS", "").split(","))) if os.environ.get("ADMIN_IDS") else []
GREEN_API_INSTANCE   = os.environ.get("GREEN_API_INSTANCE", "")   # WhatsApp (Green API)
GREEN_API_TOKEN      = os.environ.get("GREEN_API_TOKEN", "")
PORT                 = int(os.environ.get("PORT", 8080))
RENDER_URL           = os.environ.get("RENDER_URL", "")           # e.g. https://your-app.onrender.com

# Conversation states
(
    WAITING_APP_ID,
    WAITING_TG_GROUP,
    WAITING_WA_GROUP,
) = range(3)

# ─── Firebase Init ────────────────────────────────────────
def init_firebase():
    try:
        cred_dict = json.loads(FIREBASE_CRED_JSON)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        logger.info("✅ Firebase initialized")
        return firestore.client()
    except Exception as e:
        logger.error(f"❌ Firebase init failed: {e}")
        raise

db = init_firebase()

# ─── Groq AI Client ───────────────────────────────────────
groq_client = Groq(api_key=GROQ_API_KEY)

# ─── Keep-Alive HTTP Server (Render 24/7) ─────────────────
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
        logger.debug("Ping received")

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logs

def run_keep_alive_server():
    server = HTTPServer(("0.0.0.0", PORT), PingHandler)
    logger.info(f"🌐 Keep-alive server running on port {PORT}")
    server.serve_forever()

def start_keep_alive():
    t = threading.Thread(target=run_keep_alive_server, daemon=True)
    t.start()

def self_ping_loop():
    """Ping own URL every 4 minutes to prevent Render from sleeping."""
    if not RENDER_URL:
        logger.warning("⚠️  RENDER_URL not set — self-ping disabled")
        return
    while True:
        try:
            requests.get(RENDER_URL, timeout=10)
            logger.debug(f"🔄 Self-pinged {RENDER_URL}")
        except Exception as e:
            logger.warning(f"Self-ping failed: {e}")
        time.sleep(240)  # Every 4 minutes

def start_self_ping():
    t = threading.Thread(target=self_ping_loop, daemon=True)
    t.start()

# ─── Firebase Helpers ─────────────────────────────────────
def get_all_apps():
    """Return list of app configs from Firestore."""
    docs = db.collection("apps").stream()
    return [{"id": d.id, **d.to_dict()} for d in docs]

def save_app(app_id: str, tg_group: str, wa_group: str = ""):
    db.collection("apps").document(app_id).set({
        "app_id": app_id,
        "tg_group": tg_group,
        "wa_group": wa_group,
        "added_at": firestore.SERVER_TIMESTAMP,
        "active": True,
    }, merge=True)

def delete_app(app_id: str):
    db.collection("apps").document(app_id).delete()

def is_review_seen(app_id: str, review_id: str) -> bool:
    doc = db.collection("seen_reviews").document(f"{app_id}_{review_id}").get()
    return doc.exists

def mark_review_seen(app_id: str, review_id: str, date_str: str):
    db.collection("seen_reviews").document(f"{app_id}_{review_id}").set({
        "app_id": app_id,
        "review_id": review_id,
        "date": date_str,
        "seen_at": firestore.SERVER_TIMESTAMP,
    })

def increment_daily_count(app_id: str, date_str: str) -> int:
    ref = db.collection("daily_counts").document(f"{app_id}_{date_str}")
    doc = ref.get()
    if doc.exists:
        count = doc.to_dict().get("count", 0) + 1
    else:
        count = 1
    ref.set({"app_id": app_id, "date": date_str, "count": count})
    return count

def get_daily_count(app_id: str, date_str: str) -> int:
    doc = db.collection("daily_counts").document(f"{app_id}_{date_str}").get()
    return doc.to_dict().get("count", 0) if doc.exists else 0

# ─── Screenshot Generator ─────────────────────────────────
def generate_review_screenshot(review: dict, app_name: str) -> bytes:
    """Create a nice review card image."""
    W, H = 800, 420
    BG      = (245, 248, 255)
    CARD    = (255, 255, 255)
    GOLD    = (255, 193, 7)
    DARK    = (30, 30, 50)
    GRAY    = (100, 110, 130)
    GREEN   = (34, 197, 94)

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Card shadow
    draw.rounded_rectangle([18, 18, W-18, H-18], radius=20, fill=(220, 225, 240))
    # Card
    draw.rounded_rectangle([14, 14, W-14, H-14], radius=20, fill=CARD)

    # Header bar
    draw.rounded_rectangle([14, 14, W-14, 70], radius=20, fill=(63, 81, 181))
    draw.rectangle([14, 50, W-14, 70], fill=(63, 81, 181))

    try:
        font_big   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        font_med   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
    except Exception:
        font_big = font_med = font_small = ImageFont.load_default()

    # App name in header
    draw.text((30, 28), f"📱 {app_name}", fill=(255, 255, 255), font=font_big)

    # Stars
    stars = "★" * int(review.get("score", 5)) + "☆" * (5 - int(review.get("score", 5)))
    draw.text((30, 88), stars, fill=GOLD, font=font_big)

    # Reviewer name & date
    user_name = review.get("userName", "Anonymous")
    at_str    = review.get("at", "")
    if hasattr(at_str, "strftime"):
        at_str = at_str.strftime("%d %b %Y, %I:%M %p")

    draw.text((30, 125), f"👤 {user_name}", fill=DARK, font=font_med)
    draw.text((W - 220, 125), f"🗓 {at_str}", fill=GRAY, font=font_small)

    # Divider
    draw.line([(30, 158), (W-30, 158)], fill=(220, 225, 240), width=2)

    # Review text (wrap)
    content = review.get("content", "").strip() or "(No text)"
    max_chars = 80
    lines = []
    words = content.split()
    cur = ""
    for w in words:
        if len(cur) + len(w) + 1 > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    lines = lines[:6]  # max 6 lines

    y = 172
    for ln in lines:
        draw.text((30, y), ln, fill=DARK, font=font_med)
        y += 28

    # Thumbs up
    thumbs = review.get("thumbsUpCount", 0)
    draw.text((30, H - 55), f"👍 {thumbs} people found this helpful", fill=GRAY, font=font_small)

    # Live badge
    draw.rounded_rectangle([W - 130, H - 65, W - 20, H - 35], radius=12, fill=GREEN)
    draw.text((W - 118, H - 57), "🟢 LIVE", fill=(255, 255, 255), font=font_small)

    # Footer
    draw.text((30, H - 30), "Play Store Review Monitor Bot", fill=GRAY, font=font_small)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()

# ─── WhatsApp Sender (Green API) ──────────────────────────
def send_whatsapp_message(group_id: str, text: str):
    if not GREEN_API_INSTANCE or not GREEN_API_TOKEN:
        logger.warning("WhatsApp credentials not configured")
        return
    url = (
        f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE}"
        f"/sendMessage/{GREEN_API_TOKEN}"
    )
    payload = {"chatId": group_id, "message": text}
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        logger.info(f"✅ WhatsApp message sent to {group_id}")
    except Exception as e:
        logger.error(f"❌ WhatsApp send failed: {e}")

def send_whatsapp_image(group_id: str, image_bytes: bytes, caption: str):
    if not GREEN_API_INSTANCE or not GREEN_API_TOKEN:
        return
    url = (
        f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE}"
        f"/sendFileByUpload/{GREEN_API_TOKEN}"
    )
    try:
        files = {"file": ("review.png", image_bytes, "image/png")}
        data  = {"chatId": group_id, "caption": caption}
        r = requests.post(url, files=files, data=data, timeout=30)
        r.raise_for_status()
        logger.info(f"✅ WhatsApp image sent to {group_id}")
    except Exception as e:
        logger.error(f"❌ WhatsApp image send failed: {e}")

# ─── Groq AI Helper ───────────────────────────────────────
def ai_summarize_review(content: str) -> str:
    try:
        resp = groq_client.chat.completions.create(
            model="llama3-8b-8192",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful assistant. Summarize the given Play Store "
                        "review in 1-2 sentences, highlighting the key praise. "
                        "Be concise and positive."
                    ),
                },
                {"role": "user", "content": f"Review: {content}"},
            ],
            max_tokens=120,
            temperature=0.5,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"Groq summarize failed: {e}")
        return content[:200]

# ─── Play Store Scraper ───────────────────────────────────
def fetch_five_star_reviews(app_id: str, count: int = 50) -> list:
    try:
        result, _ = gps.reviews(
            app_id,
            lang="en",
            country="us",
            sort=gps.Sort.NEWEST,
            count=count,
            filter_score_with=5,
        )
        return result
    except Exception as e:
        logger.error(f"❌ Failed to fetch reviews for {app_id}: {e}")
        return []

def get_app_name(app_id: str) -> str:
    try:
        info = gps.app(app_id, lang="en", country="us")
        return info.get("title", app_id)
    except Exception:
        return app_id

# ─── Core Review Checker ──────────────────────────────────
async def check_reviews_for_app(bot: Bot, app_config: dict):
    app_id   = app_config["app_id"]
    tg_group = app_config.get("tg_group", "")
    wa_group = app_config.get("wa_group", "")

    logger.info(f"🔍 Checking reviews: {app_id}")
    reviews  = fetch_five_star_reviews(app_id)
    app_name = get_app_name(app_id)
    today    = datetime.date.today().isoformat()
    new_found = 0

    for review in reviews:
        review_id = review.get("reviewId", "")
        if not review_id:
            continue
        if is_review_seen(app_id, review_id):
            continue

        # New unseen 5-star review!
        new_found += 1
        mark_review_seen(app_id, review_id, today)
        daily_count = increment_daily_count(app_id, today)

        # Generate screenshot
        screenshot = generate_review_screenshot(review, app_name)

        # AI summary
        ai_summary = ai_summarize_review(review.get("content", ""))

        at_str = review.get("at", "")
        if hasattr(at_str, "strftime"):
            at_str = at_str.strftime("%d %b %Y %I:%M %p")

        caption = (
            f"⭐⭐⭐⭐⭐ *New 5-Star Review!*\n\n"
            f"📱 *App:* `{app_name}`\n"
            f"👤 *User:* {review.get('userName','Anonymous')}\n"
            f"🗓 *Date:* {at_str}\n\n"
            f"💬 *Review:*\n_{review.get('content','')[:300]}_\n\n"
            f"🤖 *AI Summary:* {ai_summary}\n\n"
            f"📊 *Today's 5★ reviews:* {daily_count}"
        )

        # ── Send to Telegram group ──
        if tg_group:
            try:
                await bot.send_photo(
                    chat_id=tg_group,
                    photo=io.BytesIO(screenshot),
                    caption=caption,
                    parse_mode="Markdown",
                )
                logger.info(f"📤 Sent to TG group {tg_group}")
            except TelegramError as e:
                logger.error(f"TG send error: {e}")

        # ── Send to WhatsApp group ──
        if wa_group:
            wa_text = caption.replace("*", "").replace("_", "").replace("`", "")
            send_whatsapp_image(wa_group, screenshot, wa_text)

    if new_found:
        logger.info(f"✅ {new_found} new review(s) sent for {app_id}")
    else:
        logger.info(f"ℹ️  No new reviews for {app_id}")

async def review_check_job(context: ContextTypes.DEFAULT_TYPE):
    """Called by APScheduler every 5 minutes."""
    bot  = context.bot
    apps = get_all_apps()
    if not apps:
        logger.info("No apps configured yet.")
        return
    for app_cfg in apps:
        if app_cfg.get("active", True):
            await check_reviews_for_app(bot, app_cfg)

# ─── Daily Summary Job ────────────────────────────────────
async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    bot   = context.bot
    apps  = get_all_apps()
    today = datetime.date.today().isoformat()

    for app_cfg in apps:
        app_id   = app_cfg["app_id"]
        tg_group = app_cfg.get("tg_group", "")
        wa_group = app_cfg.get("wa_group", "")
        count    = get_daily_count(app_id, today)
        app_name = get_app_name(app_id)

        msg = (
            f"📊 *Daily Summary — {today}*\n\n"
            f"📱 *App:* `{app_name}`\n"
            f"⭐ *5-Star Reviews Today:* {count}\n\n"
            f"_Keep up the great work!_ 🚀"
        )

        if tg_group:
            try:
                await bot.send_message(tg_group, msg, parse_mode="Markdown")
            except TelegramError as e:
                logger.error(f"Daily summary TG error: {e}")

        if wa_group:
            send_whatsapp_message(wa_group, msg.replace("*","").replace("_",""))

# ─── Admin Commands ───────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"👋 *Hello, {user.first_name}!*\n\n"
        f"I'm your *Play Store Review Monitor Bot* 🤖\n\n"
        f"*Commands:*\n"
        f"🔧 /admin — Admin Panel\n"
        f"📋 /listapps — Show monitored apps\n"
        f"ℹ️  /help — Help & Info\n"
        f"📊 /status — Bot status"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Help*\n\n"
        "*Admin Commands:*\n"
        "/addapp — Add a Play Store app to monitor\n"
        "/removeapp <app_id> — Stop monitoring an app\n"
        "/listapps — List all monitored apps\n"
        "/check — Manually trigger review check\n"
        "/summary — Send daily summary now\n"
        "/status — Bot uptime & stats\n\n"
        "*How it works:*\n"
        "• Bot checks for new ⭐⭐⭐⭐⭐ reviews every 5 min\n"
        "• Screenshots are sent to your Telegram & WhatsApp groups\n"
        "• Daily count summary sent at midnight\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    apps  = get_all_apps()
    today = datetime.date.today().isoformat()
    lines = [f"🟢 *Bot is running!*\n\n📅 Date: {today}\n"]
    lines.append(f"📱 *Monitored apps:* {len(apps)}")
    for a in apps:
        count = get_daily_count(a["app_id"], today)
        lines.append(f"  • `{a['app_id']}` — {count} reviews today")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─── Admin Panel ──────────────────────────────────────────
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only command.")
        return
    keyboard = [
        [InlineKeyboardButton("➕ Add App",    callback_data="admin_addapp")],
        [InlineKeyboardButton("🗑 Remove App", callback_data="admin_removeapp")],
        [InlineKeyboardButton("📋 List Apps",  callback_data="admin_listapps")],
        [InlineKeyboardButton("🔍 Check Now",  callback_data="admin_check")],
        [InlineKeyboardButton("📊 Daily Summary", callback_data="admin_summary")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🛡 *Admin Panel*\nChoose an action:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if not is_admin(query.from_user.id):
        await query.edit_message_text("❌ Admin only.")
        return

    if data == "admin_addapp":
        await query.edit_message_text(
            "📝 Send me the *Play Store App ID*\n\n"
            "Example: `com.example.myapp`\n\n"
            "_Type /cancel to abort_",
            parse_mode="Markdown",
        )
        context.user_data["action"] = "add_app_id"
        return WAITING_APP_ID

    elif data == "admin_removeapp":
        apps = get_all_apps()
        if not apps:
            await query.edit_message_text("No apps configured.")
            return
        keyboard = [
            [InlineKeyboardButton(f"🗑 {a['app_id']}", callback_data=f"del_{a['app_id']}")]
            for a in apps
        ]
        await query.edit_message_text(
            "Select app to remove:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("del_"):
        app_id = data[4:]
        delete_app(app_id)
        await query.edit_message_text(f"✅ `{app_id}` removed.", parse_mode="Markdown")

    elif data == "admin_listapps":
        apps = get_all_apps()
        if not apps:
            await query.edit_message_text("No apps configured yet.")
            return
        lines = ["📋 *Monitored Apps:*\n"]
        for a in apps:
            lines.append(
                f"• `{a['app_id']}`\n"
                f"  📢 TG: `{a.get('tg_group','—')}`\n"
                f"  💬 WA: `{a.get('wa_group','—')}`"
            )
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")

    elif data == "admin_check":
        await query.edit_message_text("🔍 Checking reviews now...")
        apps = get_all_apps()
        bot  = context.bot
        for app_cfg in apps:
            await check_reviews_for_app(bot, app_cfg)
        await query.edit_message_text("✅ Review check complete!")

    elif data == "admin_summary":
        await daily_summary_job(context)
        await query.edit_message_text("✅ Daily summary sent!")

# ─── Conversation: Add App ────────────────────────────────
async def conv_app_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_id = update.message.text.strip()
    context.user_data["new_app_id"] = app_id
    await update.message.reply_text(
        f"✅ App ID: `{app_id}`\n\n"
        f"Now send the *Telegram Group ID or @username* where reviews should be posted.\n\n"
        f"_To get group ID: forward a message from the group to @userinfobot_\n"
        f"_Type /skip to skip TG group_",
        parse_mode="Markdown",
    )
    return WAITING_TG_GROUP

async def conv_tg_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_group = update.message.text.strip()
    if tg_group.lower() == "/skip":
        tg_group = ""
    context.user_data["new_tg_group"] = tg_group
    await update.message.reply_text(
        f"✅ TG Group: `{tg_group or 'Skipped'}`\n\n"
        f"Now send the *WhatsApp Group ID* (Green API format).\n"
        f"Example: `120363XXXXXXXXXX@g.us`\n\n"
        f"_Type /skip to skip WhatsApp_",
        parse_mode="Markdown",
    )
    return WAITING_WA_GROUP

async def conv_wa_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wa_group = update.message.text.strip()
    if wa_group.lower() == "/skip":
        wa_group = ""
    app_id   = context.user_data.get("new_app_id", "")
    tg_group = context.user_data.get("new_tg_group", "")

    save_app(app_id, tg_group, wa_group)
    await update.message.reply_text(
        f"🎉 *App added successfully!*\n\n"
        f"📱 App ID: `{app_id}`\n"
        f"📢 TG Group: `{tg_group or '—'}`\n"
        f"💬 WA Group: `{wa_group or '—'}`\n\n"
        f"Bot will check for new 5★ reviews every *5 minutes*.",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    return ConversationHandler.END

async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Operation cancelled.")
    return ConversationHandler.END

# ─── List Apps & Manual Check Commands ───────────────────
async def cmd_listapps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    apps = get_all_apps()
    if not apps:
        await update.message.reply_text("No apps configured yet. Use /admin → Add App.")
        return
    lines = ["📋 *Monitored Apps:*\n"]
    for a in apps:
        lines.append(
            f"• `{a['app_id']}`\n"
            f"  📢 TG: `{a.get('tg_group','—')}`\n"
            f"  💬 WA: `{a.get('wa_group','—')}`"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    await update.message.reply_text("🔍 Triggering review check...")
    apps = get_all_apps()
    for app_cfg in apps:
        await check_reviews_for_app(context.bot, app_cfg)
    await update.message.reply_text("✅ Check complete!")

async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    await daily_summary_job(context)
    await update.message.reply_text("✅ Summary sent!")

# ─── Error Handler ────────────────────────────────────────
async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled error: {context.error}", exc_info=True)

# ─── Main Entry Point ─────────────────────────────────────
def main():
    logger.info("🚀 Starting Play Store Review Bot...")

    # Start keep-alive HTTP server
    start_keep_alive()

    # Start self-ping loop
    start_self_ping()

    # Build Telegram app
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    # ── Conversation Handler (Add App) ──
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_callback, pattern="^admin_addapp$")
        ],
        states={
            WAITING_APP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_app_id)],
            WAITING_TG_GROUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_tg_group),
                CommandHandler("skip", conv_tg_group),
            ],
            WAITING_WA_GROUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_wa_group),
                CommandHandler("skip", conv_wa_group),
            ],
        },
        fallbacks=[CommandHandler("cancel", conv_cancel)],
        per_user=True,
    )

    # ── Register Handlers ──
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("admin",      cmd_admin))
    app.add_handler(CommandHandler("listapps",   cmd_listapps))
    app.add_handler(CommandHandler("check",      cmd_check))
    app.add_handler(CommandHandler("summary",    cmd_summary))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CallbackQueryHandler(admin_callback))
    app.add_error_handler(error_handler)

    # ── Scheduled Jobs ──
    job_queue = app.job_queue
    # Every 5 minutes — review check
    job_queue.run_repeating(review_check_job, interval=300, first=30, name="review_check")
    # Every day at 23:59 — daily summary
    job_queue.run_daily(
        daily_summary_job,
        time=datetime.time(23, 59, 0),
        name="daily_summary",
    )

    logger.info("✅ Bot is ready. Starting polling...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
