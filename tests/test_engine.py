import asyncio
from datetime import datetime, timedelta, timezone
import pytest
from portfolio_analyzer.demo import demo_snapshot
from portfolio_analyzer.engine import EngineDependencies, analyze_portfolio
from portfolio_analyzer.models import AnalysisRequest, Evidence


def test_offline_safe_default():
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=demo_snapshot(), include_suggestions=True)))
    assert result.status == "partial"
    assert result.metrics["known_value"] == "19450"
    assert not result.suggestions
    assert any("investment profile" in warning for warning in result.warnings)
    assert result.schema_version == "1.0"


def test_search_budget_dedup_and_failure():
    class Research:
        calls = 0
        async def search(self, query, max_results):
            self.calls += 1
            return [Evidence(id="one", url="https://example.com/source", title="Example", publisher="example.com")]
    provider = Research()
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=demo_snapshot(), max_searches=2), EngineDependencies(research=provider)))
    assert provider.calls == 2
    assert len(result.sources) == 1


def test_stale_snapshot_and_timezone_validation():
    snapshot = demo_snapshot().model_copy(update={"as_of": datetime.now(timezone.utc) - timedelta(days=5)})
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=snapshot)))
    assert any("permitted data age" in w for w in result.warnings)
    with pytest.raises(ValueError):
        snapshot.model_validate({**snapshot.model_dump(), "as_of": datetime.now()})


def test_cancellation_propagates():
    class Research:
        async def search(self, *args, **kwargs):
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=demo_snapshot()), EngineDependencies(research=Research())))


def test_threshold_is_user_configurable():
    request = AnalysisRequest(snapshot=demo_snapshot(), position_review_threshold_pct=90)
    result = asyncio.run(analyze_portfolio(request))
    assert not any(f.title in {"Single-company concentration", "Large fund allocation", "Large holding allocation"} for f in result.findings)


def test_large_etf_allocation_is_not_single_company_risk():
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=demo_snapshot())))
    funds = [finding for finding in result.findings if finding.title == "Large fund allocation"]
    assert funds
    assert all(finding.kind == "observation" for finding in funds)
    assert not any(finding.title == "Single-company concentration" for finding in result.findings)
    assert "look-through data is unavailable" in funds[0].detail


def test_large_individual_stock_exposure_is_concentration_risk():
    from portfolio_analyzer.models import PortfolioSnapshot, Position
    snapshot = PortfolioSnapshot(as_of=datetime.now(timezone.utc), positions=[Position(symbol="AAPL", quantity=10, price=100)], cash=100)
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=snapshot)))
    risks = [finding for finding in result.findings if finding.title == "Single-company concentration"]
    assert len(risks) == 1
    assert risks[0].kind == "risk"
    assert "AAPL" in risks[0].detail


def test_supplemental_price_reports_actual_trading_date(monkeypatch):
    import pandas as pd
    import yfinance as yf
    from portfolio_analyzer.models import PortfolioSnapshot, Position
    from portfolio_analyzer.providers import YFinanceMarketDataProvider
    frame = pd.DataFrame({"Close": [99.0, 100.0]}, index=pd.to_datetime(["2026-01-02", "2026-01-05"]))
    class Ticker:
        def history(self, **kwargs):
            return frame
    monkeypatch.setattr(yf, "Ticker", lambda symbol: Ticker())
    snapshot = PortfolioSnapshot(as_of=datetime.now(timezone.utc), positions=[Position(symbol="AAPL", quantity=1)])
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=snapshot, benchmark=""), EngineDependencies(market_data=YFinanceMarketDataProvider())))
    metadata = result.metrics["market_data_as_of"]["AAPL"]
    assert metadata["trading_date"] == "2026-01-05"
    assert metadata["valuation_type"] == "daily_close"
    assert metadata["retrieved_at"]
    assert result.metrics["supplied_price_timestamp_unknown"] == []
    assert any("AAPL exceeds the permitted data age" in warning for warning in result.warnings)


def test_supplied_snapshot_time_is_not_claimed_as_price_timestamp():
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=demo_snapshot())))
    assert "VTI" in result.metrics["supplied_price_timestamp_unknown"]
    assert result.metrics["market_data_as_of"] == {}
    assert any("snapshot collection time does not verify quote freshness" in warning for warning in result.warnings)


def test_invalid_model_citations_and_suggestion_gate(monkeypatch):
    from portfolio_analyzer.models import ResearchNarrative, Finding
    async def narrative(model, request, metrics, sources):
        assert not request.include_suggestions
        return ResearchNarrative(findings=[Finding(title="Unsupported", detail="Claim", evidence_ids=["invented"])], suggestions=["Sell everything"])
    monkeypatch.setattr("portfolio_analyzer.agent.generate_narrative", narrative)
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=demo_snapshot(), include_suggestions=True, investment_profile={"objectives": "growth"}), EngineDependencies(model=object())))
    assert not result.suggestions
    assert not any(f.title == "Unsupported" for f in result.findings)
    assert any("invalid evidence" in w for w in result.warnings)


def test_provider_errors_do_not_leak_secrets():
    class Research:
        async def search(self, *args, **kwargs):
            raise RuntimeError("secret_api_key=do-not-display")
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=demo_snapshot(), max_searches=1), EngineDependencies(research=Research())))
    assert "do-not-display" not in result.model_dump_json()


def test_safe_url_rejects_private_addresses(monkeypatch):
    import socket
    from portfolio_analyzer.providers import validate_public_url
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError):
        validate_public_url("https://example.com")
    with pytest.raises(ValueError):
        validate_public_url("http://example.com")


def test_primary_feeds_extract_evidence(monkeypatch):
    from portfolio_analyzer.providers import PrimaryFeedResearchProvider
    async def retrieve(*args, **kwargs):
        return '<rss><channel><item><title>ABC earnings disclosure</title><link>https://example.com/disclosure</link><description>Company results</description><pubDate>Fri, 16 Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>'
    monkeypatch.setattr("portfolio_analyzer.providers.retrieve_public_text", retrieve)
    sources = asyncio.run(PrimaryFeedResearchProvider().search("ABC earnings", max_results=1))
    assert len(sources) == 1
    assert sources[0].published_at.year == 2026
    assert sources[0].content_type == "official_feed"


def test_langchain_parent_tool_accepts_json_without_app_configuration():
    from portfolio_analyzer.agent import create_research_tool
    tool = create_research_tool()
    result = asyncio.run(tool.ainvoke({"request": AnalysisRequest(snapshot=demo_snapshot()).model_dump(mode="json")}))
    assert result["schema_version"] == "1.0"
    assert result["status"] == "partial"
    assert result["metrics"]["known_value"] == "19450"


def test_profile_types_and_source_evidence_required(monkeypatch):
    from portfolio_analyzer.models import ResearchNarrative, Finding
    async def narrative(model, request, metrics, sources):
        assert request.include_suggestions
        return ResearchNarrative(findings=[Finding(title="Uncited market claim", detail="Price move explained"), Finding(title="Backed disclosure", detail="Company disclosed results", evidence_ids=["one"])], suggestions=["Review allocation in light of your objectives"])
    class Research:
        async def search(self, *args, **kwargs):
            return [Evidence(id="one", url="https://example.com", title="Disclosure", publisher="example.com", content_type="retrieved_page")]
    monkeypatch.setattr("portfolio_analyzer.agent.generate_narrative", narrative)
    request = AnalysisRequest(snapshot=demo_snapshot(), max_searches=1, include_suggestions=True, investment_profile={"objectives": "growth", "horizon": "10 years", "risk_tolerance": "moderate", "liquidity_needs": "none near term", "allocation_targets": {"stocks": 80, "bonds": 20}, "restrictions": []})
    result = asyncio.run(analyze_portfolio(request, EngineDependencies(model=object(), research=Research())))
    assert result.suggestions
    assert any(f.title == "Backed disclosure" for f in result.findings)
    assert not any(f.title == "Uncited market claim" for f in result.findings)


def test_dependency_budget_caps_searches():
    class Research:
        calls = 0
        async def search(self, *args, **kwargs):
            self.calls += 1
            return []
    provider = Research()
    asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=demo_snapshot(), max_searches=20), EngineDependencies(research=provider, hard_search_limit=1)))
    assert provider.calls == 1
