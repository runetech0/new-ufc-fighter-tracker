import asyncio
from typing import Any

from patchright.async_api import Request, Route, async_playwright

from app.logs_config import get_logger

from .models import Athlete
from .scraper import parse_athletes_from_html

logger = get_logger()

UFC_BASE_URL = "https://www.ufc.com"
CARD_SELECTOR = "li.l-flex__item .c-listing-athlete-flipcard"
LOAD_MORE_SELECTOR = "a.button[title='Load more items']"
AJAX_URL_MARKER = "views/ajax"
AJAX_VIEW_MARKER = "view_name=all_athletes"

BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}


class UFCBrowser:
    def __init__(self, page_load_timeout_seconds: int = 10) -> None:
        self._page_load_timeout_seconds = page_load_timeout_seconds
        self._page_url = "https://www.ufc.com/athletes/all"
        logger.info("UFCBrowser init — scraper always runs headless")

    async def _block_resources(self, route: Route) -> None:
        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()

    async def _extract_initial_athletes(self) -> list[Athlete]:
        logger.info("Extracting initial server-rendered athletes from DOM ...")
        try:
            html = await self.page.inner_html(
                "div[data-drupal-views-infinite-scroll-content-wrapper]"
            )
            athletes = parse_athletes_from_html(f"<ul>{html}</ul>")
            logger.info(f"Initial DOM batch: {len(athletes)} athletes extracted.")
            return athletes
        except Exception as e:
            logger.error(f"Failed to extract initial athletes from DOM: {e}", exc_info=True)
            return []

    async def _capture_ajax_request(self) -> Request | None:
        logger.info("Setting up AJAX request interceptor ...")
        captured: dict[str, Any] = {}

        def on_request(request: Request) -> None:
            if (
                AJAX_URL_MARKER in request.url
                and AJAX_VIEW_MARKER in request.url
                and "captured" not in captured
            ):
                logger.debug(f"AJAX request intercepted: {request.url[:100]}...")
                captured["request"] = request

        self.page.on("request", on_request)

        load_more = self.page.locator(LOAD_MORE_SELECTOR)
        if not await load_more.count():
            logger.warning("'Load More' button not found — AJAX capture skipped.")
            return None

        logger.info("Clicking 'Load More' to trigger AJAX request ...")
        try:
            await load_more.scroll_into_view_if_needed()
            await load_more.click()
        except Exception as e:
            logger.error(f"Failed to click 'Load More': {e}", exc_info=True)
            return None

        logger.info("Waiting for AJAX request to be captured (up to 10s) ...")
        for i in range(20):
            if "request" in captured:
                logger.info(f"AJAX request captured after ~{i * 0.5:.1f}s.")
                break
            await asyncio.sleep(0.5)
        else:
            logger.warning("AJAX request not captured within 10s timeout.")

        req: Request | None = captured.get("request")
        if req:
            logger.info(f"Captured AJAX URL: {req.url[:120]}...")
        return req

    async def _capture_session_once(self) -> tuple[list[Athlete], str, dict[str, str]]:
        async with async_playwright() as p:
            logger.info("Launching headless browser ...")
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            ctx = await browser.new_context()
            logger.info("Fresh headless browser context created.")

            page = await ctx.new_page()
            await page.route("**/*", self._block_resources)
            logger.debug("Resource blocking route registered.")

            logger.info(f"Navigating to {self._page_url} ...")
            try:
                await page.goto(
                    self._page_url, timeout=120_000, wait_until="domcontentloaded"
                )
                logger.info("Page DOM loaded.")
            except Exception as e:
                logger.error(f"Navigation failed: {e}", exc_info=True)
                await browser.close()
                raise

            logger.info(f"Waiting for athlete cards ({CARD_SELECTOR}) ...")
            try:
                await page.wait_for_selector(CARD_SELECTOR, timeout=60_000)
                logger.info("Athlete cards visible.")
            except Exception as e:
                logger.error(f"Timed out waiting for athlete cards: {e}", exc_info=True)
                await browser.close()
                raise

            self.page = page

            initial_athletes = await self._extract_initial_athletes()
            captured_req = await self._capture_ajax_request()

            if captured_req is None:
                logger.error("AJAX request capture failed — closing browser.")
                await browser.close()
                raise RuntimeError("No AJAX request captured — cannot continue.")

            logger.info("Reading AJAX request headers ...")
            headers = await captured_req.all_headers()
            ajax_url = captured_req.url
            logger.info(
                f"AJAX session ready. Starting page: "
                f"{ajax_url.split('page=')[-1].split('&')[0]}"
            )

            logger.info("Closing browser ...")
            await browser.close()
            return initial_athletes, ajax_url, headers

    async def capture_session(self) -> tuple[list[Athlete], str, dict[str, str]]:
        """Launch a fresh headless browser with automatic retry on failure.
        Uses exponential backoff: 10s → 20s → 40s → 80s → 160s between attempts."""
        MAX_RETRIES = 5
        BASE_DELAY = 10

        logger.info("=== capture_session() start ===")
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = await self._capture_session_once()
                if attempt > 1:
                    logger.info(f"capture_session() succeeded on attempt {attempt}.")
                logger.info("=== capture_session() complete ===")
                return result
            except Exception as e:
                last_error = e
                if attempt == MAX_RETRIES:
                    break
                delay = BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    f"capture_session() attempt {attempt}/{MAX_RETRIES} failed: {e} — "
                    f"retrying in {delay}s ..."
                )
                await asyncio.sleep(delay)

        logger.error(
            f"capture_session() failed after {MAX_RETRIES} attempts. Last error: {last_error}",
            exc_info=True,
        )
        raise RuntimeError(
            f"Browser failed to launch after {MAX_RETRIES} attempts."
        ) from last_error
