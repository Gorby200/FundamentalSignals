"""
app/agents/analyst_agent.py — ORACLE v2.1 LangGraph Analyst Agent.

Architecture:
  LangGraph StateGraph linear pipeline:
    START -> gather_context -> llm_analyze -> parse_oracle_output -> END

  LLM: z.ai GLM-5.1 via OpenAI-compatible API (ChatOpenAI with custom base_url).
  Prompt: loaded from external superprompt v2.1 .md file (editable without code changes).
  Falls back gracefully if LLM is unavailable.

ORACLE v2.1 Three-Tier Output Schema:
  tier_a_signals   — Max 7 instruments, full execution detail (entry/SL/TP1-3/confidence/R:R)
                     Each signal carries a nested `instrument` registry object with
                     tv_ticker, bloomberg_ticker, exchange, liquidity, cfd_available, etc.
  tier_b_baskets   — Theme baskets (3-20 instruments each), grouped by lead instrument.
                     Each basket instrument includes beta_vs_lead and expected_move_pct.
  tier_c_universe  — Extended monitor list, no price levels, includes monitor_trigger.
  signals_suppressed — Instruments considered but rejected, with reason and monitor_trigger.
  correlation_chains_traced — Structured contagion chains with depth and tier labels.
  hedge_recommendations — Hedging instruments with ticker_details and size_pct_vs_tier_a.
  watchlist        — Instruments to monitor with trigger conditions.
  meta             — Aggregate counts, averages, registry version.

Backward compatibility:
  If LLM returns v2.0 format (flat `signals` array), auto-migrates into v2.1 tiered
  structure: all valid v2.0 signals become tier_a_signals, no baskets or universe generated.
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, START, END

from app import config as app_config

logger = logging.getLogger("fundamentalsignals.agent")


def _get_llm_feed_logger() -> logging.Logger:
    """Lazy import to avoid circular dependency at module load."""
    from app.logging_config import get_llm_feed_logger
    return get_llm_feed_logger()


class AnalystState(TypedDict):
    news_articles: List[Dict[str, Any]]
    technical_data: Dict[str, Any]
    commodity_analysis: Dict[str, Any]
    price_data: Dict[str, Any]
    context_summary: str
    llm_response: str
    oracle_output: Dict[str, Any]
    errors: List[str]


_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "prompts"
    / "FundamentalSignals_SuperPrompt_v2.md"
)
_FALLBACK_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "prompts"
    / "analyst_prompt.txt"
)
_system_prompt_cache: Optional[str] = None


def load_analyst_prompt() -> str:
    global _system_prompt_cache
    if _system_prompt_cache is not None:
        return _system_prompt_cache

    for path in [_PROMPT_PATH, _FALLBACK_PROMPT_PATH]:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                _system_prompt_cache = f.read().strip()
            logger.info(f"Analyst prompt loaded from {path}")
            return _system_prompt_cache

    logger.warning("No analyst prompt found, using minimal default")
    _system_prompt_cache = (
        "You are ORACLE v2.1, a financial signal generation engine. "
        "Analyze the provided market data and generate trading signals in JSON format "
        "following the ORACLE v2.1 three-tier output schema with tier_a_signals, "
        "tier_b_baskets, tier_c_universe, signals_suppressed."
    )
    return _system_prompt_cache


def reload_prompt() -> str:
    global _system_prompt_cache
    _system_prompt_cache = None
    return load_analyst_prompt()


def create_llm_client() -> Optional[ChatOpenAI]:
    if not app_config.is_llm_enabled():
        return None

    zai_config = app_config.get_zai_config()
    try:
        llm = ChatOpenAI(
            model=zai_config["model"],
            api_key=zai_config["api_key"],
            base_url=zai_config["base_url"],
            max_tokens=zai_config["max_tokens"],
            temperature=zai_config["temperature"],
            request_timeout=zai_config["timeout"],
            max_retries=3,
        )
        logger.info(f"LLM client created: {zai_config['model']} @ {zai_config['base_url']}")
        return llm
    except Exception as e:
        logger.error(f"Failed to create LLM client: {e}")
        return None


def _score_article_relevance(article: Dict[str, Any]) -> float:
    score = abs(article.get("sentiment_score", 0))
    if article.get("tickers"):
        score += 0.3
    if "commodity" in article.get("asset_types", []):
        score += 0.2
    return score


def gather_context(state: AnalystState) -> dict:
    """
    Build a rich, structured text context from live prices, TA, news, and commodity data.

    Design principles (ML/Data Engineering perspective):
    1. GROUND TRUTH FIRST: Live market prices are the anchor that prevents LLM hallucination.
       Without real prices, the LLM invents numbers from its training data (e.g., BTC at $40K).
    2. DATA COMPLETENESS: We have unlimited tokens — sending 200-char truncated summaries
       is data loss. The LLM needs full context to extract accurate signals.
    3. STRUCTURED SECTIONS: Clear section headers let the LLM parse context efficiently
       and distinguish between different data types (prices vs indicators vs news).
    4. ANTI-HALLUCINATION GUARDRAILS: Every price is explicitly labeled as real-time
       from Binance, with timestamps, so the LLM knows these are NOT estimates.

    Context structure (in order of priority):
      Section 1: LIVE MARKET DATA — current prices, 24h ranges, ATR (ground truth)
      Section 2: TECHNICAL ANALYSIS — RSI, MACD, BB per pair with price context
      Section 3: NEWS — full summaries, 20 articles (up from 10), ranked by relevance
      Section 4: COMMODITY CORRELATIONS — cross-asset signals (when available)
    """
    price_data = state.get("price_data", {})
    articles = state.get("news_articles", [])
    technical_data = state.get("technical_data", {})
    commodity_analysis = state.get("commodity_analysis", {})
    parts = []

    # ── SECTION 1: LIVE MARKET DATA (ground truth for LLM) ──────────────────
    # This is the MOST CRITICAL section. Without real prices the LLM hallucinates
    # numbers from its training data, which may be months out of date.
    # We compute ATR from 24h high/low as the most reliable daily volatility measure.
    if price_data:
        parts.append("=" * 72)
        parts.append("SECTION 1: LIVE MARKET DATA (real-time from Binance WebSocket)")
        parts.append("=" * 72)
        parts.append(
            "NOTE: All prices below are REAL-TIME. Use ONLY these for analysis.\n"
            "Do NOT reference any prices from your training data — they are stale."
        )
        parts.append("")
        parts.append(
            f"{'Pair':<10} {'Price':>12} {'24h Chg%':>10} "
            f"{'24h High':>12} {'24h Low':>12} {'ATR(24h)':>12}"
        )
        parts.append("-" * 72)

        for ticker in sorted(price_data.keys()):
            pd = price_data[ticker]
            current = pd.get("current", 0)
            if not current:
                continue
            change = pd.get("change_24h", 0) or 0
            high = pd.get("high_24h", 0) or 0
            low = pd.get("low_24h", 0) or 0

            # ATR from 24h range: (H-L)/3.5 approximates ATR-14 for daily candles
            atr = (high - low) / 3.5 if high > low else current * 0.015

            parts.append(
                f"{ticker:<10} ${current:>11,.2f} {change:>+9.2f}% "
                f"${high:>11,.2f} ${low:>11,.2f} ${atr:>11,.2f}"
            )
        parts.append("")

    # ── SECTION 2: TECHNICAL ANALYSIS (enriched with price context) ──────────
    # Previously only sent direction + score + 3 indicators.
    # Now includes: current price, human-readable indicator labels, and 24h range
    # so the LLM can reason about support/resistance levels.
    if technical_data:
        parts.append("=" * 72)
        parts.append("SECTION 2: TECHNICAL ANALYSIS (computed from live Binance data)")
        parts.append("=" * 72)
        for ticker, ta in technical_data.items():
            direction = ta.get("direction", "neutral")
            score = ta.get("score", 0)
            indicators = ta.get("indicators", {})
            current_price = ta.get("current_price", 0)

            rsi = indicators.get("rsi", 50)
            macd_hist = indicators.get("macd_histogram", 0)
            bb_pct = indicators.get("bollinger_percent_b", 0.5)

            rsi_label = (
                "OVERSOLD" if rsi < 30
                else "OVERBOUGHT" if rsi > 70
                else "approaching oversold" if rsi < 40
                else "approaching overbought" if rsi > 60
                else "neutral"
            )
            macd_label = "bullish" if macd_hist > 0 else "bearish"
            bb_label = (
                "near lower band (potential bounce)" if bb_pct < 0.15
                else "near upper band (potential reversal)" if bb_pct > 0.85
                else "midrange"
            )

            price_str = f" @ ${current_price:,.2f}" if current_price else ""
            parts.append(
                f"\n{ticker}: {direction.upper()} (score: {score:+.3f}){price_str}"
            )
            parts.append(
                f"  RSI(14): {rsi:.1f} ({rsi_label}) | "
                f"MACD histogram: {macd_hist:+.4f} ({macd_label}) | "
                f"BB %B: {bb_pct:.2f} ({bb_label})"
            )

            # Add 24h range as support/resistance context from price_data
            if ticker in price_data:
                pd = price_data[ticker]
                h24 = pd.get("high_24h", 0) or 0
                l24 = pd.get("low_24h", 0) or 0
                if h24 and l24:
                    parts.append(
                        f"  Key levels — Support: ${l24:,.2f} (24h low) | "
                        f"Resistance: ${h24:,.2f} (24h high)"
                    )
        parts.append("")

    # ── SECTION 3: NEWS (full summaries, 20 articles) ───────────────────────
    # Previously: 10 articles, summary truncated to 200 chars.
    # Now: 20 articles, full summaries. The LLM needs complete context to
    # extract accurate signals. A 200-char truncation loses critical details
    # like specific numbers, dates, and policy details.
    scored = sorted(articles, key=_score_article_relevance, reverse=True)
    top_articles = scored[:20]

    if top_articles:
        parts.append("=" * 72)
        parts.append(
            f"SECTION 3: FINANCIAL NEWS ({len(top_articles)} articles, "
            "ranked by relevance)"
        )
        parts.append("=" * 72)
        for i, article in enumerate(top_articles, 1):
            tickers_str = ", ".join(
                t["ticker"] for t in article.get("tickers", [])
            )
            sentiment = article.get("sentiment_score", 0)
            label = article.get("sentiment_label", "neutral").upper()
            source = article.get("source", "Unknown")
            link = article.get("link", "")
            title = article.get("title", "")
            summary = article.get("summary", "")
            published = article.get("published", "")

            parts.append(
                f"\n{i}. [{label}] {title}\n"
                f"   URL: {link or source} | Tickers: {tickers_str or 'None detected'} "
                f"| Sentiment: {sentiment:+.2f}"
            )
            if published:
                parts.append(f"   Published: {published}")
            # NO TRUNCATION — send full summary so LLM has complete context
            if summary:
                parts.append(f"   Summary: {summary}")

    # ── SECTION 4: COMMODITY CORRELATION ANALYSIS ───────────────────────────
    if commodity_analysis.get("has_commodity_signal"):
        parts.append("\n" + "=" * 72)
        parts.append("SECTION 4: COMMODITY CORRELATION ANALYSIS")
        parts.append("=" * 72)
        parts.append(
            f"Source: {commodity_analysis.get('source_commodity', 'Unknown')}"
        )
        for corr in commodity_analysis.get("correlated_commodities", [])[:5]:
            parts.append(
                f"  -> {corr['commodity']}: {corr['direction'].upper()} "
                f"(sentiment: {corr['propagated_sentiment']:+.3f}, "
                f"correlation: {corr['correlation']:.2f})"
            )

    return {"context_summary": "\n".join(parts)}


def llm_analyze(state: AnalystState) -> dict:
    """
    Invoke the LLM with system prompt + context.
    The prompt instructs ORACLE v2.1 to output complete tiered JSON.
    Full I/O is logged to llm_feed.log for debugging and quality review.
    """
    llm = create_llm_client()
    if llm is None:
        return {"llm_response": "", "errors": state.get("errors", []) + ["LLM not configured"]}

    system_prompt = load_analyst_prompt()
    context = state.get("context_summary", "")
    if not context:
        return {"llm_response": "", "errors": state.get("errors", []) + ["No context available"]}

    articles = state.get("news_articles", [])
    llm_log = _get_llm_feed_logger()

    llm_log.info("=" * 80)
    llm_log.info("ORACLE LLM CALL START")
    llm_log.info(f"Articles in batch: {len(articles)}")
    for i, a in enumerate(articles[:5]):
        llm_log.info(f"  [{i+1}] {a.get('title', '')[:100]}")
    if len(articles) > 5:
        llm_log.info(f"  ... and {len(articles) - 5} more")
    llm_log.info(f"Context length: {len(context)} chars")
    llm_log.info(f"System prompt length: {len(system_prompt)} chars")
    llm_log.info("-" * 40)
    llm_log.info("FULL CONTEXT SENT TO LLM:")
    llm_log.info(context)

    t_start = time.time()

    try:
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(
                content=(
                    "CRITICAL DATA BOUNDARY RULES — READ BEFORE ANALYZING:\n"
                    "1. ALL current prices are in SECTION 1 (LIVE MARKET DATA) below.\n"
                    "2. You MUST use ONLY these provided prices for entry/SL/TP.\n"
                    "3. Do NOT use any prices from your training data — they are STALE.\n"
                    "4. If a ticker is not in LIVE MARKET DATA, you have NO reliable price for it.\n"
                    "5. ATR values are provided — use them for stop-loss/take-profit sizing.\n"
                    "6. Technical indicators in SECTION 2 are from real-time Binance data.\n"
                    "7. News summaries in SECTION 3 are complete — extract all relevant signals.\n\n"
                    "TICKER DISCOVERY INSTRUCTION:\n"
                    "8. Carefully read EVERY news article in SECTION 3 for mentioned companies, "
                    "cryptocurrencies, commodities, indices, currencies, and ETFs.\n"
                    "9. Many articles mention instruments by COMPANY NAME (e.g., 'MicroStrategy', "
                    "'Robinhood', 'Block Inc') rather than ticker symbol.\n"
                    "10. You MUST identify these and use proper ticker symbols in your output "
                    "(e.g., MSTR, HOOD, SQ).\n"
                    "11. Include ALL mentioned instruments — even those without price data — "
                    "in tier_c_universe or watchlist with monitor_trigger conditions.\n"
                    "12. Cross-reference: if news mentions 'semiconductor sector', identify "
                    "NVDA, AMD, INTC, AVGO, TSM and include them in appropriate tiers.\n\n"
                    "Now analyze the following market data and generate ORACLE v2.1 signals "
                    "with the complete three-tier output schema "
                    "(tier_a_signals, tier_b_baskets, tier_c_universe):\n\n"
                    f"{context}"
                )
            ),
        ]

        response = llm.invoke(messages)
        llm_output = response.content if hasattr(response, "content") else str(response)
        elapsed = time.time() - t_start

        logger.info(f"ORACLE analysis complete ({len(llm_output)} chars, {elapsed:.1f}s)")

        llm_log.info("-" * 40)
        llm_log.info(f"LLM RESPONSE ({len(llm_output)} chars, {elapsed:.1f}s):")
        llm_log.info(llm_output)
        llm_log.info("=" * 80)

        return {"llm_response": llm_output}

    except Exception as e:
        elapsed = time.time() - t_start
        error_msg = f"LLM call failed: {type(e).__name__}: {str(e)[:200]}"
        logger.warning(error_msg)

        llm_log.error(f"LLM CALL FAILED after {elapsed:.1f}s: {error_msg}")
        llm_log.info("=" * 80)

        return {"llm_response": "", "errors": state.get("errors", []) + [error_msg]}


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """
    Robust JSON extraction from LLM output.
    Tries: fenced json block -> fenced block -> raw braces.
    Returns None if no valid JSON found.
    """
    patterns = [
        r'```json\s*(\{[\s\S]*?\})\s*```',
        r'```\s*(\{[\s\S]*?\})\s*```',
        r'(\{[\s\S]*\})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return json.loads(match.group(1) if match.lastindex else match.group())
            except json.JSONDecodeError:
                continue
    return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_str(value: Any, default: str = "") -> str:
    return str(value).strip() if value else default


def _clamp_confidence(value: Any) -> float:
    """
    Confidence is always clamped to [0.10, 0.87].
    Upper bound 0.87 is a hard rule from ORACLE v2.1 spec:
    markets are non-deterministic, no signal should claim >87% certainty.
    """
    c = _safe_float(value, 0.5)
    return round(max(0.10, min(0.87, c)), 3)


def _normalize_instrument_registry(raw_instrument: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize the Master Instrument Registry fields that appear in every
    tier (A, B, C). This is the shared "identity card" for an instrument.

    v2.1 spec (Section 13): every instrument MUST have ticker, tv_ticker,
    bloomberg_ticker, exchange, instrument_type, currency, liquidity, cfd_available.
    We provide sensible defaults for any missing fields.
    """
    ticker = _safe_str(raw_instrument.get("ticker"))
    return {
        "ticker": ticker,
        "name": _safe_str(raw_instrument.get("name", ticker)),
        "tv_ticker": _safe_str(raw_instrument.get("tv_ticker", "")),
        "bloomberg_ticker": _safe_str(raw_instrument.get("bloomberg_ticker", "")),
        "exchange": _safe_str(raw_instrument.get("exchange", "")),
        "exchange_full": _safe_str(raw_instrument.get("exchange_full", "")),
        "instrument_type": _safe_str(raw_instrument.get("instrument_type", "unknown")),
        "currency": _safe_str(raw_instrument.get("currency", "USD")),
        "trading_hours_utc": _safe_str(raw_instrument.get("trading_hours_utc", "")),
        "liquidity": _safe_int(raw_instrument.get("liquidity"), 3),
        "cfd_available": bool(raw_instrument.get("cfd_available", False)),
        "cfd_brokers": raw_instrument.get("cfd_brokers", []),
        "contract_spec": _safe_str(raw_instrument.get("contract_spec", "")),
    }


def _normalize_tier_a_signal(raw: Dict[str, Any], idx: int) -> Optional[Dict[str, Any]]:
    """
    Normalize a single Tier A signal from the LLM output.

    Tier A signals carry FULL execution detail: entry, stop_loss, take_profit_1-3,
    confidence with breakdown, risk:reward, technical indicators, invalidation conditions.
    Each Tier A signal has a nested `instrument` object with the Master Registry fields.

    Returns None if the signal is invalid (missing direction or ticker).
    """
    direction = _safe_str(raw.get("direction", "")).upper()
    if direction not in ("BUY", "SELL"):
        return None

    # v2.1 nests instrument info under "instrument"; v2.0 has "ticker" at top level
    raw_instrument = raw.get("instrument", {})
    if raw_instrument:
        instrument = _normalize_instrument_registry(raw_instrument)
        ticker = instrument["ticker"]
    else:
        # Fallback: v2.0-style flat ticker at top level
        ticker = _safe_str(raw.get("ticker", ""))
        instrument = {
            "ticker": ticker,
            "name": ticker,
            "tv_ticker": "",
            "bloomberg_ticker": "",
            "exchange": _safe_str(raw.get("exchange", "")),
            "exchange_full": "",
            "instrument_type": _safe_str(raw.get("asset_type", "unknown")),
            "currency": "USD",
            "trading_hours_utc": "",
            "liquidity": 3,
            "cfd_available": False,
            "cfd_brokers": [],
            "contract_spec": "",
        }

    if not ticker:
        return None

    confidence = _clamp_confidence(raw.get("confidence"))

    # v2.1 uses signal_id like "A001"; fallback to auto-generated
    signal_id = _safe_str(raw.get("signal_id", f"A{idx:03d}"))

    # v2.1 field naming: tp1_close_pct vs take_profit_1_size_pct
    # Accept both naming conventions for robustness
    tp1_pct = raw.get("tp1_close_pct") or raw.get("take_profit_1_size_pct", 40)
    tp2_pct = raw.get("tp2_close_pct") or raw.get("take_profit_2_size_pct", 40)
    tp3_pct = raw.get("tp3_close_pct") or raw.get("take_profit_3_size_pct", 20)

    return {
        "signal_id": signal_id,
        "tier": "A",
        "direction": direction,
        "instrument": instrument,
        "entry": _safe_float(raw.get("entry"), None),
        "entry_condition": _safe_str(
            raw.get("entry_condition", "") or raw.get("entry_note", "")
        ),
        "stop_loss": _safe_float(raw.get("stop_loss"), None),
        "stop_basis": _safe_str(raw.get("stop_basis", "ATR_BASED")),
        "atr_14": _safe_float(raw.get("atr_14") or raw.get("atr_value"), 0),
        "atr_multiplier": _safe_float(raw.get("atr_multiplier"), 1.5),
        "take_profit_1": _safe_float(raw.get("take_profit_1"), None),
        "tp1_basis": _safe_str(
            raw.get("tp1_basis", "") or raw.get("take_profit_1_basis", "")
        ),
        "tp1_close_pct": _safe_int(tp1_pct, 40),
        "take_profit_2": _safe_float(raw.get("take_profit_2"), None),
        "tp2_basis": _safe_str(
            raw.get("tp2_basis", "") or raw.get("take_profit_2_basis", "")
        ),
        "tp2_close_pct": _safe_int(tp2_pct, 40),
        "take_profit_3": _safe_float(raw.get("take_profit_3"), None),
        "tp3_basis": _safe_str(
            raw.get("tp3_basis", "") or raw.get("take_profit_3_basis", "")
        ),
        "tp3_close_pct": _safe_int(tp3_pct, 20),
        "risk_reward_ratio": _safe_float(raw.get("risk_reward_ratio"), 0),
        "confidence": confidence,
        "confidence_breakdown": raw.get("confidence_breakdown", {
            "base": 0.50,
            "final": confidence,
        }),
        "timeframe": _safe_str(raw.get("timeframe", "medium")),
        "validity_hours": _safe_int(raw.get("validity_hours"), 0),
        "chain_depth": _safe_int(raw.get("chain_depth"), 1),
        "speculative": bool(raw.get("speculative", False)),
        "primary_catalyst": _safe_str(raw.get("primary_catalyst", "Direct")),
        "correlated_from": raw.get("correlated_from"),
        "correlation_coefficient": _safe_float(raw.get("correlation_coefficient"), 0),
        "reasoning": _safe_str(raw.get("reasoning", "ORACLE v2.1 analyst signal")),
        "key_factors": raw.get("key_factors", []),
        "technical_signals": raw.get("technical_signals", {}),
        "invalidation_conditions": raw.get("invalidation_conditions", []),
        "source": "oracle_llm",
    }


def _normalize_tier_b_instrument(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Normalize a single instrument within a Tier B basket.

    Tier B instruments carry: instrument registry fields + beta_vs_lead +
    expected_move_if_lead_Xpct + use_case + chain_depth.
    They do NOT carry entry/stop/TP (risk is managed at basket level via lead instrument).
    """
    ticker = _safe_str(raw.get("ticker"))
    if not ticker:
        return None

    direction = _safe_str(raw.get("direction", "")).upper()
    if direction not in ("BUY", "SELL"):
        direction = "BUY"

    return {
        "ticker": ticker,
        "name": _safe_str(raw.get("name", ticker)),
        "tv_ticker": _safe_str(raw.get("tv_ticker", "")),
        "bloomberg_ticker": _safe_str(raw.get("bloomberg_ticker", "")),
        "exchange": _safe_str(raw.get("exchange", "")),
        "instrument_type": _safe_str(raw.get("instrument_type", "unknown")),
        "currency": _safe_str(raw.get("currency", "USD")),
        "trading_hours_utc": _safe_str(raw.get("trading_hours_utc", "")),
        "liquidity": _safe_int(raw.get("liquidity"), 3),
        "cfd_available": bool(raw.get("cfd_available", False)),
        "beta_vs_lead": _safe_float(raw.get("beta_vs_lead"), 1.0),
        "expected_move_if_lead_3pct": _safe_str(
            raw.get("expected_move_if_lead_3pct", "")
        ),
        "direction": direction,
        "use_case": _safe_str(raw.get("use_case", "")),
        "chain_depth": _safe_int(raw.get("chain_depth"), 1),
        "speculative": bool(raw.get("speculative", False)),
    }


def _normalize_tier_b_basket(raw: Dict[str, Any], idx: int) -> Optional[Dict[str, Any]]:
    """
    Normalize a Tier B basket.

    A basket groups correlated instruments around a lead instrument.
    Risk is managed at basket level via the lead instrument's ATR stop.
    If the lead hits its stop, all basket positions exit simultaneously.
    """
    basket_id = _safe_str(raw.get("basket_id", f"B{idx:03d}"))
    lead_ticker = _safe_str(raw.get("lead_instrument_ticker", ""))
    if not lead_ticker:
        return None

    raw_instruments = raw.get("basket_instruments", [])
    instruments = []
    for ri in raw_instruments:
        norm = _normalize_tier_b_instrument(ri)
        if norm:
            instruments.append(norm)

    return {
        "basket_id": basket_id,
        "tier": "B",
        "basket_name": _safe_str(raw.get("basket_name", f"Basket {basket_id}")),
        "theme": _safe_str(raw.get("theme", "")),
        "lead_instrument_ticker": lead_ticker,
        "lead_instrument_direction": _safe_str(
            raw.get("lead_instrument_direction", ""), default="BUY"
        ).upper(),
        "basket_direction": _safe_str(raw.get("basket_direction", "BUY")).upper(),
        "confidence_basket": _clamp_confidence(raw.get("confidence_basket")),
        "risk_management": _safe_str(raw.get("risk_management", "")),
        "basket_instruments": instruments,
    }


def _normalize_tier_c_instrument(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Normalize a Tier C universe instrument (monitor-only).

    Tier C instruments have NO price levels. They carry registry fields +
    correlation/beta to lead + monitor_trigger (condition for promotion to Tier A).
    """
    ticker = _safe_str(raw.get("ticker"))
    if not ticker:
        return None

    return {
        "ticker": ticker,
        "name": _safe_str(raw.get("name", ticker)),
        "tv_ticker": _safe_str(raw.get("tv_ticker", "")),
        "bloomberg_ticker": _safe_str(raw.get("bloomberg_ticker", "")),
        "exchange": _safe_str(raw.get("exchange", "")),
        "instrument_type": _safe_str(raw.get("instrument_type", "unknown")),
        "currency": _safe_str(raw.get("currency", "USD")),
        "liquidity": _safe_int(raw.get("liquidity"), 3),
        "cfd_available": bool(raw.get("cfd_available", False)),
        "direction": _safe_str(raw.get("direction", "")).upper(),
        "correlation_to_lead": _safe_float(raw.get("correlation_to_lead"), 0),
        "beta_vs_lead": _safe_float(raw.get("beta_vs_lead"), 0),
        "chain_depth": _safe_int(raw.get("chain_depth"), 3),
        "monitor_trigger": _safe_str(raw.get("monitor_trigger", "")),
        "note": _safe_str(raw.get("note", "")),
        "speculative": bool(raw.get("speculative", False)),
    }


def _normalize_signals_suppressed(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Normalize a suppressed signal entry.

    v2.1 renames v2.0's signals_not_generated to signals_suppressed and adds
    structured fields: ticker, name, reason, confidence_would_be, monitor_trigger.
    """
    ticker = _safe_str(raw.get("ticker"))
    if not ticker:
        return None

    return {
        "ticker": ticker,
        "name": _safe_str(raw.get("name", ticker)),
        "reason": _safe_str(raw.get("reason", "")),
        "confidence_would_be": _safe_float(raw.get("confidence_would_be"), 0),
        "monitor_trigger": _safe_str(raw.get("monitor_trigger", "")),
    }


def _normalize_hedge_recommendation(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a hedge recommendation.

    v2.1 adds ticker_details (registry object) and size_pct_vs_tier_a
    (position size as percentage of the Tier A signal it hedges).
    """
    ticker_details = raw.get("ticker_details", {})
    if not isinstance(ticker_details, dict):
        ticker_details = {}

    return {
        "hedge_instrument": _safe_str(raw.get("hedge_instrument", "")),
        "ticker_details": {
            "tv_ticker": _safe_str(ticker_details.get("tv_ticker", "")),
            "exchange": _safe_str(ticker_details.get("exchange", "")),
            "type": _safe_str(ticker_details.get("type", "")),
        },
        "direction": _safe_str(raw.get("direction", "")).upper(),
        "size_pct_vs_tier_a": _safe_int(raw.get("size_pct_vs_tier_a"), 0),
        "rationale": _safe_str(raw.get("rationale", "")),
    }


def _normalize_watchlist_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a watchlist item.

    v2.1 adds tv_ticker, exchange, type, trigger (condition for Tier A promotion),
    and current_reason_not_tier_a (why it's not already Tier A).
    """
    return {
        "ticker": _safe_str(raw.get("ticker", "")),
        "tv_ticker": _safe_str(raw.get("tv_ticker", "")),
        "exchange": _safe_str(raw.get("exchange", "")),
        "type": _safe_str(raw.get("type", "")),
        "direction": _safe_str(raw.get("direction", "")).upper(),
        "trigger": _safe_str(raw.get("trigger", "")),
        "current_reason_not_tier_a": _safe_str(
            raw.get("current_reason_not_tier_a", "")
        ),
    }


def _normalize_correlation_chain(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a correlation chain entry.

    v2.1 uses structured objects: {primary_move, chain: [{depth, instrument, direction, coeff, tier}]}
    """
    chain = []
    for link in raw.get("chain", []):
        chain.append({
            "depth": _safe_int(link.get("depth"), 1),
            "instrument": _safe_str(link.get("instrument", "")),
            "direction": _safe_str(link.get("direction", "")).upper(),
            "coeff": _safe_float(link.get("coeff"), 0),
            "tier": _safe_str(link.get("tier", "C")),
            "speculative": bool(link.get("speculative", False)),
        })

    return {
        "primary_move": _safe_str(raw.get("primary_move", "")),
        "chain": chain,
    }


def _migrate_v20_to_v21(parsed_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Migrate v2.0 flat `signals` array into v2.1 tiered structure.

    Strategy: all v2.0 signals become tier_a_signals (they already have full
    execution detail). No baskets or universe are generated from v2.0 output.
    signals_not_generated is renamed to signals_suppressed.
    """
    raw_signals = parsed_json.get("signals", [])
    tier_a = []
    for idx, raw in enumerate(raw_signals, 1):
        signal = _normalize_tier_a_signal(raw, idx)
        if signal:
            tier_a.append(signal)

    # Rename signals_not_generated -> signals_suppressed (preserve old data)
    suppressed = []
    for item in parsed_json.get("signals_not_generated", []):
        norm = _normalize_signals_suppressed(item)
        if norm:
            suppressed.append(norm)

    parsed_json["tier_a_signals"] = tier_a
    parsed_json["tier_b_baskets"] = []
    parsed_json["tier_c_universe"] = []
    parsed_json["signals_suppressed"] = suppressed
    parsed_json["oracle_version"] = "2.1"

    return parsed_json


def parse_oracle_output(state: AnalystState) -> dict:
    """
    Parse the LLM response into a structured oracle_output dict.

    Detection logic:
      - If parsed JSON contains `tier_a_signals` → native v2.1 output, parse tiers.
      - If parsed JSON contains `signals` → v2.0 output, auto-migrate to v2.1.
      - Otherwise → empty result (LLM didn't return valid ORACLE JSON).

    This function is the SOLE parser for all ORACLE output. Every field is
    defensively normalized to prevent downstream KeyError or TypeError.
    """
    llm_response = state.get("llm_response", "")
    if not llm_response:
        return {"oracle_output": {}}

    parsed_json = _extract_json(llm_response)
    if not parsed_json:
        logger.debug("No valid JSON found in ORACLE response")
        return {"oracle_output": {}}

    try:
        oracle_version = _safe_str(
            parsed_json.get("oracle_version", "2.0")
        )

        # Auto-detect v2.0 vs v2.1 format and migrate if needed
        has_tier_a = "tier_a_signals" in parsed_json
        has_legacy_signals = "signals" in parsed_json and not has_tier_a

        if has_legacy_signals:
            logger.info("Detected v2.0 format, auto-migrating to v2.1 tiered structure")
            parsed_json = _migrate_v20_to_v21(parsed_json)
            oracle_version = "2.1"

        # --- Parse Tier A signals (primary execution-quality signals) ---
        tier_a_signals = []
        for idx, raw in enumerate(parsed_json.get("tier_a_signals", []), 1):
            signal = _normalize_tier_a_signal(raw, idx)
            if signal:
                tier_a_signals.append(signal)

        # --- Parse Tier B baskets (theme-based correlated groups) ---
        tier_b_baskets = []
        for idx, raw in enumerate(parsed_json.get("tier_b_baskets", []), 1):
            basket = _normalize_tier_b_basket(raw, idx)
            if basket:
                tier_b_baskets.append(basket)

        # --- Parse Tier C universe (extended monitor list) ---
        tier_c_universe = []
        for raw in parsed_json.get("tier_c_universe", []):
            inst = _normalize_tier_c_instrument(raw)
            if inst:
                tier_c_universe.append(inst)

        # --- Parse suppressed signals ---
        signals_suppressed = []
        for raw in parsed_json.get("signals_suppressed", []):
            norm = _normalize_signals_suppressed(raw)
            if norm:
                signals_suppressed.append(norm)

        # --- Parse correlation chains ---
        correlation_chains = []
        for raw in parsed_json.get("correlation_chains_traced", []):
            correlation_chains.append(_normalize_correlation_chain(raw))

        # --- Parse hedge recommendations ---
        hedge_recs = []
        for raw in parsed_json.get("hedge_recommendations", []):
            hedge_recs.append(_normalize_hedge_recommendation(raw))

        # --- Parse watchlist ---
        watchlist = []
        for raw in parsed_json.get("watchlist", []):
            watchlist.append(_normalize_watchlist_item(raw))

        # --- Parse meta ---
        raw_meta = parsed_json.get("meta", {})
        meta = {
            "tier_a_count": _safe_int(raw_meta.get("tier_a_count"), len(tier_a_signals)),
            "tier_b_basket_count": _safe_int(
                raw_meta.get("tier_b_basket_count"), len(tier_b_baskets)
            ),
            "tier_b_instrument_count": _safe_int(
                raw_meta.get("tier_b_instrument_count"), 0
            ),
            "tier_c_count": _safe_int(raw_meta.get("tier_c_count"), len(tier_c_universe)),
            "signals_suppressed_count": _safe_int(
                raw_meta.get("signals_suppressed_count"), len(signals_suppressed)
            ),
            "average_tier_a_confidence": _safe_float(
                raw_meta.get("average_tier_a_confidence"), 0
            ),
            "regime_alignment_score": _safe_float(
                raw_meta.get("regime_alignment_score"), 0
            ),
            "data_completeness": _safe_str(raw_meta.get("data_completeness", "unknown")),
            "correlation_chain_max_depth_reached": _safe_int(
                raw_meta.get("correlation_chain_max_depth_reached"), 0
            ),
            "ticker_registry_version": _safe_str(
                raw_meta.get("ticker_registry_version", "ORACLE_2.1")
            ),
        }

        oracle_output = {
            "oracle_version": oracle_version,
            "analysis_timestamp": parsed_json.get(
                "analysis_timestamp", datetime.now().isoformat()
            ),
            "regime": parsed_json.get("regime", {}),
            "news_assessment": parsed_json.get("news_assessment", {}),
            "tier_a_signals": tier_a_signals,
            "tier_b_baskets": tier_b_baskets,
            "tier_c_universe": tier_c_universe,
            "correlation_chains_traced": correlation_chains,
            "signals_suppressed": signals_suppressed,
            "market_outlook": _safe_str(parsed_json.get("market_outlook", "")),
            "risk_warnings": parsed_json.get("risk_warnings", []),
            "hedge_recommendations": hedge_recs,
            "watchlist": watchlist,
            "meta": meta,
        }

        total_b_instruments = sum(
            len(b.get("basket_instruments", [])) for b in tier_b_baskets
        )
        logger.info(
            f"ORACLE v{oracle_version} parsed: "
            f"tier_a={len(tier_a_signals)}, "
            f"tier_b={len(tier_b_baskets)} baskets ({total_b_instruments} instruments), "
            f"tier_c={len(tier_c_universe)}, "
            f"suppressed={len(signals_suppressed)}, "
            f"regime={oracle_output['regime'].get('code', '?')}"
        )
        return {"oracle_output": oracle_output}

    except Exception as e:
        logger.error(f"ORACLE output parsing error: {e}", exc_info=True)
        return {"oracle_output": {}}


def build_analyst_graph() -> StateGraph:
    graph = StateGraph(AnalystState)

    graph.add_node("gather_context", gather_context)
    graph.add_node("llm_analyze", llm_analyze)
    graph.add_node("parse_oracle_output", parse_oracle_output)

    graph.add_edge(START, "gather_context")
    graph.add_edge("gather_context", "llm_analyze")
    graph.add_edge("llm_analyze", "parse_oracle_output")
    graph.add_edge("parse_oracle_output", END)

    return graph


analyst_graph = build_analyst_graph()
compiled_agent = analyst_graph.compile()


async def run_analyst(
    news_articles: List[Dict[str, Any]],
    technical_data: Dict[str, Any],
    commodity_analysis: Dict[str, Any],
    price_data: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """
    Run the full ORACLE v2.1 analyst agent pipeline.

    Returns the complete oracle_output dict with tiered structure:
      tier_a_signals, tier_b_baskets, tier_c_universe, correlation_chains_traced,
      signals_suppressed, market_outlook, risk_warnings, hedge_recommendations,
      watchlist, meta, regime, news_assessment.
    Returns empty dict if LLM is unavailable or parsing fails.
    """
    initial_state: AnalystState = {
        "news_articles": news_articles,
        "technical_data": technical_data,
        "commodity_analysis": commodity_analysis,
        "price_data": price_data or {},
        "context_summary": "",
        "llm_response": "",
        "oracle_output": {},
        "errors": [],
    }

    llm_log = _get_llm_feed_logger()

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, compiled_agent.invoke, initial_state)
        oracle_output = result.get("oracle_output", {})

        if oracle_output:
            tier_a = oracle_output.get("tier_a_signals", [])
            tier_b = oracle_output.get("tier_b_baskets", [])
            tier_c = oracle_output.get("tier_c_universe", [])
            regime = oracle_output.get("regime", {})
            suppressed = oracle_output.get("signals_suppressed", [])

            llm_log.info("-" * 40)
            llm_log.info("ORACLE PARSED OUTPUT SUMMARY:")
            llm_log.info(
                f"  Tier A signals: {len(tier_a)}, "
                f"Tier B baskets: {len(tier_b)}, "
                f"Tier C universe: {len(tier_c)}, "
                f"Suppressed: {len(suppressed)}"
            )
            llm_log.info(
                f"  Regime: {regime.get('code', '?')} "
                f"({regime.get('label', '?')}), "
                f"confidence: {regime.get('confidence_pct', '?')}%"
            )
            for sig in tier_a[:3]:
                inst = sig.get("instrument", {})
                llm_log.info(
                    f"  [A] {inst.get('ticker', '?')} "
                    f"{sig.get('direction', '?')} @ "
                    f"{sig.get('entry', '?')} "
                    f"(conf: {sig.get('confidence', '?')})"
                )
            outlook = oracle_output.get("market_outlook", "")
            if outlook:
                llm_log.info(f"  Market outlook: {outlook[:200]}")
            risks = oracle_output.get("risk_warnings", [])
            if risks:
                llm_log.info(f"  Risk warnings: {len(risks)}")
                for r in risks[:3]:
                    llm_log.info(f"    - {str(r)[:150]}")
        else:
            llm_log.warning("ORACLE output was empty after parsing")

        return oracle_output
    except Exception as e:
        logger.error(f"ORACLE agent error: {e}")
        llm_log.error(f"ORACLE agent pipeline error: {type(e).__name__}: {e}")
        return {}
