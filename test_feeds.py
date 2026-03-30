"""
Quick RSS feed tester script.
Tests all feeds from crypto_news.json and outputs working ones as JSON.
Run once, save results to verified_feeds.json, then integrate into backend.
"""
import asyncio
import json
import httpx
import feedparser

FEEDS_FILE = "crypto_news.json"
OUTPUT_FILE = "verified_feeds.json"

async def test_feed(session, url, name=""):
    try:
        resp = await session.get(url, follow_redirects=True, timeout=15)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        parsed = feedparser.parse(resp.text)
        entries = len(parsed.entries)
        if entries == 0:
            return None, "0 entries"
        return entries, "OK"
    except Exception as e:
        return None, str(e)[:60]

async def main():
    with open(FEEDS_FILE, "r", encoding="utf-8") as f:
        feeds = json.load(f)

    results = []
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    ) as client:
        tasks = []
        for item in feeds:
            rss = item.get("rss", "").strip()
            if not rss:
                continue
            if not rss.startswith("http"):
                continue
            tasks.append((item, rss))

        sem = asyncio.Semaphore(10)

        async def bounded_test(item, rss):
            async with sem:
                return item, rss, await test_feed(client, rss, item.get("url", ""))

        coros = [bounded_test(item, rss) for item, rss in tasks]
        completed = await asyncio.gather(*coros, return_exceptions=True)

        working = []
        for result in completed:
            if isinstance(result, Exception):
                continue
            item, rss, (entries, status) = result
            if entries is not None and entries > 0:
                working.append({
                    "url": item.get("url", ""),
                    "rss": rss,
                    "entries": entries,
                    "category": "crypto" if any(kw in rss.lower() for kw in ["coin", "crypto", "bitcoin", "blockchain", "token", "defi", "eth"]) else "markets"
                })
                print(f"  OK  [{entries:3d} entries] {rss}")
            else:
                print(f"FAIL [{status:>12s}] {rss}")

    working.sort(key=lambda x: x["entries"], reverse=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(working, f, indent=2, ensure_ascii=False)

    print(f"\n{len(working)} working feeds saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
