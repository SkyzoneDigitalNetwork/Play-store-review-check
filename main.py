import os
import sys
import asyncio
import logging
import threading
import time
import json
import datetime
import io
import base64
import urllib.parse
import hashlib
import csv
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import pytz
from PIL import Image, ImageDraw, ImageFont
import google_play_scraper as gps
from telegram import (
    Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
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
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ReviewBot")

# ─── Environment Variables & Timezone ─────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
FIREBASE_CRED_JSON = os.environ["FIREBASE_CREDENTIALS_JSON"]
ADMIN_IDS          = list(map(int, os.environ.get("ADMIN_IDS", "").split(","))) if os.environ.get("ADMIN_IDS") else []

CALLMEBOT_API_KEY  = os.environ.get("CALLMEBOT_API_KEY", "")
WA_PHONE_NUMBER    = os.environ.get("WA_PHONE_NUMBER", "")
IMGBB_API_KEY      = os.environ.get("IMGBB_API_KEY", "")

PORT               = int(os.environ.get("PORT", 8080))
RENDER_URL         = os.environ.get("RENDER_URL", "")

# Bangladesh Timezone
BD_TZ = pytz.timezone("Asia/Dhaka")

# Conversation states
WAITING_APP_ID, WAITING_TG_GROUP, WAITING_WA_LINK, WAITING_SEARCH_APP, WAITING_SEARCH_DATE = range(5)

# ─── Firebase Init ────────────────────────────────────────
def init_firebase():
    cred_dict = json.loads(FIREBASE_CRED_JSON)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
    logger.info("✅ Firebase initialized")
    return firestore.client()

db = init_firebase()
groq_client = Groq(api_key=GROQ_API_KEY)

# ─── Keep-Alive HTTP Server (Render 24/7) ─────────────────
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def log_message(self, format, *args):
        pass

def start_keep_alive():
    def run():
        HTTPServer(("0.0.0.0", PORT), PingHandler).serve_forever()
    threading.Thread(target=run, daemon=True).start()
    logger.info(f"🌐 Keep-alive server on port {PORT}")

def start_self_ping():
    def loop():
        if not RENDER_URL:
            return
        while True:
            try:
                requests.get(RENDER_URL, timeout=10)
            except Exception:
                pass
            time.sleep(240)
    threading.Thread(target=loop, daemon=True).start()

# ─── ImgBB API ────────────────────────────────────────────
def upload_to_imgbb(image_bytes: bytes) -> str:
    if not IMGBB_API_KEY:
        return "No link available (API key missing)"
    url = "https://api.imgbb.com/1/upload"
    payload = {
        "key": IMGBB_API_KEY,
        "image": base64.b64encode(image_bytes).decode('utf-8')
    }
    try:
        res = requests.post(url, data=payload, timeout=20)
        res.raise_for_status()
        return res.json()["data"]["url"]
    except Exception as e:
        logger.error(f"❌ ImgBB upload failed: {e}")
        return "Upload failed"

# ─── CallMeBot WhatsApp API ───────────────────────────────
def send_callmebot_wa(text: str):
    if not CALLMEBOT_API_KEY or not WA_PHONE_NUMBER:
        return
    encoded_text = urllib.parse.quote(text)
    url = f"https://api.callmebot.com/whatsapp.php?phone={WA_PHONE_NUMBER}&text={encoded_text}&apikey={CALLMEBOT_API_KEY}"
    try:
        requests.get(url, timeout=15)
    except Exception as e:
        logger.error(f"❌ WA text send failed: {e}")

# ─── Firebase Helpers ─────────────────────────────────────
def get_all_apps():
    return [{"id": d.id, **d.to_dict()} for d in db.collection("apps").stream()]

def save_app(app_id: str, tg_group: str, wa_link: str):
    db.collection("apps").document(app_id).set({
        "app_id":    app_id,
        "tg_group":  tg_group,
        "wa_link":   wa_link,
        "added_at":  firestore.SERVER_TIMESTAMP,
        "active":    True,
    }, merge=True)

def delete_app(app_id: str):
    db.collection("apps").document(app_id).delete()

def check_review_status(app_id: str, review_id: str, content: str) -> str:
    """Returns 'NEW', 'DUPLICATE_ID', or 'UPDATED'"""
    doc_ref = db.collection("seen_reviews").document(f"{app_id}_{review_id}")
    doc = doc_ref.get()
    if doc.exists:
        old_content = doc.to_dict().get("content", "")
        if old_content != content:
            return "UPDATED"
        return "DUPLICATE_ID"
    return "NEW"

def check_and_mark_duplicate_text(app_id: str, content: str) -> bool:
    """Check if exact text is used in another review (Duplicate Text Alert)"""
    if not content or len(content.strip()) < 5:
        return False
    content_hash = hashlib.md5(content.strip().lower().encode('utf-8')).hexdigest()
    doc_ref = db.collection("text_hashes").document(f"{app_id}_{content_hash}")
    if doc_ref.get().exists:
        return True
    doc_ref.set({"exists": True, "created_at": firestore.SERVER_TIMESTAMP})
    return False

def save_full_review_data(app_id: str, review_id: str, user_name: str, content: str, original_date: str, processed_date: str, imgbb_url: str, is_dup_text: bool):
    db.collection("seen_reviews").document(f"{app_id}_{review_id}").set({
        "app_id": app_id, 
        "review_id": review_id,
        "user_name": user_name,
        "content": content,
        "original_date": original_date,
        "processed_date": processed_date, # Date it was caught by bot (BD Time)
        "imgbb_url": imgbb_url,
        "is_duplicate_text": is_dup_text,
        "seen_at": firestore.SERVER_TIMESTAMP,
    })

def increment_daily_count(app_id: str, date_str: str) -> int:
    ref = db.collection("daily_counts").document(f"{app_id}_{date_str}")
    doc = ref.get()
    count = (doc.to_dict().get("count", 0) + 1) if doc.exists else 1
    ref.set({"app_id": app_id, "date": date_str, "count": count})
    return count

def get_daily_count(app_id: str, date_str: str) -> int:
    doc = db.collection("daily_counts").document(f"{app_id}_{date_str}").get()
    return doc.to_dict().get("count", 0) if doc.exists else 0

# ─── Screenshot Generator (Clean Play Store UI) ───────────
def generate_screenshot(review: dict, app_name: str) -> bytes:
    W, H = 800, 360  # Reduced height, removed bottom texts
    img  = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    # Simple clean borders
    draw.rectangle([0, 0, W, 70], fill=(255, 255, 255))
    draw.line([(0, 70), (W, 70)], fill=(230, 230, 230), width=1)

    try:
        fb = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        fr = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        f_app   = ImageFont.truetype(fb, 22)
        f_big   = ImageFont.truetype(fb, 20)
        f_med   = ImageFont.truetype(fr, 17)
        f_small = ImageFont.truetype(fr, 14)
    except Exception:
        f_app = f_big = f_med = f_small = ImageFont.load_default()

    draw.text((28, 25), f"{app_name}", fill=(32, 33, 36), font=f_app)

    user = review.get("userName", "Anonymous")
    draw.text((28, 95), f"{user}", fill=(32, 33, 36), font=f_med)
    
    score = int(review.get("score", 5))
    draw.text((28, 130), "★" * score + "☆" * (5 - score), fill=(1, 135, 95), font=f_big) # Play store green star color

    at = review.get("at", "")
    if hasattr(at, "strftime"):
        at = at.strftime("%d %b %Y")
    draw.text((150, 134), f"{at}", fill=(95, 99, 104), font=f_small)

    content = review.get("content", "").strip() or "(No text)"
    words, lines, cur = content.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > 85:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
        
    y = 180
    for ln in lines[:5]:
        draw.text((28, y), ln, fill=(60, 64, 67), font=f_med)
        y += 28

    thumbs = review.get("thumbsUpCount", 0)
    if thumbs > 0:
        draw.text((28, H-40), f"{thumbs} people found this helpful", fill=(95, 99, 104), font=f_small)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()

# ─── Groq AI Summary ──────────────────────────────────────
def ai_summary(content: str) -> str:
    try:
        resp = groq_client.chat.completions.create(
            model="llama3-8b-8192",
            messages=[
                {"role": "system", "content": "Summarize this Play Store review in 1-2 sentences. Be concise and positive."},
                {"role": "user",   "content": f"Review: {content}"},
            ],
            max_tokens=100,
            temperature=0.5,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return content[:180]

# ─── Play Store Fetch ─────────────────────────────────────
def fetch_reviews(app_id: str, count=100) -> list:
    try:
        result, _ = gps.reviews(
            app_id, lang="en", country="us",
            sort=gps.Sort.NEWEST, count=count, filter_score_with=5,
        )
        return result
    except Exception as e:
        logger.error(f"Scrape failed for {app_id}: {e}")
        return []

def get_app_name(app_id: str) -> str:
    try:
        return gps.app(app_id, lang="en", country="us").get("title", app_id)
    except Exception:
        return app_id

# ─── Core Review Checker (English Only for Groups) ────────
async def check_app(bot: Bot, cfg: dict, target_date: str = None):
    app_id   = cfg["app_id"]
    tg_group = cfg.get("tg_group", "")
    
    # BD Timezone Processed Date
    processed_date = datetime.datetime.now(BD_TZ).date().isoformat()
    is_custom_search = target_date is not None

    logger.info(f"🔍 Checking: {app_id}")
    reviews  = fetch_reviews(app_id, count=300 if is_custom_search else 50)
    app_name = get_app_name(app_id)
    
    found_any = False

    for review in reviews:
        rid = review.get("reviewId", "")
        content = review.get("content", "").strip()
        user_name = review.get('userName', 'Anonymous')
        
        rev_datetime = review.get("at")
        original_date = rev_datetime.date().isoformat() if rev_datetime else processed_date
        
        # If it's a custom date search, strictly filter by date
        if is_custom_search and original_date != target_date:
            continue
            
        if not rid:
            continue

        status = check_review_status(app_id, rid, content)
        if status == "DUPLICATE_ID":
            continue

        found_any = True
        
        # AI / Duplicate Check
        is_dup_text = check_and_mark_duplicate_text(app_id, content)
        dup_warning = "⚠️ *[WARNING: Duplicate Review Text]*\n" if is_dup_text else ""
        update_tag = "🔄 *[Updated/Edited Review]*\n" if status == "UPDATED" else ""

        shot = generate_screenshot(review, app_name)
        imgbb_url = upload_to_imgbb(shot)
        
        # Save all details to Firebase for the report
        save_full_review_data(app_id, rid, user_name, content, original_date, processed_date if not is_custom_search else target_date, imgbb_url, is_dup_text)
        
        daily = increment_daily_count(app_id, processed_date) if not is_custom_search else "N/A"
        summary = ai_summary(content)

        at_str = rev_datetime.strftime("%d %b %Y  %I:%M %p") if hasattr(rev_datetime, "strftime") else ""

        # ── Telegram Payload ──
        tg_caption = (
            f"{dup_warning}{update_tag}"
            f"⭐⭐⭐⭐⭐ *New 5-Star Review!*\n\n"
            f"📱 *App:* `{app_name}`\n"
            f"👤 *User:* {user_name}\n"
            f"🗓 *Date:* {at_str}\n\n"
            f"💬 *Review:*\n_{content[:300]}_\n\n"
            f"🤖 *AI Summary:* {summary}\n\n"
            f"📊 *Daily 5★ Reviews:* {daily}"
        )

        if tg_group:
            try:
                await bot.send_photo(
                    chat_id=tg_group,
                    photo=io.BytesIO(shot),
                    caption=tg_caption,
                    parse_mode="Markdown",
                )
            except TelegramError as e:
                logger.error(f"TG error: {e}")

        # ── WhatsApp Payload ──
        wa_text = (
            f"{dup_warning.replace('*', '')}{update_tag.replace('*', '')}"
            f"⭐⭐⭐⭐⭐ *New 5-Star Review!*\n\n"
            f"📱 *App Name:* {app_name}\n"
            f"👤 *User Name:* {user_name}\n"
            f"⭐ *Rating:* {int(review.get('score', 5))} Stars\n\n"
            f"💬 *Review:*\n{content[:300]}\n\n"
            f"🤖 *AI Summary:* {summary}\n\n"
            f"📊 *Daily 5★ Reviews:* {daily}\n\n"
            f"🖼️ *Screenshot:* {imgbb_url}"
        )
        send_callmebot_wa(wa_text)
        
    return found_any

async def review_check_job(context: ContextTypes.DEFAULT_TYPE):
    apps = get_all_apps()
    for cfg in apps:
        if cfg.get("active", True):
            await check_app(context.bot, cfg)

# ─── Daily Summary & File Upload (12:01 AM BD Time) ───────
async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    # Yesterday in BD Time
    yesterday = (datetime.datetime.now(BD_TZ).date() - datetime.timedelta(days=1)).isoformat()
    
    for cfg in get_all_apps():
        app_id   = cfg["app_id"]
        tg_group = cfg.get("tg_group", "")
        count    = get_daily_count(app_id, yesterday)
        name     = get_app_name(app_id)

        msg = (
            f"📊 *Daily Report — {yesterday}*\n\n"
            f"📱 *App:* {name}\n"
            f"⭐ *Total 5-Star Reviews:* {count}\n\n"
            f"Attached is the detailed report file. 🚀"
        )
        
        # Build CSV Data
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["User Name", "Original Date", "Processed Date", "Review Text", "Screenshot Link", "Is Duplicate Text?"])
        
        # Fetch data for this app
        reviews_ref = db.collection("seen_reviews").where("app_id", "==", app_id).stream()
        found_data = False
        
        for r in reviews_ref:
            data = r.to_dict()
            # Filter by processed_date == yesterday
            if data.get("processed_date") == yesterday:
                found_data = True
                writer.writerow([
                    data.get("user_name", "Anonymous"),
                    data.get("original_date", ""),
                    data.get("processed_date", ""),
                    data.get("content", ""),
                    data.get("imgbb_url", ""),
                    "YES (Warning)" if data.get("is_duplicate_text") else "No"
                ])
                
        # Send to Telegram
        if tg_group:
            try:
                tg_msg = msg.replace(name, f"`{name}`")
                
                if found_data:
                    # Convert String buffer to Bytes buffer for Telegram
                    byte_buffer = io.BytesIO(csv_buffer.getvalue().encode('utf-8'))
                    byte_buffer.name = f"{app_id}_report_{yesterday}.csv"
                    
                    await context.bot.send_document(
                        chat_id=tg_group,
                        document=byte_buffer,
                        caption=tg_msg,
                        parse_mode="Markdown"
                    )
                else:
                    await context.bot.send_message(tg_group, tg_msg + "\n_(No detailed data found for yesterday)_", parse_mode="Markdown")
            except TelegramError as e:
                logger.error(f"Daily TG error: {e}")
                
        # WhatsApp Summary
        send_callmebot_wa(msg.replace("*", ""))

# ─── Helpers ──────────────────────────────────────────────
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def get_back_kb():
    return [InlineKeyboardButton("⬅️ Back", callback_data="admin_main")]

# ─── Commands (Bengali for Admin/Private Chats) ───────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 *স্বাগতম!*\n\n"
        f"আমি আপনার *Play Store Review Monitor Bot* 🤖\n\n"
        f"/admin — Admin প্যানেল\n"
        f"/status — বটের অবস্থা",
        parse_mode="Markdown",
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    apps  = get_all_apps()
    today = datetime.datetime.now(BD_TZ).date().isoformat()
    lines = [f"🟢 *Bot চালু আছে!*\n📅 {today} (BD Time)\n📱 অ্যাপ: {len(apps)}টি\n"]
    for a in apps:
        count = get_daily_count(a["app_id"], today)
        lines.append(f"• `{a['app_id']}` — আজ {count}টি রিভিউ")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─── Admin Panel ──────────────────────────────────────────
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ শুধু Admin ব্যবহার করতে পারবেন।")
        return
    kb = [
        [InlineKeyboardButton("➕ অ্যাপ যোগ করুন",    callback_data="admin_add")],
        [InlineKeyboardButton("🗑 অ্যাপ সরান",         callback_data="admin_remove")],
        [InlineKeyboardButton("📋 অ্যাপ তালিকা",       callback_data="admin_list")],
        [InlineKeyboardButton("🔍 নির্দিষ্ট অ্যাপ চেক",   callback_data="admin_check_menu")],
        [InlineKeyboardButton("📅 নির্দিষ্ট তারিখ সার্চ", callback_data="admin_search_menu")],
    ]
    await update.message.reply_text(
        "🛡 *Admin প্যানেল*\nএকটি অপশন বেছে নিন:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )

async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if not is_admin(q.from_user.id):
        await q.edit_message_text("❌ Admin only.")
        return

    if data == "admin_main":
        kb = [
            [InlineKeyboardButton("➕ অ্যাপ যোগ করুন",    callback_data="admin_add")],
            [InlineKeyboardButton("🗑 অ্যাপ সরান",         callback_data="admin_remove")],
            [InlineKeyboardButton("📋 অ্যাপ তালিকা",       callback_data="admin_list")],
            [InlineKeyboardButton("🔍 নির্দিষ্ট অ্যাপ চেক",   callback_data="admin_check_menu")],
            [InlineKeyboardButton("📅 নির্দিষ্ট তারিখ সার্চ", callback_data="admin_search_menu")],
        ]
        await q.edit_message_text("🛡 *Admin প্যানেল*\nএকটি অপশন বেছে নিন:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        return

    if data == "admin_add":
        await q.edit_message_text(
            "📝 *Play Store App ID* পাঠান\n\n"
            "উদাহরণ: `com.whatsapp`\n\n"
            "_বাতিল করতে /cancel_",
            parse_mode="Markdown",
        )
        return WAITING_APP_ID

    elif data == "admin_remove":
        apps = get_all_apps()
        if not apps:
            await q.edit_message_text("কোনো অ্যাপ নেই।", reply_markup=InlineKeyboardMarkup([get_back_kb()]))
            return
        kb = [[InlineKeyboardButton(f"🗑 {a['app_id']}", callback_data=f"del_{a['app_id']}")] for a in apps]
        kb.append(get_back_kb())
        await q.edit_message_text("কোন অ্যাপটি সরাবেন?", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("del_"):
        delete_app(data[4:])
        await q.edit_message_text(f"✅ `{data[4:]}` সরানো হয়েছে।", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([get_back_kb()]))

    elif data == "admin_list":
        apps = get_all_apps()
        if not apps:
            await q.edit_message_text("কোনো অ্যাপ যোগ করা হয়নি।", reply_markup=InlineKeyboardMarkup([get_back_kb()]))
            return
        lines = ["📋 *মনিটরড অ্যাপসমূহ:*\n"]
        for a in apps:
            lines.append(f"• `{a['app_id']}`\n  📢 TG: `{a.get('tg_group','—')}`\n  💬 WA: `{a.get('wa_link','—')}`")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([get_back_kb()]))

    elif data == "admin_check_menu":
        apps = get_all_apps()
        if not apps:
            await q.edit_message_text("কোনো অ্যাপ নেই।", reply_markup=InlineKeyboardMarkup([get_back_kb()]))
            return
        kb = [[InlineKeyboardButton(f"🔍 {a['app_id']}", callback_data=f"chk_{a['app_id']}")] for a in apps]
        kb.append(get_back_kb())
        await q.edit_message_text("কোন অ্যাপটি চেক করতে চান?", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("chk_"):
        app_id = data[4:]
        await q.edit_message_text(f"⏳ `{app_id}` এর নতুন রিভিউ চেক করা হচ্ছে...", parse_mode="Markdown")
        apps = get_all_apps()
        cfg = next((item for item in apps if item["app_id"] == app_id), None)
        if cfg:
            found = await check_app(context.bot, cfg)
            if found:
                await q.edit_message_text(f"✅ `{app_id}` এর নতুন রিভিউ সফলভাবে গ্রুপে পাঠানো হয়েছে।", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([get_back_kb()]))
            else:
                await q.edit_message_text(f"ℹ️ `{app_id}` এ নতুন কোনো রিভিউ পাওয়া যায়নি।", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([get_back_kb()]))
        else:
            await q.edit_message_text("❌ অ্যাপটি খুঁজে পাওয়া যায়নি।", reply_markup=InlineKeyboardMarkup([get_back_kb()]))

    elif data == "admin_search_menu":
        apps = get_all_apps()
        if not apps:
            await q.edit_message_text("কোনো অ্যাপ নেই।", reply_markup=InlineKeyboardMarkup([get_back_kb()]))
            return
        kb = [[InlineKeyboardButton(f"📅 {a['app_id']}", callback_data=f"srch_{a['app_id']}")] for a in apps]
        kb.append(get_back_kb())
        await q.edit_message_text("কোন অ্যাপের জন্য নির্দিষ্ট তারিখ সার্চ করবেন?", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("srch_"):
        context.user_data["search_app"] = data[5:]
        await q.edit_message_text(
            f"📅 অ্যাপ: `{context.user_data['search_app']}`\n\n"
            f"দয়া করে সার্চ করার **তারিখ** পাঠান।\n"
            f"ফরম্যাট: `YYYY-MM-DD` (যেমন: `2024-05-20`)\n\n"
            f"_বাতিল করতে /cancel_",
            parse_mode="Markdown"
        )
        return WAITING_SEARCH_DATE

# ─── Conversation: Search By Date ────────────────────────
async def conv_search_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.strip()
    app_id = context.user_data.get("search_app")
    
    try:
        datetime.datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        await update.message.reply_text("❌ তারিখের ফরম্যাট ভুল! দয়া করে `YYYY-MM-DD` ফরম্যাটে লিখুন। /cancel দিয়ে বের হতে পারেন।")
        return WAITING_SEARCH_DATE

    await update.message.reply_text(f"⏳ `{app_id}` এর `{date_str}` তারিখের রিভিউ খোঁজা হচ্ছে...", parse_mode="Markdown")
    
    apps = get_all_apps()
    cfg = next((item for item in apps if item["app_id"] == app_id), None)
    
    if cfg:
        found = await check_app(context.bot, cfg, target_date=date_str)
        if found:
            await update.message.reply_text(f"✅ `{date_str}` তারিখের রিভিউগুলো সফলভাবে গ্রুপে পাঠানো হয়েছে।", reply_markup=InlineKeyboardMarkup([get_back_kb()]))
        else:
            await update.message.reply_text(f"ℹ️ `{date_str}` তারিখে কোনো ৫-স্টার রিভিউ পাওয়া যায়নি বা আগেই পাঠানো হয়েছে।", reply_markup=InlineKeyboardMarkup([get_back_kb()]))
    
    context.user_data.clear()
    return ConversationHandler.END

# ─── Conversation: Add App (3 steps) ─────────────────────
async def conv_app_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["app_id"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ App ID: `{context.user_data['app_id']}`\n\n"
        f"এখন *Telegram Group ID* পাঠান\n"
        f"_(উদাহরণ: -1001234567890)_",
        parse_mode="Markdown",
    )
    return WAITING_TG_GROUP

async def conv_tg_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tg_group"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Telegram Group: `{context.user_data['tg_group']}`\n\n"
        f"এখন *WhatsApp Group Link* পাঠান\n"
        f"_(উদাহরণ: https://chat.whatsapp.com/IiJk...)_",
        parse_mode="Markdown",
    )
    return WAITING_WA_LINK

async def conv_wa_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wa_link  = update.message.text.strip()
    tg_group = context.user_data.get("tg_group", "")
    app_id   = context.user_data.get("app_id", "")
    
    save_app(app_id, tg_group, wa_link)

    await update.message.reply_text(
        f"🎉 *অ্যাপ সফলভাবে যোগ হয়েছে!*\n\n"
        f"📱 App ID: `{app_id}`\n"
        f"📢 Telegram Group: `{tg_group}`\n"
        f"💬 WA Link Saved: `{wa_link}`\n\n"
        f"✅ বট এখন থেকে নতুন ৫★ রিভিউগুলো Telegram এবং আপনার কনফিগার করা WhatsApp নাম্বারে পাঠাবে।\n\n"
        f"*(নোট: CallMeBot API সরাসরি গ্রুপ লিংকে মেসেজ পাঠাতে পারে না, এটি পার্সোনাল নাম্বারে পাঠাবে। লিংকটি ভবিষ্যতের আপডেটের জন্য সেভ রাখা হলো।)*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([get_back_kb()])
    )
    context.user_data.clear()
    return ConversationHandler.END

async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ বাতিল করা হয়েছে।", reply_markup=InlineKeyboardMarkup([get_back_kb()]))
    return ConversationHandler.END

async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}", exc_info=True)

# ─── Main ─────────────────────────────────────────────────
def main():
    logger.info("🚀 Bot starting...")
    start_keep_alive()
    start_self_ping()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_cb, pattern="^admin_add$"),
            CallbackQueryHandler(admin_cb, pattern="^srch_.*$")
        ],
        states={
            WAITING_APP_ID:      [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_app_id)],
            WAITING_TG_GROUP:    [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_tg_group)],
            WAITING_WA_LINK:     [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_wa_link)],
            WAITING_SEARCH_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_search_date)],
        },
        fallbacks=[CommandHandler("cancel", conv_cancel)],
        per_user=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("admin",    cmd_admin))
    app.add_handler(CallbackQueryHandler(admin_cb))
    app.add_error_handler(error_handler)

    jq = app.job_queue
    jq.run_repeating(review_check_job, interval=300, first=30, name="review_check")
    
    # Run daily report at exactly 12:01 AM (Bangladesh Time)
    jq.run_daily(daily_summary_job, time=datetime.time(0, 1, 0, tzinfo=BD_TZ), name="daily_summary")

    logger.info("✅ Bot polling শুরু হয়েছে!")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
