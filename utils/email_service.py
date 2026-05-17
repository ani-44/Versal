"""
email_service.py — TicketHub Email Service
Handles:
  • Welcome email after signup
  • Booking confirmation with PDF attachment
"""

from flask_mail import Message


def send_welcome_email(mail, user_name: str, user_email: str, login_url: str = "http://localhost:5000/login"):
    """Send a welcome email to a newly registered user."""
    subject = "🎟 Welcome to TicketHub — Your Journey Begins!"
    html_body = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:30px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#1a0050 0%,#1a4fc4 100%);padding:36px 40px;text-align:center;">
            <div style="font-size:36px;margin-bottom:8px;">🎟</div>
            <h1 style="color:#fff;margin:0;font-size:28px;letter-spacing:1px;">TicketHub</h1>
            <p style="color:#c0a0ff;margin:8px 0 0;font-size:14px;">Your Smart Travel Booking Platform</p>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:36px 40px;">
            <h2 style="color:#1a0050;margin:0 0 16px;font-size:22px;">
              Welcome aboard, {user_name}! 🎉
            </h2>
            <p style="color:#444;line-height:1.7;font-size:15px;">
              We're thrilled to have you join the TicketHub family. Your account has been
              successfully created and you're ready to explore amazing travel deals.
            </p>

            <!-- Credentials box -->
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f0ff;border-radius:10px;border-left:4px solid #3a00a0;margin:24px 0;">
              <tr>
                <td style="padding:20px 24px;">
                  <p style="margin:0 0 12px;color:#3a00a0;font-weight:700;font-size:14px;text-transform:uppercase;letter-spacing:0.5px;">
                    Your Login Details
                  </p>
                  <p style="margin:6px 0;color:#333;font-size:14px;">
                    <strong>📧 Email:</strong> {user_email}
                  </p>
                  <p style="margin:6px 0;color:#333;font-size:14px;">
                    <strong>🔒 Password:</strong> The password you set during signup
                  </p>
                </td>
              </tr>
            </table>

            <!-- What you can do -->
            <p style="color:#444;font-size:15px;font-weight:600;margin:20px 0 12px;">
              What you can do on TicketHub:
            </p>
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="padding:6px 0;color:#555;font-size:14px;">🚌 &nbsp;Book bus, train & flight tickets</td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#555;font-size:14px;">🏨 &nbsp;Reserve hotel rooms at great prices</td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#555;font-size:14px;">🎵 &nbsp;Get concert & event tickets</td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#555;font-size:14px;">🤖 &nbsp;AI chatbot for travel recommendations</td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#555;font-size:14px;">📄 &nbsp;Download beautiful PDF tickets instantly</td>
              </tr>
            </table>

            <!-- CTA button -->
            <div style="text-align:center;margin:32px 0 16px;">
              <a href="{login_url}"
                 style="background:linear-gradient(135deg,#3a00a0,#1a4fc4);color:#fff;text-decoration:none;
                        padding:14px 40px;border-radius:30px;font-size:16px;font-weight:700;
                        display:inline-block;letter-spacing:0.5px;">
                🚀 Login to TicketHub
              </a>
            </div>

          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#1a0050;padding:24px 40px;text-align:center;">
            <p style="color:#c0a0ff;font-size:13px;margin:0 0 6px;">
              Questions? Contact us at
              <a href="mailto:support@tickethub.com" style="color:#a0c0ff;">support@tickethub.com</a>
            </p>
            <p style="color:#6040a0;font-size:12px;margin:0;">
              © 2025 TicketHub · www.tickethub.com
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>
"""
    try:
        msg = Message(
            subject=subject,
            recipients=[user_email],
            html=html_body,
        )
        mail.send(msg)
        return True
    except Exception as e:
        print(f"Welcome email error: {e}")
        return False


def send_booking_confirmation(mail, user_name: str, user_email: str, booking: dict, pdf_buffer=None):
    """Send booking confirmation email with optional PDF attachment."""
    booking_id = booking.get('booking_id', booking.get('payment_id', 'N/A'))
    route      = booking.get('route', 'N/A')
    seat       = booking.get('seat', booking.get('seat_no', 'N/A'))
    amount     = booking.get('amount', booking.get('final_price', 'N/A'))
    t_type     = booking.get('ticket_type', booking.get('type', 'N/A'))

    html_body = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:30px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

        <tr>
          <td style="background:linear-gradient(135deg,#1a0050 0%,#1a4fc4 100%);padding:32px 40px;text-align:center;">
            <div style="font-size:40px;">✅</div>
            <h1 style="color:#fff;margin:8px 0 4px;font-size:24px;">Booking Confirmed!</h1>
            <p style="color:#c0a0ff;margin:0;font-size:14px;">Your TicketHub reservation is ready</p>
          </td>
        </tr>

        <tr>
          <td style="padding:32px 40px;">
            <p style="color:#333;font-size:15px;">Hi <strong>{user_name}</strong>, great news!</p>
            <p style="color:#555;font-size:14px;line-height:1.7;">
              Your booking has been confirmed. Your PDF ticket is attached to this email.
              Please keep it handy for your journey.
            </p>

            <!-- Booking details -->
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f0ff;border-radius:10px;border-left:4px solid #3a00a0;margin:20px 0;">
              <tr><td style="padding:20px 24px;">
                <p style="margin:0 0 14px;color:#3a00a0;font-weight:700;font-size:13px;text-transform:uppercase;">Booking Summary</p>
                <table width="100%">
                  <tr>
                    <td style="color:#888;font-size:13px;padding:4px 0;">Booking ID</td>
                    <td style="color:#1a0050;font-weight:700;font-size:14px;text-align:right;">{booking_id}</td>
                  </tr>
                  <tr>
                    <td style="color:#888;font-size:13px;padding:4px 0;">Route</td>
                    <td style="color:#333;font-size:13px;text-align:right;">{route}</td>
                  </tr>
                  <tr>
                    <td style="color:#888;font-size:13px;padding:4px 0;">Type</td>
                    <td style="color:#333;font-size:13px;text-align:right;">{t_type}</td>
                  </tr>
                  <tr>
                    <td style="color:#888;font-size:13px;padding:4px 0;">Seat / Room</td>
                    <td style="color:#333;font-size:13px;text-align:right;">{seat}</td>
                  </tr>
                  <tr>
                    <td style="color:#888;font-size:13px;padding:4px 0;border-top:1px solid #ddd;">Amount Paid</td>
                    <td style="color:#1a8a4a;font-weight:700;font-size:16px;text-align:right;border-top:1px solid #ddd;">₹{amount}</td>
                  </tr>
                </table>
              </td></tr>
            </table>

            <p style="color:#777;font-size:13px;line-height:1.7;">
              📎 Your PDF ticket is attached. Please arrive 30 minutes before departure.<br>
              This ticket is non-transferable. For cancellations, contact us within 24 hours.
            </p>
          </td>
        </tr>

        <tr>
          <td style="background:#1a0050;padding:20px 40px;text-align:center;">
            <p style="color:#c0a0ff;font-size:13px;margin:0 0 4px;">
              <a href="mailto:support@tickethub.com" style="color:#a0c0ff;">support@tickethub.com</a>
              &nbsp;|&nbsp; www.tickethub.com
            </p>
            <p style="color:#6040a0;font-size:11px;margin:0;">© 2025 TicketHub</p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""
    try:
        msg = Message(
            subject=f"🎟 TicketHub — Booking Confirmed! [{booking_id}]",
            recipients=[user_email],
            html=html_body,
        )
        if pdf_buffer:
            msg.attach(f"ticket_{booking_id}.pdf", "application/pdf", pdf_buffer.read())
        mail.send(msg)
        return True
    except Exception as e:
        print(f"Booking confirmation email error: {e}")
        return False
