import json
import logging
import re
import time as time_module
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
import storage

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

CHOOSING_SERVICE, CHOOSING_DATE, CHOOSING_TIME, ENTERING_NAME, ENTERING_PHONE, REVIEWING = range(6)
PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9\s().-]{6,19}$")
TIMEZONE = ZoneInfo(config.TIMEZONE)
OPENING_TIME = time.fromisoformat(config.OPENING_TIME)
CLOSING_TIME = time.fromisoformat(config.CLOSING_TIME)
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL_CACHE_TTL_SECONDS = 600
MODEL_REQUEST_BUDGET_SECONDS = 30
_free_model_cache: tuple[float, list[str]] = (0, [])
_model_cursor = 0


class NoFreeModelAvailable(RuntimeError):
    pass


def _is_free_model(model: dict) -> bool:
    if not model.get("id", "").endswith(":free"):
        return False
    pricing = model.get("pricing") or {}
    try:
        return all(
            Decimal(str(pricing[key])) == 0
            for key in ("prompt", "completion")
        )
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return False


async def _get_free_models(client: httpx.AsyncClient) -> list[str]:
    global _free_model_cache
    cached_at, cached_models = _free_model_cache
    if cached_models and time_module.monotonic() - cached_at < MODEL_CACHE_TTL_SECONDS:
        return cached_models

    response = await client.get(
        f"{OPENROUTER_BASE_URL}/models",
        headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
    )
    response.raise_for_status()
    data = response.json().get("data", [])
    models = [model["id"] for model in data if _is_free_model(model)]
    if not models:
        raise NoFreeModelAvailable("OpenRouter returned no free chat models.")
    _free_model_cache = (time_module.monotonic(), models)
    return models


async def _openrouter_chat(client: httpx.AsyncClient, payload: dict) -> str:
    global _model_cursor
    try:
        models = await _get_free_models(client)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
        _, cached_models = _free_model_cache
        if not cached_models:
            if config.OPENROUTER_MODEL.endswith(":free"):
                cached_models = [config.OPENROUTER_MODEL]
            else:
                raise NoFreeModelAvailable("Could not load OpenRouter's free model catalog.") from error
        logger.warning("Could not refresh OpenRouter's free model list; using cached choices.")
        models = cached_models

    start = _model_cursor % len(models)
    ordered_models = [models[(start + index) % len(models)] for index in range(len(models))]
    last_error = None
    attempted = 0
    deadline = time_module.monotonic() + MODEL_REQUEST_BUDGET_SECONDS

    for model_id in ordered_models:
        remaining = deadline - time_module.monotonic()
        if remaining <= 0:
            break
        attempted += 1
        try:
            response = await client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
                json={**payload, "model": model_id},
                timeout=min(10, remaining),
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"OpenRouter returned an empty response for {model_id}.")
            _model_cursor = (models.index(model_id) + 1) % len(models)
            logger.info("OpenRouter response received from free model %s", model_id)
            return content.strip()
        except httpx.HTTPStatusError as error:
            last_error = error
            status = error.response.status_code
            if status not in {400, 404, 408, 429, 500, 502, 503, 504}:
                raise
            logger.warning(
                "Free model %s returned HTTP %s; trying another free model.",
                model_id,
                status,
            )
        except httpx.TimeoutException as error:
            last_error = error
            logger.warning("Free model %s timed out; trying another free model.", model_id)

    _model_cursor = (start + attempted) % len(models)
    if last_error:
        raise NoFreeModelAvailable(
            f"All {attempted} attempted free models are currently unavailable."
        ) from last_error
    raise NoFreeModelAvailable("No free model could be selected.")


def available_slots(service: dict, day: date) -> list[datetime]:
    if day.weekday() == 6:
        return []

    now = datetime.now(TIMEZONE)
    earliest = now + timedelta(hours=config.MIN_NOTICE_HOURS)
    duration = timedelta(minutes=service["duration_minutes"])
    opening = datetime.combine(day, OPENING_TIME, tzinfo=TIMEZONE)
    closing = datetime.combine(day, CLOSING_TIME, tzinfo=TIMEZONE)
    occupied = storage.get_active_bookings(day)
    slots = []
    current = opening
    while current + duration <= closing:
        if current >= earliest and current.minute % config.SLOT_INTERVAL_MINUTES == 0:
            end = current + duration
            overlaps = any(
                current.replace(tzinfo=None)
                < datetime.fromisoformat(row["appointment_at"])
                + timedelta(minutes=row["duration_minutes"])
                and end.replace(tzinfo=None) > datetime.fromisoformat(row["appointment_at"])
                for row in occupied
            )
            if not overlaps:
                slots.append(current)
        current += timedelta(minutes=config.SLOT_INTERVAL_MINUTES)
    return slots


def available_dates(service: dict) -> list[date]:
    today = datetime.now(TIMEZONE).date()
    return [
        day
        for offset in range(config.BOOKING_HORIZON_DAYS + 1)
        if (day := today + timedelta(days=offset)).weekday() != 6
        and available_slots(service, day)
    ]


def is_valid_phone(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone)
    return bool(PHONE_PATTERN.fullmatch(phone) and 7 <= len(digits) <= 15)


def date_keyboard(service: dict) -> InlineKeyboardMarkup | None:
    days = available_dates(service)
    if not days:
        return None
    buttons = [
        InlineKeyboardButton(
            day.strftime("%a, %b %d"),
            callback_data=f"date_{day.isoformat()}",
        )
        for day in days
    ]
    return InlineKeyboardMarkup([buttons[index : index + 2] for index in range(0, len(buttons), 2)])


def time_keyboard(day: date, slots: list[datetime]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            slot.strftime("%H:%M"),
            callback_data=f"slot_{day.isoformat()}_{slot:%H%M}",
        )
        for slot in slots
    ]
    rows = [buttons[index : index + 3] for index in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton("← Choose another date", callback_data="back_dates")])
    return InlineKeyboardMarkup(rows)


async def show_dates(query, service: dict, message: str) -> int:
    keyboard = date_keyboard(service)
    if keyboard is None:
        await query.edit_message_text(
            "There are no available appointments in the next 14 days. Please try again later."
        )
        return CHOOSING_DATE
    await query.edit_message_text(message, reply_markup=keyboard)
    return CHOOSING_DATE


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📅 Book an appointment", callback_data="book")],
        [InlineKeyboardButton("❓ Ask a question", callback_data="ask")],
    ]
    await update.message.reply_text(
        f"Welcome to {config.BUSINESS_NAME}!\n"
        f"Working hours: {config.WORKING_HOURS}\n"
        "Appointments are in Kyiv time. Choose an option:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "ask":
        await query.edit_message_text(
            "Type your question in the chat and I'll get back to you in a few seconds 🙂"
        )


async def start_booking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    keyboard = [
        [
            InlineKeyboardButton(
                f"{service['name']} — {service['price']}",
                callback_data=f"service_{index}",
            )
        ]
        for index, service in enumerate(config.SERVICES)
    ]
    await query.edit_message_text(
        "Please choose a service:", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CHOOSING_SERVICE


async def choose_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    index = int(query.data.removeprefix("service_"))
    if not 0 <= index < len(config.SERVICES):
        await query.edit_message_text("That service is no longer available. Send /start to try again.")
        return ConversationHandler.END
    service = config.SERVICES[index]
    context.user_data["service"] = service
    return await show_dates(
        query,
        service,
        "Choose an available date. You can also type a date and time, for example: "
        '"tomorrow evening after 6".',
    )


async def choose_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    day = date.fromisoformat(query.data.removeprefix("date_"))
    service = context.user_data["service"]
    slots = available_slots(service, day)
    if not slots:
        return await show_dates(query, service, "That date no longer has available times. Choose another date:")

    context.user_data["selected_date"] = day.isoformat()
    await query.edit_message_text(
        f"Available times on {day.strftime('%A, %B %d')} (Kyiv time):",
        reply_markup=time_keyboard(day, slots),
    )
    return CHOOSING_TIME


def appointment_error(appointment: datetime, service: dict) -> str | None:
    now = datetime.now(TIMEZONE)
    day = appointment.date()
    if appointment.tzinfo is None:
        appointment = appointment.replace(tzinfo=TIMEZONE)
    if day < now.date() or day > now.date() + timedelta(days=config.BOOKING_HORIZON_DAYS):
        return "Please choose a date within the next 14 days."
    if day.weekday() == 6:
        return "We are closed on Sundays. Please choose another day."
    if appointment < now + timedelta(hours=config.MIN_NOTICE_HOURS):
        return "Appointments must be booked at least 2 hours in advance."
    if appointment.minute % config.SLOT_INTERVAL_MINUTES or appointment.second or appointment.microsecond:
        return "Please choose a time on the hour or half-hour."
    if appointment.time() < OPENING_TIME or (
        appointment + timedelta(minutes=service["duration_minutes"])
    ).time() > CLOSING_TIME:
        return "That time is outside working hours or the service would finish after closing."
    if appointment not in available_slots(service, day):
        return "That time has just become unavailable. Please choose another slot."
    return None


async def choose_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    _, day_text, time_text = query.data.split("_", 2)
    appointment = datetime.combine(
        date.fromisoformat(day_text),
        time(int(time_text[:2]), int(time_text[2:])),
        tzinfo=TIMEZONE,
    )
    error = appointment_error(appointment, context.user_data["service"])
    if error:
        await query.answer(error, show_alert=True)
        current_slots = available_slots(context.user_data["service"], appointment.date())
        if current_slots:
            await query.edit_message_text(
                f"That time is unavailable. Choose an available time on "
                f"{appointment.strftime('%A, %B %d')} (Kyiv time):",
                reply_markup=time_keyboard(appointment.date(), current_slots),
            )
        else:
            await show_dates(
                query,
                context.user_data["service"],
                "That date no longer has available times. Choose another date:",
            )
            return CHOOSING_DATE
        return CHOOSING_TIME

    await query.answer()
    context.user_data["appointment"] = appointment.isoformat()
    await query.edit_message_text(
        f"Selected: {appointment.strftime('%A, %B %d at %H:%M')} Kyiv time.\n\nWhat is your name?"
    )
    return ENTERING_NAME


async def parse_natural_datetime(text: str, selected_date: str | None) -> tuple[datetime | None, str | None]:
    if not config.OPENROUTER_API_KEY:
        return None, "Natural-language date parsing needs OPENROUTER_API_KEY. Please use the date and time buttons."

    now = datetime.now(TIMEZONE)
    date_hint = f"The selected date is {selected_date}; use it if the message gives only a time." if selected_date else ""
    payload = {
        "model": config.OPENROUTER_MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Extract an appointment date and start time from the user's message. "
                    f"Current local date/time is {now.isoformat()} in {config.TIMEZONE}. {date_hint} "
                    "Return only a JSON object with keys date (YYYY-MM-DD) and time (HH:MM, 24-hour). "
                    "Use null for either value if it is ambiguous or missing. Do not select or suggest "
                    "a slot; only interpret what the user asked for."
                ),
            },
            {"role": "user", "content": text},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            content = await _openrouter_chat(client, payload)
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                return None, "I couldn't understand that date and time. Please choose from the buttons."
            if not parsed.get("date") or not parsed.get("time"):
                return None, "I couldn't identify both a date and time. Please specify both or choose from the buttons."
            appointment = datetime.strptime(
                f"{parsed['date']} {parsed['time']}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=TIMEZONE)
            return appointment, None
    except NoFreeModelAvailable:
        logger.warning("All attempted free models are busy or unavailable.")
        return None, "All free AI models are currently busy. Please try again shortly or choose a time using the buttons."
    except (
        httpx.HTTPError,
        AttributeError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        logger.exception("Could not parse the appointment date/time with OpenRouter")
        return None, "I couldn't understand that date and time. Please try again or choose from the buttons."


async def enter_natural_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    selected_date = context.user_data.get("selected_date")
    appointment, parse_error = await parse_natural_datetime(update.message.text, selected_date)
    if parse_error:
        await update.message.reply_text(parse_error)
        return CHOOSING_TIME if selected_date else CHOOSING_DATE

    error = appointment_error(appointment, context.user_data["service"])
    if error:
        await update.message.reply_text(f"{error} Please choose an available time from the buttons.")
        return CHOOSING_TIME if selected_date else CHOOSING_DATE

    context.user_data["appointment"] = appointment.isoformat()
    context.user_data["selected_date"] = appointment.date().isoformat()
    await update.message.reply_text(
        f"I understood: {appointment.strftime('%A, %B %d at %H:%M')} Kyiv time.\n\nWhat is your name?"
    )
    return ENTERING_NAME


async def enter_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Please enter your name.")
        return ENTERING_NAME
    context.user_data["customer_name"] = name
    await update.message.reply_text("Please provide your phone number to confirm the appointment:")
    return ENTERING_PHONE


async def enter_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    if not is_valid_phone(phone):
        await update.message.reply_text(
            "That phone number doesn't look valid. Enter 7–15 digits, optionally starting with +."
        )
        return ENTERING_PHONE

    context.user_data["phone"] = phone
    service = context.user_data["service"]
    appointment = datetime.fromisoformat(context.user_data["appointment"])
    await update.message.reply_text(
        "Please review your appointment request:\n\n"
        f"Service: {service['name']} ({service['price']})\n"
        f"Date and time: {appointment.strftime('%A, %B %d, %Y at %H:%M')} Kyiv time\n"
        f"Name: {context.user_data['customer_name']}\n"
        f"Phone: {phone}\n\n"
        "Submit this request?",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Confirm", callback_data="confirm_booking"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_booking"),
            ]]
        ),
    )
    return REVIEWING


async def confirm_booking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data
    service = data["service"]
    appointment = datetime.fromisoformat(data["appointment"])
    error = appointment_error(appointment, service)
    if error:
        await query.edit_message_text(f"{error} Please start a new booking with /start.")
        context.user_data.clear()
        return ConversationHandler.END

    booking_id = storage.create_booking(
        user_id=update.effective_user.id,
        username=update.effective_user.username or str(update.effective_user.id),
        service_name=service["name"],
        price=service["price"],
        duration_minutes=service["duration_minutes"],
        appointment_at=appointment,
        customer_name=data["customer_name"],
        phone=data["phone"],
    )
    if booking_id is None:
        await query.edit_message_text(
            "Sorry, that slot has just been booked. Send /start to choose another time."
        )
        context.user_data.clear()
        return ConversationHandler.END

    summary = (
        f"📋 Appointment request #{booking_id}\n"
        f"Service: {service['name']} ({service['price']})\n"
        f"Date and time: {appointment.strftime('%A, %B %d, %Y at %H:%M')} Kyiv time\n"
        f"Duration: {service['duration']}\n"
        f"Name: {data['customer_name']}\n"
        f"Phone: {data['phone']}\n"
        f"Telegram: @{update.effective_user.username or update.effective_user.id}\n"
        "Status: Pending review"
    )
    admin_keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Accept", callback_data=f"admin_accept_{booking_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_{booking_id}"),
        ]]
    )
    try:
        await context.bot.send_message(
            chat_id=config.ADMIN_CHAT_ID,
            text=summary,
            reply_markup=admin_keyboard,
        )
    except Exception:
        logger.exception("Booking %s was saved, but admin notification failed", booking_id)
        storage.set_booking_status(booking_id, "cancelled")
        await query.edit_message_text(
            "We couldn't send your request to the administrator. No appointment was made; "
            "please try again later or contact the business directly."
        )
    else:
        await query.edit_message_text(
            "Thank you! Your appointment request has been sent to the administrator "
            "and is awaiting approval. ✅"
        )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("Appointment request cancelled. Send /start to begin again.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Booking cancelled. Send /start to begin again.")
    return ConversationHandler.END


async def back_to_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await show_dates(query, context.user_data["service"], "Choose an available date:")


async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if str(query.message.chat_id) != str(config.ADMIN_CHAT_ID):
        await query.answer("This action is only available to the administrator.", show_alert=True)
        return
    if query.message.chat.type != "private":
        try:
            member = await context.bot.get_chat_member(
                chat_id=query.message.chat_id,
                user_id=query.from_user.id,
            )
        except Exception:
            logger.exception("Could not verify admin permissions in chat %s", query.message.chat_id)
            await query.answer("Could not verify your admin permissions.", show_alert=True)
            return
        if member.status not in {"administrator", "creator"}:
            await query.answer("Only chat administrators can process requests.", show_alert=True)
            return

    _, action, booking_id_text = query.data.split("_")
    booking_id = int(booking_id_text)
    status = "accepted" if action == "accept" else "rejected"
    if not storage.set_booking_status(booking_id, status):
        await query.answer("This request has already been processed.", show_alert=True)
        return

    booking = storage.get_booking(booking_id)
    await query.answer(f"Appointment {status}.")
    try:
        await query.edit_message_text(query.message.text + f"\n\nStatus: {status.title()}")
    except Exception:
        logger.exception("Could not update admin message for booking %s", booking_id)
    customer_message = (
        f"Your appointment request for {booking['service_name']} on "
        f"{datetime.fromisoformat(booking['appointment_at']).strftime('%A, %B %d at %H:%M')} "
        f"Kyiv time has been {status}."
        if status == "accepted"
        else "Unfortunately, your appointment request could not be accepted. "
        "Please send /start to choose another time."
    )
    try:
        await context.bot.send_message(chat_id=booking["user_id"], text=customer_message)
    except Exception:
        logger.exception("Could not notify customer for booking %s", booking_id)


async def ask_ai(question: str) -> str:
    if not config.OPENROUTER_API_KEY:
        return "AI answers are not configured yet (OPENROUTER_API_KEY is missing)."

    payload = {
        "model": config.OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": config.FAQ_CONTEXT},
            {"role": "user", "content": question},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            return await _openrouter_chat(client, payload)
    except NoFreeModelAvailable:
        logger.warning("All attempted free models are busy or unavailable.")
        return "All free AI models are currently busy. Please try again shortly."
    except httpx.HTTPStatusError as error:
        logger.error(
            "OpenRouter returned HTTP %s: %s",
            error.response.status_code,
            error.response.text[:1000],
        )
        return "Sorry, I couldn't get an answer. Please try again later or contact the administrator."
    except (httpx.HTTPError, AttributeError, KeyError, IndexError, TypeError):
        logger.exception("OpenRouter request failed")
        return "Sorry, I couldn't get an answer. Please try again later or contact the administrator."


async def handle_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.chat.send_action(action="typing")
    await update.message.reply_text(await ask_ai(update.message.text))


def main() -> None:
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in the project .env file.")
    if not config.ADMIN_CHAT_ID:
        raise RuntimeError("ADMIN_CHAT_ID is required. Set it in the project .env file.")
    try:
        int(config.ADMIN_CHAT_ID)
    except ValueError as error:
        raise RuntimeError("ADMIN_CHAT_ID must be a numeric Telegram chat ID.") from error

    storage.initialize()
    application = Application.builder().token(config.BOT_TOKEN).build()
    booking_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_booking, pattern="^book$")],
        states={
            CHOOSING_SERVICE: [CallbackQueryHandler(choose_service, pattern=r"^service_\d+$")],
            CHOOSING_DATE: [
                CallbackQueryHandler(choose_date, pattern=r"^date_\d{4}-\d{2}-\d{2}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_natural_datetime),
            ],
            CHOOSING_TIME: [
                CallbackQueryHandler(choose_date, pattern=r"^date_\d{4}-\d{2}-\d{2}$"),
                CallbackQueryHandler(choose_time, pattern=r"^slot_\d{4}-\d{2}-\d{2}_\d{4}$"),
                CallbackQueryHandler(back_to_dates, pattern="^back_dates$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_natural_datetime),
            ],
            ENTERING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_name)],
            ENTERING_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_phone)],
            REVIEWING: [
                CallbackQueryHandler(confirm_booking, pattern="^confirm_booking$"),
                CallbackQueryHandler(cancel_booking, pattern="^cancel_booking$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(booking_conversation)
    application.add_handler(CallbackQueryHandler(menu_router, pattern="^ask$"))
    application.add_handler(CallbackQueryHandler(admin_decision, pattern=r"^admin_(accept|reject)_\d+$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_text))
    logger.info("Bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
