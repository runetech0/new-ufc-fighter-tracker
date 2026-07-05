import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from app.logs_config import get_logger

from .models import Athlete

logger = get_logger()

UFC_BASE_URL = "https://www.ufc.com"
_INFINITE_SCROLL_METHOD = "infiniteScrollInsertView"
# Abort the scrape if this many individual page fetches fail.  Beyond this
# threshold the seen-URL set would be too incomplete to trust for removal
# detection, so there is no benefit in continuing.
_MAX_PAGE_FAILURES = 20
# Retry each page this many times before counting it as a failure.
_PAGE_RETRY_ATTEMPTS = 3
_PAGE_RETRY_BASE_DELAY = 2  # seconds; doubles each retry

OnAthleteFn = Callable[[Athlete], Awaitable[None]]


def _build_page_url(base_url: str, page: int) -> str:
    parsed = urlparse(base_url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params["page"] = [str(page)]
    new_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=new_query))


def _parse_athletes(soup: BeautifulSoup) -> list[Athlete]:
    athletes: list[Athlete] = []

    for card in soup.select("li.l-flex__item .c-listing-athlete-flipcard"):
        name_el = card.select_one(".c-listing-athlete__name")
        name = name_el.get_text(strip=True) if name_el else None
        if not name:
            continue

        nickname_el = card.select_one(".c-listing-athlete__nickname .field__item")
        nickname = nickname_el.get_text(strip=True).strip('"') if nickname_el else None

        weight_el = card.select_one(".c-listing-athlete__title .field__item")
        weight_class = weight_el.get_text(strip=True) if weight_el else None

        record_el = card.select_one(".c-listing-athlete__record")
        record = record_el.get_text(strip=True) if record_el else None

        link_el = card.select_one(".c-listing-athlete-flipcard__action a")
        href = link_el.get("href") if link_el else None
        profile_url = f"{UFC_BASE_URL}{href}" if href else None

        img_el = card.select_one(".c-listing-athlete__thumbnail img")
        if not img_el:
            img_el = card.select_one(".c-listing-athlete__bgimg img")
        raw_src = img_el.get("src") if img_el else None
        if isinstance(raw_src, str) and raw_src.startswith("/"):
            raw_src = f"https://ufc.com{raw_src}"
        image_url = raw_src if isinstance(raw_src, str) else None

        athletes.append(
            Athlete(
                name=name,
                nickname=nickname or None,
                weight_class=weight_class,
                record=record,
                profile_url=profile_url,
                image_url=image_url,
            )
        )

    return athletes


def _extract_view_data(raw_json: Any) -> str | None:
    """Extract the HTML payload from the Drupal Views AJAX JSON response.

    Accepts the raw parsed JSON value (any type) so the caller does not need
    to pre-validate the structure.  Returns None if the expected command is
    absent or the response shape is unexpected.
    """
    if not isinstance(raw_json, list):
        return None
    for item in raw_json:  # type: ignore[reportUnknownVariableType]
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = cast(dict[str, Any], item)
        if (
            entry.get("command") == "insert"
            and entry.get("method") == _INFINITE_SCROLL_METHOD
        ):
            data = entry.get("data", "")
            if isinstance(data, str) and data:
                return data
    return None


def parse_athletes_from_html(html: str) -> list[Athlete]:
    """Parse athletes from raw HTML (used for the initial server-rendered batch)."""
    return _parse_athletes(BeautifulSoup(html, "html.parser"))


class Scraper:
    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        concurrency: int = 5,
    ) -> None:
        self._base_url = base_url
        self._headers = headers
        self._concurrency = concurrency

        self._page = int(parse_qs(urlparse(base_url).query).get("page", ["1"])[0])
        self._lock = asyncio.Lock()
        self._done = False
        self._total = 0
        # Set to True if any page fetch fails (network error, bad HTTP status,
        # JSON parse error). Used by callers to decide whether to trust the
        # seen-URL set for removal detection.
        self._had_failures = False
        # Running count of failed pages across all workers; protected by _lock.
        self._failure_count = 0

        logger.info(
            f"Scraper init — start_page={self._page}, concurrency={concurrency}"
        )

    @property
    def had_failures(self) -> bool:
        """True if at least one page could not be fetched successfully."""
        return self._had_failures

    async def _next_page(self) -> int | None:
        async with self._lock:
            if self._done:
                return None
            page = self._page
            self._page += 1
            return page

    async def _fetch_page(self, client: httpx.AsyncClient, page: int) -> list[Athlete]:
        """Fetch one AJAX page and return its athletes.

        Retries up to _PAGE_RETRY_ATTEMPTS times with exponential backoff on
        transient errors (5xx, network issues, malformed JSON) before raising.

        Returns an empty list *only* when the page genuinely has no data
        (i.e. end of pagination).  Raises on every other failure so the
        worker can count it as a failure.
        """
        url = _build_page_url(self._base_url, page)
        logger.debug(f"Fetching page {page}: {url[:80]}...")

        last_exc: Exception | None = None
        for attempt in range(1, _PAGE_RETRY_ATTEMPTS + 1):
            try:
                response = await client.get(url, headers=self._headers)

                if response.status_code != 200:
                    logger.warning(
                        f"Page {page} (attempt {attempt}/{_PAGE_RETRY_ATTEMPTS}): "
                        f"HTTP {response.status_code} — {response.text[:200]}"
                    )
                    response.raise_for_status()

                try:
                    raw_json: Any = response.json()
                except Exception as e:
                    raise ValueError(
                        f"Page {page}: invalid JSON — {e}. "
                        f"Response snippet: {response.text[:300]}"
                    ) from e

                html = _extract_view_data(raw_json)
                if not html:
                    # _extract_view_data returns None both for a non-list response
                    # and for a list with no infiniteScrollInsertView command.
                    # The latter means genuine end of pagination.
                    if not isinstance(raw_json, list):
                        raise ValueError(
                            f"Page {page}: expected JSON array, got "
                            f"{type(raw_json).__name__}. "
                            f"Snippet: {str(raw_json)[:200]}"
                        )
                    logger.debug(
                        f"Page {page}: no '{_INFINITE_SCROLL_METHOD}' command — "
                        f"end of pages."
                    )
                    return []

                athletes = _parse_athletes(BeautifulSoup(html, "html.parser"))
                logger.debug(f"Page {page}: parsed {len(athletes)} athlete cards.")
                return athletes

            except Exception as exc:
                last_exc = exc
                if attempt < _PAGE_RETRY_ATTEMPTS:
                    delay = _PAGE_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        f"Page {page}: attempt {attempt}/{_PAGE_RETRY_ATTEMPTS} "
                        f"failed ({exc}) — retrying in {delay}s ..."
                    )
                    await asyncio.sleep(delay)

        raise last_exc  # type: ignore[misc]

    async def _worker(
        self, worker_id: int, client: httpx.AsyncClient, on_athlete: OnAthleteFn
    ) -> None:
        logger.debug(f"Worker-{worker_id} started.")
        while True:
            page = await self._next_page()
            if page is None:
                logger.debug(f"Worker-{worker_id} stopping — done flag set.")
                return

            try:
                athletes = await self._fetch_page(client, page)
            except Exception as e:
                # Record the failure but do NOT stop the scrape — other pages
                # may still succeed.  If failures accumulate past the threshold
                # we abort to avoid spinning on a degraded server.
                async with self._lock:
                    self._had_failures = True
                    self._failure_count += 1
                    failure_count = self._failure_count
                    if failure_count > _MAX_PAGE_FAILURES:
                        self._done = True

                logger.error(
                    f"Worker-{worker_id} page {page}: fetch failed "
                    f"(total failures: {failure_count}) — {e}",
                    exc_info=True,
                )

                if failure_count > _MAX_PAGE_FAILURES:
                    logger.error(
                        f"Worker-{worker_id}: failure threshold ({_MAX_PAGE_FAILURES}) "
                        f"exceeded — aborting scrape."
                    )
                    return

                # Skip this page and move to the next one.
                continue

            if not athletes:
                # Genuine end of pagination — signal all workers to stop.
                logger.info(f"Worker-{worker_id} page {page}: empty — signalling done.")
                async with self._lock:
                    self._done = True
                return

            for athlete in athletes:
                try:
                    await on_athlete(athlete)

                except Exception as e:
                    logger.error(
                        f"Worker-{worker_id} page {page}: on_athlete callback failed for "
                        f"'{athlete.name}' — {e}",
                        exc_info=True,
                    )

            async with self._lock:
                self._total += len(athletes)

            logger.info(
                f"Worker-{worker_id} page {page}: {len(athletes)} athletes "
                f"(running total: {self._total})"
            )

    async def run(self, on_athlete: OnAthleteFn) -> int:
        logger.info(
            f"=== Scraper.run() start — {self._concurrency} workers, "
            f"starting at page {self._page} ==="
        )
        async with httpx.AsyncClient(timeout=30) as client:
            workers = [
                asyncio.create_task(self._worker(i, client, on_athlete))
                for i in range(self._concurrency)
            ]
            await asyncio.gather(*workers)

        logger.info(
            f"=== Scraper.run() complete — total pages processed ~{self._page - 1}, athletes: {self._total} ==="
        )
        return self._total
