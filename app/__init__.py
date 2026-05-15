import os
import time

from . import metadata
from .logs_config import get_logger

logger = get_logger()
logger.info("<" + "-" * 50 + ">")
print("\n")
logger.info(f"App Name => {metadata.NAME}")
logger.info(f"Version Number: {metadata.VERSION}")
logger.info(f"Developer Contact => {metadata.CONTACT_INFO}")
print("\n")
logger.info("<" + "-" * 50 + ">")
if not os.environ.get("VIRCHUAL", False):
    time.sleep(3)
