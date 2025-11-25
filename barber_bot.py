# barber_bot.py
import logging
import os
import re
from datetime import datetime, timedelta, time as dtime
from urllib.parse import quote_plus, unquote_plus
ADMIN_CHAT_ID = 6535793206  # 🔥 Replace with your actual Telegram user ID

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -----------------------
# Configuration & logging
# -----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    print("❌ BOT_TOKEN not found in .env, using fallback...")
    TOKEN = "8214171683:AAE-ZgPUtZE8xGRBFM0s_LeaWtzLN7eu74E"  # fallback

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "6535793206"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# -----------------------
# Global storage (simple)
# -----------------------
# reservations: user_id -> reservation dict (persist only in memory)
reservations = {}
booking_history = {}  # user_id -> list of all bookings (active, cancelled, completed)

# -----------------------
# Slot generation helpers
# -----------------------
SLOT_INTERVAL_MINUTES = 40
OPEN_TIME_STR = "08:00 AM"
CLOSE_TIME_STR = "08:00 PM"
RECOMMEND_LIMIT = 5  # how many suggestions to return


def generate_slots_for_date(date_str_iso):
    """
    date_str_iso: 'YYYY-MM-DD'
    returns list of slot strings like '08:00 AM', '08:40 AM', ...
    """
    try:
        day = datetime.strptime(date_str_iso, "%Y-%m-%d").date()
    except Exception:
        day = datetime.now().date()

    start_dt = datetime.combine(day, datetime.strptime(OPEN_TIME_STR, "%I:%M %p").time())
    end_dt = datetime.combine(day, datetime.strptime(CLOSE_TIME_STR, "%I:%M %p").time())

    slots = []
    cur = start_dt
    while cur <= end_dt:
        slots.append(cur.strftime("%I:%M %p"))
        cur += timedelta(minutes=SLOT_INTERVAL_MINUTES)
    return slots


def slot_is_free(check_date_iso, candidate_slot_str, people, reservations_dict):
    """
    Return True if candidate_slot on check_date_iso is free for 'people' people.
    A booking for N people occupies N consecutive slots starting at booked slot.
    """
    slots = generate_slots_for_date(check_date_iso)
    try:
        candidate_index = slots.index(candidate_slot_str)
    except ValueError:
        return False

    needed_end = candidate_index + int(people)
    if needed_end > len(slots):
        return False

    for other in reservations_dict.values():
        if other.get("date") != check_date_iso:
            continue
        other_time = other.get("time")
        other_people = int(other.get("people", 1))
        try:
            other_index = slots.index(other_time)
        except Exception:
            # if stored format differs, try normalization
            try:
                other_index = slots.index(datetime.strptime(other_time, "%I:%M %p").strftime("%I:%M %p"))
            except Exception:
                # unknown stored time — be conservative
                return False
        other_end = other_index + other_people
        # overlap?
        if not (needed_end <= other_index or candidate_index >= other_end):
            return False

    return True

def recommend_slots(date_str_iso, time_str, people, reservations_dict):
    """
    Return list of (slot_str, date_iso) suggestions.
    Behavior:
      - Try to find free slots on the requested date first (even if it's tomorrow).
      - If none found on that date, try next day.
      - Returns list of tuples: (slot_str, date_iso).
    """

    # Parse date safely
    try:
        selected_date = datetime.strptime(date_str_iso, "%Y-%m-%d").date()
    except Exception:
        selected_date = datetime.now().date()

    # Parse requested time robustly
    try:
        requested_time = datetime.strptime(time_str, "%I:%M %p").time()
    except Exception:
        requested_time = datetime.strptime(OPEN_TIME_STR, "%I:%M %p").time()

    # If requested_time at/after close -> move to next day
    if requested_time >= dtime(20, 0):
        selected_date = selected_date + timedelta(days=1)

    results = []
    current_date_str = selected_date.strftime("%Y-%m-%d")

    # 1) Try same requested date first
    for s in generate_slots_for_date(current_date_str):
        if slot_is_free(current_date_str, s, people, reservations_dict):
            results.append((s, current_date_str))
            if len(results) >= RECOMMEND_LIMIT:
                break

    # 2) If none, try next day
    if not results:
        next_day = selected_date + timedelta(days=1)
        next_day_str = next_day.strftime("%Y-%m-%d")
        for s in generate_slots_for_date(next_day_str):
            if slot_is_free(next_day_str, s, people, reservations_dict):
                results.append((s, next_day_str))
                if len(results) >= RECOMMEND_LIMIT:
                    break

    return results


# -----------------------
# UI helper: encode/decode slot for callback_data
# -----------------------
def enc(s: str) -> str:
    return quote_plus(s)


def dec(s: str) -> str:
    return unquote_plus(s)

def can_cancel_reservation(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Returns True if the user has an active reservation and more than 2 hours remain.
    """
    res = context.user_data.get("active_reservation")
    if not res:
        return False

    try:
        dt = datetime.strptime(f"{res['date']} {res['time']}", "%Y-%m-%d %I:%M %p")
        now = datetime.now()
        diff = dt - now
        return diff > timedelta(hours=2)
    except Exception:
        return False

# -----------------------
# Keyboards
# -----------------------
def home_keyboard(context=None):
    keyboard = [
        [InlineKeyboardButton("💈 Book Appointment", callback_data="book")],
        [InlineKeyboardButton("📖 My Bookings", callback_data="my_bookings")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ]

    # Show cancel only if an active reservation exists
    if context and context.user_data.get("active_reservation"):
        keyboard.append([InlineKeyboardButton("❌ Cancel Reservation", callback_data="cancel_booking")])

    return InlineKeyboardMarkup(keyboard)

def dates_keyboard():
    today = datetime.today()
    buttons = []
    for i in range(7):
        d = today + timedelta(days=i)
        if i == 0:
            label = f"Today {d.strftime('%a %b %d')}"
        elif i == 1:
            label = f"Tomorrow {d.strftime('%a %b %d')}"
        else:
            label = d.strftime("%a %b %d")
        buttons.append([InlineKeyboardButton(label, callback_data=f"date_{d.strftime('%Y-%m-%d')}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    return InlineKeyboardMarkup(buttons)



def times_keyboard(date_str_iso):
    slots = generate_slots_for_date(date_str_iso)
    rows = []
    row = []
    for i, s in enumerate(slots, start=1):
        if slot_is_free(date_str_iso, s, 1, reservations):
            # free slot -> clickable
            row.append(InlineKeyboardButton(s, callback_data=f"time_{enc(s)}"))
        else:
            # taken -> not clickable, show reserved label
            label = f"{s} ❌ (the spot has been reserved)"
            row.append(InlineKeyboardButton(label, callback_data=f"taken_{enc(s)}"))
        if i % 3 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_date")])
    return InlineKeyboardMarkup(rows)


def people_keyboard(count: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➖", callback_data="people_minus"),
            InlineKeyboardButton(f"{count} {'person' if count == 1 else 'people'}", callback_data="none"),
            InlineKeyboardButton("➕", callback_data="people_plus"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_time"),
         InlineKeyboardButton("✅ Continue", callback_data="confirm_people")],
    ])


def confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="back_to_people"),
         InlineKeyboardButton("✅ Confirm", callback_data="final_confirm")]
    ])

def add_cancel_button(markup: InlineKeyboardMarkup, context: ContextTypes.DEFAULT_TYPE):
    """
    Appends a cancel reservation button to any existing markup if eligible.
    """
    if can_cancel_reservation(context):
        new_inline = markup.inline_keyboard.copy()
        new_inline.append([InlineKeyboardButton("❌ Cancel Reservation", callback_data="cancel_booking")])
        return InlineKeyboardMarkup(new_inline)
    return markup

# -----------------------
# Handlers
# -----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to the MbBarber Shop Booking Bot!\nChoose an option:",
        reply_markup=home_keyboard(context)
    )


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    logging.info("handle_buttons: callback_data=%s user=%s user_data=%s", data, user_id, context.user_data)

    # HOME -> start booking
    if data == "book":
        await query.edit_message_text("📅 Choose a date:", reply_markup=dates_keyboard())
        return

    # My bookings / history
    if data == "my_bookings":
        now = datetime.now()
        bot_history = context.application.bot_data.get("booking_history", {})
        user_history = bot_history.get(user_id, []).copy()

        # include active reservation if present
        active = reservations.get(user_id)
        if active:
            exists = any((r.get("date") == active.get("date") and r.get("time") == active.get("time")) for r in user_history)
            if not exists:
                user_history.append({
                    "name": active.get("name"),
                    "phone": active.get("phone"),
                    "date": active.get("date"),
                    "time": active.get("time"),
                    "people": active.get("people"),
                    "status": "Active"
                })

        # mark past actives as completed
        for b in user_history:
            try:
                booking_dt = datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %I:%M %p")
                if b.get("status") == "Active" and booking_dt < now:
                    b["status"] = "Completed"
            except Exception:
                pass

        if not user_history:
            await query.edit_message_text("📖 You have no booking history.", reply_markup=home_keyboard(context))
            return

        # sort by status then recent
        def sort_key(b):
            order = {"Active": 0, "Completed": 1, "Cancelled": 2}
            rank = order.get(b.get("status"), 3)
            try:
                dt = datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %I:%M %p")
            except Exception:
                dt = datetime.min
            return (rank, -dt.timestamp())

        sorted_history = sorted(user_history, key=sort_key)

        lines = []
        for b in sorted_history:
            try:
                date_obj = datetime.strptime(b["date"], "%Y-%m-%d")
                formatted_date = date_obj.strftime("%d/%m/%Y")
            except Exception:
                formatted_date = b["date"]

            status = b.get("status", "Unknown")
            icon = {"Active": "🟢", "Cancelled": "🔴", "Completed": "⚪"}.get(status, "⚫")
            people_text = f"{b.get('people', 1)} {'person' if b.get('people',1) == 1 else 'people'}"

            try:
                if datetime.strptime(b["date"], "%Y-%m-%d").date() == datetime.now().date():
                    formatted_date = "Today"
            except Exception:
                pass

            lines.append(f"{icon} {formatted_date} – {b.get('time')} — {people_text} — {status}")

        text = "📖 *Your Booking History:*\n\n" + "\n".join(lines)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="home")]]))
        return

    # Help
    if data == "help":
        help_text = (
        "💈 *MbBarber Shop Information*\n\n"
        "ℹ️ *To book an appointment:*\n"
        "Choose *Date → Time → How many people → Client name → Client phone number → Review → Confirm*\n\n"
        "🏠 *Address:*\n"
        "Bulbula 93, Zemen Bank — Yalebet building, Ground Floor\n\n"
        "📞 *Phone:*\n"
        "+251920224604\n"
        "+251709576073\n\n"
        "🎵 *TikTok:*\n"
        "@MbBarberShop\n"
        "https://www.tiktok.com/@mbbarbershop02?&t=ZM-91hMkvbC0Wv\n\n"
        "📢 *Telegram:*\n"
        "@MbBarberShop\n"
        "https://t.me/mbbarbershop02\n\n"
        "💬 If you have any issues or special requests, feel free to message us directly!"
    )

    await query.edit_message_text(
        help_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Home", callback_data="home")]
        ])
    )
    return


    # Home
    if data == "home":
        await query.edit_message_text("🏠 Back to home:", reply_markup=home_keyboard(context))
        return

    # DATE selection
    if data.startswith("date_"):
        try:
            date_str = data.split("_", 1)[1]
        except Exception:
            await query.edit_message_text("⚠️ Invalid date selected. Try again.", reply_markup=dates_keyboard())
            return
        context.user_data["date"] = date_str
        await query.edit_message_text(f"⏰ Choose a time for {date_str}:", reply_markup=times_keyboard(date_str))
        return

    # TIME selection
    if data.startswith("time_"):
        try:
            slot = dec(data.split("_", 1)[1])
        except Exception:
            await query.edit_message_text("⚠️ Invalid time selected. Go back and try again.", reply_markup=dates_keyboard())
            return
        context.user_data["time"] = slot
        context.user_data["people"] = 1
        await query.edit_message_text(f"👥 How many people? (adjust then Continue)", reply_markup=people_keyboard(1))
        return

    # If user tapped a taken slot
    if data.startswith("taken_"):
        try:
            slot = dec(data.split("_", 1)[1])
        except Exception:
            await query.edit_message_text(
                "⚠️ Invalid selection. Please try again.",
                reply_markup=dates_keyboard()
            )
            return

        date_str = context.user_data.get("date")
        people = context.user_data.get("people", 1)
        recs = recommend_slots(date_str, slot, people, reservations)

        if recs:
            suggested_day = recs[0][1]
            heading = "Other available times " + ("tomorrow:" if suggested_day != date_str else "today:")
            lines = [f"❌ The spot at {slot} is reserved. {heading}"]
            kb = []

            for s, d in recs:
                try:
                    rec_date = datetime.strptime(d, "%Y-%m-%d").date()
                    cur_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    if rec_date == cur_date + timedelta(days=1):
                        label = "Tomorrow"
                    elif rec_date == cur_date:
                        label = "Today"
                    else:
                        label = rec_date.strftime("%a %b %d")
                except Exception:
                    label = "Today"

                lines.append(f"• {s} ({label})")
                kb.append([InlineKeyboardButton(f"{s} ({label})", callback_data=f"suggest_{d}_{enc(s)}")])

            kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_date")])
            await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
        else:
            await query.edit_message_text(
                "❌ The spot is reserved and no nearby options found.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_date")]])
            )
        return

    # Suggested slot chosen
    if data.startswith("suggest_"):
        try:
            _, date_iso, slot_enc = data.split("_", 2)
            slot = dec(slot_enc)
        except Exception:
            await query.edit_message_text("⚠️ Invalid suggestion selected.", reply_markup=dates_keyboard())
            return
        context.user_data["date"] = date_iso
        context.user_data["time"] = slot
        context.user_data["people"] = 1
        await query.edit_message_text(f"⏰ You picked {slot} on {date_iso}\n👥 How many people?", reply_markup=people_keyboard(1))
        return

    # People +/- and navigation
    if data == "people_minus":
        if context.user_data.get("people", 1) > 1:
            context.user_data["people"] -= 1
        await query.edit_message_reply_markup(reply_markup=people_keyboard(context.user_data.get("people", 1)))
        return

    if data == "people_plus":
        context.user_data["people"] = context.user_data.get("people", 1) + 1
        await query.edit_message_reply_markup(reply_markup=people_keyboard(context.user_data.get("people", 1)))
        return

    if data == "back_to_time":
        date_str = context.user_data.get("date")
        await query.edit_message_text(f"⏰ Choose a time for {date_str}:", reply_markup=times_keyboard(date_str))
        return

    if data == "confirm_people":
        date = context.user_data.get("date")
        time_str = context.user_data.get("time")
        people = context.user_data.get("people", 1)
        if not date or not time_str:
            await query.edit_message_text("⚠️ Missing date/time. Restart with /start", reply_markup=home_keyboard(context))
            return

        if slot_is_free(date, time_str, people, reservations):
            context.user_data["awaiting_name"] = True
            await query.edit_message_text("📝 Please enter your *Name*:", parse_mode="Markdown")
            return
        else:
            recs = recommend_slots(date, time_str, people, reservations)
            if recs:
                suggested_day = recs[0][1]
                heading = "Other available times " + ("tomorrow:" if suggested_day != date else "today:")
                lines = [f"❌ Not enough slots for {people} at {time_str} on {date}. {heading}"]
                kb = []
                for s, d in recs[:RECOMMEND_LIMIT]:
                    label = "Tomorrow" if d != date else "Today"
                    lines.append(f"• {s} ({label})")
                    kb.append([InlineKeyboardButton(f"{s} ({label})", callback_data=f"suggest_{d}_{enc(s)}")])
                kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_people")])
                await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
                return
            else:
                await query.edit_message_text("❌ Not available and no suggestions found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_people")]]))
                return

    if data == "back_to_date":
        await query.edit_message_text("📅 Choose a date:", reply_markup=add_cancel_button(dates_keyboard(), context))
        return

    if data == "back_to_people":
        await query.edit_message_text("👥 How many people?", reply_markup=people_keyboard(context.user_data.get("people", 1)))
        return

    # Final confirmation (save booking)
    if data == "final_confirm":
        user_id = query.from_user.id
        name = context.user_data.get("name")
        phone = context.user_data.get("phone")
        date = context.user_data.get("date")
        time_str = context.user_data.get("time")
        people = context.user_data.get("people", 1)

        context.user_data["active_reservation"] = {"date": date, "time": time_str, "people": people}

        # validation
        if not (name and phone and date and time_str):
            await query.edit_message_text("⚠️ Missing booking details. Please start again with /start.", reply_markup=home_keyboard(context))
            return

        # Save booking
        reservations[user_id] = {"name": name, "phone": phone, "date": date, "time": time_str, "people": people}

        # Record in history (use application.bot_data store)
        if "booking_history" not in context.application.bot_data:
            context.application.bot_data["booking_history"] = {}
        history = context.application.bot_data["booking_history"].setdefault(user_id, [])
        history.append({"name": name, "phone": phone, "date": date, "time": time_str, "people": people, "status": "Active"})

        # send confirmation to user
        await query.edit_message_text(
            f"✅ *Reservation Confirmed!*\n\n"
            f"🧍 Name: {name}\n"
            f"📞 Phone: {phone}\n"
            f"📅 Date: {date}\n"
            f"⏰ Time: {time_str}\n"
            f"👥 {people} {'person' if people == 1 else 'people'}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel Reservation", callback_data="cancel_booking")],
                [InlineKeyboardButton("🏠 Home", callback_data="home")]
            ])
        )

        # notify admin (use the global ADMIN_CHAT_ID)
        try:
            if ADMIN_CHAT_ID:
                admin_text = (
                    "📣 *New Reservation*\n\n"
                    f"🧍 *Name:* {name}\n"
                    f"📞 *Phone:* {phone}\n"
                    f"📅 *Date:* {date}\n"
                    f"⏰ *Time:* {time_str}\n"
                    f"👥 *Party:* {people}\n\n"
                    f"🕒 Created: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}"
                )
                await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=admin_text, parse_mode="Markdown")
        except Exception as e:
            logging.error("Admin notify failed: %s", e)

        return

    # Cancel booking
    if data == "cancel_booking":
        user_id = query.from_user.id
        cancelled = None
        if user_id in reservations:
            cancelled = reservations.pop(user_id)

        context.user_data["active_reservation"] = None

        if cancelled:
            # update history
            bot_hist = context.application.bot_data.get("booking_history", {})
            user_hist = bot_hist.get(user_id, [])
            for b in user_hist:
                if b.get("date") == cancelled.get("date") and b.get("time") == cancelled.get("time") and b.get("status") == "Active":
                    b["status"] = "Cancelled"
                    break

            # notify admin
            try:
                if ADMIN_CHAT_ID:
                    admin_text = (
                        "🚫 *Reservation Cancelled*\n\n"
                        f"🧍 *Name:* {cancelled.get('name','Unknown')}\n"
                        f"📞 *Phone:* {cancelled.get('phone','N/A')}\n"
                        f"📅 *Date:* {cancelled.get('date')}\n"
                        f"⏰ *Time:* {cancelled.get('time')}\n"
                        f"👥 *Party:* {cancelled.get('people',1)}\n\n"
                        f"🕒 Cancelled: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}"
                    )
                    await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=admin_text, parse_mode="Markdown")
            except Exception as e:
                logging.error("Admin cancel notify failed: %s", e)

            # tell user
            await query.edit_message_text(
                f"❌ Your reservation has been cancelled.\n\n"
                f"📅 {cancelled['date']}\n"
                f"⏰ {cancelled['time']}\n"
                f"👥 {cancelled['people']} {'person' if cancelled['people']==1 else 'people'}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="home")]])
            )
        else:
            await query.edit_message_text("⚠️ You don’t have an active reservation to cancel.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="home")]]))
        return

    # Fallback
    logging.warning("Unhandled callback_data: %s", data)
    await query.edit_message_text("⚠️ Unknown action. Use /start", reply_markup=home_keyboard(context))
    return

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Called for plain text messages (name & phone collection).
    """
    text = update.message.text.strip()

    # awaiting name?
    if context.user_data.get("awaiting_name"):
        name = text
        if not name:
            await update.message.reply_text("⚠️ Please enter a valid name (not empty).")
            return
        context.user_data["name"] = name
        context.user_data["awaiting_name"] = False
        context.user_data["awaiting_phone"] = True
        await update.message.reply_text("📞 Please enter your Phone Number (09xxxxxxxx or +2519xxxxxxxx or 2519xxxxxxxx):")
        return

    # awaiting phone?
    if context.user_data.get("awaiting_phone"):
        phone_raw = text
        phone = phone_raw.replace(" ", "").replace("-", "")
        if not re.fullmatch(r"(?:\+2519\d{8}|2519\d{8}|09\d{8})", phone):
            await update.message.reply_text("⚠️ Invalid phone format. Try 09XXXXXXXX or +2519XXXXXXXX.")
            return
        context.user_data["phone"] = phone
        context.user_data["awaiting_phone"] = False

        # Show review and confirm (do not save yet)
        name = context.user_data.get("name")
        date = context.user_data.get("date")
        time_str = context.user_data.get("time")
        people = context.user_data.get("people", 1)

        # safety: ensure required booking pieces exist
        if not all([name, date, time_str]):
            await update.message.reply_text("⚠️ Missing booking details (date/time). Restart with /start.")
            return

        await update.message.reply_text(
            f"✅ Please review your booking:\n\n"
            f"👤 Name: {name}\n"
            f"📞 Phone: {phone}\n"
            f"📅 Date: {date}\n"
            f"⏰ Time: {time_str}\n"
            f"👥 People: {people}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm", callback_data="final_confirm")],
                [InlineKeyboardButton("⬅️ Back", callback_data="back_to_people")]
            ])
        )
        return

    # default
    await update.message.reply_text("Use the menu buttons or /start to begin.")

async def send_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    """Sends a daily summary of today's bookings to the admin."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    slots = generate_slots_for_date(today_str)
    booked = []
    for res in reservations.values():
        if res["date"] == today_str:
            booked.append(f"{res['time']} — {res['people']}p — {res['name']}")

    open_slots = [s for s in slots if all(r["time"] != s for r in reservations.values() if r["date"] == today_str)]

    text = (
        f"🌅 *Daily Schedule Summary ({today_str})*\n\n"
        f"🟢 Reserved Slots:\n" +
        ("\n".join(booked) if booked else "None") +
        "\n\n⚪ Open Slots:\n" +
        ("\n".join(open_slots) if open_slots else "None")
    )

    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode="Markdown")

def main():
    app = Application.builder().token(TOKEN).build()
    job_queue = app.job_queue  # ✅ this activates the JobQueue

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    # ⏰ Schedule daily summary every morning at 7:00 AM
    app.job_queue.run_daily(send_daily_summary, time=dtime(hour=7, minute=0))

    logging.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
