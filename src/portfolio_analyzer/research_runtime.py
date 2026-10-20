"""Simple research tools with prepared source context and an optional run trace."""

import asyncio
import inspect
from datetime import datetime, timezone
from uuid import uuid4


class ResearchBudgetExceeded(RuntimeError):
    pass


class ResearchRuntime:
    def __init__(
        self,
        request,
        sources,
        *,
        research=None,
        market_data=None,
        deadline=None,
        activity=None,
        budget=None,
        activity_sink=None,
    ):
        self.request, self.sources, self.research, self.market_data = request, sources, research, market_data
        self.deadline = deadline or asyncio.get_running_loop().time() + request.max_seconds
        self.started = self.deadline - request.max_seconds
        self.reserve = min(45, request.max_seconds * 0.1)
        self.activity = activity if activity is not None else []
        self.budget = budget if budget is not None else {}
        for name in ("queries", "tool_calls", "model_calls"):
            self.budget.setdefault(name, 0)
        self.activity_sink = activity_sink

    def remaining(self):
        return max(0, self.deadline - asyncio.get_running_loop().time())

    async def emit(self, tool, status, **details):
        event = {
            "tool": tool,
            "status": status,
            "elapsed_seconds": round(asyncio.get_running_loop().time() - self.started, 2),
            "at": datetime.now(timezone.utc).isoformat(),
            **details,
        }
        self.activity.append(event)
        if self.activity_sink:
            try:
                pending = self.activity_sink(dict(event), dict(self.budget))
                if inspect.isawaitable(pending):
                    await pending
            except Exception:
                pass

    def present(self, source):
        return {
            "id": source.id,
            "title": source.title,
            "publisher": source.publisher,
            "published_at": source.published_at.isoformat() if source.published_at else None,
            "content": source.excerpt or source.title,
        }

    def register(self, sources):
        result = []
        for source in sources:
            previous = next((item for item in self.sources if item.url == source.url), None)
            if previous:
                source = source.model_copy(update={"id": previous.id})
                self.sources[self.sources.index(previous)] = source
            else:
                if any(item.id == source.id for item in self.sources):
                    source = source.model_copy(update={"id": str(uuid4())})
                self.sources.append(source)
            result.append(self.present(source))
        return result

    async def read_source(self, source):
        from urllib.parse import urlparse
        from .providers import retrieve_public_text, research_tool_sources

        host = urlparse(source.url).hostname or ""
        if host == "sec.gov" or host.endswith(".sec.gov"):
            contact = next(
                (
                    getattr(provider, "sec_user_agent", None)
                    for provider in research_tool_sources(self.research).values()
                    if getattr(provider, "sec_user_agent", None)
                ),
                None,
            )
            return await retrieve_public_text(source.url, user_agent=contact or "PortfolioResearch/1.0")
        return await retrieve_public_text(source.url)

    async def prepare_news(self, candidates):
        from .news import source_hygiene

        candidates, _ = source_hygiene(
            candidates, reference=self.request.reporting_cutoff, max_age_hours=self.request.max_data_age_hours
        )
        from .providers import is_aggregator_url

        # Source preparation belongs here, not in the analyst's writing workflow.
        async def prepare(source, index):
            if index < 4 and source.content_type != "retrieved_page" and not is_aggregator_url(source.url):
                try:
                    async with asyncio.timeout(8):
                        content = await self.read_source(source)
                    if content.strip():
                        source = source.model_copy(
                            update={"excerpt": content[:10000], "content_type": "retrieved_page"}
                        )
                except Exception:
                    pass  # The publisher summary remains useful when the article cannot be opened.
            return source

        prepared = await asyncio.gather(
            *(prepare(source, index) for index, source in enumerate(candidates[:12]))
        )
        return self.register(prepared)

    async def execute(self, name, operation, *, discovery=False, **details):
        if self.remaining() <= self.reserve or self.budget["tool_calls"] >= self.request.max_tool_calls:
            return {"message": "Finish your report with the information already collected."}
        if discovery and self.budget["queries"] >= self.request.max_searches:
            return {"message": "Use the news already collected to finish the report."}
        self.budget["tool_calls"] += 1
        if discovery:
            self.budget["queries"] += 1
        await self.emit(name, "started", **details)
        try:
            async with asyncio.timeout(min(30, max(0.1, self.remaining() - self.reserve))):
                value = await operation()
            await self.emit(name, "passed" if value else "empty", **details)
            return value
        except asyncio.CancelledError:
            raise
        except Exception as error:
            from .providers import safe_source_error

            reason = safe_source_error(error)
            await self.emit(name, "failed", reason=reason, **details)
            return {
                "message": "This lookup is unavailable. Try another query or continue with the available information."
            }

    def tools(self):
        from langchain_core.tools import StructuredTool

        async def search_news(query: str, symbol: str = "") -> list | dict:
            """Find news or research on any topic or asset. Returns publisher, date and prepared readable content."""

            async def operation():
                if self.research is None:
                    return []
                from .engine import _search_events

                position = next(
                    (p for p in self.request.snapshot.positions if p.symbol == symbol.upper()), None
                )
                candidates = await _search_events(
                    self.research,
                    query,
                    12,
                    self.request.reporting_cutoff,
                    symbol.upper() or None,
                    position.instrument_name if position else None,
                )
                return await self.prepare_news(candidates)

            return await self.execute(
                "search_news", operation, discovery=True, query=query[:1000], symbol=symbol.upper()
            )

        async def read_article(source_id: str) -> dict | list:
            """Read more detail from a news item returned by search_news."""

            async def operation():
                source = next((item for item in self.sources if item.id == source_id), None)
                if source is None:
                    return {"message": "Choose a source_id returned by search_news."}
                text = await self.read_source(source)
                return self.register(
                    [source.model_copy(update={"excerpt": text[:15000], "content_type": "retrieved_page"})]
                )

            return await self.execute("read_article", operation, source=source_id)

        async def get_market_data(symbol: str) -> dict:
            """Get the current available quote, previous session close and instrument identity for a ticker."""

            async def operation():
                if self.market_data is None:
                    return {"message": "Market data is unavailable."}
                quotes = await self.market_data.quotes([symbol.upper()])
                identity = await self.market_data.resolve_instrument(symbol.upper())
                return {"quote": quotes.get(symbol.upper()), "instrument": identity}

            return await self.execute("get_market_data", operation, symbol=symbol.upper())

        return [
            StructuredTool.from_function(coroutine=fn) for fn in (search_news, read_article, get_market_data)
        ]
