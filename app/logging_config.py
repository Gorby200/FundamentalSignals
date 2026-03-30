"""
app/logging_config.py — Centralized logging setup for FundamentalSignals.

Reads logging configuration from config/settings.json and configures:
  - Root logger with console handler (optional)
  - RotatingFileHandler for logs/oracle.log (optional)
  - Separate LLM feed logger writing to logs/llm_feed.log (optional)
  - Suppresses noisy third-party loggers (httpx, websocket, urllib3, httpcore)

Log levels (standard Python):
  DEBUG    — everything, including internal state dumps
  INFO     — normal operation, signal generation, RSS polls
  WARNING  — degraded operation, LLM failures, feed errors
  ERROR    — critical failures

Best practice: set level in settings.json, restart to apply.
"""

import logging
import logging.handlers
from pathlib import Path
from typing import Any, Dict

from app import config as app_config

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOGS_DIR = _PROJECT_ROOT / "logs"

_SETUP_DONE = False


def setup_logging() -> None:
    """
    Initialize logging subsystem. Call once at startup before anything else.
    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _SETUP_DONE
    if _SETUP_DONE:
        return
    _SETUP_DONE = True

    level_name = app_config.get("logging.level", "INFO")
    log_to_file = app_config.get("logging.log_to_file", True)
    log_to_console = app_config.get("logging.log_to_console", True)
    llm_feed_enabled = app_config.get("logging.llm_feed_enabled", True)
    max_size_mb = app_config.get("logging.max_file_size_mb", 10)
    backup_count = app_config.get("logging.backup_count", 5)

    level = getattr(logging, level_name.upper(), logging.INFO)

    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (_LOGS_DIR / ".gitkeep").touch(exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    detailed_formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger("fundamentalsignals")
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    if log_to_console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    if log_to_file:
        file_handler = logging.handlers.RotatingFileHandler(
            _LOGS_DIR / "oracle.log",
            maxBytes=max_size_mb * 1024 * 1024,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(detailed_formatter)
        root_logger.addHandler(file_handler)

    if llm_feed_enabled:
        llm_logger = logging.getLogger("fundamentalsignals.llm_feed")
        llm_logger.setLevel(logging.DEBUG)
        llm_logger.handlers.clear()
        llm_logger.propagate = False

        llm_handler = logging.handlers.RotatingFileHandler(
            _LOGS_DIR / "llm_feed.log",
            maxBytes=max_size_mb * 1024 * 1024,
            backupCount=backup_count,
            encoding="utf-8",
        )
        llm_handler.setLevel(logging.DEBUG)
        llm_handler.setFormatter(detailed_formatter)
        llm_logger.addHandler(llm_handler)

    for noisy in ("httpx", "httpcore", "websocket", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root_logger.info(
        f"Logging initialized: level={level_name}, "
        f"file={'ON' if log_to_file else 'OFF'}, "
        f"console={'ON' if log_to_console else 'OFF'}, "
        f"llm_feed={'ON' if llm_feed_enabled else 'OFF'}"
    )


def get_llm_feed_logger() -> logging.Logger:
    return logging.getLogger("fundamentalsignals.llm_feed")
