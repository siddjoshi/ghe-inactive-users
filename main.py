"""CLI tool to find inactive users in a GitHub Enterprise Cloud (EMU) via audit logs."""

import argparse
import csv
import logging
import os
import sys
from datetime import datetime, timezone

from gh_api import GitHubClient, GitHubAPIError
from inactive_users import identify_inactive_users


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find inactive users in a GitHub Enterprise Cloud (EMU) by analyzing audit logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python main.py --enterprise my-enterprise --token ghp_xxxx
  python main.py --enterprise my-enterprise --days 60 --output report.csv
  GH_TOKEN=ghp_xxxx python main.py --enterprise my-enterprise
        """,
    )
    parser.add_argument(
        "--enterprise",
        required=True,
        help="GitHub Enterprise slug (e.g., 'my-company')",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GH_TOKEN"),
        help="GitHub PAT with admin:enterprise scope (default: $GH_TOKEN env var)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Inactivity threshold in days (default: 90)",
    )
    parser.add_argument(
        "--output",
        help="Output CSV file path (default: inactive_users_YYYY-MM-DD.csv)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    return parser.parse_args()


def write_csv(users, output_path: str) -> None:
    """Write inactive users to a CSV file."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["login", "last_activity_date", "days_inactive", "status"])
        for user in users:
            last_active_str = (
                user.last_activity_date.strftime("%Y-%m-%d %H:%M:%S UTC")
                if user.last_activity_date
                else "Never"
            )
            writer.writerow(
                [user.login, last_active_str, user.days_inactive, user.status]
            )


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.token:
        logging.error(
            "No token provided. Use --token or set the GH_TOKEN environment variable."
        )
        return 1

    output_path = args.output or f"inactive_users_{datetime.now().strftime('%Y-%m-%d')}.csv"

    try:
        client = GitHubClient(token=args.token)

        logging.info("Fetching enterprise members for '%s'...", args.enterprise)
        members = client.get_enterprise_members(args.enterprise)

        logging.info(
            "Fetching audit log entries (last %d days) for %d members...", args.days, len(members)
        )
        member_logins = [m.get("login", "") for m in members if m.get("login")]
        actor_last_active = client.get_audit_log(args.enterprise, args.days, member_logins=member_logins)

        logging.info("Identifying inactive users (threshold: %d days)...", args.days)
        inactive = identify_inactive_users(members, actor_last_active, args.days)

        write_csv(inactive, output_path)

        logging.info(
            "Found %d inactive users out of %d total members.",
            len(inactive),
            len(members),
        )
        logging.info("Report written to: %s", output_path)

    except GitHubAPIError as e:
        logging.error("API error: %s", e)
        return 1
    except Exception as e:
        logging.error("Unexpected error: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
