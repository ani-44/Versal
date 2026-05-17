# TicketHub Enhanced — AI-Powered Travel Booking Platform

## What's New (v2.0)

### 1. AI Best Plan Comparison Engine  (`GET/POST /ai/best-plan`)
- Scores every ticket 0–100 based on price, discount, destination match, travel type preference
- Returns ranked list with pros/cons per option + AI-generated explanation of the top pick
- Accessible via the **"AI Best Plan"** button on the dashboard
- Frontend modal with budget/interests/destination input

### 2. Serper API — Trending Locations  (`GET /ai/trending-locations`)
- Fetches real-time trending tourist destinations from Google via Serper API
- Extracts: name, description, rating, best time to visit, image, source link
- MongoDB cache (24-hour TTL) to avoid excessive API calls
- Dedicated page: `/ai/trending-page`

### 3. RAG Chatbot  (`POST /api/chat/rag`)
- Knowledge Base covers: booking process, payment methods, ticket types, cancellation policy, account management, PDF tickets, support contact
- Dynamically injects live ticket data (prices, routes) when query is price/route-related
- Uses Groq Chat Completions with enriched context
- Dashboard chatbot now uses this endpoint

### 4. Duplicate Email Fix
- Explicit `find_one` check before insert → clear "Email already registered" message
- MongoDB `unique=True` index on `email` as DB-level safety net
- Input normalised to lowercase before storage

### 5. Welcome Email
- Sent automatically after successful signup (non-blocking — won't break signup on failure)
- HTML email: welcome message, login credentials summary, CTA button, branded footer
- Uses `utils/email_service.py` → `send_welcome_email()`

### 6. New Ticket Template
- Purple→blue gradient background matching uploaded sample template
- Left tear-off stub: seat, date, price (vertical layout)
- Dashed tear line with notch circles
- Right main body: passenger name (large), booking code badge, QR grid, destination/date chips
- Barcode strip at bottom with booking code
- Rounded corners, decorative translucent circles
- A6 landscape format (148×105 mm)

### 7. System Cleanup
- Duplicate inline booking email replaced by `utils/email_service.py`
- Centralised AI: all AI calls route through `utils/ai_engine.py`
- All new endpoints modular and non-breaking to existing routes

## Project Structure

```
ticket_hub_enhanced/
├── app.py                          # Main Flask app (all routes)
├── config.py                       # Environment-driven app configuration
├── requirements.txt
├── utils/
│   ├── pdf_generator.py            # NEW: purple-gradient ticket template
│   ├── ai_engine.py                # NEW: best-plan scoring, RAG, trending
│   ├── email_service.py            # NEW: welcome + booking confirmation emails
│   └── serper_search.py            # Existing: Serper live ticket search
├── templates/
│   ├── user/
│   │   ├── dashboard.html          # UPDATED: AI Best Plan modal + Trending button + RAG chat
│   │   ├── trending.html           # NEW: Trending destinations page
│   │   └── ...                     # All other existing templates unchanged
│   └── admin/
│       └── ...                     # Admin templates unchanged
├── static/
│   └── ...                         # Static assets unchanged
└── database/
    └── mongo_init.py               # DB init
```

## New API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET/POST | `/ai/best-plan` | AI ticket ranking. POST body: `{budget, interests[], destination}` |
| GET | `/ai/trending-locations` | Serper-powered trending tourist spots (JSON) |
| GET | `/ai/trending-page` | Trending locations HTML page |
| POST | `/api/chat/rag` | RAG chatbot. Body: `{message, messages[]}` |

## Setup

```bash
cp .env.example .env
pip install -r requirements.txt
python app.py
```

Set all required values in `.env`:
- `GROQ_API_KEY` for AI chat/completions
- `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` for OAuth login
- `MONGO_URI`/`MONGO_DB` for database
- mail variables for OTP and booking emails
