"""Optional providers. Network access occurs only when explicitly configured."""
import asyncio
import hashlib
import ipaddress
import socket
import time
from urllib.parse import urlparse
from .models import Evidence, PortfolioSnapshot


def validate_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Only public HTTPS URLs are permitted")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Private or reserved network destinations are forbidden")
    return url


async def retrieve_public_text(url: str, max_bytes: int = 200_000, *, raw: bool = False, user_agent: str = "PortfolioResearch/1.0") -> str:
    """Fetch with DNS pinning to close validation/connection rebinding gaps."""
    import http.client
    import ssl

    def fetch() -> str:
        validate_public_url(url)
        parsed = urlparse(url)
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        address = addresses[0][4][0]
        if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError("Unsafe DNS destination")
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=15, context=ssl.create_default_context())
        # The TCP destination is pinned while TLS still verifies the original hostname.
        def connect():
            sock = socket.create_connection((address, 443), timeout=15)
            connection.sock = connection._context.wrap_socket(sock, server_hostname=parsed.hostname)
        connection.connect = connect
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request("GET", path, headers={"User-Agent": user_agent, "Accept": "text/html,text/plain,application/atom+xml,application/rss+xml,application/xml"})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("Source unavailable or redirected")
            if not any(t in response.getheader("Content-Type", "") for t in ("text/html", "text/plain", "xml")):
                raise ValueError("Unsupported source content type")
            content = response.read(max_bytes + 1)
            if len(content) > max_bytes:
                content = content[:max_bytes]
            if raw:
                return content.decode("utf-8", errors="replace")
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content, "html.parser")
            for element in soup(["script", "style", "nav", "footer"]):
                element.decompose()
            return soup.get_text(" ", strip=True)[:20000]
        finally:
            connection.close()
    return await asyncio.to_thread(fetch)


def _evidence(url: str, title: str, excerpt: str) -> Evidence:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Search returned an invalid source URL")
    return Evidence(id=hashlib.sha256(url.encode()).hexdigest()[:16], url=url, title=title[:500], publisher=parsed.hostname, excerpt=excerpt[:5000])


class TavilyResearchProvider:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def search(self, query: str, max_results: int = 3) -> list[Evidence]:
        import httpx
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.post("https://api.tavily.com/search", json={"api_key": self.api_key, "query": query, "max_results": max_results, "search_depth": "basic"})
            response.raise_for_status()
            results = []
            for row in response.json().get("results", []):
                source = _evidence(row["url"], row.get("title", "Source"), row.get("content", ""))
                if row.get("published_date"):
                    from datetime import datetime, timezone
                    from email.utils import parsedate_to_datetime
                    try:
                        published = datetime.fromisoformat(row["published_date"].replace("Z", "+00:00"))
                    except ValueError:
                        try:
                            published = parsedate_to_datetime(row["published_date"])
                        except (ValueError, TypeError):
                            published = None
                    if published:
                        source.published_at = published if published.tzinfo else published.replace(tzinfo=timezone.utc)
                results.append(source)
            return results

    async def retrieve(self, source: Evidence) -> Evidence:
        content = await retrieve_public_text(source.url)
        return source.model_copy(update={"excerpt": content, "content_type": "retrieved_page"})


class DDGSResearchProvider:
    async def search(self, query: str, max_results: int = 3) -> list[Evidence]:
        def search():
            from ddgs import DDGS
            return [_evidence(row["href"], row.get("title", "Source"), row.get("body", "")) for row in DDGS(timeout=15).text(query, max_results=max_results)]
        return await asyncio.to_thread(search)

    async def retrieve(self, source: Evidence) -> Evidence:
        content = await retrieve_public_text(source.url)
        return source.model_copy(update={"excerpt": content, "content_type": "retrieved_page"})


class YFinanceMarketDataProvider:
    def __init__(self, refresh_existing: bool = True):
        self.refresh_existing = refresh_existing

    async def enrich(self, snapshot: PortfolioSnapshot) -> PortfolioSnapshot:
        result, _ = await self.enrich_with_metadata(snapshot)
        return result

    async def enrich_with_metadata(self, snapshot: PortfolioSnapshot) -> tuple[PortfolioSnapshot, dict]:
        """Keep quote observation dates separate from snapshot collection time."""
        from datetime import datetime, timezone
        result = snapshot.model_copy(deep=True)
        metadata = {}
        def fetch(symbol):
            import yfinance as yf
            return yf.Ticker(symbol).history(period="5d", auto_adjust=False, timeout=10)
        for position in result.positions:
            if self.refresh_existing or position.price is None:
                history = await asyncio.to_thread(fetch, position.symbol)
                if not history.empty:
                    from decimal import Decimal
                    position.price = Decimal(str(history["Close"].iloc[-1]))
                    metadata[position.symbol] = {"source": "yfinance", "valuation_type": "daily_close", "trading_date": str(history.index[-1].date()), "retrieved_at": datetime.now(timezone.utc).isoformat()}
                    if len(history) > 1:
                        position.previous_close = Decimal(str(history["Close"].iloc[-2]))
        return result, metadata

    async def history(self, snapshot: PortfolioSnapshot) -> dict[str, dict[str, float]]:
        def fetch(symbol):
            import yfinance as yf
            prices = yf.Ticker(symbol).history(period="1y", auto_adjust=True, timeout=10)
            return {str(date.date()): float(price) for date, price in prices["Close"].items()} if not prices.empty else {}
        results = {}
        for symbol in dict.fromkeys(p.symbol for p in snapshot.positions):
            history = await asyncio.to_thread(fetch, symbol)
            if history:
                results[symbol] = history
        return results


class PrimaryFeedResearchProvider:
    """Official disclosure and economic feeds, optionally supplementing broad search.

    Supply an identifying contact user agent to enable SEC fair-access requests.
    SEC's current feed is not exhaustive company filings coverage.
    """
    def __init__(self, fallback=None, sec_user_agent: str | None = None):
        self.fallback = fallback
        self.sec_user_agent = sec_user_agent
        self._feed_cache: dict[str, tuple[float, str]] = {}
        self._feed_lock = asyncio.Lock()
        self._last_sec_request = 0.0

    async def _fetch(self, url: str, *, raw: bool = False) -> str:
        async with self._feed_lock:
            cached = self._feed_cache.get(url) if raw else None
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            hostname = urlparse(url).hostname or ""
            if hostname == "sec.gov" or hostname.endswith(".sec.gov"):
                await asyncio.sleep(max(0, 0.2 - (time.monotonic() - self._last_sec_request)))
                self._last_sec_request = time.monotonic()
            content = await retrieve_public_text(url, max_bytes=500_000 if raw else 200_000, raw=raw, user_agent=self.sec_user_agent or "PortfolioResearch/1.0")
            if raw:
                self._feed_cache[url] = (time.monotonic(), content)
            return content

    async def search(self, query: str, max_results: int = 3) -> list[Evidence]:
        import re
        from xml.etree import ElementTree
        from email.utils import parsedate_to_datetime
        from datetime import datetime
        feeds = ["https://www.federalreserve.gov/feeds/press_all.xml", "https://www.bls.gov/feed/bls_latest.rss"]
        if self.sec_user_agent:
            feeds.append("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&output=atom")
        terms = {word.lower() for word in re.findall(r"[A-Za-z]{3,}", query)} - {"latest", "investor", "relations", "material"}
        found = []
        for url in feeds:
            try:
                raw = await self._fetch(url, raw=True)
                root = ElementTree.fromstring(raw)
                for entry in list(root.iter("item")) + list(root.iter("{http://www.w3.org/2005/Atom}entry")):
                    def value(name):
                        node = entry.find(name)
                        if node is None:
                            node = entry.find("{http://www.w3.org/2005/Atom}" + name)
                        return (node.text or "").strip() if node is not None else ""
                    title, summary = value("title"), value("description") or value("summary")
                    if terms and not any(term in (title + " " + summary).lower() for term in terms):
                        continue
                    link = value("link")
                    if not link:
                        node = entry.find("{http://www.w3.org/2005/Atom}link")
                        link = node.get("href", "") if node is not None else ""
                    source = _evidence(link, title, summary)
                    published = value("pubDate") or value("updated")
                    if published:
                        try:
                            source.published_at = parsedate_to_datetime(published)
                        except ValueError:
                            source.published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    source.content_type = "official_feed"
                    found.append(source)
            except Exception:
                continue
        if self.fallback and len(found) < max_results:
            found.extend(await self.fallback.search(query, max_results=max_results - len(found)))
        return found[:max_results]

    async def retrieve(self, source: Evidence) -> Evidence:
        content = await self._fetch(source.url)
        return source.model_copy(update={"excerpt": content, "content_type": "retrieved_page"})


class AlphaVantageResearchProvider:
    """Optional NEWS_SENTIMENT adapter; paid access may be required."""
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def search(self, query: str, max_results: int = 3) -> list[Evidence]:
        import httpx
        from datetime import datetime, timezone
        symbol = query.split()[0].upper()
        if not symbol.replace(".", "").replace("-", "").isalnum():
            raise ValueError("Invalid ticker")
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get("https://www.alphavantage.co/query", params={"function": "NEWS_SENTIMENT", "tickers": symbol, "limit": max_results, "apikey": self.api_key})
            response.raise_for_status()
            body = response.json()
            if "feed" not in body:
                raise ValueError("News access unavailable or quota exhausted")
            result = []
            for row in body["feed"][:max_results]:
                source = _evidence(row["url"], row.get("title", "News"), row.get("summary", ""))
                if row.get("time_published"):
                    source.published_at = datetime.strptime(row["time_published"], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                result.append(source)
            return result

    async def retrieve(self, source: Evidence) -> Evidence:
        content = await retrieve_public_text(source.url)
        return source.model_copy(update={"excerpt": content, "content_type": "retrieved_page"})
