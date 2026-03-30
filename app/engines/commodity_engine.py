"""
app/engines/commodity_engine.py — Commodity Correlation Engine.

This is the core innovation of FundamentalSignals: a mathematical model that
propagates news sentiment through commodity correlation chains to generate
trading signals on RELATED assets that weren't directly mentioned in the news.

ARCHITECTURE (Super-Trader Logic, 30+ Years Experience):

Why commodity correlations matter more than individual commodity analysis:
  1. INTERCONNECTED MARKETS: When oil surges +3%, it doesn't just affect oil stocks.
     It affects: airlines (negative), shipping (negative), inflation expectations
     (positive for gold), the dollar (complex), and commodity currencies (CAD, NOK).
  
  2. LEAD-LAG RELATIONSHIPS: Copper is called "Dr. Copper" because it LEADS the
     economy. When copper drops, it signals industrial slowdown 2-3 months ahead.
     Gold often LEADS inflation fears. Oil LEADS energy stock earnings.

  3. CORRELATION vs CAUSATION: We don't assume causation. We use correlations as
     PROBABILITY MULTIPLIERS. If oil is up 2% and oil-gold correlation is 0.08
     (weak), we don't trade gold based on oil. But if oil-gasoline correlation
     is 0.98, we ABSOLUTELY trade gasoline based on oil news.

THE ALGORITHM:
  1. News mentions "crude oil" with bullish sentiment (+0.6)
  2. We look up oil's correlation with every other commodity
  3. For each correlated commodity:
     - Propagated sentiment = news_sentiment * correlation * decay_factor
     - If propagated sentiment > threshold, generate secondary signal
  4. We also cross-reference with EQUITIES affected by commodities:
     - Oil up → energy stocks up (XLE, XOM, CVX)
     - Gold up → gold miners up (GDX, NEM)
     - Copper up → industrial metals up (FCX, SCCO)

Correlation source: tradingeconomics.com/commodities/correlations (real market data).
"""

import logging
import math
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger("fundamentalsignals.commodity")

# =============================================================================
# COMMODITY CORRELATION MATRIX
#
# Source: tradingeconomics.com/commodities/correlations
# These are REAL historical correlations between commodity price movements.
# Updated manually for the prototype; production would scrape daily.
#
# Interpretation:
#   +1.0 = perfect positive correlation (move together 100%)
#   +0.5 = moderate positive correlation
#    0.0 = no correlation
#   -0.5 = moderate negative correlation
#   -1.0 = perfect negative correlation (inverse movement)
# =============================================================================

COMMODITY_NAMES = [
    "Crude Oil", "Brent", "Natural Gas", "Gasoline", "Heating Oil",
    "Gold", "Silver", "Copper", "Soybeans", "Wheat",
    "Coal", "Steel", "TTF Gas", "Lumber", "Iron Ore",
]

COMMODITY_CORRELATION_MATRIX: Dict[str, Dict[str, float]] = {
    "Crude Oil":  {"Crude Oil": 1.0, "Brent": 0.99, "Natural Gas": -0.43, "Gasoline": 0.98, "Heating Oil": 0.98, "Gold": 0.08, "Silver": -0.33, "Copper": -0.60, "Soybeans": 0.81, "Wheat": 0.91, "Coal": 0.96, "Steel": 0.35, "TTF Gas": 0.92, "Lumber": 0.34, "Iron Ore": 0.25},
    "Brent":      {"Crude Oil": 0.99, "Brent": 1.0, "Natural Gas": -0.44, "Gasoline": 0.99, "Heating Oil": 0.99, "Gold": 0.02, "Silver": -0.35, "Copper": -0.65, "Soybeans": 0.79, "Wheat": 0.90, "Coal": 0.96, "Steel": 0.37, "TTF Gas": 0.92, "Lumber": 0.36, "Iron Ore": 0.28},
    "Natural Gas": {"Crude Oil": -0.43, "Brent": -0.44, "Natural Gas": 1.0, "Gasoline": -0.50, "Heating Oil": -0.41, "Gold": -0.13, "Silver": 0.34, "Copper": 0.16, "Soybeans": -0.60, "Wheat": -0.59, "Coal": -0.49, "Steel": 0.10, "TTF Gas": -0.27, "Lumber": 0.06, "Iron Ore": 0.08},
    "Gasoline":   {"Crude Oil": 0.98, "Brent": 0.99, "Natural Gas": -0.50, "Gasoline": 1.0, "Heating Oil": 0.98, "Gold": 0.10, "Silver": -0.34, "Copper": -0.62, "Soybeans": 0.84, "Wheat": 0.94, "Coal": 0.97, "Steel": 0.29, "TTF Gas": 0.91, "Lumber": 0.30, "Iron Ore": 0.19},
    "Heating Oil": {"Crude Oil": 0.98, "Brent": 0.99, "Natural Gas": -0.41, "Gasoline": 0.98, "Heating Oil": 1.0, "Gold": 0.03, "Silver": -0.34, "Copper": -0.67, "Soybeans": 0.78, "Wheat": 0.89, "Coal": 0.96, "Steel": 0.35, "TTF Gas": 0.94, "Lumber": 0.36, "Iron Ore": 0.25},
    "Gold":       {"Crude Oil": 0.08, "Brent": 0.02, "Natural Gas": -0.13, "Gasoline": 0.10, "Heating Oil": 0.03, "Gold": 1.0, "Silver": 0.59, "Copper": 0.43, "Soybeans": 0.44, "Wheat": 0.32, "Coal": 0.12, "Steel": -0.38, "TTF Gas": 0.09, "Lumber": 0.22, "Iron Ore": -0.66},
    "Silver":     {"Crude Oil": -0.33, "Brent": -0.35, "Natural Gas": 0.34, "Gasoline": -0.34, "Heating Oil": -0.34, "Gold": 0.59, "Silver": 1.0, "Copper": 0.66, "Soybeans": -0.24, "Wheat": -0.24, "Coal": -0.39, "Steel": 0.04, "TTF Gas": -0.16, "Lumber": 0.21, "Iron Ore": -0.21},
    "Copper":     {"Crude Oil": -0.60, "Brent": -0.65, "Natural Gas": 0.16, "Gasoline": -0.62, "Heating Oil": -0.67, "Gold": 0.43, "Silver": 0.66, "Copper": 1.0, "Soybeans": -0.40, "Wheat": -0.49, "Coal": -0.62, "Steel": -0.26, "TTF Gas": -0.58, "Lumber": -0.13, "Iron Ore": -0.32},
    "Soybeans":   {"Crude Oil": 0.81, "Brent": 0.79, "Natural Gas": -0.60, "Gasoline": 0.84, "Heating Oil": 0.78, "Gold": 0.44, "Silver": -0.24, "Copper": -0.40, "Soybeans": 1.0, "Wheat": 0.93, "Coal": 0.85, "Steel": -0.08, "TTF Gas": 0.70, "Lumber": 0.23, "Iron Ore": -0.21},
    "Wheat":      {"Crude Oil": 0.91, "Brent": 0.90, "Natural Gas": -0.59, "Gasoline": 0.94, "Heating Oil": 0.89, "Gold": 0.32, "Silver": -0.24, "Copper": -0.49, "Soybeans": 0.93, "Wheat": 1.0, "Coal": 0.92, "Steel": 0.11, "TTF Gas": 0.81, "Lumber": 0.21, "Iron Ore": -0.03},
    "Coal":       {"Crude Oil": 0.96, "Brent": 0.96, "Natural Gas": -0.49, "Gasoline": 0.97, "Heating Oil": 0.96, "Gold": 0.12, "Silver": -0.39, "Copper": -0.62, "Soybeans": 0.85, "Wheat": 0.92, "Coal": 1.0, "Steel": 0.18, "TTF Gas": 0.93, "Lumber": 0.27, "Iron Ore": 0.08},
    "Steel":      {"Crude Oil": 0.35, "Brent": 0.37, "Natural Gas": 0.10, "Gasoline": 0.29, "Heating Oil": 0.35, "Gold": -0.38, "Silver": 0.04, "Copper": -0.26, "Soybeans": -0.08, "Wheat": 0.11, "Coal": 0.18, "Steel": 1.0, "TTF Gas": 0.36, "Lumber": 0.24, "Iron Ore": 0.88},
    "TTF Gas":    {"Crude Oil": 0.92, "Brent": 0.92, "Natural Gas": -0.27, "Gasoline": 0.91, "Heating Oil": 0.94, "Gold": 0.09, "Silver": -0.16, "Copper": -0.58, "Soybeans": 0.70, "Wheat": 0.81, "Coal": 0.93, "Steel": 0.36, "TTF Gas": 1.0, "Lumber": 0.40, "Iron Ore": 0.21},
    "Lumber":     {"Crude Oil": 0.34, "Brent": 0.36, "Natural Gas": 0.06, "Gasoline": 0.30, "Heating Oil": 0.36, "Gold": 0.22, "Silver": 0.21, "Copper": -0.13, "Soybeans": 0.23, "Wheat": 0.21, "Coal": 0.27, "Steel": 0.24, "TTF Gas": 0.40, "Lumber": 1.0, "Iron Ore": 0.16},
    "Iron Ore":   {"Crude Oil": 0.25, "Brent": 0.28, "Natural Gas": 0.08, "Gasoline": 0.19, "Heating Oil": 0.25, "Gold": -0.66, "Silver": -0.21, "Copper": -0.32, "Soybeans": -0.21, "Wheat": -0.03, "Coal": 0.08, "Steel": 0.88, "TTF Gas": 0.21, "Lumber": 0.16, "Iron Ore": 1.0},
}

# =============================================================================
# COMMODITY → TICKER MAPPING
#
# Maps commodity names to Yahoo Finance / Binance tickers and related equities.
# "related_equities" are stocks that move WITH the commodity (same direction).
# "inverse_equities" are stocks that move AGAINST the commodity.
# =============================================================================

COMMODITY_TICKER_MAP: Dict[str, Dict[str, Any]] = {
    "Crude Oil":   {"ticker": "CL=F",  "type": "commodity", "related_equities": ["XOM", "CVX", "XLE", "COP", "OXY"], "inverse_equities": ["AAL", "DAL", "UAL", "LUV"]},
    "Brent":       {"ticker": "BZ=F",  "type": "commodity", "related_equities": ["XOM", "CVX", "XLE", "SHEL", "BP"], "inverse_equities": ["AAL", "DAL", "UAL"]},
    "Natural Gas": {"ticker": "NG=F",  "type": "commodity", "related_equities": ["EQN", "RRC", "SWN", "COG"], "inverse_equities": ["NI", "D", "SO"]},
    "Gasoline":    {"ticker": "RB=F",  "type": "commodity", "related_equities": ["XOM", "CVX", "VLO", "MPC"], "inverse_equities": ["AAL", "DAL", "UAL"]},
    "Heating Oil": {"ticker": "HO=F",  "type": "commodity", "related_equities": ["XOM", "CVX", "PSX"], "inverse_equities": []},
    "Gold":        {"ticker": "GC=F",  "type": "commodity", "related_equities": ["NEM", "GDX", "AEM", "GOLD", "WPM"], "inverse_equities": ["DX-Y.NYB"]},
    "Silver":      {"ticker": "SI=F",  "type": "commodity", "related_equities": ["SLV", "PAAS", "HL", "CDE"], "inverse_equities": []},
    "Copper":      {"ticker": "HG=F",  "type": "commodity", "related_equities": ["FCX", "SCCO", "TECK", "HBM"], "inverse_equities": []},
    "Soybeans":    {"ticker": "ZS=F",  "type": "commodity", "related_equities": ["ADM", "BG", "CTVA"], "inverse_equities": []},
    "Wheat":       {"ticker": "ZW=F",  "type": "commodity", "related_equities": ["ADM", "BG", "CTVA", "NTR"], "inverse_equities": []},
    "Coal":        {"ticker": "MTF",   "type": "commodity", "related_equities": ["BTU", "ARCH", "AMR"], "inverse_equities": []},
    "Steel":       {"ticker": "SLX",   "type": "commodity", "related_equities": ["NUE", "STLD", "CLF", "MT"], "inverse_equities": []},
    "TTF Gas":     {"ticker": "NG=F",  "type": "commodity", "related_equities": ["EQN", "RRC"], "inverse_equities": []},
    "Lumber":      {"ticker": "LBS=F", "type": "commodity", "related_equities": ["WFG", "POPE", "RYAM"], "inverse_equities": []},
    "Iron Ore":    {"ticker": "SCCO",  "type": "commodity", "related_equities": ["RIO", "BHP", "VALE"], "inverse_equities": []},
}

# =============================================================================
# COMMODITY NEWS KEYWORD MAPPING
#
# Maps news keywords to the commodity they represent.
# This is how we detect that "OPEC cuts production" = Crude Oil news.
# =============================================================================

COMMODITY_KEYWORDS: Dict[str, str] = {
    "crude oil": "Crude Oil", "oil price": "Crude Oil", "oil prices": "Crude Oil",
    "wti": "Crude Oil", "opec": "Crude Oil", "brent": "Brent", "brent crude": "Brent",
    "natural gas": "Natural Gas", "gas price": "Natural Gas", "gas prices": "Natural Gas",
    "gasoline": "Gasoline", "heating oil": "Heating Oil",
    "gold": "Gold", "precious metal": "Gold", "safe haven": "Gold",
    "silver": "Silver",
    "copper": "Copper", "dr. copper": "Copper", "industrial metal": "Copper",
    "soybean": "Soybeans", "soybeans": "Soybeans", "soy": "Soybeans",
    "wheat": "Wheat", "grain": "Wheat",
    "corn": "Wheat", "coffee": "Wheat",
    "coal": "Coal", "thermal coal": "Coal",
    "steel": "Steel", "iron ore": "Iron Ore",
    "iron": "Iron Ore",
    "lumber": "Lumber", "timber": "Lumber",
    "ttf gas": "TTF Gas", "european gas": "TTF Gas",
    "energy price": "Crude Oil", "energy prices": "Crude Oil",
    "commodity price": "Gold", "commodity prices": "Gold",
    "inflation": "Gold", "cpi": "Gold",
    "supply cut": "Crude Oil", "production cut": "Crude Oil",
    "inventory": "Crude Oil", "stockpile": "Crude Oil",
    "rig count": "Crude Oil", "drilling": "Crude Oil",
    "shale": "Crude Oil", "fracking": "Crude Oil",
    "refinery": "Gasoline", "pipeline": "Crude Oil",
}


class CommodityCorrelationEngine:
    """
    The Commodity Correlation Engine: propagates news sentiment through
    the commodity correlation matrix to generate secondary signals.

    Think of it as a ripple effect:
      - Stone drops (news hits) at "Crude Oil"
      - Ripple spreads to correlated commodities (Brent 0.99, Gasoline 0.98...)
      - Each ripple weakens with distance (correlation * decay)
      - If ripple is strong enough (> threshold), it creates a tradable signal

    This is how professional commodity desks think:
      "Oil is up 3%. That means gasoline will follow (0.98 corr).
       Coal too (0.96). Wheat might tick up (0.91).
       But copper? Probably not (-0.60)."
    """

    def __init__(self, min_correlation: float = 0.30, decay_factor: float = 0.85):
        """
        Args:
            min_correlation: Below this threshold, we ignore the correlation.
                0.30 means we only propagate through moderate+ correlations.
                Why? Weak correlations (0.08 for Oil-Gold) produce noise, not signals.

            decay_factor: Multiplier applied to each propagation step.
                0.85 means the second-order signal is 85% of the first-order.
                This prevents small correlations from compounding into false signals.
        """
        self.min_correlation = min_correlation
        self.decay_factor = decay_factor

    def detect_commodity_from_text(self, text: str) -> Optional[str]:
        """
        Detect which commodity a news article is about.

        Strategy: longest keyword match wins (same as ticker extraction).
        "crude oil" beats "oil" which beats "gas".
        """
        text_lower = text.lower()

        # Sort by keyword length descending for longest match first
        sorted_keywords = sorted(COMMODITY_KEYWORDS.keys(), key=len, reverse=True)

        for keyword in sorted_keywords:
            if keyword in text_lower:
                return COMMODITY_KEYWORDS[keyword]

        return None

    def propagate_sentiment(
        self,
        source_commodity: str,
        sentiment: float,
        max_depth: int = 1
    ) -> List[Dict[str, Any]]:
        """
        Propagate sentiment from source commodity through correlation matrix.

        Args:
            source_commodity: The commodity mentioned in news (e.g. "Crude Oil")
            sentiment: News sentiment score (-1.0 to +1.0)
            max_depth: How many hops to propagate (1 = direct correlations only)

        Returns:
            List of propagated signals: [
                {
                    "commodity": "Gasoline",
                    "ticker": "RB=F",
                    "type": "commodity",
                    "propagated_sentiment": 0.588,
                    "correlation": 0.98,
                    "source": "Crude Oil",
                    "related_equities": ["XOM", "CVX", "VLO", "MPC"],
                    "inverse_equities": ["AAL", "DAL", "UAL"]
                },
                ...
            ]
        """
        if source_commodity not in COMMODITY_CORRELATION_MATRIX:
            return []

        source_correlations = COMMODITY_CORRELATION_MATRIX[source_commodity]
        propagated = []

        for target_commodity, correlation in source_correlations.items():
            if target_commodity == source_commodity:
                continue

            if abs(correlation) < self.min_correlation:
                continue

            # Propagated sentiment = source sentiment * correlation strength * decay
            # Why multiply by correlation?
            #   - If oil sentiment is +0.6 and oil-gasoline correlation is 0.98,
            #     gasoline gets +0.6 * 0.98 * 0.85 = +0.50 (strong bullish)
            #   - If oil sentiment is +0.6 and oil-gold correlation is 0.08,
            #     gold gets +0.6 * 0.08 * 0.85 = +0.04 (below threshold, ignored)
            propagated_sentiment = sentiment * abs(correlation) * self.decay_factor

            # Determine direction based on correlation sign
            # Negative correlation means INVERSE movement:
            #   Oil up +0.6 with Copper correlation -0.60 → Copper sentiment = -0.6 * 0.6 * 0.85 = -0.31
            if correlation < 0:
                propagated_sentiment = -propagated_sentiment

            if abs(propagated_sentiment) < 0.10:
                continue

            target_info = COMMODITY_TICKER_MAP.get(target_commodity, {})

            propagated.append({
                "commodity": target_commodity,
                "ticker": target_info.get("ticker", ""),
                "type": "commodity",
                "propagated_sentiment": round(propagated_sentiment, 4),
                "correlation": correlation,
                "source": source_commodity,
                "related_equities": target_info.get("related_equities", []),
                "inverse_equities": target_info.get("inverse_equities", []),
                "direction": "buy" if propagated_sentiment > 0 else "sell",
            })

        # Sort by absolute propagated sentiment (strongest first)
        propagated.sort(key=lambda x: abs(x["propagated_sentiment"]), reverse=True)

        return propagated

    def generate_equity_signals(
        self,
        commodity: str,
        sentiment: float,
        correlation_to_commodity: float = 1.0
    ) -> List[Dict[str, Any]]:
        """
        Generate equity signals from commodity news.

        When oil surges, we don't just signal oil — we signal:
          1. Energy stocks (XOM, CVX) — same direction, leveraged beta
          2. Airlines (AAL, DAL) — inverse direction, cost input
          3. Airline signals are INVERSE: oil up = airline costs up = bearish

        This is how real commodity desks generate trade ideas:
          "Oil up 3% → buy XOM, sell DAL" is a classic pairs trade.
        """
        if commodity not in COMMODITY_TICKER_MAP:
            return []

        info = COMMODITY_TICKER_MAP[commodity]
        signals = []

        # Related equities move in SAME direction as commodity
        for eq_ticker in info.get("related_equities", []):
            equity_sentiment = sentiment * correlation_to_commodity * 0.7
            if abs(equity_sentiment) > 0.10:
                signals.append({
                    "ticker": eq_ticker,
                    "type": "stock",
                    "sentiment": round(equity_sentiment, 4),
                    "direction": "buy" if equity_sentiment > 0 else "sell",
                    "reason": f"{commodity} sentiment propagation (corr: {correlation_to_commodity:.2f})",
                    "source_commodity": commodity,
                })

        # Inverse equities move in OPPOSITE direction
        for eq_ticker in info.get("inverse_equities", []):
            equity_sentiment = -sentiment * correlation_to_commodity * 0.6
            if abs(equity_sentiment) > 0.10:
                signals.append({
                    "ticker": eq_ticker,
                    "type": "stock",
                    "sentiment": round(equity_sentiment, 4),
                    "direction": "buy" if equity_sentiment > 0 else "sell",
                    "reason": f"{commodity} inverse correlation (corr: {-correlation_to_commodity:.2f})",
                    "source_commodity": commodity,
                })

        return signals

    def analyze_news(self, title: str, summary: str, sentiment: float) -> Dict[str, Any]:
        """
        Full analysis pipeline for a news article.

        Steps:
          1. Detect which commodity the news is about
          2. Propagate sentiment through correlation matrix
          3. Generate equity signals from commodity signal
          4. Return complete analysis with all secondary signals

        This is the MAIN ENTRY POINT called by the signal engine.
        """
        full_text = f"{title} {summary}"

        source_commodity = self.detect_commodity_from_text(full_text)
        if not source_commodity:
            return {"has_commodity_signal": False}

        # Step 1: Propagate through correlation matrix
        correlated_signals = self.propagate_sentiment(source_commodity, sentiment)

        # Step 2: Generate equity signals for the source commodity
        equity_signals = self.generate_equity_signals(source_commodity, sentiment)

        # Step 3: Generate equity signals for strongly correlated commodities
        for corr_signal in correlated_signals[:3]:
            if abs(corr_signal["correlation"]) > 0.5:
                eq_signals = self.generate_equity_signals(
                    corr_signal["commodity"],
                    sentiment,
                    corr_signal["correlation"]
                )
                equity_signals.extend(eq_signals)

        return {
            "has_commodity_signal": True,
            "source_commodity": source_commodity,
            "source_ticker": COMMODITY_TICKER_MAP.get(source_commodity, {}).get("ticker", ""),
            "correlated_commodities": correlated_signals,
            "equity_signals": equity_signals,
        }
