from __future__ import annotations

import tempfile
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from planner_store import (
    add_vehicle,
    acquire_booking_edit,
    authenticate,
    BookingChanged,
    BookingConflict,
    clear_login_failures,
    claim_booking_creation,
    claim_pending_booking_creation,
    complete_booking_creation,
    connect,
    create_booking,
    create_first_admin,
    create_user,
    get_booking,
    get_vehicle,
    import_vehicle_tab_history,
    import_workbook_data,
    initialize_database,
    login_is_locked,
    list_vehicles,
    list_recent_booking_events,
    list_users,
    planner_revision,
    release_booking_edit,
    fail_booking_creation,
    record_login_failure,
    record_audit_event,
    reapprove_failed_booking,
    review_booking,
    sync_workbook_updates,
    update_imported_activity_colors,
    update_imported_activity_spans,
    update_user_access,
    update_vehicle_groups,
    update_booking,
    set_vehicle_blocked,
    vehicle_block_bounds,
)


class PlannerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "planner.sqlite3"
        initialize_database(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_admin_bootstrap_and_temporary_user_password(self) -> None:
        create_first_admin("first.admin", "a-very-long-password", self.db_path)
        self.assertIsNotNone(authenticate("FIRST.ADMIN", "a-very-long-password", self.db_path))
        with self.assertRaises(ValueError):
            create_first_admin("second.admin", "another-long-password", self.db_path)

        temporary_password = create_user("test.planner", "planner", self.db_path)
        user = authenticate("test.planner", temporary_password, self.db_path)
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], "planner")
        self.assertEqual(user["must_change_password"], 0)

    def test_recent_booking_events_filter_and_limit_before_sorting(self) -> None:
        for number in range(7):
            record_audit_event("tester", f"Submitted booking #{number}", db_path=self.db_path)
        record_audit_event("tester", "Signed in", db_path=self.db_path)
        record_audit_event("reviewer", "Booking rejected", "Booking #3", db_path=self.db_path)
        record_audit_event("tester", "Edited booking #4", "Changed fields: dates", db_path=self.db_path)
        events = list_recent_booking_events(self.db_path)
        self.assertEqual(
            [event["action"] for event in events],
            ["Edited booking #4", "Booking rejected", "Submitted booking #6",
             "Submitted booking #5", "Submitted booking #4"],
        )
        self.assertEqual(events[1]["details"], "Booking #3")

    def test_booking_conflicts_cover_ranges_weeks_and_rejected_bookings(self) -> None:
        vehicle_id = add_vehicle({"label": "CONFLICT-CAR"}, self.db_path)
        first_id = create_booking(
            vehicle_id, "First", "KW24-2026", "2026-06-12", "", "", "First user",
            self.db_path, planned_end_date="2026-06-14",
        )
        with self.assertRaisesRegex(BookingConflict, "First user"):
            create_booking(vehicle_id, "Overlap", "KW24-2026", "", "", "", "Second", self.db_path)
        saturday_after = create_booking(
            vehicle_id, "Next", "KW25-2026", "2026-06-15", "", "", "Second", self.db_path
        )
        with self.assertRaises(BookingConflict):
            update_booking(
                saturday_after, vehicle_id, "Next", "KW24-2026", "2026-06-14", "",
                self.db_path,
            )
        review_booking(first_id, "rejected", "reviewer", self.db_path)
        create_booking(vehicle_id, "Now available", "KW24-2026", "2026-06-14", "", "", "Second", self.db_path)

    def test_simultaneous_booking_attempts_do_not_both_succeed(self) -> None:
        vehicle_id = add_vehicle({"label": "RACE-CAR"}, self.db_path)
        barrier = threading.Barrier(2)

        def reserve(actor: str) -> str:
            barrier.wait(timeout=10)
            try:
                create_booking(vehicle_id, "Race", "KW24-2026", "2026-06-12", "", "", actor, self.db_path)
            except BookingConflict:
                return "blocked"
            return "created"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, ("one", "two")))
        self.assertCountEqual(results, ["created", "blocked"])

    def test_temporary_vehicle_block_dates_and_unblocking_preserve_bookings(self) -> None:
        vehicle_id = add_vehicle({"label": "BLOCK-CAR"}, self.db_path)
        today = date.today()
        period = f"KW{today.isocalendar().week:02d}-{today.isocalendar().year}"
        end = today + timedelta(days=2)
        existing = create_booking(vehicle_id, "Existing", period, today.isoformat(), "", "", "First", self.db_path)
        before = planner_revision(self.db_path)
        set_vehicle_blocked(vehicle_id, True, "Repair", end.isoformat(), self.db_path)
        self.assertGreater(planner_revision(self.db_path), before)
        self.assertEqual(vehicle_block_bounds(get_vehicle(vehicle_id, self.db_path)), (today, end))
        with self.assertRaisesRegex(BookingConflict, "Repair"):
            create_booking(vehicle_id, "Blocked end", period, end.isoformat(), "", "", "Second", self.db_path)
        with self.assertRaises(BookingConflict):
            create_booking(
                vehicle_id, "Crosses block", period, (today - timedelta(days=1)).isoformat(), "", "", "Second",
                self.db_path, planned_end_date=today.isoformat(),
            )
        after = create_booking(vehicle_id, "After", period, (end + timedelta(days=1)).isoformat(), "", "", "Second", self.db_path)
        with self.assertRaises(BookingConflict):
            update_booking(after, vehicle_id, "Moved inside", period, end.isoformat(), "", self.db_path)
        update_booking(existing, vehicle_id, "Kept existing", period, today.isoformat(), "Notes", self.db_path)
        self.assertEqual(get_booking(existing, self.db_path)["activity"], "Kept existing")
        set_vehicle_blocked(vehicle_id, False, db_path=self.db_path)
        vehicle = get_vehicle(vehicle_id, self.db_path)
        self.assertIsNone(vehicle_block_bounds(vehicle))
        self.assertEqual(vehicle["blocked_reason"], "")
        self.assertEqual(vehicle["active"], 1)
        create_booking(vehicle_id, "Now allowed", period, end.isoformat(), "", "", "Second", self.db_path)

    def test_open_ended_expired_and_invalid_vehicle_blocks(self) -> None:
        vehicle_id = add_vehicle({"label": "OPEN-BLOCK"}, self.db_path)
        today = date.today()
        period = f"KW{today.isocalendar().week:02d}-{today.isocalendar().year}"
        set_vehicle_blocked(vehicle_id, True, db_path=self.db_path)
        with self.assertRaises(BookingConflict):
            create_booking(
                vehicle_id, "Future", period, (today + timedelta(days=400)).isoformat(), "", "", "planner", self.db_path,
            )
        for invalid in ("not-a-date", (today - timedelta(days=1)).isoformat()):
            with self.subTest(until=invalid), self.assertRaises(ValueError):
                set_vehicle_blocked(vehicle_id, True, until=invalid, db_path=self.db_path)
        with self.assertRaises(ValueError):
            set_vehicle_blocked(vehicle_id, True, reason="x" * 2001, db_path=self.db_path)
        self.assertEqual(vehicle_block_bounds(get_vehicle(vehicle_id, self.db_path))[1], date.max)
        with connect(self.db_path) as db:
            db.execute(
                "UPDATE vehicles SET blocked_from = ?, blocked_until = ? WHERE id = ?",
                ((today - timedelta(days=2)).isoformat(), (today - timedelta(days=1)).isoformat(), vehicle_id),
            )
        self.assertIsNone(vehicle_block_bounds(get_vehicle(vehicle_id, self.db_path)))
        create_booking(vehicle_id, "Expired", period, today.isoformat(), "", "", "planner", self.db_path)

    def test_edit_lease_and_revision_prevent_stale_overwrites(self) -> None:
        vehicle_id = add_vehicle({"label": "EDIT-CAR"}, self.db_path)
        booking_id = create_booking(vehicle_id, "Original", "KW24-2026", "2026-06-12", "", "", "planner", self.db_path)
        original = get_booking(booking_id, self.db_path)
        self.assertIsNone(acquire_booking_edit(booking_id, "token-one", "First", self.db_path))
        self.assertEqual(acquire_booking_edit(booking_id, "token-two", "Second", self.db_path), "First")
        before = planner_revision(self.db_path)
        with connect(self.db_path) as db:
            db.execute("UPDATE bookings SET description = 'Another change' WHERE id = ?", (booking_id,))
        self.assertGreater(planner_revision(self.db_path), before)
        with self.assertRaises(BookingChanged):
            update_booking(
                booking_id, vehicle_id, "Stale", "KW24-2026", "2026-06-12", "",
                self.db_path, expected_revision=original["revision"], edit_token="token-one",
            )
        self.assertEqual(get_booking(booking_id, self.db_path)["description"], "Another change")
        release_booking_edit(booking_id, "token-one", self.db_path)
        self.assertIsNone(acquire_booking_edit(booking_id, "token-two", "Second", self.db_path))

    def test_expired_edit_lease_can_be_taken_over_and_old_token_cannot_save(self) -> None:
        vehicle_id = add_vehicle({"label": "EXPIRED-EDIT"}, self.db_path)
        booking_id = create_booking(vehicle_id, "Original", "KW24-2026", "2026-06-12", "", "", "planner", self.db_path)
        revision = get_booking(booking_id, self.db_path)["revision"]
        self.assertIsNone(acquire_booking_edit(booking_id, "old", "First", self.db_path))
        with connect(self.db_path) as db:
            db.execute("UPDATE booking_edit_leases SET expires_at = 0 WHERE booking_id = ?", (booking_id,))
        self.assertIsNone(acquire_booking_edit(booking_id, "new", "Second", self.db_path))
        release_booking_edit(booking_id, "old", self.db_path)
        with self.assertRaises(BookingChanged):
            update_booking(
                booking_id, vehicle_id, "Old edit", "KW24-2026", "2026-06-12", "",
                self.db_path, expected_revision=revision, edit_token="old",
            )
        self.assertEqual(acquire_booking_edit(booking_id, "old", "First", self.db_path), "Second")

    def test_duplicate_vehicle_labels_keep_booking_foreign_keys(self) -> None:
        first_id = add_vehicle({"label": "PT1023", "group_name": "MLBevo"}, self.db_path)
        second_id = add_vehicle({"label": "pt1023", "group_name": "E³ 1.2"}, self.db_path)
        booking_id = create_booking(
            second_id, "Campaign test", "KW47-2026", "", "", "analysis_checklist", "planner", self.db_path
        )
        self.assertNotEqual(first_id, second_id)
        with connect(self.db_path) as db:
            booking = db.execute("SELECT vehicle_id FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            foreign_key_violations = db.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(booking["vehicle_id"], second_id)
        self.assertEqual(foreign_key_violations, [])
        self.assertEqual(len(list_vehicles(self.db_path)), 2)

    def test_legacy_vehicle_unique_constraint_is_migrated(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        db = sqlite3.connect(legacy_path)
        db.executescript(
            """
                CREATE TABLE vehicles (
                    id INTEGER PRIMARY KEY,
                    label TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    logger TEXT NOT NULL DEFAULT '',
                    series TEXT NOT NULL DEFAULT '',
                    cluster TEXT NOT NULL DEFAULT '',
                    market TEXT NOT NULL DEFAULT '',
                    vr_stand TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL DEFAULT '',
                    vin TEXT NOT NULL DEFAULT '',
                    jira_asset_key TEXT NOT NULL DEFAULT '',
                    group_name TEXT NOT NULL DEFAULT '',
                    group_order INTEGER NOT NULL DEFAULT 0,
                    display_order INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE bookings (
                    id INTEGER PRIMARY KEY,
                    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
                    activity TEXT NOT NULL,
                    period TEXT NOT NULL,
                    planned_date TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    checklist_template TEXT NOT NULL DEFAULT 'analysis_checklist',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_by TEXT NOT NULL,
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    jira_issue_key TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO vehicles (id, label, group_name) VALUES (7, 'PT1023', 'MLBevo');
                INSERT INTO bookings (id, vehicle_id, activity, period, created_by)
                VALUES (11, 7, 'Old activity', 'KW01-2026', 'planner');
            """
        )
        db.close()
        initialize_database(legacy_path)
        duplicate_id = add_vehicle({"label": "pt1023"}, legacy_path)
        with connect(legacy_path) as db:
            booking = db.execute("SELECT vehicle_id FROM bookings WHERE id = 11").fetchone()
            violations = db.execute("PRAGMA foreign_key_check").fetchall()
        self.assertNotEqual(duplicate_id, 7)
        self.assertEqual(booking["vehicle_id"], 7)
        self.assertEqual(violations, [])

    def test_last_active_admin_cannot_be_demoted_or_disabled(self) -> None:
        create_first_admin("admin", "a-very-long-password", self.db_path)
        admin_id = list_users(self.db_path)[0]["id"]
        with self.assertRaisesRegex(ValueError, "last active admin"):
            update_user_access(admin_id, "planner", True, self.db_path)
        with self.assertRaisesRegex(ValueError, "last active admin"):
            update_user_access(admin_id, None, False, self.db_path)

    def test_login_lockout_clears_after_successful_authentication(self) -> None:
        for _ in range(5):
            record_login_failure("planner", "127.0.0.1", self.db_path)
        self.assertTrue(login_is_locked("PLANNER", "127.0.0.1", self.db_path))
        self.assertFalse(login_is_locked("planner", "127.0.0.2", self.db_path))

        clear_login_failures("planner", "127.0.0.1", self.db_path)
        self.assertFalse(login_is_locked("planner", "127.0.0.1", self.db_path))

    def test_booking_review_and_ticket_creation_are_single_use(self) -> None:
        vehicle_id = add_vehicle(
            {
                "label": "PT1023",
                "logger": "Yes",
                "vin": "VIN-1",
                "jira_asset_key": "FZG-1",
            },
            self.db_path,
        )
        booking_id = create_booking(
            vehicle_id,
            "Campaign test",
            "KW47-2026",
            "2026-11-18",
            "",
            "analysis_checklist",
            "planner",
            self.db_path,
        )
        review_booking(booking_id, "approved", "automation", self.db_path)
        self.assertTrue(claim_booking_creation(booking_id, self.db_path))
        self.assertFalse(claim_booking_creation(booking_id, self.db_path))
        complete_booking_creation(booking_id, "PHUB-123", self.db_path)
        booking = get_booking(booking_id, self.db_path)
        self.assertEqual(booking["status"], "created")
        self.assertEqual(booking["jira_issue_key"], "PHUB-123")
        with self.assertRaises(ValueError):
            review_booking(booking_id, "rejected", "automation", self.db_path)

    def test_approved_booking_can_return_to_pending_but_not_after_creation_starts(self) -> None:
        vehicle_id = add_vehicle({"label": "PT1023"}, self.db_path)
        booking_id = create_booking(
            vehicle_id, "Campaign test", "KW47-2026", "", "", "analysis_checklist", "planner", self.db_path
        )
        review_booking(booking_id, "approved", "automation", self.db_path)
        review_booking(booking_id, "pending", "automation", self.db_path)
        self.assertEqual(get_booking(booking_id, self.db_path)["status"], "pending")

        review_booking(booking_id, "approved", "automation", self.db_path)
        self.assertTrue(claim_booking_creation(booking_id, self.db_path))
        with self.assertRaisesRegex(ValueError, "no longer available for review"):
            review_booking(booking_id, "pending", "automation", self.db_path)

        another_booking_id = create_booking(
            vehicle_id, "Another campaign", "KW48-2026", "", "", "analysis_checklist", "planner", self.db_path
        )
        review_booking(another_booking_id, "approved", "automation", self.db_path)
        review_booking(another_booking_id, "rejected", "automation", self.db_path)
        self.assertEqual(get_booking(another_booking_id, self.db_path)["status"], "rejected")

    def test_failed_ticket_requires_explicit_review_before_retry(self) -> None:
        vehicle_id = add_vehicle({"label": "PT1023"}, self.db_path)
        booking_id = create_booking(
            vehicle_id, "Campaign test", "KW47-2026", "", "", "analysis_checklist", "planner", self.db_path
        )
        review_booking(booking_id, "approved", "automation", self.db_path)
        self.assertTrue(claim_booking_creation(booking_id, self.db_path))
        fail_booking_creation(booking_id, "ConnectError", self.db_path)
        self.assertEqual(get_booking(booking_id, self.db_path)["status"], "creation_error")
        reapprove_failed_booking(booking_id, self.db_path)
        self.assertTrue(claim_booking_creation(booking_id, self.db_path))

    def test_pending_ticket_claim_rejects_stale_and_duplicate_previews(self) -> None:
        vehicle_id = add_vehicle({"label": "PT1023"}, self.db_path)
        booking_id = create_booking(
            vehicle_id, "Campaign", "KW47-2026", "", "", "", "planner", self.db_path
        )
        revision = get_booking(booking_id, self.db_path)["revision"]
        with connect(self.db_path) as db:
            db.execute("UPDATE bookings SET description = 'Changed' WHERE id = ?", (booking_id,))
        self.assertFalse(claim_pending_booking_creation(booking_id, "reviewer", revision, "analysis_checklist", self.db_path))
        revision = get_booking(booking_id, self.db_path)["revision"]
        self.assertTrue(claim_pending_booking_creation(booking_id, "reviewer", revision, "analysis_checklist", self.db_path))
        self.assertFalse(claim_pending_booking_creation(booking_id, "other", revision, "analysis_checklist", self.db_path))
        booking = get_booking(booking_id, self.db_path)
        self.assertEqual((booking["status"], booking["reviewed_by"]), ("creating", "reviewer"))

    def test_workbook_import_merges_existing_vehicles_and_runs_once(self) -> None:
        existing_vehicle_id = add_vehicle(
            {"label": "PT1023", "vin": "VIN-EXISTING", "series": "Manually maintained"},
            self.db_path,
        )
        vehicle = {
            "label": "pt1023",
            "vin": "VIN-WORKBOOK",
            "series": "Workbook series",
            "market": "Germany",
        }
        booking = {
            "vehicle_label": "PT1023",
            "activity": "Campaign test",
            "period": "KW47-2020",
            "planned_date": "2020-11-18",
            "activity_color": "#12AB34",
            "planned_end_date": "2020-11-20",
            "checklist_template": "analysis_checklist",
        }
        counts = import_workbook_data("hash", "calendar.xlsx", [vehicle], [booking], self.db_path)
        self.assertEqual(counts, (1, 1))
        vehicles = list_vehicles(self.db_path)
        self.assertEqual(len(vehicles), 1)
        self.assertEqual(vehicles[0]["id"], existing_vehicle_id)
        self.assertEqual(vehicles[0]["vin"], "VIN-EXISTING")
        self.assertEqual(vehicles[0]["series"], "Manually maintained")
        self.assertEqual(vehicles[0]["market"], "Germany")
        with connect(self.db_path) as db:
            imported_booking = db.execute(
                "SELECT vehicle_id, status, activity_color, planned_end_date "
                "FROM bookings WHERE created_by = 'excel-import'"
            ).fetchone()
        self.assertEqual(imported_booking["vehicle_id"], existing_vehicle_id)
        self.assertEqual(imported_booking["status"], "historical")
        self.assertEqual(imported_booking["activity_color"], "#12AB34")
        self.assertEqual(imported_booking["planned_end_date"], "2020-11-20")
        with connect(self.db_path) as db:
            db.execute("UPDATE bookings SET activity_color = '' WHERE created_by = 'excel-import'")
        self.assertEqual(
            update_imported_activity_colors(
                [{**booking, "activity": "Campaign test", "activity_color": "#12AB34"}],
                self.db_path,
            ),
            1,
        )
        with connect(self.db_path) as db:
            recolored_booking = db.execute(
                "SELECT activity_color FROM bookings WHERE created_by = 'excel-import'"
            ).fetchone()
        self.assertEqual(recolored_booking["activity_color"], "#12AB34")
        with connect(self.db_path) as db:
            db.execute("UPDATE bookings SET planned_end_date = '' WHERE created_by = 'excel-import'")
        self.assertEqual(
            update_imported_activity_spans(
                [{**booking, "activity": "Campaign test"}],
                self.db_path,
            ),
            1,
        )
        with connect(self.db_path) as db:
            spanning_booking = db.execute(
                "SELECT planned_end_date FROM bookings WHERE created_by = 'excel-import'"
            ).fetchone()
        self.assertEqual(spanning_booking["planned_end_date"], "2020-11-20")
        self.assertEqual(
            import_vehicle_tab_history(
                "hash",
                "calendar.xlsx",
                [{
                    "vehicle_label": "PT1023",
                    "activity": "Detailed note",
                    "period": "KW31-2026",
                    "planned_date": "2026-07-28",
                    "description": "Recorded by: Namik",
                }],
                self.db_path,
            ),
            1,
        )
        with connect(self.db_path) as db:
            detail = db.execute(
                "SELECT vehicle_id, status, description FROM bookings WHERE created_by = 'excel-detail-import'"
            ).fetchone()
        self.assertEqual(detail["vehicle_id"], existing_vehicle_id)
        self.assertEqual(detail["status"], "historical")
        self.assertEqual(detail["description"], "Recorded by: Namik")
        with self.assertRaisesRegex(ValueError, "already been imported"):
            import_vehicle_tab_history("hash", "calendar.xlsx", [], self.db_path)
        with self.assertRaisesRegex(ValueError, "already been imported"):
            import_workbook_data("different-hash", "calendar.xlsx", [vehicle], [booking], self.db_path)

    def test_update_vehicle_groups_preserves_workbook_order(self) -> None:
        add_vehicle({"label": "SEU780"}, self.db_path)
        add_vehicle({"label": "SEP861"}, self.db_path)
        grouped = [
            {"label": "SEP861", "group_name": "MLBevo", "group_order": "0", "display_order": "0"},
            {"label": "SEU780", "group_name": "E³ 1.2", "group_order": "1", "display_order": "1"},
        ]
        self.assertEqual(update_vehicle_groups(grouped, self.db_path), 2)
        vehicles = list_vehicles(self.db_path)
        self.assertEqual([vehicle["label"] for vehicle in vehicles], ["SEP861", "SEU780"])
        self.assertEqual([vehicle["group_name"] for vehicle in vehicles], ["MLBevo", "E³ 1.2"])

    def test_workbook_update_adds_new_records_without_overwriting_existing_data(self) -> None:
        existing_vehicle_id = add_vehicle(
            {"label": "PT1023", "series": "Manually maintained", "group_name": "Old group"},
            self.db_path,
        )
        existing_booking_id = create_booking(
            existing_vehicle_id,
            "Existing activity",
            "KW47-2026",
            "2026-11-18",
            "",
            "analysis_checklist",
            "planner",
            self.db_path,
        )
        vehicles = [
            {
                "label": "PT1023",
                "series": "Workbook series",
                "market": "Germany",
                "group_name": "MLBevo",
                "group_order": "0",
                "display_order": "0",
            },
            {
                "label": "SE0534",
                "series": "H2",
                "group_name": "E³ 1.2",
                "group_order": "1",
                "display_order": "1",
            },
        ]
        bookings = [
            {
                "vehicle_label": "PT1023",
                "activity": "Existing activity",
                "period": "KW47-2026",
                "planned_date": "2026-11-18",
            },
            {
                "vehicle_label": "SE0534",
                "activity": "New historical activity",
                "period": "KW01-2020",
                "planned_date": "2020-01-02",
                "planned_end_date": "2020-01-03",
            },
        ]

        preview = sync_workbook_updates(vehicles, bookings, self.db_path)
        self.assertEqual(preview, {"vehicles_added": 1, "vehicles_updated": 1, "bookings_added": 1})
        self.assertEqual(len(list_vehicles(self.db_path)), 1)

        applied = sync_workbook_updates(vehicles, bookings, self.db_path, apply=True)
        self.assertEqual(applied, preview)
        self.assertEqual(
            sync_workbook_updates(vehicles, bookings, self.db_path, apply=True),
            {"vehicles_added": 0, "vehicles_updated": 0, "bookings_added": 0},
        )
        updated_vehicle = next(
            vehicle for vehicle in list_vehicles(self.db_path) if vehicle["label"] == "PT1023"
        )
        self.assertEqual(updated_vehicle["series"], "Manually maintained")
        self.assertEqual(updated_vehicle["market"], "Germany")
        self.assertEqual(updated_vehicle["group_name"], "MLBevo")
        with connect(self.db_path) as db:
            existing = db.execute(
                "SELECT id, created_by FROM bookings WHERE id = ?", (existing_booking_id,)
            ).fetchone()
            imported = db.execute(
                "SELECT status, planned_end_date, created_by FROM bookings "
                "WHERE activity = 'New historical activity'"
            ).fetchone()
            booking_count = db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        self.assertEqual(existing["created_by"], "planner")
        self.assertEqual(imported["status"], "historical")
        self.assertEqual(imported["planned_end_date"], "2020-01-03")
        self.assertEqual(imported["created_by"], "excel-import")
        self.assertEqual(booking_count, 2)


if __name__ == "__main__":
    unittest.main()
