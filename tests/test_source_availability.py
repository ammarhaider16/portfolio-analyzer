import asyncio
import json

import pytest
from pydantic import ValidationError

from portfolio_analyzer.config import Settings, load_settings
from portfolio_analyzer import providers
from portfolio_analyzer.providers import (
    CompositeResearchProvider, PrimaryFeedResearchProvider, SecFilingsResearchProvider,
    SourceHTTPError, TickerNewsFeedProvider, probe_research_sources, source_diagnostics,
    supports_market_wide,
)
from portfolio_analyzer.models import Evidence


def test_contact_defaults_and_validation():
    assert not Settings().sec_company_filings_enabled
    assert Settings(sec_contact="Analyst contact@example.com").sec_company_filings_enabled
    assert not Settings(sec_contact="Analyst contact@example.com", sec_company_filings_enabled=False).sec_company_filings_enabled
    for value in ("John", "contact@example.com\r\nInjected: yes", " ", "test@example.com\x7f"):
        with pytest.raises(ValidationError) as error:
            Settings(sec_contact=value)
        assert error.value.errors()[0]["loc"] == ("sec_contact",)


def test_saved_invalid_contact_is_rejected(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"sec_contact": "John", "sec_company_filings_enabled": True}))
    with pytest.raises(ValidationError):
        load_settings(tmp_path)


@pytest.mark.parametrize("host", ["sec.gov", "www.sec.gov", "data.sec.gov", "SEC.GOV."])
def test_sec_retrieval_guard_runs_before_dns(monkeypatch, host):
    def forbidden(*args, **kwargs):
        pytest.fail("Contact guard must run before DNS or network access")
    monkeypatch.setattr(providers.socket, "getaddrinfo", forbidden)
    with pytest.raises(ValueError, match="identifying contact"):
        asyncio.run(providers.retrieve_public_text(f"https://{host}/filings"))


def test_sec_http_failure_does_not_look_empty(monkeypatch):
    async def blocked(*args, **kwargs):
        raise SourceHTTPError(403)
    monkeypatch.setattr(providers, "retrieve_public_text", blocked)
    provider = SecFilingsResearchProvider("Analyst contact@example.com")
    outcomes = asyncio.run(probe_research_sources(provider))
    assert outcomes["sec_filings"]["status"] == "failed"
    assert outcomes["sec_filings"]["reason"] == "HTTP 403"


def test_probes_use_ticker_and_market_paths_and_test_full_fallback(monkeypatch):
    calls = []
    class Search:
        async def search(self, query, max_results=3):
            calls.append(("search", query))
            return []
    official = PrimaryFeedResearchProvider(fallback=Search())
    async def official_fetch(*args, **kwargs):
        return "<rss><channel/></rss>"
    monkeypatch.setattr(providers, "retrieve_public_text", official_fetch)
    ticker = TickerNewsFeedProvider()
    async def ticker_entries(feed, query, symbol, name, cutoff):
        calls.append((feed, symbol))
        if feed == "nasdaq":
            raise RuntimeError("secret URL must never be shown")
        return []
    monkeypatch.setattr(ticker, "_source_entries", ticker_entries)
    sec = SecFilingsResearchProvider()
    outcomes = asyncio.run(probe_research_sources(CompositeResearchProvider([official, sec, ticker])))
    assert {key: row["status"] for key, row in outcomes.items()} == {
        "fed": "empty", "search": "empty", "sec_filings": "skipped", "google_news": "empty", "nasdaq": "failed"}
    assert ("google_news", "AAPL") in calls and ("nasdaq", "AAPL") in calls
    assert "secret" not in str(outcomes)
    assert any(call[0] == "search" and "Federal Reserve" in call[1] for call in calls)


def test_partial_official_failure_persists(monkeypatch):
    provider = PrimaryFeedResearchProvider()
    async def mixed(url, **kwargs):
        return "<rss><channel><item><title>Inflation update</title><link>https://federalreserve.gov/update</link></item></channel></rss>"
    monkeypatch.setattr(provider, "_fetch", mixed)
    assert asyncio.run(provider.search("inflation"))
    assert source_diagnostics(provider)["fed"]["status"] == "passed"


def test_duplicate_url_preserves_stronger_evidence():
    headline = Evidence(id="headline", url="https://example.com/event", publisher="example.com", title="AAPL earnings beat estimates", content_type="news_feed")
    full = headline.model_copy(update={"id": "full", "content_type": "retrieved_page"})
    discovery = headline.model_copy(update={"id": "discovery", "content_type": "search_excerpt"})
    assert providers._dedupe_sources([discovery, headline, full]) == [full]
    assert providers._dedupe_sources([full, headline, discovery]) == [full]


def test_ticker_candidates_consult_later_feed_after_saturated_syndication(monkeypatch):
    ticker = TickerNewsFeedProvider(sources=("google_news", "nasdaq"))
    calls = []
    async def entries(feed, query, symbol, name, cutoff):
        calls.append(feed)
        if feed == "google_news":
            return [Evidence(id=f"copy{index}", url=f"https://example.com/copy{index}", publisher="example.com", title="Apple beats Q4 estimates", content_type="news_feed") for index in range(30)]
        return [Evidence(id="distinct", url="https://nasdaq.com/distinct", publisher="nasdaq.com", title="Apple announces dividend", content_type="news_feed")]
    monkeypatch.setattr(ticker, "_source_entries", entries)
    candidate_provider = CompositeResearchProvider([ticker])
    items = asyncio.run(candidate_provider.search_symbol_candidates("AAPL", max_results=12))
    assert calls == ["google_news", "nasdaq"]
    assert items[1].id == "distinct"
    assert len(items) == 13  # Twelve candidates per feed, not a first-feed global cap.


def test_market_wide_capability_recurses_without_sec_false_positive():
    sec = SecFilingsResearchProvider("Analyst contact@example.com")
    nasdaq = TickerNewsFeedProvider(sources=("nasdaq",))
    google = TickerNewsFeedProvider(sources=("google_news",))
    assert not supports_market_wide(sec)
    assert not supports_market_wide(CompositeResearchProvider([sec, nasdaq]))
    assert supports_market_wide(CompositeResearchProvider([CompositeResearchProvider([sec, google])]))
    assert supports_market_wide(PrimaryFeedResearchProvider())


def test_identifying_contact_only_sent_to_sec(monkeypatch):
    agents = []
    async def fetch(url, **kwargs):
        agents.append(kwargs["user_agent"])
        return "ok"
    monkeypatch.setattr(providers, "retrieve_public_text", fetch)
    provider = PrimaryFeedResearchProvider(sec_user_agent="Analyst contact@example.com")
    asyncio.run(provider._fetch("https://www.federalreserve.gov/feed"))
    asyncio.run(provider._fetch("https://www.sec.gov/feed"))
    assert agents == ["PortfolioResearch/1.0", "Analyst contact@example.com"]


def test_source_probes_run_independently_and_keep_timeout(monkeypatch):
    calls = []
    async def exercise():
        ready = asyncio.Event()
        async def fetch(url, **kwargs):
            calls.append(url)
            ready.set()
            await ready.wait()
            return "<rss><channel><item><title>Market update</title><link>https://www.federalreserve.gov/update</link></item></channel></rss>"
        monkeypatch.setattr(providers, "retrieve_public_text", fetch)
        return await asyncio.wait_for(probe_research_sources(PrimaryFeedResearchProvider()), timeout=1)
    outcomes = asyncio.run(exercise())
    assert len(calls) == 1
    assert outcomes["fed"]["status"] == "passed"


def test_symbol_candidate_dispatch_reaches_general_search():
    calls = []
    class GeneralSearch:
        async def search(self, query, max_results=3):
            calls.append(query)
            return []
    provider = CompositeResearchProvider([GeneralSearch()])
    asyncio.run(provider.search_symbol_candidates("AAPL", name="Apple Inc"))
    assert calls == ["AAPL Apple Inc latest news"]
    assert source_diagnostics(provider)["search"]["status"] == "empty"


def test_asset_tool_search_preserves_analyst_focus_and_ticker(monkeypatch):
    calls = []
    provider = TickerNewsFeedProvider(sources=("google_news", "nasdaq"))
    async def entries(feed, query, symbol, name, cutoff):
        calls.append((feed, query, symbol, name))
        return []
    monkeypatch.setattr(provider, "_source_entries", entries)
    asyncio.run(providers.search_asset_query(provider, "AAPL", "Apple Inc", "AAPL supplier disruption material exposure"))
    assert calls == [
        ("google_news", "AAPL supplier disruption material exposure", "AAPL", "Apple Inc"),
        ("nasdaq", "AAPL supplier disruption material exposure", "AAPL", "Apple Inc"),
    ]


def test_composite_candidates_keep_fast_results_when_sibling_times_out(monkeypatch):
    evidence = Evidence(id="fast", url="https://example.com/news", publisher="example.com", title="Current news")
    class Fast:
        async def search(self, query, max_results=3):
            return [evidence]
    class Slow:
        async def search(self, query, max_results=3):
            await asyncio.sleep(10)
    monkeypatch.setattr(providers, "SOURCE_CHILD_SECONDS", .02)
    composite = CompositeResearchProvider([Fast(), Slow()])
    assert asyncio.run(composite.search_symbol_candidates("AAPL")) == [evidence]


def test_four_parallel_tools_share_four_leaf_source_slots():
    async def exercise():
        active = peak = calls = 0
        class Source:
            async def search(self, query, max_results=3):
                nonlocal active, peak, calls
                active += 1
                peak = max(peak, active)
                calls += 1
                try:
                    # Nested same-task network work must reuse the held slot.
                    async with providers.source_network_slot():
                        await asyncio.sleep(.005)
                    return []
                finally:
                    active -= 1
        composite = CompositeResearchProvider([CompositeResearchProvider([Source()])] + [Source() for _ in range(5)])
        with providers.source_network_scope():
            await asyncio.wait_for(asyncio.gather(*(composite.search_symbol_candidates("AAPL") for _ in range(4))), timeout=1)
        assert calls == 24
        assert peak == 4
        assert active == 0
    asyncio.run(exercise())


def test_partial_collector_has_total_deadline_including_slot_queue(monkeypatch):
    async def exercise():
        evidence = Evidence(id="fast", url="https://example.com/news", publisher="example.com", title="Current news")
        class Fast:
            async def search(self, query, max_results=3):
                return [evidence]
        class Slow:
            async def search(self, query, max_results=3):
                await asyncio.sleep(10)
        monkeypatch.setattr(providers, "SOURCE_CHILD_SECONDS", .03)
        with providers.source_network_scope(limit=1):
            result = await asyncio.wait_for(CompositeResearchProvider([Fast(), Slow(), Slow()]).search_symbol_candidates("AAPL"), timeout=.15)
        assert result == [evidence]
    asyncio.run(exercise())


def test_composite_asset_query_retains_sec_and_focus(monkeypatch):
    calls = []
    sec = SecFilingsResearchProvider("Analyst contact@example.com")
    ticker = TickerNewsFeedProvider(sources=("google_news",))
    async def sec_search(symbol, name, **kwargs):
        calls.append(("sec", symbol))
        return []
    async def ticker_search(feed, query, symbol, name, cutoff):
        calls.append((feed, query, symbol))
        return []
    monkeypatch.setattr(sec, "search_symbol", sec_search)
    monkeypatch.setattr(ticker, "_source_entries", ticker_search)
    asyncio.run(providers.search_asset_query(CompositeResearchProvider([sec, ticker]), "AAPL", "Apple", "supply chain risk"))
    assert ("sec", "AAPL") in calls
    assert ("google_news", "supply chain risk", "AAPL") in calls


def test_source_registry_supports_independent_tool_selection():
    ticker = TickerNewsFeedProvider()
    official = PrimaryFeedResearchProvider()
    registry = providers.research_tool_sources(CompositeResearchProvider([ticker, official]))
    assert set(registry) == {"fed", "google_news", "nasdaq"}
    assert registry["fed"].sources == ("fed",)
    assert registry["nasdaq"].sources == ("nasdaq",)
    assert not supports_market_wide(registry["nasdaq"])
    assert registry["fed"].source_outcomes is official.source_outcomes
