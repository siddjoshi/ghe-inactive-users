"""Identify inactive users by comparing enterprise members against audit log activity."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class UserActivity:
    login: str
    last_activity_date: Optional[datetime]
    days_inactive: int
    status: str  # "inactive" or "active"


def identify_inactive_users(
    members: list[dict],
    actor_last_active: dict[str, datetime],
    threshold_days: int,
) -> list[UserActivity]:
    """Compare member list against audit log activity to find inactive users.

    Args:
        members: List of enterprise member dicts (must have 'login' key).
        actor_last_active: Mapping of actor login -> last activity datetime.
        threshold_days: Number of days without activity to be considered inactive.

    Returns:
        List of UserActivity records for inactive users, sorted by days_inactive desc.
    """
    now = datetime.now(timezone.utc)
    inactive_users: list[UserActivity] = []

    for member in members:
        login = member.get("login", "")
        if not login:
            continue

        last_active = actor_last_active.get(login)

        if last_active is None:
            # No audit log activity at all in the queried window
            inactive_users.append(
                UserActivity(
                    login=login,
                    last_activity_date=None,
                    days_inactive=threshold_days,
                    status="inactive (no activity found)",
                )
            )
        else:
            days_since = (now - last_active).days
            if days_since >= threshold_days:
                inactive_users.append(
                    UserActivity(
                        login=login,
                        last_activity_date=last_active,
                        days_inactive=days_since,
                        status="inactive",
                    )
                )

    # Sort by days inactive descending (most inactive first)
    inactive_users.sort(key=lambda u: u.days_inactive, reverse=True)
    return inactive_users
