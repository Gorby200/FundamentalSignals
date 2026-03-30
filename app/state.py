"""
app/state.py — Application state container.

Thread-safe state for the FundamentalSignals ORACLE v2.1 system.
Stores news articles, tiered signals (A/B/C), price data, oracle metadata,
and websocket connection pool.

v2.1 changes from v2.0:
  - active_signals now holds ALL signals (tier A + deterministic), flattened for broadcast
  - oracle_meta includes tier_b_baskets, tier_c_universe, signals_suppressed
  - oracle_version bumped to "2.1"
"""

import threading
from collections import deque
from typing import Any, Dict


class AppState:
    def __init__(self):
        self.news_articles: deque = deque(maxlen=200)

        # Flat list of all active signals (tier A from ORACLE + deterministic fallbacks)
        # Used for backward-compatible broadcast. Tier B/C live in oracle_meta.
        self.active_signals: deque = deque(maxlen=50)

        self.price_cache: Dict[str, Any] = {}
        self.websocket_clients: set = set()
        self.seen_news_slugs: set = set()
        self.recent_signal_keys: deque = deque(maxlen=200)

        self.stats: Dict[str, Any] = {
            "articles_processed": 0,
            "signals_generated": 0,
            "signals_broadcast": 0,
            "rss_polls": 0,
            "llm_calls": 0,
            "llm_errors": 0,
            "llm_last_success": None,
            "started_at": None,
        }

        # ORACLE v2.1 metadata — updated every signal generation cycle
        self.oracle_meta: Dict[str, Any] = {
            "regime": {},
            "news_assessment": {},
            "market_outlook": "",
            "risk_warnings": [],
            "hedge_recommendations": [],
            "watchlist": [],
            "correlation_chains_traced": [],
            "signals_suppressed": [],
            # v2.1 tiered structures
            "tier_b_baskets": [],
            "tier_c_universe": [],
            "meta": {},
            # Timestamps and version
            "last_oracle_timestamp": None,
            "oracle_version": "2.1",
        }

        self.lock = threading.Lock()

        # ORACLE news queue — track which articles have been sent to LLM
        # so the same articles are not re-analyzed on every cycle
        self.oracle_processed_hashes: set = set()
        self.oracle_batch_counter: int = 0
