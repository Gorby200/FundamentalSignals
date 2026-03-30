"""
app/engines/news_engine.py — RSS News Polling Engine.

Architecture:
  - Async polling of multiple RSS feeds at configurable intervals.
  - Each feed has its own timeout, category, and reliability rating.
  - Articles are deduplicated by title hash to prevent signal duplication.
  - Verified crypto feeds from verified_feeds.json are loaded lazily.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any

import httpx
import feedparser

from app.engines.sentiment import process_article, generate_news_slug

logger = logging.getLogger("fundamentalsignals.news")

CORE_RSS_FEEDS: List[Dict[str, Any]] = [
    {"name": "Thomson Reuters", "url": "https://ir.thomsonreuters.com/rss/news-releases.xml", "category": "markets", "reliability": "high", "timeout": 20},
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex", "category": "markets", "reliability": "high", "timeout": 20},
    {"name": "MarketWatch", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories", "category": "markets", "reliability": "high", "timeout": 20},
    {"name": "Seeking Alpha", "url": "https://seekingalpha.com/market_currents.xml", "category": "markets", "reliability": "medium", "timeout": 20},
    {"name": "CNBC Markets", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "category": "markets", "reliability": "high", "timeout": 20},
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "category": "crypto", "reliability": "high", "timeout": 20},
    {"name": "Google Business", "url": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB", "category": "markets", "reliability": "medium", "timeout": 20},
    {"name": "Investing.com", "url": "https://www.investing.com/rss/news_301.rss", "category": "markets", "reliability": "medium", "timeout": 20},
]

CRYPTO_RSS_FEEDS: List[Dict[str, Any]] = []

_VERIFIED_FEEDS_PATH = (
    Path(__file__).resolve().parent.parent.parent / "verified_feeds.json"
)
_CRYPTO_FEEDS_LOADED = False

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def _ensure_crypto_feeds_loaded():
    global CRYPTO_RSS_FEEDS, _CRYPTO_FEEDS_LOADED
    if _CRYPTO_FEEDS_LOADED:
        return
    _CRYPTO_FEEDS_LOADED = True

    if not _VERIFIED_FEEDS_PATH.exists():
        logger.warning(f"Verified feeds file not found: {_VERIFIED_FEEDS_PATH}")
        return

    try:
        with open(_VERIFIED_FEEDS_PATH, "r", encoding="utf-8") as f:
            verified = json.load(f)

        for item in verified[:20]:
            rss_url = item.get("rss", "").strip()
            if not rss_url.startswith("http"):
                continue
            CRYPTO_RSS_FEEDS.append({
                "name": item.get("url", "Crypto Feed"),
                "url": rss_url,
                "category": "crypto",
                "reliability": "medium",
                "timeout": 15,
            })

        logger.info(f"Loaded {len(CRYPTO_RSS_FEEDS)} verified crypto feeds")
    except Exception as e:
        logger.warning(f"Failed to load verified feeds: {e}")


async def poll_feed(
    client: httpx.AsyncClient,
    feed_config: Dict[str, Any],
    seen_slugs: set,
    max_articles: int = 15,
    max_age_hours: int = 4,
) -> List[Dict[str, Any]]:
    url = feed_config["url"]
    timeout = feed_config.get("timeout", 20)

    try:
        resp = await client.get(url, timeout=timeout)
        if resp.status_code != 200:
            logger.debug(f"Feed {feed_config['name']} returned HTTP {resp.status_code}")
            return []

        parsed = feedparser.parse(resp.text)
        if not parsed.entries:
            return []

        new_articles = []

        for entry in parsed.entries[:max_articles]:
            title = getattr(entry, "title", "").strip()
            if not title:
                continue

            slug = generate_news_slug(title)
            if slug in seen_slugs:
                continue

            summary = ""
            if hasattr(entry, "summary"):
                summary = entry.summary
            elif hasattr(entry, "description"):
                summary = entry.description

            summary = re.sub(r'<[^>]+>', ' ', summary).strip()
            summary = re.sub(r'\s+', ' ', summary)[:500]

            published = getattr(entry, "published", "") or getattr(entry, "updated", "")
            link = getattr(entry, "link", "")

            raw = {
                "title": title,
                "summary": summary,
                "link": link,
                "published": published,
                "source": feed_config["name"],
                "category": feed_config["category"],
                "reliability": feed_config.get("reliability", "medium"),
                "timestamp": datetime.now().isoformat(),
            }

            processed = process_article(raw)
            if processed:
                new_articles.append(processed)
                seen_slugs.add(slug)

        return new_articles

    except (httpx.TimeoutException, httpx.ConnectError):
        return []
    except Exception as e:
        logger.debug(f"Feed {feed_config['name']} error: {e}")
        return []


async def poll_all_feeds(
    seen_slugs: set,
    poll_interval: int = 60,
) -> List[Dict[str, Any]]:
    _ensure_crypto_feeds_loaded()

    all_feeds = CORE_RSS_FEEDS + CRYPTO_RSS_FEEDS
    all_articles = []

    async with httpx.AsyncClient(headers=HTTP_HEADERS, follow_redirects=True) as client:
        sem = asyncio.Semaphore(8)

        async def bounded_poll(feed):
            async with sem:
                return await poll_feed(client, feed, seen_slugs)

        tasks = [bounded_poll(feed) for feed in all_feeds]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, list):
                all_articles.extend(result)

    all_articles.sort(key=lambda x: abs(x.get("sentiment_score", 0)), reverse=True)

    logger.info(f"Polled {len(all_feeds)} feeds, found {len(all_articles)} new articles")
    return all_articles
