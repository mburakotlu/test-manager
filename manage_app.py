"""Initialize planner accounts and application secrets."""

from __future__ import annotations

import argparse
import getpass
import secrets
from pathlib import Path

from dotenv import load_dotenv
from planner_store import (
    change_password,
    create_first_admin,
    get_user,
    initialize_database,
)

load_dotenv(Path(__file__).parent / ".env")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("bootstrap-admin", help="Create the first administrator account.")
    subparsers.add_parser("generate-secret", help="Print a random APP_SECRET_KEY for the local .env file.")
    reset_parser = subparsers.add_parser("reset-password", help="Reset an existing app user's password.")
    reset_parser.add_argument("username", help="Username of the account to reset.")
    args = parser.parse_args()

    if args.command == "generate-secret":
        print(secrets.token_urlsafe(48))
        return

    initialize_database()
    if args.command == "reset-password":
        user = get_user(args.username)
        if user is None:
            raise SystemExit(f"User {args.username!r} was not found in the configured planner database.")
        if not user["active"]:
            raise SystemExit(f"User {user['username']!r} is inactive; enable the account before resetting its password.")
        password = getpass.getpass("New password (minimum 12 characters): ")
        confirmation = getpass.getpass("Confirm new password: ")
        if password != confirmation:
            raise SystemExit("Passwords do not match.")
        try:
            change_password(user["id"], password, must_change=False)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Password reset for {user['username']!r}. The password was not displayed.")
        return

    username = input("Admin username: ").strip()
    password = getpass.getpass("Admin password (minimum 12 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    try:
        create_first_admin(username, password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Administrator {username!r} created. Keep the password private.")


if __name__ == "__main__":
    main()
