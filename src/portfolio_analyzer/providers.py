"""Optional providers. Network access occurs only when explicitly configured."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import re
import socket
import time
from urllib.parse import quote, urlparse
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
            if not any(t in response.getheader("Content-Type", "") for t in ("text/html", "text/plain", "xml", "json")):
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
        return await self._search(query, max_results, topic="general")

    async def search_events(self, query: str, max_results: int = 3, reporting_cutoff=None) -> list[Evidence]:
        return await self._search(query, max_results, topic="news", reporting_cutoff=reporting_cutoff)

    async def _search(self, query: str, max_results: int, *, topic: str, reporting_cutoff=None) -> list[Evidence]:
        import httpx
        payload = {"api_key": self.api_key, "query": query, "max_results": max_results,
                   "search_depth": "basic", "topic": topic}
        if topic == "news":
            if reporting_cutoff is None:
                payload["time_range"] = "month"
            else:
                payload["end_date"] = reporting_cutoff.date().isoformat()
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.post("https://api.tavily.com/search", json=payload)
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

    async def resolve_instrument(self, symbol: str) -> dict | None:
        """Return exact-symbol quote identity, or abstain on missing/ambiguous data."""
        def fetch():
            import yfinance as yf
            return yf.Ticker(symbol).get_info()
        info = await asyncio.to_thread(fetch)
        if not isinstance(info, dict) or str(info.get("symbol", "")).upper() != symbol.upper():
            return None
        type_map = {"EQUITY": "stock", "ETF": "etf", "MUTUALFUND": "mutual_fund", "INDEX": "index"}
        asset_type = type_map.get(str(info.get("quoteType", "")).upper())
        name = info.get("longName") or info.get("shortName")
        if not asset_type or not isinstance(name, str) or not name.strip():
            return None
        exchange = info.get("fullExchangeName") or info.get("exchange")
        return {"symbol": symbol, "asset_type": asset_type, "instrument_name": name.strip()[:200],
                "exchange": str(exchange).strip()[:40] if exchange else None,
                "sector": str(info["sector"]).strip()[:100] if asset_type == "stock" and info.get("sector") else None,
                "source": "yfinance", "resolved_at": datetime.now(timezone.utc).isoformat()}

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

    async def quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch daily-close quotes for many symbols in one batched yfinance request.

        Returns per-symbol ``price``, ``previous_close``, ``trading_date``,
        ``retrieved_at`` and ``source``. Symbols the provider does not return, or
        rows without a usable close, are omitted rather than invented.
        """
        unique = list(dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol and symbol.strip()))
        if not unique:
            return {}

        def fetch():
            import yfinance as yf
            return yf.download(" ".join(unique), period="5d", interval="1d", auto_adjust=False,
                               progress=False, threads=True, group_by="ticker", timeout=10)

        try:
            data = await asyncio.to_thread(fetch)
        except Exception:
            return {}
        if data is None or getattr(data, "empty", True):
            return {}
        from decimal import Decimal
        retrieved_at = datetime.now(timezone.utc).isoformat()
        multi = getattr(data.columns, "nlevels", 1) > 1
        quotes = {}
        for symbol in unique:
            try:
                series = data[(symbol, "Close")] if multi else data["Close"]
            except (KeyError, TypeError):
                continue
            series = series.dropna()
            if series.empty:
                continue
            try:
                price = Decimal(str(series.iloc[-1]))
                trading_date = str(series.index[-1].date())
                previous_close = Decimal(str(series.iloc[-2])) if len(series) > 1 else None
            except (ValueError, ArithmeticError, IndexError):
                continue
            quotes[symbol] = {"price": price, "previous_close": previous_close, "trading_date": trading_date,
                              "retrieved_at": retrieved_at, "source": "yfinance"}
        return quotes

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

    async def search(self, query: str, max_results: int = 3, reporting_cutoff=None) -> list[Evidence]:
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
                        if source.published_at and source.published_at.tzinfo is None:
                            source.published_at = source.published_at.replace(tzinfo=timezone.utc)
                    source.content_type = "official_feed"
                    found.append(source)
            except Exception:
                continue
        if self.fallback and len(found) < max_results:
            if reporting_cutoff is not None or hasattr(self.fallback, "search_events"):
                search = getattr(self.fallback, "search_events", None)
                if search:
                    found.extend(await search(query, max_results=max_results - len(found), reporting_cutoff=reporting_cutoff))
                else:
                    found.extend(await self.fallback.search(query, max_results=max_results - len(found)))
            else:
                found.extend(await self.fallback.search(query, max_results=max_results - len(found)))
        return found[:max_results]

    async def search_events(self, query: str, max_results: int = 3, reporting_cutoff=None) -> list[Evidence]:
        return await self.search(query, max_results=max_results, reporting_cutoff=reporting_cutoff)

    async def retrieve(self, source: Evidence) -> Evidence:
        content = await self._fetch(source.url)
        return source.model_copy(update={"excerpt": content, "content_type": "retrieved_page"})


def _dedupe_sources(sources: list[Evidence]) -> list[Evidence]:
    seen: set[str] = set()
    unique: list[Evidence] = []
    for source in sources:
        if source.url in seen:
            continue
        seen.add(source.url)
        unique.append(source)
    return unique


def _local_name(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _feed_datetime(value: str | None):
    if not value:
        return None
    from email.utils import parsedate_to_datetime
    try:
        parsed = parsedate_to_datetime(value)
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _feed_items(raw: str):
    from xml.etree import ElementTree
    root = ElementTree.fromstring(raw)
    return [element for element in root.iter() if _local_name(element.tag) in ("item", "entry")]


def _entry_value(entry, *names) -> str:
    accepted = {name.lower() for name in names}
    for child in list(entry):
        if _local_name(child.tag) in accepted:
            text = (child.text or "").strip()
            if text:
                return text
    return ""


def _entry_link(entry) -> str:
    for child in list(entry):
        if _local_name(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        if href:
            return href
        text = (child.text or "").strip()
        if text:
            return text
    return ""


def _entry_publisher(entry, fallback: str) -> str:
    for child in list(entry):
        if _local_name(child.tag) != "source":
            continue
        url = (child.get("url") or "").strip()
        host = urlparse(url).hostname if url else None
        if host:
            return host
    return fallback


def _entry_tickers(entry) -> set[str]:
    tickers: set[str] = set()
    for child in entry.iter():
        if _local_name(child.tag) != "tickers":
            continue
        for token in re.split(r"[,\s]+", (child.text or "").strip()):
            if token:
                tickers.add(token.upper())
    return tickers


def _recency_query(query: str, reporting_cutoff) -> str:
    if reporting_cutoff is None:
        return query
    after = (reporting_cutoff - timedelta(days=90)).date().isoformat()
    before = (reporting_cutoff + timedelta(days=1)).date().isoformat()
    return f"{query} after:{after} before:{before}"


def _decode_bing_link(link: str) -> str:
    from urllib.parse import parse_qs
    parsed = urlparse(link)
    if parsed.hostname and parsed.hostname.endswith("bing.com"):
        target = parse_qs(parsed.query).get("url", [""])[0]
        return target.strip()
    return link


class SecFilingsResearchProvider:
    """Per-company SEC filings from keyless official JSON endpoints.

    Requires an identifying contact user agent for SEC fair access; without one the
    provider abstains rather than guessing. Filings are trusted official_feed evidence
    and need no page retrieval.
    """

    TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
    SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

    def __init__(self, sec_user_agent: str | None = None, forms: tuple[str, ...] = ("8-K", "10-Q", "10-K"), ttl: int = 86400):
        self.sec_user_agent = sec_user_agent
        self.forms = tuple(form.upper() for form in forms)
        self.ttl = ttl
        self._tickers: dict[str, tuple[int, str]] = {}
        self._tickers_at = 0.0
        self._tickers_lock = asyncio.Lock()
        self._last_sec_request = 0.0

    def _enabled(self) -> bool:
        return bool(self.sec_user_agent and self.sec_user_agent.strip())

    async def _request_json(self, url: str):
        import json
        await asyncio.sleep(max(0.0, 0.2 - (time.monotonic() - self._last_sec_request)))
        self._last_sec_request = time.monotonic()
        raw = await retrieve_public_text(url, max_bytes=1_000_000, raw=True,
                                         user_agent=self.sec_user_agent or "PortfolioResearch/1.0")
        return json.loads(raw)

    async def _ticker_map(self) -> dict[str, tuple[int, str]]:
        async with self._tickers_lock:
            if self._tickers and time.monotonic() - self._tickers_at < self.ttl:
                return self._tickers
            payload = await self._request_json(self.TICKER_MAP_URL)
            rows = payload.values() if isinstance(payload, dict) else payload
            mapping: dict[str, tuple[int, str]] = {}
            for row in rows:
                try:
                    ticker = str(row["ticker"]).strip().upper()
                    cik = int(row["cik_str"])
                except (KeyError, TypeError, ValueError):
                    continue
                mapping[ticker] = (cik, str(row.get("title", "")).strip())
            self._tickers = mapping
            self._tickers_at = time.monotonic()
            return mapping

    @staticmethod
    def _symbol_from_query(query: str) -> str:
        match = re.search(r"\$?([A-Z]{1,5})(?![A-Za-z])", query or "")
        return match.group(1) if match else ""

    async def search_symbol(self, symbol: str, name: str | None = None, max_results: int = 4, reporting_cutoff=None) -> list[Evidence]:
        if not self._enabled() or max_results <= 0:
            return []
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return []
        try:
            mapping = await self._ticker_map()
        except Exception:
            return []
        entry = mapping.get(symbol) or mapping.get(symbol.replace("-", ".")) or mapping.get(symbol.replace(".", "-"))
        if entry is None:
            return []
        cik, company = entry
        try:
            payload = await self._request_json(self.SUBMISSIONS_URL.format(cik=f"{cik:010d}"))
        except Exception:
            return []
        recent = payload.get("filings", {}).get("recent") if isinstance(payload, dict) else None
        if not isinstance(recent, dict):
            return []
        forms = recent.get("form") or []
        accessions = recent.get("accessionNumber") or []
        dates = recent.get("filingDate") or []
        results: list[Evidence] = []
        for index, form in enumerate(forms):
            form = str(form).upper()
            if form not in self.forms or index >= len(accessions) or index >= len(dates):
                continue
            published = _feed_datetime(str(dates[index]))
            if reporting_cutoff is not None and (published is None or published > reporting_cutoff):
                continue
            accession = str(accessions[index]).replace("-", "")
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/"
            label = " ".join(part for part in (symbol, company) if part).strip()
            results.append(Evidence(
                id=hashlib.sha256(url.encode()).hexdigest()[:16], url=url,
                title=f"{symbol} {form} filing"[:500], publisher="sec.gov", published_at=published,
                excerpt=f"{label} filed a {form} on {dates[index]}."[:5000], content_type="official_feed"))
            if len(results) >= max_results:
                break
        return results

    async def search(self, query: str, max_results: int = 3, reporting_cutoff=None) -> list[Evidence]:
        symbol = self._symbol_from_query(query)
        if not symbol:
            return []
        return await self.search_symbol(symbol, None, max_results=max_results, reporting_cutoff=reporting_cutoff)

    async def search_events(self, query: str, max_results: int = 3, reporting_cutoff=None) -> list[Evidence]:
        return await self.search(query, max_results=max_results, reporting_cutoff=reporting_cutoff)


class TickerNewsFeedProvider:
    """Keyless, dated headline feeds for per-company news (Google News / Nasdaq).

    Aggregator links are never page-retrieved; items are supplied as dated ``news_feed``
    evidence only, so no ``retrieved_page`` is produced for them.
    """

    SOURCES = ("google_news", "nasdaq", "bing_news")
    GOOGLE_NEWS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    NASDAQ_URL = "https://nasdaq.com/feed/rssoutbound?symbol={symbol}"
    BING_NEWS_URL = "https://www.bing.com/news/search?q={query}&format=RSS"

    def __init__(self, sources=("google_news", "nasdaq"), timeout: float = 15, ttl: int = 300, fallback=None):
        if isinstance(sources, str):
            sources = (sources,)
        self.sources = tuple(source for source in sources if source in self.SOURCES)
        self.timeout = timeout
        self.ttl = ttl
        self.fallback = fallback
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    async def _fetch(self, url: str) -> str:
        async with self._lock:
            cached = self._cache.get(url)
            if cached and time.monotonic() - cached[0] < self.ttl:
                return cached[1]
            raw = await asyncio.wait_for(retrieve_public_text(url, max_bytes=500_000, raw=True), timeout=self.timeout)
            self._cache[url] = (time.monotonic(), raw)
            return raw

    @staticmethod
    def _symbol_from_query(query: str) -> str:
        match = re.search(r"\$?([A-Z]{1,5})(?![A-Za-z])", query or "")
        return match.group(1) if match else ""

    @staticmethod
    def _symbol_query(symbol: str, name: str | None) -> str:
        if name and name.strip():
            return f'"{name.strip()}" OR {symbol} stock'
        return f"{symbol} stock"

    def _to_evidence(self, entry, symbol, name, reporting_cutoff, require_tickers: bool = False, bing: bool = False):
        link = _entry_link(entry)
        if bing:
            link = _decode_bing_link(link)
        if not link or not link.startswith("https://"):
            return None
        published = _feed_datetime(_entry_value(entry, "pubDate", "published", "updated", "date"))
        if published is None:
            return None
        if reporting_cutoff is not None and published > reporting_cutoff:
            return None
        if require_tickers:
            tickers = _entry_tickers(entry)
            if not tickers or not symbol or symbol.upper() not in tickers:
                return None
        title = _entry_value(entry, "title") or "News update"
        summary = _entry_value(entry, "description", "summary", "content")
        host = urlparse(link).hostname or "news"
        publisher = _entry_publisher(entry, host)
        label = " ".join(part for part in (symbol, name) if part).strip()
        excerpt = f"{label}: {summary}" if label else summary
        return Evidence(id=hashlib.sha256(link.encode()).hexdigest()[:16], url=link, title=title[:500],
                        publisher=publisher[:200], published_at=published, excerpt=excerpt.strip()[:5000],
                        content_type="news_feed")

    async def _source_entries(self, source: str, query: str, symbol: str, name: str | None, reporting_cutoff) -> list[Evidence]:
        if source == "google_news":
            url = self.GOOGLE_NEWS_URL.format(query=quote(_recency_query(query, reporting_cutoff), safe=""))
            entries = _feed_items(await self._fetch(url))
            return [item for entry in entries if (item := self._to_evidence(entry, symbol, name, reporting_cutoff)) is not None]
        if source == "nasdaq":
            if not symbol:
                return []
            url = self.NASDAQ_URL.format(symbol=quote(symbol, safe=""))
            entries = _feed_items(await self._fetch(url))
            return [item for entry in entries
                    if (item := self._to_evidence(entry, symbol, name, reporting_cutoff, require_tickers=True)) is not None]
        if source == "bing_news":
            url = self.BING_NEWS_URL.format(query=quote(_recency_query(query, reporting_cutoff), safe=""))
            entries = _feed_items(await self._fetch(url))
            return [item for entry in entries
                    if (item := self._to_evidence(entry, symbol, name, reporting_cutoff, bing=True)) is not None]
        return []

    async def search_symbol(self, symbol: str, name: str | None = None, max_results: int = 4, reporting_cutoff=None) -> list[Evidence]:
        symbol = (symbol or "").strip().upper()
        if not symbol or max_results <= 0:
            return []
        query = self._symbol_query(symbol, name)
        results: list[Evidence] = []
        seen: set[str] = set()
        for source in self.sources:
            try:
                entries = await self._source_entries(source, query, symbol, name, reporting_cutoff)
            except Exception:
                continue
            for entry in entries:
                if entry.url in seen:
                    continue
                seen.add(entry.url)
                results.append(entry)
                if len(results) >= max_results:
                    return results
        return await self._supplement(query, seen, results, max_results, reporting_cutoff)

    async def search(self, query: str, max_results: int = 3) -> list[Evidence]:
        return await self._generic(query, max_results, None)

    async def search_events(self, query: str, max_results: int = 3, reporting_cutoff=None) -> list[Evidence]:
        return await self._generic(query, max_results, reporting_cutoff)

    async def _generic(self, query: str, max_results: int, reporting_cutoff) -> list[Evidence]:
        if max_results <= 0:
            return []
        symbol = self._symbol_from_query(query)
        results: list[Evidence] = []
        seen: set[str] = set()
        for source in self.sources:
            try:
                entries = await self._source_entries(source, query, symbol, None, reporting_cutoff)
            except Exception:
                continue
            for entry in entries:
                if entry.url in seen:
                    continue
                seen.add(entry.url)
                results.append(entry)
                if len(results) >= max_results:
                    return results
        return await self._supplement(query, seen, results, max_results, reporting_cutoff)

    async def _supplement(self, query, seen, results, max_results, reporting_cutoff):
        if self.fallback is None or len(results) >= max_results:
            return results[:max_results]
        try:
            handler = getattr(self.fallback, "search_events", None)
            if handler is not None:
                extra = await handler(query, max_results=max_results - len(results), reporting_cutoff=reporting_cutoff)
            else:
                extra = await self.fallback.search(query, max_results=max_results - len(results))
        except Exception:
            return results[:max_results]
        for entry in extra:
            if entry.url not in seen:
                seen.add(entry.url)
                results.append(entry)
            if len(results) >= max_results:
                break
        return results[:max_results]


class CompositeResearchProvider:
    """Ordered research providers; official feeds are consulted first, RSS news supplements them."""

    def __init__(self, providers):
        self.providers = [provider for provider in providers if provider is not None]

    async def search(self, query: str, max_results: int = 3) -> list[Evidence]:
        results: list[Evidence] = []
        for provider in self.providers:
            search = getattr(provider, "search", None)
            if search is None:
                continue
            try:
                results.extend(await search(query, max_results=max_results))
            except Exception:
                continue
        return _dedupe_sources(results)[:max_results]

    async def search_events(self, query: str, max_results: int = 3, reporting_cutoff=None) -> list[Evidence]:
        results: list[Evidence] = []
        for provider in self.providers:
            handler = getattr(provider, "search_events", None)
            try:
                if handler is not None:
                    results.extend(await handler(query, max_results=max_results, reporting_cutoff=reporting_cutoff))
                elif hasattr(provider, "search"):
                    results.extend(await provider.search(query, max_results=max_results))
            except Exception:
                continue
        return _dedupe_sources(results)[:max_results]

    async def search_symbol(self, symbol: str, name: str | None = None, max_results: int = 4, reporting_cutoff=None) -> list[Evidence]:
        results: list[Evidence] = []
        for provider in self.providers:
            handler = getattr(provider, "search_symbol", None)
            if handler is None:
                continue
            try:
                results.extend(await handler(symbol, name, max_results=max_results, reporting_cutoff=reporting_cutoff))
            except Exception:
                continue
        return _dedupe_sources(results)[:max_results]

    async def retrieve(self, source: Evidence) -> Evidence:
        # Official and headline evidence is trusted as supplied; never fetch an aggregator page.
        if source.content_type in {"official_feed", "news_feed"}:
            return source
        for provider in self.providers:
            handler = getattr(provider, "retrieve", None)
            if handler is not None:
                return await handler(source)
        return source


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
