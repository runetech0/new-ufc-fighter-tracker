"""Checks individual fighter profile pages for Active / Not Fighting status.

The /athletes/all listing shows every fighter ever (active, retired, released)
so URL-presence never changes.  The status badge on each profile page is the
only reliable signal for whether a fighter is currently under UFC contract.
"""

import asyncio

import httpx
from bs4 import BeautifulSoup

from app.logs_config import get_logger

logger = get_logger()

STATUS_SELECTOR = "p.hero-profile__tag"
STATUS_ACTIVE = "Active"
STATUS_NOT_FIGHTING = "Not Fighting"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


async def _fetch_status(
    client: httpx.AsyncClient,
    profile_url: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, str | None]:
    """Return (profile_url, status_text | None).  None means the request failed."""
    async with semaphore:
        try:
            r = await client.get(profile_url)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            el = soup.select_one(STATUS_SELECTOR)
            status = el.get_text(strip=True) if el else None
            return profile_url, status
        except Exception as exc:
            logger.debug(f"Status check failed for {profile_url}: {exc}")
            return profile_url, None


async def fetch_statuses(
    profile_urls: set[str],
    concurrency: int = 20,
    timeout: int = 15,
) -> tuple[dict[str, str], bool]:
    """Fetch all fighter profiles concurrently and return their live statuses.

    Returns:
        (statuses, had_failures)
        statuses: dict mapping profile_url → live status text for every URL
                  that was successfully fetched.  URLs that failed to fetch are
                  absent from the dict (caller should treat absence as unknown,
                  not as a status change).
        had_failures: True if any profile fetch failed.
    """
    if not profile_urls:
        return {}, False

    logger.info(
        f"Status check — fetching {len(profile_urls)} profile(s) "
        f"(concurrency={concurrency}) ..."
    )

    semaphore = asyncio.Semaphore(concurrency)
    statuses: dict[str, str] = {}
    failure_count = 0

    async with httpx.AsyncClient(
        timeout=timeout,
        headers=_HEADERS,
        follow_redirects=True,
    ) as client:
        tasks = [
            asyncio.create_task(_fetch_status(client, url, semaphore))
            for url in profile_urls
        ]
        results = await asyncio.gather(*tasks)

    for url, status in results:
        if status is None:
            failure_count += 1
        else:
            statuses[url] = status

    had_failures = failure_count > 0
    if had_failures:
        logger.warning(
            f"Status check — {failure_count}/{len(profile_urls)} profile fetch(es) failed "
            f"(those fighters are excluded from comparison this cycle)."
        )
    logger.info(
        f"Status check complete — {len(statuses)} fetched, {failure_count} failures."
    )
    return statuses, had_failures
