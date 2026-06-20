# 1:1 Coaching Booking Bot

A Telegram bot that runs leads through a short trust-building sequence
(qualifying question → credibility → social proof → format → pricing) and
then takes payment via Stripe Checkout, sending a booking link the moment
payment is confirmed.

## How it works (no webhook server needed)

1. Person taps through the funnel and picks a package.
2. Bot creates a Stripe Checkout Session and sends them a "Pay & Book" link.
3. After paying, Stripe redirects their browser straight back into Telegram
   (`t.me/your_bot?start=paid_<token>`).
4. That `/start` fires, the bot looks up the token, asks Stripe whether the
   session is paid, and if so — sends the calendar link + notifies you.

This avoids needing a public server to receive Stripe webhooks. The only
downside: if someone closes their browser tab *before* the redirect fires,
they won't get auto-confirmed (rare, but if you want bulletproof webhook
capture later, that's a small addition — just ask).

## Setup

1. **Create the bot**: message [@BotFather](https://t.me/BotFather) on
   Telegram → `/newbot` → follow the prompts → copy the token it gives you
   and the `@username` you chose.

2. **Get Stripe keys**: go to
   [dashboard.stripe.com/apikeys](https://dashboard.stripe.com/apikeys).
   Start with the **test** secret key (`sk_test_...`) so you can run through
   a fake payment first. Switch to the **live** key (`sk_live_...`) only
   once you've tested the whole flow.

3. **Configure**:
   ```
   cp .env.example .env
   ```
   Then open `.env` and fill in `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`,
   `ADMIN_TELEGRAM_ID` (your own numeric Telegram ID), and `STRIPE_SECRET_KEY`.

4. **Install & run**:
   ```
   pip install -r requirements.txt --break-system-packages
   python coaching_booking_bot.py
   ```

5. **Test it for real before going live**: tap through the bot yourself,
   pick a package, and on the Stripe Checkout page use the test card
   `4242 4242 4242 4242`, any future expiry date, any CVC. Confirm you land
   back in the chat with the booking link and that you (the admin) get the
   notification.

6. **Go live**: swap `STRIPE_SECRET_KEY` in `.env` for your `sk_live_...`
   key, restart the bot.

## Deploying on Railway (same platform as your panic bot)

1. **Push these files to a new GitHub repo.** Easiest way without git on
   your machine: create a new repo on GitHub (e.g. `coaching-booking-bot`),
   then use the "Add file → Upload files" button on the repo page and drag
   in everything in this folder *except* `.env` (you'll never have a real
   `.env` file to upload anyway — Railway stores secrets separately, see
   below).

2. **In Railway**: New Project → Deploy from GitHub repo → pick the new
   repo. Railway will detect `requirements.txt` and the `Procfile` and run
   it as a worker (no web server, no port needed — this bot just polls
   Telegram, which is exactly what `worker:` in the Procfile tells Railway).

3. **Add a Volume** so booking data survives redeploys: in the Railway
   service → **Settings → Volumes** → add a volume, mount path `/data`.
   Then in **Variables**, set `DATA_DIR=/data`.

4. **Set environment variables**: in the same service → **Variables** tab,
   add each one from `.env.example`:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_BOT_USERNAME`
   - `ADMIN_TELEGRAM_ID`
   - `STRIPE_SECRET_KEY`
   - `CALENDAR_LINK`
   - `DATA_DIR` = `/data` (from step 3)

   Railway injects these directly into the running process — you don't
   need an actual `.env` file on Railway at all.

5. **Deploy**, then check the **Deployments → Logs** tab for
   `Bot starting...` with no errors. Message your bot on Telegram to
   confirm `/start` responds.

6. **Test a real payment** with Stripe's test card before switching
   `STRIPE_SECRET_KEY` to your live key (see testing notes above).

## Deploying on a VPS instead (for reference)

If you ever move this off Railway, same pattern as a typical
self-hosted bot — a small systemd unit keeps it running and restarts it
if it crashes. Example `/etc/systemd/system/coaching-booking-bot.service`:

```ini
[Unit]
Description=Coaching Booking Bot
After=network.target

[Service]
WorkingDirectory=/path/to/booking_bot
EnvironmentFile=/path/to/booking_bot/.env
ExecStart=/usr/bin/python3 /path/to/booking_bot/coaching_booking_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:
```
sudo systemctl daemon-reload
sudo systemctl enable --now coaching-booking-bot
```

## Customizing the copy

Everything you'd want to edit lives near the top of
`coaching_booking_bot.py`, clearly separated under
`# ── Editable content ──`:

- `PACKAGES` — names, prices (in cents), and descriptions
- `QUALIFYING_OPTIONS` / `VALIDATION_RESPONSES` — the opening question and
  the empathetic reply for each answer
- `BIO_TEXT` — your credibility section
- `TESTIMONIALS_TEXT` — the social proof step
- `SESSION_FORMAT_TEXT` — what a session includes
- `PRICING_TEXT` — the value stack + price reveal

None of these require touching the logic below them.

### Swapping in real voice-note testimonials

Right now `TESTIMONIALS_TEXT` uses written quotes. To send real voice
messages instead (like the reference funnel did):

1. Send the voice note to your bot once (or forward one you already have).
2. Temporarily log `update.message.voice.file_id` somewhere (e.g. print it
   in `on_text`) to capture the ID.
3. In the `step_proof` handler, replace the `reply_text(TESTIMONIALS_TEXT, ...)`
   call with `message.reply_voice(voice=that_file_id)` — you can send one or
   two in a row before the "Continue" button.

Happy to wire this in directly once you send the actual voice files.

## Admin commands

- `/bookings` — lists everyone who's paid and confirmed, with package and date.

## One unrelated thing worth fixing

Your existing `panic_bot.py` on GitHub has the bot token and admin ID
hardcoded directly in the source file, in a public repo. Anyone who finds
it can take control of that bot. Worth doing soon:

1. Message @BotFather → `/revoke` (or `/token`) on that bot to get a new
   token, invalidating the old one.
2. Move the token (and admin ID, for hygiene) into environment variables,
   the same way this new bot does it.
3. Remove the old hardcoded values from the file before your next commit.

Let me know if you'd like me to make that edit for you.
