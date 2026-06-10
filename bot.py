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

COUNTER_FILE = "counter.json"

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


def vehicle_keyboard():
    keyboard = [
        [InlineKeyboardButton(f"{v['name']} ({v['capacity']})", callback_data=f"veh_{i}")]
        for i, v in enumerate(VEHICLES)
    ]
    return InlineKeyboardMarkup(keyboard)


def tour_keyboard(day_num):
    keyboard = [
        [InlineKeyboardButton(t, callback_data=f"tour_{day_num}_{i}")]
        for i, t in enumerate(TOUR_LIST)
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
    pax = data["pax"]
    nights = data["nights"]
    days = data["days"]
    rate = data["exchange_rate"]
    vcol = VEHICLES[data["vehicle"]]["col"]

    hotel_nights = nights + (1 if data.get("early_checkin") else 0)

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

    transport_total = 0
    tickets_per_pax = 0
    ticket_lines = []
    extra_minivan_total = 0
    for tour in data["tours"]:
        transport_total += TRANSPORT[tour][vcol]
        for tname, tprice in tickets_for_tour(tour):
            tickets_per_pax += tprice
            ticket_lines.append(f"{tname}: {fmt_kzt(tprice)} per pax")
        if pax >= 14 and "transfer" in tour.lower():
            extra = TRANSPORT[tour][2]
            extra_minivan_total += extra

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

    shared_per_pax = (transport_total + extra_minivan_total + shared_flat) / pax
    land_per_pax_kzt = shared_per_pax + tickets_per_pax + meals_per_pax + alcohol_per_pax + water_per_pax
    land_with_tax = land_per_pax_kzt * 1.04
    land_usd = land_with_tax / rate
    markup = data["markup"]
    land_final = math.ceil(land_usd + markup)

    return {
        "hotels": hotel_results,
        "hotel_nights": hotel_nights,
        "transport_total": transport_total,
        "extra_minivan_total": extra_minivan_total,
        "shared_flat": shared_flat,
        "tickets_per_pax": tickets_per_pax,
        "ticket_lines": ticket_lines,
        "meals_per_pax": meals_per_pax,
        "alcohol_per_pax": alcohol_per_pax,
        "water_per_pax": water_per_pax,
        "land_per_pax_kzt": land_per_pax_kzt,
        "land_with_tax": land_with_tax,
        "land_usd_before_markup": land_usd,
        "markup": markup,
        "land_final": land_final,
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
    story.append(Paragraph(f"Travel dates: {data['dates_text']} ({data['nights']} nights / {data['days']} days)", body))
    story.append(Paragraph(f"Group size: {data['pax']} pax", body))
    story.append(Spacer(1, 10))

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

    story.append(Paragraph(f"B) Land Part Cost — {fmt_usd(calc['land_final'])} per 1 pax", h2))
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
    story.append(Paragraph("• Daily water", body))

    doc.build(story)
    return filename


def send_hidden_email(code, data, calc, pdf_path):
    body_lines = [
        f"Calculation {code}",
        f"Dates: {data['dates_text']} ({data['nights']} nights / {data['days']} days)",
        f"Pax: {data['pax']}",
        f"Exchange rate: {data['exchange_rate']} KZT/USD",
        f"Vehicle: {VEHICLES[data['vehicle']]['name']}",
        "",
        "--- LAND PART BREAKDOWN (KZT, per pax unless noted) ---",
        f"Transport total (whole group): {fmt_kzt(calc['transport_total'])}",
    ]
    if calc["extra_minivan_total"]:
        body_lines.append(f"Extra minivan (14+ pax transfers): {fmt_kzt(calc['extra_minivan_total'])}")
    if calc["shared_flat"]:
        body_lines.append(f"DJ/Dancers (whole group): {fmt_kzt(calc['shared_flat'])}")
    body_lines += [
        f"Tickets per pax: {fmt_kzt(calc['tickets_per_pax'])}",
        f"Meals per pax: {fmt_kzt(calc['meals_per_pax'])}",
        f"Alcohol per pax: {fmt_kzt(calc['alcohol_per_pax'])}",
        f"Water per pax: {fmt_kzt(calc['water_per_pax'])}",
        f"Land per pax before tax: {fmt_kzt(calc['land_per_pax_kzt'])}",
        f"Land per pax with 4% tax: {fmt_kzt(calc['land_with_tax'])}",
        f"Land per pax USD before markup: {calc['land_usd_before_markup']:.2f} USD",
        f"MARKUP: {calc['markup']:.2f} USD per pax",
        f"LAND FINAL (in PDF): {calc['land_final']} USD per pax",
        "",
        "--- HOTEL PART (nett, USD per pax) ---",
    ]
    for h in calc["hotels"]:
        rooms = ", ".join(f"{k}: {v}" for k, v in h["rooms"].items())
        body_lines.append(f"{h['name']} [{h['payment']}]: {rooms}")

    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(HIDDEN_EMAILS)
    msg["Subject"] = code
    msg.attach(MIMEText("\n".join(body_lines), "plain"))
    with open(pdf_path, "rb") as f:
        part = MIMEApplication(f.read(), Name=os.path.basename(pdf_path))
    part["Content-Disposition"] = f'attachment; filename="{os.path.basename(pdf_path)}"'
    msg.attach(part)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, HIDDEN_EMAILS, msg.as_string())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await jabe_menu(update, context)


async def jabe_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    # Guard: buttons from before a restart reference a calculation
    # that no longer exists in memory
    if query.data.startswith(STATEFUL_PREFIXES) and "exchange_rate" not in ud:
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
        ud["step"] = None
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
        await query.edit_message_text(
            "Select *vehicle type:*",
            parse_mode="Markdown",
            reply_markup=vehicle_keyboard()
        )

    elif query.data.startswith("veh_"):
        ud["vehicle"] = int(query.data.split("_")[1])
        ud["tours"] = []
        await query.edit_message_text(
            f"Select tour for *Day 1:*",
            parse_mode="Markdown",
            reply_markup=tour_keyboard(1)
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
                reply_markup=tour_keyboard(day_num + 1)
            )
        else:
            ud["step"] = "lunches"
            await query.edit_message_text(
                "How many *lunches*?\nExample: 4",
                parse_mode="Markdown"
            )

    elif query.data.startswith("alc_"):
        ud["alcohol"] = query.data.split("_")[1]
        keyboard = [
            [InlineKeyboardButton("DJ (Gala) — 25,000 KZT", callback_data="dj_gala")],
            [InlineKeyboardButton("DJ (Gala + Conference) — 40,000 KZT", callback_data="dj_conference")],
            [InlineKeyboardButton("No DJ", callback_data="dj_none")]
        ]
        await query.edit_message_text(
            "Select *DJ option:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data.startswith("dj_"):
        ud["dj"] = query.data.split("_")[1]
        keyboard = [
            [InlineKeyboardButton("✅ Yes — 50,000 KZT", callback_data="dnc_yes"),
             InlineKeyboardButton("❌ No", callback_data="dnc_no")]
        ]
        await query.edit_message_text(
            "Add *dancers* (2 dancers)?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data.startswith("dnc_"):
        ud["dancers"] = query.data == "dnc_yes"
        ud["step"] = "markup"
        await query.edit_message_text(
            "Enter *markup amount* in USD (flat, per pax):\nExample: 50",
            parse_mode="Markdown"
        )


async def finalize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    data = {
        "exchange_rate": ud["exchange_rate"],
        "pax": ud["pax"],
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

    await update.message.reply_text("✅ Calculation complete! Sending proposal...")
    with open(pdf_path, "rb") as f:
        await update.message.reply_document(document=f, filename=pdf_path)

    try:
        send_hidden_email(code, data, calc, pdf_path)
    except Exception as e:
        print(f"Hidden email failed: {e}")

    try:
        os.remove(pdf_path)
    except OSError:
        pass

    keyboard = [[InlineKeyboardButton("🧮 New Calculation", callback_data="new_calc")]]
    await update.message.reply_text(
        f"Proposal *{code}* ready.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    ud.clear()


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    if update.effective_chat.type != "private" and not ud.get("step"):
        return
    step = ud.get("step")
    if not step:
        return
    text = update.message.text.strip()

    if step == "rate":
        try:
            ud["exchange_rate"] = float(text.replace(",", "."))
            ud["step"] = "pax"
            await update.message.reply_text("Enter *pax count* (1-16):", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("Please enter a number, e.g. 530")

    elif step == "pax":
        try:
            pax = int(text)
            if not 1 <= pax <= 16:
                raise ValueError
            ud["pax"] = pax
            ud["step"] = "dates"
            await update.message.reply_text(
                "Enter *travel dates:*\nExample: June 10 - June 15",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("Please enter a number between 1 and 16.")

    elif step == "dates":
        ud["dates_text"] = text
        parsed = parse_dates(text)
        if parsed:
            start_d, end_d, nights = parsed
            ud["nights"] = nights
            ud["days"] = nights + 1
            keyboard = [
                [InlineKeyboardButton("✅ Correct", callback_data="dates_ok"),
                 InlineKeyboardButton("✏️ Enter nights manually", callback_data="dates_manual")]
            ]
            await update.message.reply_text(
                f"That's *{nights} nights / {nights + 1} days*. Correct?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            ud["step"] = "nights_manual"
            await update.message.reply_text(
                "I couldn't read the dates. Enter *number of nights* manually:\nExample: 4",
                parse_mode="Markdown"
            )

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
                parse_mode="Markdown",
                reply_markup=hotel_keyboard([])
            )
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
                    "Select *alcohol package* for gala:",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                ud["step"] = "markup"
                await update.message.reply_text(
                    "Enter *markup amount* in USD (flat, per pax):\nExample: 50",
                    parse_mode="Markdown"
                )
        except ValueError:
            await update.message.reply_text("Please enter a number, e.g. 1")

    elif step == "markup":
        try:
            ud["markup"] = float(text.replace(",", "."))
            ud["step"] = None
            await finalize(update, context)
        except ValueError:
            await update.message.reply_text("Please enter a number, e.g. 50")


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("jabe", jabe_menu))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    print("Calculator bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
