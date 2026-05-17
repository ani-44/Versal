import logging
import math
import os
import re
import time
from datetime import datetime
from functools import lru_cache
from typing import Dict, List

import requests

from utils.llm_orchestrator import call_ai_with_fallback, groq_health_check as _groq_health

logger = logging.getLogger(__name__)
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "").strip()
SERPER_URL = "https://google.serper.dev/search"
DEFAULT_BUDGET = 5000.0
MAX_RANKED_RESULTS = 5


def call_ai(messages: list, max_tokens: int = 512, temperature: float = 0.20, stream: bool = False) -> str:
    return call_ai_with_fallback(messages=messages, max_tokens=max_tokens, temperature=temperature, timeout=45)


def groq_health_check() -> dict:
    return _groq_health()


def _score_price(final_price: float, budget: float) -> float:
    if budget <= 0:
        return 15.0
    ratio = final_price / budget
    if ratio <= 0.6:
        return 28.0
    if ratio <= 1.0:
        return 35.0 - (ratio - 0.6) * 15
    return max(0.0, 20.0 - (ratio - 1.0) * 35)


def _score_discount(discount: float) -> float:
    return min(20.0, max(0.0, discount * 0.8))


def _score_type_match(ticket_type: str, interests: List[str]) -> float:
    if not interests:
        return 10.0
    return 20.0 if ticket_type.upper() in [i.upper() for i in interests] else 6.0


def _score_route_match(ticket: Dict, source: str, destination: str) -> float:
    t_src = str(ticket.get("source", "")).lower()
    t_dst = str(ticket.get("destination", "")).lower()
    src = (source or "").lower().strip()
    dst = (destination or "").lower().strip()
    if not src and not dst:
        return 6.0
    if src and dst and ((src in t_src and dst in t_dst) or (src in t_dst and dst in t_src)):
        return 12.0
    if src and (src in t_src or src in t_dst):
        return 7.0
    if dst and (dst in t_src or dst in t_dst):
        return 7.0
    return 0.0


def _score_availability(total_seats: int) -> float:
    if total_seats <= 0:
        return 3.0
    return min(8.0, math.log1p(total_seats))


def _to_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _to_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _normalize_ticket(ticket: Dict) -> Dict:
    t_type = str(ticket.get("type", "")).upper().strip()
    title = str(ticket.get("title", "")).strip() or (f"{t_type} Ticket" if t_type else "Ticket")
    src = str(ticket.get("source", "")).strip()
    dst = str(ticket.get("destination", "")).strip()
    price = max(0.0, _to_float(ticket.get("price", 0), 0.0))
    discount = _to_float(ticket.get("discount", 0), 0.0)
    discount = min(80.0, max(0.0, discount))
    final_price = _to_float(ticket.get("final_price", round(price * (1 - discount / 100), 2)), 0.0)
    if final_price <= 0 and price > 0:
        final_price = round(price * (1 - discount / 100), 2)
    seats = max(0, _to_int(ticket.get("total_seats", 0), 0))
    return {
        **ticket,
        "type": t_type,
        "title": title,
        "source": src,
        "destination": dst,
        "price": price,
        "discount": discount,
        "final_price": final_price,
        "total_seats": seats,
    }


def _default_reason(best: Dict, budget: float) -> str:
    title = best.get("title", "this option")
    score = _to_float(best.get("score", 0), 0)
    final_price = _to_float(best.get("final_price", 0), 0)
    if budget > 0 and final_price > 0 and final_price <= budget:
        return f"{title} offers strong value with a good score ({score:.0f}/100) and stays within your budget."
    return f"{title} ranks highest by combined price, route match, discount, and availability (score {score:.0f}/100)."


def get_best_plan(tickets: list, prefs: dict = None) -> dict:
    if not tickets:
        return {"best_plan": None, "ai_reason": "No matching tickets found for this query.", "ranked": [], "prefs_used": prefs or {}}

    prefs = prefs or {}
    budget = _to_float(prefs.get("budget", DEFAULT_BUDGET), DEFAULT_BUDGET)
    if budget <= 0:
        budget = DEFAULT_BUDGET
    interests = prefs.get("interests", []) or []
    src = prefs.get("source", "")
    dst = prefs.get("destination", "")

    scored = []
    for t in tickets:
        t = _normalize_ticket(t)
        price = t["price"]
        discount = t["discount"]
        final_price = t["final_price"]
        seats = t["total_seats"]
        if final_price <= 0:
            continue

        score = (
            _score_price(final_price, budget)
            + _score_discount(discount)
            + _score_type_match(str(t.get("type", "")), interests)
            + _score_route_match(t, src, dst)
            + _score_availability(seats)
        )
        scored.append({**t, "final_price": final_price, "score": round(min(100.0, score), 2)})

    if not scored:
        return {"best_plan": None, "ai_reason": "No matching tickets found for this query.", "ranked": [], "prefs_used": prefs or {}}

    scored.sort(key=lambda x: x["score"], reverse=True)
    ranked = []
    for i, t in enumerate(scored[:MAX_RANKED_RESULTS], 1):
        ranked.append({
            "rank": i,
            "id": str(t.get("id", t.get("_id", ""))),
            "title": t.get("title", ""),
            "type": t.get("type", ""),
            "source": t.get("source", ""),
            "destination": t.get("destination", ""),
            "price": float(t.get("price", 0) or 0),
            "discount": float(t.get("discount", 0) or 0),
            "final_price": float(t.get("final_price", 0) or 0),
            "total_seats": int(t.get("total_seats", 0) or 0),
            "score": float(t.get("score", 0) or 0),
        })

    best = ranked[0]
    ai_reason_prompt = (
        "Explain in 1-2 short sentences why this is a good ticket option based on price/value/availability. "
        f"Ticket={best['title']} Type={best['type']} Route={best['source']}->{best['destination']} "
        f"FinalPrice={best['final_price']} Budget={budget}"
    )
    try:
        ai_reason = call_ai([{"role": "user", "content": ai_reason_prompt}], max_tokens=90, temperature=0.15)
        if not str(ai_reason or "").strip():
            ai_reason = _default_reason(best, budget)
    except Exception:
        ai_reason = _default_reason(best, budget)

    return {"best_plan": best, "ai_reason": ai_reason, "ranked": ranked, "prefs_used": prefs}


@lru_cache(maxsize=64)
def _serper_search_cached(query: str, num: int = 8) -> dict:
    # Cached wrapper helps repeated trending/search prompts within short server lifetimes.
    return _serper_search(query, num)


def _serper_search(query: str, num: int = 8) -> dict:
    if not SERPER_API_KEY:
        return {}
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    payload = {"q": query, "gl": "in", "hl": "en", "num": num}
    for attempt in range(1, 4):
        try:
            resp = requests.post(SERPER_URL, headers=headers, json=payload, timeout=15)
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2.0, 0.4 * (2 ** (attempt - 1))))
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if attempt < 3:
                time.sleep(min(2.0, 0.4 * (2 ** (attempt - 1))))
    return {}


def fetch_trending_locations(db=None) -> list:
    cache_ttl = 6 * 3600
    if db is not None:
        try:
            cached = db.trending_locations.find_one({"cached_at": {"$gt": datetime.now().timestamp() - cache_ttl}})
            if cached and cached.get("locations"):
                return cached["locations"]
        except Exception:
            pass

    queries = [
        "top trending travel destinations India this year",
        "most visited tourist places India currently",
    ]
    locations = []
    seen = set()
    for q in queries:
        data = _serper_search_cached(q, num=6)
        for r in data.get("organic", [])[:6]:
            title = (r.get("title") or "").strip()
            if not title:
                continue
            key = title.lower()[:40]
            if key in seen:
                continue
            seen.add(key)
            snippet = (r.get("snippet") or "").strip()
            rating = None
            m = re.search(r"\b([3-5]\.\d)\b", snippet)
            if m:
                rating = float(m.group(1))
            locations.append({
                "name": title[:60],
                "description": snippet[:220],
                "rating": rating if rating is not None else 4.0,
                "popularity": "High",
                "travel_suitability": "General Tourism",
                "best_time": "Seasonal",
                "image": "",
                "link": r.get("link", ""),
                "source": "Serper",
            })

    locations = locations[:12]
    if db is not None and locations:
        try:
            db.trending_locations.delete_many({})
            db.trending_locations.insert_one({"locations": locations, "cached_at": datetime.now().timestamp()})
        except Exception as e:
            logger.warning("Trending cache write failed: %s", e)
    return locations


KNOWLEDGE_BASE = {
    "booking_process": "To book: login, search by type/source/destination/date, select seat, pay, and download ticket.",
    "payment_methods": "TicketHub supports Card, UPI, and Net Banking payment flows.",
    "ticket_types": "Supported types include BUS, TRAIN, FLIGHT, HOTEL, and CONCERT/EVENT.",
    "pricing": "Pricing is dynamic and varies by live availability, travel date, and operator offers.",
}

_KEYWORD_MAP = {
    "book": "booking_process",
    "pay": "payment_methods",
    "upi": "payment_methods",
    "type": "ticket_types",
    "bus": "ticket_types",
    "train": "ticket_types",
    "flight": "ticket_types",
    "hotel": "ticket_types",
    "concert": "ticket_types",
    "price": "pricing",
    "cost": "pricing",
    "cheap": "pricing",
}


def rag_retrieve(query: str, db=None, tickets: list = None) -> str:
    q = (query or "").lower()
    chunks = []
    added = set()

    for k, v in _KEYWORD_MAP.items():
        if k in q and v not in added:
            chunks.append(KNOWLEDGE_BASE[v])
            added.add(v)

    if tickets and any(k in q for k in ["price", "cost", "route", "available", "ticket"]):
        lines = ["Live tickets:"]
        for t in tickets[:10]:
            t = _normalize_ticket(t)
            final = t.get("final_price", 0)
            if final <= 0:
                continue
            lines.append(
                f"[{t.get('type', '?')}] {t.get('title', '?')} | {t.get('source', '?')} -> {t.get('destination', '?')} | INR {final}"
            )
        if len(lines) > 1:
            chunks.append("\n".join(lines))

    return "\n\n".join(chunks)
