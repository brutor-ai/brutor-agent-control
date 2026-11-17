# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Email service — sends transactional emails via Resend API.

`BRUTOR_RESEND_API_KEY` switches email on; `BRUTOR_RESEND_FROM_EMAIL` is
the sender address. The sign-in link of the welcome message is the User
Portal's address, where the installation has one
(`app.composition.hooks.portal_public_url`); without one the message
carries no link.
"""

import logging
from typing import Optional

import httpx
from app.composition.hooks import portal_public_url
from app.env_vars import BRUTOR_RESEND_API_KEY, BRUTOR_RESEND_FROM_EMAIL

log = logging.getLogger("brutor-email")

RESEND_API_URL = "https://api.resend.com/emails"


class EmailService:
    """Async email service using Resend API."""

    def __init__(
        self,
        api_key: str = "",
        from_email: str = "",
        portal_url: str = "",
    ):
        self.api_key = api_key or BRUTOR_RESEND_API_KEY.exact() or ""
        self.from_email = from_email or BRUTOR_RESEND_FROM_EMAIL.require()
        # Empty: the installation has no Portal to link to.
        self.portal_url = portal_url or portal_public_url() or ""
        self.enabled = bool(self.api_key)
        if not self.enabled:
            log.warning(f"EmailService: {BRUTOR_RESEND_API_KEY.name} not set — email disabled")

    async def _send(
        self, to_email: str, subject: str, html_body: str
    ) -> Optional[str]:
        """Send an email. Returns the Resend message ID or None on failure."""
        if not self.enabled:
            log.info(f"Email skipped (disabled): to={to_email}, subject={subject}")
            return None

        payload = {
            "from": f"Brutor AI <{self.from_email}>",
            "to": [to_email],
            "subject": subject,
            "html": html_body,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    RESEND_API_URL, json=payload, headers=headers
                )
                resp.raise_for_status()
                result = resp.json()
                msg_id = result.get("id", "")
                log.info(f"Email sent: id={msg_id}, to={to_email}")
                return msg_id
        except Exception as e:
            log.error(f"Failed to send email to {to_email}: {e}")
            return None

    # ── Welcome email (admin-created end user) ─────────────────────

    async def send_welcome_email(
        self,
        to_email: str,
        username: str,
        display_name: str,
        tenant_name: str,
        tenant_id: str,
    ) -> Optional[str]:
        """Send a welcome email to a newly admin-created end user.

        SECURITY: the email NEVER contains the plaintext password. The
        creating administrator sets the password and shares it with the user
        over a secure out-of-band channel. Emailing credentials in cleartext
        is a compliance anti-pattern (email is not a secure transport) — this
        message only confirms the account and links to the portal.
        """
        login_url = f"{self.portal_url}?tenant={tenant_id}" if self.portal_url else ""
        subject = f"Welcome to {tenant_name} — Brutor AI Platform"
        html = self._build_welcome_html(
            display_name or username, username, tenant_name,
            login_url,
        )
        return await self._send(to_email, subject, html)

    def _build_welcome_html(
        self,
        name: str,
        username: str,
        tenant_name: str,
        login_url: str,
    ) -> str:
        # The button is there when there is a Portal to open.
        portal_button = f"""\
              <table cellpadding="0" cellspacing="0" style="margin:0 auto 24px;">
                <tr>
                  <td align="center" style="background:#1bb5e8;border-radius:8px;">
                    <a href="{login_url}"
                       style="display:inline-block;padding:14px 32px;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;">
                      Open User Portal
                    </a>
                  </td>
                </tr>
              </table>
""" if login_url else ""
        return f"""\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background:#f8f9fb;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f9fb;padding:40px 20px;">
    <tr>
      <td align="center">
        <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;border:1px solid #e8e9ea;border-left:3px solid #1bb5e8;overflow:hidden;">
          <tr>
            <td style="padding:36px 40px 0 40px;">
              <h1 style="margin:0;font-size:22px;font-weight:700;color:#000000;">
                Brutor AI Platform
              </h1>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 40px;">
              <p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:#4a4b4c;">
                Hi {name},
              </p>
              <p style="margin:0 0 24px;font-size:15px;line-height:1.6;color:#4a4b4c;">
                You've been added to <strong>{tenant_name}</strong> on the Brutor AI Platform.
                Your administrator will share your initial password with you
                separately — for your security we never send passwords by email.
              </p>
{portal_button}              <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f9fb;border-radius:8px;padding:16px;margin-bottom:24px;">
                <tr>
                  <td style="font-size:13px;font-weight:600;color:#000000;padding-bottom:8px;">
                    Your username:
                  </td>
                </tr>
                <tr>
                  <td style="font-size:14px;color:#4a4b4c;line-height:1.8;font-family:monospace;">
                    Username: {username}
                  </td>
                </tr>
              </table>
              <p style="margin:0;font-size:13px;line-height:1.5;color:#717273;">
                Your administrator will provide your initial password separately.
                Please change it after your first login for security.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:0 40px 24px;">
              <table width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #e8e9ea;padding-top:20px;">
                <tr>
                  <td style="font-size:13px;font-weight:600;color:#000000;padding-bottom:12px;">
                    Getting started:
                  </td>
                </tr>
                <tr>
                  <td style="font-size:13px;color:#4a4b4c;line-height:2.0;">
                    1. Log in to the User Portal with your username and the password your administrator provides<br>
                    2. Change your password from Profile &rarr; Account<br>
                    3. Start a chat conversation with AI models<br>
                    4. Connect to MCP servers for tool access
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:20px 40px;background:#f8f9fb;border-top:1px solid #e8e9ea;">
              <p style="margin:0;font-size:12px;color:#717273;text-align:center;">
                Brutor AI &mdash; Intelligent AI Governance
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    # ── 2FA setup confirmation email ───────────────────────────────

    async def send_2fa_enabled_email(
        self, to_email: str, name: str
    ) -> Optional[str]:
        """Notify user that 2FA was enabled on their account."""
        subject = "Two-Factor Authentication Enabled — Brutor AI"
        html = f"""\
<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f8f9fb;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f9fb;padding:40px 20px;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;border:1px solid #e8e9ea;border-left:3px solid #1bb5e8;overflow:hidden;">
        <tr><td style="padding:36px 40px 0 40px;">
          <h1 style="margin:0;font-size:22px;font-weight:700;color:#000000;">Brutor AI Platform</h1>
        </td></tr>
        <tr><td style="padding:24px 40px;">
          <p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:#4a4b4c;">Hi {name},</p>
          <p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:#4a4b4c;">
            Two-factor authentication has been <strong>enabled</strong> on your account.
            You will now need your authenticator app to log in.
          </p>
          <p style="margin:0;font-size:13px;line-height:1.5;color:#717273;">
            If you did not enable this, please contact your administrator immediately.
          </p>
        </td></tr>
        <tr><td style="padding:20px 40px;background:#f8f9fb;border-top:1px solid #e8e9ea;">
          <p style="margin:0;font-size:12px;color:#717273;text-align:center;">Brutor AI &mdash; Intelligent AI Governance</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
        return await self._send(to_email, subject, html)


# ── Singleton ──────────────────────────────────────────────────────

_email_service: Optional[EmailService] = None


def get_email_service() -> EmailService:
    """Get or create the singleton email service."""
    global _email_service
    if _email_service is None:
        _email_service = EmailService()
    return _email_service
