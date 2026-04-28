import os
import sys
import asyncio
import logging
import threading
import time
import json
import datetime
import io
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
GREEN_API_INSTANCE = os.environ["GREEN_API_INSTANCE"]   # REQUIRED — WhatsApp
GREEN_API_TOKEN    = os.environ["GREEN_API_TOKEN"]       # REQUIRED — WhatsApp
PORT               = int(os.environ.get("PORT", 8080))
RENDER_URL         = os.environ.get("RENDER_URL", "")

# Conversation states
WAITING_APP_ID, WAITING_TG_GROUP, WAITING_WA_INVITE = range(3)

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
    """Ping own URL every 4 min so Render never sleeps."""
    def loop():
        if not RENDER_URL:
            return
        while True:
            try:
                requests.get(RENDER_URL, timeout=10)
                logger.debug("🔄 Self-pinged")
            except Exception as e:
                logger.warning(f"Self-ping failed: {e}")
            time.sleep(240)
    threading.Thread(target=loop, daemon=True).start()

# ─── WhatsApp via Green API ───────────────────────────────
def wa_join_group(invite_link: str) -> str:
    """
    Bot joins WhatsApp group via invite link.
    Returns group chatId (e.g. 120363XXXXXXXXXX@g.us).
    """
    url = (
        f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE}"
        f"/joinGroupByInviteLink/{GREEN_API_TOKEN}"
    )
    try:
        resp = requests.post(url, json={"inviteLink": invite_link}, timeout=20)
        data = resp.json()
        group_id = data.get("groupId", "")
        if group_id:
            logger.info(f"✅ Joined WA group: {group_id}")
            return group_id
        logger.warning(f"WA join response: {data}")
        return ""
    except Exception as e:
        logger.error(f"❌ WA join failed: {e}")
        return ""

def wa_resolve_group(invite_link: str) -> str:
    """
    Resolve group ID from invite link without joining
    (used as fallback if already a member).
    """
    url = (
        f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE}"
        f"/checkGroupInviteLink/{GREEN_API_TOKEN}"
    )
    try:
        resp = requests.post(url, json={"inviteLink": invite_link}, timeout=15)
        return resp.json().get("groupId", "")
    except Exception as e:
        logger.error(f"❌ WA resolve failed: {e}")
        return ""

def send_wa_image(group_id: str, image_bytes: bytes, caption: str):
    """Send screenshot image to WhatsApp group."""
    url = (
        f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE}"
        f"/sendFileByUpload/{GREEN_API_TOKEN}"
    )
    try:
        files = {"file": ("review.png", image_bytes, "image/png")}
        data  = {"chatId": group_id, "caption": caption}
        r = requests.post(url, files=files, data=data, timeout=30)
        r.raise_for_status()
        logger.info(f"✅ WA image sent → {group_id}")
    except Exception as e:
        logger.error(f"❌ WA image send failed: {e}")

def send_wa_text(group_id: str, text: str):
    """Send text message to WhatsApp group."""
    url = (
        f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE}"
        f"/sendMessage/{GREEN_API_TOKEN}"
    )
    try:
        r = requests.post(url, json={"chatId": group_id, "message": text}, timeout=15)
        r.raise_for_status()
        logger.info(f"✅ WA text sent → {group_id}")
    except Exception as e:
        logger.error(f"❌ WA text send failed: {e}")

# ─── Firebase Helpers ─────────────────────────────────────
def get_all_apps():
    return [{"id": d.id, **d.to_dict()} for d in db.collection("apps").stream()]

def save_app(app_id: str, tg_group: str, wa_group_id: str, wa_invite: str):
    db.collection("apps").document(app_id).set({
        "app_id":    app_id,
        "tg_group":  tg_group,
        "wa_group":  wa_group_id,
        "wa_invite": wa_invite,
        "added_at":  firestore.SERVER_TIMESTAMP,
        "active":    True,
    }, merge=True)

def delete_app(app_id: str):
    db.collection("apps").document(app_id).delete()

def is_review_seen(app_id: str, review_id: str) -> bool:
    return db.collection("seen_reviews").document(f"{app_id}_{review_id}").get().exists

def mark_review_seen(app_id: str, review_id: str, date_str: str):
    db.collection("seen_reviews").document(f"{app_id}_{review_id}").set({
        "app_id": app_id, "review_id": review_id,
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
    except Exception as e:
        logger.warning(f"Groq failed: {e}")
        return content[:180]

# ─── Play Store Fetch ─────────────────────────────────────
def fetch_reviews(app_id: str) -> list:
    try:
        result, _ = gps.reviews(
            app_id, lang="en", country="us",
            sort=gps.Sort.NEWEST, count=50, filter_score_with=5,
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

# ─── Core Review Checker ──────────────────────────────────
async def check_app(bot: Bot, cfg: dict):
    app_id   = cfg["app_id"]
    tg_group = cfg.get("tg_group", "")
    wa_group = cfg.get("wa_group", "")
    today    = datetime.date.today().isoformat()

    logger.info(f"🔍 Checking: {app_id}")
    reviews  = fetch_reviews(app_id)
    app_name = get_app_name(app_id)

    for review in reviews:
        rid = review.get("reviewId", "")
        if not rid or is_review_seen(app_id, rid):
            continue

        mark_review_seen(app_id, rid, today)
        daily   = increment_daily_count(app_id, today)
        shot    = generate_screenshot(review, app_name)
        summary = ai_summary(review.get("content", ""))

        at = review.get("at", "")
        if hasattr(at, "strftime"):
            at = at.strftime("%d %b %Y  %I:%M %p")

        tg_caption = (
            f"⭐⭐⭐⭐⭐ *নতুন ৫-স্টার রিভিউ!*\n\n"
            f"📱 *App:* `{app_name}`\n"
            f"👤 *User:* {review.get('userName','Anonymous')}\n"
            f"🗓 *Date:* {at}\n\n"
            f"💬 *Review:*\n_{review.get('content','')[:300]}_\n\n"
            f"🤖 *AI Summary:* {summary}\n\n"
            f"📊 *আজকের ৫★ রিভিউ:* {daily}টি"
        )
        wa_caption = tg_caption.replace("*","").replace("_","").replace("`","")

        # ── Telegram (mandatory) ──
        if tg_group:
            try:
                await bot.send_photo(
                    chat_id=tg_group,
                    photo=io.BytesIO(shot),
                    caption=tg_caption,
                    parse_mode="Markdown",
                )
                logger.info(f"📤 TG sent → {tg_group}")
            except TelegramError as e:
                logger.error(f"TG error: {e}")

        # ── WhatsApp (mandatory) ──
        if wa_group:
            send_wa_image(wa_group, shot, wa_caption)
        else:
            logger.warning(f"⚠️ WA group ID missing for {app_id} — screenshot not sent to WA")

async def review_check_job(context: ContextTypes.DEFAULT_TYPE):
    apps = get_all_apps()
    for cfg in apps:
        if cfg.get("active", True):
            await check_app(context.bot, cfg)

# ─── Daily Summary ────────────────────────────────────────
async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.date.today().isoformat()
    for cfg in get_all_apps():
        app_id   = cfg["app_id"]
        tg_group = cfg.get("tg_group", "")
        wa_group = cfg.get("wa_group", "")
        count    = get_daily_count(app_id, today)
        name     = get_app_name(app_id)

        msg = (
            f"📊 *Daily Summary — {today}*\n\n"
            f"📱 *App:* `{name}`\n"
            f"⭐ *আজ ৫-স্টার রিভিউ:* {count}টি\n\n"
            f"_দারুণ কাজ চালিয়ে যান!_ 🚀"
        )
        if tg_group:
            try:
                await context.bot.send_message(tg_group, msg, parse_mode="Markdown")
            except TelegramError as e:
                logger.error(f"Daily TG error: {e}")
        if wa_group:
            send_wa_text(wa_group, msg.replace("*","").replace("_","").replace("`",""))

# ─── Helpers ──────────────────────────────────────────────
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# ─── Commands ─────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 *স্বাগতম!*\n\n"
        f"আমি আপনার *Play Store Review Monitor Bot* 🤖\n\n"
        f"/admin — Admin প্যানেল\n"
        f"/listapps — মনিটরড অ্যাপ\n"
        f"/status — বটের অবস্থা\n"
        f"/help — সাহায্য",
        parse_mode="Markdown",
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Help*\n\n"
        "/admin — Admin প্যানেল খুলুন\n"
        "/listapps — সব অ্যাপ দেখুন\n"
        "/check — এখনই রিভিউ চেক করুন\n"
        "/summary — দৈনিক সারসংক্ষেপ এখনই পাঠান\n"
        "/status — বটের অবস্থা\n\n"
        "⚙️ *কীভাবে কাজ করে:*\n"
        "• প্রতি ৫ মিনিটে নতুন ৫★ রিভিউ চেক হয়\n"
        "• Telegram ও WhatsApp দুটোতেই screenshot যায়\n"
        "• WhatsApp-এ invite link দিলে বট নিজেই join করে\n"
        "• রাত ১১:৫৯ তে দৈনিক হিসাব পাঠায়",
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
        [InlineKeyboardButton("🔍 এখনই চেক করুন",     callback_data="admin_check")],
        [InlineKeyboardButton("📊 দৈনিক সারসংক্ষেপ",  callback_data="admin_summary")],
    ]
    await update.message.reply_text(
        "🛡 *Admin প্যানেল*\nএকটি অপশন বেছে নিন:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )

async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data

    if not is_admin(q.from_user.id):
        await q.edit_message_text("❌ Admin only.")
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
            await q.edit_message_text("কোনো অ্যাপ নেই।")
            return
        kb = [[InlineKeyboardButton(f"🗑 {a['app_id']}", callback_data=f"del_{a['app_id']}")] for a in apps]
        await q.edit_message_text("কোন অ্যাপটি সরাবেন?", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("del_"):
        delete_app(data[4:])
        await q.edit_message_text(f"✅ `{data[4:]}` সরানো হয়েছে।", parse_mode="Markdown")

    elif data == "admin_list":
        apps = get_all_apps()
        if not apps:
            await q.edit_message_text("কোনো অ্যাপ যোগ করা হয়নি।")
            return
        lines = ["📋 *মনিটরড অ্যাপসমূহ:*\n"]
        for a in apps:
            lines.append(
                f"• `{a['app_id']}`\n"
                f"  📢 TG: `{a.get('tg_group','—')}`\n"
                f"  💬 WA Group: `{a.get('wa_group','—')}`\n"
                f"  🔗 Invite: `{a.get('wa_invite','—')}`"
            )
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown")

    elif data == "admin_check":
        await q.edit_message_text("🔍 চেক করা হচ্ছে...")
        for cfg in get_all_apps():
            await check_app(context.bot, cfg)
        await q.edit_message_text("✅ চেক সম্পন্ন!")

    elif data == "admin_summary":
        await daily_summary_job(context)
        await q.edit_message_text("✅ সারসংক্ষেপ পাঠানো হয়েছে!")

# ─── Conversation: Add App (3 steps) ─────────────────────
async def conv_app_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["app_id"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ App ID: `{context.user_data['app_id']}`\n\n"
        f"এখন *Telegram Group ID* পাঠান\n"
        f"_(উদাহরণ: -1001234567890)_\n\n"
        f"💡 Group ID পেতে:\n"
        f"গ্রুপ থেকে @userinfobot এ যেকোনো মেসেজ forward করুন।\n"
        f"⚠️ বটকে গ্রুপে admin করতে ভুলবেন না!",
        parse_mode="Markdown",
    )
    return WAITING_TG_GROUP

async def conv_tg_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tg_group"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Telegram Group: `{context.user_data['tg_group']}`\n\n"
        f"এখন *WhatsApp Group Invite Link* পাঠান\n"
        f"_(উদাহরণ: https://chat.whatsapp.com/XXXXXXXXXX)_\n\n"
        f"💡 Invite link পেতে:\n"
        f"WhatsApp গ্রুপ → Info → Invite via link\n\n"
        f"⚙️ বট স্বয়ংক্রিয়ভাবে ঐ গ্রুপে join করবে এবং\n"
        f"প্রতিটি নতুন রিভিউর screenshot পাঠাবে।",
        parse_mode="Markdown",
    )
    return WAITING_WA_INVITE

async def conv_wa_invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    invite_link = update.message.text.strip()
    app_id      = context.user_data.get("app_id", "")
    tg_group    = context.user_data.get("tg_group", "")

    msg = await update.message.reply_text("⏳ WhatsApp গ্রুপে join করা হচ্ছে...")

    # Step 1: try joining
    wa_group_id = wa_join_group(invite_link)

    # Step 2: fallback — resolve without joining (if already a member)
    if not wa_group_id:
        wa_group_id = wa_resolve_group(invite_link)

    if wa_group_id:
        status_text = f"✅ WhatsApp গ্রুপে সফলভাবে join হয়েছে!\nGroup ID: `{wa_group_id}`"
    else:
        status_text = (
            "⚠️ এখনই join করা যায়নি।\n"
            "Green API instance চালু আছে কিনা চেক করুন।\n"
            "অ্যাপটি সেভ হয়েছে — পরে /check দিয়ে আবার চেষ্টা করুন।"
        )

    save_app(app_id, tg_group, wa_group_id, invite_link)

    await update.message.reply_text(
        f"🎉 *অ্যাপ সফলভাবে যোগ হয়েছে!*\n\n"
        f"📱 App ID: `{app_id}`\n"
        f"📢 Telegram Group: `{tg_group}`\n"
        f"🔗 WA Invite: `{invite_link}`\n"
        f"💬 WA Group ID: `{wa_group_id or 'pending'}`\n\n"
        f"{status_text}\n\n"
        f"✅ বট এখন থেকে প্রতি *৫ মিনিটে* নতুন ৫★ রিভিউ\n"
        f"Telegram ও WhatsApp *দুটো গ্রুপেই* পাঠাবে।",
        parse_mode="Markdown",
    )
    context.user_data.clear()
    return ConversationHandler.END

async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ বাতিল করা হয়েছে।")
    return ConversationHandler.END

# ─── Other Commands ───────────────────────────────────────
async def cmd_listapps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    apps = get_all_apps()
    if not apps:
        await update.message.reply_text("কোনো অ্যাপ যোগ করা হয়নি। /admin → অ্যাপ যোগ করুন।")
        return
    lines = ["📋 *মনিটরড অ্যাপসমূহ:*\n"]
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
    await update.message.reply_text("🔍 চেক করা হচ্ছে...")
    for cfg in get_all_apps():
        await check_app(context.bot, cfg)
    await update.message.reply_text("✅ চেক সম্পন্ন!")

async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    await daily_summary_job(context)
    await update.message.reply_text("✅ সারসংক্ষেপ পাঠানো হয়েছে!")

async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}", exc_info=True)

# ─── Main ─────────────────────────────────────────────────
def main():
    logger.info("🚀 Bot starting...")
    start_keep_alive()
    start_self_ping()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Conversation: Add App (3 steps)
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_cb, pattern="^admin_add$")],
        states={
            WAITING_APP_ID:   [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_app_id)],
            WAITING_TG_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_tg_group)],
            WAITING_WA_INVITE:[MessageHandler(filters.TEXT & ~filters.COMMAND, conv_wa_invite)],
        },
        fallbacks=[CommandHandler("cancel", conv_cancel)],
        per_user=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("admin",    cmd_admin))
    app.add_handler(CommandHandler("listapps", cmd_listapps))
    app.add_handler(CommandHandler("check",    cmd_check))
    app.add_handler(CommandHandler("summary",  cmd_summary))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CallbackQueryHandler(admin_cb))
    app.add_error_handler(error_handler)

    jq = app.job_queue
    jq.run_repeating(review_check_job, interval=300, first=30, name="review_check")
    jq.run_daily(daily_summary_job, time=datetime.time(23, 59, 0), name="daily_summary")

    logger.info("✅ Bot polling শুরু হয়েছে!")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
