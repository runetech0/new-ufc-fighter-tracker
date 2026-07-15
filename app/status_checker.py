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
STATUS_ACTIVE = "active"
STATUS_NOT_FIGHTING = "not fighting"
STATUS_RETIRED = "retired"

# Statuses that mean the fighter is no longer under active UFC contract.
INACTIVE_STATUSES = {STATUS_NOT_FIGHTING, STATUS_RETIRED}

# All known status values.  Any p.hero-profile__tag whose text is NOT in this
# set is a supplementary label (e.g. "Hall of Fame", "Title Holder") and is
# ignored when looking for the fighter's activity status.
KNOWN_STATUSES = {STATUS_ACTIVE, STATUS_NOT_FIGHTING, STATUS_RETIRED}

# Multi-language aliases → canonical English status.
# UFC pages are geo-IP localised; the server may ignore Accept-Language headers.
_STATUS_MAP: dict[str, str] = {
    # English
    "active": STATUS_ACTIVE,
    "not fighting": STATUS_NOT_FIGHTING,
    "retired": STATUS_RETIRED,
    # French
    "actif": STATUS_ACTIVE,
    "active": STATUS_ACTIVE,        # French feminine (identical spelling)
    "ne se bat pas": STATUS_NOT_FIGHTING,
    "retraité": STATUS_RETIRED,
    "retraitée": STATUS_RETIRED,
    "à la retraite": STATUS_RETIRED,
    # Spanish (UFC.com also localises to Spanish)
    "activo": STATUS_ACTIVE,
    "activa": STATUS_ACTIVE,
    "no está peleando": STATUS_NOT_FIGHTING,
    "retirado": STATUS_RETIRED,
    "retirada": STATUS_RETIRED,
    # Portuguese
    "ativo": STATUS_ACTIVE,
    "ativa": STATUS_ACTIVE,
    "não está lutando": STATUS_NOT_FIGHTING,
    "aposentado": STATUS_RETIRED,
    "aposentada": STATUS_RETIRED,
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Two failure categories to distinguish HTTP/network errors from parse misses.
_FAIL_HTTP = "http_error"
_FAIL_NO_STATUS = "no_status_tag"


async def _fetch_status(
    client: httpx.AsyncClient,
    profile_url: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, str | None, str | None]:
    """Return (profile_url, status_text | None, failure_reason | None).

    failure_reason is one of _FAIL_HTTP, _FAIL_NO_STATUS, or None (success).
    Scans all p.hero-profile__tag elements and returns the first whose text
    matches a known status value (Active / Not Fighting / Retired).  Extra
    labels such as 'Hall of Fame' or 'Title Holder' are ignored.
    """
    async with semaphore:
        try:
            r = await client.get(profile_url)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.select(STATUS_SELECTOR):
                text = el.get_text(strip=True).lower()
                canonical = _STATUS_MAP.get(text)
                if canonical:
                    return profile_url, canonical, None
            # Page loaded fine but no recognised status tag — capture diagnostics.
            title = soup.title.get_text(strip=True) if soup.title else "no <title>"
            all_tags = [el.get_text(strip=True) for el in soup.select(STATUS_SELECTOR)]
            body_snippet = r.text[:300].replace("\n", " ")
            diag = (
                f"title='{title}' | "
                f"p.hero-profile__tag found={all_tags} | "
                f"body[:300]={body_snippet!r}"
            )
            return profile_url, None, f"{_FAIL_NO_STATUS}: {diag}"
        except Exception as exc:
            return profile_url, None, f"{_FAIL_HTTP}: {exc}"


async def fetch_statuses(
    profile_urls: set[str],
    concurrency: int = 5,
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
    http_errors: list[str] = []
    no_status_count = 0

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

    no_status_samples: list[str] = []
    for url, status, reason in results:
        if status is not None:
            statuses[url] = status
        elif reason and reason.startswith(_FAIL_HTTP):
            http_errors.append(f"{url} — {reason}")
        else:
            no_status_count += 1
            if len(no_status_samples) < 3 and reason:
                no_status_samples.append(f"  {url}\n    {reason}")

    failure_count = len(http_errors) + no_status_count
    had_failures = failure_count > 0

    if http_errors:
        sample = http_errors[:5]
        logger.warning(
            f"Status check — {len(http_errors)} HTTP/network error(s). "
            f"First {len(sample)}:\n" + "\n".join(f"  {e}" for e in sample)
        )

    if no_status_count:
        sample_txt = "\n".join(no_status_samples) if no_status_samples else ""
        logger.warning(
            f"Status check — {no_status_count} profile(s) loaded OK but had "
            f"no recognisable status tag (selector='{STATUS_SELECTOR}', "
            f"known={sorted(KNOWN_STATUSES)})."
            + (f"\nSample diagnostics:\n{sample_txt}" if sample_txt else "")
        )

    logger.info(
        f"Status check complete — {len(statuses)} fetched successfully, "
        f"{len(http_errors)} HTTP errors, {no_status_count} missing status tag."
    )
    return statuses, had_failures
