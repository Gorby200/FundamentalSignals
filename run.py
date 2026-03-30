"""
run.py — Entry point for FundamentalSignals ORACLE v2.1 backend.

Usage:
    python run.py

This script:
  1. Initializes centralized logging (logs/oracle.log + logs/llm_feed.log)
  2. Imports the FastAPI app from app.main
  3. Starts uvicorn with the configured host/port
"""

import uvicorn

from app.logging_config import setup_logging

setup_logging()

from app import config as app_config

import logging
logger = logging.getLogger("fundamentalsignals")

host = app_config.get("server.host", "0.0.0.0")
port = app_config.get("server.port", 8000)

logger.info(f"Starting ORACLE v2.1 on {host}:{port}")

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        log_level="info",
        ws_ping_interval=20,
        ws_ping_timeout=10,
    )
