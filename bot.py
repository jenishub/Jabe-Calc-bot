import json
import math
import os
import re
import smtplib
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")

HIDDEN_EMAILS = ["vip@jabe.kz", "production@jabe.kz"]

with open("rates.json", "r") as f:
    RATES = json.load(f)

HOTELS = RATES["hotels"]
TICKETS = RATES["tickets"]
MEALS = RATES["meals"]
VEHICLES = RATES["vehicles"]
TRANSPORT = RATES["transport"]
TOUR_LIST = list(TRANSPORT.keys())
SUV = RATES.get("suv", {"cost": 0, "capacity": 5, "tour_keyword": "kaindy"})

# Tours allowed only on the first day (arrival) and last day (departure)
ARRIVAL_TOURS = [t for t in TOUR_LIST if t.lower().startswith("arrival")]
DEPARTURE_TOURS = [t for t in TOUR_LIST if t.lower().startswith("departure")]
MIDDLE_TOURS = [t for t in TOUR_LIST if t not in ARRIVAL_TOURS and t not in DEPARTURE_TOURS]

COUNTER_FILE = os.path.join(os.getenv("DATA_DIR", "."), "counter.json")
QUOTES_FILE = os.path.join(os.getenv("DATA_DIR", "."), "quotes.json")

# Ensure the data directory exists (Render Disk mount or local folder)
os.makedirs(os.getenv("DATA_DIR", "."), exist_ok=True)

# Access whitelist: comma-separated Telegram IDs in ALLOWED_IDS env var.
# If unset/empty, the bot allows everyone (with a startup warning).
_raw_ids = os.getenv("ALLOWED_IDS", "").replace(" ", "")
ALLOWED_IDS = {int(x) for x in _raw_ids.split(",") if x.isdigit()}


def is_authorized(update):
    if not ALLOWED_IDS:
        return True
    user = update.effective_user
    return user is not None and user.id in ALLOWED_IDS


def load_quotes():
    if os.path.exists(QUOTES_FILE):
        try:
            with open(QUOTES_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_quote(record):
    quotes = load_quotes()
    quotes.append(record)
    with open(QUOTES_FILE, "w") as f:
        json.dump(quotes, f, indent=2)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def next_code():
    if os.path.exists(COUNTER_FILE):
        with open(COUNTER_FILE, "r") as f:
            data = json.load(f)
    else:
        data = {"counter": 0}
    data["counter"] += 1
    with open(COUNTER_FILE, "w") as f:
        json.dump(data, f)
    return f"JBC{data['counter']:03d}"


def parse_single_date(text):
    text = text.strip().lower().replace(",", " ")
    parts = text.split()
    day = None
    month = None
    for p in parts:
        if p.isdigit():
            day = int(p)
        elif p in MONTHS:
            month = MONTHS[p]
    if day and month:
        year = datetime.now().year
        try:
            d = datetime(year, month, day)
        except ValueError:
            return None
        if d.date() < datetime.now().date():
            d = datetime(year + 1, month, day)
        return d
    return None


def parse_dates(text):
    pieces = re.split(r"\s*[-–—]\s*|\s+to\s+", text.strip(), maxsplit=1)
    if len(pieces) != 2:
        return None
    start = parse_single_date(pieces[0])
    end = parse_single_date(pieces[1])
    if not start or not end:
        return None
    if end <= start:
        try:
            end = end.replace(year=end.year + 1)
        except ValueError:
            return None
    nights = (end.date() - start.date()).days
    return start, end, nights


def fmt_kzt(x):
    return f"{int(round(x)):,} KZT"


def fmt_usd(x):
    return f"{x:,.0f} USD"


# ----- Children helpers -----
def classify_age(age):
    """Return pricing category for a child age."""
    if age <= 4:
        return "free"          # 1-4: no charge at all
    if age <= 10:
        return "half_both"     # 5-10: 50% tickets + 50% meals
    if age <= 12:
        return "half_ticket"   # 11-12: 50% tickets, full meals
    return "adult"             # 13+: full adult


def summarize_children(ages):
    """From a list of ages, return counts per category."""
    counts = {"free": 0, "half_both": 0, "half_ticket": 0, "adult": 0}
    for a in ages:
        counts[classify_age(a)] += 1
    return counts


def vehicle_keyboard(seat_count):
    keyboard = []
    for i, v in enumerate(VEHICLES):
        if v["max"] >= seat_count:
            keyboard.append([InlineKeyboardButton(
                f"{v['name']} ({v['capacity']})", callback_data=f"veh_{i}")])
    return InlineKeyboardMarkup(keyboard)


def tours_for_day(day_num, total_days):
    if day_num == 1:
        return ARRIVAL_TOURS
    if day_num == total_days:
        return DEPARTURE_TOURS
    return MIDDLE_TOURS


def tour_keyboard(day_num, total_days):
    options = tours_for_day(day_num, total_days)
    # callback carries index into the FULL TOUR_LIST so the name is unambiguous
    keyboard = [
        [InlineKeyboardButton(t, callback_data=f"tour_{day_num}_{TOUR_LIST.index(t)}")]
        for t in options
    ]
    return InlineKeyboardMarkup(keyboard)


def hotel_keyboard(selected):
    keyboard = []
    for i, h in enumerate(HOTELS):
        check = "✅" if i in selected else "⬜"
        keyboard.append([InlineKeyboardButton(f"{check} {h['name']}", callback_data=f"hot_{i}")])
    keyboard.append([InlineKeyboardButton("✔️ Confirm Hotels", callback_data="hot_confirm")])
    return InlineKeyboardMarkup(keyboard)


def room_keyboard(selected):
    labels = ["Single", "Double", "Triple"]
    keyboard = []
    for i, lab in enumerate(labels):
        check = "✅" if i in selected else "⬜"
        keyboard.append([InlineKeyboardButton(f"{check} {lab}", callback_data=f"room_{i}")])
    keyboard.append([InlineKeyboardButton("✔️ Confirm Rooms", callback_data="room_confirm")])
    return InlineKeyboardMarkup(keyboard)


def tickets_for_tour(tour_name):
    found = []
    for key, price in TICKETS.items():
        if key.lower() in tour_name.lower():
            found.append((key, price))
    return found


def calculate(data):
    adult_count = data["adult_count"]       # adults + children 13+
    child_counts = data["child_counts"]     # dict of category -> count
    nights = data["nights"]
    days = data["days"]
    rate = data["exchange_rate"]
    vcol = VEHICLES[data["vehicle"]]["col"]

    hotel_nights = nights + (1 if data.get("early_checkin") else 0)

    # ----- Hotel part (per occupant of a room, age independent) -----
    hotel_results = []
    for hi in data["hotels"]:
        h = HOTELS[hi]
        rooms = {}
        room_map = [("Single", "single", 1), ("Double", "double", 2), ("Triple", "triple", 3)]
        for label, key, occupancy in room_map:
            idx = {"Single": 0, "Double": 1, "Triple": 2}[label]
            if idx not in data["rooms"]:
                continue
            room_rate = h.get(key)
            if not room_rate:
                continue
            per_pax_kzt = room_rate / occupancy * hotel_nights
            if h["payment"] == "Cash":
                per_pax_kzt *= 1.04
            rooms[label] = math.ceil(per_pax_kzt / rate)
        if rooms:
            hotel_results.append({"name": h["name"], "rooms": rooms, "payment": h["payment"]})

    # ----- Land part components -----
    transport_total = 0
    tickets_per_pax = 0
    ticket_lines = []
    extra_minivan_total = 0
    suv_total = 0
    suv_count = 0
    seats_needed = data.get("seats_needed", data["seat_count"] + 1)
    for tour in data["tours"]:
        transport_total += TRANSPORT[tour][vcol]
        for tname, tprice in tickets_for_tour(tour):
            tickets_per_pax += tprice
            ticket_lines.append(f"{tname}: {fmt_kzt(tprice)} per pax")
        if data["seat_count"] >= 14 and "transfer" in tour.lower():
            extra_minivan_total += TRANSPORT[tour][2]
        if SUV["tour_keyword"].lower() in tour.lower():
            n = math.ceil(seats_needed / SUV["capacity"])
            suv_count += n
            suv_total += n * SUV["cost"]

    lunches = data["lunches"]
    dinners = data["dinners"]
    galas = data["galas"]
    meals_per_pax = lunches * MEALS["lunch"] + dinners * MEALS["dinner"] + galas * MEALS["gala"]

    alcohol_per_pax = 0
    if galas > 0 and data.get("alcohol") == "local":
        alcohol_per_pax = MEALS["alcohol_local"] * galas
    elif galas > 0 and data.get("alcohol") == "premium":
        alcohol_per_pax = MEALS["alcohol_premium"] * galas

    shared_flat = 0
    if galas > 0 and data.get("dj") == "gala":
        shared_flat += MEALS["dj_gala"]
    elif galas > 0 and data.get("dj") == "conference":
        shared_flat += MEALS["dj_gala_conference"]
    if galas > 0 and data.get("dancers"):
        shared_flat += MEALS["dancers"]

    water_per_pax = MEALS["water_per_day"] * days
    markup = data["markup"]

    # Shared costs (transport + DJ/dancers + extra minivan + SUV) split among adults only
    shared_total = transport_total + extra_minivan_total + shared_flat + suv_total
    shared_per_adult = shared_total / adult_count if adult_count else 0

    def to_usd(kzt):
        return kzt * 1.04 / rate

    # Adult price
    adult_land_kzt = shared_per_adult + tickets_per_pax + meals_per_pax + alcohol_per_pax + water_per_pax
    adult_final = math.ceil(to_usd(adult_land_kzt) + markup)

    # Child 5-10: 50% tickets + 50% meals + water (no alcohol, no shared)
    c510_kzt = 0.5 * tickets_per_pax + 0.5 * meals_per_pax + water_per_pax
    c510_final = math.ceil(to_usd(c510_kzt) + markup)

    # Child 11-12: 50% tickets + full meals + water
    c1112_kzt = 0.5 * tickets_per_pax + meals_per_pax + water_per_pax
    c1112_final = math.ceil(to_usd(c1112_kzt) + markup)

    return {
        "hotels": hotel_results,
        "hotel_nights": hotel_nights,
        "transport_total": transport_total,
        "extra_minivan_total": extra_minivan_total,
        "suv_total": suv_total,
        "suv_count": suv_count,
        "shared_flat": shared_flat,
        "shared_per_adult": shared_per_adult,
        "tickets_per_pax": tickets_per_pax,
        "ticket_lines": ticket_lines,
        "meals_per_pax": meals_per_pax,
        "alcohol_per_pax": alcohol_per_pax,
        "water_per_pax": water_per_pax,
        "markup": markup,
        "adult_count": adult_count,
        "child_counts": child_counts,
        "adult_land_kzt": adult_land_kzt,
        "adult_usd_before_markup": to_usd(adult_land_kzt),
        "adult_final": adult_final,
        "c510_final": c510_final,
        "c1112_final": c1112_final,
    }


def build_pdf(code, data, calc):
    filename = f"{code}.pdf"
    doc = SimpleDocTemplate(filename, pagesize=A4,
                            leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=2 * cm, bottomMargin=2 * cm)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("T", parent=styles["Title"], fontSize=18, spaceAfter=6)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=13, spaceBefore=12, spaceAfter=4)
    body = ParagraphStyle("B", parent=styles["Normal"], fontSize=11, leading=15)

    story = []
    story.append(Paragraph("JABE CONCIERGE", title))
    story.append(Paragraph(f"Proposal {code}", styles["Heading3"]))
    story.append(Paragraph(
        f"Travel dates: {data['dates_text']} ({data['nights']} nights / {data['days']} days)", body))
    cc = calc["child_counts"]
    paying_children = cc["half_both"] + cc["half_ticket"]
    free_children = cc["free"]
    story.append(Paragraph(
        f"Group size: {calc['adult_count']} adult(s)"
        + (f", {paying_children} child(ren)" if paying_children else "")
        + (f", {free_children} infant(s) free" if free_children else ""), body))
    story.append(Spacer(1, 10))

    # Hotel part
    story.append(Paragraph("A) Hotel Part Cost (nett rate)", h2))
    for h in calc["hotels"]:
        story.append(Paragraph(f"<b>{h['name']}</b>", body))
        for label, usd in h["rooms"].items():
            story.append(Paragraph(f"{label}: {fmt_usd(usd)} per 1 pax", body))
        story.append(Spacer(1, 6))
    early = " (with early check-in)" if data.get("early_checkin") else " (without early check-in or late check-out)"
    story.append(Paragraph("<b>Inclusions:</b>", body))
    story.append(Paragraph(f"• Hotel accommodation for {calc['hotel_nights']} nights{early}", body))
    story.append(Paragraph("• Daily breakfast", body))

    # Land part
    story.append(Paragraph("B) Land Part Cost", h2))
    story.append(Paragraph(f"<b>Adult: {fmt_usd(calc['adult_final'])} per 1 pax</b>", body))
    if cc["half_both"]:
        story.append(Paragraph(f"<b>Child (5-10 y.o.): {fmt_usd(calc['c510_final'])} per 1 pax</b>", body))
    if cc["half_ticket"]:
        story.append(Paragraph(f"<b>Child (11-12 y.o.): {fmt_usd(calc['c1112_final'])} per 1 pax</b>", body))
    if cc["free"]:
        story.append(Paragraph("<b>Child (1-4 y.o.): complimentary</b>", body))
    story.append(Spacer(1, 4))
    story.append(Paragraph("<b>Inclusions:</b>", body))
    story.append(Paragraph("• All transfers PVT", body))
    vehicle = VEHICLES[data["vehicle"]]
    driver_line = "English speaking driver or guide" if vehicle["english"] else "Driver (non-English speaking)"
    story.append(Paragraph(f"• {driver_line} ({vehicle['name']})", body))
    for i, tour in enumerate(data["tours"], 1):
        story.append(Paragraph(f"• Day {i}: {tour}", body))
    if data["lunches"]:
        story.append(Paragraph(f"• {data['lunches']} x Lunch", body))
    if data["dinners"]:
        story.append(Paragraph(f"• {data['dinners']} x Dinner", body))
    if data["galas"]:
        story.append(Paragraph(f"• {data['galas']} x Gala Dinner", body))
        if data.get("alcohol") == "local":
            story.append(Paragraph("• Alcohol package (local)", body))
        elif data.get("alcohol") == "premium":
            story.append(Paragraph("• Alcohol package (premium)", body))
        if data.get("dj") in ("gala", "conference"):
            story.append(Paragraph("• DJ", body))
        if data.get("dancers"):
            story.append(Paragraph("• Dance show (2 dancers)", body))
    if calc["ticket_lines"]:
        story.append(Paragraph("• Entrance tickets included for selected tours", body))
    if calc.get("suv_count"):
        story.append(Paragraph("• 4x4 SUV transfer at Kaindy Lake", body))
    story.append(Paragraph("• Daily water", body))
    if cc["half_both"] or cc["half_ticket"] or cc["free"]:
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            "<i>Child pricing: 1-4 y.o. free; 5-10 y.o. 50% off tickets &amp; meals; "
            "11-12 y.o. 50% off tickets; 13 y.o. and above charged as adult.</i>", body))

    doc.build(story)
    return filename


def send_hidden_email(code, data, calc, pdf_path):
    cc = calc["child_counts"]
    lines = [
        f"Calculation {code}",
        f"Dates: {data['dates_text']} ({data['nights']} nights / {data['days']} days)",
        f"Adults (incl. 13+): {calc['adult_count']}  |  Children 5-10: {cc['half_both']}  |  "
        f"Children 11-12: {cc['half_ticket']}  |  Infants 1-4 (free): {cc['free']}",
        f"Seat count (5+ y.o.): {data['seat_count']}",
        f"Exchange rate: {data['exchange_rate']} KZT/USD",
        f"Vehicle: {VEHICLES[data['vehicle']]['name']}",
        "",
        "=== COST BREAKDOWN BY PART (KZT) ===",
        "",
        "-- VEHICLE / TRANSPORT (whole group) --",
        f"Transport total: {fmt_kzt(calc['transport_total'])}",
    ]
    if calc["extra_minivan_total"]:
        lines.append(f"Extra minivan (14+ seats, transfers): {fmt_kzt(calc['extra_minivan_total'])}")
    if calc.get("suv_total"):
        lines.append(f"SUV at Kaindy ({calc['suv_count']} x {fmt_kzt(SUV['cost'])}): {fmt_kzt(calc['suv_total'])}")
    if calc["shared_flat"]:
        lines.append(f"DJ / Dancers: {fmt_kzt(calc['shared_flat'])}")
    lines += [
        f"Shared total: {fmt_kzt(calc['transport_total'] + calc['extra_minivan_total'] + calc['shared_flat'] + calc['suv_total'])}",
        f"Per adult (÷{calc['adult_count']}): {fmt_kzt(calc['shared_per_adult'])}",
        "",
        "-- TICKETS (per pax, full price) --",
    ]
    lines += ["  " + l for l in calc["ticket_lines"]] or ["  none"]
    lines += [
        f"Tickets total per pax: {fmt_kzt(calc['tickets_per_pax'])}",
        "",
        "-- MEALS (per pax, full price) --",
        f"Lunches: {data['lunches']} | Dinners: {data['dinners']} | Gala: {data['galas']}",
        f"Meals total per pax: {fmt_kzt(calc['meals_per_pax'])}",
        f"Alcohol per pax (adults only): {fmt_kzt(calc['alcohol_per_pax'])}",
        f"Water per pax: {fmt_kzt(calc['water_per_pax'])}",
        "",
        "-- HOTEL (nett, USD per pax) --",
    ]
    for h in calc["hotels"]:
        rooms = ", ".join(f"{k}: {v}" for k, v in h["rooms"].items())
        lines.append(f"  {h['name']} [{h['payment']}]: {rooms}")
    lines += [
        "",
        "=== LAND TOTALS (USD per pax) ===",
        f"Adult land before markup: {calc['adult_usd_before_markup']:.2f} USD",
        f"MARKUP: {calc['markup']:.2f} USD per paying pax",
        f"Adult FINAL: {calc['adult_final']} USD",
    ]
    if cc["half_both"]:
        lines.append(f"Child 5-10 FINAL: {calc['c510_final']} USD")
    if cc["half_ticket"]:
        lines.append(f"Child 11-12 FINAL: {calc['c1112_final']} USD")

    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(HIDDEN_EMAILS)
    msg["Subject"] = code
    msg.attach(MIMEText("\n".join(lines), "plain"))
    with open(pdf_path, "rb") as f:
        part = MIMEApplication(f.read(), Name=os.path.basename(pdf_path))
    part["Content-Disposition"] = f'attachment; filename="{os.path.basename(pdf_path)}"'
    msg.attach(part)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, HIDDEN_EMAILS, msg.as_string())


# ---------------- Telegram flow ----------------
async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Your Telegram ID is: `{user.id}`\n\n"
        "Share this with the admin to be granted access.",
        parse_mode="Markdown"
    )


async def list_quotes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return
    quotes = load_quotes()
    if not quotes:
        await update.message.reply_text("No saved quotes yet.")
        return
    recent = quotes[-15:]
    lines = ["📑 Recent quotes:\n"]
    for q in recent:
        lines.append(
            f"{q['code']} | {q.get('dates_text', '')} | "
            f"{q.get('adult_count', '?')} ad | Adult {q.get('adult_final', '?')} USD"
        )
    await update.message.reply_text("\n".join(lines))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not is_authorized(update):
        await update.message.reply_text(
            "⛔ You are not authorized to use this bot.\n"
            "Send /myid and share your ID with the admin to request access."
        )
        return
    await jabe_menu(update, context)


async def jabe_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return
    context.user_data.clear()
    keyboard = [[InlineKeyboardButton("🧮 New Calculation", callback_data="new_calc")]]
    await update.message.reply_text(
        "Welcome to JABE Calculator Bot!",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def session_expired(query):
    keyboard = [[InlineKeyboardButton("🧮 New Calculation", callback_data="new_calc")]]
    await query.edit_message_text(
        "⚠️ This calculation session has expired (the bot was restarted).\n"
        "Please start a new calculation.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


STATEFUL_PREFIXES = ("dates_", "hot_", "room_", "early_", "veh_", "tour_", "alc_", "dj_", "dnc_")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ud = context.user_data

    if not is_authorized(update):
        await query.edit_message_text("⛔ You are not authorized to use this bot.")
        return

    if query.data.startswith(STATEFUL_PREFIXES) and "exchange_rate" not in ud:
        await session_expired(query)
        return
    if query.data.startswith(("veh_", "tour_")) and "seats_needed" not in ud:
        await session_expired(query)
        return
    if query.data.startswith("tour_") and "days" not in ud:
        await session_expired(query)
        return

    if query.data == "new_calc":
        ud.clear()
        ud["step"] = "rate"
        await query.edit_message_text(
            "Enter *exchange rate* (KZT per 1 USD):\nExample: 530",
            parse_mode="Markdown"
        )

    elif query.data == "dates_ok":
        ud["hotels_selected"] = []
        ud["step"] = "hotels"
        await query.edit_message_text(
            "Select *hotels* (tap to toggle, then confirm):",
            parse_mode="Markdown",
            reply_markup=hotel_keyboard([])
        )

    elif query.data == "dates_manual":
        ud["step"] = "nights_manual"
        await query.edit_message_text(
            "Enter *number of nights* manually:\nExample: 4",
            parse_mode="Markdown"
        )

    elif query.data.startswith("hot_"):
        if query.data == "hot_confirm":
            if not ud.get("hotels_selected"):
                await query.answer("Select at least one hotel!", show_alert=True)
                return
            ud["rooms_selected"] = []
            await query.edit_message_text(
                "Select *room types* to include:",
                parse_mode="Markdown",
                reply_markup=room_keyboard([])
            )
        else:
            i = int(query.data.split("_")[1])
            sel = ud.get("hotels_selected", [])
            if i in sel:
                sel.remove(i)
            else:
                sel.append(i)
            ud["hotels_selected"] = sel
            await query.edit_message_text(
                "Select *hotels* (tap to toggle, then confirm):",
                parse_mode="Markdown",
                reply_markup=hotel_keyboard(sel)
            )

    elif query.data.startswith("room_"):
        if query.data == "room_confirm":
            if not ud.get("rooms_selected"):
                await query.answer("Select at least one room type!", show_alert=True)
                return
            keyboard = [
                [InlineKeyboardButton("✅ Yes", callback_data="early_yes"),
                 InlineKeyboardButton("❌ No", callback_data="early_no")]
            ]
            await query.edit_message_text(
                "Include *early check-in*? (adds 1 extra night to hotel cost)",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            i = int(query.data.split("_")[1])
            sel = ud.get("rooms_selected", [])
            if i in sel:
                sel.remove(i)
            else:
                sel.append(i)
            ud["rooms_selected"] = sel
            await query.edit_message_text(
                "Select *room types* to include:",
                parse_mode="Markdown",
                reply_markup=room_keyboard(sel)
            )

    elif query.data in ("early_yes", "early_no"):
        ud["early_checkin"] = query.data == "early_yes"
        kb = vehicle_keyboard(ud["seat_count"])
        if not kb.inline_keyboard:
            await query.edit_message_text(
                f"⚠️ No single vehicle fits {ud['seat_count']} passengers (max 16). "
                "Please split the group into smaller calculations."
            )
            ud.clear()
            return
        await query.edit_message_text(
            f"Select *vehicle type* (for {ud['seat_count']} passengers):",
            parse_mode="Markdown",
            reply_markup=kb
        )

    elif query.data.startswith("veh_"):
        ud["vehicle"] = int(query.data.split("_")[1])
        ud["tours"] = []
        await query.edit_message_text(
            "Select tour for *Day 1:*",
            parse_mode="Markdown",
            reply_markup=tour_keyboard(1, ud["days"])
        )

    elif query.data.startswith("tour_"):
        parts = query.data.split("_")
        day_num = int(parts[1])
        tour_index = int(parts[2])
        ud.setdefault("tours", []).append(TOUR_LIST[tour_index])
        if day_num < ud["days"]:
            await query.edit_message_text(
                f"✅ Day {day_num}: {TOUR_LIST[tour_index]}\n\nSelect tour for *Day {day_num + 1}:*",
                parse_mode="Markdown",
                reply_markup=tour_keyboard(day_num + 1, ud["days"])
            )
        else:
            ud["step"] = "lunches"
            await query.edit_message_text("How many *lunches*?\nExample: 4", parse_mode="Markdown")

    elif query.data.startswith("alc_"):
        ud["alcohol"] = query.data.split("_")[1]
        keyboard = [
            [InlineKeyboardButton("DJ (Gala) — 25,000 KZT", callback_data="dj_gala")],
            [InlineKeyboardButton("DJ (Gala + Conference) — 40,000 KZT", callback_data="dj_conference")],
            [InlineKeyboardButton("No DJ", callback_data="dj_none")]
        ]
        await query.edit_message_text(
            "Select *DJ option:*", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith("dj_"):
        ud["dj"] = query.data.split("_")[1]
        keyboard = [
            [InlineKeyboardButton("✅ Yes — 50,000 KZT", callback_data="dnc_yes"),
             InlineKeyboardButton("❌ No", callback_data="dnc_no")]
        ]
        await query.edit_message_text(
            "Add *dancers* (2 dancers)?", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith("dnc_"):
        ud["dancers"] = query.data == "dnc_yes"
        if ud.pop("editing", False):
            await show_review(query.message, ud)
        else:
            ud["step"] = "markup"
            await query.edit_message_text(
                "Enter *markup amount* in USD (flat, per paying pax):\nExample: 50",
                parse_mode="Markdown")

    elif query.data == "gen_proposal":
        if "markup" not in ud:
            await session_expired(query)
            return
        await finalize(update, context)

    elif query.data == "edit_rate":
        if "exchange_rate" not in ud:
            await session_expired(query)
            return
        ud["step"] = "edit_rate"
        await query.edit_message_text(
            "Enter the new *exchange rate* (KZT per 1 USD):", parse_mode="Markdown")

    elif query.data == "edit_markup":
        if "markup" not in ud:
            await session_expired(query)
            return
        ud["step"] = "edit_markup"
        await query.edit_message_text(
            "Enter the new *markup amount* in USD (per paying pax):", parse_mode="Markdown")

    elif query.data == "edit_meals":
        if "exchange_rate" not in ud:
            await session_expired(query)
            return
        ud["step"] = "edit_lunches"
        await query.edit_message_text("How many *lunches*?", parse_mode="Markdown")


def build_review_text(ud):
    counts = ud.get("child_counts", {"free": 0, "half_both": 0, "half_ticket": 0, "adult": 0})
    paying = counts["half_both"] + counts["half_ticket"]
    veh = VEHICLES[ud["vehicle"]]["name"]
    hotels = ", ".join(HOTELS[i]["name"] for i in ud["hotels_selected"])
    rooms = ", ".join(["Single", "Double", "Triple"][i] for i in ud["rooms_selected"])
    lines = [
        "📋 PLEASE REVIEW BEFORE GENERATING:",
        "",
        f"💱 Exchange rate: {ud['exchange_rate']} KZT/USD",
        f"👥 Adults: {ud['adult_count']}"
        + (f", paying children: {paying}" if paying else "")
        + (f", free infants: {counts['free']}" if counts['free'] else ""),
        f"🪑 Passengers (5+ y.o.): {ud['seat_count']}",
        f"📅 Dates: {ud['dates_text']} ({ud['nights']}n/{ud['days']}d)",
        f"🏨 Hotels: {hotels}",
        f"🛏️ Rooms: {rooms}",
        f"🔑 Early check-in: {'Yes' if ud.get('early_checkin') else 'No'}",
        f"🚐 Vehicle: {veh}",
        "🗺️ Tours: " + "; ".join(f"D{i+1} {t}" for i, t in enumerate(ud["tours"])),
        f"🍴 Meals: {ud['lunches']} lunch, {ud['dinners']} dinner, {ud['galas']} gala",
    ]
    if ud["galas"] > 0:
        lines.append(
            f"🥂 Gala extras: alcohol={ud.get('alcohol', 'none')}, "
            f"dj={ud.get('dj', 'none')}, dancers={'yes' if ud.get('dancers') else 'no'}"
        )
    lines.append(f"💵 Markup: {ud['markup']} USD per paying pax")
    return "\n".join(lines)


def review_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Generate Proposal", callback_data="gen_proposal")],
        [InlineKeyboardButton("✏️ Exchange Rate", callback_data="edit_rate"),
         InlineKeyboardButton("✏️ Markup", callback_data="edit_markup")],
        [InlineKeyboardButton("✏️ Meals & Gala", callback_data="edit_meals")],
        [InlineKeyboardButton("🔄 Start Over", callback_data="new_calc")],
    ])


async def show_review(message, ud):
    ud["step"] = None
    # plain text (no parse_mode) so hotel names with '*' can't break parsing
    await message.reply_text(build_review_text(ud), reply_markup=review_keyboard())


async def finalize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    data = {
        "exchange_rate": ud["exchange_rate"],
        "adult_count": ud["adult_count"],
        "child_counts": ud["child_counts"],
        "child_ages": ud.get("child_ages", []),
        "seat_count": ud["seat_count"],
        "seats_needed": ud["seats_needed"],
        "dates_text": ud["dates_text"],
        "nights": ud["nights"],
        "days": ud["days"],
        "hotels": ud["hotels_selected"],
        "rooms": ud["rooms_selected"],
        "early_checkin": ud.get("early_checkin", False),
        "vehicle": ud["vehicle"],
        "tours": ud["tours"],
        "lunches": ud["lunches"],
        "dinners": ud["dinners"],
        "galas": ud["galas"],
        "alcohol": ud.get("alcohol", "none"),
        "dj": ud.get("dj", "none"),
        "dancers": ud.get("dancers", False),
        "markup": ud["markup"],
    }
    code = next_code()
    calc = calculate(data)
    pdf_path = build_pdf(code, data, calc)

    # Works whether finalize was triggered by a text message or a button click
    msg = update.message or (update.callback_query.message if update.callback_query else None)

    # Persist a lightweight record of the quote
    try:
        save_quote({
            "code": code,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "dates_text": data["dates_text"],
            "adult_count": data["adult_count"],
            "adult_final": calc["adult_final"],
            "c510_final": calc["c510_final"],
            "c1112_final": calc["c1112_final"],
            "markup": data["markup"],
        })
    except Exception as e:
        print(f"Quote save failed: {e}")

    await msg.reply_text("✅ Calculation complete! Sending proposal...")
    with open(pdf_path, "rb") as f:
        await msg.reply_document(document=f, filename=pdf_path)

    try:
        send_hidden_email(code, data, calc, pdf_path)
    except Exception as e:
        print(f"Hidden email failed: {e}")

    try:
        os.remove(pdf_path)
    except OSError:
        pass

    keyboard = [[InlineKeyboardButton("🧮 New Calculation", callback_data="new_calc")]]
    await msg.reply_text(
        f"Proposal {code} ready.",
        reply_markup=InlineKeyboardMarkup(keyboard))
    ud.clear()


async def ask_first_tour_or_dates(update, ud):
    ud["hotels_selected"] = []
    ud["step"] = "hotels"


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    if not is_authorized(update):
        return
    if update.effective_chat.type != "private" and not ud.get("step"):
        return
    step = ud.get("step")
    if not step:
        return
    text = update.message.text.strip()

    if step == "rate":
        try:
            ud["exchange_rate"] = float(text.replace(",", "."))
            ud["step"] = "adults"
            await update.message.reply_text("How many *adults*?\nExample: 2", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Please enter a number, e.g. 530")

    elif step == "adults":
        try:
            adults = int(text)
            if adults < 1:
                raise ValueError
            ud["adults_entered"] = adults
            ud["step"] = "children"
            await update.message.reply_text(
                "How many *children*? (enter 0 if none)\nExample: 1", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Please enter a whole number (at least 1 adult).")

    elif step == "children":
        try:
            children = int(text)
            if children < 0:
                raise ValueError
            ud["children_entered"] = children
            ud["child_ages"] = []
            if children == 0:
                finish_pax_setup(ud)
                await send_pax_summary(update, ud)
            else:
                ud["child_index"] = 1
                ud["step"] = "child_age"
                await update.message.reply_text(
                    "Enter age of *child 1:*", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Please enter a whole number (0 or more).")

    elif step == "child_age":
        try:
            age = int(text)
            if age < 0 or age > 17:
                raise ValueError
            ud["child_ages"].append(age)
            if ud["child_index"] < ud["children_entered"]:
                ud["child_index"] += 1
                await update.message.reply_text(
                    f"Enter age of *child {ud['child_index']}:*", parse_mode="Markdown")
            else:
                finish_pax_setup(ud)
                await send_pax_summary(update, ud)
        except ValueError:
            await update.message.reply_text("Please enter a valid age (0-17).")

    elif step == "dates":
        ud["dates_text"] = text
        parsed = parse_dates(text)
        if parsed:
            _, _, nights = parsed
            ud["nights"] = nights
            ud["days"] = nights + 1
            keyboard = [
                [InlineKeyboardButton("✅ Correct", callback_data="dates_ok"),
                 InlineKeyboardButton("✏️ Enter nights manually", callback_data="dates_manual")]
            ]
            await update.message.reply_text(
                f"That's *{nights} nights / {nights + 1} days*. Correct?",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            ud["step"] = "nights_manual"
            await update.message.reply_text(
                "I couldn't read the dates. Enter *number of nights* manually:\nExample: 4",
                parse_mode="Markdown")

    elif step == "nights_manual":
        try:
            nights = int(text)
            if nights < 1:
                raise ValueError
            ud["nights"] = nights
            ud["days"] = nights + 1
            ud["hotels_selected"] = []
            ud["step"] = "hotels"
            await update.message.reply_text(
                "Select *hotels* (tap to toggle, then confirm):",
                parse_mode="Markdown", reply_markup=hotel_keyboard([]))
        except ValueError:
            await update.message.reply_text("Please enter a valid number of nights.")

    elif step == "lunches":
        try:
            ud["lunches"] = int(text)
            ud["step"] = "dinners"
            await update.message.reply_text("How many *dinners*?", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Please enter a number, e.g. 4")

    elif step == "dinners":
        try:
            ud["dinners"] = int(text)
            ud["step"] = "galas"
            await update.message.reply_text("How many *gala dinners*?", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Please enter a number, e.g. 3")

    elif step == "galas":
        try:
            ud["galas"] = int(text)
            ud["step"] = None
            if ud["galas"] > 0:
                keyboard = [
                    [InlineKeyboardButton("Local (vodka) — 1,500/pax", callback_data="alc_local")],
                    [InlineKeyboardButton("Premium (red label, wines) — 3,500/pax", callback_data="alc_premium")],
                    [InlineKeyboardButton("No alcohol", callback_data="alc_none")]
                ]
                await update.message.reply_text(
                    "Select *alcohol package* for gala:", parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                ud["step"] = "markup"
                await update.message.reply_text(
                    "Enter *markup amount* in USD (flat, per paying pax):\nExample: 50",
                    parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Please enter a number, e.g. 1")

    elif step == "markup":
        try:
            ud["markup"] = float(text.replace(",", "."))
            await show_review(update.message, ud)
        except ValueError:
            await update.message.reply_text("Please enter a number, e.g. 50")

    # ----- single-field edits from the review screen -----
    elif step == "edit_rate":
        try:
            ud["exchange_rate"] = float(text.replace(",", "."))
            await show_review(update.message, ud)
        except ValueError:
            await update.message.reply_text("Please enter a number, e.g. 530")

    elif step == "edit_markup":
        try:
            ud["markup"] = float(text.replace(",", "."))
            await show_review(update.message, ud)
        except ValueError:
            await update.message.reply_text("Please enter a number, e.g. 50")

    elif step == "edit_lunches":
        try:
            ud["lunches"] = int(text)
            ud["step"] = "edit_dinners"
            await update.message.reply_text("How many *dinners*?", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Please enter a number, e.g. 4")

    elif step == "edit_dinners":
        try:
            ud["dinners"] = int(text)
            ud["step"] = "edit_galas"
            await update.message.reply_text("How many *gala dinners*?", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Please enter a number, e.g. 3")

    elif step == "edit_galas":
        try:
            ud["galas"] = int(text)
            ud["step"] = None
            if ud["galas"] > 0:
                ud["editing"] = True
                keyboard = [
                    [InlineKeyboardButton("Local (vodka) — 1,500/pax", callback_data="alc_local")],
                    [InlineKeyboardButton("Premium (red label, wines) — 3,500/pax", callback_data="alc_premium")],
                    [InlineKeyboardButton("No alcohol", callback_data="alc_none")]
                ]
                await update.message.reply_text(
                    "Select *alcohol package* for gala:", parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                ud["alcohol"] = "none"
                ud["dj"] = "none"
                ud["dancers"] = False
                await show_review(update.message, ud)
        except ValueError:
            await update.message.reply_text("Please enter a number, e.g. 1")


def finish_pax_setup(ud):
    """Compute adult_count, child category counts, and seat_count from input."""
    ages = ud.get("child_ages", [])
    counts = summarize_children(ages)
    ud["child_counts"] = counts
    # adults for cost division = entered adults + children 13+
    ud["adult_count"] = ud["adults_entered"] + counts["adult"]
    # seat count = everyone aged 5+ (infants 1-4 don't take a seat)
    children_5plus = counts["half_both"] + counts["half_ticket"] + counts["adult"]
    ud["seat_count"] = ud["adults_entered"] + children_5plus
    # group size includes the guide/driver (+1) for vehicle sizing
    ud["seats_needed"] = ud["seat_count"] + 1


async def send_pax_summary(update, ud):
    counts = ud["child_counts"]
    paying = counts["half_both"] + counts["half_ticket"]
    msg = (
        f"👥 *Group summary*\n"
        f"Adults: {ud['adult_count']}"
    )
    if counts["adult"]:
        msg += f"  _(includes {counts['adult']} child(ren) aged 13+ counted as adults)_"
    msg += f"\nChildren (paying): {paying}"
    if counts["half_both"]:
        msg += f"\n  • 5-10 y.o.: {counts['half_both']}"
    if counts["half_ticket"]:
        msg += f"\n  • 11-12 y.o.: {counts['half_ticket']}"
    if counts["free"]:
        msg += f"\nInfants 1-4 (free): {counts['free']}"
    msg += f"\nSeats needed (5+ y.o.): {ud['seat_count']}"
    msg += "\n\n_Note: children aged 13 and above are counted as adults._"

    ud["step"] = "dates"
    await update.message.reply_text(
        msg + "\n\nNow enter *travel dates:*\nExample: June 10 - June 15",
        parse_mode="Markdown")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("jabe", jabe_menu))
    app.add_handler(CommandHandler("myid", my_id))
    app.add_handler(CommandHandler("quotes", list_quotes))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    if ALLOWED_IDS:
        print(f"Calculator bot running. Whitelist active for {len(ALLOWED_IDS)} ID(s).")
    else:
        print("Calculator bot running. WARNING: ALLOWED_IDS not set — bot is open to everyone.")
    app.run_polling()


if __name__ == "__main__":
    main()
