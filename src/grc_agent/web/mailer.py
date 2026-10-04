"""Email from GRC Flow (invites and password resets), sent through Resend.

Set in .env:
  RESEND_API_KEY   a Resend API key (resend.com > API Keys; "Sending access" is enough)
  GRC_MAIL_FROM    the sender, on a domain verified in Resend,
                   e.g. "GRC Flow <noreply@grc-flow.com>"

Without both, the app sends nothing: admins share starting passwords by hand, and the
forgot-password page says to ask an admin.
"""

from __future__ import annotations

import html
import os
import re

from grc_agent.connectors.base import ConnectorError, request

API_URL = "https://api.resend.com/emails"
EMAIL_RE = re.compile(r"^[^@\s<>\"',;]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


class MailError(Exception):
    """Sending failed. The message is safe to show (it never contains the API key)."""


def configured() -> bool:
    return bool(os.getenv("RESEND_API_KEY", "").strip() and os.getenv("GRC_MAIL_FROM", "").strip())


def valid_email(address: str) -> bool:
    return len(address) <= 254 and bool(EMAIL_RE.match(address))


def send(to: str, subject: str, lines: list[str], link: str = "", button: str = "") -> None:
    """Send a short plain email: a few paragraphs and, optionally, one button link."""
    if not configured():
        raise MailError("Email isn't set up on this server (RESEND_API_KEY, GRC_MAIL_FROM).")
    if not valid_email(to):
        raise MailError("That email address doesn't look right.")
    text = "\n\n".join([*lines, *([f"{button}: {link}"] if link else []), "GRC Flow"])
    paras = "".join(f'<p style="margin:0 0 16px">{html.escape(line)}</p>' for line in lines)
    cta = (
        f'<p style="margin:24px 0"><a href="{html.escape(link, quote=True)}" '
        'style="background:#2563eb;color:#fff;padding:10px 18px;border-radius:6px;'
        f'text-decoration:none;font-weight:600">{html.escape(button)}</a></p>'
        f'<p style="margin:0 0 16px;color:#555;font-size:13px">Or paste this link into your '
        f"browser: {html.escape(link)}</p>"
        if link
        else ""
    )
    body = (
        '<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;font-size:15px;'
        f'line-height:1.5;color:#111;max-width:560px">{paras}{cta}'
        '<p style="margin:24px 0 0;color:#555;font-size:13px">GRC Flow</p></div>'
    )
    try:
        resp = request(
            "POST",
            API_URL,
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY'].strip()}"},
            json_body={
                "from": os.environ["GRC_MAIL_FROM"].strip(),
                "to": [to],
                "subject": subject,
                "text": text,
                "html": body,
            },
        )
    except ConnectorError as exc:
        raise MailError(str(exc)) from None
    if not resp.ok:
        detail = resp.body.get("message") if isinstance(resp.body, dict) else ""
        suffix = f": {detail}" if detail else ""
        raise MailError(f"Resend refused the email ({resp.status}){suffix}.")
