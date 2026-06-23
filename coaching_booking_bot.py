"""
Panic Circle Bot
-------------------------------------
A single Telegram bot that:
  1. Sells the Panic Circle — a $79/month, 20-person max private group —
     through a short voice + text story sequence (Screens 2-5 + price)
  2. After payment, generates a single-use invite link into the private
     group automatically (no shared/reusable links)
  3. Offers a full menu of panic / nervous-system exercises, a free cheat
     sheet, and an optional 1:1 coaching upsell (mentioned only at the end)

Commands:
  /start      - Begin (or resume) the bot
  /help       - List all commands
  /panic, /breathe, /ground, /sigh, /relax, /hum, /cold, /valsalva,
  /orient, /push, /shake, /yawn, /walk, /hold, /smell, /butterfly, /belly
              - Individual exercises
  /support    - Book a real 1:1 session with Oxana
  /learn      - Mini course on panic symptoms
  /stopremind - Turn off daily reminders
  /bookings   - (admin) list confirmed 1:1 + Circle payments
  /users      - (admin) list bot users
  /broadcast  - (admin) message all users

Setup:
  1. pip install -r requirements.txt
  2. Copy .env.example to .env and fill in your values
  3. Put nervous_system_cheat_sheet.pdf in the same folder as this file
  4. python coaching_booking_bot.py

See README.md for full setup instructions, including the one-time step of
making this bot an admin of your private Panic Circle group.
"""

import asyncio
import json
import logging
import os
import random
import secrets
from datetime import datetime, time as dtime, timezone

import stripe
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update,
)
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, ChatMemberHandler,
    CommandHandler, ContextTypes, MessageHandler, filters,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M",
)
log = logging.getLogger(__name__)


# ── Required configuration ───────────────────────────────────────────────────

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = _require_env("TELEGRAM_BOT_USERNAME").lstrip("@")
ADMIN_ID = int(_require_env("ADMIN_TELEGRAM_ID"))
stripe.api_key = _require_env("STRIPE_SECRET_KEY")

# Fallback link sent if the bot can't auto-generate a single-use invite
# (e.g. it hasn't been made an admin of the group yet). Set this to a
# regular group invite link as a safety net — see README.
CALENDAR_LINK = os.environ.get(
    "CALENDAR_LINK", "https://calendar.app.google/UdVSNtXD6Xnfyord9"
)

# Optional but recommended: the numeric chat ID of your private Panic
# Circle group. When set (and the bot is an admin there), every new payer
# gets their own one-time invite link instead of a reusable one.
# See README for how to find this number.
PANIC_CIRCLE_GROUP_ID = os.environ.get("PANIC_CIRCLE_GROUP_ID")
if PANIC_CIRCLE_GROUP_ID:
    PANIC_CIRCLE_GROUP_ID = int(PANIC_CIRCLE_GROUP_ID)

DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
PENDING_FILE = os.path.join(DATA_DIR, "pending_payments.json")
BOOKINGS_FILE = os.path.join(DATA_DIR, "bookings.json")
PREFS_FILE = os.path.join(DATA_DIR, "user_prefs.json")
LOG_FILE = os.path.join(DATA_DIR, "bot_usage.log")

CHEATSHEET_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "nervous_system_cheat_sheet.pdf"
)


# ── Storage helpers ───────────────────────────────────────────────────────────

def _load(path: str) -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(path: str, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_pending(): return _load(PENDING_FILE)
def save_pending(d): _save(PENDING_FILE, d)
def load_bookings(): return _load(BOOKINGS_FILE)
def save_bookings(d): _save(BOOKINGS_FILE, d)
def load_prefs(): return _load(PREFS_FILE)
def save_prefs(d): _save(PREFS_FILE, d)


# ── Admin notify ──────────────────────────────────────────────────────────────

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text)
    except Exception:
        log.exception("Failed to notify admin")


def log_command(update: Update, command: str) -> None:
    user = update.effective_user
    username = f"@{user.username}" if user.username else f"user_id:{user.id}"
    name = user.first_name or ""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"[{timestamp}] {username} ({name}) used /{command}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    try:
        asyncio.get_event_loop().create_task(
            update.get_bot().send_message(chat_id=ADMIN_ID, text=f"📊 {line}")
        )
    except Exception:
        pass


# ── Rate limiting ─────────────────────────────────────────────────────────────

LAST_USED = {}

def is_rate_limited(user_id: int, seconds: int = 3) -> bool:
    now = datetime.now().timestamp()
    if user_id in LAST_USED and now - LAST_USED[user_id] < seconds:
        return True
    LAST_USED[user_id] = now
    return False


# ── Keyboards: general ────────────────────────────────────────────────────────

def persistent_keyboard():
    return ReplyKeyboardMarkup([["Menu"]], resize_keyboard=True, is_persistent=True)


def make_menu(show_reminder=False, is_member=False):
    keyboard = [
        [InlineKeyboardButton("🚨 In Panic Now", callback_data="menu_panic")],
        [InlineKeyboardButton("🌿 Calm My Nervous System", callback_data="menu_ns")],
        [InlineKeyboardButton("📖 Learn & Understand", callback_data="learn_menu")],
        [InlineKeyboardButton("📄 Free Cheat Sheet", callback_data="get_cheatsheet")],
    ]
    if is_member:
        keyboard.append([InlineKeyboardButton("💬 Ask about 1:1 coaching", callback_data="book_coaching")])
    else:
        keyboard.append([InlineKeyboardButton("💛 Join the Panic Circle", callback_data="price")])
    if show_reminder:
        keyboard.append([
            InlineKeyboardButton("🔔 Remind me daily", callback_data="remind_yes"),
            InlineKeyboardButton("No thanks", callback_data="remind_no"),
        ])
    return InlineKeyboardMarkup(keyboard)


def make_panic_menu():
    keyboard = [
        [InlineKeyboardButton("🚨 Emergency panic", callback_data="panic"),
         InlineKeyboardButton("🫁 Breathe", callback_data="breathe")],
        [InlineKeyboardButton("🌬 Sigh", callback_data="sigh"),
         InlineKeyboardButton("💧 Cold water", callback_data="cold")],
        [InlineKeyboardButton("🧠 Ground", callback_data="ground"),
         InlineKeyboardButton("👁 Orient", callback_data="orient")],
        [InlineKeyboardButton("🫁 Valsalva", callback_data="valsalva")],
        [InlineKeyboardButton("← Back", callback_data="back_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def make_ns_menu():
    keyboard = [
        [InlineKeyboardButton("💪 Relax", callback_data="relax"),
         InlineKeyboardButton("🎵 Hum", callback_data="hum")],
        [InlineKeyboardButton("🤜 Push", callback_data="push"),
         InlineKeyboardButton("🫨 Shake", callback_data="shake")],
        [InlineKeyboardButton("😮 Yawn", callback_data="yawn"),
         InlineKeyboardButton("🚶 Walk", callback_data="walk")],
        [InlineKeyboardButton("🤗 Hold", callback_data="hold"),
         InlineKeyboardButton("👃 Smell", callback_data="smell")],
        [InlineKeyboardButton("🦋 Butterfly", callback_data="butterfly"),
         InlineKeyboardButton("🌬 Belly Breath", callback_data="belly")],
        [InlineKeyboardButton("← Back", callback_data="back_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


TIMEZONE_NAMES = {
    "-8": "🌎 Americas (West)", "-5": "🌎 Americas (East)",
    "1": "🌍 Europe", "3": "🌍 Middle East / Africa",
    "8": "🌏 Asia", "10": "🌏 Australia",
}

def timezone_keyboard():
    keyboard = [
        [InlineKeyboardButton("🌎 Americas (West)", callback_data="tz_-8"),
         InlineKeyboardButton("🌎 Americas (East)", callback_data="tz_-5")],
        [InlineKeyboardButton("🌍 Europe", callback_data="tz_1"),
         InlineKeyboardButton("🌍 Middle East / Africa", callback_data="tz_3")],
        [InlineKeyboardButton("🌏 Asia", callback_data="tz_8"),
         InlineKeyboardButton("🌏 Australia", callback_data="tz_10")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ── send_steps (exercises) ────────────────────────────────────────────────────

async def send_steps(steps, delays, update, context, is_ns=False):
    user_id = str(update.effective_user.id)
    prefs = load_prefs()
    show_reminder = is_ns and "utc_offset" not in prefs.get(user_id, {})
    is_member = prefs.get(user_id, {}).get("circle_member", False)
    formatted = "\n\n".join(f"<blockquote>{step}</blockquote>" for step in steps)
    formatted += "\n\n─────────────────\n\nYou did that. 💙\n\nTake a moment. When you're ready — choose what feels right:"
    if show_reminder:
        formatted += "\n\n🔔 Want a daily reminder like this?"
    await update.effective_message.reply_text(
        formatted, parse_mode="HTML",
        reply_markup=make_menu(show_reminder=show_reminder, is_member=is_member)
    )


# ── Reminders ──────────────────────────────────────────────────────────────────

EXERCISES = [
    ("🌬 Physiological Sigh", "sigh"), ("🫁 Box Breathing", "breathe"),
    ("💪 Progressive Muscle Relaxation", "relax"), ("🎵 Humming", "hum"),
    ("💧 Cold Water Dive Reflex", "cold"), ("🧠 Grounding 5-4-3-2-1", "ground"),
    ("👁 Slow Orienting", "orient"), ("🤜 Wall Pushing", "push"),
    ("🫨 Shaking", "shake"), ("😮 Yawning & Sighing", "yawn"),
    ("🚶 Mindful Walking", "walk"), ("🤗 Self-Holding", "hold"),
    ("👃 Smell", "smell"), ("🦋 Butterfly Hug", "butterfly"),
    ("🌬 Belly Breathing", "belly"),
]

async def send_reminder(context):
    exercise_name, exercise_cmd = random.choice(EXERCISES)
    await context.bot.send_message(
        chat_id=context.job.chat_id,
        text=f"Hey 💙 Your daily moment for yourself.\n\nToday's exercise: {exercise_name}\n\nTap to start: /{exercise_cmd}"
    )

def schedule_reminder(app, chat_id, utc_offset):
    current_jobs = app.job_queue.get_jobs_by_name(str(chat_id))
    for job in current_jobs:
        job.schedule_removal()
    utc_hour = (10 - utc_offset) % 24
    app.job_queue.run_daily(send_reminder, time=dtime(utc_hour, 30), chat_id=chat_id, name=str(chat_id))

def load_all_reminders(app):
    prefs = load_prefs()
    for user_id, data in prefs.items():
        if "utc_offset" in data and "chat_id" in data:
            schedule_reminder(app, data["chat_id"], data["utc_offset"])


# ── Voice notes ────────────────────────────────────────────────────────────────
# Drop in file_ids as you record them. Screens with no file_id just skip the
# voice note and show text only — nothing breaks either way.

VOICE_NOTES = {
    "screen2": "AwACAgEAAxkBAAMqajaMEjumzp2scWEoC8yO2Cs2_ZYAAqsGAAKRwrBFarK4028IeCk8BA",
    "screen3": "AwACAgEAAxkBAAMuajaMsenjKSxy6RRM4Inu4YGT5uUAAqwGAAKRwrBF5XiWI5aOQqI8BA",
    "screen4": "AwACAgEAAxkBAAMyajaNI6KSKh8I2x07wptKw3UF0jIAAq0GAAKRwrBFh1LVpKczQUs8BA",
    "screen5": "AwACAgEAAxkBAAM6ajaP1yLwSDGjqaa-aeP9SC06qnYAArEGAAKRwrBFP21dHvQC3cM8BA",
    # "price": "",  # record this one and drop the file_id in here
}

async def send_voice_if_set(screen_key, chat_id, context):
    file_id = VOICE_NOTES.get(screen_key)
    if not file_id:
        return
    try:
        await context.bot.send_voice(chat_id=chat_id, voice=file_id)
    except Exception:
        log.exception("Failed to send voice note for %s", screen_key)


async def voice_capture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: reply with the file_id of any voice note sent to the bot,
    so new screens' voice notes can be grabbed easily."""
    if update.effective_user.id != ADMIN_ID:
        return
    file_id = update.effective_message.voice.file_id
    await update.effective_message.reply_text(f"file_id:\n{file_id}")


# ── Panic Circle funnel (Screens 2-5 + price) ────────────────────────────────

SCREEN2_TEXT = (
    "<blockquote>Every morning starts the same.\n\n"
    "You wake up. Before you even get out of bed, you're already doing the work.\n\n"
    "Breathing. Meditation. Grounding. Cold showers. Journaling.\n\n"
    "You finally feel calm. You finally feel okay.\n\n"
    "And then the next day...\n\n"
    "<b>it's back.</b>\n\n"
    "Like you have to start all over again.</blockquote>"
)

SCREEN3_TEXT = (
    "<blockquote>You know all the tricks by now. Breathing. Grounding. Cold water. All of it.\n\n"
    "But be honest with yourself for a second —\n\n"
    "<i>is this just your life now? Doing the tricks every single morning just to feel normal?</i>\n\n"
    "I didn't want to manage it anymore.\n\n"
    "I wanted it gone.</blockquote>"
)

SCREEN4_TEXT = (
    "<blockquote>Here's the part that changed everything for me.\n\n"
    "Calming a panic attack, and stopping it from coming back — not the same thing.\n\n"
    "Breathing calms it. Grounding calms it. Cold water calms it.\n\n"
    "But if whatever's underneath is still there...\n\n"
    "<b>it comes back.</b>\n\n"
    "Every time.\n\n"
    "That's when I stopped just putting out the fire, and started asking why it kept starting.</blockquote>"
)

SCREEN5_TEXT = (
    "<b>So I built the Panic Circle.</b>\n\n"
    "Not a course. You don't watch me talk for 40 minutes and close the tab.\n\n"
    "🫂 People who actually get it — not strangers, people living this right now\n"
    "💬 A place to ask questions\n"
    "📅 I go through the chat and answer them — every day\n"
    "🤝 You watch other people figure it out too, which teaches you more than you'd think\n"
    "🛠 Real tools, the same day you ask\n\n"
    "Not managing it forever. Actually working on why it's there."
)

PRICE_TEXT = (
    "<b>Panic Circle — $79 a month.</b>\n\n"
    "Less than one single session with me. Less than what you've probably already "
    "spent on stuff that didn't stick.\n\n"
    "<b>Cancel any time. No contract. No catch.</b>\n\n"
    "I'm not promising you'll never panic again — nobody honest can promise that. "
    "I'm promising real answers, every day, from someone who's actually been there. "
    "Not a video from two years ago.\n\n"
    "Want more — actual 1:1 time with me too? Just ask, that's available separately."
)


def single_button(label, callback_data):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=callback_data)]])


def screen2_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 Read Oxana's Story", callback_data="bio_s2")],
        [InlineKeyboardButton("Show me how to get better", callback_data="screen3")],
    ])


def price_cta_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, let's do this", callback_data="cta_yes")],
        [InlineKeyboardButton("🤔 Not sure yet", callback_data="cta_unsure")],
        [InlineKeyboardButton("❓ I have a question", callback_data="cta_question")],
    ])


async def send_screen(update_or_message, chat_id, screen_key, text, keyboard, context):
    message = update_or_message
    file_id = VOICE_NOTES.get(screen_key)
    if file_id:
        await message.reply_text(text, parse_mode="HTML", reply_markup=persistent_keyboard())
        try:
            await context.bot.send_voice(chat_id=chat_id, voice=file_id, reply_markup=keyboard)
        except Exception:
            log.exception("Failed to send voice note for %s", screen_key)
            await message.reply_text("💙", reply_markup=keyboard)
    else:
        await message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def send_cheatsheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    try:
        with open(CHEATSHEET_PATH, "rb") as f:
            await context.bot.send_document(
                chat_id=chat.id,
                document=f,
                filename="Nervous System Regulating Cheat Sheet.pdf",
                caption="Here it is. 💙 Save it somewhere you'll actually find it at 2am.",
            )
    except FileNotFoundError:
        log.error("Cheat sheet PDF not found at %s", CHEATSHEET_PATH)
        await update.effective_message.reply_text(
            "I couldn't find the cheat sheet file just now — message me directly and I'll send it personally. 💙"
        )


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args:
        payload = args[0]
        if payload.startswith("paid_"):
            await handle_payment_return(update, context, payload[len("paid_"):])
            return
        if payload == "cancelled":
            await update.effective_message.reply_text(
                "No worries — the offer's still here whenever you're ready. 💙",
                reply_markup=single_button("💛 Join the Panic Circle", "price"),
            )
            return

    log_command(update, "start")
    user = update.effective_user
    user_id = str(user.id)
    name = user.first_name or ""
    chat_id = update.effective_message.chat_id
    prefs = load_prefs()

    if user_id not in prefs:
        prefs[user_id] = {"chat_id": chat_id, "name": name}
        save_prefs(prefs)

    record = prefs[user_id]

    if record.get("circle_member"):
        await update.effective_message.reply_text(
            f"Welcome back{' ' + name if name else ''}. 💙 You're already in the Circle.\n\n"
            "Use the Menu button below whenever you need me.\n\n"
            "⚠️ This bot is a self-help tool, not a substitute for professional help.",
            reply_markup=persistent_keyboard()
        )
        await update.effective_message.reply_text(
            "Choose where you'd like to start:", reply_markup=make_menu(is_member=True)
        )
        return

    record["seen_funnel"] = True
    save_prefs(prefs)
    await send_screen(
        update.effective_message, chat_id, "screen2", SCREEN2_TEXT,
        screen2_keyboard(), context,
    )


# ── /help ─────────────────────────────────────────────────────────────────────

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_command(update, "help")
    await update.effective_message.reply_text(
        "Here's what I can help with:\n\n"
        "🚨 Emergency\n/panic — Active panic attack support\n\n"
        "🫁 Breathing\n/breathe — Box breathing\n/sigh — Physiological sigh\n\n"
        "💪 Body\n/relax — Progressive muscle relaxation\n/hum — Humming\n"
        "/cold — Cold water dive reflex\n/valsalva — Vagus nerve pressure\n"
        "/push — Wall pushing\n/shake — Full body shaking\n/yawn — Yawning and sighing\n"
        "/hold — Self-holding\n/butterfly — Butterfly hug\n/belly — Belly breathing\n\n"
        "🚶 Movement\n/walk — Mindful walking\n\n"
        "🧠 Grounding\n/orient — Slow orienting\n/ground — 5-4-3-2-1\n\n"
        "🤝 Human support\n/support — Book a real 1:1 session with Oxana\n\n"
        "📖 /learn — Understand your symptoms\n"
        "🔕 /stopremind — Turn off daily reminders\n\n"
        "You don't have to face this alone. Tap any command above."
    )


# ── Exercises ──────────────────────────────────────────────────────────────────

PANIC_STEPS = [
    "🛑 Stop and notice — you are having a panic attack.\n\nPanic attacks are intense but not dangerous. Your body is trying to protect you — it has made a mistake, but you are safe.\n\nLet's get through this together. First:\n\n👉 Plant both feet flat on the floor.\nFeel the ground underneath you. It's solid. You're supported.",
    "Good. Now let's slow your breathing.\n\nBreathe IN slowly for 4 counts... 1, 2, 3, 4\nHold for 2 counts... 1, 2\nBreathe OUT slowly for 6 counts... 1, 2, 3, 4, 5, 6\n\nRepeat this 3 times on your own, then come back here.\n\nTake your time — I'll wait.",
    "You're doing great. 💙\n\nNow look around and name 5 things you can SEE right now.\nSay them out loud or type them — it doesn't matter which.\n\nTake your time. The panic is already beginning to pass.",
    "The wave is passing. Panic attacks typically peak within 10 minutes and then fade.\n\nA few things to remember:\n• What you feel is temporary.\n• You have gotten through this before.\n• You are not in danger.\n\nWhen you're ready, try /ground for a full calming exercise, or just sit quietly for a moment. You did well. 🌿",
]
async def panic(update, context):
    log_command(update, "panic")
    await send_steps(PANIC_STEPS, 12, update, context)

BREATHE_STEPS = [
    "Let's do box breathing together. It activates your body's calming response.\n\nGet comfortable. Sit or lie down if you can.",
    "🟦 BREATHE IN\nInhale slowly through your nose...\n1... 2... 3... 4",
    "⬜ HOLD\nHold your breath gently...\n1... 2... 3... 4",
    "🟦 BREATHE OUT\nExhale slowly through your mouth...\n1... 2... 3... 4",
    "⬜ HOLD\nHold, lungs empty...\n1... 2... 3... 4",
    "That's one cycle. Let's do it two more times.\n\n🟦 IN — 1,2,3,4\n⬜ HOLD — 1,2,3,4\n🟦 OUT — 1,2,3,4\n⬜ HOLD — 1,2,3,4\n\n🟦 IN — 1,2,3,4\n⬜ HOLD — 1,2,3,4\n🟦 OUT — 1,2,3,4\n⬜ HOLD — 1,2,3,4",
    "Well done. 🌿\n\nYour nervous system is resetting. Notice if your body feels even slightly calmer.\n\nYou can repeat this any time. Use /ground when you're ready for the next step.",
]
async def breathe(update, context):
    log_command(update, "breathe")
    await send_steps(BREATHE_STEPS, 8, update, context)

GROUND_STEPS = [
    "Let's do the 5-4-3-2-1 grounding exercise.\n\nThis technique brings your attention into the present moment and interrupts the panic cycle.\n\nTake a slow breath and let's begin.",
    "👁 5 things you can SEE\n\nLook around slowly and notice five things. A chair, a window, your hands — anything.\n\nTake your time.",
    "✋ 4 things you can TOUCH\n\nNotice four things you can physically feel right now.\nThe texture of your clothes, the surface under you, the temperature of the air.\n\nTouch them and notice how they feel.",
    "👂 3 things you can HEAR\n\nListen carefully. Traffic outside, a fan, your own breathing.\nName three sounds.",
    "👃 2 things you can SMELL\n\nEven subtle smells count — your skin, the air, a nearby drink.\nFind two.",
    "👅 1 thing you can TASTE\n\nWhat do you taste right now? Even the faint taste in your mouth counts.",
    "You're back. 💙\n\nThat's the 5-4-3-2-1 technique. Your brain is now anchored in the present, not the spiral.\n\nYou can use this any time anxiety rises. The more you practice, the faster it works.\n\nTake a gentle breath. You're okay.",
]
async def ground(update, context):
    log_command(update, "ground")
    await send_steps(GROUND_STEPS, 15, update, context)

SIGH_STEPS = [
    "The physiological sigh. 🌬\n\nThis is the single fastest way to lower arousal — mammals do it naturally when coming down from stress. It takes about 10 seconds.\n\nHere's how it works: two quick inhales, then one long slow exhale.",
    "Step 1 — First inhale\n\nBreathe in deeply through your nose...\nFill your lungs most of the way.",
    "Step 2 — Second inhale\n\nWithout exhaling, take one more short sniff in through your nose.\nTop up your lungs completely. 👃👃",
    "Step 3 — Long exhale\n\nNow breathe ALL the way out through your mouth...\nSlow... long... empty your lungs completely. 😮‍💨\n\nLet it go.",
    "That's it. One cycle.\n\nRepeat 2–3 more times if needed. Each cycle lowers your CO2 and signals your brain that the threat has passed.\n\nNotice how your body feels even slightly different. 💙",
]
async def sigh(update, context):
    log_command(update, "sigh")
    await send_steps(SIGH_STEPS, [5, 8, 8, 12], update, context)

RELAX_STEPS = [
    "Progressive muscle relaxation. 💪\n\nWe'll move through your body, tensing and releasing each muscle group.\nWhen you release, your nervous system gets a clear signal: safe, no threat.\n\nSit or lie down comfortably. Let's begin.",
    "👣 Feet and calves\n\nCurl your toes and tense your feet and calves tightly...\nHold for 5 seconds... 1,2,3,4,5\n\nNow release completely. Let them go limp. Notice the difference.",
    "🦵 Thighs and hips\n\nSqueeze your thigh muscles and tighten your hips and buttocks...\nHold for 5 seconds... 1,2,3,4,5\n\nRelease. Let the tension drain away.",
    "🤰 Stomach and back\n\nPull your stomach in and tighten your core muscles...\nHold for 5 seconds... 1,2,3,4,5\n\nRelease. Let your belly go soft.",
    "✋ Hands and arms\n\nMake tight fists and tense your forearms and biceps...\nHold for 5 seconds... 1,2,3,4,5\n\nRelease. Let your arms feel heavy and warm.",
    "🤷 Shoulders\n\nShrug your shoulders up to your ears as tightly as you can...\nHold for 5 seconds... 1,2,3,4,5\n\nRelease. Let them drop. Feel the weight fall away.",
    "😬 Face and jaw\n\nScrunch your whole face — clench your jaw, squeeze your eyes, furrow your brow...\nHold for 5 seconds... 1,2,3,4,5\n\nRelease. Soften your jaw. Let your mouth fall open slightly.",
    "🌿 Complete.\n\nScan your body from feet to head. Notice the difference between where you started and now.\n\nYour muscles have released tension they were holding on your behalf. That tension was real. So is this release.\n\nRest here for a moment. You did well. 💙",
]
async def relax(update, context):
    log_command(update, "relax")
    await send_steps(RELAX_STEPS, [5, 18, 18, 18, 18, 18, 18], update, context, is_ns=True)

HUM_STEPS = [
    "Humming exercise. 🎵\n\nThe vagus nerve runs through your throat and larynx. When you hum, the vibrations travel directly along this nerve, activating your body's calming system.\n\nYou'll feel a bit silly. That's fine. It works.\n\nFind somewhere you can make sound.",
    "Put your fingers gently in your ears. 👂👂\n\nThis amplifies the internal vibration so you can feel it more clearly.\n\nTake a breath in...",
    "Now hum on the exhale. 🎶\n\nAny pitch, any tune — just let the sound come.\nFeel the vibration in your throat, your chest, your skull.\n\nKeep humming for the next 30 seconds. Let the sound be bigger than your worry.",
    "Keep going... 🎵\n\nStay with the vibration. Notice where you feel it most.\nIf you run out of breath, inhale and continue.",
    "Last round. Make it a long, slow one. 😮‍💨🎵\n\nOne deep breath in...\nThen hum all the way to the end of your exhale.",
    "Done. 🌿\n\nNotice how your body feels. Many people feel a warmth in the chest, a softening in the jaw, a slight heaviness in the limbs — those are signs your parasympathetic system has engaged.\n\nYou can do this for 10–15 minutes when you want a deeper reset. Even 2 minutes helps. 💙",
]
async def hum(update, context):
    log_command(update, "hum")
    await send_steps(HUM_STEPS, [6, 8, 30, 30, 20], update, context, is_ns=True)

COLD_STEPS = [
    "Cold water dive reflex. 💧\n\nExposing your face to cold water triggers a hard-wired parasympathetic response called the dive reflex. It slows your heart rate within seconds and shifts your nervous system toward calm.\n\nYou need: cold water (ideally with ice), a bowl or sink.\n\nGo get it — I'll wait.",
    "Ready? Here's what to do:\n\n1. Fill a bowl or your sink with cold water\n2. Add ice if you have it\n3. Take a breath in and hold it\n4. Submerge your face for 15–30 seconds\n\nFocus on your forehead, eyes, and cheeks — that's where the reflex receptors are.\n\nGo ahead when you're ready.",
    "How was it? 💙\n\nEven splashing cold water on your face works if full immersion isn't possible.\n\nWhat just happened: your brainstem detected cold + breath hold and fired the mammalian dive reflex — the same response that allows seals and dolphins to slow their heart rate underwater. Your heart rate dropped. Your sympathetic system stepped back.\n\nYou can repeat this 2–3 times. Each round works.\n\nIf you don't have ice: a cold, damp cloth held over your face for 30 seconds produces a similar effect. 🌿",
]
async def cold(update, context):
    log_command(update, "cold")
    await send_steps(COLD_STEPS, [15, 45], update, context)

async def valsalva(update, context):
    log_command(update, "valsalva")
    steps = [
        "Valsalva manoeuvre. 🫁\n\nThis directly stimulates the vagus nerve and slows your heart rate within seconds. Used by doctors to stop rapid heart rate episodes.\n\nHere's how:",
        "1. Pinch your nose shut\n2. Close your mouth tightly\n3. Try to exhale forcefully — as if inflating a stiff balloon — but don't let any air out\n4. Hold that pressure for 10–15 seconds\n\nGo ahead.",
        "Release and breathe normally. 💙\n\nNotice your heart rate. It should feel slightly slower or more steady.\n\nYou can repeat this 2–3 times. It works because the pressure activates the baroreceptors in your chest, which signal the vagus nerve to slow everything down.\n\nSimple. Fast. Effective. 🌿",
    ]
    await send_steps(steps, [8, 20], update, context, is_ns=True)

async def orient(update, context):
    log_command(update, "orient")
    steps = [
        "Slow orienting. 👁\n\nWhen we're anxious, our gaze narrows and fixes. This exercise reverses that — it lets your nervous system scan the environment and register: I am safe here.\n\nTake a slow breath and let's begin.",
        "Soften your eyes. Let your gaze go wide rather than focused.\n\nNow slowly turn your head and look around the room.\nMove slower than feels natural.\n\nLet your eyes rest on different objects — don't rush.",
        "As your eyes land on each object, silently name it.\n\nChair. Window. Cup. Plant. Door.\n\nJust name what you see. No judgement. No story.",
        "Notice if anything feels pleasant to look at — a colour, a texture, light coming through a window.\n\nLet your eyes rest there for a moment.",
        "Good. 🌿\n\nYour nervous system has just scanned the environment and found no threat. That scan is a biological safety signal — it tells the deeper brain it can stand down.\n\nYou can do this any time: walking into a new space, sitting in a waiting room, waking up anxious. Slow eyes, slow breath. 💙",
    ]
    await send_steps(steps, [6, 15, 15, 15], update, context)

async def push(update, context):
    log_command(update, "push")
    steps = [
        "Wall pushing. 💪\n\nWhen panic hits, your body prepares to fight or flee — but then does neither. The energy has nowhere to go.\n\nThis exercise gives it somewhere to go. You're completing the motor pattern your nervous system was primed for.\n\nFind a wall.",
        "Stand facing the wall, arms' length away.\n\nPlace your palms flat against it at shoulder height.\n\nNow push. Slowly and steadily — engage your arms, shoulders, and core.\nHold the push for 10 seconds... breathe while you push.\n\n1...2...3...4...5...6...7...8...9...10",
        "Release. Step back. Shake your arms out gently.\n\nNotice any shift in your body — warmth, release of tension, a slight heaviness in the arms.\n\nRepeat 3–5 times.\n\nNo wall? Press your palms together hard in front of your chest — same effect. 🌿",
    ]
    await send_steps(steps, [8, 20], update, context)

async def shake(update, context):
    log_command(update, "shake")
    steps = [
        "Shaking. 🫨\n\nAnimals naturally shake after a threat — it's how they discharge the survival energy the body mobilised. Humans do it too, but we're usually taught to suppress it.\n\nThis exercise lets you do it on purpose. Stand up if you can.",
        "Start with your hands.\n\nLet them shake loosely — like you're flicking water off your fingers.\nThen let the shaking travel up your arms.",
        "Now let it spread to your whole body.\n\nKnees slightly bent. Let your legs, hips, shoulders all join in.\nThere's no right way to do this — just let the body move.\n\nKeep going for 1–2 minutes.",
        "Slow down gradually and come to stillness.\n\nStand quietly for a moment. Notice what you feel.\n\nMany people feel warmth, tingling, or a surprising sense of calm. That's the activation discharging — exactly what the nervous system needed.\n\nYou can do this after any stressful event, not just panic attacks. 💙",
    ]
    await send_steps(steps, [8, 15, 90], update, context, is_ns=True)

async def yawn(update, context):
    log_command(update, "yawn")
    steps = [
        "Yawning and sighing. 😮\n\nYawns stretch the vagus nerve and release jaw and facial tension — two of the places we hold stress most. Sighs act as the body's built-in reset button.\n\nThe trick: you can trigger a real yawn on purpose.",
        "Open your mouth as wide as it goes.\nStretch your arms above your head.\nMake the biggest, most exaggerated yawn you can — the kind you'd normally suppress in public.\n\nGo for it. 😪",
        "Now a big sigh.\n\nInhale deeply... then let it all out with a long, audible sigh.\nMake some noise. Don't hold back.\n\nHaaaah... 😮‍💨",
        "Repeat both 3–5 times.\n\nNotice your jaw, your shoulders, your chest. Most people feel them soften within a few cycles.\n\nYour body knows how to do this — you've just been taught to hide it. Let it happen. 🌿",
    ]
    await send_steps(steps, [8, 15, 15], update, context, is_ns=True)

async def walk(update, context):
    log_command(update, "walk")
    steps = [
        "Mindful walking. 🚶\n\nAfter a panic attack, your motor system was primed for running — and then didn't run. A walk gives it the discharge it was prepared for.\n\nEven 5 minutes helps. You don't need to go far.",
        "Start walking — slower than your normal pace.\n\nPut your phone away if you can.\n\nFocus on the sensation of each foot contacting the ground.\nFeel the weight shift from heel to toe with each step.",
        "Notice what's around you.\n\nNot thoughts about the walk — the actual walk.\nThe air temperature. Sounds. Light. What your feet feel.\n\nIf your mind wanders to worry, gently bring it back to the next step.",
        "Keep going for at least 5 minutes. Longer if it feels good.\n\nThere's no destination. Aimless is better than purposeful here.\n\nYou're not trying to get anywhere — you're letting your nervous system close the loop it opened during the panic. 🌿💙",
    ]
    await send_steps(steps, [6, 60, 60], update, context, is_ns=True)

async def hold(update, context):
    log_command(update, "hold")
    steps = [
        "Self-holding. 🤗\n\nDeveloped by Peter Levine, this exercise creates a felt sense of being contained — like having someone hold you, but you do it yourself.\n\nIt's surprisingly effective for moments when anxiety feels like it's spilling out of you.\n\nSit comfortably.",
        "Give yourself a hug.\n\nPlace one hand under your opposite arm.\nPlace the other hand on the upper part of that arm.\n\nHold firmly. Feel yourself as a container.",
        "Close your eyes if that feels okay.\n\nTake slow, deep breaths.\n\nNotice the weight and warmth of your own hands.\nLet yourself feel held.",
        "Stay here for 2–3 minutes.\n\nWatch for any shifts — in your breathing, in the sensations in your body, in how the anxiety feels.\n\nYou don't need to do anything except be here, holding yourself. 💙\n\nYou can switch arm positions any time:\n• One hand on your heart, one on your belly\n• Both hands on your face\n• One hand on your forehead, one on the back of your head\n\nStay as long as you need. 🌿",
    ]
    await send_steps(steps, [8, 15, 30], update, context, is_ns=True)

async def smell(update, context):
    log_command(update, "smell")
    steps = [
        "Smell something familiar. 👃\n\nSmell is the most primitive of our senses — it travels directly to the limbic system, the part of the brain that regulates emotion and safety. A familiar, pleasant scent can calm the nervous system faster than almost anything.\n\nLook around you. Find something to smell — an orange, an apple, coffee, herbs, hand cream, a candle, even your own skin.\n\nTake your time finding it.",
        "Got something? Good.\n\nBring it close and take a slow, deep inhale through your nose.\n\nDon't rush. Let the scent fill you completely.",
        "Inhale again — even slower this time. 🍊\n\nNotice every detail of the smell.\nIs it sweet? Warm? Sharp? Earthy?\n\nLet your whole attention rest on the scent. Nothing else exists right now.",
        "One more time. Breathe it in deeply.\n\nNotice if any memory or feeling comes with it — a place, a person, a moment of safety.\n\nLet that feeling settle in your body.",
        "You're back. 💙\n\nSmell bypasses thinking entirely — it speaks directly to the part of your brain that knows you are safe.\n\nKeep something nearby that smells familiar and good. A small orange. A sachet of lavender. A jar of coffee. Your own scent on a piece of clothing.\n\nIt's one of the simplest anchors you have. 🌿",
    ]
    await send_steps(steps, [20, 15, 15, 15], update, context, is_ns=True)

async def butterfly(update, context):
    log_command(update, "butterfly")
    steps = [
        "The Butterfly Hug. 🦋\n\nThis is a bilateral tapping technique — tapping that alternates left and right sides of the body. It's widely used to calm anxiety by engaging both sides of the brain at once.\n\nCross your arms over your chest, each hand resting on the opposite shoulder or upper arm — like giving yourself a hug.",
        "Now alternate. Tap gently with one hand, then the other.\n\nLeft... right... left... right...\n\nLike the slow flutter of wings. Keep a steady, gentle rhythm.",
        "Keep going for about a minute. 🦋\n\nIf thoughts or feelings come up, that's fine — you don't need to push anything away. Just keep tapping, slowly and steadily, and let them move through.",
        "Slow the tapping down... and let it stop.\n\nLet your arms rest. Notice how your body feels now compared to when you started.\n\nYou can do this anywhere — a waiting room, a bus, lying in bed. No one even has to know. 💙",
    ]
    await send_steps(steps, [10, 12, 60, 15], update, context, is_ns=True)

async def belly(update, context):
    log_command(update, "belly")
    steps = [
        "Belly breathing. 🌬\n\nMost of us breathe shallow, high in the chest, especially when anxious. This exercise retrains your breath to go deep — which directly signals your nervous system to calm down.\n\nPlace one hand on your chest and one hand on your belly.",
        "Breathe in slowly through your nose.\n\nTry to keep the hand on your chest still, and let the hand on your belly rise as it fills with air.\n\nTake your time — there's no rush.",
        "Now breathe out slowly through your mouth.\n\nFeel your belly hand fall back down as the air leaves.\n\nChest hand barely moves. Belly hand does all the work.",
        "Keep going at your own pace for 5–6 breaths.\n\nIn through the nose, belly rises...\nOut through the mouth, belly falls...",
        "Well done. 🌿\n\nThis is the foundation underneath almost every other breathing exercise here — no counting, no holding, just depth.\n\nYou can do this anywhere, anytime, even without anyone noticing. 💙",
    ]
    await send_steps(steps, [10, 15, 15, 20], update, context, is_ns=True)

async def support(update, context):
    log_command(update, "support")
    await update.effective_message.reply_text(
        "💬 <b>Talk to someone</b>\n\n"
        "Sometimes working through this with another person makes all the difference. "
        "There's no shame in that — it's actually one of the most effective things you can do.\n\n"
        "You can book a session with Oxana here — someone who has been through panic attacks herself:\n\n"
        f"👉 {CALENDAR_LINK}\n\n"
        "You don't have to figure this out alone. 💙",
        parse_mode="HTML",
    )


# ── Learn content ──────────────────────────────────────────────────────────────

LEARN_CONTENT = {
    "learn_heart": (
        "❤️ Racing heart — what's actually happening\n\n"
        "During a panic attack, your brain sends an emergency signal: threat detected. "
        "Your adrenal glands release adrenaline, and your heart rate jumps — sometimes to 140–180 bpm.\n\n"
        "This feels terrifying. But it's not a heart attack.\n\n"
        "Here's the difference:\n"
        "• Heart attacks cause pain that spreads to the jaw or arm, worsens with movement, and doesn't pass on its own\n"
        "• Panic palpitations peak within minutes and fade — always\n\n"
        "What helps: /sigh or /breathe — both slow the heart within minutes by activating the vagus nerve."
    ),
    "learn_breath": (
        "😮‍💨 Can't breathe — the counterintuitive truth\n\n"
        "During panic, you feel like you're not getting enough air. But what's actually happening is the opposite: you're breathing too fast.\n\n"
        "Hyperventilation removes too much CO2 from the blood, which creates that desperate feeling of suffocation — even though your lungs are full.\n\n"
        "You are not suffocating. You are over-breathing.\n\n"
        "What helps: /sigh is the fastest single reset. /breathe for a longer exercise."
    ),
    "learn_dizzy": (
        "💫 Dizziness — why it happens\n\n"
        "Dizziness during panic has two causes: hyperventilation reducing CO2 (less blood flow to the brain), and blood pooling in your legs if you stay still.\n\n"
        "Neither of these will make you faint. Fainting from panic is extremely rare.\n\n"
        "What helps: /breathe to restore CO2 balance. /orient to give your nervous system a safety signal."
    ),
    "learn_unreal": (
        "🌫 Feeling unreal (derealization / depersonalization)\n\n"
        "The world looks foggy or distant, or you feel detached from yourself. Both are common during panic and both are terrifying — but neither means you're losing your mind.\n\n"
        "People with actual psychosis don't worry about going crazy — they're convinced their experience is real. Your fear that it isn't real is evidence you're okay.\n\n"
        "What helps: /ground or /orient — sensory anchoring brings you back into your body."
    ),
    "learn_numb": (
        "🫲 Numbness & tingling — where it comes from\n\n"
        "Hyperventilation changes blood pH and constricts blood vessels in your hands and feet — causing tingling that reverses completely once your breathing normalises.\n\n"
        "What helps: /breathe or /sigh. Tingling usually disappears within 2–3 minutes."
    ),
    "learn_die": (
        "😰 'I'm going to die' — the thought that drives panic\n\n"
        "This thought is a symptom, not a warning. Adrenaline triggers threat-detection, and the mind concludes something must be terribly wrong.\n\n"
        "What research confirms: no one has ever died from a panic attack, and the attack always ends — usually within 10–20 minutes.\n\n"
        "What helps: /panic for real-time support. /breathe to interrupt the cycle."
    ),
    "learn_crazy": (
        "🤯 'I'm going crazy' — why this thought appears\n\n"
        "When physical symptoms don't have an obvious cause, the mind looks for an explanation. But people with actual psychosis don't worry about going crazy — they believe their experience is completely real.\n\n"
        "The very fact that you're asking 'am I losing my mind?' is strong evidence that you are not.\n\n"
        "What helps: /ground or /orient."
    ),
    "learn_sleep": (
        "😴 Sleep problems — the anxiety-sleep loop\n\n"
        "Anxiety and poor sleep feed each other — sympathetic activation primes the body to stay alert, not rest.\n\n"
        "Sleep improves as anxiety reduces, not the other way around. Treating sleep as the primary target often makes it worse.\n\n"
        "What helps before bed: /relax or /hum — both activate the parasympathetic system, the body's sleep-entry pathway."
    ),
}

def learn_keyboard():
    keyboard = [
        [InlineKeyboardButton("❤️ Racing heart", callback_data="learn_heart"),
         InlineKeyboardButton("😮‍💨 Can't breathe", callback_data="learn_breath")],
        [InlineKeyboardButton("💫 Dizziness", callback_data="learn_dizzy"),
         InlineKeyboardButton("🌫 Feeling unreal", callback_data="learn_unreal")],
        [InlineKeyboardButton("🫲 Numbness & tingling", callback_data="learn_numb"),
         InlineKeyboardButton("😰 'I'm dying'", callback_data="learn_die")],
        [InlineKeyboardButton("🤯 'I'm going crazy'", callback_data="learn_crazy"),
         InlineKeyboardButton("😴 Sleep problems", callback_data="learn_sleep")],
    ]
    return InlineKeyboardMarkup(keyboard)

async def learn(update, context):
    log_command(update, "learn")
    await update.effective_message.reply_text(
        "📖 <b>What's happening in my body?</b>\n\n"
        "When panic hits, the symptoms feel terrifying. But each one has a simple explanation — "
        "and understanding it takes away some of its power.\n\nWhat are you experiencing?",
        parse_mode="HTML",
        reply_markup=learn_keyboard()
    )

COMMAND_MAP = {
    "panic": panic, "breathe": breathe, "sigh": sigh,
    "relax": relax, "hum": hum, "cold": cold,
    "ground": ground, "orient": orient, "valsalva": valsalva,
    "push": push, "shake": shake, "yawn": yawn,
    "walk": walk, "hold": hold, "smell": smell, "support": support,
    "butterfly": butterfly, "belly": belly,
}


# ── /stopremind ────────────────────────────────────────────────────────────────

async def stopremind(update, context):
    log_command(update, "stopremind")
    user_id = str(update.effective_user.id)
    chat_id = update.effective_message.chat_id
    prefs = load_prefs()
    if user_id in prefs and "utc_offset" in prefs[user_id]:
        del prefs[user_id]["utc_offset"]
        save_prefs(prefs)
        current_jobs = context.application.job_queue.get_jobs_by_name(str(chat_id))
        for job in current_jobs:
            job.schedule_removal()
        await update.effective_message.reply_text(
            "Reminders turned off. 💙\n\nYou can turn them back on any time with /start."
        )
    else:
        await update.effective_message.reply_text(
            "You don't have any reminders set up. Use /start to set one. 💙"
        )


# ── Admin commands ─────────────────────────────────────────────────────────────

async def users_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    prefs = load_prefs()
    if not prefs:
        await update.effective_message.reply_text("No users yet.")
        return
    lines = []
    for uid, data in prefs.items():
        name = data.get("name", "unknown")
        reminder = "🔔" if "utc_offset" in data else "—"
        member = "💛" if data.get("circle_member") else "—"
        lines.append(f"{member}{reminder} {name} (id: {uid})")
    await update.effective_message.reply_text(f"👥 Users ({len(prefs)} total):\n\n" + "\n".join(lines))

async def broadcast_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /broadcast Your message here")
        return
    message = " ".join(context.args)
    prefs = load_prefs()
    sent = 0
    for uid, data in prefs.items():
        try:
            await context.bot.send_message(chat_id=data["chat_id"], text=message)
            sent += 1
        except Exception:
            pass
    await update.effective_message.reply_text(f"✅ Sent to {sent} users.")

async def bookings_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    bookings = load_bookings()
    if not bookings:
        await update.effective_message.reply_text("No confirmed payments yet.")
        return
    lines = []
    for b in bookings.values():
        label = b.get("label", b.get("package", "Panic Circle"))
        lines.append(f"• {label} — @{b.get('username') or b.get('user_id')} ({b.get('confirmed_at', '')[:10]})")
    await update.effective_message.reply_text(f"📋 Confirmed payments ({len(bookings)}):\n\n" + "\n".join(lines))


# ── Optional 1:1 coaching upsell (unchanged content, reused) ──────────────────

PACKAGES = {
    "pkg_single": {"label": "Single session", "price_cents": 15000, "description": "Single session — $150"},
    "pkg_package3": {"label": "3-session package", "price_cents": 40000, "description": "3-session package — $400 (save $50)"},
}

BIO_TEXT = (
    "<b>A little about me:</b>\n\n"
    "<blockquote>I had panic disorder. Agoraphobia. Social phobia. A flying phobia. "
    "Fear of choking. Fear of accidentally eating something bad.\n\n"
    "Plus inflammation, allergies, food intolerances, gut issues, chronic pain, skin "
    "problems, headaches, eczema.\n\n"
    "<i>For years.</i></blockquote>\n\n"

    "<blockquote>It started with a niacin flush. My whole body went red and burning. "
    "I thought something was seriously wrong with me.\n\n"
    "I didn't leave the house for a month after that. Every time I got near the front "
    "gate my heart would start racing so hard I'd shake. My feet and knees would sweat "
    "so much I'd slip right out of my shoes.</blockquote>\n\n"

    "<i>Why was I fine yesterday, and not today?</i>\n\n"

    "<blockquote>So I started digging. First I thought it was food — histamines. "
    "I cut every high-histamine food out. Didn't help much.\n\n"
    "Then I thought it was sulfur. Cut that too. Helped a little, not enough.\n\n"
    "I kept cutting things until I was down to five ingredients:\n"
    "<code>rice, plain chicken, cucumber, olive oil, parsley</code>\n\n"
    "I lost a lot of weight. I was scared of food.</blockquote>\n\n"

    "<blockquote>Then I heard Matt Blackburn on Instagram say something like: "
    "<i>how do you expect your body to stop reacting to food if it's depleted?</i>\n\n"
    "That hit me. You need certain vitamins just to absorb other vitamins.\n\n"
    "So I started eating again. Gained some weight back.\n\n"
    "<b>My baseline anxiety dropped by more than half.</b> I could finally think "
    "straight.</blockquote>\n\n"

    "<blockquote>Then I went looking into nervous system work. Red light therapy. "
    "Ice baths — I never actually did the ice part, my body had enough stress already, "
    "but I took the idea behind it: train your brain not to panic about stress.\n\n"
    "I used my own panic attacks as that training ground instead — staying as aware "
    "as I could, while it was happening.</blockquote>\n\n"

    "<blockquote>My panic attacks got a lot better. Less frequent. Shorter when they "
    "did happen. A real, definite improvement.\n\n"
    "But I was still avoiding triggers. I'd stopped socializing. Still afraid of "
    "certain foods, afraid of eating at new restaurants. I stopped seeing friends "
    "because I was scared I'd panic in front of them.\n\n"
    "<i>I got really lonely.</i> My whole life had shrunk down to only the things "
    "that wouldn't set off a panic attack.</blockquote>\n\n"

    "<blockquote>After that: polyvagal theory. Hypnotherapy. Generational trauma "
    "work through Mark Wolynn's program. Psychosomatic and health coaching. "
    "Psychology training under Kurpatov.</blockquote>\n\n"

    "<b>None of it alone fixed it. All of it together did.</b>\n\n"

    "<blockquote>I'm not a therapist. I don't have a clinical license.\n\n"
    "What I have is years of digging through my own body's mess until it actually "
    "stopped — and the training to understand why it worked.</blockquote>"
)


def bio_gate_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 About me", callback_data="show_bio")],
        [InlineKeyboardButton("Continue →", callback_data="step_proof")],
    ])

TESTIMONIALS_TEXT = (
    "I'll say this directly: a session with me isn't a script-reading exercise. We follow what's actually happening for you.\n\n"
    "Here's what people who've worked with me say:\n\n"
    '<i>"I\'d tried therapy for two years. The first session with Oxana, she found the actual pattern in twenty minutes."</i>\n\n'
    '<i>"I didn\'t think 1:1 would feel different from a course. It was. We got to the root in one call."</i>'
)

SESSION_FORMAT_TEXT = (
    "<b>Here's exactly what a session includes:</b>\n\n"
    "🕐 60 minutes, one-on-one — video or voice call, your choice\n"
    "🎯 We go straight to what's keeping your nervous system stuck right now — no generic script\n"
    "🛠 Real tools you can use the moment we hang up, not homework you'll never open\n"
    "📝 A short follow-up note after every session so nothing gets lost\n\n"
    "This is the same work behind the techniques you might already know from me — but built around your specific patterns, in real time."
)

PRICING_TEXT = (
    "<b>Here's what's included and what it costs:</b>\n\n"
    "1️⃣ Direct 1:1 access to me — not a group, not a course, not a bot script\n"
    "2️⃣ A real map of your specific panic/anxiety pattern, not generic advice\n"
    "3️⃣ Tools chosen for what's actually happening in your body, tested in the session itself\n"
    "4️⃣ A written follow-up after every session\n"
    "5️⃣ The option to message me between sessions if something comes up\n\n"
    f"💳 {PACKAGES['pkg_single']['description']}\n"
    f"💳 {PACKAGES['pkg_package3']['description']} — most people need at least 3 sessions to see the pattern actually shift\n\n"
    "I only take a limited number of 1:1 clients each month so I can give each person real attention."
)


def continue_keyboard(label, callback_data):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=callback_data)]])

def cta_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, let's book", callback_data="cta_yes_1on1")],
        [InlineKeyboardButton("🤔 Not sure yet", callback_data="cta_unsure_1on1")],
        [InlineKeyboardButton("❓ I have a question", callback_data="cta_question")],
    ])

def package_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton(p["description"], callback_data=key)] for key, p in PACKAGES.items()])


# ── Payment: Panic Circle subscription ($79/mo) ──────────────────────────────

async def create_circle_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    short_id = secrets.token_urlsafe(8)

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "Panic Circle — Monthly Membership"},
                    "unit_amount": 7900,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            success_url=f"https://t.me/{BOT_USERNAME}?start=paid_{short_id}",
            cancel_url=f"https://t.me/{BOT_USERNAME}?start=cancelled",
            metadata={"telegram_user_id": str(user.id), "product": "panic_circle"},
        )
    except Exception:
        log.exception("Stripe session creation failed")
        await update.effective_message.reply_text(
            "Something went wrong setting up the payment. Please try again in a moment, or message me directly. 💙"
        )
        return

    pending = load_pending()
    pending[short_id] = {
        "stripe_session_id": session.id, "user_id": user.id, "chat_id": chat_id,
        "username": user.username, "name": user.first_name, "product": "panic_circle",
        "label": "Panic Circle membership",
        "created_at": datetime.now(timezone.utc).isoformat(), "status": "pending",
    }
    save_pending(pending)

    await update.effective_message.reply_text(
        "Here's your secure payment link. Once it's confirmed, I'll send your group invite right here in this chat. 💙",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pay & Join", url=session.url)]]),
    )


async def get_circle_invite_link(context: ContextTypes.DEFAULT_TYPE, short_id: str) -> str:
    """Returns a single-use invite link if possible, otherwise the static fallback."""
    if not PANIC_CIRCLE_GROUP_ID:
        return CALENDAR_LINK
    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=PANIC_CIRCLE_GROUP_ID,
            member_limit=1,
            name=f"member-{short_id}",
        )
        return invite.invite_link
    except Exception:
        log.exception("Could not create single-use invite link, using fallback")
        await notify_admin(
            context,
            "⚠️ Couldn't auto-create a group invite link — sent the fallback "
            "link instead. Check that the bot is an admin of the Panic Circle "
            "group with 'Invite Users via Link' permission.",
        )
        return CALENDAR_LINK


async def handle_payment_return(update: Update, context: ContextTypes.DEFAULT_TYPE, short_id: str) -> None:
    pending = load_pending()
    record = pending.get(short_id)

    if not record:
        await update.effective_message.reply_text(
            "I can't find that payment session. If you completed payment, message me directly and I'll sort it out right away. 💙"
        )
        return

    if record["status"] == "confirmed":
        link = record.get("invite_link", CALENDAR_LINK)
        await update.effective_message.reply_text(f"You're already in! 🎉 Here's your link again:\n\n{link}")
        return

    try:
        session = stripe.checkout.Session.retrieve(record["stripe_session_id"])
    except Exception:
        log.exception("Stripe session retrieval failed")
        await update.effective_message.reply_text(
            "I couldn't verify the payment just now. If you completed it, give it a moment and try /start again, or message me directly. 💙"
        )
        return

    paid = session.payment_status == "paid" or getattr(session, "status", None) == "complete"
    if paid:
        invite_link = await get_circle_invite_link(context, short_id)

        record["status"] = "confirmed"
        record["confirmed_at"] = datetime.now(timezone.utc).isoformat()
        record["invite_link"] = invite_link
        pending[short_id] = record
        save_pending(pending)

        bookings = load_bookings()
        bookings[short_id] = record
        save_bookings(bookings)

        user_id = str(record["user_id"])
        prefs = load_prefs()
        prefs.setdefault(user_id, {})["circle_member"] = True
        save_prefs(prefs)

        await update.effective_message.reply_text(
            "<b>Payment confirmed! 🎉 Welcome to the Panic Circle.</b>\n\n"
            "Here's your link to join — it only works once, so it's just for you:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💛 Join the Group", url=invite_link)]]),
        )
        await notify_admin(
            context,
            f"💰 New Panic Circle member: @{record.get('username') or record.get('user_id')} ({record.get('name')})",
        )
    else:
        await update.effective_message.reply_text(
            "Looks like the payment isn't complete yet. If you already paid, give it a minute and tap the link again, or message me directly. 💙"
        )


# ── Payment: optional 1:1 coaching (unchanged logic, one-time payment) ───────

async def create_1on1_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, package_key: str) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    package = PACKAGES[package_key]
    short_id = secrets.token_urlsafe(8)

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"1:1 Coaching — {package['label']}"},
                    "unit_amount": package["price_cents"],
                },
                "quantity": 1,
            }],
            success_url=f"https://t.me/{BOT_USERNAME}?start=paid_{short_id}",
            cancel_url=f"https://t.me/{BOT_USERNAME}?start=cancelled",
            metadata={"telegram_user_id": str(user.id), "package": package_key},
        )
    except Exception:
        log.exception("Stripe session creation failed")
        await update.effective_message.reply_text(
            "Something went wrong setting up the payment. Please try again in a moment, or message me directly. 💙"
        )
        return

    pending = load_pending()
    pending[short_id] = {
        "stripe_session_id": session.id, "user_id": user.id, "chat_id": chat_id,
        "username": user.username, "name": user.first_name, "package": package_key,
        "product": "1on1", "label": package["label"],
        "created_at": datetime.now(timezone.utc).isoformat(), "status": "pending",
    }
    save_pending(pending)

    await update.effective_message.reply_text(
        "Great — here's your secure payment link. Once it's confirmed, I'll send your booking link right here in this chat. 💙",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pay & Book", url=session.url)]]),
    )


# ── Callback router ────────────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
  
    data = query.data
    message = query.message
    chat_id = message.chat_id
    user_id = str(query.from_user.id)

    # Panic Circle funnel
    if data == "screen2":
        await send_screen(message, chat_id, "screen2", SCREEN2_TEXT, screen2_keyboard(), context)
        return
    if data == "screen3":
        await send_screen(message, chat_id, "screen3", SCREEN3_TEXT, single_button("I feel this", "screen4"), context)
        return
    if data == "screen4":
        await send_screen(message, chat_id, "screen4", SCREEN4_TEXT, single_button("Continue", "screen5"), context)
        return
    if data == "screen5":
        await send_screen(message, chat_id, "screen5", SCREEN5_TEXT, single_button("Continue", "price"), context)
        return
    if data == "price":
        await send_screen(message, chat_id, "price", PRICE_TEXT, price_cta_keyboard(), context)
        return
    if data == "cta_yes":
        await create_circle_payment(update, context)
        return
    if data == "cta_unsure":
        await message.reply_text(
            "Totally fair — this is a real decision, not a small one. 💙\n\n"
            "Here's something free in the meantime, no strings attached:",
        )
        await send_cheatsheet(update, context)
        await message.reply_text("Whenever you're ready, I'm right here:", reply_markup=make_menu())
        return
    if data == "cta_question":
        context.user_data["awaiting_question"] = True
        await message.reply_text("Type your question below and I'll personally get back to you. 💙")
        return

    # Free cheat sheet from the menu
    if data == "get_cheatsheet":
        await send_cheatsheet(update, context)
        return

    # Optional 1:1 coaching upsell
    if data == "book_coaching":
        await message.reply_text(
            "Before we talk about sessions — want to know my actual story? "
            "Totally optional, no pressure either way.",
            reply_markup=bio_gate_keyboard(),
        )
        return
    if data == "show_bio":
        await message.reply_text(BIO_TEXT, parse_mode="HTML",
                                  reply_markup=continue_keyboard("See what real sessions look like", "step_proof"))
        return
    if data == "bio_s2":
        await message.reply_text(BIO_TEXT, parse_mode="HTML",
                                  reply_markup=single_button("Show me how to get better", "screen3"))
        return
    if data == "step_proof":
        await message.reply_text(TESTIMONIALS_TEXT, parse_mode="HTML",
                                  reply_markup=continue_keyboard("👉 See how a session works", "step_format"))
        return
    if data == "step_format":
        await message.reply_text(SESSION_FORMAT_TEXT, parse_mode="HTML",
                                  reply_markup=continue_keyboard("💰 See pricing", "step_pricing"))
        return
    if data == "step_pricing":
        await message.reply_text(PRICING_TEXT, parse_mode="HTML", reply_markup=cta_keyboard())
        return
    if data == "cta_yes_1on1":
        await message.reply_text("Which works for you?", reply_markup=package_keyboard())
        return
    if data == "cta_unsure_1on1":
        await message.reply_text(
            "Totally fair — this is a real decision, not a small one. 💙\n\nNo pressure. If you want to see the options anyway, they're right here:",
            reply_markup=package_keyboard(),
        )
        return
    if data in PACKAGES:
        await create_1on1_payment(update, context, data)
        return

    # Exercise menus
    if data in COMMAND_MAP:
        await COMMAND_MAP[data](update, context)
        return
    if data == "remind_yes":
        await message.reply_text("Great! 🙌 Where in the world are you?", reply_markup=timezone_keyboard())
        return
    if data == "remind_no":
        await message.reply_text("No problem! You can always turn it on later with /start. 💙")
        return
    if data in LEARN_CONTENT:
        await message.reply_text(LEARN_CONTENT[data])
        return
    if data == "menu_panic":
        await message.reply_text("🚨 <b>In Panic Now</b>\n\nPick any — there's no wrong choice:",
                                  parse_mode="HTML", reply_markup=make_panic_menu())
        return
    if data == "menu_ns":
        await message.reply_text(
            "🌿 <b>Calm Your Nervous System</b>\n\nThese work best with regular practice — but help any time:",
            parse_mode="HTML", reply_markup=make_ns_menu())
        return
    if data == "back_menu":
        prefs = load_prefs()
        is_member = prefs.get(user_id, {}).get("circle_member", False)
        await message.reply_text("Choose where you'd like to go 💙", reply_markup=make_menu(is_member=is_member))
        return
    if data == "learn_menu":
        await message.reply_text(
            "📖 <b>What's happening in my body?</b>\n\nWhat are you experiencing?",
            parse_mode="HTML", reply_markup=learn_keyboard())
        return
    if data.startswith("tz_"):
        utc_offset = int(data[3:])
        prefs = load_prefs()
        prefs.setdefault(user_id, {})
        prefs[user_id]["chat_id"] = chat_id
        prefs[user_id]["utc_offset"] = utc_offset
        save_prefs(prefs)
        schedule_reminder(context.application, chat_id, utc_offset)
        region = TIMEZONE_NAMES.get(str(utc_offset), f"UTC{'+' if utc_offset >= 0 else ''}{utc_offset}")
        await message.reply_text(
            f"Done! ✅ I'll send you a daily exercise reminder at 10:30am ({region}).\n\nYou can turn it off any time with /stopremind. 💙"
        )
        return


# ── Free text ──────────────────────────────────────────────────────────────────

async def menu_button(update, context):
    user_id = str(update.effective_user.id)
    prefs = load_prefs()
    is_member = prefs.get(user_id, {}).get("circle_member", False)
    await update.effective_message.reply_text("Choose where you'd like to go 💙", reply_markup=make_menu(is_member=is_member))

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message.voice:
        await voice_capture(update, context)
        return

    if context.user_data.get("awaiting_question"):
        context.user_data["awaiting_question"] = False
        user = update.effective_user
        await notify_admin(
            context,
            f"❓ Question from @{user.username or user.id} ({user.first_name}):\n\n{update.effective_message.text}",
        )
        await update.effective_message.reply_text("Got it — I'll personally get back to you on this within 24h. 💙")
        return

    text = (update.effective_message.text or "").lower()
    name = update.effective_user.first_name or ""
    greeting = f"{name}, " if name else ""

    if any(w in text for w in ["panic", "attack", "dying", "die", "heart", "can't breathe", "cant breathe", "help me"]):
        await panic(update, context)
    elif any(w in text for w in ["scared", "afraid", "fear", "anxious", "anxiety", "nervous"]):
        await update.effective_message.reply_text(f"{greeting}I hear you. 💙\n\nTry one of these right now:", reply_markup=make_menu())
    elif any(w in text for w in ["breathe", "breathing", "breath", "air"]):
        await breathe(update, context)
    elif any(w in text for w in ["dizzy", "dizziness", "spinning", "faint"]):
        await sigh(update, context)
    elif any(w in text for w in ["can't sleep", "cant sleep", "sleep", "awake", "insomnia"]):
        await relax(update, context)
    elif any(w in text for w in ["hi", "hello", "hey"]):
        await update.effective_message.reply_text(f"Hi {name}! 💙 I'm here. Tap Menu below whenever you're ready.", reply_markup=make_menu())
    else:
        await update.effective_message.reply_text("I'm here with you. 💙\n\nTap Menu to find something that might help:", reply_markup=make_menu())


async def track_member(update, context):
    result = update.my_chat_member
    user = result.from_user
    username = f"@{user.username}" if user.username else f"user_id:{user.id}"
    name = user.first_name or ""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if result.new_chat_member.status == "kicked":
        line = f"[{timestamp}] {username} ({name}) left the bot"
        print(line)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"👋 {line}")


async def error_handler(update, context):
    print(f"Error: {context.error}")
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something went wrong on my end. 💙 Please try again.")


async def weekly_summary(context):
    prefs = load_prefs()
    total = len(prefs)
    members = sum(1 for d in prefs.values() if d.get("circle_member"))
    reminded = sum(1 for d in prefs.values() if "utc_offset" in d)
    try:
        log_lines = open(LOG_FILE).readlines()
        commands = {}
        for line in log_lines[-500:]:
            if "used /" in line:
                cmd = line.split("used /")[1].strip()
                commands[cmd] = commands.get(cmd, 0) + 1
        top = sorted(commands.items(), key=lambda x: x[1], reverse=True)[:3]
        top_str = "\n".join([f"  /{cmd}: {n}x" for cmd, n in top])
    except Exception:
        top_str = "no data"
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📊 Weekly summary\n\n👥 Total users: {total}\n💛 Circle members: {members}\n🔔 With reminders: {reminded}\n\nTop commands:\n{top_str}"
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("panic", panic))
    app.add_handler(CommandHandler("breathe", breathe))
    app.add_handler(CommandHandler("ground", ground))
    app.add_handler(CommandHandler("sigh", sigh))
    app.add_handler(CommandHandler("relax", relax))
    app.add_handler(CommandHandler("hum", hum))
    app.add_handler(CommandHandler("cold", cold))
    app.add_handler(CommandHandler("valsalva", valsalva))
    app.add_handler(CommandHandler("orient", orient))
    app.add_handler(CommandHandler("push", push))
    app.add_handler(CommandHandler("shake", shake))
    app.add_handler(CommandHandler("yawn", yawn))
    app.add_handler(CommandHandler("walk", walk))
    app.add_handler(CommandHandler("hold", hold))
    app.add_handler(CommandHandler("smell", smell))
    app.add_handler(CommandHandler("butterfly", butterfly))
    app.add_handler(CommandHandler("belly", belly))
    app.add_handler(CommandHandler("support", support))
    app.add_handler(CommandHandler("learn", learn))
    app.add_handler(CommandHandler("stopremind", stopremind))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("bookings", bookings_command))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^Menu$"), menu_button))
    app.add_handler(MessageHandler(filters.VOICE, voice_capture))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatMemberHandler(track_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(error_handler)

    load_all_reminders(app)
    app.job_queue.run_daily(weekly_summary, time=dtime(9, 0), days=(0,), name="weekly_summary")

    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
