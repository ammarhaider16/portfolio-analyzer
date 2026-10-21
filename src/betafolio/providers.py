"""Optional providers. Network access occurs only when explicitly configured."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import html
import ipaddress
import re
import socket
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
from weakref import WeakKeyDictionary
from urllib.parse import quote, urlparse, urljoin
from .models import Evidence, PortfolioSnapshot
from .source_status import record_outcome, safe_source_error, valid_sec_contact


_SOURCE_WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="public-source")
SOURCE_REQUEST_SECONDS = 15.0
SOURCE_CHILD_SECONDS = 6.0
_RUN_SOURCE_LIMIT = ContextVar("run_source_limit", default=None)
_SOURCE_SLOT_OWNER = ContextVar("source_slot_owner", default=None)
_LOOP_SOURCE_LIMITS = WeakKeyDictionary()


@contextmanager
def source_network_scope(limit=4):
    """Install one source-operation limit shared by every task in a run."""
    token = _RUN_SOURCE_LIMIT.set(asyncio.Semaphore(limit))
    try:
        yield
    finally:
        _RUN_SOURCE_LIMIT.reset(token)


@asynccontextmanager
async def source_network_slot():
    """Bound leaf adapters; nested work in the same task reuses its slot."""
    task = asyncio.current_task()
    if _SOURCE_SLOT_OWNER.get() is task:
        yield
        return
    limiter = _RUN_SOURCE_LIMIT.get()
    if limiter is None:
        loop = asyncio.get_running_loop()
        limiter = _LOOP_SOURCE_LIMITS.setdefault(loop, asyncio.Semaphore(4))
    async with limiter:
        token = _SOURCE_SLOT_OWNER.set(task)
        try:
            yield
        finally:
            _SOURCE_SLOT_OWNER.reset(token)


async def _run_source_worker(function, *args):
    """Uncancellable library calls retain the same bounded worker capacity."""
    cancelled = threading.Event()
    def call():
        if cancelled.is_set():
            raise TimeoutError("Source request cancelled before starting")
        return function(*args)
    future = asyncio.get_running_loop().run_in_executor(_SOURCE_WORKERS, call)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        cancelled.set()
        future.cancel()
        raise


class SourceHTTPError(ValueError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class SourceAccessError(ValueError):
    """A classified provider error containing only safe, predefined metadata."""
    def __init__(self, category: str, reason: str):
        self.category = category
        self.safe_reason = reason
        super().__init__(reason)


def validate_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Only public HTTPS URLs are permitted")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Private or reserved network destinations are forbidden")
    return url


async def retrieve_public_text(url: str, max_bytes: int = 200_000, *, raw: bool = False, user_agent: str = "PortfolioResearch/1.0") -> str:
    async with source_network_slot():
        return await _retrieve_public_text(url, max_bytes, raw=raw, user_agent=user_agent)


async def retrieve_public_url(url: str, *, max_bytes: int = 2048, user_agent: str = "PortfolioResearch/1.0") -> str:
    """Follow redirects with the pinned transport and return the landing URL."""
    async with source_network_slot():
        return await _retrieve_public_text(url, max_bytes, raw=True, user_agent=user_agent, landed=True)


async def _retrieve_public_text(url: str, max_bytes: int = 200_000, *, raw: bool = False, user_agent: str = "PortfolioResearch/1.0", landed: bool = False) -> str:
    """Fetch with DNS pinning to close validation/connection rebinding gaps."""
    import http.client
    import ssl

    cancelled = threading.Event()
    state_lock = threading.Lock()
    active = [None]
    deadline = time.monotonic() + SOURCE_REQUEST_SECONDS

    def stop():
        cancelled.set()
        with state_lock:
            sock = active[0]
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

    def remaining():
        seconds = deadline - time.monotonic()
        if cancelled.is_set() or seconds <= 0:
            raise TimeoutError("Source request timed out")
        return seconds

    def track(sock):
        with state_lock:
            if cancelled.is_set():
                sock.close()
                raise TimeoutError("Source request timed out")
            active[0] = sock

    def fetch_one(current_url):
        remaining()
        hostname = (urlparse(current_url).hostname or "").lower().rstrip(".")
        is_sec = hostname == "sec.gov" or hostname.endswith(".sec.gov")
        if is_sec and not valid_sec_contact(user_agent):
            raise SourceAccessError("configuration", "SEC filings were not checked because an identifying contact is missing or invalid")
        validate_public_url(current_url)
        remaining()
        parsed = urlparse(current_url)
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        address = addresses[0][4][0]
        if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError("Unsafe DNS destination")
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=remaining(), context=ssl.create_default_context())
        # The TCP destination is pinned while TLS still verifies the original hostname.
        def connect():
            sock = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM)
            track(sock)
            sock.settimeout(remaining())
            sock.connect((address, 443))
            tls_sock = connection._context.wrap_socket(sock, server_hostname=parsed.hostname, do_handshake_on_connect=False)
            track(tls_sock)
            connection.sock = tls_sock
            tls_sock.settimeout(remaining())
            tls_sock.do_handshake()
        connection.connect = connect
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            # A contact supplied for SEC is never forwarded to another origin.
            request_agent = user_agent if is_sec or not valid_sec_contact(user_agent) else "PortfolioResearch/1.0"
            connection.request("GET", path, headers={"User-Agent": request_agent, "Accept": "text/html,text/plain,application/atom+xml,application/rss+xml,application/xml"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise SourceHTTPError(response.status)
                return None, urljoin(current_url, location)
            if response.status != 200:
                raise SourceHTTPError(response.status)
            if not any(t in response.getheader("Content-Type", "") for t in ("text/html", "text/plain", "xml", "json")):
                raise ValueError("Unsupported source content type")
            content = response.read(max_bytes + 1)
            remaining()
            if len(content) > max_bytes:
                content = content[:max_bytes]
            if raw:
                return content.decode("utf-8", errors="replace"), None
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content, "html.parser")
            for element in soup(["script", "style", "nav", "footer"]):
                element.decompose()
            return soup.get_text(" ", strip=True)[:20000], None
        finally:
            connection.close()
            with state_lock:
                if active[0] is not None:
                    active[0].close()
                active[0] = None
    def fetch():
        remaining()  # Cancelled/expired queued work must never start a request.
        timer = threading.Timer(remaining(), stop)
        timer.daemon = True
        timer.start()
        try:
            current_url = url
            seen = set()
            for _ in range(4):
                if current_url in seen:
                    raise SourceAccessError("redirect", "Source redirect loop")
                seen.add(current_url)
                content, target = fetch_one(current_url)
                if target is None:
                    return content, current_url
                current_url = target
            raise SourceAccessError("redirect", "Source redirect limit exceeded")
        except Exception as error:
            if cancelled.is_set():
                raise TimeoutError("Source request timed out") from error
            raise
        finally:
            timer.cancel()
    future = asyncio.get_running_loop().run_in_executor(_SOURCE_WORKERS, fetch)
    try:
        content, final_url = await asyncio.shield(future)
        return final_url if landed else content
    except asyncio.CancelledError:
        stop()
        future.cancel()
        raise


def _evidence(url: str, title: str, excerpt: str) -> Evidence:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Search returned an invalid source URL")
    from .news import clean_text, EXCERPT_LIMIT
    return Evidence(id=hashlib.sha256(url.encode()).hexdigest()[:16], url=url, title=clean_text(title)[:500] or "Source",
                    publisher=parsed.hostname, excerpt=clean_text(excerpt)[:EXCERPT_LIMIT])


def is_aggregator_url(url: str) -> bool:
    """True only for redirector/aggregator hosts, never for publisher or mirror hosts."""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if not host:
        return False
    return (host == "bing.com" or host.endswith(".bing.com") or host == "google.com"
            or host.endswith(".google.com") or host.startswith("news.google."))


def _google_article_target(link: str) -> str:
    """Decode the publisher URL embedded in older news.google.com article ids when present."""
    import base64
    parsed = urlparse(link)
    if not (parsed.hostname or "").lower().startswith("news.google.") or "/articles/" not in parsed.path:
        return ""
    token = parsed.path.rsplit("/articles/", 1)[1].split("/", 1)[0]
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        return ""
    match = re.search(rb"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]{6,500}", raw)
    if not match:
        return ""
    target = match.group(0).decode("ascii", "ignore").rstrip(".,;)")
    return target if target.startswith("https://") and not is_aggregator_url(target) else ""


def _summary_link(summary: str) -> str:
    """First publisher link in a feed description, ignoring aggregator redirect markup."""
    for href in re.findall(r"""href=["']([^"']+)["']""", html.unescape(summary or "")):
        href = href.strip()
        if href.startswith("https://") and not is_aggregator_url(href):
            return href
    return ""


async def resolve_feed_link(url: str) -> str:
    """Best-effort aggregator redirect resolution with a short timeout; "" when unresolved."""
    if not is_aggregator_url(url):
        return url
    try:
        landed = await asyncio.wait_for(retrieve_public_url(url), timeout=3.0)
    except Exception:
        return ""
    return landed if landed.startswith("https://") and not is_aggregator_url(landed) else ""


async def resolve_feed_links(items: list[Evidence], budget: float = 4.0) -> list[Evidence]:
    """Replace aggregator links with publisher pages at ingestion when resolvable."""
    pending = [item for item in items if is_aggregator_url(item.url)]
    if not pending:
        return items
    async def one(item):
        return item, await resolve_feed_link(item.url)
    try:
        async with asyncio.timeout(budget):
            outcomes = await asyncio.gather(*(one(item) for item in pending), return_exceptions=True)
    except TimeoutError:
        return items
    resolved_by_id = {}
    for outcome in outcomes:
        if isinstance(outcome, tuple):
            item, resolved = outcome
            if resolved:
                resolved_by_id[item.id] = resolved
    from urllib.parse import urlparse as _urlparse
    updated = []
    for item in items:
        resolved = resolved_by_id.get(item.id)
        if not resolved:
            updated.append(item)
            continue
        update = {"url": resolved}
        # The landing host is the true publisher when the feed only named an aggregator.
        if not item.publisher or is_aggregator_url(f"https://{item.publisher}"):
            update["publisher"] = _urlparse(resolved).hostname or item.publisher
        updated.append(item.model_copy(update=update))
    return updated


def _count_hygiene(provider, counts: dict) -> None:
    if not counts:
        return
    if not hasattr(provider, "hygiene_counts"):
        provider.hygiene_counts = {}
    for key, value in counts.items():
        provider.hygiene_counts[key] = provider.hygiene_counts.get(key, 0) + value


def source_hygiene_counts(provider) -> dict:
    """Per-run ingestion filter counts, including nested adapters, for engine diagnostics."""
    if provider is None:
        return {}
    result = dict(getattr(provider, "hygiene_counts", {}))
    for child in list(getattr(provider, "providers", [])) + [getattr(provider, "fallback", None)]:
        for key, value in source_hygiene_counts(child).items():
            result[key] = result.get(key, 0) + value
    return result


def _hygiene_filter(items, reference=None, max_age_hours=None):
    from .news import source_hygiene
    return source_hygiene(items, reference=reference, max_age_hours=max_age_hours)


async def _finalize_sources(provider, items, reference=None):
    """FR-16 hygiene then FR-4 link resolution, before evidence registration."""
    kept, counts = _hygiene_filter(items, reference=reference,
                                   max_age_hours=getattr(provider, "max_data_age_hours", None))
    _count_hygiene(provider, counts)
    return await resolve_feed_links(kept)


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
            return await resolve_feed_links(results)

    async def retrieve(self, source: Evidence) -> Evidence:
        content = await retrieve_public_text(source.url)
        return source.model_copy(update={"excerpt": content, "content_type": "retrieved_page"})


class DDGSResearchProvider:
    async def search(self, query: str, max_results: int = 3) -> list[Evidence]:
        def search():
            from ddgs import DDGS
            return [_evidence(row["href"], row.get("title", "Source"), row.get("body", "")) for row in DDGS(timeout=15).text(query, max_results=max_results)]
        return await resolve_feed_links(await _run_source_worker(search))

    async def retrieve(self, source: Evidence) -> Evidence:
        content = await retrieve_public_text(source.url)
        return source.model_copy(update={"excerpt": content, "content_type": "retrieved_page"})


class YFinanceMarketDataProvider:
    def __init__(self, refresh_existing: bool = True):
        self.refresh_existing = refresh_existing
        self._info_cache = {}

    async def resolve_instrument(self, symbol: str) -> dict | None:
        """Return exact-symbol quote identity, or abstain on missing/ambiguous data."""
        info = await self._info(symbol)
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

    async def _info(self, symbol):
        cached = self._info_cache.get(symbol)
        if cached and time.monotonic()-cached[0] < 30:
            return cached[1]
        def fetch():
            import yfinance as yf
            return yf.Ticker(symbol).get_info()
        info = await _run_source_worker(fetch)
        self._info_cache[symbol] = (time.monotonic(), info)
        return info

    async def enrich_with_metadata(self, snapshot: PortfolioSnapshot) -> tuple[PortfolioSnapshot, dict]:
        from datetime import date
        result = snapshot.model_copy(deep=True)
        symbols = [position.symbol for position in result.positions if self.refresh_existing or position.price is None]
        quotes = await self.quotes(symbols)
        metadata = {}
        for position in result.positions:
            quote = quotes.get(position.symbol)
            if not quote or not quote_is_newer(quote, position):
                continue
            position.price, position.previous_close = quote['price'], quote['previous_close']
            position.price_session = date.fromisoformat(quote['trading_date'])
            position.price_as_of = datetime.fromisoformat(quote['quote_as_of']) if quote.get('quote_as_of') else None
            position.price_source = quote['source']
            metadata[position.symbol] = {key:value for key,value in quote.items() if key not in {'price','previous_close'}}
        return result, metadata

    async def quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Use timestamped regular-session quotes, falling back honestly to dated closes."""
        from zoneinfo import ZoneInfo
        unique = list(dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip()))
        async def one(symbol):
            now = datetime.now(timezone.utc)
            candidate = None
            try:
                async with asyncio.timeout(15):
                    info = await self._info(symbol)
                price = valid_market_price(info.get('regularMarketPrice'))
                previous = valid_market_price(info.get('regularMarketPreviousClose',info.get('previousClose')))
                stamp = info.get('regularMarketTime')
                if price is not None and isinstance(stamp,(int,float)) and stamp > 0:
                    observed = datetime.fromtimestamp(stamp,timezone.utc)
                    zone = ZoneInfo(info.get('exchangeTimezoneName') or 'America/New_York')
                    if observed <= now:
                        candidate = {'price':price, 'previous_close':previous,
                            'trading_date':observed.astimezone(zone).date().isoformat(),
                            'quote_as_of':observed.isoformat(), 'retrieved_at':now.isoformat(),
                            'source':'yfinance', 'valuation_type':'regular_session'}
                        if candidate['trading_date'] >= expected_market_session(now):
                            return symbol, candidate
            except Exception:
                pass
            def fetch():
                import yfinance as yf
                return yf.Ticker(symbol).history(period='5d',interval='1d',auto_adjust=False,timeout=10)
            try:
                history = await _run_source_worker(fetch)
                series = history['Close'].dropna()
                series = series[series.map(lambda value:valid_market_price(value) is not None)]
                if series.empty:
                    return symbol, candidate
                if candidate and candidate['trading_date'] >= str(series.index[-1].date()):
                    return symbol, candidate
                return symbol, {'price':valid_market_price(series.iloc[-1]),
                    'previous_close':valid_market_price(series.iloc[-2]) if len(series)>1 else None,
                    'trading_date':str(series.index[-1].date()), 'quote_as_of':None,
                    'retrieved_at':now.isoformat(), 'source':'yfinance', 'valuation_type':'daily_close'}
            except Exception:
                return symbol, candidate
        values = await asyncio.gather(*(one(symbol) for symbol in unique))
        return {symbol:quote for symbol,quote in values if quote}

    async def history(self, snapshot: PortfolioSnapshot) -> dict[str, dict[str, float]]:
        def fetch(symbol):
            import yfinance as yf
            prices = yf.Ticker(symbol).history(period="1y", auto_adjust=True, timeout=10)
            return {str(date.date()): float(price) for date, price in prices["Close"].items()} if not prices.empty else {}
        results = {}
        for symbol in dict.fromkeys(p.symbol for p in snapshot.positions):
            history = await _run_source_worker(fetch, symbol)
            if history:
                results[symbol] = history
        return results


def quote_is_newer(quote, position):
    """Never downgrade a stored observation to an older session or timestamp."""
    session = quote.get('trading_date')
    if position.price_session:
        saved = position.price_session.isoformat()
        if not session or session < saved:
            return False
        if session == saved and position.price_as_of:
            stamp = quote.get('quote_as_of')
            return bool(stamp and datetime.fromisoformat(stamp) >= position.price_as_of)
    return True


def expected_market_session(now=None):
    """The US session currently trading, or the last session before today's open."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    import exchange_calendars as calendars
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo('America/New_York'))
    calendar = calendars.get_calendar('XNYS', start=(local.date()-timedelta(days=40)).isoformat(),
                                     end=(local.date()+timedelta(days=10)).isoformat())
    session = calendar.date_to_session(local.date().isoformat(), direction='previous')
    if calendar.session_open(session).to_pydatetime() > now:
        session = calendar.previous_session(session)
    return session.date().isoformat()


def valid_market_price(value):
    """Accept only finite positive provider quotes before they reach mutable models."""
    from decimal import Decimal, InvalidOperation
    try:
        price = Decimal(str(value))
        return price if price.is_finite() and price > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


class PrimaryFeedResearchProvider:
    """Official disclosure and economic feeds, optionally supplementing broad search.

    Supply an identifying contact user agent to enable SEC fair-access requests.
    SEC's current feed is not exhaustive company filings coverage.
    """
    FEEDS = {"fed": "https://www.federalreserve.gov/feeds/press_all.xml"}

    def __init__(self, fallback=None, sec_user_agent: str | None = None, sources=None):
        self.fallback = fallback
        self.sec_user_agent = sec_user_agent
        self._feed_cache: dict[str, tuple[float, str]] = {}
        self._feed_lock = asyncio.Lock()
        self._last_sec_request = 0.0
        self.source_outcomes = {}
        self.hygiene_counts = {}
        self.max_data_age_hours = None
        self.sources = tuple(source for source in (sources if sources is not None else self.FEEDS) if source in self.FEEDS)

    async def _fetch(self, url: str, *, raw: bool = False) -> str:
        async with self._feed_lock:
            cached = self._feed_cache.get(url) if raw else None
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            hostname = (urlparse(url).hostname or "").lower().rstrip(".")
            is_sec = hostname == "sec.gov" or hostname.endswith(".sec.gov")
            if is_sec:
                await asyncio.sleep(max(0, 0.2 - (time.monotonic() - self._last_sec_request)))
                self._last_sec_request = time.monotonic()
            content = await retrieve_public_text(url, max_bytes=500_000 if raw else 200_000, raw=raw,
                                                user_agent=(self.sec_user_agent or "PortfolioResearch/1.0") if is_sec else "PortfolioResearch/1.0")
            if raw:
                self._feed_cache[url] = (time.monotonic(), content)
            return content

    async def search(self, query: str, max_results: int = 3, reporting_cutoff=None) -> list[Evidence]:
        import re
        from xml.etree import ElementTree
        from email.utils import parsedate_to_datetime
        from datetime import datetime
        feeds = [(source, self.FEEDS[source]) for source in self.sources]
        # SEC company data has its own explicitly enabled, contact-gated provider.
        terms = {word.lower() for word in re.findall(r"[A-Za-z]{3,}", query)} - {"latest", "investor", "relations", "material"}
        found = []
        for source_id, url in feeds:
            before = len(found)
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
                record_outcome(self, source_id, "passed" if len(found) > before else "empty", "" if len(found) > before else "No matching official updates")
            except Exception as error:
                record_outcome(self, source_id, "failed", safe_source_error(error))
                continue
        if self.fallback and len(found) < max_results:
            before_fallback = len(found)
            try:
                found.extend(await _probe_call(self.fallback, query, max_results=max_results - len(found), reporting_cutoff=reporting_cutoff))
                record_outcome(self.fallback, _source_id(self.fallback), "passed" if len(found) > before_fallback else "empty", "" if len(found) > before_fallback else "No matching updates")
            except Exception as error:
                record_outcome(self.fallback, _source_id(self.fallback), "failed", safe_source_error(error))
        found, counts = _hygiene_filter(found, reference=reporting_cutoff,
                                        max_age_hours=getattr(self, "max_data_age_hours", None))
        _count_hygiene(self, counts)
        return found[:max_results]

    async def search_events(self, query: str, max_results: int = 3, reporting_cutoff=None) -> list[Evidence]:
        return await self.search(query, max_results=max_results, reporting_cutoff=reporting_cutoff)

    async def retrieve(self, source: Evidence) -> Evidence:
        content = await self._fetch(source.url)
        return source.model_copy(update={"excerpt": content, "content_type": "retrieved_page"})


def _dedupe_sources(sources: list[Evidence]) -> list[Evidence]:
    indices: dict[str, int] = {}
    unique: list[Evidence] = []
    grades = {"search_excerpt": 0, "news_feed": 1, "retrieved_page": 2, "official_feed": 2}
    for source in sources:
        if source.url in indices:
            index = indices[source.url]
            if grades.get(source.content_type, 0) > grades.get(unique[index].content_type, 0):
                unique[index] = source
            continue
        indices[source.url] = len(unique)
        unique.append(source)
    return unique


def _interleave_candidates(groups, limit=36):
    """Give each consulted adapter/feed a bounded share before story clustering."""
    return _dedupe_sources([group[index] for index in range(max((len(group) for group in groups), default=0))
                            for group in groups if index < len(group)])[:limit]


async def _partial_source_groups(requests):
    """Return completed sources before the caller's overall discovery timeout."""
    tasks = [asyncio.create_task(request) for request in requests]
    try:
        done, pending = await asyncio.wait(tasks, timeout=SOURCE_CHILD_SECONDS) if tasks else (set(), set())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return [task.result() for task in tasks if task in done and not task.cancelled() and task.exception() is None]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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


def _entry_source_url(entry) -> str:
    """The RSS source element link; a full publisher path can replace an aggregator redirect."""
    for child in list(entry):
        if _local_name(child.tag) != "source":
            continue
        url = (child.get("url") or "").strip()
        if url.startswith("https://") and urlparse(url).path not in ("", "/"):
            return url
    return ""


def _title_publisher(title: str) -> str:
    from .news import clean_text
    match = re.match(r"^.*\s+[|–—-]\s+(\S(?:.*\S)?)$", clean_text(title))
    return match.group(1).strip() if match else ""


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
        self.source_outcomes = {}
        self.hygiene_counts = {}
        self.max_data_age_hours = None
        if not self._enabled():
            record_outcome(self, "sec_filings", "skipped", "SEC filings were not checked because an identifying contact is missing or invalid")

    def _enabled(self) -> bool:
        return valid_sec_contact(self.sec_user_agent)

    async def _request_json(self, url: str):
        import json
        if not self._enabled():
            raise ValueError("SEC identifying contact is missing or invalid")
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
        except Exception as error:
            record_outcome(self, "sec_filings", "failed", safe_source_error(error))
            return []
        entry = mapping.get(symbol) or mapping.get(symbol.replace("-", ".")) or mapping.get(symbol.replace(".", "-"))
        if entry is None:
            record_outcome(self, "sec_filings", "empty", "No SEC company match for this symbol")
            return []
        cik, company = entry
        try:
            payload = await self._request_json(self.SUBMISSIONS_URL.format(cik=f"{cik:010d}"))
        except Exception as error:
            record_outcome(self, "sec_filings", "failed", safe_source_error(error))
            return []
        recent = payload.get("filings", {}).get("recent") if isinstance(payload, dict) else None
        if not isinstance(recent, dict):
            record_outcome(self, "sec_filings", "empty", "No recent filings returned")
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
        results, counts = _hygiene_filter(results, reference=reporting_cutoff,
                                          max_age_hours=getattr(self, "max_data_age_hours", None))
        _count_hygiene(self, counts)
        record_outcome(self, "sec_filings", "passed" if results else "empty", "" if results else "No matching recent filings")
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
    NASDAQ_URL = "https://www.nasdaq.com/feed/rssoutbound?symbol={symbol}"
    BING_NEWS_URL = "https://www.bing.com/news/search?q={query}&format=RSS"

    def __init__(self, sources=("google_news", "nasdaq"), timeout: float = 15, ttl: int = 300, fallback=None):
        if isinstance(sources, str):
            sources = (sources,)
        self.sources = tuple(source for source in sources if source in self.SOURCES)
        self.timeout = timeout
        self.ttl = ttl
        self.fallback = fallback
        # Optional freshness window (hours); with none set, only the 30-day hard rule applies.
        self.max_data_age_hours = None
        self.hygiene_counts = {}
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    async def _fetch(self, url: str) -> str:
        async with self._lock:
            cached = self._cache.get(url)
            if cached and time.monotonic() - cached[0] < self.ttl:
                return cached[1]
            async with asyncio.timeout(self.timeout):
                raw = await retrieve_public_text(url, max_bytes=500_000, raw=True)
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
        from .news import clean_feed_title, clean_text, EXCERPT_LIMIT
        title = _entry_value(entry, "title") or "News update"
        summary = _entry_value(entry, "description", "summary", "content")
        # FR-4: prefer the publisher page over an aggregator redirect when the feed carries it.
        if is_aggregator_url(link):
            for candidate in (_summary_link(summary), _google_article_target(link), _entry_source_url(entry)):
                if candidate and candidate.startswith("https://") and not is_aggregator_url(candidate):
                    link = candidate
                    break
        host = urlparse(link).hostname or "news"
        publisher = _entry_publisher(entry, "")
        if not publisher and not is_aggregator_url(link):
            publisher = host
        if not publisher:
            # Unresolved aggregator link: the title suffix still names the true publisher.
            publisher = _title_publisher(title) or host
        # FR-5: evidence fields carry the item's own text only; never the queried symbol/name.
        return Evidence(id=hashlib.sha256(link.encode()).hexdigest()[:16], url=link,
                        title=clean_feed_title(title, publisher)[:500],
                        publisher=publisher[:200], published_at=published,
                        excerpt=clean_text(summary)[:EXCERPT_LIMIT],
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

    async def search_symbol_candidates(self, symbol, name=None, max_results=12, reporting_cutoff=None):
        symbol = (symbol or "").strip().upper()
        if not symbol or max_results <= 0:
            return []
        return await self._candidates(self._symbol_query(symbol, name), symbol, name, max_results, reporting_cutoff)

    async def search_events_candidates(self, query, max_results=12, reporting_cutoff=None):
        return await self._candidates(query, self._symbol_from_query(query), None, max_results, reporting_cutoff)

    async def _candidates(self, query, symbol, name, max_results, reporting_cutoff):
        if max_results <= 0:
            return []
        groups = []
        for feed in self.sources:
            try:
                entries = await self._source_entries(feed, query, symbol, name, reporting_cutoff)
                record_outcome(self, feed, "passed" if entries else "empty", "" if entries else "No matching ticker news")
                groups.append(entries[:min(max_results, 12)])
            except Exception as error:
                record_outcome(self, feed, "failed", safe_source_error(error))
        if self.fallback is not None:
            try:
                groups.append((await _probe_call(self.fallback, query, max_results=min(max_results, 12), reporting_cutoff=reporting_cutoff))[:12])
            except Exception as error:
                record_outcome(self.fallback, _source_id(self.fallback), "failed", safe_source_error(error))
        return await _finalize_sources(self, _interleave_candidates(groups), reference=reporting_cutoff)

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
                record_outcome(self, source, "passed" if entries else "empty", "" if entries else "No matching ticker news")
            except Exception as error:
                record_outcome(self, source, "failed", safe_source_error(error))
                continue
            for entry in entries:
                if entry.url in seen:
                    continue
                seen.add(entry.url)
                results.append(entry)
                if len(results) >= max_results:
                    return await _finalize_sources(self, results, reference=reporting_cutoff)
        return await _finalize_sources(self, await self._supplement(query, seen, results, max_results, reporting_cutoff), reference=reporting_cutoff)

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
                record_outcome(self, source, "passed" if entries else "empty", "" if entries else "No matching ticker news")
            except Exception as error:
                record_outcome(self, source, "failed", safe_source_error(error))
                continue
            for entry in entries:
                if entry.url in seen:
                    continue
                seen.add(entry.url)
                results.append(entry)
                if len(results) >= max_results:
                    return await _finalize_sources(self, results, reference=reporting_cutoff)
        return await _finalize_sources(self, await self._supplement(query, seen, results, max_results, reporting_cutoff), reference=reporting_cutoff)

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
        self.hygiene_counts = {}
        self.max_data_age_hours = None

    async def search_symbol_candidates(self, symbol, name=None, max_results=12, reporting_cutoff=None):
        return await self._candidates("search_symbol", symbol, name=name, max_results=max_results, reporting_cutoff=reporting_cutoff)

    async def search_events_candidates(self, query, max_results=12, reporting_cutoff=None):
        return await self._candidates("search_events", query, max_results=max_results, reporting_cutoff=reporting_cutoff)

    async def _candidates(self, method, query, *, name=None, max_results=12, reporting_cutoff=None):
        async def collect(provider):
            if method == "search_events" and not supports_market_wide(provider):
                return []
            handler = getattr(provider, method + "_candidates", None) or getattr(provider, method, None)
            try:
                async with source_network_slot():
                    async with asyncio.timeout(SOURCE_CHILD_SECONDS):
                        kwargs = {"max_results": min(max_results, 12), "reporting_cutoff": reporting_cutoff}
                        if handler is None:
                            items = await _probe_call(provider, f"{query} {name or ''} latest news" if method == "search_symbol" else query, **kwargs)
                        elif method == "search_symbol":
                            kwargs["name"] = name
                            items = await handler(query, **kwargs)
                        else:
                            items = await handler(query, **kwargs)
                if not isinstance(provider, (SecFilingsResearchProvider, PrimaryFeedResearchProvider, TickerNewsFeedProvider)):
                    record_outcome(provider, _source_id(provider), "passed" if items else "empty", "" if items else "No matching updates")
                return items
            except Exception as error:
                ids = provider.sources if isinstance(provider, (PrimaryFeedResearchProvider, TickerNewsFeedProvider)) else (_source_id(provider),)
                for source in ids:
                    record_outcome(provider, source, "failed", safe_source_error(error))
                return []
        # Flatten fallback/feed adapters so each source has an independent bound.
        adapters = [adapter for child in self.providers for adapter in research_tool_sources(child).values()]
        groups = await _partial_source_groups(collect(adapter) for adapter in adapters)
        return await _finalize_sources(self, _interleave_candidates(groups), reference=reporting_cutoff)

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
        return await _finalize_sources(self, _dedupe_sources(results)[:max_results])

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
        return await _finalize_sources(self, _dedupe_sources(results)[:max_results], reference=reporting_cutoff)

    async def search_symbol(self, symbol: str, name: str | None = None, max_results: int = 4, reporting_cutoff=None) -> list[Evidence]:
        results: list[Evidence] = []
        for provider in self.providers:
            handler = getattr(provider, "search_symbol", None)
            try:
                if handler is None:
                    results.extend(await _probe_call(provider, f"{symbol} {name or ''} latest news", max_results=max_results, reporting_cutoff=reporting_cutoff))
                else:
                    results.extend(await handler(symbol, name, max_results=max_results, reporting_cutoff=reporting_cutoff))
            except Exception:
                continue
        return await _finalize_sources(self, _dedupe_sources(results)[:max_results], reference=reporting_cutoff)

    async def retrieve(self, source: Evidence) -> Evidence:
        # Official and headline evidence is trusted as supplied; never fetch an aggregator page.
        if source.content_type in {"official_feed", "news_feed"}:
            return source
        for provider in self.providers:
            handler = getattr(provider, "retrieve", None)
            if handler is not None:
                return await handler(source)
        return source


def supports_market_wide(provider) -> bool:
    """Query capability is explicit for adapters whose generic method is ticker-only."""
    if provider is None or isinstance(provider, SecFilingsResearchProvider):
        return False
    if isinstance(provider, CompositeResearchProvider):
        return any(supports_market_wide(child) for child in provider.providers)
    if isinstance(provider, TickerNewsFeedProvider):
        return any(feed in {"google_news", "bing_news"} for feed in provider.sources) or supports_market_wide(provider.fallback)
    explicit = getattr(provider, "supports_market_wide", None)
    if explicit is not None:
        return bool(explicit)
    return any(hasattr(provider, method) for method in ("search", "search_events", "search_events_candidates"))


def _source_id(provider):
    if isinstance(provider, SecFilingsResearchProvider):
        return "sec_filings"
    if isinstance(provider, PrimaryFeedResearchProvider):
        return "official_feeds"
    if isinstance(provider, TickerNewsFeedProvider):
        return "ticker_news"
    return "search"


def source_diagnostics(provider) -> dict:
    """Collect availability without performing requests, including nested search."""
    if provider is None:
        return {}
    result = dict(getattr(provider, "source_outcomes", {}))
    for child in getattr(provider, "providers", []):
        result.update(source_diagnostics(child))
    fallback = getattr(provider, "fallback", None)
    if fallback is not None:
        result.update(source_diagnostics(fallback))
    return result


def research_tool_sources(provider) -> dict:
    """Expose individual selectable adapters, sharing caches and diagnostic state."""
    from copy import copy
    if provider is None:
        return {}
    if isinstance(provider, CompositeResearchProvider):
        result = {}
        for child in provider.providers:
            result.update(research_tool_sources(child))
        return result
    result = {}
    if isinstance(provider, (PrimaryFeedResearchProvider, TickerNewsFeedProvider)):
        if not hasattr(provider, "source_outcomes"):
            provider.source_outcomes = {}
        for source in provider.sources:
            adapter = copy(provider)
            adapter.sources = (source,)
            adapter.fallback = None
            if isinstance(adapter, PrimaryFeedResearchProvider):
                adapter._feed_lock = asyncio.Lock()
            else:
                adapter._lock = asyncio.Lock()
            result[source] = adapter
    else:
        result[_source_id(provider)] = provider
    result.update(research_tool_sources(getattr(provider, "fallback", None)))
    return result


async def search_asset_query(provider, symbol, name, query, max_results=6, reporting_cutoff=None):
    """Keep explicit asset identity and the analyst's research focus together."""
    if isinstance(provider, CompositeResearchProvider):
        async def collect(adapter):
            try:
                async with source_network_slot():
                    async with asyncio.timeout(SOURCE_CHILD_SECONDS):
                        return await search_asset_query(adapter, symbol, name, query, max_results, reporting_cutoff)
            except Exception as error:
                ids = adapter.sources if isinstance(adapter, (PrimaryFeedResearchProvider, TickerNewsFeedProvider)) else (_source_id(adapter),)
                for source in ids:
                    record_outcome(adapter, source, "failed", safe_source_error(error))
                return []
        groups = await _partial_source_groups(collect(adapter) for child in provider.providers for adapter in research_tool_sources(child).values())
        return await _finalize_sources(provider, _interleave_candidates(groups), reference=reporting_cutoff)
    async with source_network_slot():
        return await _finalize_sources(provider, await _search_asset_leaf(provider, symbol, name, query, max_results, reporting_cutoff),
                                       reference=reporting_cutoff)


async def _search_asset_leaf(provider, symbol, name, query, max_results, reporting_cutoff):
    if isinstance(provider, TickerNewsFeedProvider):
        return await provider._candidates(query, symbol, name, max_results, reporting_cutoff)
    if isinstance(provider, SecFilingsResearchProvider):
        return await provider.search_symbol(symbol, name, max_results=max_results, reporting_cutoff=reporting_cutoff)
    return await _probe_call(provider, query, max_results=max_results, reporting_cutoff=reporting_cutoff)


async def _probe_call(provider, query, *, max_results=3, reporting_cutoff=None):
    handler = getattr(provider, "search_events", None)
    if handler is not None:
        return await handler(query, max_results=max_results, reporting_cutoff=reporting_cutoff)
    return await provider.search(query, max_results=max_results)


async def probe_research_sources(provider, symbol="AAPL", name=None) -> dict:
    """Exercise every configured adapter using its actual supported request path."""
    if provider is None:
        return {}
    if isinstance(provider, CompositeResearchProvider):
        results = {}
        for outcomes in await asyncio.gather(*(probe_research_sources(child, symbol, name) for child in provider.providers)):
            results.update(outcomes)
        return results
    source_id = _source_id(provider)
    provider.source_outcomes = {}
    if isinstance(provider, SecFilingsResearchProvider) and not provider._enabled():
        record_outcome(provider, source_id, "skipped", "SEC filings were not checked because an identifying contact is missing or invalid")
        return source_diagnostics(provider)
    fallback = getattr(provider, "fallback", None)
    async def bounded_probe(request, diagnostic_id=source_id):
        try:
            items = await asyncio.wait_for(request, timeout=8)
            record_outcome(provider, diagnostic_id, "passed" if items else "empty", "" if items else "No matching updates")
        except Exception as error:
            record_outcome(provider, diagnostic_id, "failed", safe_source_error(error))

    async def official_feed(source):
        # Exercise production evidence parsing, with independent locks so a
        # blocked feed cannot prevent a working feed's diagnostic from finishing.
        from copy import copy
        probe = copy(provider)
        probe.sources = (source,)
        probe.fallback = None
        probe._feed_lock = asyncio.Lock()
        return await probe.search("", max_results=3)

    if isinstance(provider, PrimaryFeedResearchProvider):
        requests = [(source, official_feed(source)) for source in provider.sources]
    elif isinstance(provider, TickerNewsFeedProvider):
        from copy import copy
        requests = []
        for feed in provider.sources:
            probe = copy(provider)
            probe._lock = asyncio.Lock()
            requests.append((feed, probe._source_entries(feed, probe._symbol_query(symbol, name), symbol, name, None)))
    elif isinstance(provider, SecFilingsResearchProvider):
        requests = [(source_id, provider.search_symbol(symbol, name, max_results=3))]
    else:
        query = "Federal Reserve inflation market policy"
        requests = [(source_id, _probe_call(provider, query, max_results=3))]
    jobs = [bounded_probe(request, diagnostic_id) for diagnostic_id, request in requests]
    if fallback is not None:
        jobs.append(probe_research_sources(fallback, symbol, name))
    await asyncio.gather(*jobs)
    results = source_diagnostics(provider)
    if not requests:
        record_outcome(provider, source_id, "skipped", "No source feeds selected")
        results = source_diagnostics(provider)
    return results
