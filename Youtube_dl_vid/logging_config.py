import inspect
import logging
import os
import sys
from pathlib import Path

from loguru import logger


class InterceptHandler(logging.Handler):
    """
    Intercept standard library logging records and forward them to Loguru.

    Based on the official Loguru recipe for integrating with the standard
    logging library while preserving caller file, function, and line info.
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding Loguru level if it exists.
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller frame to preserve file/function/line info.
        frame, depth = inspect.currentframe(), 0
        while frame:
            filename = frame.f_code.co_filename
            is_logging = filename == logging.__file__
            is_frozen = "importlib" in filename and "_bootstrap" in filename
            if depth > 0 and not (is_logging or is_frozen):
                break
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging(log_dir: str = "/var/log/gunicorn") -> None:
    """
    Configure Loguru sinks and prepare stdlib log interception.

    Sinks:
    - stdout: human-readable, DEBUG+
    - app.log: JSON, INFO+, 30-day retention
    - error.log: JSON, ERROR+, 90-day retention
    - debug.log: JSON, DEBUG+, 7-day retention
    """
    logger.remove()

    # Ensure log directory exists.
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    # Console sink: human-readable for local/dev visibility.
    logger.add(
        sys.stdout,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "{name}:{function}:{line} | {message}"
        ),
        level="DEBUG",
        colorize=False,
        enqueue=True,
    )

    # File sinks: JSON serialized for structured log aggregation.
    logger.add(
        os.path.join(log_dir, "app.log"),
        rotation="10 MB",
        retention="30 days",
        level="INFO",
        serialize=True,
        enqueue=True,
    )

    logger.add(
        os.path.join(log_dir, "error.log"),
        rotation="10 MB",
        retention="90 days",
        level="ERROR",
        serialize=True,
        enqueue=True,
    )

    logger.add(
        os.path.join(log_dir, "debug.log"),
        rotation="10 MB",
        retention="7 days",
        level="DEBUG",
        serialize=True,
        enqueue=True,
    )


setup_logging()
