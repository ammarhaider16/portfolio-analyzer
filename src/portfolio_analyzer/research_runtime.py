"""Read-only research tools with shared run budgets and an evidence registry."""
import asyncio
import inspect
from datetime import datetime, timezone
from uuid import uuid4

from .models import Evidence


class ResearchBudgetExceeded(RuntimeError):
    """The bounded analyst must finalize from evidence already collected."""


class ResearchToolUnavailable(ValueError):
    """A safe, application-authored tool configuration explanation."""


class ResearchRuntime:
    def __init__(self, request, sources, *, research=None, market_data=None, deadline=None,
                 activity=None, budget=None, activity_sink=None):
        self.request, self.sources = request, sources
        self.research, self.market_data = research, market_data
        self.deadline = deadline or asyncio.get_running_loop().time() + request.max_seconds
        self.activity = activity if activity is not None else []
        self.budget = budget if budget is not None else {}
        self.budget.update({"max_queries": request.max_searches, "max_model_calls": request.max_model_calls,
                            "max_tool_calls": request.max_tool_calls})
        for key in ("queries", "model_calls", "tool_calls"):
            self.budget.setdefault(key, 0)
        self.activity_sink = activity_sink
        self.network = asyncio.Semaphore(4)
        # A second runtime wraps the analyst after initial coverage; retain one
        # elapsed-time origin across both phases (including capacity wait).
        self.started = self.deadline - request.max_seconds
        self.reserve = min(20.0, request.max_seconds * 0.2)
        from .providers import research_tool_sources
        self.providers = research_tool_sources(research) if research is not None else {}

    def remaining(self):
        return max(0.0, self.deadline - asyncio.get_running_loop().time())

    async def emit(self, tool, status, *, query="", source="", symbol="", **extra):
        event = {"tool": tool, "status": status, "query": query[:500], "source": source,
                 "symbol": symbol, "elapsed_seconds": round(asyncio.get_running_loop().time() - self.started, 3),
                 "at": datetime.now(timezone.utc).isoformat(), **extra}
        self.activity.append(event)
        self.budget["remaining_seconds"] = round(self.remaining(), 2)
        if self.activity_sink:
            try:
                pending = self.activity_sink(dict(event), dict(self.budget))
                if inspect.isawaitable(pending):
                    await pending
            except Exception:
                # An unavailable UI/storage subscriber must not fail research.
                pass

    def _claim(self, query=False):
        if self.remaining() <= self.reserve:
            raise ResearchBudgetExceeded("Research time reserved for finalization; use collected evidence.")
        if self.budget["tool_calls"] >= self.request.max_tool_calls:
            raise ResearchBudgetExceeded("Research tool-call limit reached; use collected evidence.")
        if query and self.budget["queries"] >= self.request.max_searches:
            raise ResearchBudgetExceeded("Discovery-query limit reached; use collected evidence.")
        self.budget["tool_calls"] += 1
        if query:
            self.budget["queries"] += 1

    def register(self, sources):
        accepted = []
        reference = self.request.reporting_cutoff or datetime.now(timezone.utc)
        for source in sources[:36]:
            if source.published_at and source.published_at > reference:
                continue
            if self.request.reporting_cutoff and source.published_at is None:
                continue
            existing = next((item for item in self.sources if item.url == source.url), None)
            if existing:
                grade = {"search_excerpt": 0, "news_feed": 1, "official_feed": 2, "retrieved_page": 2}
                if grade.get(source.content_type, 0) > grade.get(existing.content_type, 0):
                    source = source.model_copy(update={"id": existing.id})
                    self.sources[self.sources.index(existing)] = source
                else:
                    source = existing
            else:
                if len(self.sources) >= 200:
                    continue
                if any(item.id == source.id for item in self.sources):
                    source = source.model_copy(update={"id": str(uuid4())})
                self.sources.append(source)
            accepted.append({**source.model_dump(mode="json"), "excerpt": source.excerpt[:5000]})
        return accepted

    async def execute(self, tool, operation, *, query="", source="", symbol="", discovery=False):
        try:
            self._claim(discovery)
        except ResearchBudgetExceeded as error:
            await self.emit(tool, "budget_exhausted", query=query, source=source, symbol=symbol)
            return {"status": "budget_exhausted", "reason": str(error), "budget": dict(self.budget)}
        await self.emit(tool, "started", query=query, source=source, symbol=symbol)
        try:
            async with self.network:
                async with asyncio.timeout(max(0.001, min(15, self.remaining() - self.reserve))):
                    value = await operation()
            count = len(value) if isinstance(value, list) else None
            await self.emit(tool, "passed" if value else "empty", query=query, source=source, symbol=symbol,
                            result_count=count)
            return {"status": "passed" if value else "empty", "results": value, "budget": dict(self.budget)}
        except asyncio.CancelledError:
            await self.emit(tool, "cancelled", query=query, source=source, symbol=symbol)
            raise
        except Exception as error:
            from .providers import safe_source_error
            reason = str(error) if isinstance(error, ResearchToolUnavailable) else safe_source_error(error)
            await self.emit(tool, "failed", query=query, source=source, symbol=symbol, reason=reason)
            return {"status": "failed", "reason": reason, "budget": dict(self.budget)}

    def tools(self):
        from langchain_core.tools import StructuredTool

        async def search_asset_news(symbol: str, focus: str = "recent material developments", source: str = "all") -> dict:
            """Investigate one portfolio asset with a free-form focus; choose a listed source or all."""
            position = next((p for p in self.request.snapshot.positions if p.symbol == symbol.upper()), None)
            if position is None:
                return {"status": "invalid", "reason": "Use a symbol from the complete portfolio roster."}
            query = f'"{position.instrument_name or position.symbol}" {position.symbol} {focus[:1000]}'
            async def operation():
                from .providers import search_asset_query
                provider = self.research if source == "all" else self.providers.get(source)
                if provider is None:
                    raise ResearchToolUnavailable("Requested research source is unavailable. Choose source='all' or one of: " + ", ".join(self.providers))
                return self.register(await search_asset_query(provider, position.symbol, position.instrument_name,
                                                              query, 12, self.request.reporting_cutoff))
            return await self.execute("search_asset_news", operation, query=query, source=source,
                                      symbol=position.symbol, discovery=True)

        async def search_research(query: str, source: str = "all") -> dict:
            """Research market-wide news, exposures, fund mandates, or follow-up questions; choose a listed source."""
            async def operation():
                from .engine import _search_events
                provider = self.research if source == "all" else self.providers.get(source)
                if provider is None:
                    raise ResearchToolUnavailable("Requested research source is unavailable. Choose source='all' or one of: " + ", ".join(self.providers))
                return self.register(await _search_events(provider, query[:1500], 12, self.request.reporting_cutoff))
            return await self.execute("search_research", operation, query=query, source=source, discovery=True)

        async def retrieve_public_page(url: str, evidence_id: str = "") -> dict:
            """Fetch public HTTPS article details. Prefer a discovered evidence_id to preserve publisher/date metadata."""
            async def operation():
                from .providers import retrieve_public_text
                from urllib.parse import urlsplit
                if self.research is None or not self.request.max_searches:
                    raise ResearchToolUnavailable("External research is disabled")
                # The production fetcher validates and DNS-pins every redirect in its
                # worker thread; do not repeat blocking DNS work on the event loop.
                original = next((item for item in self.sources if item.url == url
                                 and (not evidence_id or item.id == evidence_id)), None)
                if urlsplit(url).hostname in {"news.google.com", "www.google.com", "www.bing.com"}:
                    raise ResearchToolUnavailable("Retrieve the publisher page instead of an aggregator link")
                hostname = urlsplit(url).hostname or ""
                sec = self.providers.get("sec_filings")
                if hostname == "sec.gov" or hostname.endswith(".sec.gov"):
                    text = await retrieve_public_text(url, user_agent=getattr(sec, "sec_user_agent", None) or "PortfolioResearch/1.0")
                else:
                    text = await retrieve_public_text(url)
                if original:
                    value = original.model_copy(update={"content_type": "retrieved_page", "excerpt": text[:12000]})
                else:
                    value = Evidence(id=str(uuid4()), url=url, title=urlsplit(url).hostname or "Public page",
                                     publisher=urlsplit(url).hostname or "", excerpt=text[:12000], content_type="retrieved_page")
                return self.register([value])
            return await self.execute("retrieve_public_page", operation, query=url)

        async def lookup_market_data(symbol: str) -> dict:
            """Look up instrument identity and supplemental daily-close metadata without changing portfolio measures."""
            async def operation():
                if self.market_data is None or not hasattr(self.market_data, "resolve_instrument"):
                    raise ResearchToolUnavailable("Market-data lookup is unavailable")
                from .providers import source_network_slot
                async with source_network_slot():
                    identity = await self.market_data.resolve_instrument(symbol.upper())
                    quote = await self.market_data.quotes([symbol.upper()]) if hasattr(self.market_data, "quotes") else {}
                return {"identity": identity, "daily_close": quote.get(symbol.upper()),
                        "note": "Supplemental lookup; deterministic portfolio measures remain unchanged."}
            return await self.execute("lookup_market_data", operation, symbol=symbol.upper())

        return [StructuredTool.from_function(coroutine=fn) for fn in
                (search_asset_news, search_research, retrieve_public_page, lookup_market_data)]
