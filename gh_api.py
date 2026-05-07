"""GitHub Enterprise Cloud API client with rate-limit handling and pagination."""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests

logger = logging.getLogger(__name__)

DEFAULT_PER_PAGE = 100
MAX_RETRIES = 5
INITIAL_BACKOFF = 1  # seconds


class GitHubAPIError(Exception):
    """Raised when the GitHub API returns an error."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"GitHub API error {status_code}: {message}")


class GitHubClient:
    """Authenticated GitHub REST API client with retry and pagination."""

    def __init__(self, token: str, base_url: str = "https://api.github.com"):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Make an API request with rate-limit-aware retry and backoff."""
        backoff = INITIAL_BACKOFF
        for attempt in range(MAX_RETRIES):
            resp = self.session.request(method, url, **kwargs)

            if resp.status_code == 200:
                return resp

            # Rate limit exceeded
            if resp.status_code in (403, 429):
                retry_after = resp.headers.get("Retry-After")
                rate_reset = resp.headers.get("X-RateLimit-Reset")
                if retry_after:
                    wait = int(retry_after)
                elif rate_reset:
                    wait = max(int(rate_reset) - int(time.time()), 1)
                else:
                    wait = backoff

                logger.warning(
                    "Rate limited (attempt %d/%d). Waiting %ds...",
                    attempt + 1,
                    MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
                backoff *= 2
                continue

            # Server errors — retry
            if resp.status_code >= 500:
                logger.warning(
                    "Server error %d (attempt %d/%d). Retrying in %ds...",
                    resp.status_code,
                    attempt + 1,
                    MAX_RETRIES,
                    backoff,
                )
                time.sleep(backoff)
                backoff *= 2
                continue

            # Client error — fail immediately
            raise GitHubAPIError(resp.status_code, resp.text)

        raise GitHubAPIError(resp.status_code, f"Max retries exceeded. Last: {resp.text}")

    def get_enterprise_members(self, enterprise: str) -> list[dict]:
        """Fetch all members of a GitHub Enterprise (EMU) via REST API.

        Returns a list of dicts with at least 'login' key per member.
        """
        members = []
        url = f"{self.base_url}/enterprises/{enterprise}/members"
        params = {"per_page": DEFAULT_PER_PAGE}

        while url:
            resp = self._request("GET", url, params=params)
            data = resp.json()
            members.extend(data)
            logger.info("Fetched %d members so far...", len(members))

            # Link-header pagination
            url = self._next_link(resp)
            params = None  # params are included in the next URL

        logger.info("Total enterprise members: %d", len(members))
        return members

    def get_audit_log(
        self, enterprise: str, since_days: int
    ) -> dict[str, datetime]:
        """Fetch audit log entries and return the most recent activity per actor.

        Args:
            enterprise: Enterprise slug.
            since_days: How many days back to query.

        Returns:
            Dict mapping actor login -> most recent activity datetime.
        """
        since_date = datetime.now(timezone.utc) - timedelta(days=since_days)
        phrase = f"created:>={since_date.strftime('%Y-%m-%d')}"

        actor_last_active: dict[str, datetime] = {}
        url = f"{self.base_url}/enterprises/{enterprise}/audit-log"
        params = {"phrase": phrase, "per_page": DEFAULT_PER_PAGE, "include": "all"}
        after_cursor: Optional[str] = None
        page = 0

        while True:
            if after_cursor:
                params["after"] = after_cursor

            resp = self._request("GET", url, params=params)
            entries = resp.json()

            if not entries:
                break

            page += 1
            for entry in entries:
                actor = entry.get("actor") or entry.get("user")
                if not actor:
                    continue

                ts = entry.get("created_at") or entry.get("@timestamp")
                if not ts:
                    continue

                # Parse timestamp (epoch ms or ISO string)
                if isinstance(ts, (int, float)):
                    activity_time = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                else:
                    activity_time = datetime.fromisoformat(
                        ts.replace("Z", "+00:00")
                    )

                existing = actor_last_active.get(actor)
                if existing is None or activity_time > existing:
                    actor_last_active[actor] = activity_time

            logger.info(
                "Processed audit log page %d (%d entries, %d unique actors so far)",
                page,
                len(entries),
                len(actor_last_active),
            )

            # Cursor-based pagination: use 'after' from Link header or last entry
            after_cursor = self._extract_after_cursor(resp)
            if not after_cursor:
                break

        logger.info(
            "Audit log scan complete: %d unique actors found", len(actor_last_active)
        )
        return actor_last_active

    @staticmethod
    def _next_link(resp: requests.Response) -> Optional[str]:
        """Extract the 'next' URL from the Link header."""
        link_header = resp.headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                return url
        return None

    @staticmethod
    def _extract_after_cursor(resp: requests.Response) -> Optional[str]:
        """Extract the 'after' cursor for audit log pagination."""
        next_url = GitHubClient._next_link(resp)
        if next_url:
            parsed = urlparse(next_url)
            qs = parse_qs(parsed.query)
            after_values = qs.get("after")
            if after_values:
                return after_values[0]
        return None
