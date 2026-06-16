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

HOTELS = RATES["hotels"]                 # dict: location -> [hotels]
LOCATIONS = ["Almaty", "Shymbulak", "Kolsay"]
TICKETS = RATES["tickets"]
MEALS = RATES["meals"]
VEHICLES = RATES["vehicles"]
TRANSPORT = RATES["transport"]
SUV = RATES.get("suv", {"cost": 20000, "capacity": 5, "tour_keyword": "kaindy"})
DRIVER_STAY = RATES.get("driver_stay", {"solo": 15000, "both": 30000})
SHYMBULAK_CAB = RATES.get("shymbulak_cab", {"cost": 4000, "capacity": 4, "hotel_keyword": "Shymbulak Ski Resort Hotel"})
TOUR_LIST = list(TRANSPORT.keys())

ARRIVAL_TOURS = [t for t in TOUR_LIST if t.lower().startswith("arrival")]
DEPARTURE_TOURS = [t for t in TOUR_LIST if t.lower().startswith("departure")]
MIDDLE_TOURS = [t for t in TOUR_LIST if t not in ARRIVAL_TOURS and t not in DEPARTURE_TOURS]
ROOM_LABELS = ["Single", "Double", "Triple"]

COUNTER_FILE = os.path.join(os.getenv("DATA_DIR", "."), "counter.json")
QUOTES_FILE = os.path.join(os.getenv("DATA_DIR", "."), "quotes.json")
os.makedirs(os.getenv("DATA_DIR", "."), exist_ok=True)

_raw_ids = os.getenv("ALLOWED_IDS", "").replace(" ", "")
ALLOWED_IDS = {int(x) for x in _raw_ids.split(",") if x.isdigit()}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def is_authorized(update):
    if not ALLOWED_IDS:
        return True
    u = update.effective_user
    return u is not None and u.id in ALLOWED_IDS


def load_quotes():
    if os.path.exists(QUOTES_FILE):
        try:
            with open(QUOTES_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_quote(record):
    q = load_quotes()
    q.append(record)
    with open(QUOTES_FILE, "w") as f:
        json.dump(q, f, indent=2)


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
    day = month = None
    for p in text.split():
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
    return start, end, (end.date() - start.date()).days


def is_september(dates_text, parsed):
    if parsed and parsed[0].month == 9:
        return True
    return "sep" in (dates_text or "").lower()


def fmt_kzt(x):
    return f"{int(round(x)):,} KZT"


def fmt_usd(x):
    return f"{x:,.0f} USD"


def classify_age(age):
    if age <= 4:
        return "free"
    if age <= 10:
        return "half_both"
    if age <= 12:
        return "half_ticket"
    return "adult"


def summarize_children(ages):
    counts = {"free": 0, "half_both": 0, "half_ticket": 0, "adult": 0}
    for a in ages:
        counts[classify_age(a)] += 1
    return counts


def tickets_for_tour(tour_name):
    return [(k, v) for k, v in TICKETS.items() if k.lower() in tour_name.lower()]


def tours_for_day(day_num, total_days):
    if day_num == 1:
        return ARRIVAL_TOURS
    if day_num == total_days:
        return DEPARTURE_TOURS
    return MIDDLE_TOURS


# ---------- keyboards ----------
def vehicle_keyboard(seat_count):
    kb = [[InlineKeyboardButton(f"{v['name']} ({v['capacity']})", callback_data=f"veh_{i}")]
          for i, v in enumerate(VEHICLES) if v["max"] >= seat_count]
    return InlineKeyboardMarkup(kb)


def tour_keyboard(day_num, total_days):
    opts = tours_for_day(day_num, total_days)
    kb = [[InlineKeyboardButton(t, callback_data=f"tour_{day_num}_{TOUR_LIST.index(t)}")] for t in opts]
    return InlineKeyboardMarkup(kb)


def loc_hotel_keyboard(location, selected):
    kb = []
    for i, h in enumerate(HOTELS[location]):
        check = "✅" if i in selected else "⬜"
        kb.append([InlineKeyboardButton(f"{check} {h['name']}", callback_data=f"lh_{i}")])
    kb.append([InlineKeyboardButton("✔️ Confirm", callback_data="lh_confirm")])
    return InlineKeyboardMarkup(kb)


def room_keyboard(selected):
    kb = []
    for i, lab in enumerate(ROOM_LABELS):
        check = "✅" if i in selected else "⬜"
        kb.append([InlineKeyboardButton(f"{check} {lab}", callback_data=f"room_{i}")])
    kb.append([InlineKeyboardButton("✔️ Confirm Rooms", callback_data="room_confirm")])
    return InlineKeyboardMarkup(kb)


def hotel_choice_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏨 Include hotel", callback_data="hotelmode_include")],
        [InlineKeyboardButton("🚗 Land only (guest booked own hotel)", callback_data="hotelmode_land")],
    ])


def yesno_keyboard(prefix):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"{prefix}_yes"),
        InlineKeyboardButton("❌ No", callback_data=f"{prefix}_no"),
    ]])


def review_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Generate Proposal", callback_data="gen_proposal")],
        [InlineKeyboardButton("✏️ Exchange Rate", callback_data="edit_rate"),
         InlineKeyboardButton("✏️ Markup", callback_data="edit_markup")],
        [InlineKeyboardButton("🔄 Start Over", callback_data="new_calc")],
    ])


# ---------- calculation ----------
def calculate(data):
    adult_count = data["adult_count"]
    cc = data["child_counts"]
    days = data["days"]
    rate = data["exchange_rate"]
    vcol = VEHICLES[data["vehicle"]]["col"]
    vehicle_has_guide = "guide" in VEHICLES[data["vehicle"]]["name"].lower()
    mode = data.get("hotel_mode", "include")
    nights_split = data.get("nights_split", {})
    sel = data.get("sel", {})
    rooms_sel = data.get("rooms", [])

    # ----- Hotel part (per location) -----
    hotel_results = []
    if mode == "include":
        for loc in LOCATIONS:
            loc_nights = nights_split.get(loc, 0)
            if loc == "Almaty" and data.get("early_checkin"):
                loc_nights += 1
            if loc_nights <= 0:
                continue
            loc_hotels = []
            for hi in sel.get(loc, []):
                h = HOTELS[loc][hi]
                rooms = {}
                for idx, (label, key, occ) in enumerate(
                        [("Single", "single", 1), ("Double", "double", 2), ("Triple", "triple", 3)]):
                    if idx not in rooms_sel:
                        continue
                    room_rate = h.get(key)
                    if not room_rate:
                        continue
                    per_pax = room_rate / occ * loc_nights
                    if h["payment"] == "Cash":
                        per_pax *= 1.04
                    rooms[label] = math.ceil(per_pax / rate)
                if rooms:
                    loc_hotels.append({"name": h["name"], "rooms": rooms, "payment": h["payment"]})
            if loc_hotels:
                hotel_results.append({"location": loc, "nights": loc_nights, "hotels": loc_hotels})

    # ----- Land part -----
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

    # Kolsay driver/guide overnight stay (per Kolsay night)
    kolsay_nights = nights_split.get("Kolsay", 0) if mode == "include" else 0
    stay_rate = DRIVER_STAY["both"] if vehicle_has_guide else DRIVER_STAY["solo"]
    driver_stay_total = kolsay_nights * stay_rate

    # Shymbulak return cab (once per stay, passengers ÷ 4)
    shymbulak_cab_total = 0
    shymbulak_cab_count = 0
    if mode == "include":
        shymbulak_selected = any(
            HOTELS["Shymbulak"][hi]["name"] == SHYMBULAK_CAB["hotel_keyword"]
            for hi in sel.get("Shymbulak", [])
        )
        if shymbulak_selected:
            shymbulak_cab_count = math.ceil(data["seat_count"] / SHYMBULAK_CAB["capacity"])
            shymbulak_cab_total = shymbulak_cab_count * SHYMBULAK_CAB["cost"]

    lunches, dinners, galas = data["lunches"], data["dinners"], data["galas"]
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

    shared_total = transport_total + extra_minivan_total + shared_flat + suv_total + driver_stay_total + shymbulak_cab_total
    shared_per_adult = shared_total / adult_count if adult_count else 0

    def to_usd(kzt):
        return kzt * 1.04 / rate

    adult_land_kzt = shared_per_adult + tickets_per_pax + meals_per_pax + alcohol_per_pax + water_per_pax
    adult_final = math.ceil(to_usd(adult_land_kzt) + markup)
    c510_kzt = 0.5 * tickets_per_pax + 0.5 * meals_per_pax + water_per_pax
    c510_final = math.ceil(to_usd(c510_kzt) + markup)
    c1112_kzt = 0.5 * tickets_per_pax + meals_per_pax + water_per_pax
    c1112_final = math.ceil(to_usd(c1112_kzt) + markup)

    return {
        "hotel_results": hotel_results,
        "transport_total": transport_total,
        "extra_minivan_total": extra_minivan_total,
        "suv_total": suv_total,
        "suv_count": suv_count,
        "driver_stay_total": driver_stay_total,
        "kolsay_nights": kolsay_nights,
        "shymbulak_cab_total": shymbulak_cab_total,
        "shymbulak_cab_count": shymbulak_cab_count,
        "shared_flat": shared_flat,
        "shared_per_adult": shared_per_adult,
        "tickets_per_pax": tickets_per_pax,
        "ticket_lines": ticket_lines,
        "meals_per_pax": meals_per_pax,
        "alcohol_per_pax": alcohol_per_pax,
        "water_per_pax": water_per_pax,
        "markup": markup,
        "adult_count": adult_count,
        "child_counts": cc,
        "adult_land_kzt": adult_land_kzt,
        "adult_usd_before_markup": to_usd(adult_land_kzt),
        "adult_final": adult_final,
        "c510_final": c510_final,
        "c1112_final": c1112_final,
    }


def build_pdf(code, data, calc):
    filename = f"{code}.pdf"
    doc = SimpleDocTemplate(filename, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=2 * cm, bottomMargin=2 * cm)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("T", parent=styles["Title"], fontSize=18, spaceAfter=6)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=13, spaceBefore=12, spaceAfter=4)
    body = ParagraphStyle("B", parent=styles["Normal"], fontSize=11, leading=15)
    cc = calc["child_counts"]

    s = []
    s.append(Paragraph("JABE CONCIERGE", title))
    s.append(Paragraph(f"Proposal {code}", styles["Heading3"]))
    s.append(Paragraph(f"Travel dates: {data['dates_text']} ({data['nights']} nights / {data['days']} days)", body))
    paying = cc["half_both"] + cc["half_ticket"]
    s.append(Paragraph(
        f"Group size: {calc['adult_count']} adult(s)"
        + (f", {paying} child(ren)" if paying else "")
        + (f", {cc['free']} infant(s) free" if cc["free"] else ""), body))
    s.append(Spacer(1, 10))

    mode = data.get("hotel_mode", "include")
    if mode == "include":
        s.append(Paragraph("A) Hotel Part Cost (nett rate)", h2))
        for loc in calc["hotel_results"]:
            s.append(Paragraph(f"<b>{loc['location']} — {loc['nights']} night(s)</b>", body))
            for h in loc["hotels"]:
                s.append(Paragraph(f"{h['name']}", body))
                for label, usd in h["rooms"].items():
                    s.append(Paragraph(f"&nbsp;&nbsp;&nbsp;{label}: {fmt_usd(usd)} per 1 pax", body))
            s.append(Spacer(1, 6))

        # (no combined total — multiple hotels per location may be shown as alternatives)
        early = " (with early check-in)" if data.get("early_checkin") else " (without early check-in or late check-out)"
        s.append(Paragraph("<b>Inclusions:</b>", body))
        s.append(Paragraph(f"• Accommodation as listed{early}", body))
        s.append(Paragraph("• Daily breakfast", body))
    elif mode == "high_season":
        s.append(Paragraph("A) Hotel Part Cost", h2))
        s.append(Paragraph(
            "September is peak high season. Hotel rates and availability change frequently and "
            "cannot be quoted automatically. Please confirm actual rates and availability with "
            "<b>sales@jabe.kz</b>.", body))
    else:
        s.append(Paragraph("A) Hotel Part Cost", h2))
        s.append(Paragraph("Hotel not included — accommodation arranged directly by the guest.", body))

    s.append(Paragraph("B) Land Part Cost", h2))
    s.append(Paragraph(f"<b>Adult: {fmt_usd(calc['adult_final'])} per 1 pax</b>", body))
    if cc["half_both"]:
        s.append(Paragraph(f"<b>Child (5-10 y.o.): {fmt_usd(calc['c510_final'])} per 1 pax</b>", body))
    if cc["half_ticket"]:
        s.append(Paragraph(f"<b>Child (11-12 y.o.): {fmt_usd(calc['c1112_final'])} per 1 pax</b>", body))
    if cc["free"]:
        s.append(Paragraph("<b>Child (1-4 y.o.): complimentary</b>", body))
    s.append(Spacer(1, 4))
    s.append(Paragraph("<b>Inclusions:</b>", body))
    s.append(Paragraph("• All transfers PVT", body))
    veh = VEHICLES[data["vehicle"]]
    s.append(Paragraph(
        f"• {'English speaking driver or guide' if veh['english'] else 'Driver (non-English speaking)'} ({veh['name']})", body))
    for i, tour in enumerate(data["tours"], 1):
        s.append(Paragraph(f"• Day {i}: {tour}", body))
    if data["lunches"]:
        s.append(Paragraph(f"• {data['lunches']} x Lunch", body))
    if data["dinners"]:
        s.append(Paragraph(f"• {data['dinners']} x Dinner", body))
    if data["galas"]:
        s.append(Paragraph(f"• {data['galas']} x Gala Dinner", body))
        if data.get("alcohol") == "local":
            s.append(Paragraph("• Alcohol package (local)", body))
        elif data.get("alcohol") == "premium":
            s.append(Paragraph("• Alcohol package (premium)", body))
        if data.get("dj") in ("gala", "conference"):
            s.append(Paragraph("• DJ", body))
        if data.get("dancers"):
            s.append(Paragraph("• Dance show (2 dancers)", body))
    if calc["ticket_lines"]:
        s.append(Paragraph("• Entrance tickets included for selected tours", body))
    if calc.get("suv_count"):
        s.append(Paragraph("• 4x4 SUV transfer at Kaindy Lake", body))
    if calc.get("kolsay_nights"):
        s.append(Paragraph("• Driver/guide overnight stay at Kolsay", body))
    if calc.get("shymbulak_cab_count"):
        s.append(Paragraph("• Return cab transfer at Shymbulak", body))
    s.append(Paragraph("• Daily water", body))
    if paying or cc["free"]:
        s.append(Spacer(1, 6))
        s.append(Paragraph(
            "<i>Child pricing: 1-4 y.o. free; 5-10 y.o. 50% off tickets &amp; meals; "
            "11-12 y.o. 50% off tickets; 13 y.o. and above charged as adult.</i>", body))

    issue_date = datetime.now().strftime("%d %B %Y")
    s.append(Spacer(1, 12))
    roe = ParagraphStyle("ROE", parent=body, fontSize=9, textColor="#555555")
    s.append(Paragraph(
        f"Based on ROE {data['exchange_rate']} KZT/USD as of {issue_date}. "
        "Subject to change to the actual ROE before confirmation.", roe))

    doc.build(s)
    return filename


def send_hidden_email(code, data, calc, pdf_path):
    cc = calc["child_counts"]
    lines = [
        f"Calculation {code}",
        f"Dates: {data['dates_text']} ({data['nights']} nights / {data['days']} days)",
        f"Hotel mode: {data.get('hotel_mode', 'include')}",
        f"Adults (incl. 13+): {calc['adult_count']} | Children 5-10: {cc['half_both']} | "
        f"Children 11-12: {cc['half_ticket']} | Infants 1-4: {cc['free']}",
        f"Seat count (5+ y.o.): {data['seat_count']}",
        f"Exchange rate: {data['exchange_rate']} KZT/USD",
        f"Vehicle: {VEHICLES[data['vehicle']]['name']}",
        "",
        "=== COST BREAKDOWN BY PART (KZT, nett — 4% land tax applied at totals) ===",
        "",
        "-- VEHICLE / TRANSPORT (whole group) --",
        f"Transport total: {fmt_kzt(calc['transport_total'])}",
    ]
    if calc["extra_minivan_total"]:
        lines.append(f"Extra minivan (14+ seats): {fmt_kzt(calc['extra_minivan_total'])}")
    if calc.get("suv_total"):
        lines.append(f"SUV at Kaindy ({calc['suv_count']} x {fmt_kzt(SUV['cost'])}): {fmt_kzt(calc['suv_total'])}")
    if calc.get("driver_stay_total"):
        lines.append(f"Driver/guide Kolsay stay ({calc['kolsay_nights']} night(s)): {fmt_kzt(calc['driver_stay_total'])}")
    if calc.get("shymbulak_cab_total"):
        lines.append(f"Shymbulak return cab ({calc['shymbulak_cab_count']} x {fmt_kzt(SHYMBULAK_CAB['cost'])}): {fmt_kzt(calc['shymbulak_cab_total'])}")
    if calc["shared_flat"]:
        lines.append(f"DJ / Dancers: {fmt_kzt(calc['shared_flat'])}")
    lines += [
        f"Shared total: {fmt_kzt(calc['transport_total'] + calc['extra_minivan_total'] + calc['shared_flat'] + calc['suv_total'] + calc['driver_stay_total'] + calc['shymbulak_cab_total'])}",
        f"Per adult (÷{calc['adult_count']}): {fmt_kzt(calc['shared_per_adult'])}",
        "",
        "-- TICKETS (per pax, full) --",
    ]
    lines += ["  " + l for l in calc["ticket_lines"]] or ["  none"]
    lines += [
        f"Tickets total per pax: {fmt_kzt(calc['tickets_per_pax'])}",
        "",
        "-- MEALS (per pax, full) --",
        f"Lunches: {data['lunches']} | Dinners: {data['dinners']} | Gala: {data['galas']}",
        f"Meals total per pax: {fmt_kzt(calc['meals_per_pax'])}",
        f"Alcohol per pax (adults only): {fmt_kzt(calc['alcohol_per_pax'])}",
        f"Water per pax: {fmt_kzt(calc['water_per_pax'])}",
        "",
        "-- HOTEL (nett, USD per pax, per location) --",
    ]
    if data.get("hotel_mode") == "include":
        for loc in calc["hotel_results"]:
            lines.append(f"  {loc['location']} ({loc['nights']} night(s)):")
            for h in loc["hotels"]:
                rooms = ", ".join(f"{k}: {v}" for k, v in h["rooms"].items())
                lines.append(f"    {h['name']} [{h['payment']}]: {rooms}")
    else:
        lines.append(f"  ({data.get('hotel_mode')})")
    lines += [
        "",
        "=== LAND TOTALS (per adult) ===",
        f"Land subtotal (nett, KZT): {fmt_kzt(calc['adult_land_kzt'])}",
        f"+ 4% land tax (KZT): {fmt_kzt(calc['adult_land_kzt'] * 1.04)}",
        f"= before markup (USD, ÷{data['exchange_rate']}): {calc['adult_usd_before_markup']:.2f} USD",
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


# ---------- pax setup ----------
def finish_pax_setup(ud):
    ages = ud.get("child_ages", [])
    counts = summarize_children(ages)
    ud["child_counts"] = counts
    ud["adult_count"] = ud["adults_entered"] + counts["adult"]
    children_5plus = counts["half_both"] + counts["half_ticket"] + counts["adult"]
    ud["seat_count"] = ud["adults_entered"] + children_5plus
    ud["seats_needed"] = ud["seat_count"] + 1


# ---------- Telegram handlers ----------
async def my_id(update, context):
    await update.message.reply_text(
        f"Your Telegram ID is: {update.effective_user.id}\n"
        "Share this with the admin to be granted access.")


async def list_quotes(update, context):
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return
    quotes = load_quotes()
    if not quotes:
        await update.message.reply_text("No saved quotes yet.")
        return
    lines = ["📑 Recent quotes:\n"]
    for q in quotes[-15:]:
        lines.append(f"{q['code']} | {q.get('dates_text','')} | {q.get('adult_count','?')} ad | "
                     f"Adult {q.get('adult_final','?')} USD")
    await update.message.reply_text("\n".join(lines))


async def start(update, context):
    if update.effective_chat.type != "private":
        return
    if not is_authorized(update):
        await update.message.reply_text(
            "⛔ You are not authorized. Send /myid and share your ID with the admin.")
        return
    await jabe_menu(update, context)


async def jabe_menu(update, context):
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return
    context.user_data.clear()
    await update.message.reply_text(
        "Welcome to JABE Calculator Bot!",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧮 New Calculation", callback_data="new_calc")]]))


async def session_expired(query):
    await query.edit_message_text(
        "⚠️ This calculation session has expired (the bot was restarted). Please start a new calculation.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧮 New Calculation", callback_data="new_calc")]]))


STATEFUL_PREFIXES = ("dates_", "hotelmode_", "lh_", "room_", "early_", "veh_", "tour_",
                     "meals_", "alc_", "dj_", "dnc_", "gen_", "edit_")


async def show_vehicle_prompt(send, ud):
    kb = vehicle_keyboard(ud["seat_count"])
    if not kb.inline_keyboard:
        await send(f"⚠️ No single vehicle fits {ud['seat_count']} passengers (max 16). "
                   "Please split the group into smaller calculations.")
        ud.clear()
        return
    await send(f"Select vehicle type (for {ud['seat_count']} passengers):", reply_markup=kb)


async def show_current_location_hotels(query, ud):
    loc = ud["acc_locs"][ud["acc_idx"]]
    ud.setdefault("sel", {}).setdefault(loc, [])
    await query.edit_message_text(
        f"Select hotel(s) in *{loc}* ({ud['nights_split'][loc]} night(s)):",
        parse_mode="Markdown", reply_markup=loc_hotel_keyboard(loc, ud["sel"][loc]))


async def advance_after_hotels(query, ud):
    """After all locations' hotels chosen, ask room types."""
    ud["rooms_selected"] = []
    ud["step"] = None
    await query.edit_message_text("Select *room types* to include:",
                                  parse_mode="Markdown", reply_markup=room_keyboard([]))


async def proceed_to_vehicle_or_early(query, ud):
    """After rooms: ask early check-in if Almaty has nights, else vehicle."""
    if ud.get("hotel_mode") == "include" and ud["nights_split"].get("Almaty", 0) > 0:
        await query.edit_message_text(
            "Include *early check-in* in Almaty? (adds 1 extra night to Almaty hotel cost)",
            parse_mode="Markdown", reply_markup=yesno_keyboard("early"))
    else:
        ud["early_checkin"] = False
        await show_vehicle_prompt(
            lambda text, reply_markup=None: query.edit_message_text(text, reply_markup=reply_markup), ud)


async def ask_meals_required(send):
    await send("Are meals required for this package?", reply_markup=yesno_keyboard("meals"))


async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    ud = context.user_data
    if not is_authorized(update):
        await query.edit_message_text("⛔ You are not authorized to use this bot.")
        return
    data = query.data

    if data.startswith(STATEFUL_PREFIXES) and "exchange_rate" not in ud and data != "edit_rate":
        await session_expired(query)
        return

    if data == "new_calc":
        ud.clear()
        ud["step"] = "rate"
        await query.edit_message_text("Enter *exchange rate* (KZT per 1 USD):\nExample: 530", parse_mode="Markdown")

    # ----- dates confirmation -----
    elif data == "dates_ok":
        if ud.get("is_september"):
            ud["hotel_mode"] = "high_season"
            await query.edit_message_text(
                "⚠️ September is peak high season. Hotel rates/availability must be confirmed with "
                "sales@jabe.kz, so the hotel part is excluded. Proceeding with the land package.")
            await show_vehicle_prompt(
                lambda text, reply_markup=None: query.message.reply_text(text, reply_markup=reply_markup), ud)
        else:
            await query.edit_message_text("Will this quote include a hotel, or land only?",
                                          reply_markup=hotel_choice_keyboard())

    elif data == "dates_manual":
        ud["step"] = "nights_manual"
        await query.edit_message_text("Enter *number of nights* manually:\nExample: 4", parse_mode="Markdown")

    # ----- hotel mode -----
    elif data == "hotelmode_include":
        ud["hotel_mode"] = "include"
        ud["step"] = "nights_almaty"
        await query.edit_message_text(
            f"Total {ud['nights']} night(s). How many nights in *Almaty*?", parse_mode="Markdown")

    elif data == "hotelmode_land":
        ud["hotel_mode"] = "land_only"
        ud["sel"] = {}
        ud["nights_split"] = {}
        ud["rooms_selected"] = []
        await show_vehicle_prompt(
            lambda text, reply_markup=None: query.edit_message_text(text, reply_markup=reply_markup), ud)

    # ----- per-location hotel selection -----
    elif data.startswith("lh_"):
        loc = ud["acc_locs"][ud["acc_idx"]]
        if data == "lh_confirm":
            if not ud["sel"].get(loc):
                await query.answer("Select at least one hotel!", show_alert=True)
                return
            ud["acc_idx"] += 1
            if ud["acc_idx"] < len(ud["acc_locs"]):
                await show_current_location_hotels(query, ud)
            else:
                await advance_after_hotels(query, ud)
        else:
            i = int(data.split("_")[1])
            seln = ud["sel"].setdefault(loc, [])
            if i in seln:
                seln.remove(i)
            else:
                seln.append(i)
            await query.edit_message_text(
                f"Select hotel(s) in *{loc}* ({ud['nights_split'][loc]} night(s)):",
                parse_mode="Markdown", reply_markup=loc_hotel_keyboard(loc, seln))

    # ----- room types -----
    elif data.startswith("room_"):
        if data == "room_confirm":
            if not ud.get("rooms_selected"):
                await query.answer("Select at least one room type!", show_alert=True)
                return
            await proceed_to_vehicle_or_early(query, ud)
        else:
            i = int(data.split("_")[1])
            seln = ud.setdefault("rooms_selected", [])
            if i in seln:
                seln.remove(i)
            else:
                seln.append(i)
            await query.edit_message_text("Select *room types* to include:",
                                          parse_mode="Markdown", reply_markup=room_keyboard(seln))

    # ----- early check-in -----
    elif data in ("early_yes", "early_no"):
        ud["early_checkin"] = data == "early_yes"
        await show_vehicle_prompt(
            lambda text, reply_markup=None: query.edit_message_text(text, reply_markup=reply_markup), ud)

    # ----- vehicle -----
    elif data.startswith("veh_"):
        ud["vehicle"] = int(data.split("_")[1])
        ud["tours"] = []
        await query.edit_message_text("Select tour for *Day 1:*", parse_mode="Markdown",
                                      reply_markup=tour_keyboard(1, ud["days"]))

    elif data.startswith("tour_"):
        parts = data.split("_")
        day_num, tour_index = int(parts[1]), int(parts[2])
        ud.setdefault("tours", []).append(TOUR_LIST[tour_index])
        if day_num < ud["days"]:
            await query.edit_message_text(
                f"✅ Day {day_num}: {TOUR_LIST[tour_index]}\n\nSelect tour for *Day {day_num + 1}:*",
                parse_mode="Markdown", reply_markup=tour_keyboard(day_num + 1, ud["days"]))
        else:
            await ask_meals_required(
                lambda text, reply_markup=None: query.edit_message_text(text, reply_markup=reply_markup))

    # ----- meals yes/no -----
    elif data == "meals_yes":
        ud["step"] = "lunches"
        await query.edit_message_text("How many *lunches*?\nExample: 4", parse_mode="Markdown")

    elif data == "meals_no":
        ud["lunches"] = ud["dinners"] = ud["galas"] = 0
        ud["alcohol"] = "none"
        ud["dj"] = "none"
        ud["dancers"] = False
        ud["step"] = "markup"
        await query.edit_message_text(
            "Enter *markup amount* in USD (flat, per paying pax):\nExample: 50", parse_mode="Markdown")

    # ----- gala extras -----
    elif data.startswith("alc_"):
        ud["alcohol"] = data.split("_")[1]
        await query.edit_message_text("Select *DJ option:*", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("DJ (Gala) — 25,000 KZT", callback_data="dj_gala")],
                [InlineKeyboardButton("DJ (Gala + Conference) — 40,000 KZT", callback_data="dj_conference")],
                [InlineKeyboardButton("No DJ", callback_data="dj_none")]]))

    elif data.startswith("dj_"):
        ud["dj"] = data.split("_")[1]
        await query.edit_message_text("Add *dancers* (2 dancers)?", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes — 50,000 KZT", callback_data="dnc_yes"),
                InlineKeyboardButton("❌ No", callback_data="dnc_no")]]))

    elif data.startswith("dnc_"):
        ud["dancers"] = data == "dnc_yes"
        if ud.pop("editing", False):
            await show_review(query.message, ud)
        else:
            ud["step"] = "markup"
            await query.edit_message_text(
                "Enter *markup amount* in USD (flat, per paying pax):\nExample: 50", parse_mode="Markdown")

    # ----- review actions -----
    elif data == "gen_proposal":
        if "markup" not in ud:
            await session_expired(query)
            return
        await finalize(update, context)

    elif data == "edit_rate":
        if "exchange_rate" not in ud:
            await session_expired(query)
            return
        ud["step"] = "edit_rate"
        await query.edit_message_text("Enter the new *exchange rate* (KZT per 1 USD):", parse_mode="Markdown")

    elif data == "edit_markup":
        if "markup" not in ud:
            await session_expired(query)
            return
        ud["step"] = "edit_markup"
        await query.edit_message_text("Enter the new *markup amount* in USD:", parse_mode="Markdown")


# ---------- review ----------
def build_review_text(ud):
    cc = ud.get("child_counts", {"free": 0, "half_both": 0, "half_ticket": 0, "adult": 0})
    paying = cc["half_both"] + cc["half_ticket"]
    veh = VEHICLES[ud["vehicle"]]["name"]
    lines = [
        "📋 PLEASE REVIEW BEFORE GENERATING:", "",
        f"Exchange rate: {ud['exchange_rate']} KZT/USD",
        f"Adults: {ud['adult_count']}" + (f", paying children: {paying}" if paying else "")
        + (f", free infants: {cc['free']}" if cc["free"] else ""),
        f"Passengers (5+ y.o.): {ud['seat_count']}",
        f"Dates: {ud['dates_text']} ({ud['nights']}n/{ud['days']}d)",
    ]
    mode = ud.get("hotel_mode", "include")
    if mode == "include":
        split = ud.get("nights_split", {})
        acc = []
        for loc in LOCATIONS:
            if split.get(loc, 0) > 0:
                names = ", ".join(HOTELS[loc][i]["name"] for i in ud.get("sel", {}).get(loc, []))
                acc.append(f"{loc} {split[loc]}n: {names}")
        lines.append("Hotels: " + " | ".join(acc))
        lines.append("Rooms: " + ", ".join(ROOM_LABELS[i] for i in ud.get("rooms_selected", [])))
        lines.append(f"Early check-in: {'Yes' if ud.get('early_checkin') else 'No'}")
    elif mode == "high_season":
        lines.append("Hotels: SEPTEMBER high season — confirm with sales@jabe.kz")
    else:
        lines.append("Hotels: Land only (guest booked own)")
    lines.append(f"Vehicle: {veh}")
    lines.append("Tours: " + "; ".join(f"D{i+1} {t}" for i, t in enumerate(ud["tours"])))
    lines.append(f"Meals: {ud['lunches']} lunch, {ud['dinners']} dinner, {ud['galas']} gala")
    if ud["galas"] > 0:
        lines.append(f"Gala extras: alcohol={ud.get('alcohol','none')}, dj={ud.get('dj','none')}, "
                     f"dancers={'yes' if ud.get('dancers') else 'no'}")
    lines.append(f"Markup: {ud['markup']} USD per paying pax")
    return "\n".join(lines)


async def show_review(message, ud):
    ud["step"] = None
    await message.reply_text(build_review_text(ud), reply_markup=review_keyboard())


async def finalize(update, context):
    ud = context.user_data
    data = {
        "exchange_rate": ud["exchange_rate"], "adult_count": ud["adult_count"],
        "child_counts": ud["child_counts"], "child_ages": ud.get("child_ages", []),
        "seat_count": ud["seat_count"], "seats_needed": ud["seats_needed"],
        "dates_text": ud["dates_text"], "nights": ud["nights"], "days": ud["days"],
        "hotel_mode": ud.get("hotel_mode", "include"),
        "nights_split": ud.get("nights_split", {}), "sel": ud.get("sel", {}),
        "rooms": ud.get("rooms_selected", []), "early_checkin": ud.get("early_checkin", False),
        "vehicle": ud["vehicle"], "tours": ud["tours"],
        "lunches": ud["lunches"], "dinners": ud["dinners"], "galas": ud["galas"],
        "alcohol": ud.get("alcohol", "none"), "dj": ud.get("dj", "none"),
        "dancers": ud.get("dancers", False), "markup": ud["markup"],
    }
    code = next_code()
    calc = calculate(data)
    pdf_path = build_pdf(code, data, calc)
    msg = update.message or (update.callback_query.message if update.callback_query else None)

    try:
        save_quote({"code": code, "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "dates_text": data["dates_text"], "adult_count": data["adult_count"],
                    "adult_final": calc["adult_final"], "markup": data["markup"]})
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
    await msg.reply_text(f"Proposal {code} ready.",
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧮 New Calculation", callback_data="new_calc")]]))
    ud.clear()


# ---------- message (text) handler ----------
async def message_handler(update, context):
    ud = context.user_data
    if not is_authorized(update):
        return
    if update.effective_chat.type != "private" and not ud.get("step"):
        return
    step = ud.get("step")
    if not step:
        return
    text = update.message.text.strip()
    reply = update.message.reply_text

    if step == "rate":
        try:
            ud["exchange_rate"] = float(text.replace(",", "."))
            ud["step"] = "adults"
            await reply("How many *adults*?\nExample: 2", parse_mode="Markdown")
        except ValueError:
            await reply("Please enter a number, e.g. 530")

    elif step == "adults":
        try:
            a = int(text)
            if a < 1:
                raise ValueError
            ud["adults_entered"] = a
            ud["step"] = "children"
            await reply("How many *children*? (0 if none)\nExample: 1", parse_mode="Markdown")
        except ValueError:
            await reply("Please enter a whole number (at least 1 adult).")

    elif step == "children":
        try:
            c = int(text)
            if c < 0:
                raise ValueError
            ud["children_entered"] = c
            ud["child_ages"] = []
            if c == 0:
                finish_pax_setup(ud)
                await send_pax_summary(update, ud)
            else:
                ud["child_index"] = 1
                ud["step"] = "child_age"
                await reply("Enter age of *child 1:*", parse_mode="Markdown")
        except ValueError:
            await reply("Please enter a whole number (0 or more).")

    elif step == "child_age":
        try:
            age = int(text)
            if age < 0 or age > 17:
                raise ValueError
            ud["child_ages"].append(age)
            if ud["child_index"] < ud["children_entered"]:
                ud["child_index"] += 1
                await reply(f"Enter age of *child {ud['child_index']}:*", parse_mode="Markdown")
            else:
                finish_pax_setup(ud)
                await send_pax_summary(update, ud)
        except ValueError:
            await reply("Please enter a valid age (0-17).")

    elif step == "dates":
        ud["dates_text"] = text
        parsed = parse_dates(text)
        ud["is_september"] = is_september(text, parsed)
        if parsed:
            _, _, nights = parsed
            ud["nights"], ud["days"] = nights, nights + 1
            await reply(f"That's *{nights} nights / {nights + 1} days*. Correct?", parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ Correct", callback_data="dates_ok"),
                            InlineKeyboardButton("✏️ Enter nights manually", callback_data="dates_manual")]]))
        else:
            ud["step"] = "nights_manual"
            await reply("I couldn't read the dates. Enter *number of nights* manually:\nExample: 4", parse_mode="Markdown")

    elif step == "nights_manual":
        try:
            nights = int(text)
            if nights < 1:
                raise ValueError
            ud["nights"], ud["days"] = nights, nights + 1
            ud["step"] = None
            if ud.get("is_september"):
                ud["hotel_mode"] = "high_season"
                await reply("⚠️ September is peak high season. Hotel part excluded — confirm with sales@jabe.kz. "
                            "Proceeding with land package.")
                await show_vehicle_prompt(lambda t, reply_markup=None: update.message.reply_text(t, reply_markup=reply_markup), ud)
            else:
                await reply("Will this quote include a hotel, or land only?", reply_markup=hotel_choice_keyboard())
        except ValueError:
            await reply("Please enter a valid number of nights.")

    # ----- accommodation nights split -----
    elif step == "nights_almaty":
        n = _parse_int(text)
        if n is None:
            await reply("Please enter a number (0 or more).")
            return
        ud["split_almaty"] = n
        ud["step"] = "nights_shymbulak"
        await reply("How many nights in *Shymbulak*? (0 if none)", parse_mode="Markdown")

    elif step == "nights_shymbulak":
        n = _parse_int(text)
        if n is None:
            await reply("Please enter a number (0 or more).")
            return
        ud["split_shymbulak"] = n
        ud["step"] = "nights_kolsay"
        await reply("How many nights in *Kolsay*? (0 if none)", parse_mode="Markdown")

    elif step == "nights_kolsay":
        n = _parse_int(text)
        if n is None:
            await reply("Please enter a number (0 or more).")
            return
        ud["split_kolsay"] = n
        total = ud["split_almaty"] + ud["split_shymbulak"] + n
        if total != ud["nights"]:
            await reply(f"⚠️ Those add up to {total} nights but the trip is {ud['nights']} nights. "
                        f"Let's re-enter. How many nights in *Almaty*?", parse_mode="Markdown")
            ud["step"] = "nights_almaty"
            return
        ud["nights_split"] = {"Almaty": ud["split_almaty"], "Shymbulak": ud["split_shymbulak"], "Kolsay": n}
        ud["acc_locs"] = [loc for loc in LOCATIONS if ud["nights_split"][loc] > 0]
        ud["acc_idx"] = 0
        ud["sel"] = {}
        ud["step"] = None
        loc = ud["acc_locs"][0]
        await reply(f"Select hotel(s) in *{loc}* ({ud['nights_split'][loc]} night(s)):",
                    parse_mode="Markdown", reply_markup=loc_hotel_keyboard(loc, []))

    # ----- meals -----
    elif step == "lunches":
        v = _parse_int(text)
        if v is None:
            await reply("Please enter a number, e.g. 4")
            return
        ud["lunches"] = v
        ud["step"] = "dinners"
        await reply("How many *dinners*?", parse_mode="Markdown")

    elif step == "dinners":
        v = _parse_int(text)
        if v is None:
            await reply("Please enter a number, e.g. 3")
            return
        ud["dinners"] = v
        ud["step"] = "galas"
        await reply("How many *gala dinners*?", parse_mode="Markdown")

    elif step == "galas":
        v = _parse_int(text)
        if v is None:
            await reply("Please enter a number, e.g. 1")
            return
        ud["galas"] = v
        ud["step"] = None
        if v > 0:
            await reply("Select *alcohol package* for gala:", parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Local (vodka) — 1,500/pax", callback_data="alc_local")],
                    [InlineKeyboardButton("Premium (red label, wines) — 3,500/pax", callback_data="alc_premium")],
                    [InlineKeyboardButton("No alcohol", callback_data="alc_none")]]))
        else:
            ud["alcohol"] = "none"
            ud["dj"] = "none"
            ud["dancers"] = False
            ud["step"] = "markup"
            await reply("Enter *markup amount* in USD (flat, per paying pax):\nExample: 50", parse_mode="Markdown")

    elif step == "markup":
        try:
            ud["markup"] = float(text.replace(",", "."))
            await show_review(update.message, ud)
        except ValueError:
            await reply("Please enter a number, e.g. 50")

    elif step == "edit_rate":
        try:
            ud["exchange_rate"] = float(text.replace(",", "."))
            await show_review(update.message, ud)
        except ValueError:
            await reply("Please enter a number, e.g. 530")

    elif step == "edit_markup":
        try:
            ud["markup"] = float(text.replace(",", "."))
            await show_review(update.message, ud)
        except ValueError:
            await reply("Please enter a number, e.g. 50")


def _parse_int(text):
    try:
        v = int(text)
        return v if v >= 0 else None
    except ValueError:
        return None


async def send_pax_summary(update, ud):
    cc = ud["child_counts"]
    paying = cc["half_both"] + cc["half_ticket"]
    msg = f"👥 Group summary\nAdults: {ud['adult_count']}"
    if cc["adult"]:
        msg += f" (includes {cc['adult']} child(ren) 13+ counted as adults)"
    msg += f"\nChildren (paying): {paying}"
    if cc["half_both"]:
        msg += f"\n  5-10 y.o.: {cc['half_both']}"
    if cc["half_ticket"]:
        msg += f"\n  11-12 y.o.: {cc['half_ticket']}"
    if cc["free"]:
        msg += f"\nInfants 1-4 (free): {cc['free']}"
    msg += f"\nSeats needed (5+ y.o.): {ud['seat_count']}"
    msg += "\n\nNote: children aged 13 and above are counted as adults."
    ud["step"] = "dates"
    await update.message.reply_text(msg + "\n\nNow enter travel dates:\nExample: June 10 - June 15")


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
