"""Engine acceptance for headline-grade news evidence and in-run source timing."""
import asyncio
from datetime import datetime, timezone

from portfolio_analyzer.engine import EngineDependencies, _entity_matches, analyze_portfolio
from portfolio_analyzer.models import AnalysisRequest, Evidence, PortfolioSnapshot, Position


def snapshot_for(symbol="AAPL", name="Apple Inc", **overrides):
    position = {"symbol": symbol, "quantity": 1, "price": 100, "asset_type": "stock", "instrument_name": name}
    position.update(overrides)
    return PortfolioSnapshot(as_of=datetime.now(timezone.utc), positions=[Position(**position)])


def test_headline_news_feed_is_accepted_and_graded_headline():
    """A dated headline published during the run must not be discarded as future evidence."""
    class Research:
        async def search(self, query, max_results=3):
            return [Evidence(id="hf", url="https://news.example.org/apple", title="Apple Stock rises on results",
                             publisher="news.example.org", published_at=datetime.now(timezone.utc),
                             content_type="news_feed")]

    result = asyncio.run(analyze_portfolio(
        AnalysisRequest(snapshot=snapshot_for(), max_searches=1, benchmark=""),
        EngineDependencies(research=Research())))
    assert [source.content_type for source in result.sources] == ["news_feed"]
    entry = result.coverage["position_news"][0]
    assert entry["status"] == "verified"
    assert entry["evidence_grade"] == "headline"
    assert result.coverage["headline_only_evidence"] is True


def test_headline_style_name_without_legal_suffix_still_matches():
    class Research:
        async def search(self, query, max_results=3):
            return [Evidence(id="short", url="https://news.example.org/aapl", title="Apple Stock falls after guidance",
                             publisher="news.example.org", published_at=datetime.now(timezone.utc),
                             content_type="news_feed")]

    result = asyncio.run(analyze_portfolio(
        AnalysisRequest(snapshot=snapshot_for(name="Apple Inc."), max_searches=1, benchmark=""),
        EngineDependencies(research=Research())))
    assert [source.id for source in result.sources] == ["short"]


def test_symbol_aware_provider_is_preferred_for_positions():
    calls = []

    class Research:
        async def search_symbol(self, symbol, name, max_results=4, reporting_cutoff=None):
            calls.append((symbol, name))
            return [Evidence(id="sec", url="https://www.sec.gov/Archives/edgar/data/320193/x-index.htm",
                             title="AAPL 8-K filing", publisher="sec.gov",
                             published_at=datetime.now(timezone.utc), content_type="official_feed")]

        async def search(self, query, max_results=3):
            raise AssertionError("the symbol-aware path should be used for a holding")

    result = asyncio.run(analyze_portfolio(
        AnalysisRequest(snapshot=snapshot_for(), max_searches=1, benchmark=""),
        EngineDependencies(research=Research())))
    assert calls == [("AAPL", "Apple Inc")]
    assert [source.content_type for source in result.sources] == ["official_feed"]


def test_ambiguous_common_word_ticker_still_needs_full_name():
    position = Position(symbol="CAN", quantity=1, instrument_name=None)
    collision = Evidence(id="c", url="https://example.com/x", title="Can we do this", publisher="example.com",
                         excerpt="Can do anything")
    assert not _entity_matches(collision, position)
