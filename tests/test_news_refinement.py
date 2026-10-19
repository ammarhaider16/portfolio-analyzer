"""Deterministic mixed-quality news fixtures independent of live network access."""
import asyncio
from datetime import datetime, timedelta, timezone

from portfolio_analyzer.engine import EngineDependencies, analyze_portfolio
from portfolio_analyzer.models import AnalysisRequest, Evidence, PortfolioSnapshot, Position
from portfolio_analyzer.news import cluster_stories, publisher_label


NOW = datetime.now(timezone.utc) - timedelta(minutes=1)


def test_shared_url_upgrade_refreshes_all_holding_evidence_grades():
    class SharedNews:
        async def search_symbol(self, symbol, name, **kwargs):
            return [source(symbol, title='Apple and Microsoft announce merger', url='https://reuters.com/shared',
                           content_type='news_feed' if symbol == 'AAPL' else 'retrieved_page')]

    snapshot = PortfolioSnapshot(as_of=NOW, positions=[
        Position(symbol='AAPL', quantity=2, price=100, instrument_name='Apple Inc'),
        Position(symbol='MSFT', quantity=1, price=100, instrument_name='Microsoft Corporation')])
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=snapshot, max_searches=2, benchmark=''),
                                          EngineDependencies(research=SharedNews())))
    assert len(result.sources) == 1
    assert result.sources[0].content_type == 'retrieved_page'
    assert all(item['evidence_grade'] == 'full' for item in result.metrics['position_news'])
    assert all(item['stories'][0]['evidence_grade'] == 'full' for item in result.metrics['position_news'])


def test_sec_only_composite_has_no_market_wide_capability():
    from portfolio_analyzer.providers import CompositeResearchProvider, SecFilingsResearchProvider
    snapshot = PortfolioSnapshot(as_of=NOW, positions=[
        Position(symbol='AAPL', quantity=1, price=100), Position(symbol='MSFT', quantity=1, price=100)])
    provider = CompositeResearchProvider([SecFilingsResearchProvider()])
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=snapshot, max_searches=4, benchmark=''),
                                          EngineDependencies(research=provider)))
    assert result.metrics['setup_news_reason'] == 'unsupported_provider'


def source(identifier, title="Apple beats Q4 estimates on iPhone demand", **changes):
    values = dict(id=identifier, title=title, url=f"https://reuters.com/{identifier}", publisher="Reuters",
                  content_type="news_feed", published_at=NOW)
    values.update(changes)
    return Evidence(**values)


def test_clustering_preserves_best_grade_and_related_reports():
    headline = source("headline", title="Apple beats Q4 estimates on iPhone demand - Business Insider",
                      publisher="Business Insider", url="https://businessinsider.com/apple")
    full = source("full", content_type="retrieved_page")
    stories = cluster_stories([headline, full], "AAPL")
    assert len(stories) == 1
    assert stories[0]["representative_source_id"] == "full"
    assert stories[0]["related_report_count"] == 1
    assert stories[0]["evidence_grade"] == "full"
    assert stories[0]["category"] == "event"


def test_shingle_matches_and_distinct_updates_are_retained():
    first = source("a")
    near = source("b", title="Apple beats Q4 estimates on iPhone demand today")
    old = source("old", published_at=NOW - timedelta(hours=49))
    figure = source("figure", title="Apple beats Q3 estimates on iPhone demand")
    event = source("event", title="Apple cuts Q4 guidance on iPhone demand")
    stories = cluster_stories([first, near, old, figure, event], "AAPL")
    assert len(stories) == 4
    assert any(set(item["member_source_ids"]) == {"a", "b"} for item in stories)


def test_conservative_events_first_and_publisher_never_upgrades_evidence():
    opinion = source("opinion", title="Could Apple earnings unlock its next leg of growth?", content_type="retrieved_page")
    event = source("event", url="https://finance.yahoo.com/event", publisher="Yahoo Finance")
    stories = cluster_stories([opinion, event], "AAPL")
    assert [item["category"] for item in stories] == ["event", "also"]
    assert stories[0]["evidence_grade"] == "headline"
    assert stories[0]["publisher_tier"] == "secondary"
    assert publisher_label(source("gov", url="https://www.sec.gov/archive")) == ("sec.gov", "official")


def test_engine_clusters_before_two_story_cap_and_exposes_extra_stories():
    class Research:
        async def search(self, query, max_results=3):
            return [source("a"), source("b", url="https://businessinsider.com/b", publisher="Business Insider"),
                    source("c", title="Apple announces dividend increase"),
                    source("d", title="Could Apple grow next year?")]

    snapshot = PortfolioSnapshot(as_of=NOW, positions=[Position(symbol="AAPL", quantity=1, price=100, instrument_name="Apple Inc")])
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=snapshot, max_searches=1, benchmark=""), EngineDependencies(research=Research())))
    entry = result.metrics["position_news"][0]
    assert len(entry["stories"]) == 2
    assert entry["stories"][0]["related_report_count"] == 1
    assert len(entry["additional_stories"]) == 1
    assert result.metrics["setup_news_reason"] == "budget_exhausted"
    assert len(result.sources) == 4


def test_display_order_and_market_skip_reasons():
    class SymbolOnly:
        async def search_symbol(self, symbol, name, **kwargs):
            return []

    snapshot = PortfolioSnapshot(as_of=NOW, positions=[
        Position(symbol="MSFT", quantity=1, price=100, instrument_name="Microsoft Corporation"),
        Position(symbol="AAPL", quantity=3, price=100, instrument_name="Apple Inc"),
        Position(symbol="ZZZZ", quantity=1, instrument_name="Unknown company"),
        Position(symbol="AAAA", quantity=1, instrument_name="Other company")])
    request = AnalysisRequest(snapshot=snapshot, max_searches=6, benchmark="")
    result = asyncio.run(analyze_portfolio(request, EngineDependencies(research=SymbolOnly())))
    assert result.metrics["position_news_display_order"] == ["AAPL", "MSFT", "AAAA", "ZZZZ"]
    assert result.metrics["setup_news_reason"] == "unsupported_provider"
    result = asyncio.run(analyze_portfolio(request, EngineDependencies()))
    assert result.metrics["setup_news_reason"] == "research_disabled"
