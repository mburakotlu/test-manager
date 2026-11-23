"""Persistent SQLite storage for planner users, vehicles, and bookings."""

from __future__ import annotations

import os
import hashlib
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from werkzeug.security import check_password_hash, generate_password_hash

from app_paths import APP_DIR

ROLES = ("admin", "automation", "planner")
BOOKING_STATUSES = ("pending", "approved", "rejected", "historical", "creating", "created", "creation_error")
BOOKING_PROGRESS = ("not_started", "in_progress", "closed")
# Work progress can only be tracked once a booking has been approved.
PROGRESS_EDITABLE_STATUSES = ("approved", "created")
MIN_PASSWORD_LENGTH = 12
MAX_VEHICLE_COMMENT_LENGTH = 2000


class BookingConflict(ValueError):
    """The selected vehicle has a booking during the requested dates."""


class BookingChanged(ValueError):
    """The booking changed since the edit form was opened."""


def database_path() -> Path:
    configured = os.environ.get("APP_DATABASE_PATH", "").strip()
    return Path(configured).expanduser().resolve() if configured else APP_DIR / "data" / "planner.sqlite3"


@contextmanager
def connect(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path) if db_path else database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(db_path: str | Path | None = None) -> None:
    with connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'automation', 'planner')),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS vehicles (
                id INTEGER PRIMARY KEY,
                label TEXT NOT NULL COLLATE NOCASE,
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
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY,
                vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
                activity TEXT NOT NULL,
                period TEXT NOT NULL,
                planned_date TEXT NOT NULL DEFAULT '',
                planned_end_date TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                activity_color TEXT NOT NULL DEFAULT '',
                checklist_template TEXT NOT NULL DEFAULT 'analysis_checklist',
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected', 'historical', 'creating', 'created', 'creation_error')),
                created_by TEXT NOT NULL,
                reviewed_by TEXT NOT NULL DEFAULT '',
                jira_issue_key TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS bookings_status_period_idx ON bookings(status, period);
            CREATE INDEX IF NOT EXISTS bookings_vehicle_idx ON bookings(vehicle_id);
            CREATE TABLE IF NOT EXISTS planner_imports (
                source_hash TEXT PRIMARY KEY,
                source_name TEXT NOT NULL,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                vehicle_count INTEGER NOT NULL,
                booking_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                attempt_key TEXT PRIMARY KEY,
                failures INTEGER NOT NULL,
                window_started_at TEXT NOT NULL,
                locked_until TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS audit_events_created_at_idx ON audit_events(created_at);
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS vehicles_label_idx ON vehicles(label COLLATE NOCASE)")
        booking_columns = {row["name"] for row in db.execute("PRAGMA table_info(bookings)")}
        if "planned_end_date" not in booking_columns:
            db.execute("ALTER TABLE bookings ADD COLUMN planned_end_date TEXT NOT NULL DEFAULT ''")
        if "activity_color" not in booking_columns:
            db.execute("ALTER TABLE bookings ADD COLUMN activity_color TEXT NOT NULL DEFAULT ''")
        if "checklist_template" not in booking_columns:
            db.execute(
                "ALTER TABLE bookings ADD COLUMN checklist_template TEXT NOT NULL DEFAULT 'analysis_checklist'"
            )
        if "progress" not in booking_columns:
            db.execute("ALTER TABLE bookings ADD COLUMN progress TEXT NOT NULL DEFAULT 'not_started'")
        if "revision" not in booking_columns:
            db.execute("ALTER TABLE bookings ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        vehicle_columns = {row["name"] for row in db.execute("PRAGMA table_info(vehicles)")}
        for column, definition in (
            ("group_name", "TEXT NOT NULL DEFAULT ''"),
            ("group_order", "INTEGER NOT NULL DEFAULT 0"),
            ("display_order", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in vehicle_columns:
                db.execute(f"ALTER TABLE vehicles ADD COLUMN {column} {definition}")
    _migrate_vehicle_labels(Path(db_path) if db_path else database_path())
    with connect(db_path) as db:
        vehicle_columns = {row["name"] for row in db.execute("PRAGMA table_info(vehicles)")}
        if "comment" not in vehicle_columns:
            db.execute("ALTER TABLE vehicles ADD COLUMN comment TEXT NOT NULL DEFAULT ''")
        for column in ("blocked_from", "blocked_until", "blocked_reason"):
            if column not in vehicle_columns:
                db.execute(f"ALTER TABLE vehicles ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS booking_edit_leases (
                booking_id INTEGER PRIMARY KEY REFERENCES bookings(id) ON DELETE CASCADE,
                token TEXT NOT NULL,
                actor TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS planner_change_counter (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                revision INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO planner_change_counter (id) VALUES (1);
            CREATE TRIGGER IF NOT EXISTS booking_revision_update AFTER UPDATE ON bookings
            WHEN NEW.revision = OLD.revision
            BEGIN
                UPDATE bookings SET revision = OLD.revision + 1 WHERE id = NEW.id;
            END;
            CREATE TRIGGER IF NOT EXISTS planner_bookings_insert AFTER INSERT ON bookings
            BEGIN UPDATE planner_change_counter SET revision = revision + 1 WHERE id = 1; END;
            CREATE TRIGGER IF NOT EXISTS planner_bookings_update AFTER UPDATE ON bookings
            WHEN NEW.revision = OLD.revision
            BEGIN UPDATE planner_change_counter SET revision = revision + 1 WHERE id = 1; END;
            CREATE TRIGGER IF NOT EXISTS planner_bookings_delete AFTER DELETE ON bookings
            BEGIN UPDATE planner_change_counter SET revision = revision + 1 WHERE id = 1; END;
            CREATE TRIGGER IF NOT EXISTS planner_vehicles_insert AFTER INSERT ON vehicles
            BEGIN UPDATE planner_change_counter SET revision = revision + 1 WHERE id = 1; END;
            CREATE TRIGGER IF NOT EXISTS planner_vehicles_update AFTER UPDATE ON vehicles
            BEGIN UPDATE planner_change_counter SET revision = revision + 1 WHERE id = 1; END;
            CREATE TRIGGER IF NOT EXISTS planner_vehicles_delete AFTER DELETE ON vehicles
            BEGIN UPDATE planner_change_counter SET revision = revision + 1 WHERE id = 1; END;
            """
        )


def _migrate_vehicle_labels(path: Path) -> None:
    connection = sqlite3.connect(path, timeout=15)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'vehicles'"
        ).fetchone()
        if row and re.search(r"\blabel\s+TEXT\s+NOT\s+NULL\s+UNIQUE\b", row[0], re.IGNORECASE):
            vehicle_indexes = [
                index_row[0]
                for index_row in connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'vehicles' AND sql IS NOT NULL"
                )
            ]
            connection.execute(
                """CREATE TABLE vehicles_new (
                    id INTEGER PRIMARY KEY,
                    label TEXT NOT NULL COLLATE NOCASE,
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
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            connection.execute(
                """INSERT INTO vehicles_new
                   (id, label, logger, series, cluster, market, vr_stand, purpose, vin, jira_asset_key,
                    group_name, group_order, display_order, active, created_at, updated_at)
                   SELECT id, label, logger, series, cluster, market, vr_stand, purpose, vin, jira_asset_key,
                          group_name, group_order, display_order, active, created_at, updated_at
                   FROM vehicles"""
            )
            connection.execute("DROP TABLE vehicles")
            connection.execute("ALTER TABLE vehicles_new RENAME TO vehicles")
            for index_sql in vehicle_indexes:
                connection.execute(index_sql)
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(f"Vehicle schema migration found foreign-key violations: {violations!r}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def validate_username(username: str) -> str:
    normalized = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,63}", normalized):
        raise ValueError("Username must be 2–64 characters using letters, numbers, dots, underscores, or hyphens.")
    return normalized


def validate_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return password


def create_first_admin(username: str, password: str, db_path: str | Path | None = None) -> None:
    username = validate_username(username)
    validate_password(password)
    with connect(db_path) as db:
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise ValueError("An account already exists; create further users from the admin page.")
        db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
            (username, generate_password_hash(password)),
        )


def create_user(username: str, role: str, db_path: str | Path | None = None) -> str:
    username = validate_username(username)
    if role not in ROLES:
        raise ValueError("Unknown user role.")
    temporary_password = secrets.token_urlsafe(18)
    with connect(db_path) as db:
        db.execute(
            """INSERT INTO users (username, password_hash, role, must_change_password)
               VALUES (?, ?, ?, 0)""",
            (username, generate_password_hash(temporary_password), role),
        )
    return temporary_password


def get_user(username: str, db_path: str | Path | None = None) -> dict[str, Any] | None:
    with connect(db_path) as db:
        row = db.execute(
            """SELECT id, username, password_hash, role, active, must_change_password
               FROM users WHERE username = ? COLLATE NOCASE""",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def authenticate(username: str, password: str, db_path: str | Path | None = None) -> dict[str, Any] | None:
    user = get_user(username, db_path)
    if not user or not user["active"] or not check_password_hash(user["password_hash"], password):
        return None
    return user


def _login_attempt_key(username: str, remote_addr: str) -> str:
    value = f"{username.strip().casefold()}|{remote_addr}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def login_is_locked(username: str, remote_addr: str, db_path: str | Path | None = None) -> bool:
    key = _login_attempt_key(username, remote_addr)
    with connect(db_path) as db:
        row = db.execute("SELECT locked_until FROM login_attempts WHERE attempt_key = ?", (key,)).fetchone()
    return bool(row and row["locked_until"] and row["locked_until"] > datetime.now(timezone.utc).isoformat())


def record_login_failure(username: str, remote_addr: str, db_path: str | Path | None = None) -> None:
    key = _login_attempt_key(username, remote_addr)
    now = datetime.now(timezone.utc)
    with connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT failures, window_started_at FROM login_attempts WHERE attempt_key = ?", (key,)
        ).fetchone()
        if row and now - datetime.fromisoformat(row["window_started_at"]) < timedelta(minutes=15):
            failures = row["failures"] + 1
            window_started_at = row["window_started_at"]
        else:
            failures = 1
            window_started_at = now.isoformat()
        locked_until = (now + timedelta(minutes=15)).isoformat() if failures >= 5 else ""
        db.execute(
            """INSERT INTO login_attempts (attempt_key, failures, window_started_at, locked_until)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(attempt_key) DO UPDATE SET failures = excluded.failures,
                   window_started_at = excluded.window_started_at, locked_until = excluded.locked_until""",
            (key, failures, window_started_at, locked_until),
        )
        db.execute(
            "DELETE FROM login_attempts WHERE window_started_at < ?",
            ((now - timedelta(days=1)).isoformat(),),
        )


def clear_login_failures(username: str, remote_addr: str, db_path: str | Path | None = None) -> None:
    key = _login_attempt_key(username, remote_addr)
    with connect(db_path) as db:
        db.execute("DELETE FROM login_attempts WHERE attempt_key = ?", (key,))


def change_password(
    user_id: int,
    password: str,
    db_path: str | Path | None = None,
    *,
    must_change: bool = False,
) -> None:
    validate_password(password)
    with connect(db_path) as db:
        result = db.execute(
            "UPDATE users SET password_hash = ?, must_change_password = ? WHERE id = ? AND active = 1",
            (generate_password_hash(password), int(must_change), user_id),
        )
        if result.rowcount != 1:
            raise ValueError("Active user account not found.")


def list_users(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    with connect(db_path) as db:
        rows = db.execute(
            "SELECT id, username, role, active, must_change_password, created_at FROM users ORDER BY username"
        ).fetchall()
    return [dict(row) for row in rows]


def rename_user(user_id: int, username: str, db_path: str | Path | None = None) -> str:
    username = validate_username(username)
    with connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise ValueError("User account not found.")
        if db.execute(
            "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE AND id != ?", (username, user_id)
        ).fetchone():
            raise ValueError("That username is already in use.")
        db.execute("UPDATE users SET username = ? WHERE id = ?", (username, user_id))
    return user["username"]


def record_audit_event(
    actor: str,
    action: str,
    details: str = "",
    db_path: str | Path | None = None,
) -> None:
    if not actor.strip() or not action.strip():
        raise ValueError("Audit events require an actor and action.")
    with connect(db_path) as db:
        db.execute(
            "INSERT INTO audit_events (actor, action, details) VALUES (?, ?, ?)",
            (actor.strip()[:80], action.strip()[:120], details.strip()[:1000]),
        )


def list_audit_events(limit: int = 500, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    if not 1 <= limit <= 5000:
        raise ValueError("Audit event limit must be between 1 and 5000.")
    with connect(db_path) as db:
        rows = db.execute(
            """SELECT actor, action, details, created_at
               FROM audit_events ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_recent_booking_events(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return the latest recorded booking submissions, edits, and rejections."""
    with connect(db_path) as db:
        rows = db.execute(
            """SELECT actor, action, details, created_at FROM audit_events
               WHERE action LIKE 'Submitted booking #%'
                  OR action LIKE 'Edited booking #%'
                  OR action = 'Booking rejected'
               ORDER BY id DESC LIMIT 5"""
        ).fetchall()
    return [dict(row) for row in rows]


def get_help_contact(db_path: str | Path | None = None) -> str:
    with connect(db_path) as db:
        row = db.execute("SELECT value FROM app_settings WHERE key = 'help_contact'").fetchone()
    return row["value"] if row else ""


def set_help_contact(name: str, db_path: str | Path | None = None) -> None:
    name = name.strip()
    if not name or len(name) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError("Enter a name of 1–80 characters without control characters.")
    with connect(db_path) as db:
        db.execute(
            """INSERT INTO app_settings (key, value) VALUES ('help_contact', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (name,),
        )


def update_user_access(
    user_id: int,
    role: str | None,
    active: bool,
    db_path: str | Path | None = None,
) -> None:
    if role is not None and role not in ROLES:
        raise ValueError("Unknown user role.")
    with connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT role, active FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise ValueError("User account not found.")
        if role is None:
            role = user["role"]
        if user["role"] == "admin" and user["active"] and (role != "admin" or not active):
            enabled_admins = db.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
            ).fetchone()[0]
            if enabled_admins <= 1:
                raise ValueError("The last active admin cannot be disabled or demoted.")
        db.execute("UPDATE users SET role = ?, active = ? WHERE id = ?", (role, int(active), user_id))


def vehicle_name_exists(label: str, db_path: str | Path | None = None) -> bool:
    with connect(db_path) as db:
        return db.execute(
            "SELECT 1 FROM vehicles WHERE label = ? COLLATE NOCASE LIMIT 1",
            (label.strip(),),
        ).fetchone() is not None


def add_vehicle(data: dict[str, str], db_path: str | Path | None = None) -> int:
    label = data.get("label", "").strip()
    if not label:
        raise ValueError("Vehicle name is required.")
    group_name = data.get("group_name", "").strip() or "Other vehicles"
    fields = ("logger", "series", "cluster", "market", "vr_stand", "purpose", "vin", "jira_asset_key", "comment")
    values = [data.get(field, "").strip() for field in fields]
    if len(values[-1]) > MAX_VEHICLE_COMMENT_LENGTH:
        raise ValueError(f"Vehicle comment must be at most {MAX_VEHICLE_COMMENT_LENGTH} characters.")
    with connect(db_path) as db:
        group = db.execute(
            "SELECT MIN(group_order) AS group_order, MAX(display_order) AS display_order "
            "FROM vehicles WHERE group_name = ?",
            (group_name,),
        ).fetchone()
        if group["group_order"] is None:
            group_order = db.execute("SELECT COALESCE(MAX(group_order), -1) + 1 FROM vehicles").fetchone()[0]
            display_order = 0
        else:
            group_order = group["group_order"]
            display_order = group["display_order"] + 1
        result = db.execute(
            """INSERT INTO vehicles
               (label, logger, series, cluster, market, vr_stand, purpose, vin, jira_asset_key, comment,
                group_name, group_order, display_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (label, *values, group_name, group_order, display_order),
        )
        return int(result.lastrowid)


def update_vehicle(vehicle_id: int, data: dict[str, str], db_path: str | Path | None = None) -> None:
    """Update vehicle details; optional fields absent from ``data`` keep their stored value."""
    label = data.get("label", "").strip()
    if not label:
        raise ValueError("Vehicle name is required.")
    group_name = data.get("group_name", "").strip() or "Other vehicles"
    fields = [
        field
        for field in ("logger", "series", "cluster", "market", "vr_stand", "purpose", "vin", "jira_asset_key", "comment")
        if field in data
    ]
    values = [data[field].strip() for field in fields]
    if "comment" in data and len(data["comment"].strip()) > MAX_VEHICLE_COMMENT_LENGTH:
        raise ValueError(f"Vehicle comment must be at most {MAX_VEHICLE_COMMENT_LENGTH} characters.")
    with connect(db_path) as db:
        current = db.execute(
            "SELECT group_name, group_order, display_order FROM vehicles WHERE id = ?",
            (vehicle_id,),
        ).fetchone()
        if current is None:
            raise ValueError("Vehicle not found.")
        if current["group_name"] == group_name:
            group_order = current["group_order"]
            display_order = current["display_order"]
        else:
            group = db.execute(
                """SELECT MIN(group_order) AS group_order, MAX(display_order) AS display_order
                   FROM vehicles WHERE group_name = ?""",
                (group_name,),
            ).fetchone()
            if group["group_order"] is None:
                group_order = db.execute("SELECT COALESCE(MAX(group_order), -1) + 1 FROM vehicles").fetchone()[0]
                display_order = 0
            else:
                group_order = group["group_order"]
                display_order = group["display_order"] + 1
        result = db.execute(
            f"""UPDATE vehicles
               SET label = ?, {"".join(f"{field} = ?, " for field in fields)}group_name = ?, group_order = ?,
                   display_order = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (label, *values, group_name, group_order, display_order, vehicle_id),
        )
        if result.rowcount != 1:
            raise ValueError("Vehicle not found.")


def count_vehicle_bookings(vehicle_id: int, db_path: str | Path | None = None) -> int:
    with connect(db_path) as db:
        return int(db.execute("SELECT COUNT(*) FROM bookings WHERE vehicle_id = ?", (vehicle_id,)).fetchone()[0])


def delete_vehicle(vehicle_id: int, db_path: str | Path | None = None) -> None:
    """Delete a vehicle that has no bookings; vehicles with history must be deactivated instead."""
    with connect(db_path) as db:
        if db.execute("SELECT 1 FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone() is None:
            raise ValueError("Vehicle not found.")
        result = db.execute(
            "DELETE FROM vehicles WHERE id = ? AND NOT EXISTS (SELECT 1 FROM bookings WHERE vehicle_id = ?)",
            (vehicle_id, vehicle_id),
        )
        if result.rowcount != 1:
            raise ValueError("This vehicle has bookings, so it cannot be deleted. Move it to inactive instead.")


def set_vehicle_active(vehicle_id: int, active: bool, db_path: str | Path | None = None) -> bool:
    """Set a vehicle's active state; returns False if it was already in that state."""
    with connect(db_path) as db:
        if db.execute("SELECT 1 FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone() is None:
            raise ValueError("Vehicle not found.")
        result = db.execute(
            "UPDATE vehicles SET active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND active != ?",
            (int(active), vehicle_id, int(active)),
        )
        return result.rowcount == 1


def update_vehicle_comment(vehicle_id: int, comment: str, db_path: str | Path | None = None) -> None:
    comment = comment.strip()
    if len(comment) > MAX_VEHICLE_COMMENT_LENGTH:
        raise ValueError(f"Vehicle comment must be at most {MAX_VEHICLE_COMMENT_LENGTH} characters.")
    with connect(db_path) as db:
        result = db.execute(
            "UPDATE vehicles SET comment = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (comment, vehicle_id),
        )
        if result.rowcount != 1:
            raise ValueError("Vehicle not found.")


def vehicle_block_bounds(vehicle: dict[str, Any]) -> tuple[date, date] | None:
    """Return a current temporary block, ignoring blocks whose end date has passed."""
    raw_start = vehicle.get("blocked_from", "")
    if not raw_start:
        return None
    start = date.fromisoformat(raw_start)
    end = date.fromisoformat(vehicle["blocked_until"]) if vehicle.get("blocked_until") else date.max
    return (start, end) if end >= date.today() else None


def set_vehicle_blocked(
    vehicle_id: int, blocked: bool, reason: str = "", until: str = "",
    db_path: str | Path | None = None,
) -> None:
    """Set today's temporary block or clear it without changing bookings or active status."""
    reason = reason.strip()
    until = until.strip()
    if blocked:
        if len(reason) > MAX_VEHICLE_COMMENT_LENGTH:
            raise ValueError(f"Block reason must be at most {MAX_VEHICLE_COMMENT_LENGTH} characters.")
        if until:
            try:
                end = date.fromisoformat(until)
            except ValueError as exc:
                raise ValueError("Choose a valid block end date.") from exc
            if end < date.today():
                raise ValueError("Block end date cannot be before today.")
            until = end.isoformat()
    with connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        vehicle = db.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
        if vehicle is None:
            raise ValueError("Vehicle not found.")
        current = vehicle_block_bounds(dict(vehicle))
        start = (current[0] if current else date.today()).isoformat() if blocked else ""
        db.execute(
            """UPDATE vehicles SET blocked_from = ?, blocked_until = ?, blocked_reason = ?,
               updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (start, until if blocked else "", reason if blocked else "", vehicle_id),
        )


def get_vehicle(vehicle_id: int, db_path: str | Path | None = None) -> dict[str, Any] | None:
    with connect(db_path) as db:
        row = db.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
    return dict(row) if row else None


def list_vehicles(db_path: str | Path | None = None, *, active_only: bool = True) -> list[dict[str, Any]]:
    query = "SELECT * FROM vehicles"
    if active_only:
        query += " WHERE active = 1"
    query += """ ORDER BY
        CASE WHEN group_name = '' THEN 1 ELSE 0 END,
        group_order, display_order, label COLLATE NOCASE"""
    with connect(db_path) as db:
        return [dict(row) for row in db.execute(query).fetchall()]


def update_vehicle_groups(
    workbook_vehicles: list[dict[str, str]],
    db_path: str | Path | None = None,
) -> int:
    """Apply workbook sections and row order to existing vehicles without importing bookings."""
    with connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        known_labels = {
            row["label"].strip().casefold()
            for row in db.execute("SELECT label FROM vehicles").fetchall()
        }
        missing = [
            vehicle["label"]
            for vehicle in workbook_vehicles
            if vehicle["label"].strip().casefold() not in known_labels
        ]
        if missing:
            raise ValueError(
                "Some workbook vehicles are not in the planner database: " + ", ".join(missing[:10])
            )
        for index, vehicle in enumerate(workbook_vehicles):
            db.execute(
                """UPDATE vehicles
                   SET group_name = ?, group_order = ?, display_order = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = (
                       SELECT MIN(id) FROM vehicles WHERE label = ? COLLATE NOCASE
                   )""",
                (
                    vehicle.get("group_name", "").strip(),
                    int(vehicle.get("group_order", 0)),
                    int(vehicle.get("display_order", index)),
                    vehicle["label"].strip(),
                ),
            )
    return len(workbook_vehicles)


def booking_date_bounds(booking: dict[str, Any]) -> tuple[date, date] | None:
    """Resolve explicit dates or a week-only booking's Monday through Friday."""
    planned_date = booking["planned_date"].strip()
    if planned_date:
        start = date.fromisoformat(planned_date)
    else:
        match = re.fullmatch(r"KW(\d{1,2})(?:-(\d{4}))?", booking["period"].strip().upper())
        if not match:
            return None
        year = int(match.group(2)) if match.group(2) else date.today().isocalendar().year
        try:
            start = date.fromisocalendar(year, int(match.group(1)), 1)
        except ValueError:
            return None
    raw_end = booking.get("planned_end_date", "").strip()
    end = date.fromisoformat(raw_end) if raw_end else start + timedelta(days=4 if not planned_date else 0)
    if end < start:
        raise ValueError("A booking end date cannot be before its start date.")
    return start, end


def _check_booking_availability(
    db: sqlite3.Connection, vehicle_id: int, fields: dict[str, Any], exclude_id: int | None = None,
) -> None:
    requested = booking_date_bounds(fields)
    if requested is None:
        raise ValueError("Choose a valid planned week or date.")
    vehicle = dict(db.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone())
    blocked = vehicle_block_bounds(vehicle)
    if blocked and requested[0] <= blocked[1] and blocked[0] <= requested[1]:
        until = f" through {blocked[1].isoformat()}" if blocked[1] != date.max else " until unblocked"
        reason = f" Reason: {vehicle['blocked_reason']}" if vehicle["blocked_reason"] else ""
        raise BookingConflict(
            f"Vehicle is BLOCKED — not ready for testing from {blocked[0].isoformat()}{until}.{reason}"
        )
    rows = db.execute(
        """SELECT id, activity, period, planned_date, planned_end_date, created_by
           FROM bookings WHERE vehicle_id = ? AND status != 'rejected' AND id != ?
           ORDER BY id""",
        (vehicle_id, exclude_id if exclude_id is not None else -1),
    )
    for row in rows:
        existing = booking_date_bounds(dict(row))
        if existing and requested[0] <= existing[1] and existing[0] <= requested[1]:
            raise BookingConflict(
                f"Vehicle already booked for {existing[0].isoformat()} to {existing[1].isoformat()}: "
                f"{row['activity']} (booked by {row['created_by']})."
            )


def create_booking(
    vehicle_id: int,
    activity: str,
    period: str,
    planned_date: str,
    description: str,
    checklist_template: str,
    created_by: str,
    db_path: str | Path | None = None,
    planned_end_date: str = "",
    activity_color: str = "",
) -> int:
    if checklist_template and not re.fullmatch(r"[a-zA-Z0-9_-]+", checklist_template):
        raise ValueError("Invalid checklist template.")
    fields = _validated_booking_fields(
        activity, period, planned_date, planned_end_date, description, activity_color
    )
    with connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        vehicle = db.execute("SELECT id FROM vehicles WHERE id = ? AND active = 1", (vehicle_id,)).fetchone()
        if not vehicle:
            raise ValueError("Select an active vehicle.")
        _check_booking_availability(db, vehicle_id, fields)
        result = db.execute(
            """INSERT INTO bookings
               (vehicle_id, activity, period, planned_date, planned_end_date, description,
                checklist_template, created_by, activity_color)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vehicle_id,
                fields["activity"],
                fields["period"],
                fields["planned_date"],
                fields["planned_end_date"],
                fields["description"],
                checklist_template,
                created_by,
                fields["activity_color"],
            ),
        )
        return int(result.lastrowid)


def update_booking(
    booking_id: int,
    vehicle_id: int,
    activity: str,
    period: str,
    planned_date: str,
    description: str,
    db_path: str | Path | None = None,
    *,
    planned_end_date: str = "",
    activity_color: str = "",
    expected_revision: int | None = None,
    edit_token: str | None = None,
) -> list[str]:
    """Update a booking still under review; returns names of changed fields."""
    fields = {
        "vehicle_id": vehicle_id,
        **_validated_booking_fields(
            activity, period, planned_date, planned_end_date, description, activity_color
        ),
    }
    with connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT * FROM bookings WHERE id = ? AND status IN ('pending', 'approved') AND jira_issue_key = ''",
            (booking_id,),
        ).fetchone()
        if current is None:
            raise ValueError("Only pending or approved bookings without a Jira ticket can be edited.")
        if expected_revision is not None and current["revision"] != expected_revision:
            raise BookingChanged("This booking was changed by someone else. Reload it before saving.")
        if edit_token is not None:
            lease = db.execute(
                "SELECT token FROM booking_edit_leases WHERE booking_id = ? AND expires_at > unixepoch('now')",
                (booking_id,),
            ).fetchone()
            if lease is None or lease["token"] != edit_token:
                raise BookingChanged("Your edit session expired or another person is editing. Reload before saving.")
        if vehicle_id != current["vehicle_id"] and not db.execute(
            "SELECT 1 FROM vehicles WHERE id = ? AND active = 1", (vehicle_id,)
        ).fetchone():
            raise ValueError("Select an active vehicle.")
        changed = [name for name, value in fields.items() if current[name] != value]
        if any(name in changed for name in ("vehicle_id", "period", "planned_date", "planned_end_date")):
            _check_booking_availability(db, vehicle_id, fields, exclude_id=booking_id)
        if not changed:
            return []
        result = db.execute(
            f"""UPDATE bookings SET {", ".join(f"{name} = ?" for name in changed)},
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status IN ('pending', 'approved') AND jira_issue_key = ''""",
            (*(fields[name] for name in changed), booking_id),
        )
        if result.rowcount != 1:
            raise ValueError("Only pending or approved bookings without a Jira ticket can be edited.")
        return changed


def acquire_booking_edit(booking_id: int, token: str, actor: str, db_path: str | Path | None = None) -> str | None:
        """Acquire or renew a short edit lease; return the other editor's name if occupied."""
        with connect(db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            booking = db.execute(
                "SELECT id FROM bookings WHERE id = ? AND status IN ('pending', 'approved') AND jira_issue_key = ''",
                (booking_id,),
            ).fetchone()
            if booking is None:
                raise ValueError("This booking is no longer available for editing.")
            lease = db.execute(
                """SELECT token, actor FROM booking_edit_leases
                   WHERE booking_id = ? AND expires_at > CAST(strftime('%s', 'now') AS INTEGER)""",
                (booking_id,),
            ).fetchone()
            if lease is not None and lease["token"] != token:
                return lease["actor"]
            db.execute(
                """INSERT INTO booking_edit_leases (booking_id, token, actor, expires_at)
                   VALUES (?, ?, ?, CAST(strftime('%s', 'now') AS INTEGER) + 120)
                   ON CONFLICT(booking_id) DO UPDATE SET token = excluded.token, actor = excluded.actor,
                       expires_at = excluded.expires_at""",
                (booking_id, token, actor),
            )
        return None


def release_booking_edit(booking_id: int, token: str, db_path: str | Path | None = None) -> None:
        with connect(db_path) as db:
            db.execute("DELETE FROM booking_edit_leases WHERE booking_id = ? AND token = ?", (booking_id, token))


def release_user_edits(token: str, db_path: str | Path | None = None) -> None:
        with connect(db_path) as db:
            db.execute("DELETE FROM booking_edit_leases WHERE token = ?", (token,))


def planner_revision(db_path: str | Path | None = None) -> int:
        with connect(db_path) as db:
            return int(db.execute("SELECT revision FROM planner_change_counter WHERE id = 1").fetchone()[0])


def _validated_booking_fields(
    activity: str,
    period: str,
    planned_date: str,
    planned_end_date: str,
    description: str,
    activity_color: str,
) -> dict[str, str]:
    activity = activity.strip()
    period = period.strip()
    if not activity or not period:
        raise ValueError("Activity and planned week are required.")
    planned_date = planned_date.strip()
    if planned_date:
        try:
            date.fromisoformat(planned_date)
        except ValueError as exc:
            raise ValueError("Choose a valid start date.") from exc
    planned_end_date = planned_end_date.strip()
    if planned_end_date:
        if not planned_date:
            raise ValueError("Choose a start date before setting an end date.")
        try:
            end_date = date.fromisoformat(planned_end_date)
        except ValueError as exc:
            raise ValueError("Choose a valid end date.") from exc
        if end_date < date.fromisoformat(planned_date):
            raise ValueError("The end date must be on or after the start date.")
    activity_color = activity_color.strip().upper()
    if activity_color and not re.fullmatch(r"#[0-9A-F]{6}", activity_color):
        raise ValueError("Choose a valid activity color.")
    return {
        "activity": activity,
        "period": period,
        "planned_date": planned_date,
        "planned_end_date": planned_end_date,
        "description": description.strip(),
        "activity_color": activity_color,
    }


def list_imported_activity_colors(db_path: str | Path | None = None) -> list[str]:
    with connect(db_path) as db:
        rows = db.execute(
            "SELECT DISTINCT activity_color FROM bookings WHERE created_by = 'excel-import' AND activity_color != ''"
        ).fetchall()
    return sorted({
        color for row in rows
        if re.fullmatch(r"#[0-9A-F]{6}", color := row["activity_color"].strip().upper())
    })


def list_bookings(
    db_path: str | Path | None = None,
    *,
    status: str | None = None,
    statuses: tuple[str, ...] | None = None,
    created_by: str | None = None,
    vehicle_id: int | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[Any] = []
    if status:
        if status not in BOOKING_STATUSES:
            raise ValueError("Unknown booking status.")
        clauses.append("b.status = ?")
        params.append(status)
    if statuses:
        if any(value not in BOOKING_STATUSES for value in statuses):
            raise ValueError("Unknown booking status.")
        clauses.append(f"b.status IN ({','.join('?' for _ in statuses)})")
        params.extend(statuses)
    if created_by:
        clauses.append("b.created_by = ?")
        params.append(created_by)
    if vehicle_id is not None:
        clauses.append("b.vehicle_id = ?")
        params.append(vehicle_id)
    query = """SELECT b.*, v.label AS vehicle_label, v.vin AS vehicle_vin,
                      v.jira_asset_key AS jira_asset_key, v.blocked_from, v.blocked_until, v.blocked_reason
               FROM bookings b JOIN vehicles v ON v.id = b.vehicle_id"""
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY b.created_at DESC, b.id DESC LIMIT ?"
    params.append(limit)
    with connect(db_path) as db:
        return [dict(row) for row in db.execute(query, params).fetchall()]


def get_booking(booking_id: int, db_path: str | Path | None = None) -> dict[str, Any] | None:
    with connect(db_path) as db:
        row = db.execute(
            """SELECT b.*, v.label AS vehicle_label, v.logger, v.series, v.cluster,
                      v.market, v.vr_stand, v.purpose, v.vin AS vehicle_vin,
                      v.jira_asset_key, v.blocked_from, v.blocked_until, v.blocked_reason
               FROM bookings b JOIN vehicles v ON v.id = b.vehicle_id
               WHERE b.id = ?""",
            (booking_id,),
        ).fetchone()
    return dict(row) if row else None


def review_booking(
    booking_id: int,
    status: str,
    reviewer: str,
    db_path: str | Path | None = None,
    *,
    expected_revision: int | None = None,
) -> None:
    if status not in ("approved", "rejected", "pending"):
        raise ValueError("Review status must be approved, rejected, or returned to pending.")
    expected_status = "approved" if status == "pending" else "pending"
    with connect(db_path) as db:
        result = db.execute(
            """UPDATE bookings
               SET status = ?, reviewed_by = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND (status = ? OR (? = 'rejected' AND status = 'approved'))
                   AND jira_issue_key = ''
                   AND (? IS NULL OR (revision = ? AND status = 'pending'))""",
            (status, reviewer, booking_id, expected_status, status, expected_revision, expected_revision),
        )
        if result.rowcount != 1:
            raise ValueError("Booking not found, already processed, or no longer available for review.")


def update_booking_progress(booking_id: int, progress: str, db_path: str | Path | None = None) -> str:
    """Set the work progress of an approved booking; returns the previous progress."""
    if progress not in BOOKING_PROGRESS:
        raise ValueError("Choose Not started, In progress, or Closed.")
    with connect(db_path) as db:
        current = db.execute("SELECT status, progress FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if current is None:
            raise ValueError("Booking not found.")
        if current["status"] not in PROGRESS_EDITABLE_STATUSES:
            raise ValueError("Progress can only be changed after the booking is approved.")
        db.execute(
            "UPDATE bookings SET progress = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status IN (?, ?)",
            (progress, booking_id, *PROGRESS_EDITABLE_STATUSES),
        )
        return current["progress"]


def claim_booking_creation(
    booking_id: int, db_path: str | Path | None = None, *, checklist_template: str | None = None
) -> bool:
    if checklist_template is not None and not re.fullmatch(r"[a-zA-Z0-9_-]+", checklist_template):
        raise ValueError("Invalid checklist template.")
    with connect(db_path) as db:
        result = db.execute(
            """UPDATE bookings SET status = 'creating', error_code = '',
                   checklist_template = COALESCE(?, checklist_template), updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'approved' AND jira_issue_key = ''""",
            (checklist_template, booking_id),
        )
        return result.rowcount == 1


def claim_pending_booking_creation(
    booking_id: int, reviewer: str, expected_revision: int, checklist_template: str,
    db_path: str | Path | None = None,
) -> bool:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", checklist_template):
        raise ValueError("Invalid checklist template.")
    with connect(db_path) as db:
        result = db.execute(
            """UPDATE bookings SET status = 'creating', reviewed_by = ?, error_code = '',
                   checklist_template = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'pending' AND revision = ? AND jira_issue_key = ''""",
            (reviewer, checklist_template, booking_id, expected_revision),
        )
        return result.rowcount == 1


def complete_booking_creation(
    booking_id: int,
    issue_key: str,
    db_path: str | Path | None = None,
) -> None:
    if not issue_key.strip():
        raise ValueError("Jira did not return an issue key.")
    with connect(db_path) as db:
        result = db.execute(
            """UPDATE bookings
               SET status = 'created', jira_issue_key = ?, error_code = '',
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'creating' AND jira_issue_key = ''""",
            (issue_key.strip(), booking_id),
        )
        if result.rowcount != 1:
            raise ValueError("Booking is no longer in the creating state.")


def fail_booking_creation(booking_id: int, error_code: str, db_path: str | Path | None = None) -> None:
    with connect(db_path) as db:
        db.execute(
            """UPDATE bookings SET status = 'creation_error', error_code = ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'creating'""",
            (error_code[:80], booking_id),
        )


def reapprove_failed_booking(booking_id: int, db_path: str | Path | None = None) -> None:
    with connect(db_path) as db:
        result = db.execute(
            """UPDATE bookings SET status = 'approved', error_code = '',
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'creation_error' AND jira_issue_key = ''""",
            (booking_id,),
        )
        if result.rowcount != 1:
            raise ValueError("Booking is not in a retryable state.")


def import_workbook_data(
    source_hash: str,
    source_name: str,
    vehicles: list[dict[str, str]],
    bookings: list[dict[str, Any]],
    db_path: str | Path | None = None,
) -> tuple[int, int]:
    """Merge a workbook once, preserving existing vehicle values and bookings."""
    with connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM planner_imports LIMIT 1").fetchone():
            raise ValueError("A workbook has already been imported into this planner database.")
        vehicle_ids: dict[str, int] = {}
        for vehicle in vehicles:
            label = vehicle["label"].strip()
            fields = ("logger", "series", "cluster", "market", "vr_stand", "purpose", "vin", "jira_asset_key")
            existing = db.execute(
                "SELECT id FROM vehicles WHERE label = ? COLLATE NOCASE ORDER BY id LIMIT 1", (label,)
            ).fetchone()
            values = tuple(vehicle.get(field, "").strip() for field in fields)
            if existing:
                vehicle_id = int(existing["id"])
                db.execute(
                    """UPDATE vehicles
                       SET logger = CASE WHEN logger = '' THEN ? ELSE logger END,
                           series = CASE WHEN series = '' THEN ? ELSE series END,
                           cluster = CASE WHEN cluster = '' THEN ? ELSE cluster END,
                           market = CASE WHEN market = '' THEN ? ELSE market END,
                           vr_stand = CASE WHEN vr_stand = '' THEN ? ELSE vr_stand END,
                           purpose = CASE WHEN purpose = '' THEN ? ELSE purpose END,
                           vin = CASE WHEN vin = '' THEN ? ELSE vin END,
                           jira_asset_key = CASE WHEN jira_asset_key = '' THEN ? ELSE jira_asset_key END,
                           group_name = ?, group_order = ?, display_order = ?,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (
                        *values,
                        vehicle.get("group_name", "").strip(),
                        int(vehicle.get("group_order", 0)),
                        int(vehicle.get("display_order", 0)),
                        vehicle_id,
                    ),
                )
            else:
                result = db.execute(
                    """INSERT INTO vehicles
                       (label, logger, series, cluster, market, vr_stand, purpose, vin, jira_asset_key,
                        group_name, group_order, display_order)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        label,
                        *values,
                        vehicle.get("group_name", "").strip(),
                        int(vehicle.get("group_order", 0)),
                        int(vehicle.get("display_order", 0)),
                    ),
                )
                vehicle_id = int(result.lastrowid)
            vehicle_ids[label.casefold()] = vehicle_id
        from datetime import date

        for booking in bookings:
            key = booking["vehicle_label"].strip().casefold()
            vehicle_id = vehicle_ids.get(key)
            if vehicle_id is None:
                raise ValueError(f"Workbook activity refers to unknown vehicle {booking['vehicle_label']!r}.")
            status = "historical" if booking.get("planned_date", "") < date.today().isoformat() else "pending"
            db.execute(
                """INSERT INTO bookings
                   (vehicle_id, activity, period, planned_date, planned_end_date, description, activity_color,
                    checklist_template, status, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'excel-import')""",
                (
                    vehicle_id,
                    booking["activity"].strip(),
                    booking["period"].strip(),
                    booking.get("planned_date", "").strip(),
                    booking.get("planned_end_date", booking.get("planned_date", "")).strip(),
                    booking.get("description", "").strip(),
                    booking.get("activity_color", "").strip(),
                    booking.get("checklist_template", "analysis_checklist").strip(),
                    status,
                ),
            )
        db.execute(
            """INSERT INTO planner_imports (source_hash, source_name, vehicle_count, booking_count)
               VALUES (?, ?, ?, ?)""",
            (source_hash, source_name, len(vehicles), len(bookings)),
        )
    return len(vehicles), len(bookings)


def sync_workbook_updates(
    vehicles: list[dict[str, str]],
    bookings: list[dict[str, Any]],
    db_path: str | Path | None = None,
    *,
    apply: bool = False,
) -> dict[str, int]:
    """Add workbook-only records while preserving populated planner fields and bookings."""
    counts = {"vehicles_added": 0, "vehicles_updated": 0, "bookings_added": 0}
    vehicle_fields = ("logger", "series", "cluster", "market", "vr_stand", "purpose", "vin")

    with connect(db_path) as db:
        if apply:
            db.execute("BEGIN IMMEDIATE")
        existing_vehicles: dict[str, sqlite3.Row] = {}
        for row in db.execute("SELECT * FROM vehicles ORDER BY id").fetchall():
            existing_vehicles.setdefault(row["label"].strip().casefold(), row)

        booking_keys = {
            (
                row["vehicle_label"].strip().casefold(),
                row["activity"].strip(),
                row["period"].strip(),
                row["planned_date"].strip(),
            )
            for row in db.execute(
                """SELECT v.label AS vehicle_label, b.activity, b.period, b.planned_date
                   FROM bookings b JOIN vehicles v ON v.id = b.vehicle_id"""
            ).fetchall()
        }

        for index, vehicle in enumerate(vehicles):
            label = vehicle["label"].strip()
            label_key = label.casefold()
            group_name = vehicle.get("group_name", "").strip()
            group_order = int(vehicle.get("group_order", 0))
            display_order = int(vehicle.get("display_order", index))
            current = existing_vehicles.get(label_key)
            if current is None:
                counts["vehicles_added"] += 1
                if apply:
                    result = db.execute(
                        """INSERT INTO vehicles
                           (label, logger, series, cluster, market, vr_stand, purpose, vin,
                            group_name, group_order, display_order)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            label,
                            *(vehicle.get(field, "").strip() for field in vehicle_fields),
                            group_name,
                            group_order,
                            display_order,
                        ),
                    )
                    current = db.execute(
                        "SELECT * FROM vehicles WHERE id = ?", (result.lastrowid,)
                    ).fetchone()
                    existing_vehicles[label_key] = current
            else:
                values_to_fill = {
                    field: vehicle.get(field, "").strip()
                    for field in vehicle_fields
                    if not current[field] and vehicle.get(field, "").strip()
                }
                group_changed = (
                    current["group_name"] != group_name
                    or current["group_order"] != group_order
                    or current["display_order"] != display_order
                )
                if values_to_fill or group_changed:
                    counts["vehicles_updated"] += 1
                    if apply:
                        assignments = [
                            f"{field} = ?" for field in values_to_fill
                        ] + ["group_name = ?", "group_order = ?", "display_order = ?",
                             "updated_at = CURRENT_TIMESTAMP"]
                        db.execute(
                            f"UPDATE vehicles SET {', '.join(assignments)} WHERE id = ?",
                            (
                                *values_to_fill.values(),
                                group_name,
                                group_order,
                                display_order,
                                current["id"],
                            ),
                        )

        for booking in bookings:
            key = (
                booking["vehicle_label"].strip().casefold(),
                booking["activity"].strip(),
                booking["period"].strip(),
                booking.get("planned_date", "").strip(),
            )
            if key in booking_keys:
                continue
            counts["bookings_added"] += 1
            booking_keys.add(key)
            if apply:
                vehicle_id = existing_vehicles[key[0]]["id"]
                status = (
                    "historical"
                    if key[3] and key[3] < date.today().isoformat()
                    else "pending"
                )
                db.execute(
                    """INSERT INTO bookings
                       (vehicle_id, activity, period, planned_date, planned_end_date,
                        description, activity_color, checklist_template, status, created_by)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'excel-import')""",
                    (
                        vehicle_id,
                        booking["activity"].strip(),
                        booking["period"].strip(),
                        key[3],
                        booking.get("planned_end_date", key[3]).strip(),
                        booking.get("description", "").strip(),
                        booking.get("activity_color", "").strip(),
                        booking.get("checklist_template", "analysis_checklist").strip(),
                        status,
                    ),
                )

    return counts


def update_imported_activity_colors(
    bookings: list[dict[str, Any]],
    db_path: str | Path | None = None,
) -> int:
    """Apply workbook fill colors to matching calendar-imported activities."""
    updated = 0
    with connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        vehicle_ids: dict[str, int] = {}
        for row in db.execute("SELECT id, label FROM vehicles ORDER BY id").fetchall():
            vehicle_ids.setdefault(row["label"].strip().casefold(), int(row["id"]))
        for booking in bookings:
            color = booking.get("activity_color", "").strip()
            if not color:
                continue
            vehicle_id = vehicle_ids.get(booking["vehicle_label"].strip().casefold())
            if vehicle_id is None:
                continue
            result = db.execute(
                """UPDATE bookings SET activity_color = ?
                   WHERE vehicle_id = ? AND created_by = 'excel-import'
                     AND planned_date = ? AND activity = ? COLLATE NOCASE
                     AND activity_color != ?""",
                (color, vehicle_id, booking.get("planned_date", "").strip(), booking["activity"].strip(), color),
            )
            updated += result.rowcount
    return updated


def update_imported_activity_spans(
    bookings: list[dict[str, Any]],
    db_path: str | Path | None = None,
) -> int:
    """Apply merged Excel date ranges to matching calendar-imported activities."""
    updated = 0
    with connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        vehicle_ids: dict[str, int] = {}
        for row in db.execute("SELECT id, label FROM vehicles ORDER BY id").fetchall():
            vehicle_ids.setdefault(row["label"].strip().casefold(), int(row["id"]))
        for booking in bookings:
            start_date = booking.get("planned_date", "").strip()
            end_date = booking.get("planned_end_date", start_date).strip()
            if not start_date or not end_date or end_date <= start_date:
                continue
            vehicle_id = vehicle_ids.get(booking["vehicle_label"].strip().casefold())
            if vehicle_id is None:
                continue
            result = db.execute(
                """UPDATE bookings SET planned_end_date = ?
                   WHERE vehicle_id = ? AND created_by = 'excel-import'
                     AND planned_date = ? AND activity = ? COLLATE NOCASE
                     AND planned_end_date != ?""",
                (end_date, vehicle_id, start_date, booking["activity"].strip(), end_date),
            )
            updated += result.rowcount
    return updated


def import_vehicle_tab_history(
    source_hash: str,
    source_name: str,
    bookings: list[dict[str, Any]],
    db_path: str | Path | None = None,
) -> int:
    """Append the workbook's vehicle-sheet history once, without changing vehicles."""
    import_key = f"vehicle-tabs:{source_hash}"
    with connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "SELECT 1 FROM planner_imports WHERE source_hash = ?", (source_hash,)
        ).fetchone():
            raise ValueError("Import the workbook calendar into this database before importing vehicle-tab history.")
        if db.execute(
            "SELECT 1 FROM planner_imports WHERE source_hash = ?", (import_key,)
        ).fetchone():
            raise ValueError("Vehicle-tab history from this workbook has already been imported.")

        vehicle_ids: dict[str, int] = {}
        for row in db.execute("SELECT id, label FROM vehicles ORDER BY id").fetchall():
            vehicle_ids.setdefault(row["label"].strip().casefold(), int(row["id"]))
        for booking in bookings:
            label = booking["vehicle_label"].strip()
            vehicle_id = vehicle_ids.get(label.casefold())
            if vehicle_id is None:
                raise ValueError(f"Vehicle history refers to unknown vehicle {label!r}.")
            db.execute(
                """INSERT INTO bookings
                   (vehicle_id, activity, period, planned_date, planned_end_date, description,
                    checklist_template, status, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'historical', 'excel-detail-import')""",
                (
                    vehicle_id,
                    booking["activity"].strip(),
                    booking["period"].strip(),
                    booking["planned_date"].strip(),
                    booking["planned_end_date"].strip() if booking.get("planned_end_date") else booking["planned_date"].strip(),
                    booking.get("description", "").strip(),
                    booking.get("checklist_template", "analysis_checklist").strip(),
                ),
            )
        db.execute(
            """INSERT INTO planner_imports (source_hash, source_name, vehicle_count, booking_count)
               VALUES (?, ?, 0, ?)""",
            (import_key, source_name, len(bookings)),
        )
    return len(bookings)
