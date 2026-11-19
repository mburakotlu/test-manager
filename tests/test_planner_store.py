from __future__ import annotations

import tempfile
import sqlite3
import unittest
from pathlib import Path

from planner_store import (
    add_vehicle,
    authenticate,
    clear_login_failures,
    claim_booking_creation,
    complete_booking_creation,
    connect,
    create_booking,
    create_first_admin,
    create_user,
    get_booking,
    import_vehicle_tab_history,
    import_workbook_data,
    initialize_database,
    login_is_locked,
    list_vehicles,
    list_users,
    fail_booking_creation,
    record_login_failure,
    reapprove_failed_booking,
    review_booking,
    sync_workbook_updates,
    update_imported_activity_colors,
    update_imported_activity_spans,
    update_user_access,
    update_vehicle_groups,
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
