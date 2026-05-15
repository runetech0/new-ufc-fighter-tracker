import copy
import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Any

import colorama

colorama.init()


VIRCHUAL = os.getenv("VIRCHUAL", False)

LOGS_FILENAME = "logs.log"


class NoExceptionStreamHandler(logging.StreamHandler[Any]):
    def emit(self, record: logging.LogRecord) -> Any:
        record = copy.copy(record)
        # Clear exc_info to disable exception output for this handler only
        record.exc_info = None
        super().emit(record)


class LevelBasedFormatter(logging.Formatter):
    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        style: str = "%",
        level_formats: dict[int, str] | None = None,
    ) -> None:
        super().__init__(fmt, datefmt, style)  # type: ignore
        # If no level_formats are provided, initialize with an empty dict
        self.level_formats: dict[int, str] = level_formats or {}
        self.colors = {
            logging.DEBUG: colorama.Fore.LIGHTWHITE_EX,
            logging.INFO: colorama.Fore.GREEN + colorama.Style.BRIGHT,
            logging.WARNING: colorama.Fore.YELLOW + colorama.Style.BRIGHT,
            logging.ERROR: colorama.Fore.RED + colorama.Style.BRIGHT,
            logging.CRITICAL: colorama.Fore.WHITE
            + colorama.Style.BRIGHT
            + colorama.Back.RED,
        }
        self.color_reset = colorama.Style.RESET_ALL

    def format(self, record: logging.LogRecord) -> str:
        # Check if there's a format for this level; use it if it exists
        log_fmt = self.level_formats.get(record.levelno, self._fmt)
        formatter = logging.Formatter(log_fmt)

        # Apply color to the message based on log level
        formatted_message = formatter.format(record)
        color_code = self.colors.get(record.levelno, "")
        return f"{color_code}{formatted_message}{self.color_reset}"


def get_logger() -> logging.Logger:
    max_filesize_in_mbs = 2_000
    file_encoding = "UTF-8"

    logger = logging.Logger(__name__)

    level_formats: dict[int, str] = {
        logging.DEBUG: "[.] %(message)s",
        logging.INFO: "[+] %(message)s",
        logging.WARNING: "[*] %(message)s",
        logging.ERROR: "[-] %(message)s",
        logging.CRITICAL: "[!] %(message)s",
    }

    level_based_fmt = LevelBasedFormatter(
        "[+] %(message)s", level_formats=level_formats
    )
    console_handler = NoExceptionStreamHandler()
    console_handler.setFormatter(level_based_fmt)

    file_handler = RotatingFileHandler(
        LOGS_FILENAME,
        mode="a",
        maxBytes=max_filesize_in_mbs * 1024 * 1024,
        backupCount=2,
        encoding=file_encoding,
        delay=False,
    )
    file_logging_fmt = logging.Formatter(
        "[%(asctime)s - %(levelname)-8s - %(filename)s - %(lineno)-4s - %(message)s"
    )
    file_logging_fmt = logging.Formatter(
        "%(asctime)s - %(levelname)-8s - [%(filename)s:%(lineno)s] - %(message)s"
    )
    file_handler.setFormatter(file_logging_fmt)

    # Log level of logger should always be DEBUG
    logger.setLevel(logging.DEBUG)
    file_handler.setLevel(logging.DEBUG)
    console_handler.setLevel(logging.INFO)

    # Add handlers to the logger
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger
