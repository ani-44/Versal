"""
Dynamic Serper integration for TicketHub.
No hardcoded/mock price values are injected. Prices are extracted from live snippets only.
"""

import json
import logging
import os
import re
import time
from typing import Dict, List

import requests

logger = logging.getLogger(__name__)

SERPER_API_KEY = os.getenv("SERPER_API_KEY", "").strip()
SERPER_URL = "https://google.serper.dev/search"

TYPE_CONFIG = {
    "BUS": {"icon": "🚌", "color": "linear-gradient(135deg,#065f46,#10b981)", "required": ["bus", "volvo", "sleeper", "travels", "ticket"], "blocked": ["hotel", "room", "homestay"]},
    "TRAIN": {"icon": "🚆", "color": "linear-gradient(135deg,#1e40af,#3b82f6)", "required": ["train", "rail", "irctc", "railway", "express"], "blocked": ["hotel", "flight", "airfare", "bus"]},
    "FLIGHT": {"icon": "✈️", "color": "linear-gradient(135deg,#135f6d,#1a7a8a)", "required": ["flight", "airline", "airfare", "fare", "airport"], "blocked": ["hotel", "bus", "train"]},
    "HOTEL": {"icon": "🏨", "color": "linear-gradient(135deg,#92400e,#f59e0b)", "required": ["hotel", "stay", "resort", "room"], "blocked": []},
    "CONCERT": {"icon": "🎵", "color": "linear-gradient(135deg,#9d174d,#ec4899)", "required": ["concert", "event", "show", "tickets"], "blocked": []},
    "EVENT": {"icon": "🎫", "color": "linear-gradient(135deg,#9d174d,#ec4899)", "required": ["concert", "event", "show", "tickets"], "blocked": []},
}


def _extract_price(text: str):
    if not text:
        return None
    t = str(text).lower().replace(",", "")
    patterns = [
        r"(?:\u20b9|₹|rs\.?|inr)\s*([0-9]{2,7})",
        r"([0-9]{2,7})\s*(?:\u20b9|₹|rs\.?|inr)",
    ]
    for p in patterns:
        m = re.search(p, t, flags=re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                if 50 <= val <= 500000:
                    return val
            except Exception:
                return None
    return None


def _extract_discount(text: str):
    if not text:
        return None
    m = re.search(r"(\d{1,2})\s*%\s*(?:off|discount)?", str(text).lower())
    if not m:
        return None
    try:
        d = float(m.group(1))
        return d if 0 <= d <= 80 else None
    except Exception:
        return None


def _request_serper(query: str, num: int = 10) -> Dict:
    if not SERPER_API_KEY:
        return {}
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    payload = {"q": query, "gl": "in", "hl": "en", "num": num}

    last_error = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(SERPER_URL, headers=headers, data=json.dumps(payload), timeout=15)
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = f"status {resp.status_code}"
                time.sleep(min(2.0, 0.4 * (2 ** (attempt - 1))))
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_error = str(e)
            if attempt < 3:
                time.sleep(min(2.0, 0.4 * (2 ** (attempt - 1))))

    logger.warning("Serper request failed: %s", last_error)
    return {}


def _relevant(ticket_type: str, title: str, snippet: str) -> bool:
    cfg = TYPE_CONFIG.get(ticket_type, TYPE_CONFIG["BUS"])
    text = f"{title or ''} {snippet or ''}".lower()
    if any(b in text for b in cfg["blocked"]):
        return False
    if not cfg["required"]:
        return True
    return any(k in text for k in cfg["required"])


def _query_for(ticket_type: str, source: str, destination: str, date: str, keyword: str) -> str:
    t = ticket_type.upper().strip()
    route = f"{source} to {destination}".strip()
    if t == "BUS":
        q = f"{route} bus ticket booking India"
    elif t == "TRAIN":
        q = f"{route} train ticket IRCTC booking India"
    elif t == "FLIGHT":
        q = f"flights from {source} to {destination} airfare India"
    elif t == "HOTEL":
        city = destination or source
        q = f"hotels in {city} booking prices India"
    elif t in ("CONCERT", "EVENT"):
        city = destination or source
        q = f"{keyword or 'upcoming concerts'} in {city} tickets India"
    else:
        q = f"{ticket_type} {source} {destination} {keyword} ticket India"
    if date:
        q = f"{q} {date}"
    return " ".join(q.split())


def _normalize_results(ticket_type: str, source: str, destination: str, raw: Dict) -> List[Dict]:
    cfg = TYPE_CONFIG.get(ticket_type, TYPE_CONFIG["BUS"])
    out = []

    organic = raw.get("organic", []) or []
    for r in organic:
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        if not _relevant(ticket_type, title, snippet):
            continue

        text = f"{title} {snippet}"
        price = _extract_price(text)
        discount = _extract_discount(text)

        item = {
            "type": ticket_type,
            "title": title,
            "snippet": snippet,
            "source": source,
            "destination": destination,
            "icon": cfg["icon"],
            "color": cfg["color"],
            "link": r.get("link", ""),
            "price": float(price) if price is not None else None,
            "discount": float(discount) if discount is not None else None,
        }
        if item["price"] is not None:
            d = item["discount"] or 0.0
            item["final_price"] = round(item["price"] * (1 - d / 100), 2)
        else:
            item["final_price"] = None
        out.append(item)
        if len(out) >= 8:
            break

    # Fallback: if strict keyword filtering yields nothing, include top organic results
    # so the UI still shows live Serper discovery cards.
    if not out:
        for r in organic[:8]:
            title = r.get("title", "")
            snippet = r.get("snippet", "")
            text = f"{title} {snippet}"
            price = _extract_price(text)
            discount = _extract_discount(text)
            item = {
                "type": ticket_type,
                "title": title,
                "snippet": snippet,
                "source": source,
                "destination": destination,
                "icon": cfg["icon"],
                "color": cfg["color"],
                "link": r.get("link", ""),
                "price": float(price) if price is not None else None,
                "discount": float(discount) if discount is not None else None,
            }
            if item["price"] is not None:
                d = item["discount"] or 0.0
                item["final_price"] = round(item["price"] * (1 - d / 100), 2)
            else:
                item["final_price"] = None
            out.append(item)

    ab = raw.get("answerBox") or {}
    if ab.get("title"):
        ab_text = f"{ab.get('title', '')} {ab.get('answer', '')} {ab.get('snippet', '')}"
        price = _extract_price(ab_text)
        discount = _extract_discount(ab_text)
        featured = {
            "type": ticket_type,
            "title": ab.get("title", ""),
            "snippet": ab.get("answer") or ab.get("snippet", ""),
            "source": source,
            "destination": destination,
            "icon": cfg["icon"],
            "color": cfg["color"],
            "link": ab.get("link", ""),
            "featured": True,
            "price": float(price) if price is not None else None,
            "discount": float(discount) if discount is not None else None,
        }
        if featured["price"] is not None:
            d = featured["discount"] or 0.0
            featured["final_price"] = round(featured["price"] * (1 - d / 100), 2)
        else:
            featured["final_price"] = None
        out.insert(0, featured)

    return out[:8]


def unified_search(ticket_type: str, source: str = "", destination: str = "", date: str = "", keyword: str = "") -> List[Dict]:
    t = (ticket_type or "").upper().strip()
    if not t:
        return []

    query = _query_for(t, source.strip(), destination.strip(), date.strip(), keyword.strip())
    raw = _request_serper(query=query, num=10)
    return _normalize_results(t, source.strip(), destination.strip(), raw)
