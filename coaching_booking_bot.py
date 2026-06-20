"""
1:1 Coaching Booking Bot
-------------------------
A Telegram bot that walks a lead through a short qualifying + trust-building
sequence, then sells a 1:1 coaching session or package via Stripe Checkout,
and hands them a booking link the moment payment is confirmed.

No public webhook server is required: after payment, Stripe redirects the
browser straight back into Telegram (t.me/<bot>?start=paid_<token>), and the
bot verifies the payment via the Stripe API the moment that /start fires.

Commands:
    /start     Begin (or restart) the booking flow
    /help      List available commands
    /bookings  (admin only) List confirmed bookings

Setup:
    1. pip install -r requirements.txt
    2. Copy .env.example to .env and fill in your values
    3. python coaching_booking_bot.py

See README.md for full setup + customization instructions.
"""

import json
import logging
import os
import secrets
from datetime import datetime, timezone

import stripe
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Load a .env file automatically if python-dotenv is installed and one exists.
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


# ── Required configuration (set as environment variables, see .env.example) ─

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = _require_env("TELEGRAM_BOT_USERNAME").lstrip("@")
ADMIN_ID = int(_require_env("ADMIN_TELEGRAM_ID"))
stripe.api_key = _require_env("STRIPE_SECRET_KEY")

CALENDAR_LINK = os.environ.get(
    "CALENDAR_LINK", "https://calendar.app.google/UdVSNtXD6Xnfyord9"
)

# On platforms like Railway, the filesystem resets on every redeploy unless
# you attach a persistent Volume. Set DATA_DIR to that volume's mount path
# (e.g. "/data") so booking records survive deploys. Defaults to the local
# folder, which is fine for local/VPS use where the disk is already durable.
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
PENDING_FILE = os.path.join(DATA_DIR, "pending_payments.json")
BOOKINGS_FILE = os.path.join(DATA_DIR, "bookings.json")


# ── Editable content ─────────────────────────────────────────────────────────
# Everything below is copy you can freely rewrite without touching any of the
# logic further down the file.

PACKAGES = {
    "pkg_single": {
        "label": "Single session",
        "price_cents": 15000,
        "description": "Single session — $150",
    },
    "pkg_package3": {
        "label": "3-session package",
        "price_cents": 40000,
        "description": "3-session package — $400 (save $50)",
    },
}

QUALIFYING_OPTIONS = [
    ("screen2_yes", "Yep"),
    ("screen2_no", "Not really"),
]

VALIDATION_RESPONSES = {
    "screen2_yes": (
        "Have you learned all the nervous system tricks...\n\n"
        "but you want to just enjoy life and not have to constantly check if you are regulated?"
    ),
    "screen2_no": (
        "Have you learned all the nervous system tricks...\n\n"
        "but you want to just enjoy life and not have to constantly check if you are regulated?"
    ),
}

BIO_TEXT = (
    "<b>A little about me:</b>\n\n"
    "• I've spent years working 1:1 with people on panic attacks and "
    "nervous system regulation\n"
    "• My approach is built on the actual neuroscience of panic — Selye, "
    "Sapolsky, Porges, Levine — not generic advice\n"
    "• I've been through this myself. This isn't theory I read about — "
    "it's something I lived and came out the other side of\n\n"
    "I built a course on this (<i>The Brain That Saved You</i>), but most "
    "of the real shifts happen 1:1 — when we can go straight to what's "
    "actually keeping your nervous system stuck."
)

# Testimonials can be plain text quotes, or you can swap these for real
# voice messages later — see README.md for how to grab a Telegram file_id
# and send voice notes instead of text.
TESTIMONIALS_TEXT = (
    "I'll say this directly: a session with me isn't a script-reading "
    "exercise. We follow what's actually happening for you.\n\n"
    "Here's what people who've worked with me say:\n\n"
    '<i>"I\'d tried therapy for two years. The first session with Oxana, '
    'she found the actual pattern in twenty minutes."</i>\n\n'
    '<i>"I didn\'t think 1:1 would feel different from a course. It was. '
    'We got to the root in one call."</i>'
)

SESSION_FORMAT_TEXT = (
    "<b>Here's exactly what a session includes:</b>\n\n"
    "🕐 60 minutes, one-on-one — video or voice call, your choice\n"
    "🎯 We go straight to what's keeping your nervous system stuck right "
    "now — no generic script\n"
    "🛠 Real tools you can use the moment we hang up, not homework you'll "
    "never open\n"
    "📝 A short follow-up note after every session so nothing gets lost\n\n"
    "This is the same work behind the techniques you might already know "
    "from me — but built around your specific patterns, in real time."
)

PRICING_TEXT = (
    "<b>Here's what's included and what it costs:</b>\n\n"
    "1️⃣ Direct 1:1 access to me — not a group, not a course, not a bot "
    "script\n"
    "2️⃣ A real map of your specific panic/anxiety pattern, not generic "
    "advice\n"
    "3️⃣ Tools chosen for what's actually happening in your body, tested "
    "in the session itself\n"
    "4️⃣ A written follow-up after every session\n"
    "5️⃣ The option to message me between sessions if something comes "
    "up\n\n"
    f"💳 {PACKAGES['pkg_single']['description']}\n"
    f"💳 {PACKAGES['pkg_package3']['description']} — most people need at "
    "least 3 sessions to see the pattern actually shift\n\n"
    "I only take a limited number of 1:1 clients each month so I can give "
    "each person real attention — so if this is calling to you, don't "
    "wait too long to grab a spot."
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


def load_pending() -> dict:
    return _load(PENDING_FILE)


def save_pending(data: dict) -> None:
    _save(PENDING_FILE, data)


def load_bookings() -> dict:
    return _load(BOOKINGS_FILE)


def save_bookings(data: dict) -> None:
    _save(BOOKINGS_FILE, data)


# ── Keyboards ─────────────────────────────────────────────────────────────────

def qualifying_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=key)] for key, label in QUALIFYING_OPTIONS]
    )


def continue_keyboard(label: str, callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=callback_data)]])


def cta_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Yes, let's book", callback_data="cta_yes")],
            [InlineKeyboardButton("🤔 Not sure yet", callback_data="cta_unsure")],
            [InlineKeyboardButton("❓ I have a question", callback_data="cta_question")],
        ]
    )


def package_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(p["description"], callback_data=key)] for key, p in PACKAGES.items()]
    )


# ── Admin notify ──────────────────────────────────────────────────────────────

async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text)
    except Exception:
        log.exception("Failed to notify admin")


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if args:
        payload = args[0]
        if payload.startswith("paid_"):
            await handle_payment_return(update, context, payload[len("paid_"):])
            return
        if payload == "cancelled":
            await update.effective_message.reply_text(
                "No worries — the offer's still here whenever you're ready. 💙",
                reply_markup=package_keyboard(),
            )
            return

    name = update.effective_user.first_name or ""
    await update.effective_message.reply_text(
        "Imagine how frustrating this feels...\n\n"
        "Every morning you wake up and have to regulate your nervous system before starting your day.\n\n"
        "Breathing.\n"
        "Meditation.\n"
        "Grounding.\n"
        "Cold showers.\n"
        "Journaling.\n\n"
        "You finally feel calm.\n\n"
        "You finally feel better.\n\n"
        "And then the next day...\n\n"
        "it's back.\n\n"
        "Like you have to start all over again.",
        reply_markup=qualifying_keyboard(),
    )


# ── /help ─────────────────────────────────────────────────────────────────────

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "/start — Begin (or restart) the booking flow\n/help — This message"
    )


# ── Callback router ───────────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    message = query.message

    if data in VALIDATION_RESPONSES:
        await message.reply_text(
            VALIDATION_RESPONSES[data] + "\n\nLet me tell you a bit about how I work.",
            reply_markup=continue_keyboard("Continue", "step_bio"),
        )
        return

    if data == "step_bio":
        await message.reply_text(
            BIO_TEXT,
            parse_mode="HTML",
            reply_markup=continue_keyboard("See what real sessions look like", "step_proof"),
        )
        return

    if data == "step_proof":
        await message.reply_text(
            TESTIMONIALS_TEXT,
            parse_mode="HTML",
            reply_markup=continue_keyboard("👉 See how a session works", "step_format"),
        )
        return

    if data == "step_format":
        await message.reply_text(
            SESSION_FORMAT_TEXT,
            parse_mode="HTML",
            reply_markup=continue_keyboard("💰 See pricing", "step_pricing"),
        )
        return

    if data == "step_pricing":
        await message.reply_text(PRICING_TEXT, parse_mode="HTML", reply_markup=cta_keyboard())
        return

    if data == "cta_yes":
        await message.reply_text("Which works for you?", reply_markup=package_keyboard())
        return

    if data == "cta_unsure":
        await message.reply_text(
            "Totally fair — this is a real decision, not a small one. 💙\n\n"
            "No pressure. If you want to see the options anyway, they're "
            "right here:",
            reply_markup=package_keyboard(),
        )
        return

    if data == "cta_question":
        context.user_data["awaiting_question"] = True
        await message.reply_text(
            "Type your question below and I'll personally get back to you. 💙"
        )
        return

    if data in PACKAGES:
        await create_payment(update, context, data)
        return


# ── Payment ────────────────────────────────────────────────────────────────────

async def create_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, package_key: str) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    package = PACKAGES[package_key]
    short_id = secrets.token_urlsafe(8)

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": f"1:1 Coaching — {package['label']}"},
                        "unit_amount": package["price_cents"],
                    },
                    "quantity": 1,
                }
            ],
            success_url=f"https://t.me/{BOT_USERNAME}?start=paid_{short_id}",
            cancel_url=f"https://t.me/{BOT_USERNAME}?start=cancelled",
            metadata={"telegram_user_id": str(user.id), "package": package_key},
        )
    except Exception:
        log.exception("Stripe session creation failed")
        await update.effective_message.reply_text(
            "Something went wrong setting up the payment. Please try again "
            "in a moment, or message me directly. 💙"
        )
        return

    pending = load_pending()
    pending[short_id] = {
        "stripe_session_id": session.id,
        "user_id": user.id,
        "chat_id": chat_id,
        "username": user.username,
        "name": user.first_name,
        "package": package_key,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }
    save_pending(pending)

    await update.effective_message.reply_text(
        "Great — here's your secure payment link. Once it's confirmed, "
        "I'll send your booking link right here in this chat. 💙",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pay & Book", url=session.url)]]),
    )


async def handle_payment_return(update: Update, context: ContextTypes.DEFAULT_TYPE, short_id: str) -> None:
    pending = load_pending()
    record = pending.get(short_id)

    if not record:
        await update.effective_message.reply_text(
            "I can't find that payment session. If you completed payment, "
            "message me directly and I'll sort it out right away. 💙"
        )
        return

    if record["status"] == "confirmed":
        await update.effective_message.reply_text(
            f"You're already booked! 🎉 Here's your link again:\n\n{CALENDAR_LINK}"
        )
        return

    try:
        session = stripe.checkout.Session.retrieve(record["stripe_session_id"])
    except Exception:
        log.exception("Stripe session retrieval failed")
        await update.effective_message.reply_text(
            "I couldn't verify the payment just now. If you completed it, "
            "give it a moment and try /start again, or message me "
            "directly. 💙"
        )
        return

    if session.payment_status == "paid":
        record["status"] = "confirmed"
        record["confirmed_at"] = datetime.now(timezone.utc).isoformat()
        pending[short_id] = record
        save_pending(pending)

        bookings = load_bookings()
        bookings[short_id] = record
        save_bookings(bookings)

        package = PACKAGES[record["package"]]
        await update.effective_message.reply_text(
            "Payment confirmed! 🎉 Welcome aboard.\n\nHere's where to pick "
            "your session time:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📅 Pick your time", url=CALENDAR_LINK)]]
            ),
        )
        await notify_admin(
            context,
            f"💰 New booking: {package['label']} — "
            f"@{record.get('username') or record.get('user_id')} "
            f"({record.get('name')})",
        )
    else:
        await update.effective_message.reply_text(
            "Looks like the payment isn't complete yet. If you already "
            "paid, give it a minute and tap the link again, or message me "
            "directly. 💙"
        )


# ── Free text (question relay) ────────────────────────────────────────────────

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("awaiting_question"):
        context.user_data["awaiting_question"] = False
        user = update.effective_user
        await notify_admin(
            context,
            f"❓ Question from @{user.username or user.id} ({user.first_name}):\n\n"
            f"{update.effective_message.text}",
        )
        await update.effective_message.reply_text(
            "Got it — I'll personally get back to you on this within 24h. 💙"
        )
        return

    await update.effective_message.reply_text(
        "I'm best at guiding you through buttons — tap /start to begin, or "
        "use the buttons in our last message. 💙"
    )


# ── Admin: list confirmed bookings ────────────────────────────────────────────

async def bookings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    bookings = load_bookings()
    if not bookings:
        await update.effective_message.reply_text("No confirmed bookings yet.")
        return
    lines = []
    for b in bookings.values():
        package = PACKAGES.get(b["package"], {}).get("label", b["package"])
        lines.append(
            f"• {package} — @{b.get('username') or b.get('user_id')} "
            f"({b.get('confirmed_at', '')[:10]})"
        )
    await update.effective_message.reply_text(
        f"📋 Confirmed bookings ({len(bookings)}):\n\n" + "\n".join(lines)
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("bookings", bookings_command))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
