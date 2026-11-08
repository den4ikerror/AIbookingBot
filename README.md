# AI Booking Bot

A Telegram bot for small businesses with appointment booking and AI-powered answers to customer questions.

## Features

- Inline service, date, and available-time selection.
- Kyiv-time scheduling: 10:00–20:00 Monday through Saturday, closed Sunday.
- 30-minute start-time intervals, bookings up to 14 days ahead, and at least 2 hours' notice.
- Overlapping pending or accepted appointments are not offered; appointment duration determines how much time is blocked.
- Phone-number validation and a customer review/confirmation step.
- SQLite persistence in `bookings.sqlite3`.
- Admin Accept/Reject buttons; customers are notified of the decision.
- OpenRouter automatically loads its catalog of free models and tries another free model if one is unavailable or rate-limited. Natural-language requests such as "tomorrow evening after 6" are checked against the same booking rules as button selections.

## Setup

1. Install the dependencies:
   ```powershell
   python -m pip install -r requirements.txt
   ```
2. Create a Telegram bot by messaging [@BotFather](https://t.me/BotFather) and using `/newbot`.
3. Find the Telegram chat ID where admin notifications should be sent. Add the bot to that chat first if using a group, and make it an administrator so it can verify who accepts or rejects requests.
4. Get an API key from [OpenRouter](https://openrouter.ai) for AI answers and natural-language date/time parsing.
5. Create a `.env` file in the project directory:
   ```dotenv
   BOT_TOKEN=your_telegram_bot_token
   ADMIN_CHAT_ID=your_numeric_telegram_chat_id
   OPENROUTER_API_KEY=your_openrouter_api_key
   ```
   `BOT_TOKEN` and `ADMIN_CHAT_ID` are required. Without `OPENROUTER_API_KEY`, button-based booking still works, but AI answers and natural-language date/time parsing are unavailable.
   The bot discovers free models automatically; `OPENROUTER_MODEL` is optional and is only used as a fallback if the model catalog cannot be loaded.
6. Start the bot:
   ```powershell
   python main.py
   ```

## Customize the business

Edit `config.py` to change the business details, hours, booking rules, and services. Service durations are stored in minutes and are used to prevent overlapping appointments.

## Project files

- `main.py` — Telegram handlers, booking flow, and OpenRouter integration.
- `config.py` — business settings and environment-based configuration.
- `storage.py` — SQLite booking persistence and overlap protection.
- `requirements.txt` — Python dependencies.
