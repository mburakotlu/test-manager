"""Email notifications for planner bookings.

Configuration (environment variables):
- BOOKING_NOTIFY_TO: comma-separated approver addresses. Notifications are off when empty.
- MAIL_FROM: sender address.
- SMTP_HOST / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD / SMTP_SECURITY (starttls, ssl, none).
- MAIL_OUTBOX_DIR: for local testing, write .eml files here instead of using SMTP.
- APP_BASE_URL: public app address used in email links, e.g. https://planner.example.com.
"""

from __future__ import annotations

import os
import html
import re
import smtplib
import ssl
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

_ADDRESS_PATTERN = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")


@dataclass(frozen=True)
class MailSettings:
    recipients: tuple[str, ...]
    sender: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_security: str
    outbox_dir: str
    app_base_url: str

    @property
    def enabled(self) -> bool:
        return bool(self.recipients)


def load_mail_settings() -> MailSettings:
    recipients = tuple(
        address.strip()
        for address in os.environ.get("BOOKING_NOTIFY_TO", "").split(",")
        if address.strip()
    )
    invalid = [address for address in recipients if not _ADDRESS_PATTERN.fullmatch(address)]
    if invalid:
        raise ValueError(f"BOOKING_NOTIFY_TO contains invalid address(es): {', '.join(invalid)}")
    security = os.environ.get("SMTP_SECURITY", "starttls").strip().lower() or "starttls"
    if security not in ("starttls", "ssl", "none"):
        raise ValueError("SMTP_SECURITY must be starttls, ssl, or none.")
    port_text = os.environ.get("SMTP_PORT", "").strip()
    try:
        port = int(port_text) if port_text else (465 if security == "ssl" else 587)
    except ValueError as exc:
        raise ValueError("SMTP_PORT must be a number.") from exc
    sender = os.environ.get("MAIL_FROM", "").strip()
    if recipients and not _ADDRESS_PATTERN.fullmatch(sender):
        raise ValueError("MAIL_FROM must be a valid sender address when BOOKING_NOTIFY_TO is set.")
    settings = MailSettings(
        recipients=recipients,
        sender=sender,
        smtp_host=os.environ.get("SMTP_HOST", "").strip(),
        smtp_port=port,
        smtp_username=os.environ.get("SMTP_USERNAME", "").strip(),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_security=security,
        outbox_dir=os.environ.get("MAIL_OUTBOX_DIR", "").strip(),
        app_base_url=os.environ.get("APP_BASE_URL", "").strip().rstrip("/"),
    )
    if settings.enabled and not (settings.smtp_host or settings.outbox_dir):
        raise ValueError("Set SMTP_HOST, or MAIL_OUTBOX_DIR for local testing, to send notifications.")
    return settings


def _single_line(value: object, limit: int = 120) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_booking_submitted_email(
    settings: MailSettings, booking: dict, review_url: str, recipients: tuple[str, ...] | None = None,
    *, approve_url: str, reject_url: str,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = _single_line(
        f"New vehicle booking to review: {booking['vehicle_label']} – {booking['activity']}", 200
    )
    message["From"] = settings.sender
    message["To"] = ", ".join(recipients or settings.recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=settings.sender.rsplit("@", 1)[-1])
    lines = [
        "A new vehicle booking is waiting for review.",
        "",
        f"Booking ID: #{booking['id']}",
        f"Vehicle: {_single_line(booking['vehicle_label'])}",
        f"VIN: {_single_line(booking.get('vehicle_vin') or 'Not recorded')}",
        f"Activity: {_single_line(booking['activity'], 300)}",
        f"Week: {_single_line(booking['period'])}",
        f"Date(s): {_single_line(booking.get('date_label') or 'Week only')}",
        f"Submitted by: {_single_line(booking['created_by'])}",
    ]
    if booking.get("description"):
        lines += ["", "Notes:", str(booking["description"]).strip()]
    lines += [
        "",
        f"Preview and approve with Jira creation: {approve_url}",
        f"Preview and reject: {reject_url}",
        f"Open the review queue: {review_url}",
        "",
        "Sign in, review the Jira ticket preview, and confirm to approve and create the ticket.",
        "The reject link opens a separate confirmation without creating a ticket.",
        "This message was sent automatically by the Vehicle Activity Planner.",
    ]
    message.set_content("\n".join(lines))
    escape = lambda value: html.escape(str(value), quote=True)
    details = [
        ("Booking ID", f"#{booking['id']}"),
        ("Vehicle", _single_line(booking["vehicle_label"])),
        ("VIN", _single_line(booking.get("vehicle_vin") or "Not recorded")),
        ("Activity", _single_line(booking["activity"], 300)),
        ("Week", _single_line(booking["period"])),
        ("Date(s)", _single_line(booking.get("date_label") or "Week only")),
        ("Submitted by", _single_line(booking["created_by"])),
    ]
    details_html = "".join(f"<li><strong>{escape(label)}:</strong> {escape(value)}</li>" for label, value in details)
    notes = (
        f'<p><strong>Notes:</strong><br>{escape(str(booking["description"]).strip()).replace(chr(10), "<br>")}</p>'
        if booking.get("description") else ""
    )
    message.add_alternative(
        '<html><body><p>A new vehicle booking is waiting for review.</p>'
        f'<ul>{details_html}</ul>{notes}'
        f'<p><a href="{escape(approve_url)}">Preview and approve with Jira creation</a> &nbsp; | &nbsp; '
        f'<a href="{escape(reject_url)}">Preview and reject</a></p>'
        f'<p><a href="{escape(review_url)}">Open the review queue</a></p>'
        '<p>Sign in, review the Jira ticket preview, and confirm to approve and create the ticket. '
        'The reject link opens a separate confirmation without creating a ticket.</p>'
        '</body></html>',
        subtype="html",
    )
    return message


def build_test_email(settings: MailSettings, requested_by: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "Vehicle Activity Planner test email"
    message["From"] = settings.sender
    message["To"] = ", ".join(settings.recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=settings.sender.rsplit("@", 1)[-1])
    message.set_content(
        "This is a test email from the Vehicle Activity Planner.\n"
        f"Requested by: {_single_line(requested_by)}\n\n"
        "If you received it, booking notifications are configured correctly."
    )
    return message


def send_email(settings: MailSettings, message: EmailMessage) -> str:
    """Send or store the message; returns a short description of where it went."""
    if settings.outbox_dir and not settings.smtp_host:
        outbox = Path(settings.outbox_dir).expanduser()
        outbox.mkdir(parents=True, exist_ok=True)
        path = outbox / f"{uuid.uuid4().hex}.eml"
        path.write_bytes(bytes(message))
        return f"saved to {path}"
    context = ssl.create_default_context()
    if settings.smtp_security == "ssl":
        client: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=context)
    else:
        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
    with client:
        if settings.smtp_security == "starttls":
            client.starttls(context=context)
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)
    return f"sent via {settings.smtp_host}"
