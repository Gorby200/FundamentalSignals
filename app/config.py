"""
app/config.py — Centralized configuration loader for FundamentalSignals.

Reads settings.json from config/ directory and provides a typed
access layer for all subsystems (z.ai API, RSS feeds, signal params, etc.).

Why a JSON config file instead of env vars?
  - Investor demo: single file is easier to hand off and audit.
  - Nested structure maps cleanly to subsystem boundaries.
  - No .env dependency — keeps the prototype self-contained.
"""

import json
import os
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("fundamentalsignals.config")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config" / "settings.json"

_settings: Dict[str, Any] = {}


def _load() -> Dict[str, Any]:
    global _settings
    if not _settings:
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                _settings = json.load(f)
            logger.info(f"Configuration loaded from {_CONFIG_PATH}")
        else:
            logger.warning(f"Config file not found: {_CONFIG_PATH}, using defaults")
            _settings = {}
    return _settings


def get(path: str, default: Any = None) -> Any:
    """
    Dot-notation config getter.
    Example: get("zai.api_key")  -> "sk-..."
    Example: get("server.port", 8000)  -> 8000
    """
    data = _load()
    keys = path.split(".")
    for key in keys:
        if isinstance(data, dict) and key in data:
            data = data[key]
        else:
            return default
    return data


def get_all() -> Dict[str, Any]:
    return _load()


def get_zai_config() -> Dict[str, Any]:
    return {
        "api_key": get("zai.api_key", ""),
        "base_url": get("zai.base_url", "https://api.z.ai/api/coding/paas/v4"),
        "model": get("zai.model", "glm-5"),
        "max_tokens": get("zai.max_tokens", 4096),
        "temperature": get("zai.temperature", 0.6),
        "timeout": get("zai.timeout", 30),
    }


def is_llm_enabled() -> bool:
    key = get("zai.api_key", "")
    return bool(key and key != "YOUR_ZAI_API_KEY_HERE")
