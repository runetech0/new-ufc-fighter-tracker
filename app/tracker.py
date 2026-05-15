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
        "✅ New Fighter Added!",
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
        "❌ Fighter Removed from Roster!",
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
        removed = await get_random_removed_athlete(db)
        if not removed:
            logger.info("TEST_MODE: no removed athletes in DB yet — skipping removed test tweet.")
        else:
            logger.info(f"TEST_MODE: tweeting random removed athlete → {removed}")
            await self._tweet_athlete(removed, removed=True)

    async def _detect_and_handle_removed(
        self,
        db: aiosqlite.Connection,
        seen_urls: set[str],
        tweet: bool,
    ) -> int:
        """Compare seen URLs vs active DB URLs; mark missing ones as removed."""
        active_urls = await get_all_active_profile_urls(db)
        removed_urls = active_urls - seen_urls
        if not removed_urls:
            logger.info("No removed athletes detected.")
            return 0

        logger.info(f"{len(removed_urls)} athlete(s) appear to have been removed.")
        removed_athletes = await mark_athletes_removed(db, removed_urls)

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
    ) -> tuple[int, set[str]]:
        logger.info(f"=== _scrape_and_save() start — tweet_new={tweet_new} ===")
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
        await Scraper(ajax_url, headers).run(on_athlete)
        elapsed = time.monotonic() - t0

        logger.info(
            f"=== _scrape_and_save() complete — "
            f"{len(new_athletes)} new athlete(s) in {elapsed:.1f}s ==="
        )

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

        return len(new_athletes), seen_urls

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

        new_count, seen_scraped = await self._scrape_and_save(db, ajax_url, headers, tweet_new=True)
        seen_all = seen_initial | seen_scraped

        removed_count = await self._detect_and_handle_removed(db, seen_all, tweet=True)

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
            new_count, seen_scraped = await self._scrape_and_save(
                db, ajax_url, headers, tweet_new=not is_first_run
            )
            seen_all = seen_initial | seen_scraped

            if not is_first_run:
                await self._detect_and_handle_removed(db, seen_all, tweet=True)

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
