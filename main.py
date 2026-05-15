import asyncio

from pyvirtualdisplay.display import Display

from app.browser import UFCBrowser
from app.config_reader import Config
from app.logs_config import get_logger
from app.poster import TweetPoster
from app.tracker import Tracker

display = Display(visible=False, size=(1920, 1080))
display.start()

logger = get_logger()


async def main() -> None:
    browser = UFCBrowser(
        headless=Config.BROWSER.HEADLESS,
        page_load_timeout_seconds=Config.BROWSER.PAGE_LOAD_TIMEOUT_SECONDS,
    )

    poster: TweetPoster | None = None
    if Config.TWITTER.AUTH_TOKEN:
        poster = TweetPoster(
            Config.TWITTER.AUTH_TOKEN,
            Config.TWITTER.SCREENSHOTS_DIR,
            logger=logger,
        )

    if poster:
        await Tracker(browser, poster, test_mode=Config.TWITTER.TEST_MODE).run()
    else:
        logger.warning("No Twitter auth token set — running without tweet posting.")
        await Tracker(browser).run()


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except Exception as e:
        logger.error(f"ERROR: {e}", exc_info=True)

    finally:
        logger.info("Bot exiting... If you need any help get on Telegram || @runetech")
        input("Press 'Enter' to close the bot <- ")
        input("Press 'Enter' 2 times more ... <- ")
        input("Press 'Enter' one more time to complete exit <- ")
