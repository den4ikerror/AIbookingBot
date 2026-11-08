import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# --- Technical settings (loaded from .env) ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL", "google/gemma-4-31b-it:free"
).strip()
if OPENROUTER_MODEL.startswith("OPENROUTER_MODEL="):
    OPENROUTER_MODEL = OPENROUTER_MODEL.removeprefix("OPENROUTER_MODEL=").strip()
DATABASE_PATH = BASE_DIR / "bookings.sqlite3"
TIMEZONE = "Europe/Kyiv"
OPENING_TIME = "10:00"
CLOSING_TIME = "20:00"
SLOT_INTERVAL_MINUTES = 30
BOOKING_HORIZON_DAYS = 14
MIN_NOTICE_HOURS = 2

# --- Business information (customize these for each business) ---
BUSINESS_NAME = "Vibe Barbershop"
WORKING_HOURS = "Mon–Sat, 10:00 AM–8:00 PM (closed Sunday)"
ADDRESS = "10 Example Street, Lviv"

# List of services: name, price, duration
SERVICES = [
    {"name": "Men's haircut", "price": "300 UAH", "duration": "40 min", "duration_minutes": 40},
    {"name": "Children's haircut", "price": "200 UAH", "duration": "30 min", "duration_minutes": 30},
    {"name": "Haircut + beard trim", "price": "450 UAH", "duration": "60 min", "duration_minutes": 60},
    {"name": "Beard shave", "price": "150 UAH", "duration": "20 min", "duration_minutes": 20},
]

# Additional context for AI answers to customer questions
FAQ_CONTEXT = f"""
You are the virtual assistant for "{BUSINESS_NAME}".
Working hours: {WORKING_HOURS}.
Address: {ADDRESS}.
Services and prices:
{chr(10).join(f"- {s['name']}: {s['price']} ({s['duration']})" for s in SERVICES)}

Reply in English, briefly (2-4 sentences), and in a friendly manner.
If a question is unrelated to the business or you do not know the exact answer,
say honestly that the customer should ask the administrator directly.
Do not make up promotions, discounts, or services that are not listed above.
"""
