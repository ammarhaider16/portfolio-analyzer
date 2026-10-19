"""Provider checks for SEC company filings and keyless ticker news feeds (no network)."""
import asyncio
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import portfolio_analyzer.providers as providers
from portfolio_analyzer.config import Settings
from portfolio_analyzer.models import Evidence
from portfolio_analyzer.providers import (CompositeResearchProvider, SecFilingsResearchProvider,
                                          TickerNewsFeedProvider)


def run(coro):
    return asyncio.run(coro)


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Apple Inc beats estimates</title><link>https://example.com/apple</link>
<description>Apple Inc reported quarterly results.</description>
<pubDate>Fri, 16 Oct 2026 12:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Microsoft Corporation update</title><link href="https://example.com/msft"/>
<updated>2026-10-15T12:00:00Z</updated><summary>Microsoft Corporation summary</summary></entry>
</feed>"""

GOOGLE_NEWS_RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Apple Inc launches product</title>
<link>https://news.google.com/rss/articles/CBMiExample</link>
<description>Apple Inc product launch coverage.</description>
<pubDate>Fri, 16 Oct 2026 12:00:00 GMT</pubDate>
<source url="https://www.reuters.com">Reuters</source></item>
</channel></rss>"""

NASDAQ_RSS = """<?xml version="1.0"?><rss version="2.0" xmlns:nasdaq="https://www.nasdaq.com"><channel>
<item><title>Apple tagged</title><link>https://example.com/tagged</link>
<pubDate>Fri, 16 Oct 2026 12:00:00 GMT</pubDate><description>tagged</description>
<nasdaq:tickers>AAPL</nasdaq:tickers></item>
<item><title>Other untagged</title><link>https://example.com/untagged</link>
<pubDate>Fri, 16 Oct 2026 12:00:00 GMT</pubDate><description>untagged</description></item>
</channel></rss>"""

HTTP_FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Insecure</title><link>http://example.com/insecure</link>
<pubDate>Fri, 16 Oct 2026 12:00:00 GMT</pubDate><description>insecure</description></item>
</channel></rss>"""

CUTOFF_FEED = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Future</title><link>https://example.com/future</link>
<pubDate>Fri, 01 Jan 2027 12:00:00 GMT</pubDate><description>future</description></item>
<item><title>Undated</title><link>https://example.com/undated</link><description>undated</description></item>
<item><title>Valid</title><link>https://example.com/valid</link>
<pubDate>Sat, 10 Oct 2026 12:00:00 GMT</pubDate><description>valid</description></item>
</channel></rss>"""

TICKERS_JSON = '{"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}'
SUBMISSIONS_JSON = json.dumps({"filings": {"recent": {
    "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002"],
    "form": ["8-K", "4"],
    "filingDate": ["2026-10-15", "2026-10-14"],
    "primaryDocument": ["a.htm", "b.htm"]}}})


def patch_retrieve(monkeypatch, response):
    calls = []

    async def fake(url, *args, **kwargs):
        calls.append((url, kwargs))
        value = response(url)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(providers, "retrieve_public_text", fake)
    return calls


def test_rss_and_atom_items_become_dated_news_feed(monkeypatch):
    provider = TickerNewsFeedProvider(sources=("google_news",))
    patch_retrieve(monkeypatch, lambda url: RSS)
    rss = run(provider.search_symbol("AAPL", "Apple Inc", max_results=4))
    assert rss[0].content_type == "news_feed"
    assert rss[0].url == "https://example.com/apple"
    assert rss[0].published_at == datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    assert "AAPL" in rss[0].excerpt

    patch_retrieve(monkeypatch, lambda url: ATOM)
    atom = run(provider.search_symbol("MSFT", "Microsoft Corporation", max_results=4))
    assert atom[0].content_type == "news_feed"
    assert atom[0].url == "https://example.com/msft"
    assert atom[0].published_at.year == 2026


def test_google_news_preserves_link_and_parses_publisher(monkeypatch):
    patch_retrieve(monkeypatch, lambda url: GOOGLE_NEWS_RSS)
    provider = TickerNewsFeedProvider(sources=("google_news",))
    items = run(provider.search_symbol("AAPL", "Apple Inc", max_results=4))
    assert items[0].url == "https://news.google.com/rss/articles/CBMiExample"
    assert items[0].publisher == "www.reuters.com"
    assert items[0].content_type == "news_feed"
    assert "AAPL" in items[0].excerpt


def test_nasdaq_requires_symbol_in_tickers_tag(monkeypatch):
    patch_retrieve(monkeypatch, lambda url: NASDAQ_RSS)
    provider = TickerNewsFeedProvider(sources=("nasdaq",))
    items = run(provider.search_symbol("AAPL", "Apple Inc", max_results=4))
    assert [item.url for item in items] == ["https://example.com/tagged"]
    assert run(provider.search_symbol("MSFT", "Microsoft Corporation", max_results=4)) == []


def test_malformed_feed_is_skipped(monkeypatch):
    patch_retrieve(monkeypatch, lambda url: "<not xml" if "news.google" in url else RSS)
    provider = TickerNewsFeedProvider(sources=("google_news", "bing_news"))
    items = run(provider.search("Apple", max_results=4))
    assert [item.url for item in items] == ["https://example.com/apple"]


def test_feed_error_does_not_break_other_sources(monkeypatch):
    patch_retrieve(monkeypatch, lambda url: ValueError("Source unavailable or redirected") if "news.google" in url else RSS)
    provider = TickerNewsFeedProvider(sources=("google_news", "bing_news"))
    assert [item.url for item in run(provider.search("Apple", max_results=4))] == ["https://example.com/apple"]


def test_duplicate_urls_are_deduped(monkeypatch):
    patch_retrieve(monkeypatch, lambda url: RSS)
    provider = TickerNewsFeedProvider(sources=("google_news", "bing_news"))
    assert len(run(provider.search("Apple", max_results=4))) == 1


def test_future_and_undated_items_rejected(monkeypatch):
    patch_retrieve(monkeypatch, lambda url: CUTOFF_FEED)
    provider = TickerNewsFeedProvider(sources=("google_news",))
    cutoff = datetime(2026, 10, 15, tzinfo=timezone.utc)
    items = run(provider.search_symbol("AAPL", "Apple Inc", max_results=4, reporting_cutoff=cutoff))
    assert [item.url for item in items] == ["https://example.com/valid"]
    # Without a cutoff, undated items are still dropped but future-dated ones are left to the engine.
    assert [item.url for item in run(provider.search("Apple", max_results=4))] == [
        "https://example.com/future", "https://example.com/valid"]


def test_non_https_link_is_rejected(monkeypatch):
    patch_retrieve(monkeypatch, lambda url: HTTP_FEED)
    provider = TickerNewsFeedProvider(sources=("google_news",))
    assert run(provider.search_symbol("AAPL", "Apple Inc", max_results=4)) == []


def test_feed_cache_respects_ttl(monkeypatch):
    calls = patch_retrieve(monkeypatch, lambda url: RSS)
    provider = TickerNewsFeedProvider(sources=("google_news",), ttl=300)
    run(provider.search_symbol("AAPL", "Apple Inc", max_results=4))
    run(provider.search_symbol("AAPL", "Apple Inc", max_results=4))
    assert len(calls) == 1


def test_feed_cache_expires(monkeypatch):
    calls = patch_retrieve(monkeypatch, lambda url: RSS)
    provider = TickerNewsFeedProvider(sources=("google_news",), ttl=0)
    run(provider.search_symbol("AAPL", "Apple Inc", max_results=4))
    run(provider.search_symbol("AAPL", "Apple Inc", max_results=4))
    assert len(calls) == 2


def test_sec_filings_emit_official_feed_and_send_fair_access_agent(monkeypatch):
    calls = patch_retrieve(monkeypatch, lambda url: TICKERS_JSON if "company_tickers" in url else SUBMISSIONS_JSON)
    provider = SecFilingsResearchProvider(sec_user_agent="Test Contact test@example.com")
    items = run(provider.search_symbol("AAPL", "Apple Inc", max_results=4))
    assert [item.content_type for item in items] == ["official_feed"]
    assert items[0].url == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/"
    assert items[0].publisher == "sec.gov"
    assert items[0].title == "AAPL 8-K filing"
    assert items[0].published_at == datetime(2026, 10, 15, tzinfo=timezone.utc)
    assert all(call[1].get("user_agent") == "Test Contact test@example.com" for call in calls)


def test_sec_filings_abstain_without_contact(monkeypatch):
    calls = patch_retrieve(monkeypatch, lambda url: TICKERS_JSON)
    provider = SecFilingsResearchProvider(sec_user_agent=None)
    assert run(provider.search_symbol("AAPL", "Apple Inc", max_results=4)) == []
    assert run(provider.search("AAPL latest filings", max_results=4)) == []
    assert calls == []


def test_sec_filings_require_a_ticker(monkeypatch):
    patch_retrieve(monkeypatch, lambda url: TICKERS_JSON if "company_tickers" in url else SUBMISSIONS_JSON)
    provider = SecFilingsResearchProvider(sec_user_agent="Test Contact test@example.com")
    assert run(provider.search("recent Federal Reserve economic policy", max_results=3)) == []


def test_composite_prefers_official_and_dedupes(monkeypatch):
    class Official:
        async def search_symbol(self, symbol, name=None, max_results=4, reporting_cutoff=None):
            return [Evidence(id="official", url="https://example.com/shared", title=f"{symbol} filing",
                             publisher="sec.gov", content_type="official_feed")]

    class News:
        async def search_symbol(self, symbol, name=None, max_results=4, reporting_cutoff=None):
            return [Evidence(id="news", url="https://example.com/shared", title=f"{symbol} headline",
                             publisher="news.example", content_type="news_feed"),
                    Evidence(id="other", url="https://example.com/other", title=f"{symbol} more",
                             publisher="news.example", content_type="news_feed")]

    composite = CompositeResearchProvider([Official(), News()])
    items = run(composite.search_symbol("AAPL", "Apple Inc", max_results=4))
    assert [item.url for item in items] == ["https://example.com/shared", "https://example.com/other"]
    assert items[0].content_type == "official_feed"


def test_composite_retrieve_never_fetches_aggregators(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("aggregator pages must not be fetched")

    monkeypatch.setattr(providers, "retrieve_public_text", boom)
    composite = CompositeResearchProvider([TickerNewsFeedProvider(sources=("google_news",))])
    for content_type in ("news_feed", "official_feed"):
        source = Evidence(id="x", url="https://news.google.com/rss/articles/abc", title="t",
                          publisher="news.google.com", content_type=content_type)
        assert run(composite.retrieve(source)) is source


def test_settings_validate_news_feed_sources():
    assert Settings().news_feed_sources == ["google_news", "nasdaq"]
    assert Settings(news_feed_sources=["Nasdaq", "google_news", "nasdaq"]).news_feed_sources == ["nasdaq", "google_news"]
    with pytest.raises(ValidationError):
        Settings(news_feed_sources=["twitter"])
    assert Settings(sec_company_filings_enabled=True, ticker_news_enabled=True).sec_company_filings_enabled is True


def test_build_dependencies_composes_official_and_news_providers(tmp_path):
    from portfolio_analyzer.secrets import SecretStore
    from portfolio_analyzer.service import build_dependencies
    settings = Settings(search_provider="keyless", sec_contact="Test Contact test@example.com")
    dependencies = build_dependencies(settings, SecretStore(tmp_path, server=True))
    assert isinstance(dependencies.research, CompositeResearchProvider)
    assert dependencies.research.providers[0].__class__.__name__ == "PrimaryFeedResearchProvider"
    assert any(isinstance(provider, SecFilingsResearchProvider) for provider in dependencies.research.providers)
    assert any(isinstance(provider, TickerNewsFeedProvider) for provider in dependencies.research.providers)


def test_build_dependencies_disabled_has_no_research(tmp_path):
    from portfolio_analyzer.secrets import SecretStore
    from portfolio_analyzer.service import build_dependencies
    settings = Settings(search_provider="disabled")
    dependencies = build_dependencies(settings, SecretStore(tmp_path, server=True))
    assert dependencies.research is None


def test_build_dependencies_keeps_keyless_fallback_when_feeds_off(tmp_path):
    from portfolio_analyzer.secrets import SecretStore
    from portfolio_analyzer.service import build_dependencies
    settings = Settings(search_provider="keyless", primary_feeds_enabled=False)
    dependencies = build_dependencies(settings, SecretStore(tmp_path, server=True))
    names = [provider.__class__.__name__ for provider in dependencies.research.providers]
    assert names[0] == "DDGSResearchProvider"
    assert "SecFilingsResearchProvider" in names and "TickerNewsFeedProvider" in names
