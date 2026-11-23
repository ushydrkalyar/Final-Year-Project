"""
email_service.py — sends transactional emails via Resend API.

Two public functions:
    send_otp_email(to_email, otp)           — 6-digit verification code
    send_reset_email(to_email, token, base_url) — password reset link
"""
import os
import resend

resend.api_key = os.environ.get("RESEND_API_KEY", "")
FROM_ADDRESS   = os.environ.get("RESEND_FROM", "CutSmart <onboarding@resend.dev>")


def _smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_USER", "").strip() and os.environ.get("SMTP_PASSWORD", "").strip())


def _dev_mode() -> bool:
    """No email provider configured -> print codes/links in the terminal instead of emailing."""
    if os.environ.get("DEV_MODE") == "1":
        return True
    return not (_smtp_configured() or os.environ.get("RESEND_API_KEY", "").strip())


def _deliver(params: dict) -> None:
    """Send via Gmail/any SMTP if SMTP_USER + SMTP_PASSWORD are set, otherwise via Resend."""
    if not _smtp_configured():
        resend.Emails.send(params)
        return
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    user = os.environ["SMTP_USER"].strip()
    pwd = os.environ["SMTP_PASSWORD"].replace(" ", "")
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    msg = MIMEMultipart("alternative")
    msg["Subject"] = params["subject"]
    msg["From"] = os.environ.get("SMTP_FROM", f"CutSmart <{user}>")
    msg["To"] = ", ".join(params["to"])
    msg.attach(MIMEText(params["html"], "html"))
    with smtplib.SMTP(host, port, timeout=20) as server:
        server.starttls()
        server.login(user, pwd)
        server.sendmail(user, params["to"], msg.as_string())


def send_otp_email(to_email: str, otp: str) -> bool:
    """Send a 6-digit OTP to verify the user's email address."""
    if _dev_mode():
        print(f"\n[DEV MODE] Verification code for {to_email}: {otp}\n", flush=True)
        return True
    try:
        params = {
            "from": FROM_ADDRESS,
            "to":   [to_email],
            "subject": f"{otp} is your CutSmart verification code",
            "html": f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0F1117;font-family:'Inter',system-ui,sans-serif;color:#fff">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0F1117;padding:40px 20px">
    <tr><td align="center">
      <table width="440" cellpadding="0" cellspacing="0"
             style="background:#1A1D29;border:1px solid rgba(255,255,255,.07);border-radius:12px;padding:36px 40px">
        <tr><td>
          <!-- Logo -->
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:28px">
            <div style="width:32px;height:32px;background:#6366F1;border-radius:8px;display:inline-block;
                        text-align:center;line-height:32px;font-weight:800;font-size:14px">C</div>
            <span style="font-weight:700;font-size:15px;vertical-align:middle;margin-left:6px">CutSmart</span>
          </div>

          <h2 style="margin:0 0 8px;font-size:20px;font-weight:800;letter-spacing:-.01em">Verify your email</h2>
          <p style="margin:0 0 28px;font-size:13px;color:#94A3B8;line-height:1.6">
            Enter this code in CutSmart to complete your sign-up.<br>
            It expires in <strong style="color:#fff">10 minutes</strong>.
          </p>

          <!-- OTP box -->
          <div style="background:#22263A;border:1px solid rgba(255,255,255,.13);border-radius:10px;
                      padding:20px;text-align:center;margin-bottom:28px">
            <span style="font-family:'Courier New',monospace;font-size:36px;font-weight:700;
                         letter-spacing:12px;color:#6366F1">{otp}</span>
          </div>

          <p style="margin:0;font-size:12px;color:#475569;line-height:1.6">
            If you didn't request this, you can safely ignore this email.<br>
            Never share this code with anyone.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>""",
        }
        _deliver(params)
        return True
    except Exception as e:
        print(f"[email] Failed to send OTP to {to_email}: {e}")
        return False


def send_reset_email(to_email: str, token: str, base_url: str) -> bool:
    if _dev_mode():
        print(f"\n[DEV MODE] Password reset for {to_email}: {base_url}/reset_password.html?token={token}\n", flush=True)
        return True
    return _send_reset_email_real(to_email, token, base_url)


def _send_reset_email_real(to_email: str, token: str, base_url: str) -> bool:
    """Send a password-reset link containing a one-time token."""
    reset_url = f"{base_url}/reset_password.html?token={token}"
    try:
        params = {
            "from": FROM_ADDRESS,
            "to":   [to_email],
            "subject": "Reset your CutSmart password",
            "html": f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#0F1117;font-family:'Inter',system-ui,sans-serif;color:#fff">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0F1117;padding:40px 20px">
    <tr><td align="center">
      <table width="440" cellpadding="0" cellspacing="0"
             style="background:#1A1D29;border:1px solid rgba(255,255,255,.07);border-radius:12px;padding:36px 40px">
        <tr><td>
          <div style="margin-bottom:28px">
            <div style="width:32px;height:32px;background:#6366F1;border-radius:8px;display:inline-block;
                        text-align:center;line-height:32px;font-weight:800;font-size:14px">C</div>
            <span style="font-weight:700;font-size:15px;vertical-align:middle;margin-left:6px">CutSmart</span>
          </div>

          <h2 style="margin:0 0 8px;font-size:20px;font-weight:800;letter-spacing:-.01em">Reset your password</h2>
          <p style="margin:0 0 28px;font-size:13px;color:#94A3B8;line-height:1.6">
            Click the button below to choose a new password.<br>
            This link expires in <strong style="color:#fff">1 hour</strong> and can only be used once.
          </p>

          <!-- CTA button -->
          <div style="text-align:center;margin-bottom:28px">
            <a href="{reset_url}"
               style="display:inline-block;background:#6366F1;color:#fff;font-weight:700;font-size:14px;
                      padding:13px 32px;border-radius:8px;text-decoration:none;letter-spacing:-.01em">
              Reset Password
            </a>
          </div>

          <p style="margin:0 0 12px;font-size:12px;color:#475569">Or paste this link into your browser:</p>
          <p style="margin:0 0 24px;font-size:11.5px;color:#6366F1;word-break:break-all">{reset_url}</p>

          <p style="margin:0;font-size:12px;color:#475569;line-height:1.6">
            If you didn't request a password reset, ignore this email — your account is safe.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>""",
        }
        _deliver(params)
        return True
    except Exception as e:
        print(f"[email] Failed to send reset email to {to_email}: {e}")
        return False
