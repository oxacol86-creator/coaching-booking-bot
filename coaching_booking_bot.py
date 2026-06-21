"""
Panic Circle Membership Bot
-------------------------
A Telegram bot that walks a lead through a short story-driven sequence,
then sells a Panic Circle membership ($79/month) via Stripe Checkout, and
hands them the private group invite link the moment payment is confirmed.

No public webhook server is required: after payment, Stripe redirects the
browser straight back into Telegram (t.me/<bot>?start=paid_<token>), and the
bot verifies the payment via the Stripe API the moment that /start fires.

Commands:
    /start     Begin (or restart) the funnel
    /help      List available commands
    /bookings  (admin only) List confirmed Panic Circle members

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


# ── Voice notes (optional) ────────────────────────────────────────────────────
# Add your own voice notes here, one per screen. Leave a screen set to None
# to skip it — the bot will just send the text like normal.
#
# How to fill these in:
#   1. Record a voice note in Telegram and send it to your OWN bot.
#   2. The bot will reply with a long code (the "file_id"). Copy it.
#   3. Paste it here, in quotes, replacing the None for that screen.
#   4. Commit and redeploy. Done — the bot will now play your voice note
#      right before that screen's text every time.
VOICE_NOTES = {
    "screen2": "AwACAgEAAxkBAAMqajaMEjumzp2scWEoC8yO2Cs2_ZYAAqsGAAKRwrBFarK4028IeCk8BA",
    "screen3": "AwACAgEAAxkBAAMuajaMsenjKSxy6RRM4Inu4YGT5uUAAqwGAAKRwrBF5XiWI5aOQqI8BA",
    "screen4": "AwACAgEAAxkBAAMyajaNI6KSKh8I2x07wptKw3UF0jIAAq0GAAKRwrBFh1LVpKczQUs8BA",
    "screen5": "AwACAgEAAxkBAAM6ajaP1yLwSDGjqaa-aeP9SC06qnYAArEGAAKRwrBFP21dHvQC3cM8BA",
    "step_pricing": None,
}


# ── Editable content ─────────────────────────────────────────────────────────
# Everything below is copy you can freely rewrite without touching any of the
# logic further down the file.

PACKAGES = {
    "pkg_circle": {
        "label": "Panic Circle Membership",
        "price_cents": 7900,
        "description": "Panic Circle — $79/month",
        "interval": "month",
    },
}

SCREEN2_TEXT = (
    "Hi, I'm Oxana and I've found what actually stops panic attacks "
    "(not just calms them).\n\n"
    "<blockquote>How frustrating it is when you wake up in the morning hoping to "
    "have a good day instead you viscerally feel a panic building...</blockquote>\n\n"
    "<blockquote>You then start your \"calming\" routine:\n\n"
    "• Breathing\n"
    "• Maybe a warm bath to warm up your shivering body\n"
    "• Ice on the neck or hands?\n"
    "• Vagus nerve exercises?\n"
    "• Other nervous system regulating techniques...</blockquote>\n\n"
    "<blockquote>...just to feel normal...</blockquote>\n\n"
    "Other people just go about their days, while you feel betrayed by "
    "your own body."
)

SCREEN3_TEXT = (
    "You've tried all the tricks. 😩\n\n"
    "Breathing. Red lamp therapy. Warm baths. Cold showers. Ice on "
    "your neck.\n\n"
    "You got really good at it.\n\n"
    "But is this just going to be your life now?\n\n"
    "Doing all the tricks every morning, just to feel normal?\n\n"
    "You don't want to manage it.\n\n"
    "You want it gone."
)

SCREEN4_TEXT = (
    "Calming a panic attack and stopping it from coming back are not the "
    "same thing.\n\n"
    "All those nervous system regulating techniques? They calm it "
    "down. 🌬️🧘\n\n"
    "But they don't ask why it started in the first place.\n\n"
    "And if you never ask that question...\n\n"
    "<b>it comes back.</b> 🔁"
)

SCREEN5_TEXT = (
    "<b>That's why I built the Panic Circle.</b>\n\n"
    "I know you know all the tricks. You're doing them every day, and "
    "you have to keep doing them, again and again.\n\n"
    "I wanted us to do the work that actually looks at where this is "
    "coming from. Not just managing it — figuring out how to stop it "
    "from coming back.\n\n"
    "Here's what's inside. 👇\n\n"
    "🫂 People who get it. Not strangers.\n\n"
    "🧠 Videos, scripts, and tools you can use the same day.\n\n"
    "💬 A place to ask anything, anytime.\n\n"
    "📅 Every day I go through the chat and answer questions.\n\n"
    "🤝 Backup the moment you need it, not three days later.\n\n"
    "🎉 Wins from people just like you, so you can see it's possible."
)

# This is the price reveal — it runs right after Screen 5. The 1:1 mention
# at the end is intentional: Panic Circle is the main offer, 1:1 is just a
# small note for people who want more.
OFFER_TEXT = (
    "Here's how it works.\n\n"
    "You join. ✅\n\n"
    "You're in the group the same day. ⚡\n\n"
    "No calls to schedule.\n\n"
    "No pressure to perform.\n\n"
    "Just support. Real tools. One question answered by me, every day. 💬\n\n"
    "You've probably already spent more than $79 on things that only "
    "worked for a day. 🛁🥶\n\n"
    f"💰 <b>{PACKAGES['pkg_circle']['description']}</b>\n"
    "<b>Cancel anytime.</b> No contract. 🔓\n\n"
    "That's less than one single session with me. And here, you get "
    "me every single day — not just once.\n\n"
    "P.S. Still want more than that? I do one-on-one too. Just ask. 💛"
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

def yes_no_keyboard(yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Yep", callback_data=yes_data)],
            [InlineKeyboardButton("Not really", callback_data=no_data)],
        ]
    )


def continue_keyboard(label: str, callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=callback_data)]])


def cta_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ I'm in", callback_data="cta_yes")],
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


# ── Screens (text + optional voice note) ──────────────────────────────────────

async def send_screen(message, screen_key: str, text: str, reply_markup) -> None:
    """Sends a screen: plays the voice note for it first (if one is set in
    VOICE_NOTES), then sends the text with its buttons."""
    voice_id = VOICE_NOTES.get(screen_key)
    if voice_id:
        try:
            await message.reply_voice(voice=voice_id)
        except Exception:
            log.exception(f"Failed to send voice note for {screen_key}")
    await message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)


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

    await send_screen(message, "screen2", SCREEN2_TEXT, yes_no_keyboard("screen2_yes", "screen2_no"))


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

    if data == "screen2":
        await send_screen(message, "screen2", SCREEN2_TEXT, yes_no_keyboard("screen2_yes", "screen2_no"))
        return

    if data in ("screen2_yes", "screen2_no"):
        await send_screen(message, "screen3", SCREEN3_TEXT, yes_no_keyboard("screen3_yes", "screen3_no"))
        return

    if data in ("screen3_yes", "screen3_no"):
        await send_screen(message, "screen4", SCREEN4_TEXT, continue_keyboard("Continue", "screen5"))
        return

    if data == "screen5":
        await send_screen(message, "screen5", SCREEN5_TEXT, continue_keyboard("Continue", "step_pricing"))
        return

    if data == "step_pricing":
        await send_screen(message, "step_pricing", OFFER_TEXT, cta_keyboard())
        return

    if data == "cta_yes":
        await message.reply_text("Tap below to join 👇", reply_markup=package_keyboard())
        return

    if data == "cta_unsure":
        await message.reply_text(
            "Totally fair. 💙\n\n"
            "No pressure.\n\n"
            "Here it is, if you change your mind:",
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
            mode="subscription",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": package["label"]},
                        "unit_amount": package["price_cents"],
                        "recurring": {"interval": package["interval"]},
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
        "Here's your payment link.\n\n"
        "Once it goes through, I'll send your invite right here. 💙",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Join the Panic Circle", url=session.url)]]),
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
            f"You're already in! 🎉 Here's your group link again:\n\n{CALENDAR_LINK}"
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
            "You're in! 🎉 <b>Welcome to the Panic Circle.</b>\n\nHere's "
            "your invite link to the group:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("👉 Join the group", url=CALENDAR_LINK)]]
            ),
        )
        await notify_admin(
            context,
            f"💰 New Panic Circle member: {package['label']} — "
            f"@{record.get('username') or record.get('user_id')} "
            f"({record.get('name')})",
        )
    else:
        await update.effective_message.reply_text(
            "Looks like the payment isn't complete yet. If you already "
            "paid, give it a minute and tap the link again, or message me "
            "directly. 💙"
        )


# ── Voice note capture (admin only) ───────────────────────────────────────────
# Send a voice note to your own bot, and it replies with the file_id you need
# for VOICE_NOTES above. Only works for you (the admin) — everyone else's
# voice notes are ignored.

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    file_id = update.message.voice.file_id
    await update.message.reply_text(
        "Got it. Here's the file_id — copy this into VOICE_NOTES in the "
        f"code:\n\n{file_id}"
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
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
