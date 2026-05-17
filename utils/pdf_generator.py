"""
pdf_generator.py  —  TicketHub Ticket Generator
Purple/blue gradient ticket with:
  • Left stub (tear-off): booking code, date, seat, price
  • Right main body: passenger, route, QR, barcode
  • Each booking carries its OWN price — no shared/hardcoded values
"""

import io
import hashlib
from datetime import datetime
from reportlab.lib.pagesizes import A6, landscape
from reportlab.lib import colors
from reportlab.lib.units import cm, mm
from reportlab.platypus import Flowable


# ── Colour Palette ───────────────────────────────────────────────────
PURPLE_DARK  = colors.HexColor('#1a0050')
PURPLE_MID   = colors.HexColor('#3a00a0')
PURPLE_LIGHT = colors.HexColor('#6a3fdb')
BLUE_MID     = colors.HexColor('#1a4fc4')
BLUE_LIGHT   = colors.HexColor('#3a8ee6')
LIGHT_PURPLE = colors.HexColor('#c0a0ff')
WHITE        = colors.white
STUB_BG      = colors.HexColor('#2d0080')
GOLD         = colors.HexColor('#f5c518')


def _format_price(amount) -> str:
    """
    Safely format a price value to a clean Rs. string.
    Handles int, float, str, None — never returns 'N/A' for numeric data.
    """
    if amount is None:
        return "N/A"
    try:
        val = float(str(amount).replace(',', '').replace('Rs.', '').replace('₹', '').strip())
        if val == int(val):
            return f"Rs.{int(val):,}"
        return f"Rs.{val:,.2f}"
    except (ValueError, TypeError):
        return str(amount)


def _extract_booking_fields(bk: dict) -> dict:
    """
    Normalise booking dict regardless of how it was created.
    Priority: explicit keys > fallback keys > sensible defaults.
    This is the SINGLE source of truth for PDF field extraction.
    """
    # ── Price (most critical field) ──────────────────────────────────
    # Try every key that might carry the final charged amount
    price_raw = (
        bk.get('final_price') or
        bk.get('amount') or
        bk.get('price') or
        bk.get('ticket_price') or
        0
    )
    try:
        price_val = float(str(price_raw).replace(',', '').strip())
    except (ValueError, TypeError):
        price_val = 0.0

    # If we have base_price + discount, recompute to guarantee accuracy
    if bk.get('base_price') and bk.get('discount') is not None:
        try:
            base     = float(bk['base_price'])
            discount = float(bk['discount'])
            computed = round(base * (1 - discount / 100), 2)
            # Use computed if price_val looks like a default/zero
            if price_val == 0 or price_val == 800:   # 800 was the old hardcoded default
                price_val = computed
        except (ValueError, TypeError):
            pass

    # ── Other fields ──────────────────────────────────────────────────
    name = str(
        bk.get('user_name') or bk.get('passenger') or 'Valued Customer'
    ).upper()

    code = str(
        bk.get('booking_id') or bk.get('payment_id') or bk.get('id') or 'TKT00000'
    ).upper()

    ticket_title = str(
        bk.get('title') or bk.get('ticket_type') or bk.get('route') or 'TicketHub Pass'
    ).upper()

    source = str(bk.get('source') or bk.get('from') or '').strip()
    dest   = str(bk.get('destination') or bk.get('to') or bk.get('dest') or '').strip()
    ticket_type = str(bk.get('type') or bk.get('ticket_category') or '').upper()

    # Hotels/concerts are location/event tickets, not source-to-destination routes.
    context_label = str(bk.get('context_label') or '').upper()
    context_value = str(bk.get('context_value') or '').strip()
    if context_label and context_value:
        if 'HOTEL' in context_label:
            route_label = 'HOTEL'
        elif 'EVENT' in context_label or 'CONCERT' in context_label:
            route_label = 'EVENT'
        else:
            route_label = context_label
        route_str = context_value
    elif ticket_type == 'HOTEL':
        route_label = 'HOTEL'
        route_str = dest or source or ticket_title
    elif ticket_type == 'CONCERT':
        route_label = 'EVENT'
        location = dest or source
        route_str = f"{ticket_title} - {location}" if location and location.upper() not in ticket_title else ticket_title
    elif source and dest and source != dest:
        route_label = 'ROUTE'
        route_str = f"{source} -> {dest}"
    else:
        route_label = 'ROUTE'
        route_str = source or dest or ticket_title

    seat = str(bk.get('seat') or bk.get('seat_no') or bk.get('seat_number') or 'N/A')

    # Travel date
    travel_d = bk.get('travel_date') or bk.get('date') or ''
    if hasattr(travel_d, 'strftime'):
        travel_d = travel_d.strftime('%d %b %Y')
    elif not travel_d:
        travel_d = datetime.now().strftime('%d %b %Y')
    else:
        travel_d = str(travel_d)[:10]

    # Booking date
    book_date = bk.get('booking_date') or bk.get('created_at') or datetime.now()
    if hasattr(book_date, 'strftime'):
        book_date = book_date.strftime('%d %b %Y')
    else:
        book_date = str(book_date)[:10]

    discount = bk.get('discount', 0)
    try:
        discount = float(discount)
    except (ValueError, TypeError):
        discount = 0.0

    return {
        'name':        name,
        'code':        code,
        'title':       ticket_title,
        'source':      source,
        'dest':        dest,
        'route':       route_str,
        'route_label': route_label,
        'seat':        seat,
        'type':        ticket_type,
        'travel_d':    travel_d,
        'book_date':   book_date,
        'price_val':   price_val,
        'discount':    discount,
        'price_str':   _format_price(price_val),
    }


# ── Drawing helpers ───────────────────────────────────────────────────

def _gradient_rect(c, x, y, width, height, c1, c2, steps=60, vertical=False):
    for i in range(steps):
        t = i / (steps - 1)
        r = c1.red   + t * (c2.red   - c1.red)
        g = c1.green + t * (c2.green - c1.green)
        b = c1.blue  + t * (c2.blue  - c1.blue)
        col = colors.Color(r, g, b)
        c.setFillColor(col)
        if vertical:
            sh = height / steps
            c.rect(x, y + i * sh, width, sh + 0.5, fill=1, stroke=0)
        else:
            sw = width / steps
            c.rect(x + i * sw, y, sw + 0.5, height, fill=1, stroke=0)


def _draw_deco_circles(c, cx, cy):
    for alpha, r in [(0.12, 2.2*cm), (0.08, 1.6*cm), (0.05, 1.0*cm)]:
        c.setFillColor(colors.Color(1, 1, 1, alpha))
        c.circle(cx, cy, r, fill=1, stroke=0)


def _draw_barcode(c, x, y, width, code):
    bar_h = 0.85*cm
    c.setFillColor(WHITE)
    c.roundRect(x + 0.2*cm, y, width - 0.4*cm, bar_h, 3, fill=1, stroke=0)
    c.setFillColor(colors.HexColor('#111111'))
    hash_val = int(hashlib.md5(code.encode()).hexdigest(), 16)
    bw = (width - 0.6*cm) / 90
    cx2 = x + 0.3*cm
    for i in range(80):
        bit = (hash_val >> (i % 64)) & 1
        bar_thick = bw * (1.8 if bit else 0.9)
        c.rect(cx2, y + 0.08*cm, bar_thick, bar_h - 0.18*cm, fill=1, stroke=0)
        cx2 += bar_thick + bw * 0.5
    c.setFillColor(colors.HexColor('#333333'))
    c.setFont('Helvetica', 5)
    c.drawCentredString(x + width/2, y + 0.02*cm, code.upper())


def _draw_qr(c, x, y, size):
    c.setFillColor(WHITE)
    c.roundRect(x, y, size, size, 2, fill=1, stroke=0)
    cell = size / 9
    c.setFillColor(colors.black)
    for (pr, pc) in [(0, 0), (0, 6), (6, 0)]:
        for dr in range(3):
            for dc in range(3):
                if dr == 0 or dr == 2 or dc == 0 or dc == 2:
                    c.rect(x+(pc+dc)*cell+0.3, y+size-(pr+dr+1)*cell+0.3,
                           cell-0.6, cell-0.6, fill=1, stroke=0)
        c.rect(x+(pc+1)*cell+0.8, y+size-(pr+2)*cell+0.8, cell-1.6, cell-1.6, fill=1, stroke=0)
    hv = int(hashlib.md5(b'qr').hexdigest(), 16)
    for r in range(9):
        for col in range(9):
            if ((hv >> (r*9+col)) & 1) and not (
                (r < 3 and col < 3) or (r < 3 and col > 5) or (r > 5 and col < 3)
            ):
                c.rect(x+col*cell+0.3, y+size-(r+1)*cell+0.3, cell-0.6, cell-0.6, fill=1, stroke=0)


# ── Ticket Flowable ───────────────────────────────────────────────────

class TicketFlowable(Flowable):
    def __init__(self, width, height, booking):
        super().__init__()
        self.width   = width
        self.height  = height
        self.booking = booking

    def draw(self):
        c    = self.canv
        w, h = self.width, self.height

        # Extract & normalise all fields through the single helper
        f = _extract_booking_fields(self.booking)

        STUB_W = w * 0.28
        TEAR_W = 5*mm
        MAIN_W = w - STUB_W - TEAR_W

        # ── Backgrounds ──────────────────────────────────────────────
        _gradient_rect(c, 0, 0, w, h, PURPLE_DARK, BLUE_MID, steps=80)
        _gradient_rect(c, 0, 0, STUB_W, h, PURPLE_MID, STUB_BG, steps=40, vertical=True)

        # ── Decorative circles ───────────────────────────────────────
        _draw_deco_circles(c, 0, h)
        _draw_deco_circles(c, STUB_W * 0.5, h * 0.9)
        _draw_deco_circles(c, w, h)
        _draw_deco_circles(c, w * 0.78, h)

        # ════════════ STUB ════════════════════════════════════════════

        # Rotated ticket type label
        c.saveState()
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 7)
        c.translate(STUB_W * 0.20, h * 0.5)
        c.rotate(90)
        c.drawCentredString(0, 0, f['type'] or 'TICKET')
        c.restoreState()

        # Rotated ticket title
        c.saveState()
        c.setFillColor(LIGHT_PURPLE)
        c.setFont('Helvetica', 6.5)
        c.translate(STUB_W * 0.50, h * 0.5)
        c.rotate(90)
        short = f['title'][:16] + ('…' if len(f['title']) > 16 else '')
        c.drawCentredString(0, 0, short)
        c.restoreState()

        # Stub info boxes
        def stub_box(by, label, val):
            c.setFillColor(colors.Color(1, 1, 1, 0.18))
            c.roundRect(0.22*cm, by, STUB_W - 0.44*cm, 1.05*cm, 4, fill=1, stroke=0)
            c.setFillColor(LIGHT_PURPLE)
            c.setFont('Helvetica', 5.5)
            c.drawCentredString(STUB_W/2, by + 0.75*cm, label)
            c.setFillColor(WHITE)
            c.setFont('Helvetica-Bold', 8)
            c.drawCentredString(STUB_W/2, by + 0.22*cm, str(val)[:12])

        stub_box(h * 0.60, 'DATE',      f['travel_d'][:10])
        stub_box(h * 0.42, 'SEAT/ROOM', f['seat'][:8])
        stub_box(0.4*cm,  'PRICE',     f['price_str'][:12])   # ← per-ticket price

        # ════════════ TEAR STRIP ══════════════════════════════════════
        tx = STUB_W
        c.setFillColor(colors.HexColor('#080020'))
        c.circle(tx + TEAR_W/2, h + 3*mm, 3.5*mm, fill=1, stroke=0)
        c.circle(tx + TEAR_W/2, -3*mm,    3.5*mm, fill=1, stroke=0)
        c.setStrokeColor(colors.Color(1, 1, 1, 0.45))
        c.setLineWidth(0.7)
        c.setDash(3, 4)
        c.line(tx + TEAR_W/2, 4*mm, tx + TEAR_W/2, h - 4*mm)
        c.setDash()

        # ════════════ MAIN BODY ═══════════════════════════════════════
        mx  = STUB_W + TEAR_W
        mw  = MAIN_W
        pad = 0.35*cm

        # Brand badge
        c.setFillColor(colors.Color(1, 1, 1, 0.18))
        c.roundRect(mx + pad, h - 1.2*cm, 2.4*cm, 0.85*cm, 4, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(mx + pad + 0.15*cm, h - 0.73*cm, '🎟 TicketHub')

        # Top-right: ticket title
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 7.5)
        c.drawRightString(mx + mw - pad, h - 0.62*cm, f['type'] or 'TICKET')
        c.setFillColor(LIGHT_PURPLE)
        c.setFont('Helvetica', 6.5)
        pkg_right = f['title'][:22] + ('…' if len(f['title']) > 22 else '')
        c.drawRightString(mx + mw - pad, h - 1.0*cm, pkg_right)

        # Separator
        c.setStrokeColor(colors.Color(1, 1, 1, 0.18))
        c.setLineWidth(0.5)
        c.line(mx + pad, h - 1.35*cm, mx + mw - pad, h - 1.35*cm)

        # Passenger name
        c.setFillColor(LIGHT_PURPLE)
        c.setFont('Helvetica', 7)
        c.drawString(mx + pad, h - 1.72*cm, 'PASSENGER')
        nf = 18 if len(f['name']) < 13 else (14 if len(f['name']) < 18 else 10)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', nf)
        disp = f['name'][:22] + ('…' if len(f['name']) > 22 else '')
        c.drawString(mx + pad, h - 2.55*cm, disp)

        # Booking code
        c.setFillColor(LIGHT_PURPLE)
        c.setFont('Helvetica', 7)
        c.drawString(mx + pad, h - 2.9*cm, 'BOOKING CODE')
        c.setFillColor(colors.Color(1, 1, 1, 0.18))
        c.roundRect(mx + pad, h - 3.75*cm, mw * 0.52, 0.72*cm, 4, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 13)
        c.drawString(mx + pad + 0.2*cm, h - 3.45*cm, f['code'][:10])

        # QR code
        qr_s = 1.75*cm
        _draw_qr(c, mx + mw - qr_s - pad, h - 3.85*cm, qr_s)
        c.setFillColor(LIGHT_PURPLE)
        c.setFont('Helvetica', 5)
        c.drawCentredString(mx + mw - qr_s/2 - pad, h - 4.05*cm, 'SCAN')

        # ── Info row: ROUTE + DATE ────────────────────────────────────
        iy  = h - 4.75*cm
        route_display = f['route'][:18]
        items = [(f"INFO {f['route_label']}", route_display), ('DATE', f['travel_d'][:10])]
        ix2 = mx + pad
        for lbl, val in items:
            c.setFillColor(colors.Color(1, 1, 1, 0.13))
            c.roundRect(ix2, iy, mw * 0.44, 0.75*cm, 3, fill=1, stroke=0)
            c.setFillColor(LIGHT_PURPLE)
            c.setFont('Helvetica', 5.5)
            c.drawString(ix2 + 0.1*cm, iy + 0.52*cm, lbl)
            c.setFillColor(WHITE)
            c.setFont('Helvetica-Bold', 7)
            c.drawString(ix2 + 0.1*cm, iy + 0.12*cm, val)
            ix2 += mw * 0.48

        # ── Price + Booked-on row ─────────────────────────────────────
        by2 = iy - 1.0*cm
        c.setFillColor(colors.Color(1, 1, 1, 0.10))
        c.roundRect(mx + pad, by2, mw - 2*pad, 0.72*cm, 3, fill=1, stroke=0)

        # Left: booked-on
        c.setFillColor(LIGHT_PURPLE)
        c.setFont('Helvetica', 5.5)
        c.drawString(mx + pad + 0.12*cm, by2 + 0.48*cm, 'BOOKED ON')
        c.setFillColor(WHITE)
        c.setFont('Helvetica-Bold', 7)
        c.drawString(mx + pad + 0.12*cm, by2 + 0.10*cm, str(f['book_date'])[:20])

        # Right: AMOUNT — always the specific booking price
        c.setFillColor(GOLD)
        c.setFont('Helvetica-Bold', 9)
        c.drawRightString(mx + mw - pad - 0.12*cm, by2 + 0.20*cm, f['price_str'])

        # Discount badge if applicable
        if f['discount'] > 0:
            c.setFillColor(colors.Color(1, 1, 1, 0.20))
            badge_w = 1.6*cm
            c.roundRect(mx + mw - pad - badge_w - 0.15*cm, by2 + 0.46*cm, badge_w, 0.22*cm, 2, fill=1, stroke=0)
            c.setFillColor(LIGHT_PURPLE)
            c.setFont('Helvetica', 5.5)
            c.drawRightString(mx + mw - pad - 0.18*cm, by2 + 0.48*cm, f'-{int(f["discount"])}% discount')

        # Barcode at bottom
        _draw_barcode(c, mx, 0.12*cm, mw, f['code'])

        # Outer glow border
        c.setStrokeColor(colors.Color(0.55, 0.35, 1.0, 0.55))
        c.setLineWidth(1.4)
        c.roundRect(0, 0, w, h, 6, fill=0, stroke=1)


# ── Public API ────────────────────────────────────────────────────────

def generate_ticket_pdf(booking: dict) -> io.BytesIO:
    """
    Generate a purple-gradient styled PDF ticket.
    `booking` must contain at minimum:
      - booking_id / payment_id
      - user_name / passenger
      - final_price OR (base_price + discount) OR amount
      - source, destination
      - seat / seat_no
    All field extraction is done inside _extract_booking_fields().
    """
    from reportlab.pdfgen.canvas import Canvas as PdfCanvas

    # Validate price before generating — prevents silent zero-price tickets
    price_raw = (
        booking.get('final_price') or
        booking.get('amount') or
        booking.get('price') or 0
    )
    try:
        assert float(str(price_raw).replace(',', '').strip()) > 0, "Price must be > 0"
    except (ValueError, AssertionError) as e:
        print(f"[PDF WARNING] Invalid price ({price_raw}): {e}. Proceeding anyway.")

    buffer  = io.BytesIO()
    page_w, page_h = landscape(A6)
    margin  = 4*mm
    ticket_w = page_w - 2*margin
    ticket_h = page_h - 2*margin

    c = PdfCanvas(buffer, pagesize=(page_w, page_h))

    # Round-corner clip path
    c.saveState()
    p   = c.beginPath()
    rad = 8
    p.moveTo(margin + rad, margin)
    p.lineTo(margin + ticket_w - rad, margin)
    p.arcTo(margin + ticket_w - 2*rad, margin,
            margin + ticket_w, margin + 2*rad, -90, 90)
    p.lineTo(margin + ticket_w, margin + ticket_h - rad)
    p.arcTo(margin + ticket_w - 2*rad, margin + ticket_h - 2*rad,
            margin + ticket_w, margin + ticket_h, 0, 90)
    p.lineTo(margin + rad, margin + ticket_h)
    p.arcTo(margin, margin + ticket_h - 2*rad,
            margin + 2*rad, margin + ticket_h, 90, 90)
    p.lineTo(margin, margin + rad)
    p.arcTo(margin, margin, margin + 2*rad, margin + 2*rad, 180, 90)
    p.close()
    c.clipPath(p, stroke=0)

    ticket = TicketFlowable(ticket_w, ticket_h, booking)
    ticket.canv = c
    c.translate(margin, margin)
    ticket.draw()
    c.restoreState()

    c.save()
    buffer.seek(0)
    return buffer
