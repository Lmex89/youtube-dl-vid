from loguru import logger
import sys

logger.remove()  # Remove default handler

# Send logs to stdout so Gunicorn captures them
logger.add(sys.stdout, colorize=True, format="<green>{time}</green> | <level>{level}</level> | {message}", level="INFO")
logger.add("/var/log/gunicorn/app.log", rotation="10 MB", retention="7 days", level="INFO")