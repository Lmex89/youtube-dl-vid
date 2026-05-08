from loguru import logger
import sys
import json

logger.remove()

# Console output - human readable
logger.add(
    sys.stdout,
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
    level="DEBUG",
    colorize=False
)

# File output - JSON format using serialize parameter
logger.add(
    "/var/log/gunicorn/app.log",
    rotation="10 MB",
    retention="30 days",
    level="INFO",
    serialize=True
)