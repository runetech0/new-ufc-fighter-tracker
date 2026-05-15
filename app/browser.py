import asyncio
import os
import random
from pathlib import Path
from typing import Any

import aiosqlite
from patchright.async_api import Request, Route, async_playwright

from app.logs_config import get_logger

from .db import DB_PATH, init_db, save_athlete
from .gvs import SCREENSHOTS_DIR, SESSIONS_DIR
from .models import Athlete
from .scraper import Scraper, parse_athletes_from_html

logger = get_logger()

UFC_BASE_URL = "https://www.ufc.com"
CARD_SELECTOR = "li.l-flex__item .c-listing-athlete-flipcard"
LOAD_MORE_SELECTOR = "a.button[title='Load more items']"
AJAX_URL_MARKER = "views/ajax"
AJAX_VIEW_MARKER = "view_name=all_athletes"

BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}


class UFCBrowser:
    def __init__(
        self,
        headless: bool = False,
        session_dir_name: str | None = None,
        page_load_timeout_seconds: int = 10,
    ):
        self.headless = headless
        logger.info(f"UFCBrowser init — headless={headless}")

        self._page_load_timeout_seconds = page_load_timeout_seconds
        session_dir_name = session_dir_name or "default"

        self._session_dir = (
            Path(os.path.join(SESSIONS_DIR, session_dir_name)).absolute().__str__()
        )
        self._screenshots_dir = (
            Path(os.path.join(SCREENSHOTS_DIR, f"{session_dir_name}-screenshots"))
            .absolute()
            .__str__()
        )

        os.makedirs(self._screenshots_dir, exist_ok=True)
        logger.debug(f"Session dir: {self._session_dir}")
        logger.debug(f"Screenshots dir: {self._screenshots_dir}")

        self._page_url = "https://www.ufc.com/athletes/all"

    async def start(self) -> None:
        try:
            await self.main()
        except Exception as e:
            logger.error(f"Unhandled error in start(): {e}", exc_info=True)
            try:
                logger.info("Attempting to close browser after error ...")
                await self.browser.close()
                logger.info("Browser closed after error.")
            except Exception as close_err:
                logger.error(f"Failed to close browser: {close_err}", exc_info=True)

    async def _random_wait(self, min: int = 1, max: int = 1) -> None:
        await asyncio.sleep(random.randint(min, max))

    async def page_screenshot(self, name: str) -> None:
        path = f"{self._screenshots_dir}/{name}.png"
        logger.debug(f"Taking screenshot: {path}")
        await self.page.screenshot(path=path)

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

    async def capture_session(self) -> tuple[list[Athlete], str, dict[str, str]]:
        """Launch browser, capture the AJAX session, return (initial_athletes, ajax_url, headers)."""
        logger.info("=== capture_session() start ===")
        async with async_playwright() as p:
            logger.info(f"Launching {'headless' if self.headless else 'headed'} browser ...")
            browser = await p.chromium.launch_persistent_context(
                headless=self.headless,
                user_data_dir=self._session_dir,
                args=["--disable-blink-features=AutomationControlled"],
            )
            logger.info("Browser launched.")

            page = await browser.new_page()
            await page.route("**/*", self._block_resources)
            logger.debug("Resource blocking route registered.")

            await asyncio.sleep(1)
            if len(browser.pages) > 1:
                logger.debug(f"Closing {len(browser.pages) - 1} extra background page(s).")
                await browser.pages[0].close()

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
            self.browser = browser

            initial_athletes = await self._extract_initial_athletes()
            captured_req = await self._capture_ajax_request()

            if captured_req is None:
                logger.error("AJAX request capture failed — closing browser.")
                await browser.close()
                raise RuntimeError("No AJAX request captured — cannot continue.")

            logger.info("Reading AJAX request headers ...")
            headers = await captured_req.all_headers()
            ajax_url = captured_req.url
            logger.info(f"AJAX session ready. Starting page: {ajax_url.split('page=')[-1].split('&')[0]}")

            logger.info("Closing browser ...")
            await browser.close()
            logger.info("=== capture_session() complete ===")

            return initial_athletes, ajax_url, headers

    async def main(self) -> None:
        logger.info("=== UFCBrowser.main() start ===")
        async with aiosqlite.connect(DB_PATH) as db:
            await init_db(db)
            logger.info("DB initialised.")

            initial_athletes, ajax_url, headers = await self.capture_session()
            logger.info(f"Session captured. Initial batch: {len(initial_athletes)} athletes.")

            saved = 0

            async def on_athlete(athlete: Athlete) -> None:
                nonlocal saved
                is_new = await save_athlete(db, athlete)
                if is_new:
                    saved += 1
                    print(athlete)

            for athlete in initial_athletes:
                await on_athlete(athlete)

            total = await Scraper(ajax_url, headers).run(on_athlete)
            logger.info(
                f"=== main() complete. Total new athletes saved: {saved + total} ==="
            )
