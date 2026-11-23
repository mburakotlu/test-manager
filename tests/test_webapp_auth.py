from __future__ import annotations

import os
import html
import json
import re
import tempfile
import unittest
from datetime import date, timedelta
from email.parser import BytesParser
from email.policy import default as default_policy
from pathlib import Path
from unittest.mock import patch

_TEMP_DIR = tempfile.TemporaryDirectory()
os.environ["APP_DATABASE_PATH"] = str(Path(_TEMP_DIR.name) / "webapp.sqlite3")
# Keep local .env email settings from affecting tests (load_dotenv does not override these).
for _mail_setting in ("BOOKING_NOTIFY_TO", "MAIL_OUTBOX_DIR", "SMTP_HOST", "APP_BASE_URL"):
    os.environ[_mail_setting] = ""

from planner_store import (
    add_vehicle,
    authenticate,
    connect,
    change_password as store_change_password,
    create_booking,
    create_first_admin,
    create_user,
    initialize_database,
    list_users,
    update_vehicle_groups,
    set_vehicle_blocked,
)
from webapp import app


class WebAppAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        initialize_database()
        create_first_admin("admin", "an-admin-password-long")
        cls.planner_password = create_user("planner", "planner")
        cls.automation_password = create_user("automation", "automation")
        cls.vehicle_id = add_vehicle(
            {
                "label": "PT1023",
                "vin": "TEST-VIN-1",
                "logger": "Yes",
                "series": "Baureihe test",
                "cluster": "Cluster test",
                "market": "Market test",
                "vr_stand": "VR test",
                "purpose": "Purpose test",
                "jira_asset_key": "PHUB-12345",
            }
        )
        update_vehicle_groups(
            [{"label": "PT1023", "group_name": "MLBevo", "group_order": "0", "display_order": "0"}]
        )
        app.config.update(TESTING=True)

    def setUp(self) -> None:
        self.client = app.test_client()
        with connect() as db:
            db.execute("DELETE FROM bookings")
            db.execute("UPDATE vehicles SET blocked_from = '', blocked_until = '', blocked_reason = ''")
            db.execute("DELETE FROM app_settings WHERE key = 'help_contact'")

    @classmethod
    def tearDownClass(cls) -> None:
        _TEMP_DIR.cleanup()

    def _csrf(self, response) -> str:
        match = re.search(rb'name="_csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def _login(self, username: str, password: str) -> None:
        page = self.client.get("/login")
        response = self.client.post(
            "/login",
            data={"username": username, "password": password, "_csrf_token": self._csrf(page)},
        )
        self.assertEqual(response.status_code, 302)
        if response.headers["Location"].endswith("/identify"):
            identity_page = self.client.get(response.headers["Location"])
            response = self.client.post(
                "/identify",
                data={"display_name": "Shared Planner Tester", "_csrf_token": self._csrf(identity_page)},
            )
            self.assertEqual(response.status_code, 302)

    def test_navigation_logo_links_to_sign_in_and_home(self) -> None:
        login = self.client.get("/login")
        self.assertIn(b'<a class="nav-brand" href="/login" aria-label="DLCM Planning Hub sign in">', login.data)
        self.assertIn(b'src="/static/app-logo.png"', login.data)
        logo = self.client.get("/static/app-logo.png")
        self.assertEqual(logo.status_code, 200)
        self.assertTrue(logo.data.startswith(b"\x89PNG\r\n\x1a\n"))
        logo.close()
        self._login("planner", self.planner_password)
        page = self.client.get("/planner")
        self.assertIn(b'<a class="nav-brand" href="/team" aria-label="DLCM Planning Hub home">', page.data)

    def test_planner_can_use_shared_workspace_but_not_admin_features(self) -> None:
        self._login("planner", self.planner_password)
        self.assertEqual(self.client.get("/planner").status_code, 200)
        self.assertEqual(self.client.get("/vehicles").status_code, 200)
        self.assertEqual(self.client.get("/planner/review").status_code, 200)
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertEqual(self.client.get("/admin/users").status_code, 403)
        self.assertEqual(self.client.get("/admin/audit").status_code, 403)
        self.assertEqual(self.client.get("/change-password").status_code, 403)
        planner_page = self.client.get("/planner")
        self.assertIn(b"Submit/Book an activity", planner_page.data)
        self.assertIn(b'href="/team"', planner_page.data)
        self.assertNotIn(b"Automation Team: Review bookings", planner_page.data)
        self.assertNotIn(b"Vehicle details", planner_page.data)
        self.assertNotIn(b"Vehicle Activity History", planner_page.data)
        self.assertIn(b"Team Workspace", planner_page.data)
        self.assertNotIn(b"<h1>Team Workspace</h1>", planner_page.data)
        self.assertNotIn(b"Manage Users", planner_page.data)
        self.assertNotIn(b"Access &amp; Change Log", planner_page.data)
        self.assertNotIn(b"Change Password", planner_page.data)
        self.assertIn(b"TEST-VIN-1", planner_page.data)
        self.assertIn(b'<optgroup label="MLBevo">', planner_page.data)
        response = self.client.post(
            "/planner/bookings/create",
            data={
                "vehicle_id": str(self.vehicle_id),
                "activity": "Shared login attribution campaign",
                "period": "KW47-2026",
                "planned_date": "2026-11-18",
                "checklist_template": "analysis_checklist",
                "_csrf_token": self._csrf(planner_page),
            },
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.get(response.headers["Location"])
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"waiting for team review", response.data)
        with connect() as db:
            created_booking = db.execute(
                "SELECT created_by FROM bookings WHERE activity = 'Shared login attribution campaign'"
            ).fetchone()
        self.assertEqual(created_booking["created_by"], "Shared Planner Tester")

        self.client.post("/logout", data={"_csrf_token": self._csrf(self.client.get("/planner"))})
        self._login("admin", "an-admin-password-long")
        audit_page = self.client.get("/admin/audit")
        self.assertIn(b"Shared Planner Tester", audit_page.data)
        self.assertIn(b"Signed in", audit_page.data)
        self.assertIn(b"Submitted booking", audit_page.data)

    def test_background_activity_is_admin_only(self) -> None:
        with patch("webapp.recent_log_entries", return_value=[
            {"time": "2026-11-19T14:00:00", "message": "Admin-only background event"},
        ]):
            login_page = self.client.get("/login")
            self.assertNotIn(b"Recent background activity", login_page.data)
            self.assertNotIn(b"Admin-only background event", login_page.data)
            self.assertEqual(self.client.get("/log").status_code, 302)
            for username, password in (
                ("planner", self.planner_password),
                ("automation", self.automation_password),
                ("admin", "an-admin-password-long"),
            ):
                with self.subTest(role=username):
                    self._login(username, password)
                    page = self.client.get("/team")
                    if username == "admin":
                        self.assertIn(b"Recent background activity", page.data)
                        self.assertIn(b"Admin-only background event", page.data)
                        self.assertIn(b'href="/log"', page.data)
                        self.assertEqual(self.client.get("/log").status_code, 200)
                    else:
                        self.assertNotIn(b"Recent background activity", page.data)
                        self.assertNotIn(b"Admin-only background event", page.data)
                        self.assertNotIn(b'href="/log"', page.data)
                        self.assertEqual(self.client.get("/log").status_code, 403)
                    self.client.post("/logout", data={"_csrf_token": self._csrf(page)})

    def test_automation_role_has_the_same_non_admin_workspace_access(self) -> None:
        self._login("automation", self.automation_password)
        page = self.client.get("/team")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Choose where you would like to go", page.data)
        automation_page = self.client.get("/team?section=automation")
        self.assertIn(b"Automation Team: Review bookings", automation_page.data)
        self.assertNotIn(b"Submit/Book an activity", automation_page.data)
        self.assertNotIn(b'href="/admin"', page.data)
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertEqual(self.client.get("/admin/users").status_code, 403)
        self.assertEqual(self.client.get("/change-password").status_code, 403)
        response = self.client.post(
            "/planner/bookings/create",
            data={
                "vehicle_id": str(self.vehicle_id),
                "activity": "Automation-created shared booking",
                "period": "KW47-2026",
                "planned_date": "2026-11-18",
                "checklist_template": "analysis_checklist",
                "_csrf_token": self._csrf(page),
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Automation-created shared booking", response.data)

    def test_reviewer_can_edit_booking_before_jira_creation(self) -> None:
        booking_id = create_booking(
            self.vehicle_id, "Incomplete booking", "KW47-2026", "", "", "", "Original Tester",
        )
        other_vehicle_id = add_vehicle({"label": "EDIT-TARGET", "vin": "EDIT-VIN"})
        self._login("automation", self.automation_password)
        review_page = self.client.get("/planner/review")
        edit_url = f"/planner/bookings/{booking_id}/edit"
        self.assertIn(f'href="{edit_url}"'.encode(), review_page.data)
        edit_page = self.client.get(edit_url)
        self.assertEqual(edit_page.status_code, 200)
        self.assertIn(b'value="Incomplete booking"', edit_page.data)
        form = {
            "vehicle_id": str(other_vehicle_id),
            "activity": "Completed booking",
            "period": "",
            "planned_date": "2026-06-12",
            "planned_end_date": "2026-06-13",
            "description": "Added missing details",
            "activity_color": "custom",
            "custom_activity_color": "#abcdef",
            "expected_revision": re.search(rb'name="expected_revision" value="(\d+)"', edit_page.data).group(1).decode(),
            "edit_token": re.search(rb'name="edit_token" value="([^"]+)"', edit_page.data).group(1).decode(),
            "_csrf_token": self._csrf(edit_page),
        }
        confirmation = self.client.post(edit_url, data=form)
        self.assertEqual(confirmation.status_code, 200)
        self.assertIn(b"Confirm weekend booking", confirmation.data)
        self.assertIn(f'name="edit_token" value="{form["edit_token"]}"'.encode(), confirmation.data)
        self.assertIn(f'name="expected_revision" value="{form["expected_revision"]}"'.encode(), confirmation.data)
        heartbeat = self.client.post(f"{edit_url}/heartbeat", data={
            "_csrf_token": self._csrf(confirmation), "edit_token": form["edit_token"],
        })
        self.assertEqual(heartbeat.status_code, 200)
        with connect() as db:
            self.assertEqual(
                db.execute("SELECT activity FROM bookings WHERE id = ?", (booking_id,)).fetchone()["activity"],
                "Incomplete booking",
            )
        confirmed = {**form, "activity_color": "#ABCDEF", "confirm_weekend": "1",
                     "_csrf_token": self._csrf(confirmation)}
        confirmed.pop("custom_activity_color")
        response = self.client.post(edit_url, data=confirmed)
        self.assertEqual(response.status_code, 302)
        with connect() as db:
            saved = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        self.assertEqual(saved["vehicle_id"], other_vehicle_id)
        self.assertEqual(saved["activity"], "Completed booking")
        self.assertEqual(saved["period"], "KW24-2026")
        self.assertEqual(saved["planned_end_date"], "2026-06-13")
        self.assertEqual(saved["description"], "Added missing details")
        self.assertEqual(saved["activity_color"], "#ABCDEF")
        self.assertEqual(saved["status"], "pending")
        self.assertEqual(saved["created_by"], "Original Tester")

        invalid = self.client.post(
            edit_url,
            data={**confirmed, "planned_end_date": "2026-06-01", "_csrf_token": self._csrf(review_page)},
        )
        self.assertEqual(invalid.status_code, 400)

        with connect() as db:
            db.execute("UPDATE bookings SET status = 'created', jira_issue_key = 'PHUB-1' WHERE id = ?", (booking_id,))
        self.assertEqual(self.client.get(edit_url).status_code, 409)
        blocked = self.client.post(edit_url, data={**confirmed, "_csrf_token": self._csrf(review_page)})
        self.assertEqual(blocked.status_code, 409)
        self.assertNotIn(f'href="{edit_url}"'.encode(), self.client.get("/planner/review?status=created").data)

        self.client.post("/logout", data={"_csrf_token": self._csrf(review_page)})
        self._login("admin", "an-admin-password-long")
        audit = self.client.get("/admin/audit")
        self.assertIn(f"Edited booking #{booking_id}".encode(), audit.data)
        self.assertIn(b"vehicle, activity, week, start date, end date, notes, color", audit.data)

    def test_booking_conflict_is_reported_without_creating_another_booking(self) -> None:
        create_booking(self.vehicle_id, "Existing booking", "KW24-2026", "2026-06-12", "", "", "First person")
        self._login("planner", self.planner_password)
        page = self.client.get("/planner")
        response = self.client.post("/planner/bookings/create", data={
            "vehicle_id": str(self.vehicle_id), "activity": "Conflicting booking",
            "planned_date": "2026-06-12", "_csrf_token": self._csrf(page),
        })
        self.assertIn(b"Vehicle already booked", response.data)
        self.assertIn(b"First person", response.data)
        with connect() as db:
            count = db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        self.assertEqual(count, 1)

    def test_other_editors_see_the_holder_and_changes_trigger_refresh_notice(self) -> None:
        booking_id = create_booking(self.vehicle_id, "Shared edit", "KW24-2026", "2026-06-12", "", "", "planner")
        self._login("automation", self.automation_password)
        edit_url = f"/planner/bookings/{booking_id}/edit"
        edit_page = self.client.get(edit_url)
        self.assertEqual(edit_page.status_code, 200)
        self.assertIn(b"data-edit-heartbeat", edit_page.data)
        self.assertIn(b"data-planner-revision", edit_page.data)
        version_before = self.client.get("/planner/updates").get_json()["revision"]

        other_client = app.test_client()
        login_page = other_client.get("/login")
        other_client.post("/login", data={
            "username": "admin", "password": "an-admin-password-long", "_csrf_token": self._csrf(login_page),
        })
        blocked = other_client.get(edit_url)
        self.assertEqual(blocked.status_code, 409)
        self.assertIn(b"automation is currently editing this booking", blocked.data)

        with connect() as db:
            db.execute("UPDATE bookings SET description = 'Updated elsewhere' WHERE id = ?", (booking_id,))
        self.assertGreater(self.client.get("/planner/updates").get_json()["revision"], version_before)
        revision = re.search(rb'name="expected_revision" value="(\d+)"', edit_page.data).group(1).decode()
        token = re.search(rb'name="edit_token" value="([^"]+)"', edit_page.data).group(1).decode()
        stale = self.client.post(edit_url, data={
            "vehicle_id": str(self.vehicle_id), "activity": "Stale edit", "period": "KW24-2026",
            "planned_date": "2026-06-12", "expected_revision": revision, "edit_token": token,
            "_csrf_token": self._csrf(edit_page),
        })
        self.assertEqual(stale.status_code, 409)
        self.assertIn(b"changed by someone else", stale.data)
        self.client.post(f"/planner/bookings/{booking_id}/edit/release", data={
            "_csrf_token": self._csrf(edit_page), "edit_token": token,
        })
        self.assertEqual(other_client.get(edit_url).status_code, 200)

    def test_old_tab_cannot_renew_or_release_new_edit_session(self) -> None:
        booking_id = create_booking(self.vehicle_id, "Shared edit", "KW24-2026", "2026-06-12", "", "", "planner")
        self._login("automation", self.automation_password)
        edit_url = f"/planner/bookings/{booking_id}/edit"
        first = self.client.get(edit_url)
        old_token = re.search(rb'name="edit_token" value="([^"]+)"', first.data).group(1).decode()
        second = self.client.get(edit_url)
        new_token = re.search(rb'name="edit_token" value="([^"]+)"', second.data).group(1).decode()
        self.assertNotEqual(old_token, new_token)
        csrf = self._csrf(second)
        self.assertEqual(self.client.post(f"{edit_url}/heartbeat", data={
            "_csrf_token": csrf, "edit_token": old_token,
        }).status_code, 409)
        self.client.post(f"{edit_url}/release", data={"_csrf_token": csrf, "edit_token": old_token})
        self.assertEqual(self.client.post(f"{edit_url}/heartbeat", data={
            "_csrf_token": csrf, "edit_token": new_token,
        }).status_code, 200)

    def test_email_review_requires_login_and_confirms_current_preview(self) -> None:
        booking_id = create_booking(self.vehicle_id, "Email review activity", "KW24-2026", "2026-06-12", "Details", "", "tester")
        approve_url = f"/planner/bookings/{booking_id}/email-review/approve"
        redirect_response = self.client.get(approve_url)
        self.assertEqual(redirect_response.status_code, 302)
        login_page = self.client.get(redirect_response.headers["Location"])
        form_action = html.unescape(re.search(r'data-login-form method="post" action="([^"]+)"', login_page.get_data(as_text=True)).group(1))
        signed_in = self.client.post(form_action, data={
            "username": "automation", "password": self.automation_password, "_csrf_token": self._csrf(login_page),
        })
        self.assertEqual(signed_in.headers["Location"], approve_url)
        selection = self.client.get(approve_url, follow_redirects=True)
        self.assertIn(b"Email review activity", selection.data)
        ticket_url = f"/planner/bookings/{booking_id}/create-ticket"
        page = self.client.get(ticket_url, query_string={"approval": "1", "checklist_template": "analysis_checklist"})
        self.assertIn(b"Approve and create Jira ticket", page.data)
        self.assertIn(b"Details", page.data)
        token = re.search(rb'name="preview_token" value="([^"]+)"', page.data).group(1).decode()
        form = {
            "approval": "1", "confirm": "1", "checklist_template": "analysis_checklist",
            "asset_key": "", "asset_field_available": "0", "preview_token": token,
            "_csrf_token": self._csrf(page),
        }
        self.assertEqual(self.client.post(ticket_url, data={**form, "preview_token": "invalid"}).status_code, 409)
        self.assertEqual(self.client.post(ticket_url, data={**form, "checklist_template": "daily_enna"}).status_code, 409)
        with connect() as db:
            self.assertEqual(db.execute("SELECT status FROM bookings WHERE id = ?", (booking_id,)).fetchone()["status"], "pending")
            db.execute("UPDATE bookings SET description = 'Updated details' WHERE id = ?", (booking_id,))
        stale = self.client.post(ticket_url, data=form)
        self.assertEqual(stale.status_code, 409)
        page = self.client.get(ticket_url, query_string={"approval": "1", "checklist_template": "analysis_checklist"})
        self.assertIn(b"Updated details", page.data)
        form["preview_token"] = re.search(rb'name="preview_token" value="([^"]+)"', page.data).group(1).decode()
        self.assertEqual(self.client.post(ticket_url, data={key: value for key, value in form.items() if key != "_csrf_token"}).status_code, 400)
        with patch("webapp.load_config") as config, patch("webapp.JiraClient") as jira, \
             patch("webapp.write_checklists_via_browser"):
            config.return_value.effective_web_base_url = "https://jira.example.test"
            jira.return_value.create_issue.return_value = {"key": "PHUB-999"}
            jira.return_value.get_checklist.return_value = "verified"
            result = self.client.post(ticket_url, data=form)
            jira.return_value.create_issue.assert_called_once()
        self.assertEqual(result.status_code, 200)
        self.assertIn(b"PHUB-999", result.data)
        self.assertEqual(self.client.post(ticket_url, data=form).status_code, 409)
        self.assertEqual(self.client.get(approve_url).status_code, 409)
        with connect() as db:
            booking = db.execute("SELECT status, jira_issue_key, reviewed_by FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        self.assertEqual((booking["status"], booking["jira_issue_key"], booking["reviewed_by"]), ("created", "PHUB-999", "automation"))

    def test_email_rejection_never_creates_ticket(self) -> None:
        booking_id = create_booking(self.vehicle_id, "Reject email", "KW24-2026", "2026-06-12", "", "", "tester")
        self._login("automation", self.automation_password)
        url = f"/planner/bookings/{booking_id}/email-review/reject"
        page = self.client.get(url)
        revision = re.search(rb'name="revision" value="(\d+)"', page.data).group(1).decode()
        result = self.client.post(url, data={"_csrf_token": self._csrf(page), "revision": revision})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn(b"Preview Jira ticket", result.data)
        with connect() as db:
            booking = db.execute("SELECT status, jira_issue_key FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        self.assertEqual((booking["status"], booking["jira_issue_key"]), ("rejected", ""))

    def test_combined_approval_failure_locks_against_duplicate_ticket(self) -> None:
        booking_id = create_booking(self.vehicle_id, "Jira failure", "KW24-2026", "2026-06-12", "", "", "tester")
        self._login("automation", self.automation_password)
        url = f"/planner/bookings/{booking_id}/create-ticket"
        preview = self.client.get(url, query_string={"approval": "1", "checklist_template": "analysis_checklist"})
        token = re.search(rb'name="preview_token" value="([^"]+)"', preview.data).group(1).decode()
        form = {
            "approval": "1", "confirm": "1", "checklist_template": "analysis_checklist",
            "asset_key": "", "asset_field_available": "0", "preview_token": token,
            "_csrf_token": self._csrf(preview),
        }
        with patch("webapp.load_config"), patch("webapp.JiraClient") as jira:
            jira.return_value.create_issue.side_effect = RuntimeError("Jira unavailable")
            response = self.client.post(url, data=form)
            self.assertEqual(response.status_code, 502)
            self.assertEqual(self.client.post(url, data=form).status_code, 409)
            jira.return_value.create_issue.assert_called_once()
        with connect() as db:
            booking = db.execute("SELECT status, reviewed_by FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        self.assertEqual((booking["status"], booking["reviewed_by"]), ("creation_error", "automation"))

    def test_email_review_link_survives_shared_planner_identity_prompt(self) -> None:
        booking_id = create_booking(self.vehicle_id, "Shared email", "KW24-2026", "2026-06-12", "", "", "tester")
        url = f"/planner/bookings/{booking_id}/email-review/approve"
        login = self.client.get(self.client.get(url).headers["Location"])
        action = html.unescape(re.search(r'data-login-form method="post" action="([^"]+)"', login.get_data(as_text=True)).group(1))
        signed_in = self.client.post(action, data={
            "username": "planner", "password": self.planner_password, "_csrf_token": self._csrf(login),
        })
        self.assertTrue(signed_in.headers["Location"].endswith("/identify"))
        identify = self.client.get(signed_in.headers["Location"])
        response = self.client.post("/identify", data={
            "display_name": "Shared Reviewer", "_csrf_token": self._csrf(identify),
        })
        self.assertEqual(response.headers["Location"], url)
        self.assertIn(b"Shared email", self.client.get(url, follow_redirects=True).data)

    def test_email_review_link_survives_required_admin_password_change(self) -> None:
        create_user("review-reset-admin", "admin")
        user = next(user for user in list_users() if user["username"] == "review-reset-admin")
        store_change_password(user["id"], "temporary-password-123", must_change=True)
        booking_id = create_booking(self.vehicle_id, "Password review", "KW24-2026", "2026-06-12", "", "", "tester")
        url = f"/planner/bookings/{booking_id}/email-review/approve"
        login = self.client.get(self.client.get(url).headers["Location"])
        action = html.unescape(re.search(r'data-login-form method="post" action="([^"]+)"', login.get_data(as_text=True)).group(1))
        signed_in = self.client.post(action, data={
            "username": "review-reset-admin", "password": "temporary-password-123", "_csrf_token": self._csrf(login),
        })
        self.assertTrue(signed_in.headers["Location"].endswith("/change-password"))
        change_page = self.client.get("/change-password")
        response = self.client.post("/change-password", data={
            "current_password": "temporary-password-123", "new_password": "replacement-password-123",
            "confirm_password": "replacement-password-123", "_csrf_token": self._csrf(change_page),
        })
        self.assertEqual(response.headers["Location"], url)

    def test_admin_can_rename_and_change_account_access(self) -> None:
        password = create_user("rename-target", "automation")
        user = next(user for user in list_users() if user["username"] == "rename-target")
        target = app.test_client()
        login_page = target.get("/login")
        target.post("/login", data={"username": "rename-target", "password": password, "_csrf_token": self._csrf(login_page)})
        self.assertEqual(target.get("/team").status_code, 200)
        self._login("admin", "an-admin-password-long")
        page = self.client.get("/admin/users")
        self.assertIn(b'<details class="admin-user-rename"><summary>Rename</summary>', page.data)
        self.assertIn(b'aria-label="New username for rename-target"', page.data)
        self.assertNotIn(b'<label>New username ', page.data)
        self.assertIn(b'name="active" value="1" checked', page.data)
        self.assertNotIn(b'>Deactivate</button>', page.data)
        token = self._csrf(page)
        rename_url = f'/admin/users/{user["id"]}/rename'
        self.assertEqual(self.client.post(rename_url, data={"username": "admin", "_csrf_token": token}).status_code, 400)
        self.assertEqual(self.client.post(rename_url, data={"username": "renamed-target"}).status_code, 400)
        renamed = self.client.post(rename_url, data={"username": "renamed-target", "_csrf_token": token})
        self.assertEqual(renamed.status_code, 302)
        self.assertIsNone(authenticate("rename-target", password))
        self.assertIsNotNone(authenticate("renamed-target", password))
        create_user("rename-target", "automation")
        self.assertEqual(target.get("/team").status_code, 302)
        self.assertEqual(self.client.post(
            f'/admin/users/{user["id"]}/access', data={"role": "automation", "_csrf_token": token},
        ).status_code, 302)
        self.assertIsNone(authenticate("renamed-target", password))
        self.assertEqual(self.client.post(
            f'/admin/users/{user["id"]}/access',
            data={"role": "automation", "active": "1", "_csrf_token": token},
        ).status_code, 302)
        self.assertIsNotNone(authenticate("renamed-target", password))
        self.assertEqual(self.client.get("/admin/audit").status_code, 200)
        audit = self.client.get("/admin/audit").get_data(as_text=True)
        self.assertIn("Renamed user account", audit)
        self.assertIn("Updated user access", audit)

    def test_new_booking_notifies_approvers_without_blocking_on_mail_errors(self) -> None:
        with tempfile.TemporaryDirectory() as outbox:
            mail_env = {
                "BOOKING_NOTIFY_TO": "approver@example.com, lead@example.com",
                "MAIL_FROM": "planner@example.com",
                "MAIL_OUTBOX_DIR": outbox,
                "APP_BASE_URL": "https://planner.example.com",
                "SMTP_HOST": "",
            }
            with patch.dict(os.environ, mail_env):
                self._login("planner", self.planner_password)
                planner_page = self.client.get("/planner")
                self.assertIn(b'name="notify_to" value="approver@example.com"', planner_page.data)
                self.assertIn(b'name="notify_to" value="lead@example.com"', planner_page.data)
                self.assertIn(b'<details class="approver-dropdown">', planner_page.data)
                self.assertIn(b"Select approver(s)</summary>", planner_page.data)
                self.assertEqual(self.client.get("/admin/email").status_code, 403)
                booking_form = {
                    "vehicle_id": str(self.vehicle_id),
                    "activity": "Emailed booking",
                    "period": "KW47-2026",
                    "_csrf_token": self._csrf(planner_page),
                }
                missing = self.client.post("/planner/bookings/create", data=booking_form)
                self.assertEqual(missing.status_code, 400)
                self.assertIn(b"Choose at least one approver", missing.data)
                outside = self.client.post(
                    "/planner/bookings/create", data={**booking_form, "notify_to": "outsider@example.com"}
                )
                self.assertEqual(outside.status_code, 400)
                with connect() as db:
                    self.assertIsNone(db.execute("SELECT 1 FROM bookings WHERE activity = 'Emailed booking'").fetchone())
                self.assertEqual(list(Path(outbox).glob("*.eml")), [])

                weekend_form = {
                    **booking_form, "period": "", "planned_date": "2026-06-13", "notify_to": "LEAD@example.com",
                }
                confirmation = self.client.post("/planner/bookings/create", data=weekend_form)
                self.assertIn(b'name="notify_to" value="lead@example.com"', confirmation.data)
                response = self.client.post(
                    "/planner/bookings/create",
                    data={**weekend_form, "notify_to": "lead@example.com", "confirm_weekend": "1",
                          "_csrf_token": self._csrf(confirmation)},
                )
                self.assertEqual(response.status_code, 302)
                emails = list(Path(outbox).glob("*.eml"))
                self.assertEqual(len(emails), 1)
                email_message = BytesParser(policy=default_policy).parsebytes(emails[0].read_bytes())
                content = email_message.get_body(preferencelist=("plain",)).get_content()
                self.assertEqual(email_message["To"], "lead@example.com")
                self.assertIn("Emailed booking", email_message["Subject"])
                self.assertIn("Emailed booking", content)
                self.assertIn("Submitted by: Shared Planner Tester", content)
                self.assertIn("https://planner.example.com/planner/review", content)
                self.assertRegex(content, r"https://planner\.example\.com/planner/bookings/\d+/email-review/approve")
                self.assertRegex(content, r"https://planner\.example\.com/planner/bookings/\d+/email-review/reject")

                with patch("webapp.send_email", side_effect=OSError("mail server down")):
                    response = self.client.post(
                        "/planner/bookings/create",
                        data={
                            "vehicle_id": str(self.vehicle_id),
                            "activity": "Booking despite mail failure",
                            "period": "KW47-2026",
                            "notify_to": "approver@example.com",
                            "_csrf_token": self._csrf(planner_page),
                        },
                    )
                self.assertEqual(response.status_code, 302)
                with connect() as db:
                    self.assertIsNotNone(
                        db.execute("SELECT 1 FROM bookings WHERE activity = 'Booking despite mail failure'").fetchone()
                    )
                self.assertEqual(self.client.get("/log").status_code, 403)

                self.client.post("/logout", data={"_csrf_token": self._csrf(planner_page)})
                self._login("admin", "an-admin-password-long")
                self.assertIn(b"notification failed (OSError)", self.client.get("/log").data)
                self.assertIn(b'href="/admin/email"', self.client.get("/admin").data)
                email_page = self.client.get("/admin/email")
                self.assertIn(b"approver@example.com", email_page.data)
                tested = self.client.post("/admin/email", data={"_csrf_token": self._csrf(email_page)})
                self.assertIn(b"Test email saved to", tested.data)
                self.assertEqual(len(list(Path(outbox).glob("*.eml"))), 2)

    def test_booking_form_previews_current_week(self) -> None:
        self._login("planner", self.planner_password)
        page = self.client.get("/planner").data.decode()
        today = date.today()
        iso_year, iso_week, _ = today.isocalendar()
        current_period = f"KW{iso_week:02d}-{iso_year}"
        monday = today - timedelta(days=today.weekday())
        self.assertIn(f"Current week: <strong>{current_period}</strong>", page)
        self.assertIn(f"{monday:%a %d %b} – {monday + timedelta(days=6):%a %d %b %Y}", page)
        self.assertIn(f'placeholder="{current_period}"', page)
        self.assertIn(f"useCurrentWeek(this, '{current_period}')", page)
        self.assertIn('data-booking-form', page)
        self.assertIn('data-booking-preview', page)
        self.assertIn('Availability is checked when you submit.', page)

    def test_section_searches_and_review_filter_are_rendered(self) -> None:
        create_booking(
            self.vehicle_id, "Filterable campaign", "KW24-2026", "2026-06-10",
            "", "", "Filter Tester",
        )
        self._login("admin", "an-admin-password-long")
        review = self.client.get("/planner/review?status=pending").data
        self.assertIn(b'data-filter-panel', review)
        self.assertRegex(review, rb'<tr data-filter-row data-filter-text="[^"]*Filterable campaign')
        self.assertIn(b'class="selected" aria-current=page href="/team?section=automation&status=actionable"', review)
        vehicles = self.client.get("/vehicles").data
        self.assertIn(b'data-vehicle-filter-panel', vehicles)
        self.assertRegex(vehicles, rb'data-vehicle-search="[^"]*PT1023[^"]*TEST-VIN-1')
        self.assertIn(b'data-filter-row', self.client.get("/admin/users").data)
        self.assertIn(b'data-filter-row', self.client.get("/admin/audit").data)

    def test_automation_filters_separate_new_bookings_and_review_decisions(self) -> None:
        statuses = ("pending", "approved", "rejected", "historical", "creating", "created", "creation_error")
        for index, status in enumerate(statuses, start=1):
            booking_id = create_booking(
                self.vehicle_id, f"Filter record {status}", "KW24-2026",
                f"2026-06-{index:02d}", "", "", "planner",
            )
            with connect() as db:
                db.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
        self._login("automation", self.automation_password)
        expected = {
            "actionable": {"pending"}, "approved": {"approved"}, "rejected": {"rejected"},
            "historical": {"historical"}, "all": set(statuses),
        }
        for selected, visible in expected.items():
            with self.subTest(filter=selected):
                response = self.client.get("/planner/review", query_string={"status": selected})
                self.assertEqual(response.status_code, 200)
                filters = re.search(rb'aria-label="Filter bookings">(.*?)</div>', response.data, re.DOTALL).group(1)
                self.assertEqual(
                    re.findall(rb'>([^<]+)</a>', filters),
                    [b"Actionable", b"Approved", b"Rejected", b"Historical", b"All"],
                )
                for status in statuses:
                    row = f">Filter record {status}<".encode()
                    if status in visible:
                        self.assertIn(row, response.data)
                    else:
                        self.assertNotIn(row, response.data)

    def test_booking_progress_and_calendar_without_excel_booker(self) -> None:
        booking_id = create_booking(
            self.vehicle_id, "Progress tracked run", "KW11-2026", "2026-03-10", "", "", "Progress Tester",
        )
        excel_id = create_booking(
            self.vehicle_id, "Imported excel run", "KW11-2026", "2026-03-12", "", "", "excel-import",
        )
        with connect() as db:
            db.execute("UPDATE bookings SET status = 'historical' WHERE id = ?", (excel_id,))
        self._login("automation", self.automation_password)
        progress_url = f"/planner/bookings/{booking_id}/progress"
        review = self.client.get("/planner/review")
        self.assertIn(b"<th>Progress</th>", review.data)
        self.assertNotIn(f'action="{progress_url}"'.encode(), review.data)
        pending = self.client.post(progress_url, data={"progress": "in_progress", "_csrf_token": self._csrf(review)})
        self.assertEqual(pending.status_code, 409)

        self.client.post(f"/planner/bookings/{booking_id}/review",
                         data={"decision": "approved", "_csrf_token": self._csrf(review)})
        review = self.client.get("/planner/review?status=approved")
        self.assertIn(f'action="{progress_url}"'.encode(), review.data)
        invalid = self.client.post(progress_url, data={"progress": "done", "_csrf_token": self._csrf(review)})
        self.assertEqual(invalid.status_code, 400)
        response = self.client.post(
            progress_url,
            data={"progress": "in_progress", "return_to": "automation", "_csrf_token": self._csrf(review)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("section=automation", response.headers["Location"])
        calendar = self.client.get("/planner/history?year=2026&view=calendar&month=2026-03").data
        self.assertIn(b"Booked by: Progress Tester", calendar)
        self.assertIn("▶ In progress".encode(), calendar)
        excel_event = calendar.split(b"<strong>Imported excel run</strong>", 1)[1].split(b"</span>", 1)[0]
        self.assertNotIn(b"Booked by", excel_event)
        self.assertIn(b"Recorded by: Not provided in Excel", excel_event)
        self.assertNotIn(b"Booked by: Excel", calendar)
        self.assertIn("▶ In progress".encode(), self.client.get("/planner/history?year=2026&view=grid").data)

        self.client.post(progress_url, data={"progress": "closed", "_csrf_token": self._csrf(review)})
        actionable = self.client.get("/planner/review").data
        self.assertIn(b"Progress tracked run (PT1023) marked Closed.", actionable)
        self.assertNotIn(b"Progress tracked run", actionable.split(b'id="review-bookings"', 1)[1])
        self.assertIn(b"Progress tracked run", self.client.get("/planner/review?status=approved").data)
        grid = self.client.get("/planner/history?year=2026&view=grid").data
        self.assertIn(b"grid-progress-closed", grid)
        self.assertIn("✓ Closed".encode(), grid)
        imported_label = grid.split(b"Imported excel run", 1)[0].rsplit(b"<span", 1)[1]
        self.assertNotIn(b"grid-progress-closed", imported_label)
        timeline = self.client.get(f"/planner/history/vehicle/{self.vehicle_id}").data
        self.assertIn("✓ Closed".encode(), timeline)

        self.client.post("/logout", data={"_csrf_token": self._csrf(review)})
        self._login("admin", "an-admin-password-long")
        audit = self.client.get("/admin/audit").data.decode()
        self.assertIn("Changed booking progress", audit)
        self.assertIn("In progress → Closed", audit)

    def test_workspace_home_links_to_each_section(self) -> None:
        self._login("planner", self.planner_password)
        home = self.client.get("/team")
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"Welcome, Shared Planner Tester", home.data)
        self.assertNotIn(b"<h1>Team Workspace</h1>", home.data)
        self.assertIn(b"<title>DLCM Planning Hub</title>", home.data)
        self.assertNotIn(b'id="submit-booking"', home.data)
        self.assertNotIn(b'href="/admin"', home.data)
        self.assertEqual(self.client.get("/admin").status_code, 403)
        nav = re.search(rb"<nav>(.*?)</nav>", home.data, re.DOTALL).group(1)
        self.assertIn(b'href="/team"', nav)
        self.assertIn(b"Sign out", nav)
        self.assertNotIn(b'section=', nav)
        for section, heading in (
            ("testing", b"Submit/Book an activity"),
            ("automation", b"Automation Team: Review bookings"),
            ("vehicles", b"Vehicle details"),
            ("history", b"Vehicle Activity History"),
        ):
            self.assertIn(f'class="workspace-card" href="/team?section={section}"'.encode(), home.data)
            page = self.client.get("/team", query_string={"section": section})
            self.assertEqual(page.status_code, 200)
            self.assertIn(heading, page.data)
            self.assertIn(b'href="/team"', page.data)
        self.assertEqual(self.client.get("/team?section=unknown").status_code, 400)

    def test_dashboard_uses_inclusive_planned_dates_and_keeps_activity_admin_only(self) -> None:
        self._login("planner", self.planner_password)
        today = date.today()
        vehicles = [
            {"id": 1, "label": "Reserved car", "active": 1, "blocked_from": ""},
            {"id": 2, "label": "Available car", "active": 1, "blocked_from": ""},
            {"id": 3, "label": "Blocked car", "active": 1, "blocked_from": today.isoformat(), "blocked_until": ""},
            {"id": 4, "label": "Progress car", "active": 1, "blocked_from": ""},
            {"id": 5, "label": "Inactive car", "active": 0, "blocked_from": ""},
        ]
        def booking(number, vehicle_id, start, end, status="pending", progress="not_started"):
            return {
                "id": number, "vehicle_id": vehicle_id, "vehicle_label": f"Car {vehicle_id}",
                "activity": f"Activity {number}", "created_by": "<User>", "period": "",
                "planned_date": start.isoformat(), "planned_end_date": end.isoformat(),
                "status": status, "progress": progress, "updated_at": today.isoformat(),
            }
        bookings = [
            booking(1, 1, today - timedelta(days=1), today),
            booking(2, 1, today, today, status="historical"),
            booking(3, 4, today, today, status="approved", progress="in_progress"),
            booking(4, 2, today, today, status="rejected"),
        ] + [
            booking(10 + n, 2, today + timedelta(days=n), today + timedelta(days=n))
            for n in range(1, 7)
        ]
        with patch("webapp.list_vehicles", return_value=vehicles), \
             patch("webapp.list_bookings", return_value=bookings) as list_mock, \
             patch("webapp.list_recent_booking_events") as events:
            page = self.client.get("/team")
        list_mock.assert_called_once_with(limit=-1)
        events.assert_not_called()
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        for label, value in (("Total Vehicles", 5), ("Active Bookings", 3),
                             ("Available Vehicles", 1), ("Upcoming Reservations", 6)):
            self.assertRegex(html, rf"(?s){label}</span>.*?<strong>{value}</strong>")
        self.assertIn("50% of active vehicles have a booking today", html)
        self.assertIn('status-not-available">Not available', html)
        self.assertIn('status-in-use">In use', html)
        self.assertIn('status-reserved">Reserved', html)
        self.assertIn("&lt;User&gt;", html)
        self.assertNotIn(">Inactive car</strong>", html)
        self.assertIn("Activity 1", html)
        self.assertNotIn("Activity 4", html)
        self.assertIn((today + timedelta(days=5)).isoformat(), html)
        self.assertNotIn((today + timedelta(days=6)).isoformat(), html)
        self.assertIn("Recently updated team bookings and their current status.", html)

    def test_dashboard_recent_activity_is_admin_only(self) -> None:
        self._login("admin", "an-admin-password-long")
        events = [{"actor": "Test user", "action": "Edited booking #123", "details": "Changed fields: dates", "created_at": "2026-01-02 12:00:00"}]
        with patch("webapp.list_recent_booking_events", return_value=events):
            page = self.client.get("/team")
        self.assertIn(b"Edited booking #123", page.data)
        self.assertIn(b"Test user", page.data)
        self.assertIn(b"2026-01-02", page.data)
        self.assertNotIn(b"Recently updated team bookings", page.data)

    def test_planner_dashboard_shows_recent_team_booking_statuses(self) -> None:
        self._login("planner", self.planner_password)
        bookings = [
            {"id": n, "vehicle_id": self.vehicle_id, "vehicle_label": "Team car",
             "activity": f"Team booking {n}", "created_by": "<Team member>", "period": "",
             "planned_date": "2026-02-02", "planned_end_date": "2026-02-02",
             "status": "rejected" if n == 2 else "pending", "progress": "not_started",
             "updated_at": f"2026-01-0{7 - n} 10:00:00"}
            for n in range(1, 7)
        ]
        with patch("webapp.list_bookings", return_value=bookings), \
             patch("webapp.list_recent_booking_events") as admin_events:
            page = self.client.get("/team")
        admin_events.assert_not_called()
        activity = page.get_data(as_text=True).split('aria-labelledby="activity-title"', 1)[1].split("</section>", 1)[0]
        self.assertLess(activity.index("Team booking 1"), activity.index("Team booking 2"))
        self.assertIn("Current status: Rejected", activity)
        self.assertIn("Booked by &lt;Team member&gt;", activity)
        self.assertIn("2026-01-06", activity)
        self.assertNotIn("Team booking 6", activity)
        self.assertIn("All bookings", activity)

    def test_dashboard_vehicle_filter_and_pagination(self) -> None:
        self._login("planner", self.planner_password)
        today = date.today().isoformat()
        vehicles = [
            {"id": n, "label": f"Vehicle {n:02}", "active": 1,
             "blocked_from": today if n == 1 else "", "blocked_until": ""}
            for n in range(1, 25)
        ]
        bookings = [
            {"id": n, "vehicle_id": n, "vehicle_label": f"Vehicle {n:02}",
             "activity": f"Run {n}", "created_by": "Tester", "period": "",
             "planned_date": today, "planned_end_date": today, "status": "approved",
             "progress": "in_progress" if n == 3 else "not_started", "updated_at": today}
            for n in (1, 2, 3)
        ]

        def table(page) -> str:
            return page.get_data(as_text=True).split('class="dashboard-table"', 1)[1].split("</table>", 1)[0]

        with patch("webapp.list_vehicles", return_value=vehicles), patch("webapp.list_bookings", return_value=bookings):
            first = self.client.get("/team")
            self.assertIn("Showing 1&ndash;10 of 24 vehicles", first.get_data(as_text=True))
            self.assertIn("Vehicle 10</strong>", table(first))
            self.assertNotIn("Vehicle 11</strong>", table(first))
            second = self.client.get("/team?vehicle_page=2")
            self.assertIn("Showing 11&ndash;20 of 24 vehicles", second.get_data(as_text=True))
            self.assertIn("Vehicle 11</strong>", table(second))
            self.assertNotIn("Vehicle 10</strong>", table(second))
            available = self.client.get("/team?vehicle_status=available&vehicle_limit=15&vehicle_page=2")
            self.assertIn("Showing 16&ndash;21 of 21 vehicles", available.get_data(as_text=True))
            self.assertEqual(table(available).count('status-available">Available'), 6)
            self.assertIn('vehicle_status=available&amp;vehicle_limit=15&amp;vehicle_page=1', available.get_data(as_text=True))
            for status, expected in (("blocked", "Vehicle 01"), ("reserved", "Vehicle 02"), ("in_use", "Vehicle 03")):
                page = self.client.get(f"/team?vehicle_status={status}")
                self.assertIn(f"{expected}</strong>", table(page))
                self.assertIn("Showing 1&ndash;1 of 1 vehicles", page.get_data(as_text=True))
            all_vehicles = self.client.get("/team?vehicle_limit=all")
            self.assertEqual(table(all_vehicles).count("<tr>"), 25)
            self.assertRegex(all_vehicles.get_data(as_text=True), r"(?s)Total Vehicles</span>.*?<strong>24</strong>")
            self.assertEqual(self.client.get("/team?vehicle_status=invalid").status_code, 400)
            self.assertEqual(self.client.get("/team?vehicle_limit=0").status_code, 400)
            self.assertEqual(self.client.get("/team?vehicle_page=0").status_code, 400)
        with patch("webapp.list_vehicles", return_value=vehicles[3:4]), patch("webapp.list_bookings", return_value=[]):
            empty_filter = self.client.get("/team?vehicle_status=blocked")
            self.assertIn(b"No vehicles match this status.", empty_filter.data)

    def test_dashboard_empty_fleet_has_no_utilization_percentage(self) -> None:
        self._login("planner", self.planner_password)
        with patch("webapp.list_vehicles", return_value=[]), patch("webapp.list_bookings", return_value=[]):
            page = self.client.get("/team")
        self.assertIn(b"No active vehicles to calculate utilization.", page.data)
        self.assertIn(b"No activities planned for today.", page.data)
        self.assertIn(b"No upcoming reservations.", page.data)

    def test_navigation_search_respects_roles_and_profile_signout(self) -> None:
        self.assertEqual(self.client.get("/search?q=vehicles").status_code, 302)
        self._login("planner", self.planner_password)
        home = self.client.get("/team")
        nav = re.search(rb"<nav>(.*?)</nav>", home.data, re.DOTALL).group(1)
        self.assertIn(b'aria-label="Search app pages"', nav)
        self.assertIn(b'<details class="nav-profile" data-nav-profile>', nav)
        self.assertIn(b"Shared Planner Tester", nav)
        self.assertIn(b"Account: planner", nav)
        self.assertIn(b'action="/logout"', nav)
        self.assertIn(b'name="_csrf_token"', nav)
        vehicles = self.client.get("/search?q=VEHICLE")
        self.assertIn(b'href="/vehicles">Vehicles</a>', vehicles.data)
        self.assertNotIn(b'href="/admin/users"', self.client.get("/search?q=users").data)
        self.assertIn(b"Calendar</a>", self.client.get("/search?q=calendar").data)
        self.assertIn(b"No matching pages or sections found.", self.client.get("/search?q=unfindable").data)
        self.assertIn(b"Enter a page or section to search.", self.client.get("/search?q=").data)
        self.assertIn(b"&lt;script&gt;", self.client.get("/search?q=%3Cscript%3E").data)
        self.client.post("/logout", data={"_csrf_token": self._csrf(home)})
        self.assertEqual(self.client.get("/search?q=vehicles").status_code, 302)
        self._login("admin", "an-admin-password-long")
        result = self.client.get("/search?q=users")
        self.assertIn(b'href="/admin/users">Manage Users</a>', result.data)

    def test_help_contact_can_be_updated_only_by_admin_and_is_escaped(self) -> None:
        self._login("admin", "an-admin-password-long")
        page = self.client.get("/admin/help-contact")
        self.assertIn(b"Admin: Not configured", page.data)
        csrf = self._csrf(page)
        self.assertEqual(self.client.post("/admin/help-contact", data={"name": "Admin"}).status_code, 400)
        invalid = self.client.post("/admin/help-contact", data={"name": "bad\nname", "_csrf_token": csrf})
        self.assertEqual(invalid.status_code, 400)
        saved = self.client.post("/admin/help-contact", data={"name": " <Admin> ", "_csrf_token": csrf})
        self.assertEqual(saved.status_code, 302)
        self.assertIn(b"Admin: &lt;Admin&gt;", self.client.get("/team").data)
        self.client.post("/logout", data={"_csrf_token": csrf})
        self._login("planner", self.planner_password)
        home = self.client.get("/team")
        self.assertIn(b"Admin: &lt;Admin&gt;", home.data)
        self.assertIn(b"contact your admin", home.data)
        self.assertIn(b'data-nav-help', home.data)
        self.assertIn(b'data-theme-select', home.data)
        self.assertEqual(self.client.get("/admin/help-contact").status_code, 403)
        self.assertEqual(
            self.client.post("/admin/help-contact", data={"name": "Other", "_csrf_token": self._csrf(home)}).status_code,
            403,
        )
        self.client.post("/logout", data={"_csrf_token": self._csrf(home)})
        self._login("admin", "an-admin-password-long")
        page = self.client.get("/admin/help-contact")
        self.client.post("/admin/help-contact", data={"name": "New admin", "_csrf_token": self._csrf(page)})
        home = self.client.get("/team")
        self.assertIn(b"Admin: New admin", home.data)
        self.assertNotIn(b"Admin: &lt;Admin&gt;", home.data)

    def test_ticket_preview_shows_line_by_line_details_for_both_approval_paths(self) -> None:
        booking_id = create_booking(
            self.vehicle_id, "Inspect <brakes>", "KW47-2026", "2026-11-18",
            "First line\nSecond <line>", "analysis_checklist", "planner",
        )
        self._login("automation", self.automation_password)
        url = f"/planner/bookings/{booking_id}/create-ticket"
        with patch("webapp.render_test_checklist", return_value="# Day 1\n- Inspect <parts>\n+ Done"):
            pending = self.client.get(url, query_string={"approval": "1"})
            with connect() as db:
                db.execute("UPDATE bookings SET status = 'approved' WHERE id = ?", (booking_id,))
            approved = self.client.get(url)
        for preview in (pending, approved):
            self.assertEqual(preview.status_code, 200)
            self.assertIn(b"<dt>Submitted by</dt><dd>planner</dd>", preview.data)
            self.assertIn(b"<dt>VIN</dt><dd>TEST-VIN-1</dd>", preview.data)
            self.assertIn(b"<dt>Notes</dt><dd>First line\nSecond &lt;line&gt;</dd>", preview.data)
            self.assertIn(b"Inspect &lt;brakes&gt;", preview.data)
            self.assertIn(b'<li class="ticket-checklist-heading"># Day 1</li>', preview.data)
            self.assertIn(b'<li class="ticket-checklist-item">- Inspect &lt;parts&gt;</li>', preview.data)
            self.assertIn(b'<li class="ticket-checklist-item">+ Done</li>', preview.data)
            self.assertIn(b"View exact Jira description", preview.data)
            self.assertIn(b"Changes apply only after you select Preview checklist again.", preview.data)
        self.assertIn(b"Approve and create Jira ticket", pending.data)
        self.assertIn(b"Confirm and create Jira ticket", approved.data)

    def test_approval_opens_ticket_preview_without_creating_a_ticket(self) -> None:
        booking_id = create_booking(
            self.vehicle_id, "Preview on approval", "KW47-2026", "2026-11-18",
            "Booking notes", "", "planner",
        )
        self._login("automation", self.automation_password)
        review = self.client.get("/planner/review")
        with patch("webapp.JiraClient") as jira:
            response = self.client.post(
                f"/planner/bookings/{booking_id}/review",
                data={"decision": "approved", "_csrf_token": self._csrf(review)},
                follow_redirects=True,
            )
            jira.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request.path, f"/planner/bookings/{booking_id}/create-ticket")
        self.assertIn(b"Preview Jira ticket", response.data)
        self.assertIn(b"<dt>Notes</dt><dd>Booking notes</dd>", response.data)
        self.assertIn(b"Choose checklist template", response.data)
        self.assertNotIn(b"Confirm and create Jira ticket", response.data)
        with connect() as db:
            booking = db.execute("SELECT status, jira_issue_key FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        self.assertEqual((booking["status"], booking["jira_issue_key"]), ("approved", ""))

    def test_checklist_is_chosen_during_ticket_preview(self) -> None:
        self._login("planner", self.planner_password)
        planner_page = self.client.get("/planner")
        self.assertNotIn(b'name="checklist_template"', planner_page.data)
        response = self.client.post(
            "/planner/bookings/create",
            data={
                "vehicle_id": str(self.vehicle_id),
                "activity": "Automation checklist selection",
                "period": "KW47-2026",
                "checklist_template": "daily_enna",
                "_csrf_token": self._csrf(planner_page),
            },
        )
        self.assertEqual(response.status_code, 302)
        with connect() as db:
            booking = db.execute(
                "SELECT id, checklist_template FROM bookings WHERE activity = ?",
                ("Automation checklist selection",),
            ).fetchone()
        self.assertEqual(booking["checklist_template"], "")
        booking_id = booking["id"]

        self.client.post("/logout", data={"_csrf_token": self._csrf(self.client.get("/planner"))})
        self._login("automation", self.automation_password)
        review_page = self.client.get("/planner/review")
        response = self.client.post(
            f"/planner/bookings/{booking_id}/review",
            data={"decision": "approved", "_csrf_token": self._csrf(review_page)},
        )
        self.assertEqual(response.status_code, 302)
        url = f"/planner/bookings/{booking_id}/create-ticket"
        selection = self.client.get(url)
        self.assertIn(b"Choose checklist template", selection.data)
        self.assertNotIn(b"Confirm and create Jira ticket", selection.data)
        invalid = self.client.get(url, query_string={"checklist_template": "../analysis_checklist"})
        self.assertEqual(invalid.status_code, 400)
        missing = self.client.post(url, data={"confirm": "1", "_csrf_token": self._csrf(review_page)})
        self.assertEqual(missing.status_code, 400)
        invalid_post = self.client.post(
            url,
            data={"confirm": "1", "checklist_template": "../analysis_checklist", "_csrf_token": self._csrf(review_page)},
        )
        self.assertEqual(invalid_post.status_code, 400)

        preview = self.client.get(url, query_string={"checklist_template": "daily_enna"})
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"Checklist preview (daily_enna)", preview.data)
        self.assertIn(b'name="checklist_template" value="daily_enna"', preview.data)
        with patch("webapp.load_config") as config, patch("webapp.JiraClient") as jira, \
             patch("webapp.write_checklists_via_browser") as write_checklists:
            config.return_value.effective_web_base_url = "https://jira.example.test"
            jira.return_value.create_issue.return_value = {"key": "PHUB-999"}
            jira.return_value.get_checklist.return_value = "verified"
            response = self.client.post(
                url,
                data={"confirm": "1", "checklist_template": "daily_enna", "_csrf_token": self._csrf(preview)},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Checklist template: daily_enna", jira.return_value.create_issue.call_args.args[3])
        self.assertEqual(write_checklists.call_args.args[1][0][0], "PHUB-999")
        self.assertIn("Stop the Tests on Tec PC", write_checklists.call_args.args[1][0][1])
        with connect() as db:
            saved = db.execute("SELECT checklist_template, status FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        self.assertEqual(saved["checklist_template"], "daily_enna")
        self.assertEqual(saved["status"], "created")

    def test_existing_booking_template_is_preserved_and_can_be_changed_in_preview(self) -> None:
        booking_id = create_booking(
            self.vehicle_id, "Existing template selection", "KW47-2026", "", "",
            "analysis_checklist", "planner",
        )
        self._login("automation", self.automation_password)
        review_page = self.client.get("/planner/review")
        response = self.client.post(
            f"/planner/bookings/{booking_id}/review",
            data={"decision": "approved", "_csrf_token": self._csrf(review_page)},
        )
        self.assertEqual(response.status_code, 302)
        url = f"/planner/bookings/{booking_id}/create-ticket"
        preview = self.client.get(url)
        self.assertIn(b"Checklist preview (analysis_checklist)", preview.data)
        changed = self.client.get(url, query_string={"checklist_template": "daily_enna"})
        self.assertIn(b"Checklist preview (daily_enna)", changed.data)
        with connect() as db:
            saved = db.execute("SELECT checklist_template FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        self.assertEqual(saved["checklist_template"], "analysis_checklist")

    def test_ticket_preview_links_selected_asset_and_updates_its_history(self) -> None:
        booking_id = create_booking(
            self.vehicle_id, "Asset link test", "KW47-2026", "2026-11-18", "Test notes",
            "analysis_checklist", "planner",
        )
        with connect() as db:
            db.execute("UPDATE bookings SET status = 'approved' WHERE id = ?", (booking_id,))
        self._login("automation", self.automation_password)
        url = f"/planner/bookings/{booking_id}/create-ticket"
        asset = {
            "id": 123, "key": "PHUB-123", "label": "Selected vehicle", "object_type": "FZG",
            "attributes": {"VIN": ["ASSET-VIN"], "Historie (JSON)": ['[{"ticket":"PHUB-OLD","other":"kept"}]']},
        }
        with patch("webapp.load_config") as config, patch("webapp.JiraClient") as jira, \
             patch("webapp.history_attribute_id", return_value=42), \
             patch("webapp.write_checklists_via_browser"):
            config.return_value.effective_web_base_url = "https://jira.example.test"
            client = jira.return_value
            client.list_test_vehicles.return_value = [{"key": "PHUB-123", "label": "Selected vehicle"}]
            client.get_insight_object.return_value = asset
            client.get_issue_create_fields.return_value = {"customfield_15815": {}}
            client.create_issue.return_value = {"key": "PHUB-999"}
            client.get_checklist.return_value = "verified"

            choices = self.client.get(url, query_string={"find_assets": "1", "checklist_template": "analysis_checklist"})
            self.assertIn(b'Selected vehicle (PHUB-123)', choices.data)
            preview = self.client.get(url, query_string={"checklist_template": "analysis_checklist", "asset_choice": "PHUB-123"})
            self.assertIn(b"Selected Assets vehicle:", preview.data)
            self.assertIn(b"ASSET-VIN", preview.data)
            self.assertIn(b"will include the Jira Assets vehicle link", preview.data)
            response = self.client.post(url, data={
                "confirm": "1", "checklist_template": "analysis_checklist", "asset_key": "PHUB-123",
                "asset_field_available": "1", "_csrf_token": self._csrf(preview),
            })
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Assets vehicle history was updated", response.data)
            self.assertEqual(client.create_issue.call_args.kwargs["extra_fields"]["customfield_15815"], [{"key": "PHUB-123"}])
            update = client.update_insight_object_attributes.call_args
            self.assertEqual(update.args[0], 123)
            history = json.loads(update.args[1][42][0])
            self.assertEqual(history[0]["other"], "kept")
            self.assertEqual(history[1]["ticket"], "PHUB-999")
            self.assertEqual(history[1]["activity"], "Asset link test")

    def test_ticket_warns_when_asset_field_or_history_is_unavailable(self) -> None:
        booking_id = create_booking(
            self.vehicle_id, "Asset fallback test", "KW47-2026", "", "",
            "analysis_checklist", "planner",
        )
        with connect() as db:
            db.execute("UPDATE bookings SET status = 'approved' WHERE id = ?", (booking_id,))
        self._login("automation", self.automation_password)
        url = f"/planner/bookings/{booking_id}/create-ticket"
        with patch("webapp.load_config") as config, patch("webapp.JiraClient") as jira, \
             patch("webapp.history_attribute_id", return_value=None), \
             patch("webapp.write_checklists_via_browser"):
            config.return_value.effective_web_base_url = "https://jira.example.test"
            client = jira.return_value
            client.get_insight_object.return_value = {
                "id": 123, "key": "PHUB-123", "label": "Asset vehicle", "object_type": "FZG",
                "attributes": {},
            }
            client.get_issue_create_fields.return_value = {}
            client.create_issue.return_value = {"key": "PHUB-998"}
            client.get_checklist.return_value = "verified"
            preview = self.client.get(url, query_string={"checklist_template": "analysis_checklist", "asset_key": "PHUB-123"})
            self.assertIn(b"will not have a formal Assets link", preview.data)
            response = self.client.post(url, data={
                "confirm": "1", "checklist_template": "analysis_checklist", "asset_key": "PHUB-123",
                "asset_field_available": "0", "_csrf_token": self._csrf(preview),
            })
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Assets history update failed", response.data)
            self.assertIn(b"Do not create the ticket again", response.data)
            self.assertNotIn("customfield_15815", client.create_issue.call_args.kwargs["extra_fields"])
            self.assertEqual(client.create_issue.call_count, 1)
            with connect() as db:
                booking = db.execute("SELECT status, jira_issue_key FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            self.assertEqual((booking["status"], booking["jira_issue_key"]), ("created", "PHUB-998"))

    def test_asset_selection_rejects_wrong_type_and_changed_creation_fields(self) -> None:
        booking_id = create_booking(
            self.vehicle_id, "Asset validation test", "KW47-2026", "", "",
            "analysis_checklist", "planner",
        )
        with connect() as db:
            db.execute("UPDATE bookings SET status = 'approved' WHERE id = ?", (booking_id,))
        self._login("automation", self.automation_password)
        url = f"/planner/bookings/{booking_id}/create-ticket"
        with patch("webapp.load_config"), patch("webapp.JiraClient") as jira:
            client = jira.return_value
            client.get_insight_object.return_value = {
                "id": 123, "key": "PHUB-123", "label": "Not a vehicle", "object_type": "Other",
                "attributes": {},
            }
            invalid = self.client.get(url, query_string={"checklist_template": "analysis_checklist", "asset_key": "PHUB-123"})
            self.assertEqual(invalid.status_code, 503)
            self.assertIn(b"Could not verify the selected Assets vehicle", invalid.data)
            client.create_issue.assert_not_called()

            client.get_insight_object.return_value["object_type"] = "FZG"
            client.get_issue_create_fields.return_value = {"customfield_15815": {}}
            preview = self.client.get(url, query_string={"checklist_template": "analysis_checklist", "asset_key": "PHUB-123"})
            client.get_issue_create_fields.return_value = {}
            changed = self.client.post(url, data={
                "confirm": "1", "checklist_template": "analysis_checklist", "asset_key": "PHUB-123",
                "asset_field_available": "1", "_csrf_token": self._csrf(preview),
            })
            self.assertEqual(changed.status_code, 409)
            self.assertIn(b"Preview the ticket again", changed.data)
            client.create_issue.assert_not_called()
            with connect() as db:
                status = db.execute("SELECT status FROM bookings WHERE id = ?", (booking_id,)).fetchone()["status"]
            self.assertEqual(status, "approved")

    def test_admin_can_open_user_management(self) -> None:
        self._login("admin", "an-admin-password-long")
        home = self.client.get("/")
        self.assertEqual(home.status_code, 302)
        self.assertIn("/team", home.headers["Location"])
        review = self.client.get("/planner/review")
        self.assertIn(b"Automation Team: Review bookings", review.data)
        self.assertIn(b'href="/team"', review.data)
        admin_home = self.client.get("/team")
        self.assertIn(b'class="workspace-card" href="/admin"', admin_home.data)
        nav = re.search(rb"<nav>(.*?)</nav>", admin_home.data, re.DOTALL).group(1)
        self.assertNotIn(b"Manage Users", nav)
        self.assertNotIn(b"Access &amp; Change Log", nav)
        self.assertNotIn(b"Change Password", nav)
        admin_page = self.client.get("/admin")
        self.assertEqual(admin_page.status_code, 200)
        for path, label in (
            ("/admin/users", b"Manage Users"),
            ("/admin/audit", b"Access &amp; Change Log"),
            ("/change-password", b"Change Password"),
        ):
            self.assertIn(f'href="{path}"'.encode(), admin_page.data)
            self.assertIn(label, admin_page.data)
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get("/admin/users").status_code, 200)
        vehicle_page = self.client.get("/vehicles")
        self.assertIn(b'class="vehicle-group-header"><th colspan="9">MLBevo</th>', vehicle_page.data)

        testing_view = self.client.get("/planner")
        self.assertEqual(testing_view.status_code, 200)
        self.assertIn(b"TEST-VIN-1", testing_view.data)
        self.assertNotIn(b"Automation Team: Review bookings", testing_view.data)

    def test_only_admin_can_reset_a_user_password(self) -> None:
        self._login("admin", "an-admin-password-long")
        page = self.client.get("/admin/users")
        planner_id = next(user["id"] for user in list_users() if user["username"] == "planner")
        response = self.client.post(
            f"/admin/users/{planner_id}/reset-password",
            data={"_csrf_token": self._csrf(page)},
        )
        self.assertEqual(response.status_code, 200)
        match = re.search(rb"<code>([^<]+)</code>", response.data)
        self.assertIsNotNone(match)
        temporary_password = match.group(1).decode()
        self.assertIsNotNone(authenticate("planner", temporary_password))
        self.__class__.planner_password = temporary_password

        self.client.post("/logout", data={"_csrf_token": self._csrf(self.client.get("/team"))})
        self._login("planner", temporary_password)
        self.assertEqual(self.client.get("/change-password").status_code, 403)
        self.assertEqual(self.client.get("/admin/users").status_code, 403)

    def test_login_requires_csrf_token(self) -> None:
        response = self.client.post("/login", data={"username": "admin", "password": "an-admin-password-long"})
        self.assertEqual(response.status_code, 400)

    def test_login_form_is_interactive_and_keeps_username_after_failure(self) -> None:
        page = self.client.get("/login")
        self.assertIn(b'data-login-form', page.data)
        self.assertIn(b'aria-controls="login-password"', page.data)
        self.assertIn(b'onclick="toggleLoginPassword(this)"', page.data)
        self.assertIn(b'Caps Lock is on.', page.data)
        self.assertIn(b'Signing in...', page.data)

        response = self.client.post(
            "/login",
            data={"username": "unknown.user", "password": "incorrect", "_csrf_token": self._csrf(page)},
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn(b'role="alert"', response.data)
        self.assertIn(b'id="login-username" type="text" name="username" value="unknown.user"', response.data)

    def test_logout_lands_on_sign_in_page(self) -> None:
        self._login("admin", "an-admin-password-long")
        page = self.client.get("/team")
        response = self.client.post(
            "/logout",
            data={"_csrf_token": self._csrf(page)},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request.path, "/login")
        self.assertIn(b"<h1>Sign in</h1>", response.data)
        self.assertIn(b'data-login-form', response.data)
        self.assertNotIn(b"Team Workspace", response.data)

    # Activity-history tests verify the Excel-style grid, date spans, weekend
    # rendering, colors, and visibility of planner bookings.
    def test_activity_history_shows_imported_weeks_and_vin(self) -> None:
        old_booking_id = create_booking(
            self.vehicle_id, "Historical activity", "KW10-2025", "2025-03-05", "", "analysis_checklist", "excel-import"
        )
        repeated_booking_id = create_booking(
            self.vehicle_id,
            "Historical activity",
            "KW11-2025",
            "2025-03-12",
            "",
            "analysis_checklist",
            "excel-import",
        )
        detail_booking_id = create_booking(
            self.vehicle_id,
            "Vehicle-tab detail",
            "KW31-2026",
            "2026-07-28",
            "Recorded by: Namik",
            "analysis_checklist",
            "excel-detail-import",
        )
        future_booking_id = create_booking(
            self.vehicle_id, "Future activity", "KW02-2027", "2027-01-13", "", "analysis_checklist", "excel-import"
        )
        with connect() as db:
            db.execute(
                "UPDATE bookings SET status = 'historical' WHERE id IN (?, ?, ?)",
                (old_booking_id, detail_booking_id, future_booking_id),
            )
            db.execute("UPDATE bookings SET status = 'historical' WHERE id = ?", (repeated_booking_id,))

        self._login("admin", "an-admin-password-long")
        page = self.client.get("/planner/history?year=2025&view=grid")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Vehicle Activity History", page.data)
        self.assertIn(b"Excel-style grid", page.data)
        self.assertIn(b"KW10", page.data)
        self.assertIn(b"MLBevo", page.data)
        self.assertIn(b"grid-filled", page.data)
        self.assertIn(b'class="grid-activity-label"', page.data)
        self.assertNotIn(b"<summary>1</summary>", page.data)
        self.assertIn(b'<th class="grid-week-col" colspan="7"><strong>KW10</strong>', page.data)
        self.assertIn(b'<th class="grid-day-col" title="Monday, 03 March 2025">3</th>', page.data)
        self.assertIn(b'class="grid-day-col grid-weekend"', page.data)
        self.assertIn(b'class="grid-day-cell grid-weekend"', page.data)
        self.assertIn(b'class="grid-fullscreen-button history-grid-fullscreen-button"', page.data)
        self.assertIn(b'function toggleHistoryGridFullscreen(button)', page.data)
        self.assertIn(b'class="grid-month grid-month-end" colspan="31">March</th>', page.data)
        self.assertIn(b".history-grid .grid-month-end { border-right: 2px solid #9aabba !important; }", page.data)
        self.assertIn(b".history-grid th.grid-day-col, .history-grid td.grid-day-cell { width: 40px; min-width: 40px; max-width: 40px; padding: 0;", page.data)
        self.assertIn(b"font-size: 0.84em; line-height: 1.25; white-space: normal; overflow-wrap: break-word;", page.data)
        self.assertIn(b"width: 190px; min-width: 190px; max-width: 190px;", page.data)
        self.assertIn(b".history-grid .grid-weekend { background-color: #e8edf2; color: #31465b; }", page.data)
        self.assertIn(b".history-grid { width: max-content; min-width: 100%; margin: 0; border: 0; border-radius: 0; box-shadow: none; overflow: visible;", page.data)
        self.assertIn(b".history-grid .grid-vehicle-col { position: sticky !important; left: 0 !important;", page.data)
        self.assertIn(b".history-grid thead th.grid-week-col { position: sticky; top: 0;", page.data)
        self.assertEqual(
            page.data.count(
                f'class="grid-vehicle-col" rowspan="1"><a href="/planner/history/vehicle/{self.vehicle_id}">'.encode()
            ),
            1,
        )
        self.assertIn(b"KW10-2025", page.data)
        self.assertIn(b"Historical activity", page.data)
        self.assertNotIn(b'class="history-status">Historical</span>', page.data)
        self.assertIn(b"TEST-VIN-1", page.data)
        self.assertIn(f"/planner/history/vehicle/{self.vehicle_id}".encode(), page.data)
        self.assertNotIn(b"Future activity", page.data)

        timeline = self.client.get("/planner/history?year=all&view=timeline")
        self.assertIn(b"Vehicle-tab detail", timeline.data)
        self.assertIn(b"Recorded by: Namik", timeline.data)

        vehicle_page = self.client.get(f"/planner/history/vehicle/{self.vehicle_id}")
        self.assertEqual(vehicle_page.status_code, 200)
        for expected in (
            b"Logger", b"Yes", b"Baureihe test", b"Cluster test", b"Market test",
            b"VR test", b"Purpose test", b"TEST-VIN-1",
            b"Historical activity", b"Vehicle-tab detail", b"Recorded by: Namik",
            b"KW31-2026", b"Future activity", b"week-tone-0",
        ):
            self.assertIn(expected, vehicle_page.data)
        self.assertNotIn(b"PHUB-12345", vehicle_page.data)
        self.assertNotIn(b'class="history-status">Historical</span>', vehicle_page.data)
        vehicle_calendar = self.client.get(
            f"/planner/history/vehicle/{self.vehicle_id}?view=calendar&month=2026-07"
        )
        self.assertIn(b"<strong>Vehicle-tab detail</strong>", vehicle_calendar.data)
        self.assertIn(b"Recorded by: Namik", vehicle_calendar.data)

    def test_activity_history_summarizes_vehicle_usage_and_last_used_date(self) -> None:
        today = date.today()
        recent_id = add_vehicle({"label": "USAGE-RECENT"})
        middle_id = add_vehicle({"label": "USAGE-MIDDLE"})
        old_id = add_vehicle({"label": "USAGE-OLD"})
        future_only_id = add_vehicle({"label": "USAGE-FUTURE-ONLY"})
        rejected_only_id = add_vehicle({"label": "USAGE-REJECTED-ONLY"})
        inactive_id = add_vehicle({"label": "USAGE-INACTIVE"})

        def add_dated_activity(vehicle_id: int, activity: str, activity_date: date) -> int:
            iso_year, iso_week, _ = activity_date.isocalendar()
            return create_booking(
                vehicle_id,
                activity,
                f"KW{iso_week:02d}-{iso_year}",
                activity_date.isoformat(),
                "",
                "analysis_checklist",
                "planner",
            )

        recent_date = today - timedelta(days=10)
        middle_date = today - timedelta(days=45)
        old_date = today - timedelta(days=75)
        future_date = today + timedelta(days=10)
        add_dated_activity(recent_id, "Recent usage activity", recent_date)
        add_dated_activity(middle_id, "Middle usage activity", middle_date)
        add_dated_activity(old_id, "Old usage activity", old_date)
        add_dated_activity(future_only_id, "Future planned activity", future_date)
        rejected_booking_id = add_dated_activity(
            rejected_only_id, "Rejected usage activity", recent_date
        )
        add_dated_activity(inactive_id, "Inactive vehicle usage", recent_date)
        with connect() as db:
            db.execute("UPDATE bookings SET status = 'rejected' WHERE id = ?", (rejected_booking_id,))
            db.execute("UPDATE vehicles SET active = 0 WHERE id = ?", (inactive_id,))

        self._login("admin", "an-admin-password-long")
        page = self.client.get("/planner/history")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Vehicle usage", page.data)
        self.assertIn(b"Used in the last 30 days", page.data)
        self.assertIn("No activity for 31–60 days".encode(), page.data)
        self.assertIn(b"No activity for over 60 days", page.data)
        self.assertIn(b"Last used", page.data)
        self.assertEqual(page.data.count(b'<details class="history-stat vehicle-usage-card">'), 4)
        self.assertIn(b"View vehicle list", page.data)
        self.assertIn(recent_date.strftime("%d %b %Y").encode(), page.data)
        self.assertIn(middle_date.strftime("%d %b %Y").encode(), page.data)
        self.assertIn(old_date.strftime("%d %b %Y").encode(), page.data)
        self.assertIn(b"USAGE-INACTIVE", page.data)
        self.assertIn(b"Inactive vehicle", page.data)
        self.assertIn(b"USAGE-FUTURE-ONLY", page.data)
        self.assertIn(b"USAGE-REJECTED-ONLY", page.data)
        for vehicle_id, label in (
            (future_only_id, b"USAGE-FUTURE-ONLY"),
            (rejected_only_id, b"USAGE-REJECTED-ONLY"),
        ):
            self.assertIn(
                f'<li><a href="/planner/history/vehicle/{vehicle_id}">'.encode()
                + label
                + b"</a><span>Last used: No recorded use</span></li>",
                page.data,
            )
        self.assertIn(
            f'<li><a href="/planner/history/vehicle/{recent_id}">USAGE-RECENT</a>'
            f'<span>Last used: {recent_date:%d %b %Y}</span></li>'.encode(),
            page.data,
        )

    def test_vehicle_history_calendar_is_scoped_to_vehicle(self) -> None:
        create_booking(
            self.vehicle_id,
            "PT1023 June activity",
            "KW24-2026",
            "2026-06-12",
            "",
            "analysis_checklist",
            "planner",
            planned_end_date="2026-06-14",
        )
        other_vehicle_id = add_vehicle({"label": "OTHER-VEHICLE"})
        create_booking(
            other_vehicle_id,
            "Other vehicle activity",
            "KW24-2026",
            "2026-06-12",
            "",
            "analysis_checklist",
            "planner",
        )
        self._login("admin", "an-admin-password-long")

        page = self.client.get(
            f"/planner/history/vehicle/{self.vehicle_id}?view=calendar&month=2026-06"
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"June 2026", page.data)
        self.assertIn(b"PT1023 June activity", page.data)
        self.assertNotIn(b"Other vehicle activity", page.data)
        self.assertIn(b"Booked by: planner", page.data)
        self.assertEqual(page.data.count(b"<strong>PT1023 June activity</strong>"), 3)
        self.assertIn(
            f'/planner/history/vehicle/{self.vehicle_id}?view=calendar&month=2026-05'.encode(),
            page.data,
        )
        self.assertIn(
            f'/planner/history/vehicle/{self.vehicle_id}?view=calendar&month=2026-07'.encode(),
            page.data,
        )
        self.assertIn(
            f'action="/planner/history/vehicle/{self.vehicle_id}"'.encode(),
            page.data,
        )
        self.assertIn(
            f'href="/planner/history/vehicle/{self.vehicle_id}?view=timeline"'.encode(),
            page.data,
        )

    def test_activity_history_includes_new_pending_team_bookings(self) -> None:
        dated_booking_id = create_booking(
            self.vehicle_id,
            "New pending campaign",
            "KW47-2026",
            "2026-11-18",
            "",
            "analysis_checklist",
            "automation",
        )
        week_only_booking_id = create_booking(
            self.vehicle_id,
            "Week-only test booking",
            "KW48-2026",
            "",
            "",
            "analysis_checklist",
            "planner",
        )
        self._login("admin", "an-admin-password-long")

        grid = self.client.get("/planner/history?year=2026&view=grid")
        self.assertEqual(grid.status_code, 200)
        self.assertIn(b"New pending campaign", grid.data)
        self.assertIn(b"Week-only test booking", grid.data)
        self.assertIn(b"Pending", grid.data)
        self.assertIn(
            b'aria-label="Activity from 23 November 2026 through 27 November 2026, KW48-2026"',
            grid.data,
        )
        self.assertIn(
            b'aria-label="Activity from 18 November 2026 through 18 November 2026, KW47-2026"',
            grid.data,
        )

        timeline = self.client.get("/planner/history?year=2026&view=timeline")
        self.assertIn(b"New pending campaign", timeline.data)
        self.assertIn(b"Week-only test booking", timeline.data)
        self.assertIn(b"KW47-2026", timeline.data)
        self.assertIn(b"KW48-2026", timeline.data)
        self.assertIn(b"23 Nov \xe2\x80\x93 27 Nov", timeline.data)
        self.assertIn(b'class="history-status">Pending</span>', timeline.data)

        vehicle_page = self.client.get(f"/planner/history/vehicle/{self.vehicle_id}")
        self.assertIn(b"New pending campaign", vehicle_page.data)
        self.assertIn(b"Week-only test booking", vehicle_page.data)

        with connect() as db:
            db.execute("UPDATE bookings SET status = 'rejected' WHERE id = ?", (dated_booking_id,))
            db.execute("UPDATE bookings SET status = 'rejected' WHERE id = ?", (week_only_booking_id,))
        rejected_history = self.client.get("/planner/history?year=2026&view=grid")
        self.assertNotIn(b"New pending campaign", rejected_history.data)
        self.assertNotIn(b"Week-only test booking", rejected_history.data)

    def test_activity_history_calendar_shows_multi_day_bookings_and_month_navigation(self) -> None:
        create_booking(
            self.vehicle_id,
            "Calendar weekend campaign",
            "KW24-2026",
            "2026-06-12",
            "",
            "analysis_checklist",
            "planner",
            planned_end_date="2026-06-14",
        )
        other_vehicle_id = add_vehicle({"label": "WEEK-CALENDAR-VEHICLE"})
        create_booking(
            other_vehicle_id,
            "Week-only calendar campaign",
            "KW24-2026",
            "",
            "",
            "analysis_checklist",
            "planner",
        )
        self._login("admin", "an-admin-password-long")

        page = self.client.get("/planner/history?year=2026&view=calendar&month=2026-06")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"June 2026", page.data)
        self.assertIn(b"Monday", page.data)
        self.assertIn(b"Calendar weekend campaign", page.data)
        self.assertIn(b"PT1023", page.data)
        self.assertIn(b"Booked by: planner", page.data)
        self.assertIn(b"Pending", page.data)
        self.assertEqual(page.data.count(b"<strong>Calendar weekend campaign</strong>"), 3)
        self.assertIn(b"2 activities in displayed dates.", page.data)

        week_page = self.client.get(
            "/planner/history?year=2026&view=calendar&calendar=week&month=2026-06&week=2026-W24"
        )
        self.assertEqual(week_page.status_code, 200)
        self.assertIn(b"Week 24, 2026", week_page.data)
        self.assertIn(b"08 Jun", week_page.data)
        self.assertIn(b"14 Jun", week_page.data)
        self.assertEqual(week_page.data.count(b"<strong>Calendar weekend campaign</strong>"), 3)
        self.assertIn(b"2 activities in displayed dates.", week_page.data)
        self.assertIn(b"Week-only calendar campaign", week_page.data)
        self.assertIn(b'type="week" name="week" value="2026-W24"', week_page.data)
        self.assertIn(b"week=2026-W23", week_page.data)
        self.assertIn(b"week=2026-W25", week_page.data)
        other_week = self.client.get(
            "/planner/history?year=2026&view=calendar&calendar=week&month=2026-06&week=2026-W26"
        )
        self.assertNotIn(b"Calendar weekend campaign", other_week.data)
        self.assertEqual(
            self.client.get("/planner/history?year=2026&view=calendar&calendar=week&month=2026-06&week=2025-W10").status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/planner/history?year=2026&view=calendar&calendar=week&month=2026-06&week=bad").status_code,
            400,
        )
        vehicle_week = self.client.get(
            f"/planner/history/vehicle/{self.vehicle_id}?view=calendar&calendar=week&month=2026-06&week=2026-W24"
        )
        self.assertEqual(vehicle_week.status_code, 200)
        self.assertIn(b"Week 24, 2026", vehicle_week.data)
        self.assertIn(b"Calendar weekend campaign", vehicle_week.data)

        def submit_picker(page, field: str, value: str):
            nav = page.data.decode()[page.data.decode().index("activity-calendar-nav"):]
            form = re.search(r'<form method="get" action="([^"]+)">(.*?)</form>', nav, re.S)
            # Browsers replace the action's query string with the form fields.
            fields = dict(re.findall(r'type="hidden" name="(\w+)" value="([^"]*)"', form.group(2)))
            fields[field] = value
            return self.client.get(form.group(1).split("?", 1)[0], query_string=fields)

        month_page = self.client.get("/planner/history?year=2026&view=calendar&month=2026-01")
        picked_month = submit_picker(month_page, "month", "2026-06")
        self.assertEqual(picked_month.status_code, 200)
        self.assertIn(b"June 2026", picked_month.data)
        self.assertIn(b"Calendar weekend campaign", picked_month.data)
        picked_week = submit_picker(week_page, "week", "2026-W24")
        self.assertIn(b"Week 24, 2026", picked_week.data)
        self.assertIn(b"Calendar weekend campaign", picked_week.data)
        picked_vehicle_week = submit_picker(vehicle_week, "week", "2026-W24")
        self.assertIn(b"Week 24, 2026", picked_vehicle_week.data)
        self.assertIn(b"Calendar weekend campaign", picked_vehicle_week.data)
        self.assertEqual(page.data.count(b"<strong>Week-only calendar campaign</strong>"), 5)
        self.assertIn(b'min="2026-01" max="2026-12"', page.data)
        self.assertIn(b"month=2026-05", page.data)
        self.assertIn(b"month=2026-07", page.data)
        self.assertIn(b"section=history", page.data)
        self.assertIn(b"view=grid", page.data)
        self.assertIn(b"view=timeline", page.data)

    def test_history_year_filter_stays_on_history_section(self) -> None:
        create_booking(self.vehicle_id, "Year filter activity", "KW10-2025", "2025-03-05", "", "", "planner")
        self._login("admin", "an-admin-password-long")
        page = self.client.get("/planner/history?year=2025&view=grid")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'<form class="card" method="get" action="/team">', page.data)
        self.assertIn(b'name="section" value="history"', page.data)

        filtered = self.client.get("/team?section=history&year=2025&view=grid")
        self.assertEqual(filtered.status_code, 200)
        self.assertIn(b"Vehicle Activity History", filtered.data)
        self.assertNotIn(b"Testing Team: Submit an activity", filtered.data)
        self.assertIn(b'<option value="2025" selected>2025</option>', filtered.data)

    def test_weekend_booking_requires_confirmation_and_preserves_form_data(self) -> None:
        self._login("admin", "an-admin-password-long")
        for day, activity in (
            ("2026-06-13", "New Saturday activity"),
            ("2026-06-14", "New Sunday activity"),
        ):
            planner_page = self.client.get("/planner")
            form_data = {
                "vehicle_id": str(self.vehicle_id),
                "activity": activity,
                "period": "KW24-2026",
                "planned_date": day,
                "description": "Weekend validation note",
                "checklist_template": "analysis_checklist",
                "_csrf_token": self._csrf(planner_page),
            }
            confirmation = self.client.post("/planner/bookings/create", data=form_data)
            self.assertEqual(confirmation.status_code, 200)
            self.assertIn(b"Confirm weekend booking", confirmation.data)
            self.assertIn(day.encode(), confirmation.data)
            with connect() as db:
                exists_before_confirmation = db.execute(
                    "SELECT 1 FROM bookings WHERE activity = ?", (activity,)
                ).fetchone()
            self.assertIsNone(exists_before_confirmation)

            form_data["_csrf_token"] = self._csrf(confirmation)
            form_data["confirm_weekend"] = "1"
            submitted = self.client.post("/planner/bookings/create", data=form_data, follow_redirects=True)
            self.assertEqual(submitted.status_code, 200)
            self.assertIn(b"waiting for team review", submitted.data)
            with connect() as db:
                saved = db.execute(
                    "SELECT planned_date, description, status FROM bookings WHERE activity = ?",
                    (activity,),
                ).fetchone()
            self.assertEqual(saved["planned_date"], day)
            self.assertEqual(saved["description"], "Weekend validation note")
            self.assertEqual(saved["status"], "pending")

        grid = self.client.get("/planner/history?year=2026&view=grid")
        self.assertIn(b"New Saturday activity", grid.data)
        self.assertIn(b"New Sunday activity", grid.data)

    def test_booking_accepts_week_or_cross_week_dates(self) -> None:
        self._login("admin", "an-admin-password-long")
        base_form = {
            "vehicle_id": str(self.vehicle_id),
            "activity": "Unscheduled activity",
            "period": "",
            "planned_date": "",
            "planned_end_date": "",
            "description": "",
            "checklist_template": "analysis_checklist",
        }
        planner_page = self.client.get("/planner")
        base_form["_csrf_token"] = self._csrf(planner_page)
        missing_schedule = self.client.post("/planner/bookings/create", data=base_form)
        self.assertEqual(missing_schedule.status_code, 400)
        self.assertIn(b"Enter a planned week or a start date.", missing_schedule.data)

        week_form = dict(base_form)
        week_form.update({"activity": "Submitted week-only activity", "period": "KW47-2026"})
        planner_page = self.client.get("/planner")
        week_form["_csrf_token"] = self._csrf(planner_page)
        week_submission = self.client.post(
            "/planner/bookings/create", data=week_form, follow_redirects=True
        )
        self.assertIn(b"waiting for team review", week_submission.data)
        with connect() as db:
            saved_week_booking = db.execute(
                "SELECT period, planned_date, planned_end_date FROM bookings WHERE activity = ?",
                ("Submitted week-only activity",),
            ).fetchone()
        self.assertEqual(saved_week_booking["period"], "KW47-2026")
        self.assertEqual(saved_week_booking["planned_date"], "")
        self.assertEqual(saved_week_booking["planned_end_date"], "")

        date_form = dict(base_form)
        date_form.update(
            {
                "activity": "Cross-week date range",
                "planned_date": "2026-11-27",
                "planned_end_date": "2026-11-30",
            }
        )
        planner_page = self.client.get("/planner")
        date_form["_csrf_token"] = self._csrf(planner_page)
        confirmation = self.client.post("/planner/bookings/create", data=date_form)
        self.assertEqual(confirmation.status_code, 200)
        self.assertIn(b"Confirm weekend booking", confirmation.data)
        date_form["_csrf_token"] = self._csrf(confirmation)
        date_form["confirm_weekend"] = "1"
        range_submission = self.client.post(
            "/planner/bookings/create", data=date_form, follow_redirects=True
        )
        self.assertIn(b"waiting for team review", range_submission.data)
        with connect() as db:
            saved_range_booking = db.execute(
                "SELECT period, planned_date, planned_end_date FROM bookings WHERE activity = ?",
                ("Cross-week date range",),
            ).fetchone()
        self.assertEqual(saved_range_booking["period"], "KW48-2026")
        self.assertEqual(saved_range_booking["planned_date"], "2026-11-27")
        self.assertEqual(saved_range_booking["planned_end_date"], "2026-11-30")

        mismatch_form = dict(base_form)
        mismatch_form.update(
            {
                "activity": "Dates override week",
                "period": "KW47-2026",
                "planned_date": "2026-12-02",
                "planned_end_date": "2026-12-03",
            }
        )
        planner_page = self.client.get("/planner")
        mismatch_form["_csrf_token"] = self._csrf(planner_page)
        mismatch_submission = self.client.post(
            "/planner/bookings/create", data=mismatch_form, follow_redirects=True
        )
        self.assertIn(b"waiting for team review", mismatch_submission.data)
        with connect() as db:
            saved_mismatch_booking = db.execute(
                "SELECT period FROM bookings WHERE activity = ?", ("Dates override week",)
            ).fetchone()
        self.assertEqual(saved_mismatch_booking["period"], "KW49-2026")

    def test_activity_color_from_excel_is_reused_for_matching_team_booking(self) -> None:
        excel_booking_id = create_booking(
            self.vehicle_id,
            "Shared activity color",
            "KW46-2025",
            "2025-11-12",
            "",
            "analysis_checklist",
            "excel-import",
        )
        with connect() as db:
            db.execute(
                "UPDATE bookings SET status = 'historical', activity_color = '#12AB34' WHERE id = ?",
                (excel_booking_id,),
            )
        create_booking(
            self.vehicle_id,
            "Shared activity color",
            "KW47-2026",
            "2026-11-18",
            "",
            "analysis_checklist",
            "planner",
        )

        self._login("admin", "an-admin-password-long")
        page = self.client.get("/planner/history?year=2026&view=grid")
        self.assertIn(b'background-color:#12AB34', page.data)
        self.assertIn(b"Shared activity color", page.data)

    def test_testing_can_choose_excel_or_custom_activity_color(self) -> None:
        create_booking(
            self.vehicle_id, "Color selection weekend activity", "KW46-2025", "2025-11-12", "",
            "analysis_checklist", "excel-import", activity_color="#12ab34",
        )
        self._login("planner", self.planner_password)
        planner_page = self.client.get("/planner")
        self.assertIn(b'name="activity_color" value="#12AB34"', planner_page.data)
        self.assertIn(b'type="color" name="custom_activity_color"', planner_page.data)
        base_form = {
            "vehicle_id": str(self.vehicle_id),
            "activity": "Color selection weekend activity",
            "planned_date": "2026-06-13",
            "planned_end_date": "2026-06-14",
            "_csrf_token": self._csrf(planner_page),
        }
        invalid = self.client.post(
            "/planner/bookings/create", data={**base_form, "activity_color": "#123456;bad"}
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn(b"Choose a valid activity color", invalid.data)
        missing_custom = self.client.post(
            "/planner/bookings/create", data={**base_form, "activity_color": "custom"}
        )
        self.assertEqual(missing_custom.status_code, 400)
        confirmation = self.client.post(
            "/planner/bookings/create",
            data={**base_form, "activity_color": "custom", "custom_activity_color": "#aabbcc"},
        )
        self.assertEqual(confirmation.status_code, 200)
        self.assertIn(b'name="activity_color" value="#AABBCC"', confirmation.data)
        submitted = self.client.post(
            "/planner/bookings/create",
            data={**base_form, "activity_color": "#AABBCC", "confirm_weekend": "1",
                  "_csrf_token": self._csrf(confirmation)},
        )
        self.assertEqual(submitted.status_code, 302)
        with connect() as db:
            saved = db.execute(
                "SELECT activity_color FROM bookings WHERE activity = ? AND created_by = ?",
                (base_form["activity"], "Shared Planner Tester"),
            ).fetchone()
        self.assertEqual(saved["activity_color"], "#AABBCC")

        for path in (
            "/planner/history?year=2026&view=grid",
            "/planner/history?year=2026&view=calendar&month=2026-06",
            "/planner/history?year=2026&view=timeline",
            f"/planner/history/vehicle/{self.vehicle_id}?view=calendar&month=2026-06",
            f"/planner/history/vehicle/{self.vehicle_id}?view=timeline",
        ):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertIn(b"Color selection weekend activity", page.data)
            self.assertIn(b"background-color:#AABBCC", page.data)

        excel_color_submission = self.client.post(
            "/planner/bookings/create",
            data={**base_form, "activity": "Selected Excel color activity", "planned_date": "2026-06-12",
                  "planned_end_date": "", "activity_color": "#12AB34"},
        )
        self.assertEqual(excel_color_submission.status_code, 302)
        with connect() as db:
            saved_excel_color = db.execute(
                "SELECT activity_color FROM bookings WHERE activity = ?", ("Selected Excel color activity",)
            ).fetchone()
        self.assertEqual(saved_excel_color["activity_color"], "#12AB34")

    def test_long_activity_uses_only_empty_days_and_does_not_cover_next_activity(self) -> None:
        create_booking(
            self.vehicle_id,
            "Long activity that should be visible in the grid",
            "KW10-2026",
            "2026-03-02",
            "",
            "analysis_checklist",
            "planner",
            planned_end_date="2026-03-05",
        )
        with connect() as db:
            db.execute(
                """INSERT INTO bookings (vehicle_id, activity, period, planned_date, created_by)
                   VALUES (?, 'Following activity', 'KW10-2026', '2026-03-04', 'excel-import')""",
                (self.vehicle_id,),
            )
        self._login("admin", "an-admin-password-long")
        page = self.client.get("/planner/history?year=2026&view=grid")
        self.assertIn(b'<td colspan="4" class="grid-day-cell grid-filled', page.data)
        self.assertIn(b"Long activity that should be visible in the grid", page.data)
        self.assertIn(b"Following activity", page.data)
        self.assertIn(b'aria-label="Activity from 02 March 2026 through 05 March 2026', page.data)
        self.assertRegex(page.data.decode(), r'class="grid-vehicle-col" rowspan="[2-9]\d*"')
        self.assertNotIn(b".history-grid .grid-activity-list { position: absolute", page.data)

    def test_multi_day_booking_confirms_weekends_and_renders_full_date_range(self) -> None:
        self._login("admin", "an-admin-password-long")
        planner_page = self.client.get("/planner")
        form_data = {
            "vehicle_id": str(self.vehicle_id),
            "activity": "Friday to Sunday campaign",
            "period": "KW24-2026",
            "planned_date": "2026-06-12",
            "planned_end_date": "2026-06-14",
            "description": "Includes booked weekend days",
            "checklist_template": "analysis_checklist",
            "_csrf_token": self._csrf(planner_page),
        }
        confirmation = self.client.post("/planner/bookings/create", data=form_data)
        self.assertEqual(confirmation.status_code, 200)
        self.assertIn(b"Saturday, 13 June 2026", confirmation.data)
        self.assertIn(b"Sunday, 14 June 2026", confirmation.data)
        self.assertIn(b'name="planned_end_date" value="2026-06-14"', confirmation.data)

        form_data["_csrf_token"] = self._csrf(confirmation)
        form_data["confirm_weekend"] = "1"
        submitted = self.client.post("/planner/bookings/create", data=form_data, follow_redirects=True)
        self.assertEqual(submitted.status_code, 200)
        with connect() as db:
            saved = db.execute(
                "SELECT planned_date, planned_end_date FROM bookings WHERE activity = ?",
                ("Friday to Sunday campaign",),
            ).fetchone()
        self.assertEqual(saved["planned_date"], "2026-06-12")
        self.assertEqual(saved["planned_end_date"], "2026-06-14")

        grid = self.client.get("/planner/history?year=2026&view=grid")
        self.assertIn(b'<td colspan="3" class="grid-day-cell grid-filled', grid.data)
        self.assertIn(b"Friday to Sunday campaign", grid.data)
        self.assertIn(b"through 14 June 2026", grid.data)

    def test_activity_spanning_year_boundary_appears_in_both_year_views(self) -> None:
        create_booking(
            self.vehicle_id,
            "Year boundary campaign",
            "KW53-2026",
            "2026-12-31",
            "",
            "analysis_checklist",
            "planner",
            planned_end_date="2027-01-02",
        )
        self._login("admin", "an-admin-password-long")
        for year in ("2026", "2027"):
            page = self.client.get(f"/planner/history?year={year}&view=grid")
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"Year boundary campaign", page.data)

    def test_saturday_yellow_and_sunday_activities_are_shown_in_separate_cells(self) -> None:
        create_booking(
            self.vehicle_id,
            "Yellow Saturday test",
            "KW24-2026",
            "2026-06-13",
            "",
            "analysis_checklist",
            "excel-import",
        )
        sunday_booking_id = create_booking(
            self.vehicle_id,
            "Sunday test",
            "KW24-2026",
            "2026-06-14",
            "",
            "analysis_checklist",
            "excel-import",
        )
        with connect() as db:
            db.execute(
                "UPDATE bookings SET status = 'historical', activity_color = '#FFC000' "
                "WHERE activity = 'Yellow Saturday test'"
            )
            db.execute(
                "UPDATE bookings SET status = 'historical' WHERE id = ?",
                (sunday_booking_id,),
            )

        self._login("admin", "an-admin-password-long")
        page = self.client.get("/planner/history?year=2026&view=grid")
        self.assertIn(b"Yellow Saturday test", page.data)
        self.assertIn(b"Sunday test", page.data)
        self.assertIn(b'background-color:#FFC000;color:#000000', page.data)
        self.assertIn(b'<td colspan="1" class="grid-day-cell grid-filled grid-weekend', page.data)
        self.assertIn(b'title="Saturday, 13 June 2026"', page.data)
        self.assertIn(b'title="Sunday, 14 June 2026"', page.data)

    def test_activity_grid_marks_current_week(self) -> None:
        today = date.today()
        iso_year, iso_week, _ = today.isocalendar()
        create_booking(
            self.vehicle_id,
            "Current week activity",
            f"KW{iso_week:02d}-{iso_year}",
            today.isoformat(),
            "",
            "analysis_checklist",
            "planner",
        )
        self._login("admin", "an-admin-password-long")
        page = self.client.get(f"/planner/history?year={iso_year}&view=grid")
        self.assertIn(b'class="grid-week-col grid-current-week"', page.data)
        self.assertIn(b"CURRENT WEEK", page.data)
        self.assertIn(b'class="grid-day-cell grid-filled grid-current-week grid-today"', page.data)
        all_years = self.client.get("/planner/history?year=all&view=grid")
        last_week = date(iso_year, 12, 28).isocalendar().week
        self.assertIn(b">December</th>", all_years.data)
        self.assertIn(f"<strong>KW{last_week:02d}</strong>".encode(), all_years.data)

    # Vehicle-management tests cover manual creation and active/inactive status changes.
    def test_vehicle_block_shades_grid_without_messages_and_preserves_calendar_bookings(self) -> None:
        today = date.today()
        end = today + timedelta(days=1)
        booking_id = create_booking(
            self.vehicle_id, "Existing while blocked", f"KW{today.isocalendar().week:02d}-{today.isocalendar().year}",
            (today - timedelta(days=1)).isoformat(), "", "", "planner",
            planned_end_date=(today + timedelta(days=2)).isoformat(),
        )
        self._login("admin", "an-admin-password-long")
        page = self.client.get("/vehicles")
        block_url = f"/vehicles/{self.vehicle_id}/block"
        blocked = self.client.post(block_url, data={
            "_csrf_token": self._csrf(page), "blocked": "1", "reason": "Repair <pending>", "until": end.isoformat(),
        })
        self.assertEqual(blocked.status_code, 302)
        page = self.client.get("/vehicles")
        self.assertIn(b'role="switch" aria-checked="true"', page.data)
        self.assertIn(b"Repair &lt;pending&gt;", page.data)
        self.assertIn(b'<tr class="vehicle-blocked-row"><td>PT1023</td>', page.data)
        row, following = page.data.split(b'<tr class="vehicle-blocked-row"><td>PT1023</td>', 1)[1].split(b"</tr>", 1)
        self.assertNotIn(b"Not Available from", row)
        self.assertTrue(following.startswith(b'<tr class="vehicle-block-note-row"><td colspan="9">'))
        availability = f"Not Available from {today.isoformat()} to {end.isoformat()}".encode()
        self.assertEqual(page.data.count(availability), 1)
        self.assertIn(b"Reason: Repair &lt;pending&gt;", page.data)
        self.assertIn(availability + " — Reason: Repair &lt;pending&gt;</span></div>".encode(), page.data)
        self.assertNotIn(b"<th>Blocked</th>", page.data)
        self.assertNotIn("Yes — not ready for testing".encode(), page.data)
        review = self.client.get("/planner/review?status=all")
        self.assertIn(b"<td>Existing while blocked</td>", review.data)
        for path in (
            "/planner/history?year=all&view=grid",
            f"/planner/history?year=all&view=calendar&month={today:%Y-%m}",
            "/planner/history?year=all&view=timeline",
            f"/planner/history/vehicle/{self.vehicle_id}?view=calendar&month={today:%Y-%m}",
            f"/planner/history/vehicle/{self.vehicle_id}",
        ):
            with self.subTest(path=path):
                history = self.client.get(path)
                self.assertEqual(history.status_code, 200)
                self.assertIn(b"Existing while blocked", history.data)
                self.assertNotIn(b"Repair &lt;pending&gt;", history.data)
                self.assertNotIn(b"not ready for testing", history.data)
                self.assertNotIn(b"grid-blocked", history.data)
                self.assertNotIn(b"vehicle-block-band", history.data)
                if "view=grid" in path:
                    self.assertIn(b"--unavailable-left:25.000000%;--unavailable-width:50.000000%", history.data)
                    self.assertRegex(history.data, rb'<td colspan="4" class="[^"]*grid-unavailable')
                else:
                    self.assertNotRegex(history.data, rb'<td[^>]*class="[^"]*grid-unavailable')
        unblocked = self.client.post(block_url, data={"_csrf_token": self._csrf(page), "blocked": "0"})
        self.assertEqual(unblocked.status_code, 302)
        page = self.client.get("/vehicles")
        self.assertNotIn(b'<tr class="vehicle-blocked-row">', page.data)
        self.assertNotIn(b'<tr class="vehicle-block-note-row">', page.data)
        self.assertNotIn(b"Repair &lt;pending&gt;", page.data)
        self.assertNotIn(b"Not Available from", page.data)
        grid = self.client.get("/planner/history?year=all&view=grid")
        self.assertNotRegex(grid.data, rb'<td[^>]*class="[^"]*grid-unavailable')
        with connect() as db:
            self.assertIsNotNone(db.execute("SELECT id FROM bookings WHERE id = ?", (booking_id,)).fetchone())
        audit = self.client.get("/admin/audit")
        self.assertIn(b"Blocked vehicle", audit.data)
        self.assertIn(b"Unblocked vehicle", audit.data)

    def test_vehicle_block_is_admin_only_and_blocks_new_bookings(self) -> None:
        today = date.today()
        empty_id = add_vehicle({"label": "UNREADY-CAR", "group_name": "Test"})
        block_url = f"/vehicles/{empty_id}/block"
        for username, password in (("planner", self.planner_password), ("automation", self.automation_password)):
            self._login(username, password)
            page = self.client.get("/vehicles")
            self.assertNotIn(f'action="{block_url}"'.encode(), page.data)
            self.assertEqual(self.client.post(block_url, data={
                "_csrf_token": self._csrf(page), "blocked": "1",
            }).status_code, 403)
            self.client.post("/logout", data={"_csrf_token": self._csrf(page)})
        self._login("admin", "an-admin-password-long")
        page = self.client.get("/vehicles")
        self.assertEqual(self.client.post(block_url, data={"blocked": "1"}).status_code, 400)
        self.assertEqual(self.client.post(block_url, data={
            "_csrf_token": self._csrf(page), "blocked": "1", "until": "bad-date",
        }).status_code, 400)
        self.assertEqual(self.client.post(block_url, data={
            "_csrf_token": self._csrf(page), "blocked": "bad-state",
        }).status_code, 400)
        set_vehicle_blocked(empty_id, True, "Maintenance")
        grid = self.client.get("/planner/history?year=all&view=grid")
        self.assertNotIn(b"grid-blocked", grid.data)
        self.assertNotIn(b"Maintenance", grid.data)
        self.assertRegex(grid.data, rb'<td class="grid-day-cell grid-unavailable')
        self.client.post("/logout", data={"_csrf_token": self._csrf(page)})
        self._login("planner", self.planner_password)
        page = self.client.get("/planner")
        self.assertIn(b'check the <a href="/vehicles">Vehicles section</a>', page.data)
        self.assertIn(b"to confirm the vehicle is ready for testing and not blocked.", page.data)
        response = self.client.post("/planner/bookings/create", data={
            "_csrf_token": self._csrf(page), "vehicle_id": str(empty_id), "activity": "Not allowed",
            "planned_date": today.isoformat(), "confirm_weekend": "1",
        })
        self.assertEqual(response.status_code, 409)
        self.assertIn(b"Maintenance", response.data)
        vehicles = self.client.get("/vehicles")
        self.assertIn(f"Not Available from {today.isoformat()} until further notice".encode(), vehicles.data)
        self.assertIn(b"Reason: Maintenance", vehicles.data)
        self.assertIn(b'<tr class="vehicle-blocked-row"><td>UNREADY-CAR</td>', vehicles.data)
        self.assertIn(b'<tr class="vehicle-block-note-row"><td colspan="9">', vehicles.data)
        self.assertNotIn(b'role="switch"', vehicles.data)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bookings WHERE vehicle_id = ?", (empty_id,)).fetchone()[0], 0)

    def test_grid_red_cells_cover_only_inclusive_block_dates_without_any_bookings(self) -> None:
        today = date.today()
        vehicle_id = add_vehicle({"label": "GRID-UNAVAILABLE", "group_name": "Test"})
        set_vehicle_blocked(vehicle_id, True, "No grid message", (today + timedelta(days=2)).isoformat())
        self._login("planner", self.planner_password)
        grid = self.client.get("/planner/history?year=all&view=grid")
        self.assertEqual(grid.status_code, 200)
        grid_table = grid.data.split(b'<table class="history-grid">', 1)[1]
        row = grid_table.split(b">GRID-UNAVAILABLE</a>", 1)[1].split(b"</tr>", 1)[0]
        self.assertEqual(len(re.findall(rb'<td class="[^"]*grid-unavailable', row)), 3)
        self.assertNotIn(b"No grid message", row)
        self.assertNotIn(b"Not Available", row)
        self.assertNotIn(b"BLOCKED", row)
        selected_year = self.client.get(f"/planner/history?year={today.year}&view=grid")
        self.assertEqual(selected_year.status_code, 200)
        calendar = self.client.get(f"/planner/history?year={today.year}&view=calendar&month={today:%Y-%m}")
        self.assertEqual(calendar.status_code, 200)
        self.assertNotRegex(calendar.data, rb'<td[^>]*class="[^"]*grid-unavailable')
        with connect() as db:
            db.execute("UPDATE vehicles SET blocked_until = ? WHERE id = ?", ((today - timedelta(days=1)).isoformat(), vehicle_id))
        expired = self.client.get("/planner/history?year=all&view=grid")
        self.assertNotRegex(expired.data, rb'<td[^>]*class="[^"]*grid-unavailable')

    def test_admin_can_add_a_manual_vehicle(self) -> None:
        self._login("admin", "an-admin-password-long")
        page = self.client.get("/vehicles")
        self.assertIn(b'aria-controls="add-vehicle-dialog"', page.data)
        self.assertIn(b'<dialog class="vehicle-dialog"', page.data)
        self.assertIn(b'action="/vehicles/create"', page.data)
        self.assertIn(b'name="group_name"', page.data)
        response = self.client.post(
            "/vehicles/create",
            data={
                "label": "MANUAL-TEST-1",
                "logger": "No",
                "series": "Test series",
                "group_name": "Neu ab 1.1.2026",
                "_csrf_token": self._csrf(page),
            },
        )
        self.assertEqual(response.status_code, 302)
        vehicle_page = self.client.get("/vehicles")
        self.assertIn(b"MANUAL-TEST-1", vehicle_page.data)
        self.assertIn(b"Neu ab 1.1.2026", vehicle_page.data)

    def test_vehicle_actions_are_grouped_in_a_three_dot_menu(self) -> None:
        self._login("admin", "an-admin-password-long")
        page = self.client.get("/vehicles")
        row = page.data.split(b"<td>PT1023</td>", 1)[1].split(b"</tr>", 1)[0]
        menu = row.split(b'<details class="vehicle-actions">', 1)[1].split(b"</details>", 1)[0]
        self.assertIn(b'aria-label="Actions for PT1023">&#8943;</summary>', menu)
        self.assertIn(f'href="/vehicles/{self.vehicle_id}/edit"'.encode(), menu)
        self.assertIn(f'href="/vehicles/{self.vehicle_id}/edit#delete-vehicle"'.encode(), menu)
        self.assertIn(b">Block vehicle</button>", menu)
        self.assertIn(b">Add comment</button>", menu)
        self.assertIn(f'action="/vehicles/{self.vehicle_id}/comment"'.encode(), menu)
        self.assertNotIn(b'role="switch"', row.split(b'<details class="vehicle-actions">', 1)[0])
        self.assertNotIn(b"Not Available from", row)
        set_vehicle_blocked(self.vehicle_id, True)
        page = self.client.get("/vehicles")
        row = page.data.split(b"<td>PT1023", 1)[1].split(b"</tr>", 1)[0]
        self.assertNotIn(b"Not Available from", row)
        self.assertIn(b'<tr class="vehicle-block-note-row"><td colspan="9">', page.data)
        self.assertNotIn(b"Reason:", row)
        menu = row.split(b'<details class="vehicle-actions">', 1)[1].split(b"</details>", 1)[0]
        self.assertIn(b">Unblock vehicle</button>", menu)

    def test_vehicle_management_shows_active_vehicles_first_and_can_filter_inactive(self) -> None:
        inactive_id = add_vehicle({"label": "INACTIVE-TEST-1", "group_name": "Archived"})
        with connect() as db:
            db.execute("UPDATE vehicles SET active = 0 WHERE id = ?", (inactive_id,))

        self._login("admin", "an-admin-password-long")
        active_page = self.client.get("/vehicles")
        self.assertIn(b"Active vehicles", active_page.data)
        self.assertIn(b"PT1023", active_page.data)
        self.assertNotIn(b"INACTIVE-TEST-1", active_page.data)
        self.assertIn(b'href="/vehicles?show=inactive" class="">Inactive vehicles</a>', active_page.data)
        self.assertIn(b'href="/vehicles" class="selected" aria-current="page">Active vehicles</a>', active_page.data)
        heading = active_page.data.split(b'<div class="vehicle-management-heading">', 1)[1].split(b"</div>", 1)[0]
        self.assertIn(b'aria-controls="add-vehicle-dialog"', heading)
        self.assertNotIn(b'id="vehicle-deactivate"', active_page.data)

        inactive_page = self.client.get("/vehicles?show=inactive")
        self.assertIn(b"Inactive vehicles", inactive_page.data)
        self.assertIn(b'href="/vehicles?show=inactive" class="selected" aria-current="page">Inactive vehicles</a>', inactive_page.data)
        self.assertIn(b"INACTIVE-TEST-1", inactive_page.data)
        inactive_table = inactive_page.data.split(b'<div class="table-wrap">', 1)[1]
        self.assertNotIn(b"PT1023", inactive_table)
        heading = inactive_page.data.split(b'<div class="vehicle-management-heading">', 1)[1].split(b"</div>", 1)[0]
        self.assertIn(b'id="vehicle-deactivate"', heading)
        self.assertNotIn(b"Add new vehicle", inactive_page.data)

    def test_vehicle_can_move_to_inactive_and_back(self) -> None:
        vehicle_id = add_vehicle({"label": "STATUS-CAR", "group_name": "MLBevo", "vin": "STATUS-VIN"})
        self._login("planner", self.planner_password)
        page = self.client.get("/vehicles?show=inactive")
        self.assertIn(b'id="vehicle-deactivate"', page.data)
        self.assertNotIn(b'id="vehicle-activate"', page.data)
        self.assertIn(b"confirmVehicleStatus", page.data)
        deactivate_select = page.data.split(b'id="vehicle-deactivate"', 1)[1].split(b"</select>", 1)[0]
        self.assertIn(f'value="{vehicle_id}"'.encode(), deactivate_select)

        response = self.client.post(
            "/vehicles/status",
            data={"vehicle_id": str(vehicle_id), "active": "0", "return_to": "active", "_csrf_token": self._csrf(page)},
        )
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/vehicles")
        self.assertIn(b"STATUS-CAR moved to the inactive list.", page.data)
        active_table = page.data.split(b'<div class="table-wrap">', 1)[1]
        self.assertNotIn(b"STATUS-CAR", active_table)
        self.assertNotIn(b"Make active again", page.data)
        inactive_page = self.client.get("/vehicles?show=inactive")
        self.assertIn(b'id="vehicle-deactivate"', inactive_page.data)
        inactive_row = inactive_page.data.split(b"<td>STATUS-CAR</td>", 1)[1].split(b"</tr>", 1)[0]
        self.assertIn(f'name="vehicle_id" value="{vehicle_id}"'.encode(), inactive_row)
        self.assertIn(b"Make active again</button>", inactive_row)
        self.assertNotIn(f'<option value="{vehicle_id}"'.encode(), self.client.get("/planner").data)
        rejected = self.client.post(
            "/planner/bookings/create",
            data={"vehicle_id": str(vehicle_id), "activity": "Inactive booking", "period": "KW47-2026",
                  "_csrf_token": self._csrf(page)},
        )
        self.assertEqual(rejected.status_code, 400)

        response = self.client.post(
            "/vehicles/status",
            data={"vehicle_id": str(vehicle_id), "active": "1", "return_to": "inactive", "_csrf_token": self._csrf(page)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/vehicles?show=inactive"))
        page = self.client.get("/vehicles")
        self.assertIn(b"STATUS-CAR", page.data.split(b'<div class="table-wrap">', 1)[1])
        self.assertIn(f'<option value="{vehicle_id}"'.encode(), self.client.get("/planner").data)
        self.assertEqual(
            self.client.post("/vehicles/status", data={"vehicle_id": "", "active": "0", "_csrf_token": self._csrf(page)}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/vehicles/status", data={"vehicle_id": "999999", "active": "0", "_csrf_token": self._csrf(page)}).status_code,
            404,
        )

        self.client.post("/logout", data={"_csrf_token": self._csrf(page)})
        self._login("admin", "an-admin-password-long")
        audit = self.client.get("/admin/audit").data
        self.assertIn(b"Deactivated vehicle", audit)
        self.assertIn(b"Reactivated vehicle", audit)

    def test_duplicate_vehicle_name_requires_explicit_confirmation(self) -> None:
        self._login("admin", "an-admin-password-long")
        page = self.client.get("/vehicles")
        response = self.client.post(
            "/vehicles/create",
            data={
                "label": "PT1023",
                "logger": "",
                "group_name": "E³ 1.2",
                "_csrf_token": self._csrf(page),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"A vehicle with this name already exists", response.data)
        self.assertIn(b"Add another vehicle with this name", response.data)
        with connect() as db:
            count_before_confirmation = db.execute(
                "SELECT COUNT(*) FROM vehicles WHERE label = 'PT1023' COLLATE NOCASE"
            ).fetchone()[0]
        self.assertEqual(count_before_confirmation, 1)

        confirmation = self.client.post(
            "/vehicles/create",
            data={
                "label": "PT1023",
                "logger": "",
                "group_name": "E³ 1.2",
                "confirm_duplicate": "1",
                "_csrf_token": self._csrf(response),
            },
        )
        self.assertEqual(confirmation.status_code, 302)
        with connect() as db:
            vehicles = db.execute(
                "SELECT id, group_name FROM vehicles WHERE label = 'PT1023' COLLATE NOCASE ORDER BY id"
            ).fetchall()
        self.assertEqual(len(vehicles), 2)
        self.assertEqual(vehicles[-1]["group_name"], "E³ 1.2")

    def test_vehicle_comments_are_editable_and_jira_assets_are_hidden(self) -> None:
        vehicle_id = add_vehicle(
            {"label": "COMMENT-CAR", "group_name": "MLBevo", "jira_asset_key": "FZG-777"}
        )
        self._login("planner", self.planner_password)
        page = self.client.get("/vehicles")
        self.assertNotIn(b"Jira Assets", page.data)
        self.assertNotIn(b"FZG-777", page.data)
        self.assertNotIn(b"<th>Comment</th>", page.data)
        self.assertIn(b'name="comment"', page.data)
        row = page.data.split(b"<td>COMMENT-CAR</td>", 1)[1].split(b"</tr>", 1)[0]
        before_menu, menu = row.split(b'<details class="vehicle-actions">', 1)
        self.assertNotIn(b">Add comment</button>", before_menu)
        self.assertIn(b">Add comment</button>", menu)
        following = page.data.split(b"<td>COMMENT-CAR</td>", 1)[1].split(b"</tr>", 1)[1]
        self.assertFalse(following.startswith(b'<tr class="vehicle-comment-note-row">'))

        response = self.client.post(
            f"/vehicles/{vehicle_id}/comment",
            data={"comment": "  Logger needs <calibration>  ", "_csrf_token": self._csrf(page)},
        )
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/vehicles")
        self.assertIn(b"Logger needs &lt;calibration&gt;", page.data)
        row = page.data.split(b"<td>COMMENT-CAR</td>", 1)[1].split(b"</tr>", 1)[0]
        before_menu, menu = row.split(b'<details class="vehicle-actions">', 1)
        self.assertNotIn(b'vehicle-comment-text', before_menu)
        following = page.data.split(b"<td>COMMENT-CAR</td>", 1)[1].split(b"</tr>", 1)[1]
        self.assertTrue(following.startswith(
            b'<tr class="vehicle-comment-note-row"><td colspan="9"><div class="vehicle-comment-text">'
            b'<strong>Note:</strong> Logger needs &lt;calibration&gt;</div></td></tr>'
        ))
        self.assertIn(b">Edit comment</button>", menu)
        self.assertIn(b"Logger needs &lt;calibration&gt;</textarea>", menu)
        too_long = self.client.post(
            f"/vehicles/{vehicle_id}/comment",
            data={"comment": "x" * 2001, "_csrf_token": self._csrf(page)},
        )
        self.assertEqual(too_long.status_code, 400)

        edit_page = self.client.get(f"/vehicles/{vehicle_id}/edit")
        self.assertNotIn(b"jira_asset_key", edit_page.data)
        self.assertIn(b"Logger needs &lt;calibration&gt;</textarea>", edit_page.data)
        response = self.client.post(
            f"/vehicles/{vehicle_id}/edit",
            data={"label": "COMMENT-CAR", "group_name": "MLBevo", "comment": "Updated in edit form",
                  "_csrf_token": self._csrf(edit_page)},
        )
        self.assertEqual(response.status_code, 302)
        with connect() as db:
            saved = db.execute("SELECT comment, jira_asset_key FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
        self.assertEqual(saved["comment"], "Updated in edit form")
        self.assertEqual(saved["jira_asset_key"], "FZG-777")
        history = self.client.get(f"/planner/history/vehicle/{vehicle_id}")
        self.assertIn(b"Updated in edit form", history.data)
        self.assertNotIn(b"Jira Assets", history.data)
        cleared = self.client.post(
            f"/vehicles/{vehicle_id}/comment", data={"comment": "  ", "_csrf_token": self._csrf(history)},
        )
        self.assertEqual(cleared.status_code, 302)
        page = self.client.get("/vehicles")
        row, following = page.data.split(b"<td>COMMENT-CAR</td>", 1)[1].split(b"</tr>", 1)
        self.assertIn(b">Add comment</button>", row)
        self.assertFalse(following.startswith(b'<tr class="vehicle-comment-note-row">'))

        self.client.post("/logout", data={"_csrf_token": self._csrf(history)})
        self._login("admin", "an-admin-password-long")
        self.assertIn(b"Updated vehicle comment", self.client.get("/admin/audit").data)

    def test_only_admin_can_delete_vehicle_without_bookings(self) -> None:
        unused_id = add_vehicle({"label": "DELETE-ME", "group_name": "MLBevo"})
        booked_id = add_vehicle({"label": "HAS-HISTORY", "group_name": "MLBevo"})
        create_booking(booked_id, "Kept history", "KW47-2026", "", "", "", "planner")

        self._login("planner", self.planner_password)
        vehicles_page = self.client.get("/vehicles")
        self.assertIn(b'aria-controls="add-vehicle-dialog"', vehicles_page.data)
        self.assertNotIn(b"#delete-vehicle", vehicles_page.data)
        edit_page = self.client.get(f"/vehicles/{unused_id}/edit")
        self.assertEqual(edit_page.status_code, 200)
        self.assertNotIn(b"Delete vehicle", edit_page.data)
        forbidden = self.client.post(f"/vehicles/{unused_id}/delete", data={"_csrf_token": self._csrf(edit_page)})
        self.assertEqual(forbidden.status_code, 403)
        self.client.post("/logout", data={"_csrf_token": self._csrf(edit_page)})

        self._login("admin", "an-admin-password-long")
        vehicles_page = self.client.get("/vehicles")
        self.assertIn(f'href="/vehicles/{unused_id}/edit#delete-vehicle"'.encode(), vehicles_page.data)
        booked_page = self.client.get(f"/vehicles/{booked_id}/edit")
        self.assertIn(b"has 1 booking(s), so it cannot be deleted", booked_page.data)
        self.assertNotIn(b'class="danger-button"', booked_page.data)
        blocked = self.client.post(f"/vehicles/{booked_id}/delete", data={"_csrf_token": self._csrf(booked_page)})
        self.assertEqual(blocked.status_code, 409)

        edit_page = self.client.get(f"/vehicles/{unused_id}/edit")
        self.assertIn(f'action="/vehicles/{unused_id}/delete"'.encode(), edit_page.data)
        self.assertIn(b"cannot be undone", edit_page.data)
        response = self.client.post(f"/vehicles/{unused_id}/delete", data={"_csrf_token": self._csrf(edit_page)})
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"DELETE-ME was deleted.", self.client.get("/vehicles").data)
        with connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM vehicles WHERE id = ?", (unused_id,)).fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM vehicles WHERE id = ?", (booked_id,)).fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM bookings WHERE vehicle_id = ?", (booked_id,)).fetchone())
        self.assertEqual(self.client.get(f"/vehicles/{unused_id}/edit").status_code, 404)
        self.assertIn(b"Deleted vehicle", self.client.get("/admin/audit").data)

    def test_manual_vehicle_requires_a_section(self) -> None:
        self._login("admin", "an-admin-password-long")
        page = self.client.get("/vehicles")
        response = self.client.post(
            "/vehicles/create",
            data={"label": "NO-SECTION-TEST", "_csrf_token": self._csrf(page)},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Choose or enter a vehicle section", response.data)

    def test_login_temporarily_locks_after_repeated_failures(self) -> None:
        for _ in range(5):
            page = self.client.get("/login")
            response = self.client.post(
                "/login",
                data={
                    "username": "unknown-user",
                    "password": "incorrect",
                    "_csrf_token": self._csrf(page),
                },
            )
            self.assertEqual(response.status_code, 401)

        page = self.client.get("/login")
        response = self.client.post(
            "/login",
            data={
                "username": "unknown-user",
                "password": "incorrect",
                "_csrf_token": self._csrf(page),
            },
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn(b"Sign-in failed", response.data)

    def test_approved_booking_can_be_returned_to_pending_or_rejected(self) -> None:
        booking_id = create_booking(
            self.vehicle_id,
            "Campaign activity",
            "KW47-2026",
            "",
            "",
            "analysis_checklist",
            "planner",
        )
        self._login("admin", "an-admin-password-long")
        page = self.client.get("/planner/review")
        response = self.client.post(
            f"/planner/bookings/{booking_id}/review",
            data={"decision": "approved", "_csrf_token": self._csrf(page)},
        )
        self.assertEqual(response.status_code, 302)

        review_page = self.client.get("/planner/review?status=approved")
        self.assertIn(b"Return to approve/reject", review_page.data)
        self.assertIn(b"TEST-VIN-1", review_page.data)
        response = self.client.post(
            f"/planner/bookings/{booking_id}/review",
            data={"decision": "pending", "_csrf_token": self._csrf(review_page)},
        )
        self.assertEqual(response.status_code, 302)

        review_page = self.client.get("/planner/review")
        self.assertIn(b"Approve", review_page.data)
        self.assertIn(b"Reject", review_page.data)
        response = self.client.post(
            f"/planner/bookings/{booking_id}/review",
            data={"decision": "rejected", "_csrf_token": self._csrf(review_page)},
        )
        self.assertEqual(response.status_code, 302)


if __name__ == "__main__":
    unittest.main()
