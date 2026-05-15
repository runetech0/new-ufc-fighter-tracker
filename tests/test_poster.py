import asyncio

from app.logs_config import get_logger
from app.poster import TweetPoster

from .config import AUTH_TOKEN

logger = get_logger()


async def main() -> None:
    auth_token = AUTH_TOKEN
    screenshots_dir = "screenshots"
    media_path = "tests/media/test.png"
    poster = TweetPoster(auth_token, screenshots_dir, logger=logger)

    await poster.setup()

    await poster.post_tweet(text="Hello, world!", media_path=media_path)

    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
