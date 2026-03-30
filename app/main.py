"""
app/main.py — FastAPI application entry point.

ORACLE v2.1 FundamentalSignals system.

Two-layer signal pipeline:
  - DETERMINISTIC (every 30s): TA + news sentiment + commodity correlation
  - ORACLE AI (every 10 min): z.ai GLM-4.5-air LLM for three-tier analysis

Data sources:
  - Binance WebSocket for real-time crypto prices (8 pairs)
  - RSS polling for financial news (28 feeds, every 10 min)
  - Technical indicators on live price data

WebSocket broadcast sends full state every 5s with tiered signal structure.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, Set
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse

from app.state import AppState
from app.engines.price_engine import binance_websocket_loop
from app.engines.news_engine import poll_all_feeds
from app.engines.signal_engine import SignalEngine

logger = logging.getLogger("fundamentalsignals")

app_state = AppState()
signal_engine = SignalEngine(app_state.price_cache, app_state)


@asynccontextmanager
async def lifespan(application: FastAPI):
    logger.info("ORACLE v2.1 FundamentalSignals starting...")

    app_state.stats["started_at"] = datetime.now().isoformat()

    background_tasks: Set[asyncio.Task] = set()

    binance_task = asyncio.create_task(
        binance_websocket_loop(app_state.price_cache), name="binance_ws"
    )
    background_tasks.add(binance_task)

    rss_task = asyncio.create_task(rss_polling_loop(), name="rss_poller")
    background_tasks.add(rss_task)

    deterministic_task = asyncio.create_task(
        deterministic_signal_loop(), name="deterministic_gen"
    )
    background_tasks.add(deterministic_task)

    oracle_task = asyncio.create_task(oracle_signal_loop(), name="oracle_gen")
    background_tasks.add(oracle_task)

    broadcast_task = asyncio.create_task(broadcast_loop(), name="broadcaster")
    background_tasks.add(broadcast_task)

    initial_fetch_task = asyncio.create_task(initial_rss_fetch(), name="initial_fetch")
    background_tasks.add(initial_fetch_task)

    logger.info(
        "All background tasks started "
        "(binance_ws, rss_poller, deterministic_gen, oracle_gen, broadcaster, initial_fetch)"
    )

    yield

    logger.info("Shutting down...")
    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    logger.info("Shutdown complete")


app = FastAPI(
    title="FundamentalSignals ORACLE v2.1",
    version="2.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def initial_rss_fetch():
    """Non-blocking initial RSS fetch so the dashboard has data on load."""
    try:
        logger.info("Initial RSS fetch starting (non-blocking)...")
        articles = await poll_all_feeds(app_state.seen_news_slugs)
        for article in articles:
            app_state.news_articles.appendleft(article)
        app_state.stats["rss_polls"] += 1
        logger.info(f"Initial RSS fetch: {len(articles)} new articles")
    except Exception as e:
        logger.warning(f"Initial RSS fetch failed: {type(e).__name__}: {e}")


async def rss_polling_loop():
    """Periodic RSS polling with configurable interval (default: 600s / 10 min)."""
    from app import config as app_config

    interval = app_config.get("feeds.rss_poll_interval_sec", 600)
    logger.info(f"RSS polling loop started (interval: {interval}s)")

    while True:
        try:
            await asyncio.sleep(interval)
            articles = await poll_all_feeds(app_state.seen_news_slugs)
            for article in articles:
                app_state.news_articles.appendleft(article)
            app_state.stats["rss_polls"] += 1
            if articles:
                logger.info(f"RSS poll: {len(articles)} new articles")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"RSS poll error: {type(e).__name__}: {e}")
            await asyncio.sleep(30)


async def deterministic_signal_loop():
    """
    LAYER 1: Fast deterministic signal generation (every 30s).

    Combines TA + news sentiment + commodity correlation into tiered signals.
    No LLM calls — purely algorithmic. Only Tier A and Tier B signals are kept;
    Tier C (low confidence) are suppressed from the active list.
    """
    from app import config as app_config

    interval = app_config.get("signals.generation_interval_sec", 30)
    logger.info(f"Deterministic signal loop started (interval: {interval}s)")

    await asyncio.sleep(10)

    while True:
        try:
            await asyncio.sleep(interval)

            articles = list(app_state.news_articles)
            if not articles:
                continue

            signals = signal_engine.generate_deterministic_signals(articles)

            if signals:
                with app_state.lock:
                    existing = list(app_state.active_signals)
                    oracle_signals = [
                        s for s in existing
                        if s.get("source") in ("oracle_enhanced", "oracle")
                    ]
                    app_state.active_signals.clear()
                    for signal in signals[:50]:
                        signal["timestamp"] = datetime.now().isoformat()
                        app_state.active_signals.appendleft(signal)
                    for osig in oracle_signals:
                        osig["timestamp"] = datetime.now().isoformat()
                        app_state.active_signals.appendleft(osig)

                app_state.stats["signals_generated"] += len(signals)
                logger.debug(f"Deterministic: {len(signals)} signals generated")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Deterministic signal error: {type(e).__name__}: {e}")
            await asyncio.sleep(10)


async def oracle_signal_loop():
    """
    LAYER 2: ORACLE AI signal generation (every 10 min).

    Sends accumulated news + TA + commodity context to the ORACLE v2.1 LLM agent.
    The LLM produces the full three-tier output:
      - tier_a_signals: execution-quality signals (max 7)
      - tier_b_baskets: correlated instrument baskets
      - tier_c_universe: extended monitor list

    ORACLE signals are merged with deterministic signals and stored in oracle_meta.
    This runs infrequently to avoid rate-limit issues.
    """
    from app import config as app_config

    interval = app_config.get("signals.oracle_interval_sec", 600)
    initial_wait = app_config.get("oracle.initial_wait_sec", 30)
    logger.info(f"ORACLE signal loop started (interval: {interval}s, initial_wait: {initial_wait}s)")

    await asyncio.sleep(initial_wait)

    while True:
        try:
            articles = list(app_state.news_articles)
            if not articles:
                await asyncio.sleep(interval)
                continue

            oracle_output = await signal_engine.generate_oracle_signals(articles)

            if oracle_output:
                det_signals = list(app_state.active_signals)
                merged = signal_engine.merge_oracle_signals(det_signals, oracle_output)

                with app_state.lock:
                    app_state.active_signals.clear()
                    for signal in merged[:50]:
                        signal["timestamp"] = datetime.now().isoformat()
                        app_state.active_signals.appendleft(signal)

                    app_state.oracle_meta.update({
                        "regime": oracle_output.get("regime", {}),
                        "news_assessment": oracle_output.get("news_assessment", {}),
                        "market_outlook": oracle_output.get("market_outlook", ""),
                        "risk_warnings": oracle_output.get("risk_warnings", []),
                        "hedge_recommendations": oracle_output.get("hedge_recommendations", []),
                        "watchlist": oracle_output.get("watchlist", []),
                        "correlation_chains_traced": oracle_output.get("correlation_chains_traced", []),
                        "tier_b_baskets": oracle_output.get("tier_b_baskets", []),
                        "tier_c_universe": oracle_output.get("tier_c_universe", []),
                        "signals_suppressed": oracle_output.get("signals_suppressed", []),
                        "meta": oracle_output.get("meta", {}),
                        "last_oracle_timestamp": datetime.now().isoformat(),
                        "oracle_version": oracle_output.get("oracle_version", "2.1"),
                    })

                app_state.stats["llm_calls"] += 1
                app_state.stats["llm_last_success"] = datetime.now().isoformat()

                tier_a_count = len(oracle_output.get("tier_a_signals", []))
                tier_b_count = len(oracle_output.get("tier_b_baskets", []))
                tier_c_count = len(oracle_output.get("tier_c_universe", []))
                regime = oracle_output.get("regime", {}).get("code", "?")
                logger.info(
                    f"ORACLE v2.1 complete: "
                    f"tier_a={tier_a_count}, tier_b={tier_b_count} baskets, "
                    f"tier_c={tier_c_count}, regime={regime}"
                )

            await asyncio.sleep(interval)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"ORACLE signal error: {type(e).__name__}: {e}")
            app_state.stats["llm_errors"] += 1
            await asyncio.sleep(30)


async def broadcast_loop():
    """
    Broadcast full state to all connected WebSocket clients every 5s.

    Payload structure (v2.1):
      - news: latest 50 articles
      - signals: flat list of active Tier A/B signals (backward compatible)
      - prices: current Binance prices
      - stats: engine statistics
      - oracle_meta: full v2.1 tiered structure
    """
    from app import config as app_config

    interval = app_config.get("signals.broadcast_interval_sec", 5)
    logger.info(f"Broadcast loop started (interval: {interval}s)")

    await asyncio.sleep(5)

    while True:
        try:
            await asyncio.sleep(interval)

            if not app_state.websocket_clients:
                continue

            prices = {}
            for ticker, data in app_state.price_cache.items():
                prices[ticker] = {
                    "current": data.get("current", 0),
                    "change_24h": data.get("change_24h", 0),
                    "type": data.get("type", "crypto"),
                    "source": data.get("source", "binance"),
                }

            payload = {
                "type": "full_state",
                "news": list(app_state.news_articles)[:50],
                "signals": list(app_state.active_signals)[:30],
                "prices": prices,
                "stats": app_state.stats,
                "oracle_meta": app_state.oracle_meta,
                "timestamp": datetime.now().isoformat(),
            }

            raw = json.dumps(payload, default=str, ensure_ascii=False)
            disconnected = set()

            for ws in app_state.websocket_clients:
                try:
                    await ws.send_text(raw)
                except Exception:
                    disconnected.add(ws)

            app_state.websocket_clients -= disconnected
            if disconnected:
                logger.debug(f"Cleaned {len(disconnected)} disconnected WS clients")

            app_state.stats["signals_broadcast"] += 1

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Broadcast error: {type(e).__name__}: {e}")
            await asyncio.sleep(2)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """
    Serve the ORACLE v2.1 dashboard from static/index.html.

    This makes the app a self-contained single-service deployment:
    both the API (WebSocket + REST) and the frontend UI are served
    from the same FastAPI process on the same port. No separate
    web server or CDN needed for the dashboard.

    For production behind a reverse proxy (nginx/caddy), this route
    still works — nginx proxies everything to this FastAPI process.
    """
    html_path = Path(__file__).resolve().parent.parent / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse(
        content="<h1>ORACLE v2.1</h1><p>Dashboard not found. "
                "Place static/index.html in project root.</p>",
        status_code=404,
    )


@app.get("/api/health")
async def health():
    """
    Health check endpoint.
    Reports v2.1 version, tier counts, and engine statistics.
    """
    oracle_meta = app_state.oracle_meta
    meta = oracle_meta.get("meta", {})

    return JSONResponse({
        "status": "ok",
        "version": "2.1.0",
        "oracle_version": oracle_meta.get("oracle_version", "2.1"),
        "articles": len(app_state.news_articles),
        "signals": len(app_state.active_signals),
        "tier_a_count": meta.get("tier_a_count", 0),
        "tier_b_basket_count": meta.get("tier_b_basket_count", 0),
        "tier_c_count": meta.get("tier_c_count", 0),
        "prices": len(app_state.price_cache),
        "clients": len(app_state.websocket_clients),
        "regime": oracle_meta.get("regime", {}).get("code", "unknown"),
        "stats": app_state.stats,
    })


@app.get("/api/state")
async def get_state():
    """Full state REST endpoint for debugging and external consumers."""
    prices = {}
    for ticker, data in app_state.price_cache.items():
        prices[ticker] = {
            "current": data.get("current", 0),
            "change_24h": data.get("change_24h", 0),
            "type": data.get("type", "crypto"),
        }

    return JSONResponse({
        "type": "full_state",
        "news": list(app_state.news_articles)[:50],
        "signals": list(app_state.active_signals)[:30],
        "prices": prices,
        "stats": app_state.stats,
        "oracle_meta": app_state.oracle_meta,
        "timestamp": datetime.now().isoformat(),
    })


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    app_state.websocket_clients.add(websocket)
    logger.info(f"WS client connected (total: {len(app_state.websocket_clients)})")

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        app_state.websocket_clients.discard(websocket)
        logger.info(f"WS client disconnected (total: {len(app_state.websocket_clients)})")
