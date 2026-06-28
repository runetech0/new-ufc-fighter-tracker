import asyncio
import os
import re
import tempfile
import time

import aiosqlite
import httpx

from app.logs_config import get_logger

from .browser import UFCBrowser
from .db import (
    DB_PATH,
    get_all_active_profile_urls,
    get_athlete_count,
    get_random_athlete,
    get_random_removed_athlete,
    init_db,
    mark_athletes_removed,
    save_athlete,
)
from .models import Athlete
from .poster import TweetPoster
from .scraper import Scraper

logger = get_logger()

POLL_INTERVAL = 1200  # 20 minutes


def _format_tweet(athlete: Athlete) -> str:
    nickname = f' "{athlete.nickname}"' if athlete.nickname else ""
    lines = [
        "✅ Fighter Added:",
        f"{athlete.name}{nickname}",
    ]
    if athlete.weight_class:
        lines.append(f"Division: {athlete.weight_class}")
    if athlete.record:
        record = re.sub(r"\s*\(W-L-D\)", "", athlete.record).strip()
        lines.append(f"Record: {record}")
    if athlete.profile_url:
        lines.append(f"🔗 {athlete.profile_url}")
    return "\n".join(lines)


def _format_removed_tweet(athlete: Athlete) -> str:
    nickname = f' "{athlete.nickname}"' if athlete.nickname else ""
    lines = [
        "❌ Fighter Removed:",
        f"{athlete.name}{nickname}",
    ]
    if athlete.weight_class:
        lines.append(f"Division: {athlete.weight_class}")
    if athlete.record:
        record = re.sub(r"\s*\(W-L-D\)", "", athlete.record).strip()
        lines.append(f"Record: {record}")
    return "\n".join(lines)


async def _download_image(url: str) -> str | None:
    logger.info(f"Downloading fighter image: {url[:80]}...")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status()

        ext = ".png" if ".png" in url else ".jpg"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        tmp.write(response.content)
        tmp.close()
        logger.info(f"Image downloaded to temp file: {tmp.name} ({len(response.content)} bytes)")
        return tmp.name
    except Exception as e:
        logger.error(f"Failed to download image from {url}: {e}", exc_info=True)
        return None


class Tracker:
    def __init__(
        self,
        browser: UFCBrowser,
        poster: TweetPoster | None = None,
        test_mode: bool = False,
    ) -> None:
        self._browser = browser
        self._poster = poster
        self._test_mode = test_mode
        self._poster_ready = False
        self._post_lock = asyncio.Lock()
        logger.info(
            f"Tracker init — poster={'enabled' if poster else 'disabled'}, "
            f"test_mode={test_mode}"
        )

    async def _ensure_poster_ready(self) -> None:
        if not self._poster:
            return
        if not self._poster_ready:
            logger.info("Poster not yet initialised — running setup ...")
            await self._poster.setup()
            self._poster_ready = True
            logger.info("Poster setup complete.")
        else:
            logger.debug("Poster already ready — skipping setup.")

    async def _close_poster(self) -> None:
        """Close the poster browser. It will be re-opened on next tweet."""
        if self._poster and self._poster_ready:
            await self._poster.close()
            self._poster_ready = False
            logger.info("Poster browser closed after tweeting session.")

    async def _tweet_athlete(
        self,
        athlete: Athlete,
        *,
        removed: bool = False,
    ) -> None:
        """Download image (if available) and post tweet, then clean up temp file."""
        text = _format_removed_tweet(athlete) if removed else _format_tweet(athlete)
        image_path: str | None = None

        if athlete.image_url:
            image_path = await _download_image(athlete.image_url)
            if not image_path:
                logger.warning(
                    f"Image download failed for {athlete.name} — will tweet text only."
                )
        else:
            logger.debug(f"No image URL for {athlete.name} — tweeting text only.")

        logger.info("Acquiring post lock ...")
        async with self._post_lock:
            logger.info("Post lock acquired.")
            try:
                await self._ensure_poster_ready()
                logger.info(
                    f"Tweeting: {athlete.name} "
                    f"({'with image' if image_path else 'text only'})"
                )
                await self._poster.post_tweet(  # type: ignore[union-attr]
                    text=text,
                    media_path=image_path,
                )
                logger.info("Tweet posted successfully.")
            except Exception as e:
                logger.error(f"_tweet_athlete() failed for {athlete.name}: {e}", exc_info=True)
            finally:
                if image_path and os.path.exists(image_path):
                    os.remove(image_path)
                    logger.debug(f"Temp image file removed: {image_path}")
        logger.debug("Post lock released.")

    async def _post_test_tweet(self, db: aiosqlite.Connection) -> None:
        logger.info("TEST_MODE: posting random new-fighter tweet ...")
        athlete = await get_random_athlete(db)
        if not athlete:
            logger.warning("TEST_MODE: no active athletes in DB — skipping test tweet.")
        else:
            logger.info(f"TEST_MODE: tweeting random athlete → {athlete}")
            await self._tweet_athlete(athlete)

        logger.info("TEST_MODE: posting random removed-fighter tweet ...")
        removed = await get_random_removed_athlete(db) or await get_random_athlete(db)
        if not removed:
            logger.info("TEST_MODE: no athletes in DB yet — skipping removed test tweet.")
        else:
            logger.info(f"TEST_MODE: tweeting random removed athlete → {removed}")
            await self._tweet_athlete(removed, removed=True)

    async def _detect_and_handle_removed(
        self,
        db: aiosqlite.Connection,
        seen_urls: set[str],
        tweet: bool,
    ) -> int:
        """Compare seen URLs vs active DB URLs; mark missing ones as removed.

        When potential removals are found a second full scrape is performed to
        confirm them.  Only fighters absent from *both* scrapes are marked
        removed, preventing false positives caused by transient request
        failures in the first scrape.
        """
        active_urls = await get_all_active_profile_urls(db)
        candidate_urls = active_urls - seen_urls

        if not candidate_urls:
            logger.info("No removed athletes detected.")
            return 0

        logger.warning(
            f"{len(candidate_urls)} potentially removed athlete(s) detected — "
            f"running confirmation scrape before marking anyone as removed ..."
        )

        # --- Confirmation scrape ---
        # Re-capture a fresh browser session and re-scrape the full roster.
        # Only fighters absent from this second pass as well are truly removed.
        try:
            _, ajax_url_2, headers_2 = await self._browser.capture_session()
            confirmation_urls: set[str] = set()

            async def _collect_url(athlete: Athlete) -> None:
                if athlete.profile_url:
                    confirmation_urls.add(athlete.profile_url)

            scraper2 = Scraper(ajax_url_2, headers_2)
            await scraper2.run(_collect_url)

            if scraper2.had_failures:
                # Confirmation scrape itself was incomplete — cannot trust it.
                logger.warning(
                    "Confirmation scrape had request failures — "
                    "skipping removal detection this cycle to avoid false positives."
                )
                return 0

            false_positives = candidate_urls & confirmation_urls
            confirmed_removed = candidate_urls - confirmation_urls

            if false_positives:
                logger.info(
                    f"{len(false_positives)} athlete(s) were false positives "
                    f"(present in confirmation scrape) — not marking as removed."
                )

            if not confirmed_removed:
                logger.info("No confirmed removals after confirmation scrape.")
                return 0

            logger.info(
                f"{len(confirmed_removed)} athlete(s) confirmed removed "
                f"(absent from both scrapes)."
            )

        except Exception as e:
            # If the confirmation scrape itself fails entirely, play it safe
            # and skip removal detection for this cycle.
            logger.error(
                f"Confirmation scrape failed: {e} — "
                f"skipping removal detection this cycle.",
                exc_info=True,
            )
            return 0

        removed_athletes = await mark_athletes_removed(db, confirmed_removed)

        if tweet and self._poster and removed_athletes:
            logger.info(f"Tweeting {len(removed_athletes)} removed athlete(s) ...")
            for i, athlete in enumerate(removed_athletes, 1):
                logger.info(f"Tweeting removed {i}/{len(removed_athletes)}: {athlete.name}")
                await self._tweet_athlete(athlete, removed=True)

        return len(removed_athletes)

    async def _scrape_and_save(
        self,
        db: aiosqlite.Connection,
        ajax_url: str,
        headers: dict[str, str],
        tweet_new: bool,
        max_retries: int = 3,
    ) -> tuple[int, set[str]]:
        """Scrape all paginated pages and save new athletes.

        If any page requests fail the entire scrape is retried (up to
        *max_retries* times) with a freshly captured browser session, so that
        the seen-URL set is complete before it is used for removal detection.
        """
        logger.info(f"=== _scrape_and_save() start — tweet_new={tweet_new} ===")

        for attempt in range(1, max_retries + 1):
            new_athletes: list[Athlete] = []
            seen_urls: set[str] = set()

            async def on_athlete(athlete: Athlete) -> None:
                if athlete.profile_url:
                    seen_urls.add(athlete.profile_url)
                is_new = await save_athlete(db, athlete)
                if is_new:
                    logger.info(f"New athlete saved: {athlete}")
                    new_athletes.append(athlete)

            t0 = time.monotonic()
            scraper = Scraper(ajax_url, headers)
            await scraper.run(on_athlete)
            elapsed = time.monotonic() - t0

            if scraper.had_failures and attempt < max_retries:
                # Some pages were not fetched — the seen-URL set is incomplete
                # and could produce false removal detections.  Re-capture a
                # fresh session and retry the full scrape.
                logger.warning(
                    f"Scrape attempt {attempt}/{max_retries} had request failures "
                    f"({len(seen_urls)} URLs seen so far) — re-capturing session "
                    f"and retrying to ensure a complete dataset ..."
                )
                try:
                    _, ajax_url, headers = await self._browser.capture_session()
                except Exception as e:
                    logger.error(
                        f"Session re-capture for retry {attempt + 1} failed: {e}",
                        exc_info=True,
                    )
                    # Keep existing ajax_url/headers and try again anyway.
                continue

            if scraper.had_failures:
                logger.warning(
                    f"Scrape still had failures after {max_retries} attempt(s) — "
                    f"proceeding with partial data; removal detection will be "
                    f"skipped this cycle."
                )
            else:
                logger.info(
                    f"=== _scrape_and_save() complete — "
                    f"{len(new_athletes)} new athlete(s) in {elapsed:.1f}s ==="
                )
            break

        if tweet_new and self._poster and new_athletes:
            logger.info(f"Tweeting {len(new_athletes)} new athlete(s) ...")
            for i, athlete in enumerate(new_athletes, 1):
                logger.info(f"Tweeting {i}/{len(new_athletes)}: {athlete.name}")
                await self._tweet_athlete(athlete)
            if not self._test_mode:
                await self._close_poster()
        elif new_athletes and not tweet_new:
            logger.info(
                f"{len(new_athletes)} new athlete(s) saved — "
                f"tweeting suppressed (first run)."
            )

        # Return whether the scrape was fully successful so callers can decide
        # whether to trust the seen-URL set for removal detection.
        return len(new_athletes), seen_urls, not scraper.had_failures

    async def _save_initial_batch(
        self,
        db: aiosqlite.Connection,
        initial_athletes: list[Athlete],
    ) -> tuple[list[Athlete], set[str]]:
        new: list[Athlete] = []
        seen_urls: set[str] = set()
        for athlete in initial_athletes:
            if athlete.profile_url:
                seen_urls.add(athlete.profile_url)
            if await save_athlete(db, athlete):
                new.append(athlete)
        logger.info(
            f"Initial DOM batch: {len(new)}/{len(initial_athletes)} new athlete(s)."
        )
        return new, seen_urls

    async def _poll(
        self,
        db: aiosqlite.Connection,
        ajax_url: str,
        headers: dict[str, str],
    ) -> None:
        logger.info("=== _poll() start — re-capturing browser session ===")
        t0 = time.monotonic()

        initial_athletes, ajax_url, headers = await self._browser.capture_session()
        new_initial, seen_initial = await self._save_initial_batch(db, initial_athletes)

        new_count, seen_scraped, scrape_complete = await self._scrape_and_save(
            db, ajax_url, headers, tweet_new=True
        )
        seen_all = seen_initial | seen_scraped

        # Only run removal detection when the scrape was fully successful.
        # A partial scrape would produce an incomplete seen-URL set and could
        # falsely flag fighters as removed.
        if scrape_complete:
            removed_count = await self._detect_and_handle_removed(db, seen_all, tweet=True)
        else:
            logger.warning(
                "Scrape was incomplete after all retries — "
                "skipping removal detection this cycle."
            )
            removed_count = 0

        total_new = new_count + len(new_initial)
        elapsed = time.monotonic() - t0
        logger.info(
            f"=== _poll() complete — {total_new} new, {removed_count} removed "
            f"in {elapsed:.1f}s ==="
        )

        if self._poster and new_initial:
            logger.info(f"Tweeting {len(new_initial)} from initial DOM batch ...")
            for i, athlete in enumerate(new_initial, 1):
                logger.info(f"Tweeting initial {i}/{len(new_initial)}: {athlete.name}")
                await self._tweet_athlete(athlete)

        if self._poster and (new_initial or removed_count) and not self._test_mode:
            await self._close_poster()

        if self._test_mode and self._poster:
            await self._post_test_tweet(db)
            await self._close_poster()

    async def run(self) -> None:
        logger.info("=== Tracker.run() start ===")
        async with aiosqlite.connect(DB_PATH) as db:
            await init_db(db)
            logger.info("Database initialised.")

            count = await get_athlete_count(db)
            is_first_run = count == 0

            if is_first_run:
                logger.info("First run — caching all athletes without posting tweets.")
            else:
                logger.info(f"DB has {count} athletes — polling for new ones.")

            logger.info("--- Phase 1: Capture browser session ---")
            initial_athletes, ajax_url, headers = await self._browser.capture_session()

            new_initial, seen_initial = await self._save_initial_batch(db, initial_athletes)

            logger.info("--- Phase 2: Full scrape ---")
            new_count, seen_scraped, scrape_complete = await self._scrape_and_save(
                db, ajax_url, headers, tweet_new=not is_first_run
            )
            seen_all = seen_initial | seen_scraped

            if not is_first_run:
                if scrape_complete:
                    await self._detect_and_handle_removed(db, seen_all, tweet=True)
                else:
                    logger.warning(
                        "Initial scrape was incomplete after all retries — "
                        "skipping removal detection."
                    )

                if self._poster and new_initial:
                    logger.info(
                        f"Tweeting {len(new_initial)} new athlete(s) from initial DOM batch ..."
                    )
                    for athlete in new_initial:
                        await self._tweet_athlete(athlete)

            if is_first_run:
                logger.info("Initial cache complete.")

            if self._test_mode and self._poster:
                logger.info("TEST_MODE active — posting test tweet after initial run ...")
                await self._post_test_tweet(db)
                await self._close_poster()

            logger.info("--- Entering polling loop ---")
            poll_count = 0
            while True:
                logger.info(
                    f"Sleeping {POLL_INTERVAL}s before next poll "
                    f"(poll #{poll_count + 1} next) ..."
                )
                await asyncio.sleep(POLL_INTERVAL)
                poll_count += 1
                logger.info(f"--- Poll #{poll_count} start ---")
                try:
                    await self._poll(db, ajax_url, headers)
                except Exception as e:
                    logger.error(
                        f"Poll #{poll_count} failed: {e}", exc_info=True
                    )
                    logger.info("Will retry on next poll interval.")
