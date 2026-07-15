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
    get_active_statuses,
    get_athlete_count,
    get_random_athlete,
    get_random_removed_athlete,
    get_status_counts,
    init_db,
    mark_athletes_removed,
    save_athlete,
    update_fighter_status,
)
from .models import Athlete
from .poster import TweetPoster
from .scraper import Scraper
from .status_checker import INACTIVE_STATUSES, STATUS_ACTIVE, fetch_statuses

logger = get_logger()

POLL_INTERVAL = 1800  # 30 minutes


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
        logger.info(
            f"Image downloaded to temp file: {tmp.name} ({len(response.content)} bytes)"
        )
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
                logger.error(
                    f"_tweet_athlete() failed for {athlete.name}: {e}", exc_info=True
                )
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
            logger.info(
                "TEST_MODE: no athletes in DB yet — skipping removed test tweet."
            )
        else:
            logger.info(f"TEST_MODE: tweeting random removed athlete → {removed}")
            await self._tweet_athlete(removed, removed=True)

    async def _detect_status_changes(
        self,
        db: aiosqlite.Connection,
        tweet: bool,
    ) -> tuple[int, int]:
        """Return (removed_count, rebaselined_count)."""
        """Detect UFC roster changes by comparing live profile-page status against DB.

        Flow:
        1. Load {profile_url: fighter_status} for every is_active=1 athlete from DB.
        2. Fetch the live status badge from each fighter's UFC profile page.
        3. For every URL that was successfully fetched, compare live vs DB status.
           - DB='Active', live='Not Fighting'  → fighter was cut/released → mark removed + tweet.
           - Status unchanged → no-op.
           - Any other live status → update DB only.
           - Failed fetches are skipped entirely to avoid false positives.

        tweet=False on the very first run so we don't spam removals for fighters
        who were already 'Not Fighting' before this bot was deployed.
        """
        db_statuses: dict[str, str] = await get_active_statuses(db)
        if not db_statuses:
            logger.info("Status change detection — no active athletes in DB.")
            return 0, 0

        logger.info(
            f"Status change detection — {len(db_statuses)} active athlete(s) in DB, "
            f"fetching live profile statuses ..."
        )

        live_statuses, had_failures = await fetch_statuses(set(db_statuses.keys()))

        if had_failures:
            logger.warning(
                f"Status check had fetch failures — "
                f"{len(live_statuses)}/{len(db_statuses)} profiles fetched successfully. "
                f"Only comparing athletes with a successful fetch."
            )

        newly_removed: set[str] = set()
        rebaselined = 0

        for url, live_status in live_statuses.items():
            # Always compare lowercase — guards against stale mixed-case DB values.
            db_status = db_statuses.get(url, STATUS_ACTIVE).lower()

            if live_status == db_status:
                continue

            logger.info(
                f"Status changed: {url} — DB='{db_status}' → live='{live_status}'"
            )

            if live_status in INACTIVE_STATUSES and db_status == STATUS_ACTIVE:
                newly_removed.add(url)
            else:
                # Any other transition: just persist the new value.
                await update_fighter_status(db, url, live_status)

        if not newly_removed:
            logger.info("Status change detection — no newly released fighters detected.")
            return 0, rebaselined

        logger.info(
            f"{len(newly_removed)} fighter(s) newly changed to 'Not Fighting' — "
            f"marking as removed ..."
        )
        removed_athletes = await mark_athletes_removed(db, newly_removed)

        if tweet and self._poster and removed_athletes:
            logger.info(
                f"Tweeting {len(removed_athletes)} status-changed removed athlete(s) ..."
            )
            for i, athlete in enumerate(removed_athletes, 1):
                logger.info(
                    f"Tweeting status-removed {i}/{len(removed_athletes)}: {athlete.name}"
                )
                await self._tweet_athlete(athlete, removed=True)

        return len(removed_athletes), rebaselined

    async def _filter_active_new_athletes(
        self,
        db: aiosqlite.Connection,
        new_athletes: list[Athlete],
    ) -> list[Athlete]:
        """Check new athletes' live profile status before tweeting them as 'added'.

        Returns only those whose status is 'Active'.  Any that are already
        'Not Fighting' are silently marked removed in the DB — they were cut
        before this bot started tracking them and should not produce an 'Added'
        tweet.  Fighters whose profile fetch failed are assumed active so we
        don't silently drop genuine new signings.
        """
        if not new_athletes:
            return []

        urls = {a.profile_url for a in new_athletes if a.profile_url}
        if not urls:
            return new_athletes

        live_statuses, _ = await fetch_statuses(urls)

        active_athletes: list[Athlete] = []
        already_cut: set[str] = set()

        for athlete in new_athletes:
            live = live_statuses.get(athlete.profile_url) if athlete.profile_url else None
            if live in INACTIVE_STATUSES:
                logger.info(
                    f"New athlete {athlete.name} is already 'Not Fighting' — "
                    f"saving silently, no tweet."
                )
                if athlete.profile_url:
                    already_cut.add(athlete.profile_url)
            else:
                # Active or fetch failed — include for tweeting.
                active_athletes.append(athlete)

        if already_cut:
            await mark_athletes_removed(db, already_cut)

        return active_athletes

    async def _scrape_and_save(
        self,
        db: aiosqlite.Connection,
        ajax_url: str,
        headers: dict[str, str],
        max_retries: int = 3,
    ) -> tuple[list[Athlete], set[str], bool]:
        """Scrape all paginated pages, save new athletes, and return them.

        Tweeting is intentionally NOT done here — the caller decides what to
        tweet after applying any additional filters (e.g. status check).

        Returns (new_athletes, seen_urls, scrape_complete).
        new_athletes is accumulated outside the retry loop so athletes already
        saved in an early attempt aren't lost when seen_urls resets on retry.
        """
        logger.info("=== _scrape_and_save() start ===")

        new_athletes: list[Athlete] = []
        seen_urls: set[str] = set()
        scraper: Scraper | None = None

        for attempt in range(1, max_retries + 1):
            seen_urls = set()

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
                logger.warning(
                    f"Scrape attempt {attempt}/{max_retries} had request failures "
                    f"({len(seen_urls)} URLs seen, {len(new_athletes)} new so far) — "
                    f"re-capturing session and retrying ..."
                )
                try:
                    _, ajax_url, headers = await self._browser.capture_session()
                except Exception as e:
                    logger.error(
                        f"Session re-capture for retry {attempt + 1} failed: {e}",
                        exc_info=True,
                    )
                continue

            if scraper.had_failures:
                logger.warning(
                    f"Scrape still had failures after {max_retries} attempt(s) — "
                    f"proceeding with partial data."
                )
            else:
                logger.info(
                    f"=== _scrape_and_save() complete — "
                    f"{len(new_athletes)} new athlete(s) in {elapsed:.1f}s ==="
                )
            break

        scrape_complete = scraper is not None and not scraper.had_failures
        return new_athletes, seen_urls, scrape_complete

    async def _save_initial_batch(
        self,
        db: aiosqlite.Connection,
        initial_athletes: list[Athlete],
    ) -> list[Athlete]:
        """Save the initial DOM batch; returns new (unseen) athletes."""
        new: list[Athlete] = []
        for athlete in initial_athletes:
            if await save_athlete(db, athlete):
                new.append(athlete)
        logger.info(
            f"Initial DOM batch: {len(new)}/{len(initial_athletes)} new athlete(s)."
        )
        return new

    async def _poll(
        self,
        db: aiosqlite.Connection,
    ) -> None:
        """Single poll cycle: re-capture session, scrape, detect status changes."""
        logger.info("=== _poll() start ===")
        t0 = time.monotonic()

        initial_athletes, ajax_url, headers = await self._browser.capture_session()
        new_initial = await self._save_initial_batch(db, initial_athletes)

        new_scraped, _, _ = await self._scrape_and_save(db, ajax_url, headers)

        # Combine all new athletes found this cycle, then verify their live
        # status before tweeting — avoids 'Fighter Added' for someone already cut.
        all_new = new_initial + new_scraped
        if all_new:
            tweetable_new = await self._filter_active_new_athletes(db, all_new)
            if tweetable_new and self._poster:
                logger.info(f"Tweeting {len(tweetable_new)} new active athlete(s) ...")
                for i, athlete in enumerate(tweetable_new, 1):
                    logger.info(f"Tweeting new {i}/{len(tweetable_new)}: {athlete.name}")
                    await self._tweet_athlete(athlete)
        else:
            tweetable_new = []

        # Status-based removal: primary mechanism for detecting roster cuts.
        removed_count, rebaselined_count = await self._detect_status_changes(db, tweet=True)

        elapsed = time.monotonic() - t0
        status_counts = await get_status_counts(db)

        active_n      = status_counts.get("active", 0)
        not_fighting_n = status_counts.get("not fighting", 0)
        retired_n     = status_counts.get("retired", 0)
        unset_n       = status_counts.get("unset", 0)
        total_n       = sum(status_counts.values())

        logger.info(
            f"\n"
            f"{'='*55}\n"
            f"  Poll cycle summary  ({elapsed:.1f}s)\n"
            f"{'='*55}\n"
            f"  DB totals  : {total_n} athletes\n"
            f"               active={active_n} | not fighting={not_fighting_n} "
            f"| retired={retired_n} | unset={unset_n}\n"
            f"  This cycle : {len(all_new)} found | {len(tweetable_new)} new tweeted "
            f"| {removed_count} removed tweeted | {rebaselined_count} re-baselined\n"
            f"{'='*55}"
        )

        if self._poster and (tweetable_new or removed_count) and not self._test_mode:
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
                logger.info("First run — caching all athletes, no tweets this cycle.")
            else:
                logger.info(f"DB has {count} active athletes — checking for changes.")

            logger.info("--- Phase 1: Capture browser session ---")
            initial_athletes, ajax_url, headers = await self._browser.capture_session()
            new_initial = await self._save_initial_batch(db, initial_athletes)

            logger.info("--- Phase 2: Full scrape ---")
            new_scraped, _, _ = await self._scrape_and_save(db, ajax_url, headers)

            if is_first_run:
                logger.info("Initial cache complete.")
                # Silently baseline all 'Not Fighting' fighters so the very first
                # polling cycle only tweets genuine new status changes.
                logger.info("First run — running silent status check to baseline DB ...")
                await self._detect_status_changes(db, tweet=False)
            else:
                # Not a fresh DB — check new athletes' status before tweeting,
                # then detect status changes for existing active athletes.
                all_new = new_initial + new_scraped
                tweetable_new: list[Athlete] = []
                if all_new:
                    tweetable_new = await self._filter_active_new_athletes(db, all_new)
                    if tweetable_new and self._poster:
                        logger.info(
                            f"Tweeting {len(tweetable_new)} new active athlete(s) ..."
                        )
                        for athlete in tweetable_new:
                            await self._tweet_athlete(athlete)

                removed_count, rebaselined_count = await self._detect_status_changes(db, tweet=True)
                status_counts = await get_status_counts(db)
                active_n       = status_counts.get("active", 0)
                not_fighting_n = status_counts.get("not fighting", 0)
                retired_n      = status_counts.get("retired", 0)
                unset_n        = status_counts.get("unset", 0)
                total_n        = sum(status_counts.values())
                logger.info(
                    f"\n"
                    f"{'='*55}\n"
                    f"  Initial run summary\n"
                    f"{'='*55}\n"
                    f"  DB totals  : {total_n} athletes\n"
                    f"               active={active_n} | not fighting={not_fighting_n} "
                    f"| retired={retired_n} | unset={unset_n}\n"
                    f"  This cycle : {len(all_new)} found | {len(tweetable_new)} new tweeted "
                    f"| {removed_count} removed tweeted | {rebaselined_count} re-baselined\n"
                    f"{'='*55}"
                )

                if self._poster and not self._test_mode:
                    await self._close_poster()

            if self._test_mode and self._poster:
                logger.info("TEST_MODE active — posting test tweet ...")
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
                    await self._poll(db)
                except Exception as e:
                    logger.error(f"Poll #{poll_count} failed: {e}", exc_info=True)
                    logger.info("Will retry on next poll interval.")
