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


def test_invalid_legacy_contact_is_visible_but_gated(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"sec_contact": "John", "sec_company_filings_enabled": True}))
    settings = load_settings(tmp_path)
    assert settings.sec_contact == "John"
    provider = SecFilingsResearchProvider(settings.sec_contact)
    assert not provider._enabled()
    assert source_diagnostics(provider)["sec_filings"]["status"] == "skipped"


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
        "official_feeds": "empty", "search": "empty", "sec_filings": "skipped", "ticker_news": "failed"}
    assert ("google_news", "AAPL") in calls and ("nasdaq", "AAPL") in calls
    assert "secret" not in str(outcomes)
    assert any(call[0] == "search" and "Federal Reserve" in call[1] for call in calls)


def test_partial_official_failure_persists(monkeypatch):
    provider = PrimaryFeedResearchProvider()
    async def mixed(url, **kwargs):
        if "bls.gov" in url:
            raise SourceHTTPError(403)
        return "<rss><channel><item><title>Inflation update</title><link>https://federalreserve.gov/update</link></item></channel></rss>"
    monkeypatch.setattr(provider, "_fetch", mixed)
    assert asyncio.run(provider.search("inflation"))
    assert source_diagnostics(provider)["official_feeds"]["status"] == "failed"


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
            if len(calls) == 2:
                ready.set()
            await ready.wait()
            if "bls.gov" in url:
                raise TimeoutError()
            return "<rss><channel><item><title>Market update</title></item></channel></rss>"
        monkeypatch.setattr(providers, "retrieve_public_text", fetch)
        return await asyncio.wait_for(probe_research_sources(PrimaryFeedResearchProvider()), timeout=1)
    outcomes = asyncio.run(exercise())
    assert len(calls) == 2
    assert outcomes["official_feeds"]["status"] == "failed"
    assert outcomes["official_feeds"]["reason"] == "Source request timed out"


def test_alpha_probe_uses_actual_portfolio_symbol(monkeypatch):
    provider = providers.AlphaVantageResearchProvider("never exposed")
    calls = []
    async def search(query, max_results=3):
        calls.append(query)
        return []
    monkeypatch.setattr(provider, "search", search)
    assert asyncio.run(probe_research_sources(provider, symbol="MSFT"))["alpha_vantage"]["status"] == "empty"
    assert calls[0].split()[0] == "MSFT"
