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
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
from PIL import Image, ImageDraw, ImageFont
import google_play_scraper as gps
from telegram import (
    Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
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

# ─── Environment Variables ────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
FIREBASE_CRED_JSON = os.environ["FIREBASE_CREDENTIALS_JSON"]
ADMIN_IDS          = list(map(int, os.environ.get("ADMIN_IDS", "").split(","))) if os.environ.get("ADMIN_IDS") else []

# CallMeBot & ImgBB Environment Variables
CALLMEBOT_API_KEY  = os.environ.get("CALLMEBOT_API_KEY", "")
WA_PHONE_NUMBER    = os.environ.get("WA_PHONE_NUMBER", "")
IMGBB_API_KEY      = os.environ.get("IMGBB_API_KEY", "")

PORT               = int(os.environ.get("PORT", 8080))
RENDER_URL         = os.environ.get("RENDER_URL", "")

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

# ─── Groq AI ──────────────────────────────────────────────
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
    """Returns 'NEW', 'DUPLICATE', or 'UPDATED'"""
    doc_ref = db.collection("seen_reviews").document(f"{app_id}_{review_id}")
    doc = doc_ref.get()
    if doc.exists:
        old_content = doc.to_dict().get("content", "")
        if old_content != content:
            return "UPDATED"
        return "DUPLICATE"
    return "NEW"

def mark_review_seen(app_id: str, review_id: str, date_str: str, content: str):
    db.collection("seen_reviews").document(f"{app_id}_{review_id}").set({
        "app_id": app_id, "review_id": review_id,
        "content": content,
        "date": date_str, "seen_at": firestore.SERVER_TIMESTAMP,
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

# ─── Screenshot Generator ─────────────────────────────────
def generate_screenshot(review: dict, app_name: str) -> bytes:
    W, H = 800, 430
    img  = Image.new("RGB", (W, H), (245, 248, 255))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle([18, 18, W-18, H-18], radius=20, fill=(210, 218, 240))
    draw.rounded_rectangle([14, 14, W-14, H-14], radius=20, fill=(255, 255, 255))
    draw.rounded_rectangle([14, 14, W-14, 68],   radius=20, fill=(48, 63, 159))
    draw.rectangle([14, 48, W-14, 68], fill=(48, 63, 159))

    try:
        fb = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        fr = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        f_big   = ImageFont.truetype(fb, 20)
        f_med   = ImageFont.truetype(fr, 16)
        f_small = ImageFont.truetype(fr, 13)
    except Exception:
        f_big = f_med = f_small = ImageFont.load_default()

    draw.text((28, 28), f"📱  {app_name}", fill=(255, 255, 255), font=f_big)

    score = int(review.get("score", 5))
    draw.text((28, 82), "★" * score + "☆" * (5 - score), fill=(255, 193, 7), font=f_big)

    user = review.get("userName", "Anonymous")
    at   = review.get("at", "")
    if hasattr(at, "strftime"):
        at = at.strftime("%d %b %Y  %I:%M %p")
    draw.text((28, 120),    f"👤  {user}", fill=(30, 30, 50),    font=f_med)
    draw.text((W-230, 120), f"🗓  {at}",   fill=(100, 110, 130), font=f_small)
    draw.line([(28, 152), (W-28, 152)], fill=(220, 225, 240), width=2)

    content = review.get("content", "").strip() or "(No text)"
    words, lines, cur = content.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > 82:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    y = 165
    for ln in lines[:6]:
        draw.text((28, y), ln, fill=(30, 30, 50), font=f_med)
        y += 28

    thumbs = review.get("thumbsUpCount", 0)
    draw.text((28, H-58), f"👍  {thumbs} people found this helpful", fill=(100,110,130), font=f_small)
    draw.rounded_rectangle([W-130, H-68, W-18, H-38], radius=12, fill=(34, 197, 94))
    draw.text((W-118, H-60), "🟢  LIVE", fill=(255, 255, 255), font=f_small)
    draw.text((28, H-30), "Play Store Review Monitor Bot", fill=(150, 160, 180), font=f_small)

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
    
    # If no target_date is provided, use today's date
    search_date = target_date if target_date else datetime.date.today().isoformat()
    is_custom_search = target_date is not None

    logger.info(f"🔍 Checking: {app_id} for date: {search_date}")
    reviews  = fetch_reviews(app_id, count=300 if is_custom_search else 50)
    app_name = get_app_name(app_id)
    
    found_any = False

    for review in reviews:
        rid = review.get("reviewId", "")
        content = review.get("content", "").strip()
        
        # Date filtering: Only process reviews matching the search date
        rev_datetime = review.get("at")
        if rev_datetime and rev_datetime.date().isoformat() != search_date:
            continue
            
        if not rid:
            continue

        status = check_review_status(app_id, rid, content)
        
        # Ignore exactly duplicate reviews. Proceed only if NEW or UPDATED.
        if status == "DUPLICATE":
            continue

        found_any = True
        mark_review_seen(app_id, rid, search_date, content)
        
        # Only increment daily count for today's normal checks
        daily = increment_daily_count(app_id, search_date) if not is_custom_search else "N/A (Custom Search)"
        
        shot    = generate_screenshot(review, app_name)
        summary = ai_summary(content)

        at_str = rev_datetime.strftime("%d %b %Y  %I:%M %p") if hasattr(rev_datetime, "strftime") else ""

        # Update Notice
        update_tag = "🔄 *[Updated/Edited Review]*\n\n" if status == "UPDATED" else ""

        # ── Telegram Payload (English) ──
        tg_caption = (
            f"{update_tag}"
            f"⭐⭐⭐⭐⭐ *New 5-Star Review!*\n\n"
            f"📱 *App:* `{app_name}`\n"
            f"👤 *User:* {review.get('userName','Anonymous')}\n"
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

        # ── WhatsApp / ImgBB Payload (English) ──
        imgbb_url = upload_to_imgbb(shot)
        
        wa_text = (
            f"{update_tag.replace('*', '')}"
            f"⭐⭐⭐⭐⭐ *New 5-Star Review!*\n\n"
            f"📱 *App Name:* {app_name}\n"
            f"👤 *User Name:* {review.get('userName', 'Anonymous')}\n"
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
            await check_app(context.bot, cfg) # target_date defaults to today

# ─── Daily Summary (12:01 AM for Previous Day) ────────────
async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    # Since it runs at 00:01, we want the summary of yesterday
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    
    for cfg in get_all_apps():
        app_id   = cfg["app_id"]
        tg_group = cfg.get("tg_group", "")
        count    = get_daily_count(app_id, yesterday)
        name     = get_app_name(app_id)

        msg = (
            f"📊 *Daily Summary — {yesterday}*\n\n"
            f"📱 *App:* {name}\n"
            f"⭐ *Total 5-Star Reviews:* {count}\n\n"
            f"Keep up the great work! 🚀"
        )
        
        if tg_group:
            try:
                tg_msg = msg.replace(name, f"`{name}`").replace("Keep up the great work!", "_Keep up the great work!_")
                await context.bot.send_message(tg_group, tg_msg, parse_mode="Markdown")
            except TelegramError as e:
                pass
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
    today = datetime.date.today().isoformat()
    lines = [f"🟢 *Bot চালু আছে!*\n📅 {today}\n📱 অ্যাপ: {len(apps)}টি\n"]
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

    # Main Menu Back Button Handle
    if data == "admin_main":
        kb = [
            [InlineKeyboardButton("➕ অ্যাপ যোগ করুন",    callback_data="admin_add")],
            [InlineKeyboardButton("🗑 অ্যাপ সরান",         callback_data="admin_remove")],
            [InlineKeyboardButton("📋 অ্যাপ তালিকা",       callback_data="admin_list")],
            [InlineKeyboardButton("🔍 নির্দিষ্ট অ্যাপ চেক",   callback_data="admin_check_menu")],
            [InlineKeyboardButton("📅 নির্দিষ্ট তারিখ সার্চ", callback_data="admin_search_menu")],
        ]
        await q.edit_message_text(
            "🛡 *Admin প্যানেল*\nএকটি অপশন বেছে নিন:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown",
        )
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
            lines.append(
                f"• `{a['app_id']}`\n"
                f"  📢 TG: `{a.get('tg_group','—')}`\n"
                f"  💬 WA: `{a.get('wa_link','—')}`"
            )
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([get_back_kb()]))

    # --- Individual App Check System ---
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
        await q.edit_message_text(f"⏳ `{app_id}` এর আজকের নতুন রিভিউ চেক করা হচ্ছে...", parse_mode="Markdown")
        
        apps = get_all_apps()
        cfg = next((item for item in apps if item["app_id"] == app_id), None)
        if cfg:
            found = await check_app(context.bot, cfg) # Checks for today by default
            if found:
                await q.edit_message_text(f"✅ `{app_id}` এর নতুন রিভিউ সফলভাবে গ্রুপে পাঠানো হয়েছে।", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([get_back_kb()]))
            else:
                await q.edit_message_text(f"ℹ️ `{app_id}` এ আজকে নতুন কোনো রিভিউ পাওয়া যায়নি।", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([get_back_kb()]))
        else:
            await q.edit_message_text("❌ অ্যাপটি খুঁজে পাওয়া যায়নি।", reply_markup=InlineKeyboardMarkup([get_back_kb()]))

    # --- Specific Date Search System ---
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
    
    # Simple Date Format validation
    try:
        datetime.datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        await update.message.reply_text("❌ তারিখের ফরম্যাট ভুল! দয়া করে `YYYY-MM-DD` ফরম্যাটে লিখুন (যেমন: 2024-05-20)। /cancel দিয়ে বের হতে পারেন।")
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
        f"✅ বট এখন থেকে শুধু **আজকের** নতুন ৫★ রিভিউগুলো Telegram এবং আপনার কনফিগার করা WhatsApp নাম্বারে পাঠাবে।\n\n"
        f"*(নোট: CallMeBot API সরাসরি গ্রুপ লিংকে মেসেজ পাঠাতে পারে না, এটি আপনার সংযুক্ত পার্সোনাল নাম্বারে মেসেজটি পাঠাবে। লিংকটি ডাটাবেসে সেভ রাখা হয়েছে ভবিষ্যতের আপডেটের জন্য।)*",
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

    # Conversation: Manage States
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
    # 5 minutes auto check
    jq.run_repeating(review_check_job, interval=300, first=30, name="review_check")
    # Daily summary exactly at 12:01 AM (00:01:00)
    jq.run_daily(daily_summary_job, time=datetime.time(0, 1, 0), name="daily_summary")

    logger.info("✅ Bot polling শুরু হয়েছে!")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
