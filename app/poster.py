import asyncio
import logging
import random

from patchright.async_api import Playwright, async_playwright


class TweetPoster:
    def __init__(
        self,
        auth_token: str,
        screenshots_dir: str,
        *,
        logger: logging.Logger,
    ):
        self._auth_token = auth_token
        self._screenshots_dir = screenshots_dir
        self._logger = logger
        self._posted_tweets_count = 0

        self._homepage_url = "https://x.com"
        self._cookie_urls = [
            "https://x.com",
            "https://api.x.com",
            "https://upload.x.com",
        ]

        self._playwright: Playwright | None = None

    async def setup(self) -> None:
        self._logger.info("=== TweetPoster.setup() start ===")
        self._playwright = await async_playwright().start()
        browser = await self._playwright.chromium.launch(headless=False)
        ctx = await browser.new_context()
        self._page = await ctx.new_page()
        self._logger.info("New page created for poster.")

        for url in self._cookie_urls:
            self._logger.debug(f"Setting auth_token cookie for {url}")
            await ctx.add_cookies(
                [{"name": "auth_token", "value": self._auth_token, "url": url}]
            )
            await asyncio.sleep(0.2)

        self._logger.info(f"Navigating to {self._homepage_url} ...")
        await self._page.goto(self._homepage_url, timeout=120_000)
        self._logger.info("=== TweetPoster.setup() complete — poster ready ===")

    async def close(self) -> None:
        self._logger.info("Closing TweetPoster browser ...")
        try:
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
                self._logger.info("TweetPoster browser closed.")
        except Exception as e:
            self._logger.warning(f"Error while closing TweetPoster browser: {e}")

    async def _random_float_wait(self, min: float = 0.1, max: float = 0.5) -> None:
        await asyncio.sleep(random.uniform(min, max))

    async def page_screenshot(self, name: str) -> None:
        path = f"{self._screenshots_dir}/{name}.png"
        self._logger.debug(f"Screenshot: {path}")
        await self._page.screenshot(path=path)

    async def click_new_tweet_button(self) -> bool:
        self._logger.info("Navigating to compose/post ...")
        try:
            await self._page.goto("https://x.com/compose/post")
            await self._random_float_wait(1.0, 2.0)
            await self.page_screenshot(f"tweet_composer_loaded_{self._posted_tweets_count}")
            self._logger.info("Compose page loaded.")
            return True
        except Exception as e:
            self._logger.error(f"Failed to navigate to compose page: {e}", exc_info=True)
            return False

    async def click_add_media_button(self, media_path: str) -> bool:
        self._logger.info(f"Uploading media: {media_path}")
        try:
            media_button = await self._page.query_selector(
                'button[aria-label="Add photos or video"]'
            )
            if not media_button:
                self._logger.warning("'Add photos or video' button not found in DOM.")
                return False

            self._logger.info("Clicking media button and waiting for file chooser ...")
            async with self._page.expect_file_chooser(timeout=10_000) as fc_info:
                await media_button.click()

            file_chooser = await fc_info.value
            await file_chooser.set_files(media_path)
            self._logger.info(f"File set in chooser: {media_path}")

            self._logger.info("Waiting for upload confirmation ([data-testid='attachments']) ...")
            await self._page.wait_for_selector('[data-testid="attachments"]', timeout=30_000)
            self._logger.info("Media upload confirmed.")
            return True

        except Exception as e:
            self._logger.error(f"Media upload failed: {e}", exc_info=True)
            return False

    async def check_and_click_got_it_button(self) -> bool:
        try:
            await self._random_float_wait(0.5, 1.0)
            got_it = await self._page.query_selector('button:has-text("Got it")')
            if not got_it or not await got_it.is_visible():
                self._logger.debug("'Got it' button not present.")
                return False
            await got_it.click()
            self._logger.info("'Got it' dialog dismissed.")
            await self._random_float_wait(0.3, 0.6)
            return True
        except Exception as e:
            self._logger.debug(f"check_and_click_got_it_button error (non-critical): {e}")
            return False

    async def click_post_tweet_button(self) -> bool:
        self._logger.info("Waiting for Post button to become enabled ...")
        try:
            selector = 'button[data-testid="tweetButton"], button[data-testid="tweetButtonInline"]'
            max_wait, interval, waited = 10.0, 0.5, 0.0
            post_button = None

            while waited < max_wait:
                await asyncio.sleep(interval)
                waited += interval
                btn = await self._page.query_selector(selector)
                if btn and not await btn.is_disabled():
                    post_button = btn
                    self._logger.info(f"Post button enabled after {waited:.1f}s.")
                    break
                self._logger.debug(f"Post button not ready yet ({waited:.1f}s / {max_wait}s) ...")

            if not post_button:
                self._logger.warning(
                    f"Post button did not become enabled within {max_wait}s."
                )
                return False

            await self._random_float_wait(0.3, 0.6)

            box = await post_button.bounding_box()
            if box:
                x = box["x"] + random.uniform(0.3, 0.7) * box["width"]
                y = box["y"] + random.uniform(0.3, 0.7) * box["height"]
                await self._page.mouse.move(x, y)
                await self._random_float_wait(0.1, 0.3)
                self._logger.debug(f"Mouse moved to post button at ({x:.0f}, {y:.0f}).")

            await post_button.click()
            self._logger.info("Post button clicked.")
            await self._random_float_wait(3.0, 5.0)
            return True

        except Exception as e:
            self._logger.error(f"Error clicking post button: {e}", exc_info=True)
            return False

    async def post_tweet(
        self,
        text: str | None = None,
        media_path: str | None = None,
    ) -> None:
        if not text and not media_path:
            raise ValueError("Either text or media_path must be provided.")

        self._logger.info(
            f"=== post_tweet() start — "
            f"text={'yes' if text else 'no'}, "
            f"media={'yes' if media_path else 'no'}, "
            f"count={self._posted_tweets_count} ==="
        )
        if text:
            self._logger.debug(f"Tweet text preview: {text[:80]}{'...' if len(text) > 80 else ''}")

        try:
            if not await self.click_new_tweet_button():
                self._logger.warning("Aborting tweet — could not open compose dialog.")
                return

            self._logger.info("Waiting for tweet textarea ...")
            await self._page.wait_for_selector(
                '[data-testid="tweetTextarea_0"]', timeout=15_000
            )
            self._logger.info("Tweet textarea ready.")
            await self._random_float_wait(0.5, 1.0)

            if text:
                self._logger.info(f"Typing tweet text ({len(text)} chars) ...")
                await self._page.evaluate(
                    "document.querySelector('[data-testid=\"tweetTextarea_0\"]').focus()"
                )
                await self._page.keyboard.type(text, delay=random.uniform(30, 80))
                self._logger.info("Tweet text typed.")
                await self._random_float_wait(0.5, 1.0)

            if media_path:
                if not await self.click_add_media_button(media_path):
                    self._logger.warning("Aborting tweet — media upload failed.")
                    await self._page.keyboard.press("Escape")
                    return
                await self.page_screenshot(
                    f"media_upload_done_{self._posted_tweets_count}"
                )

            if not await self.click_post_tweet_button():
                self._logger.warning("Aborting tweet — failed to click post button.")
                await self._page.keyboard.press("Escape")
                return

            self._posted_tweets_count += 1
            self._logger.info(
                f"=== Tweet posted successfully. Total posted: {self._posted_tweets_count} ==="
            )
            await self.page_screenshot(f"posted_tweet_{self._posted_tweets_count}")
            await self.check_and_click_got_it_button()

        except Exception as e:
            self._logger.error(f"Unhandled error in post_tweet(): {e}", exc_info=True)
            try:
                await self._page.keyboard.press("Escape")
                self._logger.debug("Pressed Escape to close any open dialogs after error.")
            except Exception as esc_err:
                self._logger.debug(f"Could not press Escape after error: {esc_err}")
