from dataclasses import dataclass
from typing import ClassVar

import toml
import os, sys

_CONFIG_FILE = 'config.toml'

if not os.path.exists(_CONFIG_FILE):
    print(
        """❌ config.toml file is missing.
          👉 Please rename 'sample-config.toml' to 'config.toml'
          or ask the developer to send you 'sample-config.toml' file."""
    )
    input("Press 'Enter' 3-times to exit...")
    input("Press 'Enter' 2-times more...")
    input("Press 'Enter' to exit now...")
    sys.exit(1)

_CONFIG_DATA = toml.load(_CONFIG_FILE)


@dataclass
class BrowserConfig:
    HEADLESS: bool = True
    PAGE_LOAD_TIMEOUT_SECONDS: int = 30


@dataclass
class TwitterConfig:
    AUTH_TOKEN: str = ""
    SCREENSHOTS_DIR: str = "screenshots"
    TEST_MODE: bool = False


class Config:
    BROWSER: ClassVar[BrowserConfig] = BrowserConfig()
    TWITTER: ClassVar[TwitterConfig] = TwitterConfig()

    @classmethod
    def load(cls) -> None:
        cls.BROWSER = BrowserConfig(
            HEADLESS=_CONFIG_DATA['BROWSER']['HEADLESS'],
            PAGE_LOAD_TIMEOUT_SECONDS=_CONFIG_DATA['BROWSER']['PAGE_LOAD_TIMEOUT_SECONDS'],
        )
        cls.TWITTER = TwitterConfig(
            AUTH_TOKEN=_CONFIG_DATA['TWITTER']['AUTH_TOKEN'],
            SCREENSHOTS_DIR=_CONFIG_DATA['TWITTER']['SCREENSHOTS_DIR'],
            TEST_MODE=_CONFIG_DATA['TWITTER']['TEST_MODE'],
        )


Config.load()
