"""
app/engines/signal_engine.py — Signal Engine (ORACLE v2.1 orchestrator).

Two-layer architecture (institutional-grade signal pipeline):

  LAYER 1 — DETERMINISTIC (fast, cheap, every 30s):
    1. TECHNICAL ANALYSIS   — SMA/RSI/MACD/BB/ATR on Binance price data
    2. NEWS SENTIMENT       — Ticker extraction + sentiment scoring per article
    3. COMMODITY CORRELATION — Propagate commodity sentiment via correlation matrix
    4. DETERMINISTIC MERGE  — Combine TA + news + commodity into base signals

  LAYER 2 — ORACLE AI (slow, expensive, every 10 min):
    5. ORACLE LLM ANALYSIS  — z.ai GLM-4.5-air via LangGraph ORACLE v2.1 agent
       Receives ALL accumulated news + TA + commodity context as one batch.
       Produces three-tier output: tier_a_signals, tier_b_baskets, tier_c_universe.

  Merge strategy:
    - Deterministic signals are shown immediately (Tier A/B based on confidence)
    - ORACLE signals override deterministic when available (higher quality)
    - Tier C deterministic signals are suppressed from active list
    - ORACLE tier_b_baskets and tier_c_universe stored in oracle_meta (not active_signals)

  This separation prevents rate-limit issues (LLM called infrequently)
  while maintaining fast deterministic signal updates.
"""

import asyncio
import hashlib
import logging
import time
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app import config as app_config
from app.engines.technical import generate_technical_signal
from app.engines.sentiment import extract_tickers, score_sentiment
from app.engines.commodity_engine import CommodityCorrelationEngine
from app.agents.analyst_agent import run_analyst

logger = logging.getLogger("fundamentalsignals.signal")


class SignalEngine:
    """
    All tuneable parameters are read from config/settings.json so the
    engine behaviour can be changed without touching source code.

    Config sections used:
      deterministic.* — weights, thresholds, ATR multipliers
      oracle.*        — batch sizes, tier thresholds, merge boost
      signals.*       — dedup, max counts
    """

    def __init__(self, price_cache: Dict, state: Any):
        self.price_cache = price_cache
        self.state = state
        self.commodity_engine = CommodityCorrelationEngine()
        self._signal_counter = 0
        self._last_oracle_output: Dict[str, Any] = {}

        # Deterministic layer parameters (from config)
        self.sentiment_threshold = app_config.get("deterministic.sentiment_threshold", 0.15)
        self.ta_score_threshold = app_config.get("deterministic.ta_score_threshold", 0.15)
        self.ta_confidence_multiplier = app_config.get("deterministic.ta_confidence_multiplier", 0.70)
        self.news_confidence_multiplier = app_config.get("deterministic.news_confidence_multiplier", 0.65)
        self.news_confidence_base = app_config.get("deterministic.news_confidence_base", 0.15)
        self.news_confidence_cap = app_config.get("deterministic.news_confidence_cap", 0.80)
        self.commodity_confidence_multiplier = app_config.get("deterministic.commodity_confidence_multiplier", 0.60)
        self.commodity_confidence_base = app_config.get("deterministic.commodity_confidence_base", 0.15)
        self.commodity_confidence_cap = app_config.get("deterministic.commodity_confidence_cap", 0.75)
        self.equity_confidence_multiplier = app_config.get("deterministic.equity_confidence_multiplier", 0.50)
        self.equity_confidence_base = app_config.get("deterministic.equity_confidence_base", 0.15)
        self.equity_confidence_cap = app_config.get("deterministic.equity_confidence_cap", 0.70)
        self.atr_sl_multiplier = app_config.get("deterministic.atr_sl_multiplier", 1.5)
        self.atr_tp_multiplier = app_config.get("deterministic.atr_tp_multiplier", 2.5)
        self.max_deterministic_signals = app_config.get("deterministic.max_deterministic_signals", 30)

        # Oracle layer parameters (from config)
        self.tier_a_threshold = app_config.get("oracle.tier_a_threshold", 0.55)
        self.tier_b_threshold = app_config.get("oracle.tier_b_threshold", 0.40)
        self.oracle_batch_size = app_config.get("oracle.batch_size_articles", 20)
        self.oracle_commodity_lookup = app_config.get("oracle.batch_size_commodity_lookup", 5)
        self.consensus_boost = app_config.get("oracle.consensus_boost", 1.15)
        self.max_merged_confidence = app_config.get("oracle.max_merged_confidence", 0.87)

        # Shared signal parameters (from config)
        self.dedup_window_min = app_config.get("signals.dedup_window_min", 30)
        self.max_active_signals = app_config.get("signals.max_active_signals", 50)

    def _next_signal_id(self) -> str:
        self._signal_counter += 1
        return f"DET_{self._signal_counter:04d}"

    @staticmethod
    def _signal_key(ticker: str, direction: str) -> str:
        return f"{ticker}_{direction}"

    def _article_hash(self, article: Dict[str, Any]) -> str:
        """
        Deterministic hash for an article based on configurable fields.
        Default fields: title + source. This ensures the same article from
        different poll cycles gets the same hash.
        """
        hash_fields = app_config.get("oracle.article_hash_fields", ["title", "source"])
        parts = []
        for field in hash_fields:
            val = article.get(field, "")
            if isinstance(val, str):
                parts.append(val.strip().lower())
            else:
                parts.append(str(val))
        raw = "|".join(parts)
        return hashlib.md5(raw.encode("utf-8", errors="replace")).hexdigest()

    def _select_unprocessed_articles(
        self, articles: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Select articles that have NOT yet been sent to ORACLE.

        Strategy:
          1. Compute hash for each article
          2. Filter out already-processed hashes
          3. Sort by relevance (articles with tickers + high |sentiment| first)
          4. Take top N (oracle.batch_size_articles)
          5. Mark selected hashes as processed
          6. If unprocessed pool drops below threshold, reset hashes
             to allow re-analysis with fresh market context

        Returns: list of unprocessed articles, most relevant first.
        """
        reset_threshold = app_config.get(
            "oracle.unprocessed_reset_threshold", 5
        )
        batch_size = self.oracle_batch_size

        unprocessed = []
        for article in articles:
            h = self._article_hash(article)
            if h not in self.state.oracle_processed_hashes:
                unprocessed.append(article)

        if len(unprocessed) < reset_threshold:
            logger.info(
                f"ORACLE article pool low ({len(unprocessed)} unprocessed < "
                f"{reset_threshold} threshold). Resetting processed hashes."
            )
            self.state.oracle_processed_hashes.clear()
            unprocessed = list(articles)

        def relevance_score(a: Dict[str, Any]) -> float:
            tickers = a.get("tickers", [])
            sentiment = abs(a.get("sentiment", 0))
            ticker_bonus = 0.3 if tickers else 0.0
            return sentiment + ticker_bonus

        unprocessed.sort(key=relevance_score, reverse=True)

        selected = unprocessed[:batch_size]

        for article in selected:
            h = self._article_hash(article)
            self.state.oracle_processed_hashes.add(h)

        self.state.oracle_batch_counter += 1

        logger.info(
            f"ORACLE article selection: {len(articles)} total, "
            f"{len(unprocessed)} unprocessed, selected {len(selected)} "
            f"(batch #{self.state.oracle_batch_counter}, "
            f"total processed hashes: {len(self.state.oracle_processed_hashes)})"
        )

        return selected

    def _is_duplicate(self, key: str) -> bool:
        recent = self.state.recent_signal_keys
        if key in recent:
            return True
        recent.append(key)
        if len(recent) > 200:
            recent.popleft()
        return False

    @staticmethod
    def _ticks_to_candles(prices: list, ticks_per_candle: int = 60) -> list:
        """
        Aggregate raw tick prices into OHLC candles.

        Binance @ticker sends ~1 tick/second per symbol.
        With 500 stored ticks and 60 ticks/candle → ~8 candles.
        This is NOT enough for proper ATR-14, so we use it only as
        a secondary source when 24h range is unavailable.

        Returns: [{"o": open, "h": high, "l": low, "c": close}, ...]
        """
        if not prices or len(prices) < 2:
            return []

        candles = []
        for start in range(0, len(prices), ticks_per_candle):
            chunk = prices[start:start + ticks_per_candle]
            if len(chunk) < 2:
                continue
            candles.append({
                "o": float(chunk[0]),
                "h": float(max(chunk)),
                "l": float(min(chunk)),
                "c": float(chunk[-1]),
            })

        return candles

    def _compute_atr(
        self,
        entry: float,
        prices: List[float],
        high_24h: float = 0,
        low_24h: float = 0,
    ) -> Tuple[float, str]:
        """
        Compute ATR using best available data source.

        Priority chain (institutional best practice):
          1. 24h high/low range (ALWAYS available from Binance, covers full day)
             ATR ≈ (H24 - L24) / 3.5 — factor 3.5 because daily range ≈ 3.5× ATR
          2. Tick-candle ATR (only if 14+ candles from 500 ticks)
          3. Tick-candle simple TR average (5-13 candles)
          4. Percentage fallback (1.5% of entry — crypto standard)

        Returns: (atr_value, source_description)
        """
        if entry <= 0:
            return 0, "no_entry"

        # --- SOURCE 1: 24h range (BEST — full day of volatility) ---
        if high_24h > 0 and low_24h > 0 and high_24h > low_24h:
            daily_range = high_24h - low_24h
            atr = daily_range / 3.5
            # Sanity: ATR should be 0.3%-8% of price for crypto
            min_atr = entry * 0.003
            max_atr = entry * 0.08
            atr = max(min_atr, min(atr, max_atr))
            return atr, f"24h_range(H={high_24h:.2f},L={low_24h:.2f},R={daily_range:.2f})"

        # --- SOURCE 2 & 3: Tick candles ---
        candles = self._ticks_to_candles(prices, ticks_per_candle=60)

        if len(candles) >= 14:
            tr_list = []
            for i in range(1, len(candles)):
                c = candles[i]
                p = candles[i - 1]
                tr = max(
                    c["h"] - c["l"],
                    abs(c["h"] - p["c"]),
                    abs(c["l"] - p["c"]),
                )
                tr_list.append(tr)
            atr = sum(tr_list[-14:]) / 14.0
            if atr > entry * 0.0005:
                return atr, f"candle_atr14({len(candles)}c,{atr:.4f})"

        if len(candles) >= 5:
            tr_list = []
            for i in range(1, len(candles)):
                c = candles[i]
                p = candles[i - 1]
                tr = max(
                    c["h"] - c["l"],
                    abs(c["h"] - p["c"]),
                    abs(c["l"] - p["c"]),
                )
                tr_list.append(tr)
            atr = sum(tr_list) / len(tr_list)
            if atr > entry * 0.0005:
                return atr, f"candle_avg_tr({len(candles)}c,{atr:.4f})"

        # --- SOURCE 4: Percentage fallback ---
        atr = entry * 0.015
        return atr, f"pct_fallback(1.5%)"

    def _atr_sl_tp(
        self,
        prices: List[float],
        direction: str,
        high_24h: float = 0,
        low_24h: float = 0,
    ) -> Tuple[float, float, float, float, float, float]:
        """
        Calculate ATR-based stop-loss and take-profit levels.

        Institutional methodology:
          - SL = entry ± risk_amount (risk = ATR × SL multiplier)
          - TP levels use R:R ratios: TP1 = 1:1, TP2 = 1:2, TP3 = 1:3
            where "1R" = risk amount (distance from entry to SL)
          - This ensures EVERY TP level has positive expected value

        R:R based TPs (configurable multipliers):
          TP1 = entry ± 1.0 × risk  (R:R = 1:1)
          TP2 = entry ± 2.0 × risk  (R:R = 1:2)
          TP3 = entry ± 3.0 × risk  (R:R = 1:3)

        Returns: (sl, tp1, tp2, tp3, atr, risk_reward_ratio)
        """
        entry = float(prices[-1]) if prices else 0

        if not prices or entry <= 0:
            return 0, 0, 0, 0, 0, 0

        atr, atr_source = self._compute_atr(entry, prices, high_24h, low_24h)

        if atr <= 0:
            atr = entry * 0.015
            atr_source = "emergency_pct"

        risk = atr * self.atr_sl_multiplier

        if direction == "BUY":
            sl = entry - risk
            tp1 = entry + risk * 1.0
            tp2 = entry + risk * 2.0
            tp3 = entry + risk * 3.0
        else:
            sl = entry + risk
            tp1 = entry - risk * 1.0
            tp2 = entry - risk * 2.0
            tp3 = entry - risk * 3.0

        reward = abs(tp2 - entry)
        rr = round(reward / risk, 2) if risk > 0 else 0

        logger.info(
            f"ATR/SL/TP: entry={entry:.2f} dir={direction} "
            f"atr={atr:.4f}({atr_source}) risk={risk:.4f} "
            f"SL={sl:.2f} TP1={tp1:.2f} TP2={tp2:.2f} TP3={tp3:.2f} R:R=1:{rr}"
        )

        return round(sl, 6), round(tp1, 6), round(tp2, 6), round(tp3, 6), round(atr, 6), rr

    def _assign_tier(self, confidence: float) -> str:
        """
        Assign tier based on confidence (thresholds from config):
          Tier A: >= oracle.tier_a_threshold (default 0.55)
          Tier B: >= oracle.tier_b_threshold (default 0.40)
          Tier C: < oracle.tier_b_threshold  (monitor only, suppressed)
        """
        if confidence >= self.tier_a_threshold:
            return "A"
        elif confidence >= self.tier_b_threshold:
            return "B"
        else:
            return "C"

    def generate_technical_signals(self) -> Dict[str, Any]:
        results = {}
        for ticker, data in self.price_cache.items():
            prices = data.get("prices", [])
            if len(prices) < 30:
                continue
            ta = generate_technical_signal(np.array(prices, dtype=float))
            if abs(ta["score"]) > self.ta_score_threshold:
                results[ticker] = {
                    "score": ta["score"],
                    "direction": ta["direction"].upper(),
                    "confidence": ta["confidence"],
                    "indicators": ta["indicators"],
                    "current_price": prices[-1],
                }
        return results

    def generate_news_signals(self, articles: List[Dict]) -> List[Dict]:
        signals = []
        for article in articles:
            tickers = article.get("tickers", [])
            sentiment = article.get("sentiment_score", 0)
            if abs(sentiment) < self.sentiment_threshold or not tickers:
                continue

            for ticker_info in tickers:
                ticker = ticker_info["ticker"]
                asset_type = ticker_info.get("type", "stock")
                direction = "BUY" if sentiment > 0 else "SELL"
                confidence = min(
                    abs(sentiment) * self.news_confidence_multiplier + self.news_confidence_base,
                    self.news_confidence_cap,
                )
                tier = self._assign_tier(confidence)

                key = self._signal_key(ticker, direction)
                if self._is_duplicate(key):
                    continue

                signals.append({
                    "signal_id": self._next_signal_id(),
                    "tier": tier,
                    "direction": direction,
                    "ticker": ticker,
                    "instrument": {
                        "ticker": ticker,
                        "name": ticker,
                        "tv_ticker": "",
                        "bloomberg_ticker": "",
                        "exchange": "",
                        "exchange_full": "",
                        "instrument_type": asset_type,
                        "currency": "USD",
                        "trading_hours_utc": "",
                        "liquidity": 3,
                        "cfd_available": False,
                        "cfd_brokers": [],
                        "contract_spec": "",
                    },
                    "asset_type": asset_type,
                    "confidence": round(confidence, 3),
                    "confidence_breakdown": {
                        "base": 0.35,
                        "news_adjustment": round(sentiment * 0.3, 3),
                        "final": round(confidence, 3),
                    },
                    "primary_catalyst": "News_Sentiment",
                    "reasoning": (
                        f"News sentiment: {sentiment:+.2f} "
                        f"from '{article.get('title', '')[:80]}'"
                    ),
                    "key_factors": [
                        f"Source: {article.get('source', 'Unknown')}",
                        f"Sentiment: {sentiment:+.2f}",
                    ],
                    "source": "deterministic_news",
                    "news_trigger": article.get("title", ""),
                    "news_source": article.get("source", ""),
                })
        return signals

    def generate_commodity_signals(self, articles: List[Dict]) -> List[Dict]:
        signals = []
        for article in articles:
            title = article.get("title", "")
            summary = article.get("summary", "")
            sentiment = article.get("sentiment_score", 0)
            if abs(sentiment) < self.sentiment_threshold:
                continue

            analysis = self.commodity_engine.analyze_news(title, summary, sentiment)
            if not analysis.get("has_commodity_signal"):
                continue

            for corr in analysis.get("correlated_commodities", []):
                key = self._signal_key(corr["commodity"], corr["direction"])
                if self._is_duplicate(key):
                    continue

                confidence = round(min(
                    abs(corr["propagated_sentiment"]) * self.commodity_confidence_multiplier + self.commodity_confidence_base,
                    self.commodity_confidence_cap,
                ), 3)
                tier = self._assign_tier(confidence)
                ticker = corr.get("ticker", corr["commodity"])

                signals.append({
                    "signal_id": self._next_signal_id(),
                    "tier": tier,
                    "direction": corr["direction"].upper(),
                    "ticker": ticker,
                    "instrument": {
                        "ticker": ticker,
                        "name": corr["commodity"],
                        "tv_ticker": "",
                        "bloomberg_ticker": "",
                        "exchange": "",
                        "exchange_full": "",
                        "instrument_type": "commodity",
                        "currency": "USD",
                        "trading_hours_utc": "",
                        "liquidity": 3,
                        "cfd_available": False,
                        "cfd_brokers": [],
                        "contract_spec": "",
                    },
                    "asset_type": "commodity",
                    "confidence": confidence,
                    "confidence_breakdown": {
                        "base": 0.30,
                        "correlation_multiplier": round(abs(corr["correlation"]), 3),
                        "final": confidence,
                    },
                    "primary_catalyst": "Correlation",
                    "correlated_from": analysis.get("source_commodity", ""),
                    "correlation_coefficient": round(corr["correlation"], 3),
                    "chain_depth": corr.get("depth", 1),
                    "reasoning": (
                        f"Commodity correlation: {analysis.get('source_commodity')} -> "
                        f"{corr['commodity']} (r={corr['correlation']:.2f})"
                    ),
                    "key_factors": [
                        f"Source commodity: {analysis.get('source_commodity')}",
                        f"Correlation: {corr['correlation']:.2f}",
                        f"Propagated sentiment: {corr['propagated_sentiment']:+.3f}",
                    ],
                    "source": "deterministic_commodity",
                    "commodity_source": analysis.get("source_commodity", ""),
                    "commodity_correlation": round(corr["correlation"], 3),
                })

            for eq in analysis.get("equity_signals", []):
                key = self._signal_key(eq["ticker"], eq["direction"])
                if self._is_duplicate(key):
                    continue

                confidence = round(min(
                    eq.get("sentiment_strength", 0.3) * self.equity_confidence_multiplier + self.equity_confidence_base,
                    self.equity_confidence_cap,
                ), 3)
                tier = self._assign_tier(confidence)

                signals.append({
                    "signal_id": self._next_signal_id(),
                    "tier": tier,
                    "direction": eq["direction"].upper(),
                    "ticker": eq["ticker"],
                    "instrument": {
                        "ticker": eq["ticker"],
                        "name": eq["ticker"],
                        "tv_ticker": "",
                        "bloomberg_ticker": "",
                        "exchange": "",
                        "exchange_full": "",
                        "instrument_type": eq.get("type", "stock"),
                        "currency": "USD",
                        "trading_hours_utc": "",
                        "liquidity": 3,
                        "cfd_available": False,
                        "cfd_brokers": [],
                        "contract_spec": "",
                    },
                    "asset_type": eq.get("type", "stock"),
                    "confidence": confidence,
                    "confidence_breakdown": {
                        "base": 0.25,
                        "correlation_multiplier": round(abs(eq.get("correlation", 0)), 3),
                        "final": confidence,
                    },
                    "primary_catalyst": "Policy_Response",
                    "correlated_from": analysis.get("source_commodity", ""),
                    "correlation_coefficient": round(eq.get("correlation", 0), 3),
                    "chain_depth": 2,
                    "speculative": True,
                    "reasoning": (
                        f"Equity from commodity: {analysis.get('source_commodity')} -> "
                        f"{eq['ticker']} ({eq.get('relation', 'related')})"
                    ),
                    "key_factors": [
                        f"Commodity: {analysis.get('source_commodity')}",
                        f"Relation: {eq.get('relation', 'related')}",
                    ],
                    "source": "deterministic_commodity_equity",
                    "commodity_source": analysis.get("source_commodity", ""),
                })

        return signals

    def enrich_signal_with_prices(self, signal: Dict) -> Dict:
        """
        Enrich a signal with ATR-based entry/SL/TP and technical indicators.
        Only populates fields not already set by ORACLE LLM.

        Uses unified _atr_sl_tp() which handles all data source combinations:
          - 24h range (primary), tick candles (secondary), % fallback
        """
        instrument = signal.get("instrument", {})
        ticker = instrument.get("ticker") or signal.get("ticker", "")
        price_data = self.price_cache.get(ticker)
        if not price_data or not price_data.get("current"):
            logger.debug(f"enrich: no price data for {ticker}, skipping")
            return signal

        prices = price_data.get("prices", [])
        direction = signal.get("direction", "BUY")
        high_24h = price_data.get("high_24h", 0)
        low_24h = price_data.get("low_24h", 0)
        current_price = price_data["current"]

        # Use tick prices if available, otherwise just current price
        tick_list = prices if (prices and len(prices) >= 2) else [current_price]

        sl, tp1, tp2, tp3, atr, rr = self._atr_sl_tp(
            tick_list, direction, high_24h, low_24h
        )
        entry = tick_list[-1]

        if sl == 0 and tp1 == 0:
            logger.warning(f"enrich: ATR computation returned zeros for {ticker}")
            return signal

        signal["entry"] = signal.get("entry") or round(entry, 6)
        signal["stop_loss"] = signal.get("stop_loss") or round(sl, 6)
        signal["stop_basis"] = signal.get("stop_basis", "ATR_BASED")
        signal["atr"] = signal.get("atr") or round(atr, 6)
        signal["atr_14"] = signal.get("atr_14") or round(atr, 6)
        signal["atr_multiplier"] = signal.get("atr_multiplier", self.atr_sl_multiplier)

        signal["tp1"] = signal.get("tp1") or round(tp1, 6)
        signal["tp2"] = signal.get("tp2") or round(tp2, 6)
        signal["tp3"] = signal.get("tp3") or round(tp3, 6)
        signal["take_profit"] = signal.get("take_profit") or round(tp1, 6)
        signal["take_profit_1"] = signal.get("take_profit_1") or round(tp1, 6)
        signal["take_profit_2"] = signal.get("take_profit_2") or round(tp2, 6)
        signal["take_profit_3"] = signal.get("take_profit_3") or round(tp3, 6)
        signal["tp1_close_pct"] = signal.get("tp1_close_pct", 40)
        signal["tp2_close_pct"] = signal.get("tp2_close_pct", 40)
        signal["tp3_close_pct"] = signal.get("tp3_close_pct", 20)
        signal["risk_reward_ratio"] = signal.get("risk_reward_ratio") or rr
        signal["current_price"] = current_price
        signal["change_24h"] = price_data.get("change_24h", 0)

        logger.debug(
            f"enrich {ticker}: entry={signal['entry']} SL={signal['stop_loss']} "
            f"TP1={signal['tp1']} TP2={signal['tp2']} TP3={signal['tp3']} "
            f"ATR={signal['atr']} R:R=1:{signal['risk_reward_ratio']}"
        )

        if not signal.get("technical_signals"):
            if prices and len(prices) >= 30:
                ta = generate_technical_signal(np.array(prices, dtype=float))
                signal["technical_signals"] = {
                    "rsi_14": round(ta["indicators"].get("rsi", 50), 1),
                    "rsi_signal": "overbought" if ta["indicators"].get("rsi", 50) > 70
                                  else "oversold" if ta["indicators"].get("rsi", 50) < 30
                                  else "neutral",
                    "macd_signal": "bullish" if ta["indicators"].get("macd_histogram", 0) > 0 else "bearish",
                    "bb_position": round(ta["indicators"].get("bollinger_percent_b", 0.5), 2),
                    "volume_confirmation": False,
                }
            else:
                signal["technical_signals"] = {
                    "rsi_14": 50,
                    "rsi_signal": "neutral",
                    "macd_signal": "neutral",
                    "bb_position": 0.5,
                    "volume_confirmation": False,
                }

        if not signal.get("timeframe"):
            signal["timeframe"] = "intraday"

        if not signal.get("validity_hours"):
            signal["validity_hours"] = 4 if signal.get("timeframe") == "intraday" else 24

        if not signal.get("invalidation_conditions"):
            signal["invalidation_conditions"] = [
                f"Price crosses {signal['stop_loss']}",
                "Major counter-catalyst news",
                "Volume dries up below 20-period average",
            ]

        return signal

    def generate_deterministic_signals(self, articles: List[Dict]) -> List[Dict]:
        """
        LAYER 1: Generate deterministic signals without LLM.

        Combines technical analysis, news sentiment, and commodity correlation
        into a flat list of tiered signals. Fast (< 100ms), no API calls.

        Returns only Tier A and Tier B signals (Tier C suppressed).
        """
        ta_results = self.generate_technical_signals()
        news_signals = self.generate_news_signals(articles)
        commodity_signals = self.generate_commodity_signals(articles)

        det_signals = news_signals + commodity_signals

        for ticker, ta in ta_results.items():
            key = self._signal_key(ticker, ta["direction"])
            if self._is_duplicate(key):
                continue
            confidence = round(ta["confidence"] * self.ta_confidence_multiplier, 3)
            tier = self._assign_tier(confidence)
            det_signals.append({
                "signal_id": self._next_signal_id(),
                "tier": tier,
                "direction": ta["direction"].upper(),
                "ticker": ticker,
                "instrument": {
                    "ticker": ticker,
                    "name": ticker,
                    "tv_ticker": f"BINANCE:{ticker}USDT",
                    "bloomberg_ticker": "",
                    "exchange": "BINANCE",
                    "exchange_full": "Binance",
                    "instrument_type": "crypto",
                    "currency": "USD",
                    "trading_hours_utc": "24/7",
                    "liquidity": 1,
                    "cfd_available": False,
                    "cfd_brokers": [],
                    "contract_spec": "",
                },
                "asset_type": "crypto",
                "confidence": confidence,
                "confidence_breakdown": {
                    "base": round(ta["score"], 3),
                    "technical_adjustment": round(ta["confidence"], 3),
                    "final": confidence,
                },
                "primary_catalyst": "Direct",
                "reasoning": f"Technical: score={ta['score']:.3f}, direction={ta['direction']}",
                "key_factors": [f"TA score: {ta['score']:.3f}"],
                "source": "deterministic_technical",
                "technical_signals": {
                    "rsi_14": round(ta["indicators"].get("rsi", 50), 1),
                    "macd_signal": "bullish" if ta["indicators"].get("macd_histogram", 0) > 0 else "bearish",
                    "bb_position": round(ta["indicators"].get("bollinger_percent_b", 0.5), 2),
                },
            })

        det_signals.sort(key=lambda s: s.get("confidence", 0), reverse=True)
        det_signals = det_signals[:self.max_deterministic_signals]

        for s in det_signals:
            self.enrich_signal_with_prices(s)

        det_signals = [
            s for s in det_signals
            if s.get("tier", "A") in ("A", "B") and s.get("entry") is not None
        ]

        return det_signals

    async def generate_oracle_signals(self, articles: List[Dict]) -> Dict[str, Any]:
        """
        LAYER 2: Generate ORACLE AI signals via LLM.

        This is the expensive, slow path (10-60 seconds, rate-limited).
        Sends ALL accumulated context to the ORACLE v2.1 agent which produces
        the full three-tier output: tier_a_signals, tier_b_baskets, tier_c_universe.

        Returns the complete oracle_output dict (may be empty if LLM fails).
        """
        ta_results = self.generate_technical_signals()

        commodity_analysis = {}
        for article in articles[:self.oracle_commodity_lookup]:
            title = article.get("title", "")
            summary = article.get("summary", "")
            sentiment = article.get("sentiment_score", 0)
            analysis = self.commodity_engine.analyze_news(title, summary, sentiment)
            if analysis.get("has_commodity_signal"):
                commodity_analysis = analysis
                break

        selected_articles = self._select_unprocessed_articles(articles)

        oracle_output = await run_analyst(
            news_articles=selected_articles,
            technical_data=ta_results,
            commodity_analysis=commodity_analysis,
            price_data=self.price_cache,
        )

        if oracle_output:
            self._last_oracle_output = oracle_output

        return oracle_output

    def merge_oracle_signals(
        self,
        deterministic_signals: List[Dict],
        oracle_output: Dict[str, Any],
    ) -> List[Dict]:
        """
        Merge ORACLE v2.1 LLM signals with deterministic signals.

        Strategy:
          1. ORACLE Tier A signals take priority for matching tickers
          2. Consensus (ORACLE + deterministic agree) gets ~15% confidence boost
          3. Deterministic-only signals (not in ORACLE) kept as-is
          4. Tier C deterministic signals excluded from active list
          5. Result sorted by confidence descending
        """
        oracle_tier_a = oracle_output.get("tier_a_signals", [])
        if not oracle_tier_a:
            return deterministic_signals

        det_by_ticker: Dict[str, Dict] = {}
        for s in deterministic_signals:
            t = s.get("ticker", "")
            if t:
                det_by_ticker[t] = s

        merged = []
        seen_tickers = set()

        for oracle_signal in oracle_tier_a:
            ticker = oracle_signal.get("ticker", "")
            seen_tickers.add(ticker)

            if ticker in det_by_ticker:
                det = det_by_ticker[ticker]
                merged_confidence = min(
                    (det["confidence"] + oracle_signal.get("confidence", 0)) / 2 * self.consensus_boost,
                    self.max_merged_confidence,
                )
                merged_signal = {**det}
                merged_signal.update({
                    k: v for k, v in oracle_signal.items()
                    if v is not None and v != "" and v != 0 and v != []
                })
                merged_signal["confidence"] = round(merged_confidence, 3)
                merged_signal["source"] = "oracle_enhanced"
                merged_signal["tier"] = "A"
                merged.append(merged_signal)
            else:
                merged.append(oracle_signal)

        for det_signal in deterministic_signals:
            ticker = det_signal.get("ticker", "")
            if ticker not in seen_tickers:
                merged.append(det_signal)

        merged.sort(key=lambda s: s.get("confidence", 0), reverse=True)

        return merged[:self.max_active_signals]

    @property
    def last_oracle_output(self) -> Dict[str, Any]:
        return self._last_oracle_output
