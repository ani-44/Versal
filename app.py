import bcrypt
import uuid
import random
import re
import json
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode
from bson import ObjectId

from flask import (
    Flask, request, redirect, render_template,
    session, send_file, flash, jsonify
)

from pymongo import MongoClient, DESCENDING
from pymongo.errors import DuplicateKeyError

from flask_mail import Mail, Message
from authlib.integrations.flask_client import OAuth
from authlib.integrations.base_client.errors import OAuthError

from config import MONGO_URI, MONGO_DB, SECRET_KEY
from utils.pdf_generator import generate_ticket_pdf
from utils.serper_search import unified_search
from utils.ai_engine import (
    get_best_plan,
    fetch_trending_locations,
    rag_retrieve,
    call_ai as ai_call,
    groq_health_check,
)
from utils.email_service import send_welcome_email, send_booking_confirmation


# ================= APP INIT =================
app = Flask(__name__)
app.config.from_pyfile("config.py")
app.secret_key = SECRET_KEY
app.config["SESSION_COOKIE_SECURE"] = app.config.get("SESSION_COOKIE_SECURE", False)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

mail = Mail(app)

oauth = OAuth(app)
google_client_id = app.config.get("GOOGLE_CLIENT_ID", "")
google_client_secret = app.config.get("GOOGLE_CLIENT_SECRET", "")
if google_client_id and google_client_secret:
    oauth.register(
        name="google",
        client_id=google_client_id,
        client_secret=google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

SYSTEM_PROMPT = """You are TicketBot, the assistant for TicketHub.
Rules:
- Never invent route, price, discount, seat count, or availability.
- Treat all ticket/pricing data as dynamic and query-driven.
- If user asks to book, guide them to dashboard/search and next booking steps.
- Keep replies concise, practical, and user-action oriented.
"""

MIN_VISIBLE_PRICE = 500.0


# ================= MONGODB CONNECTION =================
_mongo_client = None

def get_db():
    global _mongo_client
    try:
        if _mongo_client is None:
            _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        db = _mongo_client[MONGO_DB]
        # Ensure indexes
        db.users.create_index("email", unique=True)
        return db
    except Exception as e:
        print("MongoDB connection failed:", e)
        return None


def _id_str(doc):
    """Convert ObjectId to string in a document."""
    if doc and "_id" in doc:
        doc["id"] = str(doc["_id"])
    return doc


def _ids_str(docs):
    return [_id_str(d) for d in docs]


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _canonical_ticket_type(ticket_type):
    ticket_type = str(ticket_type or "").upper().strip()
    return {"EVENT": "CONCERT"}.get(ticket_type, ticket_type)


def _default_total_seats(ticket_type):
    return {
        "FLIGHT": 180,
        "TRAIN": 60,
        "CONCERT": 500,
        "HOTEL": 100,
    }.get(ticket_type, 40)


def _default_live_price(ticket_type):
    return {
        "BUS": 800,
        "TRAIN": 1200,
        "FLIGHT": 4500,
        "HOTEL": 6000,
        "CONCERT": 3000,
    }.get(ticket_type, 1000)


def _is_price_visible(price=None, final_price=None, min_price=MIN_VISIBLE_PRICE):
    """
    Enforce global visibility threshold.
    If final_price exists, use it; else use price.
    Unknown prices are allowed to pass through.
    """
    chosen = final_price if final_price not in (None, "") else price
    try:
        if chosen in (None, ""):
            return True
        return float(chosen) >= float(min_price)
    except Exception:
        return True


def _today_date():
    return datetime.now().date()


def _parse_search_date(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _valid_future_date(value):
    parsed = _parse_search_date(value)
    return parsed is not None and parsed >= _today_date()


def _format_travel_date(value):
    parsed = _parse_search_date(value)
    return parsed.strftime("%d %b %Y") if parsed else str(value or "")


def _ticket_context(ticket_info):
    ticket_type = str(ticket_info.get("type", "")).upper()
    source = str(ticket_info.get("source") or "").strip()
    destination = str(ticket_info.get("destination") or "").strip()
    title = str(ticket_info.get("title") or ticket_type or "Ticket").strip()

    if ticket_type == "HOTEL":
        location = destination or source or title
        return "Hotel / Location", location
    if ticket_type == "CONCERT":
        location = destination or source or title
        return "Event / Location", f"{title} - {location}" if location and location not in title else title
    if source and destination and source != destination:
        return "Route", f"{source} → {destination}"
    return "Route", source or destination or title


def _local_ai_fallback(user_msg: str, is_logged_in: bool = False) -> str:
    q = (user_msg or "").lower()
    if any(k in q for k in ["price", "cheapest", "cheap", "deal", "discount"]):
        return "Live prices are dynamic. Please search by route/date/type so I can suggest the current best options."
    if any(k in q for k in ["book", "how", "steps"]):
        if is_logged_in:
            return "Go to Dashboard, pick a ticket, select seat, complete payment, then download your PDF ticket."
        return "Please login first, then go to Dashboard, choose ticket, select seat, and complete payment."
    if any(k in q for k in ["flight", "bus", "train", "hotel", "concert"]):
        return "We have BUS, TRAIN, FLIGHT, HOTEL, and CONCERT tickets. Open Dashboard to browse all available options."
    return "I can help with prices, routes, and booking steps. Ask for cheapest options or how to book quickly."


def _extract_travel_intent(text):
    q = str(text or "").strip().lower()
    if not q:
        return None
    raw_text = str(text or "").strip()

    def _extract_date_from_query(s):
        s_l = str(s or "").lower().strip()
        if not s_l:
            return None

        today = _today_date()
        rel_map = {
            "today": 0,
            "tomorrow": 1,
            "day after tomorrow": 2,
        }
        for k, delta in rel_map.items():
            if k in s_l:
                return (today + timedelta(days=delta)).isoformat()

        next_day_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        m = re.search(r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", s_l)
        if m:
            target = next_day_map[m.group(1)]
            days_ahead = (target - today.weekday() + 7) % 7
            if days_ahead == 0:
                days_ahead = 7
            return (today + timedelta(days=days_ahead)).isoformat()

        m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s_l)
        if m:
            try:
                d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
                if d >= today:
                    return d.isoformat()
            except Exception:
                pass

        m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", s_l)
        if m:
            try:
                day = int(m.group(1))
                month = int(m.group(2))
                year = int(m.group(3))
                if year < 100:
                    year += 2000
                d = datetime(year, month, day).date()
                if d >= today:
                    return d.isoformat()
            except Exception:
                pass

        m = re.search(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)(?:\s+(\d{4}))?\b",
            s_l,
        )
        if m:
            month_map = {
                "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
                "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
                "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
                "nov": 11, "november": 11, "dec": 12, "december": 12,
            }
            try:
                day = int(m.group(1))
                month = month_map[m.group(2)]
                year = int(m.group(3)) if m.group(3) else today.year
                d = datetime(year, month, day).date()
                if d < today and not m.group(3):
                    d = datetime(year + 1, month, day).date()
                if d >= today:
                    return d.isoformat()
            except Exception:
                pass
        return None

    travel_type = None
    if "flight" in q:
        travel_type = "flight"
    elif "bus" in q:
        travel_type = "bus"
    elif "train" in q:
        travel_type = "train"
    elif "hotel" in q:
        travel_type = "hotel"
    elif "concert" in q or "event" in q:
        travel_type = "concert"

    if not travel_type:
        return None

    source = ""
    destination = ""
    keyword = ""

    def _clean_city_fragment(val):
        s = str(val or "")
        s = re.sub(
            r"\b(i|want|to|book|a|an|the|get|me|please|ticket|tickets|for|on|of|from|travel|trip|ride|journey|search)\b",
            " ",
            s,
            flags=re.IGNORECASE,
        )
        s = re.sub(r"\b(bus|train|flight|hotel|concert|event)\b", " ", s, flags=re.IGNORECASE)
        s = re.sub(r"\s+", " ", s).strip(" ,.-")
        return s

    if travel_type in {"flight", "bus", "train"}:
        route_match = re.search(r"\bfrom\s+([a-z\s]+?)\s+to\s+([a-z\s]+)\b", q)
        if route_match:
            source = route_match.group(1).strip()
            destination = route_match.group(2).strip()
        route_match = re.search(rf"\b{travel_type}\s+([a-z\s]+?)\s+to\s+([a-z\s]+)\b", q)
        if route_match:
            source = route_match.group(1).strip()
            destination = route_match.group(2).strip()
        else:
            generic_to = re.search(r"\b([a-z\s]+?)\s+to\s+([a-z\s]+)\b", q)
            if generic_to:
                source = generic_to.group(1).strip()
                destination = generic_to.group(2).strip()
                source = re.sub(
                    r"\b(i|want|to|book|a|an|the|get|me|please|ticket|tickets|for|on)\b",
                    " ",
                    source,
                )
                source = re.sub(r"\s+", " ", source).strip()
        source = _clean_city_fragment(source)
        destination = _clean_city_fragment(destination)
    elif travel_type == "hotel":
        in_match = re.search(r"\bhotel(?:s)?\s+(?:in|at)\s+([a-z\s]+)\b", q)
        if in_match:
            destination = in_match.group(1).strip()
    elif travel_type == "concert":
        # Supports both "concert in mumbai" and "Arijit Singh concert in mumbai"
        in_match = re.search(r"\b(?:concert|event)(?:s)?\s+(?:in|at|on)\s+([a-z\s]+)\b", q)
        if in_match:
            destination = in_match.group(1).strip()
        artist_city_match = re.search(r"\b([a-z\s]+?)\s+(?:concert|event)(?:s)?\s+(?:in|at|on)\s+([a-z\s]+)\b", q)
        if artist_city_match:
            keyword = artist_city_match.group(1).strip()
            destination = artist_city_match.group(2).strip() or destination
        # Pattern: "concert ticket honey singh on mumbai on 25/05/2026"
        ticket_artist_city = re.search(
            r"\b(?:concert|event)\s+(?:ticket|tickets)\s+([a-z\s]+?)\s+on\s+([a-z\s]+?)(?:\s+on\s+\d|\s*$)",
            q,
        )
        if ticket_artist_city:
            keyword = ticket_artist_city.group(1).strip() or keyword
            destination = ticket_artist_city.group(2).strip() or destination
        # Keyword-only fallback: "Arijit Singh concert"
        if not keyword:
            kw_match = re.search(r"\b([a-z\s]+?)\s+(?:concert|event)(?:s)?\b", q)
            if kw_match:
                keyword = kw_match.group(1).strip()
        # Last fallback: if no keyword parsed, use cleaned text
        if not keyword:
            keyword = re.sub(
                r"\b(i|want|to|book|a|an|the|get|me|please|ticket|tickets|show|find|for)\b",
                " ",
                q,
            )
            keyword = re.sub(r"\bconcert|event|events\b", " ", keyword)
            keyword = re.sub(r"\s+", " ", keyword).strip()
        # Normalize destination: trim date-like fragments accidentally captured after "on"
        destination = re.sub(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", " ", destination)
        destination = re.sub(
            r"\b\d{1,2}(?:st|nd|rd|th)?\s+(jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\b",
            " ",
            destination,
            flags=re.IGNORECASE,
        )
        destination = re.sub(r"\s+", " ", destination).strip()

    source = " ".join(w.capitalize() for w in source.split() if w)
    destination = " ".join(w.capitalize() for w in destination.split() if w)
    keyword = " ".join(w.capitalize() for w in keyword.split() if w)

    if travel_type in {"flight", "bus", "train"} and (not source or not destination):
        return None
    if travel_type == "hotel" and not destination:
        return None
    if travel_type == "concert" and not (destination or keyword):
        return None

    parsed_date = _extract_date_from_query(raw_text) or _today_date().isoformat()

    return {
        "type": travel_type,
        "source": source,
        "destination": destination,
        "keyword": keyword,
        "date": parsed_date,
    }


def _live_search_button(intent):
    if not intent:
        return None
    params = {
        "autofill": "1",
        "autorun": "1",
        "type": intent.get("type", ""),
        "source": intent.get("source", ""),
        "destination": intent.get("destination", ""),
        "keyword": intent.get("keyword", ""),
        "date": intent.get("date", ""),
    }
    url = "/search?" + urlencode(params)
    if str(intent.get("type", "")).lower() == "concert":
        k = intent.get("keyword", "") or "Concert"
        c = intent.get("destination", "") or intent.get("source", "")
        label = f"Live Search: {k}" + (f" in {c}" if c else "")
    else:
        s = intent.get("source", "")
        d = intent.get("destination", "")
        label = f"Live Search: {s} -> {d}" if (s or d) else "Live Search"
    return {"label": label, "url": url}


def _ai_understand_user_query(user_text, is_logged_in=False):
    """
    Use AI to understand user intent/action, with deterministic fallback.
    Returns: {"action": str|None, "intent": dict|None}
    """
    text = str(user_text or "").strip()
    if not text:
        return {"action": None, "intent": None}

    allowed_actions = ["book", "my_bookings", "dashboard", "logout", "general"]
    ai_result = {"action": "general", "intent": None}
    today_iso = _today_date().isoformat()

    prompt = (
        "Classify the user's travel-chat intent and extract structured fields.\n"
        "Return ONLY valid JSON with keys: action, type, source, destination, keyword, date.\n"
        "Rules:\n"
        "- action must be one of: book, my_bookings, dashboard, logout, general.\n"
        "- type must be one of: bus, train, flight, hotel, concert, or empty.\n"
        "- For bus/train/flight: try to extract source and destination.\n"
        "- For hotel: extract destination city if available.\n"
        "- For concert: extract keyword/event and optional destination city.\n"
        f"- date must be ISO yyyy-mm-dd; if user did not provide date, set to {today_iso}.\n"
        "- If uncertain, keep fields empty, action=general.\n"
        f"- User logged in: {'yes' if is_logged_in else 'no'}.\n"
        f"User query: {text}"
    )

    try:
        raw = ai_call(
            [{"role": "user", "content": prompt}],
            max_tokens=180,
            temperature=0.0,
        )
        parsed = json.loads(str(raw or "").strip())
        if isinstance(parsed, dict):
            action = str(parsed.get("action", "general")).strip().lower()
            if action not in allowed_actions:
                action = "general"
            i_type = str(parsed.get("type", "")).strip().lower()
            source = str(parsed.get("source", "")).strip()
            destination = str(parsed.get("destination", "")).strip()
            keyword = str(parsed.get("keyword", "")).strip()
            date = str(parsed.get("date", "")).strip()
            if not _valid_future_date(date):
                date = today_iso
            ai_result = {
                "action": action,
                "intent": {
                    "type": i_type,
                    "source": source,
                    "destination": destination,
                    "keyword": keyword,
                    "date": date,
                },
            }
    except Exception:
        ai_result = {"action": "general", "intent": None}

    # Deterministic fallback/guardrails.
    fallback_intent = _extract_travel_intent(text)
    q = text.lower()
    fallback_action = None
    if any(k in q for k in ["book", "buy", "purchase", "reserve", "ticket", "get ticket"]):
        fallback_action = "book"
    elif any(k in q for k in ["my booking", "my ticket", "view booking", "booking history", "show booking"]):
        fallback_action = "my_bookings"
    elif any(k in q for k in ["dashboard", "search ticket", "find ticket", "browse ticket"]):
        fallback_action = "dashboard"
    elif any(k in q for k in ["logout", "sign out", "log out"]):
        fallback_action = "logout"

    final_action = ai_result.get("action") if ai_result.get("action") != "general" else fallback_action
    final_intent = ai_result.get("intent") or fallback_intent

    if final_intent and final_intent.get("type") not in {"bus", "train", "flight", "hotel", "concert"}:
        final_intent = fallback_intent

    # Post-normalize extracted values so auto-flow links are clean and useful.
    def _clean_city_fragment(val):
        s = str(val or "")
        s = re.sub(
            r"\b(i|want|to|book|a|an|the|get|me|please|ticket|tickets|for|on|of|from|travel|trip|ride|journey|search)\b",
            " ",
            s,
            flags=re.IGNORECASE,
        )
        s = re.sub(r"\b(bus|train|flight|hotel|concert|event)\b", " ", s, flags=re.IGNORECASE)
        s = re.sub(r"\s+", " ", s).strip(" ,.-")
        return " ".join(w.capitalize() for w in s.split() if w)

    if final_intent:
        # Merge missing pieces from deterministic parser to keep auto-flow reliable.
        if fallback_intent:
            if not final_intent.get("type"):
                final_intent["type"] = fallback_intent.get("type", "")
            if not final_intent.get("source"):
                final_intent["source"] = fallback_intent.get("source", "")
            if not final_intent.get("destination"):
                final_intent["destination"] = fallback_intent.get("destination", "")
            if not final_intent.get("keyword"):
                final_intent["keyword"] = fallback_intent.get("keyword", "")
            if not _valid_future_date(final_intent.get("date", "")):
                final_intent["date"] = fallback_intent.get("date", _today_date().isoformat())

        final_intent["source"] = str(final_intent.get("source", "")).strip()
        final_intent["destination"] = str(final_intent.get("destination", "")).strip()
        final_intent["keyword"] = str(final_intent.get("keyword", "")).strip()
        if not _valid_future_date(final_intent.get("date", "")):
            final_intent["date"] = _today_date().isoformat()

        if final_intent.get("type") in {"bus", "train", "flight"}:
            final_intent["source"] = _clean_city_fragment(final_intent.get("source", ""))
            final_intent["destination"] = _clean_city_fragment(final_intent.get("destination", ""))

        if final_intent.get("type") == "concert":
            raw = text
            raw_l = raw.lower()

            # Extract artist/event more precisely for phrases like:
            # "book a concert ticket of Arjit singh on date 20/05/2026 in mumbai"
            artist_patterns = [
                r"(?:ticket|tickets)\s+of\s+([a-zA-Z\s]+?)(?:\s+on\s+date|\s+on\s+|\s+in\s+|$)",
                r"(?:concert|event)(?:\s+ticket|\s+tickets)?\s+of\s+([a-zA-Z\s]+?)(?:\s+on\s+date|\s+on\s+|\s+in\s+|$)",
                r"\bfor\s+([a-zA-Z\s]+?)(?:\s+concert|\s+event|\s+on\s+date|\s+on\s+|\s+in\s+|$)",
                r"\b([a-zA-Z\s]+?)\s+(?:concert|event)(?:\s+ticket|\s+tickets)?(?:\s+in\s+|\s+on\s+|$)",
            ]
            clean_keyword = ""
            for p in artist_patterns:
                m = re.search(p, raw, flags=re.IGNORECASE)
                if m:
                    clean_keyword = m.group(1).strip()
                    break

            if not clean_keyword:
                clean_keyword = final_intent.get("keyword", "")

            # Remove common filler words from keyword if AI returned noisy phrase.
            clean_keyword = re.sub(
                r"\b(i|want|to|book|a|an|the|get|me|please|ticket|tickets|concert|event|show|find|on|date|in)\b",
                " ",
                clean_keyword,
                flags=re.IGNORECASE,
            )
            clean_keyword = re.sub(r"\s+", " ", clean_keyword).strip()
            final_intent["keyword"] = " ".join(w.capitalize() for w in clean_keyword.split() if w)

            # Extract/normalize city.
            city_match = re.search(r"\b(?:in|at)\s+([a-zA-Z\s]+?)(?:\s+on\s+date|\s+on\s+|$)", raw, flags=re.IGNORECASE)
            if city_match:
                city = city_match.group(1).strip()
                city = re.sub(r"\b(today|tomorrow)\b", " ", city, flags=re.IGNORECASE)
                city = re.sub(r"\s+", " ", city).strip()
                final_intent["destination"] = " ".join(w.capitalize() for w in city.split() if w)

            # If still empty, use fallback intent values.
            if not final_intent.get("keyword") and fallback_intent:
                final_intent["keyword"] = fallback_intent.get("keyword", "")
            if not final_intent.get("destination") and fallback_intent:
                final_intent["destination"] = fallback_intent.get("destination", "")

    return {"action": final_action, "intent": final_intent}


# ================= SEED DATA (disabled) =================
def seed_sample_data():
    # Static seed data is intentionally disabled to keep runtime data dynamic.
    return


# ================= OTP HELPER =================
def generate_otp():
    return str(random.randint(100000, 999999))


# ================= SAVE BOOKING =================
def save_booking_to_db(data):
    db = get_db()
    if db is None:
        return False
    try:
        ticket_id_str = session.get("ticket_id", "")
        booking_doc = {
            "user_id":      data["user_id"],
            "ticket_id":    ticket_id_str,
            "seat_no":      data["seat"],
            "payment_id":   data["booking_id"],
            "final_price":  data["amount"],
            "travel_date":  data.get("travel_date_key", data.get("travel_date", "")),
            "route":        data.get("route", ""),
            "context_label": data.get("context_label", "Route"),
            "context_value": data.get("context_value", data.get("route", "")),
            "title":        data.get("title", ""),
            "type":         data.get("type", ""),
            "source":       data.get("source", ""),
            "destination":  data.get("destination", ""),
            "base_price":   data.get("base_price", data.get("amount", 0)),
            "discount":     data.get("discount", 0),
            "payment_status": data.get("payment_status", "PAID"),
            "booking_date": datetime.now(),
        }
        db.bookings.insert_one(booking_doc)
        return True
    except Exception as e:
        print("Save booking error:", e)
        return False


def save_chat_history(user_message, bot_reply, source="ticketbot", action=None):
    if "user_id" not in session:
        return False
    db = get_db()
    if db is None:
        return False
    try:
        db.chat_history.insert_one({
            "user_id": session["user_id"],
            "user_name": session.get("user_name", ""),
            "user_email": session.get("user_email", ""),
            "source": source,
            "user_message": str(user_message or ""),
            "bot_reply": str(bot_reply or ""),
            "action": action,
            "created_at": datetime.now(),
        })
        return True
    except Exception as e:
        print("Save chat history error:", e)
        return False


# ================= HOME =================
@app.route("/")
def home():
    return render_template("user/home.html")


# ================= SIGNUP (FIXED: duplicate email check + welcome email) =================
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name     = request.form.get("name", "").strip()
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")

        if not name or not email or not password:
            flash("All fields are required.", "danger")
            return redirect("/signup")

        if password != confirm:
            flash("Passwords do not match.", "danger")
            return redirect("/signup")

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return redirect("/signup")

        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        db = get_db()
        if db is None:
            flash("Database connection error.", "danger")
            return redirect("/signup")

        # Explicit duplicate-email check (clear UX) + unique index as DB-level safety net
        if db.users.find_one({"email": email}):
            flash("Email already registered. Please login or use a different email.", "warning")
            return redirect("/signup")

        try:
            db.users.insert_one({
                "name":       name,
                "email":      email,
                "password":   hashed,
                "role":       "USER",
                "otp":        None,
                "is_blocked": False,
                "created_at": datetime.now(),
            })

            # Send welcome email (non-blocking — failure doesn't break signup)
            try:
                login_url = request.host_url.rstrip("/") + "/login"
                send_welcome_email(mail, name, email, login_url)
            except Exception as mail_err:
                print(f"Welcome email failed (non-critical): {mail_err}")

            flash("Account created! Check your email for a welcome message.", "success")
            return redirect("/login")

        except DuplicateKeyError:
            flash("Email already registered.", "warning")
            return redirect("/signup")
        except Exception as e:
            flash(f"Signup error: {str(e)}", "danger")
            return redirect("/signup")

    return render_template("user/signup.html", google_oauth_enabled=bool(google_client_id and google_client_secret))


# ================= LOGIN =================
@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect("/dashboard")
    if "admin_id" in session:
        return redirect("/admin/dashboard")

    if request.method == "POST":
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        if not email or not password:
            flash("Email and password are required.", "danger")
            return redirect("/login")

        db = get_db()
        if db is None:
            flash("Database connection error", "danger")
            return redirect("/login")

        try:
            user = db.users.find_one({"email": email})
        except Exception as e:
            print(f"Login DB query error: {e}")
            flash("Database query error. Please try again.", "danger")
            return redirect("/login")
        if not user:
            flash("No account found with that email.", "danger")
            return redirect("/login")

        stored_pw = user.get("password")
        if not stored_pw:
            flash("This account uses Google sign-in. Please continue with Google.", "warning")
            return redirect("/login")
        valid = False
        if stored_pw.startswith("$2b$") or stored_pw.startswith("$2a$"):
            try:
                valid = bcrypt.checkpw(password.encode(), stored_pw.encode())
            except Exception:
                valid = False
        else:
            valid = (password == stored_pw)
            if valid:
                new_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
                try:
                    db.users.update_one({"_id": user["_id"]}, {"$set": {"password": new_hash}})
                except Exception as e:
                    print(f"Password migration update error: {e}")

        if not valid:
            flash("Incorrect password. Please try again.", "danger")
            return redirect("/login")

        role = user.get("role", "USER")
        if role == "ADMIN":
            session.clear()
            session["admin_id"]    = str(user["_id"])
            session["admin_name"]  = user["name"]
            session["admin_email"] = user["email"]
            flash(f"Welcome back, {user['name']}! (Admin)", "success")
            return redirect("/admin/dashboard")
        else:
            if user.get("is_blocked"):
                flash("Your account has been blocked. Please contact support.", "danger")
                return redirect("/login")
            session.clear()
            session["user_id"]    = str(user["_id"])
            session["user_name"]  = user["name"]
            session["user_email"] = user["email"]
            # Support ?next= param from search widget / chatbot
            next_url = request.args.get("next", "/dashboard")
            if not next_url.startswith("/"):
                next_url = "/dashboard"
            return redirect(next_url)

    return render_template("user/login.html", google_oauth_enabled=bool(google_client_id and google_client_secret))


@app.route("/auth/google/login")
def auth_google_login():
    if not (google_client_id and google_client_secret):
        flash("Google OAuth is not configured.", "warning")
        return redirect("/login")
    redirect_uri = request.host_url.rstrip("/") + "/auth/google/callback"
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def auth_google_callback():
    if not (google_client_id and google_client_secret):
        flash("Google OAuth is not configured.", "warning")
        return redirect("/login")

    try:
        token = oauth.google.authorize_access_token()
        userinfo = token.get("userinfo") or oauth.google.get("https://openidconnect.googleapis.com/v1/userinfo").json()
        email = (userinfo.get("email") or "").strip().lower()
        name = (userinfo.get("name") or "Google User").strip()
        google_sub = userinfo.get("sub", "")
        if not email:
            flash("Google login failed: email not available.", "danger")
            return redirect("/login")

        db = get_db()
        if db is None:
            flash("Database connection error.", "danger")
            return redirect("/login")

        user = db.users.find_one({"email": email})
        if user and user.get("is_blocked"):
            flash("Your account is blocked. Please contact support.", "danger")
            return redirect("/login")

        if not user:
            db.users.insert_one({
                "name": name,
                "email": email,
                "password": None,
                "role": "USER",
                "otp": None,
                "is_blocked": False,
                "auth_provider": "google",
                "google_sub": google_sub,
                "created_at": datetime.now(),
            })
            user = db.users.find_one({"email": email})
        else:
            db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"name": name, "auth_provider": "google", "google_sub": google_sub}},
            )
            user = db.users.find_one({"_id": user["_id"]})

        session.clear()
        session["user_id"] = str(user["_id"])
        session["user_name"] = user.get("name", name)
        session["user_email"] = user["email"]

        # Send greeting email on Google login (non-blocking).
        try:
            login_url = request.host_url.rstrip("/") + "/login"
            send_welcome_email(mail, session["user_name"], session["user_email"], login_url)
        except Exception as mail_err:
            print(f"Google login greet email failed (non-critical): {mail_err}")

        return redirect("/dashboard")
    except OAuthError as e:
        print(f"Google OAuth error: type=OAuthError error={getattr(e, 'error', '')} description={getattr(e, 'description', '')}")
        flash("Google sign-in session expired or became invalid. Please try again.", "danger")
        return redirect("/login")
    except Exception as e:
        print("Google OAuth error:", repr(e))
        flash("Google sign-in failed. Please try again.", "danger")
        return redirect("/login")


# ================= DASHBOARD =================
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/login")

    db = get_db()
    tickets     = []
    my_bookings = []

    if db is not None:
        raw_tickets = list(db.tickets.find().sort("_id", DESCENDING))
        for t in raw_tickets:
            t["id"] = str(t["_id"])
            tickets.append(t)

        # Fetch user's bookings with ticket info
        user_id = session["user_id"]
        raw_bookings = list(db.bookings.find({"user_id": user_id}).sort("booking_date", DESCENDING))
        for b in raw_bookings:
            b["id"] = str(b["_id"])
            saved_title = b.get("title", "")
            saved_type = b.get("type", "")
            saved_source = b.get("source", "")
            saved_destination = b.get("destination", "")
            # Attach ticket details
            try:
                t = db.tickets.find_one({"_id": ObjectId(b["ticket_id"])})
            except Exception:
                t = None
            if t:
                b["title"]       = saved_title or t.get("title", "")
                b["type"]        = saved_type or t.get("type", "")
                b["source"]      = saved_source or t.get("source", "")
                b["destination"] = saved_destination or t.get("destination", "")
            else:
                b["title"]       = saved_title or "N/A"
                b["type"]        = saved_type or "N/A"
                b["source"]      = saved_source or "N/A"
                b["destination"] = saved_destination or "N/A"
            b["context_label"] = b.get("context_label") or _ticket_context(b)[0]
            b["context_value"] = b.get("context_value") or _ticket_context(b)[1]
            my_bookings.append(b)

    return render_template("user/dashboard.html", tickets=tickets, my_bookings=my_bookings)


# ================= LOGOUT =================
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")          # ← home page after logout


# ================= FORGOT PASSWORD =================
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email")
        db = get_db()
        if db is None:
            flash("Database connection error", "danger")
            return redirect("/forgot-password")

        user = db.users.find_one({"email": email})
        if not user:
            flash("Email not registered", "danger")
            return redirect("/forgot-password")

        otp    = generate_otp()
        expiry = datetime.now() + timedelta(minutes=10)
        db.users.update_one({"email": email}, {"$set": {"otp": otp}})

        try:
            msg = Message(
                "TicketHub Password Reset OTP",
                recipients=[email],
                body=f"Your OTP is {otp}. It expires in 10 minutes."
            )
            mail.send(msg)
        except Exception as e:
            print("Mail error:", e)

        session["reset_email"] = email
        session["reset_otp"]   = otp
        session["otp_expiry"]  = str(expiry)
        flash("OTP sent to your email", "success")
        return redirect("/verify-otp")

    return render_template("user/forgot_password.html")


# ================= VERIFY OTP =================
@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    if request.method == "POST":
        otp        = request.form.get("otp")
        stored_otp = session.get("reset_otp")
        expiry_str = session.get("otp_expiry")

        if not stored_otp:
            flash("Session expired", "danger")
            return redirect("/forgot-password")

        try:
            expiry = datetime.fromisoformat(expiry_str)
            if datetime.now() > expiry:
                flash("OTP expired", "danger")
                return redirect("/forgot-password")
        except Exception:
            flash("Session error", "danger")
            return redirect("/forgot-password")

        if otp != stored_otp:
            flash("Invalid OTP", "danger")
            return redirect("/verify-otp")

        session["otp_verified"] = True
        return redirect("/reset-password")

    return render_template("user/verify_otp.html")


# ================= RESET PASSWORD =================
@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    if not session.get("otp_verified"):
        return redirect("/forgot-password")

    if request.method == "POST":
        password = request.form.get("password")
        confirm  = request.form.get("confirm")

        if password != confirm:
            flash("Passwords do not match", "danger")
            return redirect("/reset-password")

        email  = session.get("reset_email")
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        db = get_db()
        if db is not None:
            db.users.update_one({"email": email}, {"$set": {"password": hashed, "otp": None}})

        for k in ["reset_email", "reset_otp", "otp_expiry", "otp_verified"]:
            session.pop(k, None)
        flash("Password reset successful", "success")
        return redirect("/login")

    return render_template("user/reset_password.html")


# ================= SEAT SELECTION =================
@app.route("/seat-selection/<ticket_id>")
def seat_selection(ticket_id):
    if "user_id" not in session:
        return redirect("/login")

    db = get_db()
    ticket       = None
    booked_seats = []

    if db is not None:
        try:
            ticket = db.tickets.find_one({"_id": ObjectId(ticket_id)})
        except Exception:
            ticket = None

        if ticket:
            ticket["id"] = str(ticket["_id"])
            ticket["travel_date_label"] = _format_travel_date(ticket.get("travel_date", ""))
            travel_date = ticket.get("travel_date", "")
            booking_query = {"ticket_id": ticket_id}
            if travel_date:
                booking_query["travel_date"] = travel_date
            raw_booked = list(db.bookings.find(booking_query, {"seat_no": 1}))
            booked_seats = [b["seat_no"] for b in raw_booked if b.get("seat_no")]

    if not ticket:
        flash("Ticket not found", "danger")
        return redirect("/dashboard")

    # If user arrived from a live-search result card, preserve that clicked price.
    live_price = _safe_float(request.args.get("live_price"), 0)
    live_final_price = _safe_float(request.args.get("live_final_price"), 0)
    live_discount = int(_safe_float(request.args.get("live_discount"), -1))

    if live_final_price > 0:
        if live_price > 0 and 0 <= live_discount <= 80:
            ticket["price"] = float(live_price)
            ticket["discount"] = int(live_discount)
        else:
            ticket["price"] = float(live_final_price)
            ticket["discount"] = 0
    elif live_price > 0:
        ticket["price"] = float(live_price)
        ticket["discount"] = int(live_discount) if 0 <= live_discount <= 80 else 0

    session["ticket_id"] = ticket_id
    session["ticket_info"] = {
        "title":       ticket["title"],
        "type":        ticket["type"],
        "source":      ticket["source"],
        "destination": ticket["destination"],
        "price":       float(ticket["price"]),
        "discount":    ticket["discount"],
        "travel_date": ticket.get("travel_date", ""),
        "travel_date_label": _format_travel_date(ticket.get("travel_date", "")),
    }
    return render_template("user/seat_selection.html", ticket=ticket, booked_seats=booked_seats)


@app.route("/select-seat", methods=["POST"])
def select_seat():
    session["selected_seat"] = request.form.get("seat_number")
    return redirect("/payment")


# ================= PAYMENT =================
@app.route("/payment")
def payment():
    if "user_id" not in session:
        return redirect("/login")
    ticket_info = session.get("ticket_info", {})
    seat        = session.get("selected_seat", "N/A")
    price       = ticket_info.get("price", 0)
    discount    = ticket_info.get("discount", 0)
    final_price = round(price - (price * discount / 100), 2)
    return render_template("user/payment.html", seat_number=seat, ticket_info=ticket_info, final_price=final_price)


@app.route("/process-payment", methods=["POST"])
def process_payment():
    session["payment_status"] = "PAID"
    return redirect("/booking-success")


# ================= BOOKING SUCCESS =================
@app.route("/booking-success")
def booking_success():
    if "user_id" not in session:
        return redirect("/login")

    booking_id  = str(uuid.uuid4())[:8].upper()
    ticket_info = session.get("ticket_info", {})
    seat        = session.get("selected_seat", "N/A")
    price       = ticket_info.get("price", 800)
    discount    = ticket_info.get("discount", 0)
    final_price = round(price - (price * discount / 100), 2)

    ticket_title = ticket_info.get('title', ticket_info.get('type', 'Ticket'))
    context_label, context_value = _ticket_context(ticket_info)
    data = {
        "booking_id":     booking_id,
        "user_id":        session["user_id"],
        "route":          context_value,
        "context_label":  context_label,
        "context_value":  context_value,
        "ticket_type":    f"{ticket_info.get('type','')} - {ticket_title}",
        "seat":           seat,
        "amount":         final_price,
        "payment_status": "PAID",
        # Enriched fields for PDF accuracy
        "source":         ticket_info.get("source", "N/A"),
        "destination":    ticket_info.get("destination", "N/A"),
        "title":          ticket_title,
        "type":           ticket_info.get("type", ""),
        "base_price":     price,
        "discount":       discount,
        "final_price":    final_price,
        "travel_date":    ticket_info.get("travel_date_label") or _format_travel_date(ticket_info.get("travel_date", "")),
        "travel_date_key": ticket_info.get("travel_date", ""),
        "ticket_id":      session.get("ticket_id", ""),
    }

    save_booking_to_db(data)
    session["booking_id"]   = booking_id
    session["booking_data"] = data

    # ── Send confirmation email (via email_service module) ──
    try:
        user_email = session.get("user_email", "")
        user_name  = session.get("user_name", "Valued Customer")
        if user_email:
            pdf_buffer = generate_ticket_pdf({**data, "user_name": user_name})
            send_booking_confirmation(mail, user_name, user_email, data, pdf_buffer)
    except Exception as e:
        print(f"Email send error (non-critical): {e}")

    return render_template("user/booking_success.html", **data)


# ================= DOWNLOAD TICKET =================
@app.route("/download-ticket")
def download_ticket():
    if "user_id" not in session:
        return redirect("/login")
    booking_data = session.get("booking_data", {})
    if not booking_data:
        flash("No booking found", "danger")
        return redirect("/dashboard")
    booking_data_with_name = {
        **booking_data,
        "user_name": session.get("user_name", booking_data.get("user_name", "Valued Customer"))
    }
    pdf = generate_ticket_pdf(booking_data_with_name)
    return send_file(pdf, as_attachment=True, download_name=f"ticket_{booking_data.get('booking_id','ticket')}.pdf")


@app.route("/download-ticket/<booking_id>")
def download_saved_ticket(booking_id):
    if "user_id" not in session:
        return redirect("/login")

    db = get_db()
    if db is None:
        flash("Database unavailable", "danger")
        return redirect("/dashboard#my-bookings")

    try:
        booking = db.bookings.find_one({"_id": ObjectId(booking_id), "user_id": session["user_id"]})
    except Exception:
        booking = None

    if not booking:
        flash("Booking not found", "danger")
        return redirect("/dashboard#my-bookings")

    ticket = None
    try:
        if booking.get("ticket_id"):
            ticket = db.tickets.find_one({"_id": ObjectId(booking["ticket_id"])})
    except Exception:
        ticket = None

    pdf_data = {
        **(ticket or {}),
        **booking,
        "booking_id": booking.get("payment_id", str(booking["_id"])),
        "seat": booking.get("seat_no", "N/A"),
        "amount": booking.get("final_price", 0),
        "user_name": session.get("user_name", "Valued Customer"),
    }
    pdf_data.pop("_id", None)
    pdf = generate_ticket_pdf(pdf_data)
    return send_file(pdf, as_attachment=True, download_name=f"ticket_{pdf_data.get('booking_id','ticket')}.pdf")


# ================= AI CHAT TEST =================
# ================= AI CHAT TEST =================
@app.route("/api/chat/test")
def chat_test():
    try:
        reply = ai_call([{"role": "user", "content": "Say hello in one sentence."}], max_tokens=60)
        return jsonify({"status": "ok", "model": app.config.get("GROQ_MODEL"), "reply": reply})
    except Exception as e:
        return jsonify({"status": "error", "model": app.config.get("GROQ_MODEL"), "error": str(e)}), 200


# ================= AI CHATBOT API =================
@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json() or {}
    user_messages = data.get("messages")
    if not user_messages and data.get("message"):
        user_messages = [{"role": "user", "content": data.get("message", "")}]
    if not user_messages:
        return jsonify({"error": "Invalid request"}), 400
    is_logged_in  = "user_id" in session
    user_name     = session.get("user_name", "")

    last_user_msg = ""
    last_user_msg_raw = ""
    for m in reversed(user_messages):
        if m.get("role") == "user":
            last_user_msg_raw = m.get("content", "")
            last_user_msg = last_user_msg_raw.lower()
            break

    understood = _ai_understand_user_query(last_user_msg_raw, is_logged_in=is_logged_in)
    action = understood.get("action")

    if action and not is_logged_in:
        reply = "You need to be logged in to do that! Please login or sign up first."
        return jsonify({
            "reply":   reply,
            "action":  "require_login",
            "buttons": [
                {"label": "🔐 Login",   "url": "/login"},
                {"label": "📝 Sign Up", "url": "/signup"},
            ]
        })

    intent = understood.get("intent") or _extract_travel_intent(last_user_msg_raw)

    if action == "book" and is_logged_in:
        if intent:
            reply = (
                f"Perfect {user_name}! I understood your route. "
                "Use the button below to open Live Search with auto-filled details."
            )
            buttons = []
            live_btn = _live_search_button(intent)
            if live_btn:
                buttons.append(live_btn)
            buttons.append({"label": "Go to Dashboard", "url": "/dashboard"})
            save_chat_history(last_user_msg, reply, source="home_ticketbot", action=action)
            return jsonify({
                "reply": reply,
                "action": "show_options",
                "buttons": buttons
            })

        reply = f"Great, {user_name}! What type of ticket would you like to book?"
        save_chat_history(last_user_msg, reply, source="home_ticketbot", action=action)
        return jsonify({
            "reply":   reply,
            "action":  "show_options",
            "buttons": [
                {"label": "🚌 Bus",          "url": "/dashboard"},
                {"label": "🚂 Train",        "url": "/dashboard"},
                {"label": "✈️ Flight",       "url": "/dashboard"},
                {"label": "🏨 Hotel",        "url": "/dashboard"},
                {"label": "🎵 Concert/Event","url": "/dashboard"},
            ]
        })

    if action == "my_bookings" and is_logged_in:
        reply = f"Sure {user_name}! Here's a quick link to view your bookings."
        save_chat_history(last_user_msg, reply, source="home_ticketbot", action=action)
        return jsonify({
            "reply":   reply,
            "action":  "show_options",
            "buttons": [{"label": "📋 View My Bookings", "url": "/dashboard#my-bookings"}]
        })

    if action == "dashboard" and is_logged_in:
        reply = "Head to your dashboard to search and browse all available tickets!"
        save_chat_history(last_user_msg, reply, source="home_ticketbot", action=action)
        return jsonify({
            "reply":   reply,
            "action":  "show_options",
            "buttons": [{"label": "🏠 Go to Dashboard", "url": "/dashboard"}]
        })

    if action == "logout" and is_logged_in:
        reply = "You can logout using the button below."
        save_chat_history(last_user_msg, reply, source="home_ticketbot", action=action)
        return jsonify({
            "reply":   reply,
            "action":  "show_options",
            "buttons": [{"label": "🚪 Logout", "url": "/logout"}]
        })

    login_context = ""
    if is_logged_in:
        login_context = f"\n\nThe user is currently logged in as '{user_name}'. Help them book or answer questions."
    else:
        login_context = "\n\nThe user is NOT logged in. If they want to book or do anything requiring an account, tell them to login or sign up."

    messages = [{"role": "system", "content": SYSTEM_PROMPT + login_context}]
    messages.extend(user_messages)

    try:
        reply = ai_call(messages, max_tokens=512, temperature=0.20)
        save_chat_history(last_user_msg, reply, source="home_ticketbot", action=action)
        payload = {"reply": reply}
        if is_logged_in and intent:
            live_btn = _live_search_button(intent)
            if live_btn:
                payload["buttons"] = [live_btn]
        return jsonify(payload)
    except Exception as e:
        error_msg = str(e)
        print("AI API error:", error_msg)
        fallback = _local_ai_fallback(last_user_msg, is_logged_in=is_logged_in)
        save_chat_history(last_user_msg, fallback, source="home_ticketbot", action=action)
        payload = {"reply": fallback}
        if is_logged_in and intent:
            live_btn = _live_search_button(intent)
            if live_btn:
                payload["buttons"] = [live_btn]
        return jsonify(payload), 200


# ================= ADMIN SETUP =================
@app.route("/admin/setup", methods=["GET", "POST"])
def admin_setup():
    message = None
    if request.method == "POST":
        name     = request.form.get("name", "Admin").strip()
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        if not email or not password:
            message = ("danger", "Email and password are required.")
        else:
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            db = get_db()
            if not db:
                message = ("danger", "Database connection failed.")
            else:
                try:
                    existing = db.users.find_one({"email": email})
                    if existing:
                        db.users.update_one(
                            {"email": email},
                            {"$set": {"password": hashed, "role": "ADMIN", "name": name}}
                        )
                    else:
                        db.users.insert_one({
                            "name":       name,
                            "email":      email,
                            "password":   hashed,
                            "role":       "ADMIN",
                            "otp":        None,
                            "is_blocked": False,
                            "created_at": datetime.now(),
                        })
                    message = ("success", f"Admin account ready! You can now login with: {email}")
                except Exception as e:
                    message = ("danger", f"Error: {str(e)}")

    return render_template("admin/admin_setup.html", message=message)


# ================= ADMIN LOGIN REDIRECT =================
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    return redirect("/login")


# ================= ADMIN DASHBOARD =================
@app.route("/admin/dashboard")
def admin_dashboard():
    if "admin_id" not in session:
        return redirect("/admin/login")

    db      = get_db()
    users   = []
    tickets = []
    stats   = {"total_users": 0, "total_tickets": 0, "active_users": 0, "blocked_users": 0}

    if db is not None:
        try:
            raw_users = list(db.users.find({"role": "USER"}).sort("_id", DESCENDING))
            for u in raw_users:
                u["id"]           = str(u["_id"])
                u["ticket_count"] = db.bookings.count_documents({"user_id": u["id"]})
                if "created_at" not in u:
                    u["created_at"] = None
                users.append(u)

            raw_tickets = list(db.tickets.find().sort("_id", DESCENDING))
            for t in raw_tickets:
                t["id"] = str(t["_id"])
                tickets.append(t)

            stats["total_users"]   = db.users.count_documents({"role": "USER"})
            stats["total_tickets"] = db.tickets.count_documents({})
            stats["active_users"]  = db.users.count_documents({"role": "USER", "is_blocked": {"$ne": True}})
            stats["blocked_users"] = db.users.count_documents({"is_blocked": True})
        except Exception as e:
            print("Admin dashboard error:", e)

    return render_template("admin/admin_dashboard.html", users=users, tickets=tickets, stats=stats)


# ================= ADMIN ADD TICKET =================
@app.route("/admin/add-ticket", methods=["GET", "POST"])
def add_ticket():
    if "admin_id" not in session:
        return redirect("/admin/login")
    if request.method == "POST":
        travel_date = request.form.get("travel_date", "").strip()
        if not _valid_future_date(travel_date):
            flash("Please choose a valid date from today onward.", "warning")
            return redirect("/admin/add-ticket")
        db = get_db()
        if db is not None:
            db.tickets.insert_one({
                "type":        request.form["type"],
                "title":       request.form["title"],
                "source":      request.form["source"],
                "destination": request.form["destination"],
                "travel_date": travel_date,
                "price":       float(request.form["price"]),
                "discount":    int(request.form.get("discount", 0)),
                "total_seats": int(request.form.get("total_seats", 40)),
                "created_at":  datetime.now(),
            })
        flash("Ticket added successfully", "success")
        return redirect("/admin/dashboard")
    return render_template("admin/add_ticket.html")


# ================= ADMIN EDIT TICKET =================
@app.route("/admin/edit-ticket/<ticket_id>", methods=["GET", "POST"])
def edit_ticket(ticket_id):
    if "admin_id" not in session:
        return redirect("/admin/login")
    db     = get_db()
    ticket = None
    if db is not None:
        if request.method == "POST":
            travel_date = request.form.get("travel_date", "").strip()
            if not _valid_future_date(travel_date):
                flash("Please choose a valid date from today onward.", "warning")
                return redirect(f"/admin/edit-ticket/{ticket_id}")
            try:
                db.tickets.update_one(
                    {"_id": ObjectId(ticket_id)},
                    {"$set": {
                        "type":        request.form["type"],
                        "title":       request.form["title"],
                        "source":      request.form["source"],
                        "destination": request.form["destination"],
                        "travel_date": travel_date,
                        "price":       float(request.form["price"]),
                        "discount":    int(request.form.get("discount", 0)),
                        "total_seats": int(request.form.get("total_seats", 40)),
                    }}
                )
            except Exception as e:
                print("Edit ticket error:", e)
            flash("Ticket updated", "success")
            return redirect("/admin/dashboard")
        try:
            ticket = db.tickets.find_one({"_id": ObjectId(ticket_id)})
            if ticket:
                ticket["id"] = str(ticket["_id"])
        except Exception:
            ticket = None
    return render_template("admin/edit_ticket.html", ticket=ticket)


# ================= ADMIN DELETE TICKET =================
@app.route("/admin/delete-ticket/<ticket_id>")
def delete_ticket(ticket_id):
    if "admin_id" not in session:
        return redirect("/admin/login")
    db = get_db()
    if db is not None:
        try:
            db.tickets.delete_one({"_id": ObjectId(ticket_id)})
        except Exception as e:
            print("Delete ticket error:", e)
    flash("Ticket deleted", "success")
    return redirect("/admin/dashboard")


# ================= ADMIN BLOCK / UNBLOCK =================
@app.route("/admin/block-user/<user_id>")
def block_user(user_id):
    if "admin_id" not in session:
        return redirect("/admin/login")
    db = get_db()
    if db is not None:
        try:
            db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"is_blocked": True}})
        except Exception as e:
            print("Block user error:", e)
    flash("User blocked", "warning")
    return redirect("/admin/dashboard")


@app.route("/admin/unblock-user/<user_id>")
def unblock_user(user_id):
    if "admin_id" not in session:
        return redirect("/admin/login")
    db = get_db()
    if db is not None:
        try:
            db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"is_blocked": False}})
        except Exception as e:
            print("Unblock user error:", e)
    flash("User unblocked", "success")
    return redirect("/admin/dashboard")


# ================= ADMIN LOGOUT =================
@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/")          # ← home page after logout


# ================= TICKETS BY TYPE API =================
@app.route("/api/tickets-by-type")
def tickets_by_type():
    """
    Return TicketHub tickets matching a type (and optionally source/destination).
    Used by the search page 'Book on TicketHub' button to show real bookable tickets.
    """
    db = get_db()
    if db is None:
        return jsonify({"tickets": []})

    ticket_type = _canonical_ticket_type(request.args.get("type", ""))
    source      = request.args.get("source", "").strip().lower()
    destination = request.args.get("destination", "").strip().lower()
    travel_date = request.args.get("date", "").strip()
    strict      = request.args.get("strict", "0").strip().lower() in ("1", "true", "yes")

    query = {}
    if ticket_type:
        query["type"] = ticket_type
    if travel_date:
        query["travel_date"] = travel_date

    raw = list(db.tickets.find(query).sort("_id", DESCENDING))
    # CONCERT and HOTEL tickets don't have meaningful source/destination routes
    # so skip location filtering for these types to always show available options
    SKIP_LOCATION_FILTER = {"CONCERT", "HOTEL"}
    tickets = []
    for t in raw:
        # Soft-match source/destination if provided (skip for CONCERT/HOTEL)
        t_src  = t.get("source", "").lower()
        t_dest = t.get("destination", "").lower()
        if ticket_type not in SKIP_LOCATION_FILTER:
            if source and destination:
                # Include if either city matches any field
                if not (source in t_src or source in t_dest or
                        destination in t_src or destination in t_dest):
                    continue
            elif source:
                if not (source in t_src or source in t_dest):
                    continue
            elif destination:
                if not (destination in t_src or destination in t_dest):
                    continue

        discount = t.get("discount", 0)
        price    = float(t.get("price", 0))
        final    = round(price * (1 - discount / 100))
        payload = {
            "id":          str(t["_id"]),
            "title":       t.get("title", ""),
            "type":        t.get("type", ""),
            "source":      t.get("source", ""),
            "destination": t.get("destination", ""),
            "travel_date": t.get("travel_date", ""),
            "price":       price,
            "discount":    discount,
            "final_price": final,
            "total_seats": t.get("total_seats", 0),
        }
        if _is_price_visible(payload.get("price"), payload.get("final_price")):
            tickets.append(payload)

    # If no match found with filters, return all tickets of that type unless strict mode is requested.
    if not tickets and ticket_type and not strict:
        fallback_query = {"type": ticket_type}
        if travel_date:
            fallback_query["travel_date"] = travel_date
        raw2 = list(db.tickets.find(fallback_query).sort("_id", DESCENDING))
        for t in raw2:
            discount = t.get("discount", 0)
            price    = float(t.get("price", 0))
            final    = round(price * (1 - discount / 100))
            payload = {
                "id":          str(t["_id"]),
                "title":       t.get("title", ""),
                "type":        t.get("type", ""),
                "source":      t.get("source", ""),
                "destination": t.get("destination", ""),
                "travel_date": t.get("travel_date", ""),
                "price":       price,
                "discount":    discount,
                "final_price": final,
                "total_seats": t.get("total_seats", 0),
            }
            if _is_price_visible(payload.get("price"), payload.get("final_price")):
                tickets.append(payload)

    return jsonify({"tickets": tickets})


@app.route("/start-booking")
def start_booking():
    """
    Start direct booking flow from search cards:
    Search Result Click -> Seat Selection -> Payment.
    """
    if "user_id" not in session:
        # Preserve the user's current booking intent so login can continue booking directly.
        next_url = request.full_path.rstrip("?")
        return redirect(f"/login?next={quote(next_url)}")

    db = get_db()
    if db is None:
        flash("Database unavailable. Please try again.", "danger")
        return redirect("/search")

    ticket_type = _canonical_ticket_type(request.args.get("type", ""))
    source = request.args.get("source", "").strip().lower()
    destination = request.args.get("destination", "").strip().lower()
    travel_date = request.args.get("date", "").strip()
    title = request.args.get("title", "").strip()
    live_price = _safe_float(request.args.get("price"), 0)
    live_final_price = _safe_float(request.args.get("final_price"), 0)
    live_discount = int(_safe_float(request.args.get("discount"), 0))
    if not _is_price_visible(live_price or None, live_final_price or None):
        flash("Tickets below ₹500 are not available for booking.", "warning")
        return redirect("/search")

    def _seat_selection_redirect(ticket_id):
        params = {}
        if live_price > 0:
            params["live_price"] = str(live_price)
        if live_final_price > 0:
            params["live_final_price"] = str(live_final_price)
        if 0 <= live_discount <= 80:
            params["live_discount"] = str(live_discount)
        if params:
            return redirect(f"/seat-selection/{ticket_id}?{urlencode(params)}")
        return redirect(f"/seat-selection/{ticket_id}")

    if not _valid_future_date(travel_date):
        flash("Please select a valid date from today onward.", "warning")
        return redirect("/search")

    query = {}
    if ticket_type:
        query["type"] = ticket_type
    query["travel_date"] = travel_date

    raw = list(db.tickets.find(query))
    if not raw:
        price = live_price or live_final_price or _default_live_price(ticket_type)

        ticket_doc = {
            "type": ticket_type,
            "title": title or f"{ticket_type.title()} Ticket",
            "source": source.title() if source else "N/A",
            "destination": destination.title() if destination else "N/A",
            "travel_date": travel_date,
            "price": float(price),
            "discount": live_discount if 0 <= live_discount <= 80 else 0,
            "total_seats": _default_total_seats(ticket_type),
            "created_at": datetime.now(),
            "source_provider": "SERPER",
        }
        inserted = db.tickets.insert_one(ticket_doc)
        return _seat_selection_redirect(str(inserted.inserted_id))

    # Keep route filters for transport tickets, skip route matching for HOTEL/CONCERT.
    skip_location_filter = {"CONCERT", "HOTEL"}
    candidates = []
    for t in raw:
        t_src = str(t.get("source", "")).lower()
        t_dst = str(t.get("destination", "")).lower()
        if ticket_type not in skip_location_filter and (source or destination):
            if source and destination:
                if not ((source in t_src and destination in t_dst) or (source in t_dst and destination in t_src)):
                    continue
            elif source:
                if source not in t_src and source not in t_dst:
                    continue
            elif destination:
                if destination not in t_src and destination not in t_dst:
                    continue
        candidates.append(t)

    # Fallback to same type if strict route match found nothing.
    if not candidates:
        candidates = raw

    def _final_price(ticket):
        price = float(ticket.get("price", 0))
        discount = float(ticket.get("discount", 0))
        return round(price * (1 - discount / 100), 2)

    visible_candidates = [t for t in candidates if _is_price_visible(t.get("price"), _final_price(t))]
    if not visible_candidates:
        flash("No tickets found at or above ₹500 for this search.", "warning")
        return redirect("/search")

    best = sorted(visible_candidates, key=_final_price)[0]
    return _seat_selection_redirect(str(best["_id"]))


# ================= AI: BEST PLAN COMPARISON =================
@app.route("/ai/best-plan", methods=["GET", "POST"])
def ai_best_plan():
    """
    GET  /ai/best-plan                       — rank all tickets with default prefs
    POST /ai/best-plan  {budget, interests, destination}  — personalised ranking
    """
    db = get_db()
    if db is None:
        return jsonify({"error": "Database unavailable"}), 503

    # Parse preferences
    source = ""
    if request.method == "POST":
        body = request.get_json() or {}
        budget = _safe_float(body.get("budget", 5000), 5000)
        interests = body.get("interests", ["BUS", "TRAIN", "FLIGHT", "HOTEL", "CONCERT"])
        destination = body.get("destination", "")
        source = body.get("source", "")
    else:
        budget = _safe_float(request.args.get("budget", 5000), 5000)
        interests = request.args.get("interests", "BUS,TRAIN,FLIGHT").split(",")
        destination = request.args.get("destination", "")
        source = request.args.get("source", "")

    if not isinstance(interests, list):
        interests = ["BUS", "TRAIN", "FLIGHT"]

    interests = [str(i).upper().strip() for i in interests if str(i).strip()]
    prefs = {
        "budget": budget,
        "interests": interests,
        "destination": destination,
        "source": source,
    }

    # Load tickets from DB
    raw_tickets = list(db.tickets.find())
    tickets = [{**t, "id": str(t["_id"])} for t in raw_tickets]

    # Strictly filter candidates by selected ticket type(s) and route context for better recommendations.
    if interests:
        tickets = [t for t in tickets if str(t.get("type", "")).upper() in interests]

    src_q = source.strip().lower()
    dst_q = destination.strip().lower()
    if src_q or dst_q:
        route_matched = []
        for t in tickets:
            t_src = str(t.get("source", "")).lower()
            t_dst = str(t.get("destination", "")).lower()
            if src_q and dst_q:
                # Allow either direction for practical matching.
                if (src_q in t_src and dst_q in t_dst) or (src_q in t_dst and dst_q in t_src):
                    route_matched.append(t)
            elif src_q:
                if src_q in t_src or src_q in t_dst:
                    route_matched.append(t)
            elif dst_q:
                if dst_q in t_src or dst_q in t_dst:
                    route_matched.append(t)
        if route_matched:
            tickets = route_matched

    filtered_tickets = []
    for t in tickets:
        p = _safe_float(t.get("price"), 0)
        d = _safe_float(t.get("discount"), 0)
        fp = _safe_float(t.get("final_price"), round(p * (1 - d / 100), 2))
        if _is_price_visible(p, fp):
            filtered_tickets.append(t)

    result = get_best_plan(filtered_tickets, prefs)
    return jsonify(result)


@app.route("/ai/best-plan-from-results", methods=["POST"])
def ai_best_plan_from_results():
    """
    Score best plan using caller-provided live search results.
    Keeps AI recommendation aligned with exactly what user just fetched.
    """
    body = request.get_json() or {}
    raw_candidates = body.get("candidates", []) or []
    prefs = body.get("prefs", {}) or {}
    prefer_best_price = bool(body.get("prefer_best_price", True))

    candidates = []
    for i, c in enumerate(raw_candidates):
        try:
            price = _safe_float(c.get("price"), 0)
            discount = _safe_float(c.get("discount"), 0)
            final_price = _safe_float(c.get("final_price"), round(price * (1 - discount / 100), 2))
            if final_price <= 0:
                continue
            if not _is_price_visible(price, final_price):
                continue
            candidates.append({
                "id": c.get("id", f"live-{i+1}"),
                "title": c.get("title", "Live option"),
                "type": c.get("type", ""),
                "source": c.get("source", ""),
                "destination": c.get("destination", ""),
                "price": price,
                "discount": discount,
                "final_price": final_price,
                "total_seats": int(_safe_float(c.get("total_seats"), 0)),
            })
        except Exception:
            continue

    if not candidates:
        return jsonify({"best_plan": None, "ai_reason": "No priced live candidates available.", "ranked": [], "prefs_used": prefs})

    if prefer_best_price:
        # Force cheapest-first ranking for explicit "best price" intent.
        ranked = sorted(candidates, key=lambda x: (x.get("final_price", 0), -x.get("discount", 0)))
        best = ranked[0]
        best = {**best, "score": 100.0}
        ai_reason = (
            f"This is the lowest available final price in your live search results: "
            f"INR {best.get('final_price', 0):,.0f}. It gives the strongest immediate value."
        )
        payload_ranked = []
        for i, t in enumerate(ranked[:5], 1):
            payload_ranked.append({
                "rank": i,
                "id": str(t.get("id", "")),
                "title": t.get("title", ""),
                "type": t.get("type", ""),
                "source": t.get("source", ""),
                "destination": t.get("destination", ""),
                "price": float(t.get("price", 0) or 0),
                "discount": float(t.get("discount", 0) or 0),
                "final_price": float(t.get("final_price", 0) or 0),
                "total_seats": int(t.get("total_seats", 0) or 0),
                "score": 100.0 - (i - 1),
            })
        return jsonify({"best_plan": best, "ai_reason": ai_reason, "ranked": payload_ranked, "prefs_used": prefs})

    result = get_best_plan(candidates, prefs)
    return jsonify(result)


def _extract_price_from_text(text: str):
    """Try extracting an INR-like price from arbitrary text."""
    if not text:
        return None
    t = str(text).lower().replace(",", "")
    patterns = [
        r"(?:₹|rs\.?|inr)\s*([0-9]{3,6})",
        r"([0-9]{3,6})\s*(?:₹|rs\.?|inr)",
    ]
    for p in patterns:
        m = re.search(p, t, flags=re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                if 100 <= val <= 200000:
                    return val
            except Exception:
                pass
    return None


def _extract_discount_from_text(text: str):
    if not text:
        return None
    m = re.search(r"(\d{1,2})\s*%\s*(?:off|discount)?", str(text).lower())
    if not m:
        return None
    try:
        d = float(m.group(1))
        if 0 <= d <= 80:
            return d
    except Exception:
        pass
    return None


def _extract_seats_from_text(text: str):
    if not text:
        return None
    t = str(text).lower()
    m = re.search(r"(\d{1,4})\s*(?:seats?|tickets?)", t)
    if not m:
        return None
    try:
        s = int(m.group(1))
        if 1 <= s <= 5000:
            return s
    except Exception:
        pass
    return None


def _dynamic_reference_from_db(db, ticket_type: str, source: str, destination: str) -> dict:
    """
    Build dynamic fallback profile from DB ticket distribution.
    """
    t_type = str(ticket_type).upper()
    query = {"type": t_type}
    raw = list(db.tickets.find(query)) if db is not None else []

    src_q = (source or "").strip().lower()
    dst_q = (destination or "").strip().lower()
    if raw and (src_q or dst_q):
        matched = []
        for t in raw:
            t_src = str(t.get("source", "")).lower()
            t_dst = str(t.get("destination", "")).lower()
            if src_q and dst_q:
                if (src_q in t_src and dst_q in t_dst) or (src_q in t_dst and dst_q in t_src):
                    matched.append(t)
            elif src_q and (src_q in t_src or src_q in t_dst):
                matched.append(t)
            elif dst_q and (dst_q in t_src or dst_q in t_dst):
                matched.append(t)
        if matched:
            raw = matched

    if not raw:
        return {"price_min": None, "price_max": None, "discount_avg": None, "seats_avg": None}

    finals = []
    discounts = []
    seats = []
    for t in raw:
        p = float(t.get("price", 0) or 0)
        d = float(t.get("discount", 0) or 0)
        if p > 0:
            finals.append(round(p * (1 - d / 100), 2))
        discounts.append(d)
        seats.append(int(t.get("total_seats", 0) or 0))

    finals = finals or []
    discounts = discounts or []
    seats = [s for s in seats if s > 0] or []

    return {
        "price_min": float(min(finals)) if finals else None,
        "price_max": float(max(finals)) if finals else None,
        "discount_avg": float(sum(discounts) / len(discounts)) if discounts else None,
        "seats_avg": int(sum(seats) / len(seats)) if seats else None,
    }


@app.route("/ai/serper-best-plan", methods=["POST"])
def ai_serper_best_plan():
    """
    One-shot flow for dashboard:
    1) Fetch Serper tickets once (max 6)
    2) Score best plan from the same 6 candidates
    3) Return best pick + all fetched tickets
    """
    body = request.get_json() or {}
    ticket_type = str(body.get("type", "BUS")).upper().strip()
    source = (body.get("source", "") or "").strip()
    destination = (body.get("destination", "") or "").strip()
    date = (body.get("date", "") or "").strip()
    keyword = (body.get("keyword", "") or "").strip()
    input_budget = body.get("budget", None)

    serper_results = unified_search(
        ticket_type=ticket_type,
        source=source,
        destination=destination,
        date=date,
        keyword=keyword,
    )[:6]

    db = get_db()
    ref = _dynamic_reference_from_db(db, ticket_type, source, destination)

    ranked_candidates = []
    inferred_final_prices = []
    for i, r in enumerate(serper_results):
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        merged_text = f"{title} {snippet}"

        raw_price = _extract_price_from_text(snippet) or _extract_price_from_text(title)
        disc = _extract_discount_from_text(merged_text)
        seats = _extract_seats_from_text(merged_text)

        if raw_price is None:
            # Keep pricing strictly live/dynamic; do not synthesize numeric prices.
            continue

        if disc is None:
            disc = float(ref["discount_avg"]) if ref.get("discount_avg") is not None else 0.0

        if seats is None:
            seats = int(ref["seats_avg"]) if ref.get("seats_avg") is not None else 0

        computed_final = round(float(raw_price) * (1 - float(disc) / 100.0), 2)
        if not _is_price_visible(raw_price, computed_final):
            continue
        inferred_final_prices.append(computed_final)
        ranked_candidates.append({
            "id": f"serper-{ticket_type.lower()}-{i+1}",
            "title": title or f"{ticket_type} option {i+1}",
            "type": ticket_type,
            "source": r.get("source", source),
            "destination": r.get("destination", destination),
            "price": float(raw_price),
            "discount": float(disc),
            "total_seats": int(seats),
            "snippet": snippet,
            "featured": bool(r.get("featured", False)),
            "icon": r.get("icon", ""),
            "color": r.get("color", ""),
        })

    if input_budget in (None, "", 0, "0"):
        if inferred_final_prices:
            budget = round(max(inferred_final_prices) * 1.2, 2)
        elif ref.get("price_max") is not None:
            budget = round(float(ref["price_max"]) * 1.2, 2)
        else:
            budget = 5000.0
    else:
        fallback_budget = round(float(ref["price_max"]) * 1.2, 2) if ref.get("price_max") is not None else 5000.0
        budget = _safe_float(input_budget, fallback_budget)

    prefs = {
        "budget": budget,
        "interests": [ticket_type],
        "destination": destination or source,
        "source": source,
    }
    result = get_best_plan(ranked_candidates, prefs)
    result["search_results"] = ranked_candidates
    result["search_query"] = {
        "type": ticket_type,
        "source": source,
        "destination": destination,
        "date": date,
        "keyword": keyword,
        "limit": 6,
    }
    result["dynamic_budget"] = budget
    return jsonify(result)


# ================= AI: TRENDING LOCATIONS =================
@app.route("/ai/trending-locations")
def ai_trending_locations():
    """GET /ai/trending-locations — returns Serper-powered trending tourist spots."""
    db = get_db()
    locations = fetch_trending_locations(db)
    return jsonify({"count": len(locations), "locations": locations})


@app.route("/ai/health/groq")
def ai_health_groq():
    """Quick diagnostic for Groq key/model validity."""
    result = groq_health_check()
    status = 200 if result.get("ok") else 503
    return jsonify(result), status


# ================= AI: RAG CHATBOT (enhanced) =================
@app.route("/api/chat/rag", methods=["POST"])
def chat_rag():
    """RAG-powered chatbot that retrieves website + ticket knowledge before answering."""
    data = request.get_json() or {}
    user_msg = data.get("message", "")
    if not user_msg and data.get("messages"):
        for m in reversed(data.get("messages", [])):
            if m.get("role") == "user" and m.get("content"):
                user_msg = m.get("content", "")
                break
    if not user_msg:
        return jsonify({"error": "Invalid request"}), 400
    is_logged_in = "user_id" in session
    user_name    = session.get("user_name", "")

    q = (user_msg or "").lower()
    booking_intent_keywords = [
        "show my ticket", "show my tickets", "my ticket", "my tickets",
        "my booking", "my bookings", "booking history", "booked ticket",
        "booked tickets", "view booking", "view my ticket",
    ]

    if is_logged_in and any(k in q for k in booking_intent_keywords):
        db = get_db()
        if db is None:
            return jsonify({"reply": "I could not access bookings right now. Please try again in a moment."})

        try:
            raw = list(
                db.bookings.find({"user_id": session["user_id"]}).sort("booking_date", DESCENDING).limit(5)
            )
        except Exception:
            raw = []

        if not raw:
            reply = (
                f"I cannot find any tickets currently booked under your account, {user_name}. "
                "Please visit your dashboard to view booking history or search for new tickets."
            )
            return jsonify({
                "reply": reply,
                "buttons": [
                    {"label": "View My Bookings", "url": "/dashboard#my-bookings"},
                    {"label": "Live Search", "url": "/search"},
                ],
            })

        lines = [f"Here are your latest booked tickets, {user_name}:"]
        for i, b in enumerate(raw, 1):
            b_type = str(b.get("type", "TICKET")).upper()
            b_title = b.get("title") or f"{b_type} Ticket"
            b_src = b.get("source", "N/A")
            b_dst = b.get("destination", "N/A")
            b_date = _format_travel_date(b.get("travel_date", ""))
            b_seat = b.get("seat_no", "N/A")
            b_amt = int(_safe_float(b.get("final_price", 0), 0))
            lines.append(
                f"{i}. {b_title} ({b_type}) | {b_src} -> {b_dst} | {b_date} | Seat {b_seat} | Rs.{b_amt}"
            )

        reply = "\n".join(lines)
        save_chat_history(user_msg, reply, source="dashboard_ticketbot", action="rag_bookings")
        return jsonify({
            "reply": reply,
            "buttons": [{"label": "View Full Booking History", "url": "/dashboard#my-bookings"}],
        })

    db = get_db()
    tickets = []
    if db is not None:
        raw = list(db.tickets.find())
        for t in raw:
            t["id"] = str(t["_id"])
            tickets.append(t)

    # Retrieve relevant knowledge (RAG)
    knowledge_context = rag_retrieve(user_msg, db, tickets)

    # Build enriched system prompt
    rag_system = SYSTEM_PROMPT
    if knowledge_context:
        rag_system += f"\n\n=== RETRIEVED KNOWLEDGE (use this to answer) ===\n{knowledge_context}"
    if is_logged_in:
        rag_system += f"\n\nThe user is logged in as '{user_name}'."
    else:
        rag_system += "\n\nThe user is NOT logged in. Suggest login/signup for booking."

    understood = _ai_understand_user_query(user_msg, is_logged_in=is_logged_in)
    intent = understood.get("intent") or _extract_travel_intent(user_msg)

    try:
        reply = ai_call(
            [{"role": "system", "content": rag_system},
             {"role": "user",   "content": user_msg}],
            max_tokens=512, temperature=0.20
        )
        save_chat_history(user_msg, reply, source="dashboard_ticketbot", action="rag")
        payload = {"reply": reply, "rag_used": bool(knowledge_context)}
        if is_logged_in and intent:
            live_btn = _live_search_button(intent)
            if live_btn:
                payload["buttons"] = [live_btn]
        return jsonify(payload)
    except Exception as e:
        print(f"RAG chat error: {e}")
        fallback = _local_ai_fallback(user_msg, is_logged_in=is_logged_in)
        save_chat_history(user_msg, fallback, source="dashboard_ticketbot", action="rag_fallback")
        payload = {"reply": fallback, "rag_used": bool(knowledge_context)}
        if is_logged_in and intent:
            live_btn = _live_search_button(intent)
            if live_btn:
                payload["buttons"] = [live_btn]
        return jsonify(payload), 200


@app.route("/api/chat/history")
def chat_history_api():
    """Return recent TicketBot history for the logged-in user."""
    if "user_id" not in session:
        return jsonify({"messages": []})

    db = get_db()
    if db is None:
        return jsonify({"messages": []})

    try:
        docs = list(
            db.chat_history.find({"user_id": session["user_id"]})
            .sort("created_at", DESCENDING)
            .limit(25)
        )
        docs.reverse()

        messages = []
        for d in docs:
            user_text = str(d.get("user_message") or "").strip()
            bot_text = str(d.get("bot_reply") or "").strip()
            if user_text:
                messages.append({"role": "user", "content": user_text})
            if bot_text:
                messages.append({"role": "assistant", "content": bot_text})
        return jsonify({"messages": messages})
    except Exception as e:
        print("Chat history load error:", e)
        return jsonify({"messages": []})


@app.route("/api/chat/history/clear", methods=["POST"])
def chat_history_clear_api():
    """Clear TicketBot chat history for the logged-in user."""
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "Not logged in"}), 401

    db = get_db()
    if db is None:
        return jsonify({"ok": False, "error": "Database unavailable"}), 503

    try:
        result = db.chat_history.delete_many({"user_id": session["user_id"]})
        return jsonify({"ok": True, "deleted": int(result.deleted_count or 0)})
    except Exception as e:
        print("Chat history clear error:", e)
        return jsonify({"ok": False, "error": "Failed to clear history"}), 500


# ================= SERPER LIVE SEARCH API =================
@app.route("/api/serper-search", methods=["GET", "POST"])
def serper_search_api():
    """
    Live search endpoint powered by Serper (Google Search).
    Accepts GET params or JSON POST body.
    Required: type (bus|flight|hotel|concert|train)
    Optional: source, destination, date, keyword
    """
    if request.method == "POST":
        body = request.get_json() or {}
        ticket_type  = body.get("type", "")
        source       = body.get("source", "").strip()
        destination  = body.get("destination", "").strip()
        date         = body.get("date", "").strip()
        keyword      = body.get("keyword", "").strip()
    else:
        ticket_type  = request.args.get("type", "")
        source       = request.args.get("source", "").strip()
        destination  = request.args.get("destination", "").strip()
        date         = request.args.get("date", "").strip()
        keyword      = request.args.get("keyword", "").strip()

    if not ticket_type:
        return jsonify({"error": "Missing required param: type"}), 400
    if not _valid_future_date(date):
        return jsonify({"error": "Please select a valid date from today onward."}), 400

    try:
        results = unified_search(
            ticket_type=ticket_type,
            source=source,
            destination=destination,
            date=date,
            keyword=keyword,
        )
        visible_results = []
        for r in results:
            p = _safe_float(r.get("price"), None)
            fp = _safe_float(r.get("final_price"), None)
            if _is_price_visible(p, fp):
                visible_results.append(r)
        results = visible_results
    except Exception as e:
        print(f"Serper search API error: {e}")
        return jsonify({
            "query": {
                "type": ticket_type,
                "source": source,
                "destination": destination,
                "date": date,
                "keyword": keyword,
            },
            "count": 0,
            "results": [],
            "error": "Live search temporarily unavailable",
        }), 502

    return jsonify({
        "query": {
            "type":        ticket_type,
            "source":      source,
            "destination": destination,
            "date":        date,
            "keyword":     keyword,
        },
        "count":   len(results),
        "results": results,
    })


# ================= SERPER SEARCH PAGE =================
@app.route("/search")
def search_page():
    """Full-page live search powered by Serper."""
    return render_template("user/search.html")



# ================= TRENDING LOCATIONS PAGE =================
@app.route("/ai/trending-page")
def trending_page():
    if "user_id" not in session:
        return redirect("/login")
    return render_template("user/trending.html")

# ================= 404 HANDLER =================
@app.errorhandler(404)
def page_not_found(e):
    return render_template("user/404.html"), 404


@app.errorhandler(500)
def server_error(e):
    print(f"Unhandled server error: {e}")
    return render_template("user/404.html"), 500


# ================= HOME CHATBOT (for non-logged-in users on home page) =================
@app.route("/api/chat/home", methods=["POST"])
def chat_home():
    """Lightweight chatbot for the home page (no session required)."""
    data = request.get_json() or {}
    if "message" not in data:
        return jsonify({"error": "Invalid request"}), 400

    user_msg = data["message"]
    is_logged_in = "user_id" in session
    try:
        reply = ai_call([
            {"role": "system", "content": SYSTEM_PROMPT + ("\n\nThe user is logged in." if is_logged_in else "\n\nThe user is browsing the TicketHub home page and is NOT logged in yet.")},
            {"role": "user",   "content": user_msg}
        ], max_tokens=400, temperature=0.20)
        save_chat_history(user_msg, reply, source="home_light_ticketbot", action="home_chat")
        return jsonify({"reply": reply})
    except Exception as e:
        print(f"Home chat error: {e}")
        fallback = _local_ai_fallback(user_msg, is_logged_in=is_logged_in)
        save_chat_history(user_msg, fallback, source="home_light_ticketbot", action="home_chat_fallback")
        return jsonify({"reply": fallback}), 200


# ================= RUN =================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=app.config.get("PORT", 5000), debug=app.config.get("DEBUG", False))



