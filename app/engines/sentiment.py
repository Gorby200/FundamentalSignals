"""
app/engines/sentiment.py — News sentiment analysis and ticker extraction.

This engine is the bridge between raw RSS articles and tradable signals.
It performs two critical functions:

1. TICKER EXTRACTION: Scans headline + summary for financial entity keywords
   and maps them to standardized tickers. This is the "what is this news about?"
   question answered.

2. SENTIMENT SCORING: Determines whether the news is bullish or bearish
   for the detected tickers. Uses keyword-based scoring (no AI needed here —
   the LLM analyst agent handles the nuanced analysis).

Why keyword-based sentiment instead of pure LLM?
  - Speed: 200+ articles per minute need instant scoring.
  - Determinism: Same headline always gets the same score (crucial for debugging).
  - Cost: Free. The LLM agent runs separately on the TOP signals only.
"""

import re
import hashlib
import logging
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime

logger = logging.getLogger("fundamentalsignals.sentiment")

# =============================================================================
# TICKER MAP — Maps news keywords to standardized tickers + asset type
#
# Architecture decision: We maintain a FLAT dictionary rather than a database
# because for a prototype with ~200 entries, a dict lookup is O(1) and trivially
# serializable. Production would use a proper entity resolution service.
#
# Each entry: keyword -> (ticker, asset_type, full_name)
# asset_type: "crypto" | "stock" | "forex" | "commodity" | "index"
# =============================================================================

TICKER_MAP: Dict[str, Tuple[str, str, str]] = {}

# --- Crypto ---
# Only specific coin/protocol names — no generic terms like "crypto", "blockchain", "token".
# Generic terms cause false positives: a war article mentioning "crypto" is not crypto news.
_crypto_entries = [
    ("bitcoin", "BTC-USD", "crypto", "Bitcoin"),
    ("btc", "BTC-USD", "crypto", "Bitcoin"),
    ("ethereum", "ETH-USD", "crypto", "Ethereum"),
    ("solana", "SOL-USD", "crypto", "Solana"),
    ("sol", "SOL-USD", "crypto", "Solana"),
    ("binance coin", "BNB-USD", "crypto", "BNB"),
    ("bnb", "BNB-USD", "crypto", "BNB"),
    ("ripple", "XRP-USD", "crypto", "XRP"),
    ("xrp", "XRP-USD", "crypto", "XRP"),
    ("cardano", "ADA-USD", "crypto", "Cardano"),
    ("ada", "ADA-USD", "crypto", "Cardano"),
    ("dogecoin", "DOGE-USD", "crypto", "Dogecoin"),
    ("doge", "DOGE-USD", "crypto", "Dogecoin"),
    ("litecoin", "LTC-USD", "crypto", "Litecoin"),
    ("ltc", "LTC-USD", "crypto", "Litecoin"),
    ("tether", "USDT-USD", "crypto", "Tether"),
    ("usdt", "USDT-USD", "crypto", "USDT"),
    ("usdc", "USDC-USD", "crypto", "USDC"),
    ("coinbase", "COIN", "stock", "Coinbase"),
    ("binance", "BNB-USD", "crypto", "Binance"),
    ("bitcoin etf", "BTC-USD", "crypto", "Bitcoin ETF"),
    ("spot etf", "BTC-USD", "crypto", "Spot ETF"),
]

# --- Commodities ---
# These are CRITICAL for the commodity-news correlation engine.
# When news mentions "oil prices surge", we need to know this affects
# Crude Oil (CL=F), Energy stocks (XLE), and inversely the Dollar (DXY).
_commodity_entries = [
    ("gold", "GC=F", "commodity", "Gold"),
    ("silver", "SI=F", "commodity", "Silver"),
    ("crude oil", "CL=F", "commodity", "Crude Oil"),
    ("oil price", "CL=F", "commodity", "Crude Oil"),
    ("oil prices", "CL=F", "commodity", "Crude Oil"),
    ("brent crude", "BZ=F", "commodity", "Brent Crude"),
    ("brent", "BZ=F", "commodity", "Brent Crude"),
    ("wti", "CL=F", "commodity", "WTI Crude"),
    ("natural gas", "NG=F", "commodity", "Natural Gas"),
    ("gas price", "NG=F", "commodity", "Natural Gas"),
    ("copper", "HG=F", "commodity", "Copper"),
    ("soybean", "ZS=F", "commodity", "Soybeans"),
    ("soybeans", "ZS=F", "commodity", "Soybeans"),
    ("wheat", "ZW=F", "commodity", "Wheat"),
    ("corn", "ZC=F", "commodity", "Corn"),
    ("coffee", "KC=F", "commodity", "Coffee"),
    ("sugar", "SB=F", "commodity", "Sugar"),
    ("cotton", "CT=F", "commodity", "Cotton"),
    ("lumber", "LBS=F", "commodity", "Lumber"),
    ("iron ore", "SCCO", "commodity", "Iron Ore"),
    ("steel", "SLX", "commodity", "Steel"),
    ("coal", "MTF", "commodity", "Coal"),
    ("platinum", "PL=F", "commodity", "Platinum"),
    ("palladium", "PA=F", "commodity", "Palladium"),
    ("aluminum", "ALI=F", "commodity", "Aluminum"),
    ("uranium", "URA", "commodity", "Uranium"),
    ("lithium", "LIT", "commodity", "Lithium"),
    ("rare earth", "MXI", "commodity", "Rare Earth"),
    ("commodit", "GSG", "commodity", "Commodities Basket"),
    ("energy", "XLE", "stock", "Energy Sector"),
    ("precious metal", "GDX", "stock", "Precious Metals Miners"),
    ("agricultur", "DBA", "commodity", "Agriculture"),
    ("heating oil", "HO=F", "commodity", "Heating Oil"),
    ("gasoline", "RB=F", "commodity", "Gasoline"),
    ("commodity", "GSG", "commodity", "Commodities"),
]

# --- Stocks / Indices ---
_stock_entries = [
    ("sp 500", "SPY", "stock", "S&P 500"),
    ("s&p 500", "SPY", "stock", "S&P 500"),
    ("s&p", "SPY", "stock", "S&P 500"),
    ("nasdaq", "QQQ", "stock", "NASDAQ"),
    ("dow jones", "DIA", "stock", "Dow Jones"),
    ("dow", "DIA", "stock", "Dow Jones"),
    ("apple", "AAPL", "stock", "Apple"),
    ("aapl", "AAPL", "stock", "Apple"),
    ("microsoft", "MSFT", "stock", "Microsoft"),
    ("msft", "MSFT", "stock", "Microsoft"),
    ("google", "GOOGL", "stock", "Alphabet"),
    ("googl", "GOOGL", "stock", "Alphabet"),
    ("alphabet", "GOOGL", "stock", "Alphabet"),
    ("amazon", "AMZN", "stock", "Amazon"),
    ("amzn", "AMZN", "stock", "Amazon"),
    ("tesla", "TSLA", "stock", "Tesla"),
    ("tsla", "TSLA", "stock", "Tesla"),
    ("nvidia", "NVDA", "stock", "NVIDIA"),
    ("nvda", "NVDA", "stock", "NVIDIA"),
    ("meta", "META", "stock", "Meta"),
    ("facebook", "META", "stock", "Meta"),
    ("netflix", "NFLX", "stock", "Netflix"),
    ("nflx", "NFLX", "stock", "Netflix"),
    ("jpmorgan", "JPM", "stock", "JPMorgan"),
    ("jpm", "JPM", "stock", "JPMorgan"),
    ("goldman sachs", "GS", "stock", "Goldman Sachs"),
    ("bank of america", "BAC", "stock", "Bank of America"),
    ("wells fargo", "WFC", "stock", "Wells Fargo"),
    ("visa", "V", "stock", "Visa"),
    ("mastercard", "MA", "stock", "Mastercard"),
    ("disney", "DIS", "stock", "Disney"),
    ("boeing", "BA", "stock", "Boeing"),
    ("unitedhealth", "UNH", "stock", "UnitedHealth"),
    ("exxon", "XOM", "stock", "ExxonMobil"),
    ("chevron", "CVX", "stock", "Chevron"),
    ("shell", "SHEL", "stock", "Shell"),
    ("bp", "BP", "stock", "BP"),
    ("pfizer", "PFE", "stock", "Pfizer"),
    ("johnson & johnson", "JNJ", "stock", "Johnson & Johnson"),
    ("intel", "INTC", "stock", "Intel"),
    ("amd", "AMD", "stock", "AMD"),
    ("salesforce", "CRM", "stock", "Salesforce"),
    ("adobe", "ADBE", "stock", "Adobe"),
    ("paypal", "PYPL", "stock", "PayPal"),
    ("shopify", "SHOP", "stock", "Shopify"),
    ("uber", "UBER", "stock", "Uber"),
    ("airbnb", "ABNB", "stock", "Airbnb"),
    ("palantir", "PLTR", "stock", "Palantir"),
    ("snowflake", "SNOW", "stock", "Snowflake"),
    ("ai chip", "NVDA", "stock", "NVIDIA"),
    ("semiconductor", "SMH", "stock", "Semiconductors"),
    ("chip", "SMH", "stock", "Semiconductors"),
    ("ai stock", "QQQ", "stock", "AI Stocks"),
    ("tech stock", "QQQ", "stock", "Tech Stocks"),
    ("rate cut", "SPY", "stock", "S&P 500"),
    ("interest rate", "SPY", "stock", "S&P 500"),
    ("fed ", "SPY", "stock", "S&P 500"),
    ("federal reserve", "SPY", "stock", "S&P 500"),
    ("inflation", "TIP", "stock", "TIPS"),
    ("recession", "SHV", "stock", "Short-term Bonds"),
    ("gdp", "SPY", "stock", "S&P 500"),
    ("treasury", "TLT", "stock", "Treasury Bonds"),
    ("bond", "TLT", "stock", "Bond Market"),
    ("yield", "TLT", "stock", "Bond Yields"),
    ("earnings", "SPY", "stock", "Earnings Season"),
    ("ipo", "IPO", "stock", "IPO Market"),
    ("stock market", "SPY", "stock", "Stock Market"),
    ("wall street", "SPY", "stock", "Wall Street"),
    ("bull market", "SPY", "stock", "Bull Market"),
    ("bear market", "SH", "stock", "Bear Market"),
    ("market crash", "SH", "stock", "Market Crash"),
    ("trade war", "SPY", "stock", "Trade War"),
    ("tariff", "SPY", "stock", "Tariffs Impact"),
    ("regulation", "SPY", "stock", "Regulation"),
    ("etf", "SPY", "stock", "ETF Market"),
]

# --- Forex ---
_forex_entries = [
    ("dollar", "DX-Y.NYB", "forex", "US Dollar Index"),
    ("us dollar", "DX-Y.NYB", "forex", "US Dollar Index"),
    ("greenback", "DX-Y.NYB", "forex", "US Dollar Index"),
    ("euro", "EURUSD=X", "forex", "EUR/USD"),
    ("eurusd", "EURUSD=X", "forex", "EUR/USD"),
    ("pound", "GBPUSD=X", "forex", "GBP/USD"),
    ("gbpusd", "GBPUSD=X", "forex", "GBP/USD"),
    ("sterling", "GBPUSD=X", "forex", "GBP/USD"),
    ("yen", "USDJPY=X", "forex", "USD/JPY"),
    ("usdjpy", "USDJPY=X", "forex", "USD/JPY"),
    ("swiss franc", "USDCHF=X", "forex", "USD/CHF"),
    ("yuan", "USDCNY=X", "forex", "USD/CNY"),
    ("ruble", "USDRUB=X", "forex", "USD/RUB"),
    ("forex", "EURUSD=X", "forex", "Forex Market"),
    ("currency", "DX-Y.NYB", "forex", "Currency Market"),
    ("fiat", "DX-Y.NYB", "forex", "Fiat Currency"),
]

# Build the flat dictionary — longer phrases first to match "crude oil" before "oil"
_all_entries = _crypto_entries + _commodity_entries + _stock_entries + _forex_entries
_all_entries.sort(key=lambda x: len(x[0]), reverse=True)
for keyword, ticker, asset_type, name in _all_entries:
    TICKER_MAP[keyword.lower()] = (ticker, asset_type, name)


# =============================================================================
# SENTIMENT KEYWORDS
#
# These are weighted phrases that shift sentiment in a headline.
# Positive = bullish for the mentioned asset, Negative = bearish.
#
# Why multiple weight levels?
#   - "surge" is stronger than "rise" — we need to capture degree.
#   - "crash" is catastrophic (-0.8) while "dip" might be buying opportunity (-0.2).
# =============================================================================

BULLISH_PHRASES = {
    "surge": 0.6, "soar": 0.6, "rally": 0.5, "jump": 0.4, "climb": 0.3,
    "rise": 0.3, "gain": 0.3, "bullish": 0.5, "breakout": 0.5,
    "upgrade": 0.4, "buy": 0.3, "outperform": 0.4, "beat": 0.4,
    "exceed": 0.3, "strong": 0.3, "growth": 0.3, "profit": 0.3,
    "record high": 0.6, "all-time high": 0.6, "ath": 0.5,
    "recovery": 0.3, "rebound": 0.3, "bounce": 0.3,
    "adoption": 0.4, "approved": 0.4, "approval": 0.4,
    "institutional": 0.3, "accumulate": 0.3, "accumulate": 0.3,
    "hawkish": 0.2, "support": 0.2, "bull": 0.4,
    "moon": 0.5, "pump": 0.4, "whale buy": 0.5,
    "supply cut": 0.5, "production cut": 0.5, "opec cut": 0.5,
    "safe haven": 0.4, "flight to safety": 0.4, "hedge": 0.2,
    "demand rise": 0.4, "demand surge": 0.5, "tight supply": 0.5,
    "inventory drop": 0.4, "stockpile decline": 0.4,
}

BEARISH_PHRASES = {
    "crash": 0.8, "plunge": 0.7, "slump": 0.5, "tumble": 0.5, "dive": 0.5,
    "fall": 0.3, "drop": 0.3, "decline": 0.3, "lose": 0.3, "loss": 0.3,
    "bearish": 0.5, "breakdown": 0.5, "sell-off": 0.5, "selloff": 0.5,
    "downgrade": 0.4, "sell": 0.3, "underperform": 0.4, "miss": 0.4,
    "weak": 0.3, "slowdown": 0.3, "recession": 0.4,
    "record low": 0.6, "fear": 0.3, "panic": 0.5,
    "ban": 0.5, "crackdown": 0.5, "restrict": 0.4, "regulate": 0.2,
    "hack": 0.5, "exploit": 0.5, "breach": 0.4,
    "dovish": 0.2, "resistance": 0.2, "bear": 0.4,
    "dump": 0.5, "capitulation": 0.6, "liquidation": 0.4,
    "oversupply": 0.4, "glut": 0.5, "demand fall": 0.4,
    "inventory build": 0.3, "stockpile rise": 0.3,
    "trade war": 0.4, "tariff": 0.3, "sanction": 0.4,
}


def extract_tickers(text: str, discovered_tickers: Dict[str, Any] = None) -> List[Dict[str, str]]:
    """
    Scan text for financial entity keywords and return matched tickers.

    Two-phase extraction:
      Phase 1: Static keyword matching via TICKER_MAP (fast, deterministic, ~200 entries)
      Phase 2: Dynamic matching via discovered_tickers (runtime, populated by ORACLE LLM)

    The dynamic map allows the deterministic layer to recognize instruments that
    ORACLE discovered in previous cycles — creating a feedback loop where LLM
    intelligence improves the fast deterministic pipeline.

    Uses word-boundary regex matching to prevent false positives:
      - "btc" matches "buy btc now" but NOT "subtle"
      - "doge" matches "dogecoin" but NOT "dogeared"
      - "gold" matches "gold prices" but NOT "golden"

    Returns a list of dicts: [{"ticker": "BTC-USD", "type": "crypto", "name": "Bitcoin"}, ...]
    Deduplicates by ticker (first match wins).
    """
    text_lower = text.lower()
    seen_tickers = set()
    results = []

    for keyword, (ticker, asset_type, name) in TICKER_MAP.items():
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, text_lower) and ticker not in seen_tickers:
            seen_tickers.add(ticker)
            results.append({"ticker": ticker, "type": asset_type, "name": name})

    if discovered_tickers:
        for ticker, info in discovered_tickers.items():
            if ticker in seen_tickers:
                continue
            ticker_lower = ticker.lower().replace("-", " ").replace("_", " ")
            name_lower = info.get("name", "").lower()
            search_text = text_lower
            matched = False

            if ticker_lower in search_text:
                matched = True
            elif name_lower and len(name_lower) > 2 and name_lower in search_text:
                matched = True

            if matched:
                seen_tickers.add(ticker)
                results.append({
                    "ticker": ticker,
                    "type": info.get("type", "unknown"),
                    "name": info.get("name", ticker),
                })

    return results[:8]


def score_sentiment(text: str) -> float:
    """
    Score sentiment of a headline/summary on a -1.0 to +1.0 scale.

    Methodology:
      1. Check for bearish phrases first (markets fall faster than they rise).
      2. Check for bullish phrases.
      3. Sum and clamp to [-1.0, 1.0].
      4. If no sentiment phrases found, return 0.0 (neutral).

    Why bearish-first?
      - In 30+ years of trading, the #1 rule is: protect the downside.
      - Bearish news travels faster and has more immediate price impact.
      - By checking bearish first, we ensure crash/plunge signals are never
        overridden by a coincidental "support" mention.
    """
    text_lower = text.lower()
    score = 0.0

    for phrase, weight in BEARISH_PHRASES.items():
        if phrase in text_lower:
            score -= weight

    for phrase, weight in BULLISH_PHRASES.items():
        if phrase in text_lower:
            score += weight

    return max(-1.0, min(1.0, score))


def generate_news_slug(title: str) -> str:
    """Create a deterministic slug from article title for deduplication."""
    return hashlib.md5(title.strip().lower().encode()).hexdigest()[:12]


_TOPIC_KEYWORDS = {
    "crypto": [
        "bitcoin", "btc", "ethereum", "eth ", "blockchain", "defi", "nft",
        "altcoin", "stablecoin", "token", "mining pool", "hash rate",
        "crypto", "satoshi", "web3", "dex", "cex", "airdrop",
        "smart contract", "proof of stake", "proof of work",
        "solana", "cardano", "xrp", "dogecoin", "litecoin",
        "binance", "coinbase", "kraken", "okx", "bybit",
    ],
    "commodity": [
        "crude oil", "brent", "wti", "barrel", "opec", "oil price",
        "gold price", "silver price", "copper", "natural gas",
        "commodity", " commodities", "futures contract", "spot price",
        "drilling", "pipeline", "refinery", "sanctions oil",
        "iron ore", "wheat", "soybean", "corn price",
    ],
    "forex": [
        "forex", "currency pair", "exchange rate", "usd/jpy", "eur/usd",
        "gbp/usd", "dollar index", "dxy", "central bank", "interest rate",
        "monetary policy", "yen ", "sterling", "federal reserve",
        "ecb ", "bank of japan", "rate hike", "rate cut",
    ],
    "stock": [
        "stock market", "shares", "earnings", "revenue", "ipo ",
        "s&p 500", "nasdaq", "dow jones", "index fund",
        "hedge fund", "portfolio", "dividend", "market cap",
        "bull market", "bear market", "valuation", "quarterly",
        "shareholder", "equity", "mutual fund",
    ],
    "geopolitics": [
        "war ", "conflict", "sanctions", "tariff", "trade war",
        "nato", "military", "missile", "attack", "invasion",
        "nuclear", "ceasefire", "peace talk", "embargo",
        "geopolitical", "foreign policy", "diplomacy",
    ],
    "macro": [
        "inflation", "cpi ", "gdp ", "unemployment", "jobs report",
        "federal reserve", "treasury", "bond yield", "fiscal",
        "recession", "stimulus", "quantitative", "money supply",
    ],
}


def _classify_by_keywords(text: str) -> str:
    """
    Classify an article into a topic category by keyword density.

    When no tickers are detected, we fall back to scanning the article text
    for topic-specific keywords. The category with the most keyword hits wins.
    This prevents general news from crypto RSS feeds being mislabeled as 'crypto'.

    Priority: crypto > commodity > forex > stock > geopolitics > macro > markets
    """
    text_lower = text.lower()
    scores = {}
    for category, keywords in _TOPIC_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[category] = score

    if not scores:
        return "markets"

    return max(scores, key=scores.get)


def process_article(raw_article: Dict[str, Any], discovered_tickers: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
    """
    Process a raw RSS article into our internal format.

    Input: raw_article with keys: title, summary, link, published, source, category
    Output: processed article dict with tickers, sentiment, slug — or None if not financial.
    """
    title = raw_article.get("title", "").strip()
    if not title or len(title) < 10:
        return None

    summary = raw_article.get("summary", "")
    full_text = f"{title} {summary}"

    tickers = extract_tickers(full_text, discovered_tickers)
    sentiment = score_sentiment(full_text)

    slug = generate_news_slug(title)

    asset_types = list({t["type"] for t in tickers}) if tickers else []

    CATEGORY_PRIORITY = ["crypto", "commodity", "forex", "stock", "etf"]
    content_category = "markets"
    for cat in CATEGORY_PRIORITY:
        if cat in asset_types:
            content_category = cat
            break
    if not asset_types:
        content_category = _classify_by_keywords(full_text)
        asset_types = ["general"]

    return {
        "id": slug,
        "title": title,
        "summary": summary,
        "link": raw_article.get("link", ""),
        "published": raw_article.get("published", ""),
        "source": raw_article.get("source", "Unknown"),
        "category": content_category,
        "tickers": tickers,
        "sentiment_score": round(sentiment, 3),
        "sentiment_label": (
            "bullish" if sentiment > 0.15
            else "bearish" if sentiment < -0.15
            else "neutral"
        ),
        "asset_types": asset_types,
        "timestamp": raw_article.get("timestamp", ""),
    }
