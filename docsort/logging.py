from __future__ import annotations

import sys

from loguru import logger

from docsort.models import AppConfig


def configure_logging(config: AppConfig) -> None:
    """Configure console and optional file logging once per process."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=config.logging.level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {message}",
    )

    if config.logging.file_path is not None:
        config.logging.file_path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            config.logging.file_path,
            level=config.logging.level,
            rotation=config.logging.rotation,
            retention=config.logging.retention,
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )

    if config.logging.error_file_path is not None:
        config.logging.error_file_path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            config.logging.error_file_path,
            level="ERROR",
            rotation=config.logging.rotation,
            retention=config.logging.retention,
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )
