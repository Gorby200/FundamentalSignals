"""
app/engines/price_engine.py — Real-time price data engine (Binance only).

Architecture:
  - BINANCE WEBSOCKET: Real-time crypto prices (< 1 second latency)
    - 8 pairs: BTC, ETH, SOL, BNB, XRP, ADA, DOGE, LTC
    - Persistent connection with exponential-backoff auto-reconnect
    - Prices stored with full tick history for technical analysis

Why Binance only (no yfinance)?
  - yfinance was completely blocked by Yahoo (SSL errors, timeouts).
  - We have 80+ RSS feeds covering all asset classes.
  - News-driven signals don't require real-time stock/forex tick data.
  - For the prototype, crypto real-time data from Binance is sufficient.
  - Production would use Polygon.io / Alpha Vantage for stocks/forex.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Any

import numpy as np

logger = logging.getLogger("fundamentalsignals.price")

BINANCE_SYMBOLS = {
    "BTC-USD": "btcusdt",
    "ETH-USD": "ethusdt",
    "SOL-USD": "solusdt",
    "BNB-USD": "bnbusdt",
    "XRP-USD": "xrpusdt",
    "ADA-USD": "adausdt",
    "DOGE-USD": "dogeusdt",
    "LTC-USD": "ltcusdt",
}

BINANCE_REVERSE = {v: k for k, v in BINANCE_SYMBOLS.items()}


async def binance_websocket_loop(price_cache: Dict, max_retries: int = 100):
    """
    Persistent Binance WebSocket connection with auto-reconnect.

    Combined stream: single connection for all 8 pairs.
    Each tick extends the ticker's price history (capped at 500 for TA).
    Exponential backoff on disconnect: 1s -> 30s max.
    """
    import websockets

    symbols = list(BINANCE_SYMBOLS.values())
    stream_names = "/".join(f"{s}@ticker" for s in symbols)
    ws_url = f"wss://stream.binance.com:9443/stream?streams={stream_names}"

    retry_delay = 1.0

    for attempt in range(max_retries):
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                logger.info(f"Binance WebSocket connected ({len(symbols)} pairs)")
                retry_delay = 1.0

                async for raw_msg in ws:
                    try:
                        data = json.loads(raw_msg)
                        ticker_data = data.get("data", {})
                        symbol = ticker_data.get("s", "").lower()

                        if symbol in BINANCE_REVERSE:
                            our_ticker = BINANCE_REVERSE[symbol]
                            price = float(ticker_data.get("c", 0))
                            change_24h = float(ticker_data.get("P", 0))
                            volume = float(ticker_data.get("v", 0))
                            high_24h = float(ticker_data.get("h", 0))
                            low_24h = float(ticker_data.get("l", 0))

                            if our_ticker not in price_cache:
                                price_cache[our_ticker] = {
                                    "prices": [], "current": 0,
                                    "change_24h": 0, "volume": 0,
                                    "high_24h": 0, "low_24h": 0,
                                    "type": "crypto", "source": "binance",
                                }

                            price_cache[our_ticker]["prices"].append(price)
                            if len(price_cache[our_ticker]["prices"]) > 500:
                                price_cache[our_ticker]["prices"] = price_cache[our_ticker]["prices"][-500:]

                            price_cache[our_ticker].update({
                                "current": price,
                                "change_24h": change_24h,
                                "volume": volume,
                                "high_24h": high_24h,
                                "low_24h": low_24h,
                                "updated": datetime.now().isoformat(),
                            })
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue

        except Exception as e:
            logger.warning(f"Binance WS error (attempt {attempt + 1}): {type(e).__name__}")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 30)

    logger.error("Binance WebSocket: max retries exhausted")
