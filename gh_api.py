"""GitHub Enterprise Cloud API client with rate-limit handling and pagination."""

import json
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

MEMBERS_QUERY = """
query($slug: String!, $cursor: String) {
  enterprise(slug: $slug) {
    members(first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        ... on EnterpriseUserAccount {
          login
          name
        }
        ... on User {
          login
          name
        }
      }
    }
  }
}
"""


class GitHubAPIError(Exception):
    """Raised when the GitHub API returns an error."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"GitHub API error {status_code}: {message}")


class GitHubClient:
    """Authenticated GitHub REST/GraphQL API client with retry and pagination."""

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
            kwargs.setdefault("timeout", 30)
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

    def _graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        """Execute a GraphQL query with retry logic."""
        url = f"{self.base_url}/graphql"
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        resp = self._request("POST", url, json=payload)
        result = resp.json()

        if "errors" in result:
            error_msgs = "; ".join(e.get("message", str(e)) for e in result["errors"])
            raise GitHubAPIError(resp.status_code, f"GraphQL errors: {error_msgs}")

        return result["data"]

    def get_enterprise_members(self, enterprise: str) -> list[dict]:
        """Fetch all members of a GitHub Enterprise (EMU) via GraphQL API.

        Returns a list of dicts with at least 'login' key per member.
        """
        members = []
        cursor = None

        while True:
            data = self._graphql(MEMBERS_QUERY, {"slug": enterprise, "cursor": cursor})
            members_data = data["enterprise"]["members"]
            nodes = members_data["nodes"]
            members.extend(nodes)
            logger.info("Fetched %d members so far...", len(members))

            page_info = members_data["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]

        logger.info("Total enterprise members: %d", len(members))
        return members

    def get_audit_log(
        self, enterprise: str, since_days: int, member_logins: Optional[list[str]] = None
    ) -> dict[str, datetime]:
        """Fetch audit log entries and return the most recent activity per actor.

        When member_logins is provided, queries per-user for efficiency (1 API call
        per member instead of paginating the entire audit log).

        Args:
            enterprise: Enterprise slug.
            since_days: How many days back to query.
            member_logins: Optional list of specific logins to check.

        Returns:
            Dict mapping actor login -> most recent activity datetime.
        """
        since_date = datetime.now(timezone.utc) - timedelta(days=since_days)
        date_phrase = f"created:>={since_date.strftime('%Y-%m-%d')}"

        actor_last_active: dict[str, datetime] = {}
        url = f"{self.base_url}/enterprises/{enterprise}/audit-log"

        if member_logins:
            # Per-user queries: fetch only 1 entry (most recent) per user
            for login in member_logins:
                phrase = f"{date_phrase} actor:{login}"
                params = {"phrase": phrase, "per_page": 1, "include": "all", "order": "desc"}
                logger.info("Checking audit log for user '%s'...", login)

                resp = self._request("GET", url, params=params)
                entries = resp.json()

                if entries:
                    entry = entries[0]
                    ts = entry.get("created_at") or entry.get("@timestamp")
                    if ts:
                        if isinstance(ts, (int, float)):
                            activity_time = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                        else:
                            activity_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        actor_last_active[login] = activity_time
                        logger.info("  Last activity: %s", activity_time.strftime("%Y-%m-%d %H:%M:%S UTC"))
                    else:
                        logger.info("  No parseable timestamp found")
                else:
                    logger.info("  No activity in the last %d days", since_days)
        else:
            # Full scan: paginate through all audit log entries
            params = {"phrase": date_phrase, "per_page": DEFAULT_PER_PAGE, "include": "all"}
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

                    if isinstance(ts, (int, float)):
                        activity_time = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                    else:
                        activity_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))

                    existing = actor_last_active.get(actor)
                    if existing is None or activity_time > existing:
                        actor_last_active[actor] = activity_time

                logger.info(
                    "Processed audit log page %d (%d entries, %d unique actors so far)",
                    page, len(entries), len(actor_last_active),
                )

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
