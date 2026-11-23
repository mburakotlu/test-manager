from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from booking_notifications import build_booking_submitted_email, load_mail_settings, send_email

BOOKING = {
    "id": 7,
    "vehicle_label": "PT1023",
    "vehicle_vin": "VIN-1",
    "activity": "Campaign\r\nBcc: attacker@example.com",
    "period": "KW24-2026",
    "date_label": "12 Jun 2026 – 14 Jun 2026",
    "created_by": "Tester",
    "description": "Bring the logger <script>alert(1)</script>",
}
SMTP_ENV = {
    "BOOKING_NOTIFY_TO": "approver@example.com, lead@example.com",
    "MAIL_FROM": "planner@example.com",
    "SMTP_HOST": "smtp.example.com",
    "SMTP_USERNAME": "planner",
    "SMTP_PASSWORD": "secret-value",
}


class BookingNotificationTests(unittest.TestCase):
    def test_settings_are_off_without_recipients_and_validated_when_enabled(self) -> None:
        with patch.dict(os.environ, {"BOOKING_NOTIFY_TO": ""}, clear=True):
            self.assertFalse(load_mail_settings().enabled)
        with patch.dict(os.environ, {"BOOKING_NOTIFY_TO": "not-an-address", "MAIL_FROM": "a@example.com"}, clear=True):
            with self.assertRaisesRegex(ValueError, "invalid address"):
                load_mail_settings()
        with patch.dict(os.environ, {"BOOKING_NOTIFY_TO": "a@example.com", "MAIL_FROM": "b@example.com"}, clear=True):
            with self.assertRaisesRegex(ValueError, "SMTP_HOST"):
                load_mail_settings()

    def test_email_content_is_single_line_in_headers(self) -> None:
        with patch.dict(os.environ, SMTP_ENV, clear=True):
            settings = load_mail_settings()
        message = build_booking_submitted_email(
            settings, BOOKING, "https://planner.example.com/planner/review",
            approve_url="https://planner.example.com/approve", reject_url="https://planner.example.com/reject",
        )
        self.assertNotIn("\n", message["Subject"])
        self.assertIsNone(message["Bcc"])
        self.assertEqual(message["To"], "approver@example.com, lead@example.com")
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Booking ID: #7", body)
        self.assertIn("Submitted by: Tester", body)
        self.assertIn("Bring the logger", body)
        self.assertIn("https://planner.example.com/planner/review", body)
        html_body = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn('href="https://planner.example.com/approve"', html_body)
        self.assertIn('href="https://planner.example.com/reject"', html_body)
        self.assertIn("Preview and approve", html_body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_body)
        self.assertNotIn("<script>", html_body)

    def test_smtp_delivery_uses_starttls_and_login(self) -> None:
        with patch.dict(os.environ, SMTP_ENV, clear=True):
            settings = load_mail_settings()
        message = build_booking_submitted_email(
            settings, BOOKING, "https://planner.example.com/planner/review",
            approve_url="https://planner.example.com/approve", reject_url="https://planner.example.com/reject",
        )
        with patch("booking_notifications.smtplib.SMTP") as smtp:
            client = smtp.return_value
            self.assertEqual(send_email(settings, message), "sent via smtp.example.com")
        smtp.assert_called_once_with("smtp.example.com", 587, timeout=20)
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("planner", "secret-value")
        client.send_message.assert_called_once_with(message)


if __name__ == "__main__":
    unittest.main()
