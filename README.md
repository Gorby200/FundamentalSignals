# FundamentalSignals — ORACLE v2.1

**Real-time financial signal generation engine** that transforms live market data and RSS news into actionable, risk-adjusted trading signals with a two-layer pipeline: deterministic algorithms + AI-powered analysis.

---

## Overview

ORACLE v2.1 is a modular FastAPI backend that:

- Connects to **Binance WebSocket** for real-time crypto prices (8 pairs)
- Polls **28+ RSS feeds** for financial news (CoinDesk, CoinTelegraph, Reuters, etc.)
- Runs **technical analysis** on live price data (RSI, MACD, Bollinger Bands, SMA, ATR)
- Generates **two layers of signals**:
  - **LAYER 1 — Deterministic** (every 30s): algorithmic TA + news sentiment + commodity correlation
  - **LAYER 2 — ORACLE AI** (every 5 min): LLM-powered three-tier analysis with regime detection
- Broadcasts everything via **WebSocket** to a real-time HTML dashboard

All tuneable parameters live in a single `config/settings.json` — zero hardcoded magic numbers.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        DATA SOURCES                              │
│  Binance WebSocket (8 pairs)          RSS Feeds (28 sources)     │
│  BTC, ETH, SOL, BNB, XRP,            CoinDesk, Reuters,         │
│  ADA, DOGE, LTC                       CoinTelegraph, etc.        │
└──────────────┬──────────────────────────────────┬───────────────┘
               │                                  │
               ▼                                  ▼
┌──────────────────────┐           ┌──────────────────────────┐
│    price_engine      │           │      news_engine          │
│  Real-time ticks,    │           │  Article scraping,        │
│  24h high/low/change │           │  deduplication, ticker    │
│  500-tick buffer     │           │  extraction, sentiment    │
└──────────┬───────────┘           └────────────┬──────────────┘
           │                                    │
           ▼                                    ▼
┌──────────────────────────────────────────────────────────────┐
│                      SIGNAL ENGINE                            │
│                                                              │
│  ┌─────────────────────┐    ┌──────────────────────────────┐ │
│  │  LAYER 1 (30s)      │    │  LAYER 2 (5 min)             │ │
│  │  Deterministic      │    │  ORACLE AI (LLM)             │ │
│  │                     │    │                               │ │
│  │  technical.py       │    │  analyst_agent.py             │ │
│  │  → SMA, RSI, MACD   │    │  → LangGraph StateGraph      │ │
│  │  → BB, ATR, SL/TP   │    │  → z.ai GLM-4.5-air          │ │
│  │                     │    │  → SuperPrompt v2.1           │ │
│  │  sentiment.py       │    │  → Tier A/B/C signals         │ │
│  │  → keyword scoring  │    │  → Regime detection           │ │
│  │                     │    │  → Correlation chains         │ │
│  │  commodity_engine   │    │  → Market outlook             │ │
│  │  → 15×15 corr matrix│    │  → Risk warnings             │ │
│  └─────────┬───────────┘    └──────────────┬───────────────┘ │
│            │                                │                 │
│            └──────────┬─────────────────────┘                 │
│                       ▼                                       │
│              Merge & Deduplicate                               │
│              ATR-based SL/TP                                   │
│              Tier assignment (A/B/C)                           │
└───────────────────────┬──────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────┐
│                   FastAPI + WebSocket                         │
│                                                              │
│  /                  — dashboard UI (static/index.html)        │
│  /api/health        — system health & stats                  │
│  /api/state         — full JSON state dump                   │
│  /ws                — real-time WebSocket broadcast (5s)      │
└──────────────────────────────────────────────────────────────┘
```

---

## Two-Layer Signal Pipeline

### Layer 1: Deterministic (every 30 seconds)

Pure algorithmic — no LLM calls, fast and cheap:

1. **Technical analysis** — RSI(14), MACD, Bollinger Bands %B, SMA crossover, price velocity
2. **News sentiment** — keyword-based scoring, ticker extraction, sentiment labeling
3. **Commodity correlation** — 15×15 correlation matrix with decay weighting
4. **Signal scoring** — weighted combination (40% news, 60% technical)
5. **ATR-based SL/TP** — ATR from 24h high/low range, R:R ratios 1:1 / 1:2 / 1:3

### Layer 2: ORACLE AI (every 5 minutes)

LLM-powered deep analysis using LangGraph StateGraph pipeline:

1. **gather_context()** — builds rich structured context:
   - Section 1: Live market data (8 pairs, prices, 24h ranges, ATR)
   - Section 2: Technical analysis (RSI, MACD, BB with labels)
   - Section 3: News (20 articles, full summaries, sentiment)
   - Section 4: Commodity correlations
2. **llm_analyze()** — sends context + superprompt to z.ai GLM-4.5-air
3. **parse_oracle_output()** — extracts JSON, validates schema

### Tier System

| Tier | Threshold | Description | Action |
|------|-----------|-------------|--------|
| **A** | confidence ≥ 0.55 | Execution-ready signals with entry/SL/TP | Act immediately |
| **B** | confidence ≥ 0.40 | Correlated instrument baskets | Monitor, secondary entry |
| **C** | confidence < 0.40 | Extended watchlist | Observation only |

Thresholds are configurable via `oracle.tier_a_threshold` / `oracle.tier_b_threshold`.

---

## Quick Start

### Prerequisites

- Python 3.11+
- pip

### Installation

```bash
# Clone the repository
git clone https://github.com/Gorby200/FundamentalSignals.git
cd Prototype

# Create virtual environment
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate

# Activate (macOS/Linux)
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration

Edit `config/settings.json`:

```json
{
  "zai": {
    "api_key": "YOUR_ZAI_API_KEY_HERE",
    "base_url": "https://api.z.ai/api/coding/paas/v4",
    "model": "glm-4.5-air"
  }
}
```

All parameters are documented inline in `config/settings.json`. Key sections:

| Section | Purpose | Key Parameters |
|---------|---------|----------------|
| `zai` | LLM API config | api_key, model, max_tokens, temperature |
| `server` | HTTP server | host, port |
| `feeds` | RSS polling | rss_poll_interval_sec, rss_timeout_sec |
| `signals` | Pipeline timing | generation_interval_sec, oracle_interval_sec |
| `deterministic` | Layer 1 weights | news_weight, technical_weight, atr_sl_multiplier |
| `oracle` | Layer 2 params | tier_a/b_threshold, consensus_boost, batch_size |
| `binance` | WS connection | symbols list, ping intervals |
| `logging` | Log config | level, file/console toggles, llm_feed log |

### Run

```bash
python run.py
```

Server starts at `http://localhost:8000`.

- **Dashboard**: open `http://localhost:8000/` in a browser — served by FastAPI
- **Health**: `http://localhost:8000/api/health`
- **State**: `http://localhost:8000/api/state`

---

## API Endpoints

### `GET /`

Serves the ORACLE v2.1 dashboard UI from `static/index.html`.

### `GET /api/health`

System health check. Returns version, signal counts, regime, uptime stats.

```json
{
  "status": "ok",
  "version": "2.1.0",
  "signals": 12,
  "tier_a_count": 3,
  "regime": "risk_on",
  "prices": 8,
  "clients": 1
}
```

### `GET /api/state`

Full state dump — all news, signals, prices, oracle metadata.

### `WebSocket /ws`

Real-time broadcast every 5 seconds. Payload:

```json
{
  "type": "full_state",
  "news": [...],
  "signals": [...],
  "prices": {...},
  "stats": {...},
  "oracle_meta": {
    "regime": {...},
    "tier_b_baskets": [...],
    "tier_c_universe": [...],
    "market_outlook": "...",
    "risk_warnings": [...]
  }
}
```

---

## Project Structure

```
Prototype/
├── run.py                          # Entry point — starts uvicorn
├── requirements.txt                # Python dependencies
├── .gitignore
│
├── app/
│   ├── __init__.py
│   ├── main.py                     # FastAPI app, lifespan, loops, endpoints
│   ├── config.py                   # Dot-notation config loader (settings.json)
│   ├── state.py                    # Thread-safe AppState container
│   ├── logging_config.py           # Centralized logging setup
│   │
│   ├── engines/
│   │   ├── __init__.py
│   │   ├── price_engine.py         # Binance WebSocket listener (8 pairs)
│   │   ├── news_engine.py          # RSS polling (28 feeds), article processing
│   │   ├── technical.py            # Pure TA functions (SMA, RSI, MACD, BB, ATR)
│   │   ├── sentiment.py            # Keyword-based news sentiment scorer
│   │   ├── commodity_engine.py     # Cross-asset correlation matrix
│   │   └── signal_engine.py        # Two-layer pipeline, ATR/SL/TP, merge
│   │
│   └── agents/
│       ├── __init__.py
│       └── analyst_agent.py        # LangGraph ORACLE agent (gather → LLM → parse)
│
├── config/
│   └── settings.json               # ALL tuneable parameters (zero hardcoded)
│
├── prompts/
│   └── FundamentalSignals_SuperPrompt_v2.md   # ORACLE v2.1 system prompt
│
├── static/
│   └── index.html                  # Dashboard UI (served by FastAPI at /)
│
├── logs/
│   └── .gitkeep
│
├── verified_feeds.json             # Verified RSS feed URLs
└── test_feeds.py                   # Utility: verify RSS feed connectivity
```

---

## Technology Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Backend | FastAPI + Uvicorn | Async HTTP + WebSocket server |
| Real-time prices | Binance WebSocket | 8 crypto pairs, tick-level |
| News | RSS via feedparser + httpx | 28 financial feeds |
| Technical Analysis | NumPy | SMA, EMA, RSI, MACD, BB, ATR |
| AI Agent | LangGraph + LangChain | StateGraph pipeline for LLM |
| LLM | z.ai GLM-4.5-air | Signal analysis, regime detection |
| Dashboard | Vanilla HTML/CSS/JS | Served by FastAPI, no build step |

---

## Signal Output Schema

Each signal includes:

```json
{
  "instrument": {
    "ticker": "BTC-USD",
    "name": "Bitcoin",
    "type": "crypto",
    "exchange": "Binance"
  },
  "direction": "BUY",
  "confidence": 0.72,
  "tier": "A",
  "entry": 66601.00,
  "stop_loss": 66184.33,
  "tp1": 67017.67,
  "tp2": 67434.33,
  "tp3": 67851.00,
  "atr": 277.78,
  "atr_source": "24h_range",
  "reasoning": "Strong bullish momentum with RSI confirming...",
  "sources": ["technical", "news"],
  "timestamp": "2026-03-30T12:00:00"
}
```

---

## ATR / Stop-Loss / Take-Profit

Stop-loss and take-profit levels are computed using ATR (Average True Range):

- **ATR source priority**: 24h high/low range → candle ATR-14 → avg True Range → % fallback
- **ATR from 24h range**: `(high_24h - low_24h) / 3.5` with sanity clamp (0.3%–8% of price)
- **Risk**: `ATR × atr_sl_multiplier` (default 1.5)
- **Stop-loss**: `entry ± risk`
- **Take-profit levels**: R:R ratios — TP1 = 1:1, TP2 = 1:2, TP3 = 1:3

---

## ORACLE Data Pipeline

The LLM receives a richly structured context to prevent hallucination:

1. **LIVE MARKET DATA** — real-time prices, 24h change, 24h high/low, ATR for all 8 pairs
2. **TECHNICAL ANALYSIS** — RSI, MACD, BB with human-readable labels and support/resistance levels
3. **NEWS** — up to 20 articles with full summaries (not truncated), sentiment scores, tickers
4. **COMMODITY CORRELATIONS** — cross-asset signals when triggered

Anti-hallucination guardrails:
- Explicit instruction: "Use ONLY provided prices, NOT training data"
- SuperPrompt Section 0.5 defines data boundaries
- HumanMessage lists 7 critical data boundary rules

---

## Logging

| Log File | Content |
|----------|---------|
| `logs/oracle.log` | General system events, signal generation, errors |
| `logs/llm_feed.log` | Full LLM I/O — context sent + raw response + parsed output |

LLM feed logging is invaluable for debugging signal quality. Every ORACLE call logs:
- Articles in batch
- Full context sent to LLM
- Raw LLM response
- Parsed output summary (tier counts, regime, top signals)

---

## Production Deployment

The app is a **self-contained single-service** — FastAPI serves both the REST/WebSocket API and the dashboard UI from the same process on the same port. No separate frontend server needed.

### Architecture for Deployment

```
Internet → [nginx/Caddy reverse proxy] → [uvicorn FastAPI :8000]
                                              ├── /            → dashboard (static/index.html)
                                              ├── /ws          → WebSocket broadcast
                                              ├── /api/health  → health check
                                              └── /api/state   → state dump
```

### Quick Deploy (VPS)

**Option 1: Direct on VPS**

```bash
# On your server
git clone https://github.com/Gorby200/FundamentalSignals.git
cd Prototype
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Edit config — add your ZAI API key
nano config/settings.json

# Run with uvicorn (production)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

**Option 2: Behind nginx (recommended)**

```nginx
server {
    listen 80;
    server_name oracle.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

The dashboard automatically uses `wss://` when served over HTTPS — no configuration needed.

### How Dynamic URLs Work

The frontend derives all endpoints from `window.location`:

```javascript
// Automatically detects http/https, ws/wss, and current host
var API_BASE = window.location.origin;
var WS_PROTOCOL = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
var wsUrl = WS_PROTOCOL + '//' + window.location.host + '/ws';
```

This means the same deployment works on:
- `http://localhost:8000` (local dev)
- `http://192.168.1.100:8000` (LAN)
- `https://oracle.example.com` (public domain with SSL)
- Any reverse proxy setup

---

## License

Proprietary — all rights reserved.
