"""Read and update the vehicle YAML attachments on the Jira-Manager reference ticket.

Keeps vehicles/*.yaml in sync with the attachments on a Jira ticket (default:
PHUB-2200, the "Jira-Manager" Xray Test Repository reference ticket used as a
shared read/write location for these files). Jira has no "update attachment in
place" endpoint, so refreshing a file means deleting the old attachment and
uploading the new one.

Usage:
    python sync_vehicle_attachments.py read [--ticket PHUB-2200]
    python sync_vehicle_attachments.py update [--ticket PHUB-2200]
        [--vehicles-dir vehicles] [--confirm]

"read" only lists what's currently attached (never modifies anything).
"update" is dry-run by default: shows which local files would be uploaded,
refreshed (delete + re-upload), or removed (orphaned remote attachments with
no matching local file) — pass --confirm to actually apply it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from jira_agent.config import load_config
from jira_agent.jira_client import JiraClient

DEFAULT_TICKET = "PHUB-2200"
VEHICLES_DIR = Path(__file__).parent / "vehicles"


def cmd_read(client: JiraClient, ticket: str) -> None:
    """List the current attachments on ``ticket``."""
    attachments = client.get_attachments(ticket)
    if not attachments:
        print(f"No attachments found on {ticket}.")
        return
    print(f"Attachments on {ticket} ({len(attachments)}):")
    for att in attachments:
        print(f"  - {att['filename']:30s} {att['size']:>8} bytes  created {att['created']}  id={att['id']}")


def cmd_update(client: JiraClient, ticket: str, vehicles_dir: Path, confirm: bool) -> None:
    """Sync vehicles_dir/*.yaml onto ticket's attachments (delete + re-upload to refresh)."""
    local_files = sorted(vehicles_dir.glob("*.yaml"))
    local_by_name = {p.name: p for p in local_files}

    remote_attachments = client.get_attachments(ticket)
    remote_yaml = [a for a in remote_attachments if a["filename"].endswith(".yaml")]
    remote_by_name = {a["filename"]: a for a in remote_yaml}

    to_refresh = [name for name in local_by_name if name in remote_by_name]
    to_upload = [name for name in local_by_name if name not in remote_by_name]
    to_remove = [name for name in remote_by_name if name not in local_by_name]

    print(f"Ticket: {ticket}  Local vehicles dir: {vehicles_dir}")
    print(f"  Refresh (delete + re-upload): {len(to_refresh)}")
    print(f"  New upload: {len(to_upload)}")
    print(f"  Orphaned remote attachment to remove: {len(to_remove)}")
    for name in to_refresh:
        print(f"    refresh  {name}")
    for name in to_upload:
        print(f"    upload   {name}")
    for name in to_remove:
        print(f"    remove   {name} (id={remote_by_name[name]['id']})")

    if not (to_refresh or to_upload or to_remove):
        print("Nothing to do — already in sync.")
        return
    if not confirm:
        print("DRY-RUN only — re-run with --confirm to apply these changes.")
        return

    for name in to_refresh + to_remove:
        att = remote_by_name[name]
        client.delete_attachment(att["id"], confirm=True)
        print(f"Deleted old attachment: {name} (id={att['id']})")

    for name in to_refresh + to_upload:
        result = client.add_attachment(ticket, str(local_by_name[name]), confirm=True)
        print(f"Uploaded: {name} -> {result['attachments'][0]['filename']}")


def main() -> None:
    """Parse CLI args and dispatch to the requested subcommand."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    read_parser = subparsers.add_parser("read", help="List current attachments on the reference ticket")
    read_parser.add_argument("--ticket", default=DEFAULT_TICKET, help=f"Jira ticket key (default: {DEFAULT_TICKET})")

    update_parser = subparsers.add_parser("update", help="Sync local vehicles/*.yaml onto the reference ticket")
    update_parser.add_argument("--ticket", default=DEFAULT_TICKET, help=f"Jira ticket key (default: {DEFAULT_TICKET})")
    update_parser.add_argument("--vehicles-dir", default=str(VEHICLES_DIR), help="Directory of local vehicle YAML files (default: vehicles/)")
    update_parser.add_argument("--confirm", action="store_true", help="Actually delete/upload attachments (default: dry-run only)")

    args = parser.parse_args()
    config = load_config()
    client = JiraClient(config)
    try:
        if args.command == "read":
            cmd_read(client, args.ticket)
        else:
            cmd_update(client, args.ticket, Path(args.vehicles_dir), args.confirm)
    finally:
        client.close()


if __name__ == "__main__":
    main()
