"""Acceptance checks for reviewable metadata and all-position news coverage."""
import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_analyzer.delivery import render_report
from portfolio_analyzer.engine import EngineDependencies, analyze_portfolio
from portfolio_analyzer.enrichment import PortfolioEnrichmentAgent
from portfolio_analyzer.models import AnalysisRequest, Evidence, PortfolioSnapshot, Position
from portfolio_analyzer.screenshot_import import DraftPosition
from portfolio_analyzer.web import create_app


def test_enrichment_preserves_user_prices_and_missing_quantities():
    class Market:
        async def resolve_instrument(self, symbol):
            return {"symbol": symbol, "asset_type": "stock", "instrument_name": "Apple Inc.",
                    "sector": "Technology", "exchange": "Nasdaq", "source": "market"}

        async def enrich_with_metadata(self, snapshot):
            row = snapshot.positions[0]
            row.price = Decimal("210")
            row.previous_close = Decimal("205")
            return snapshot, {row.symbol: {"source": "market", "trading_date": "2026-10-17"}}

    draft = DraftPosition(symbol="AAPL", quantity=None, price=200)
    result = asyncio.run(PortfolioEnrichmentAgent(Market()).enrich([draft]))
    enriched = result["positions"][0]
    assert enriched["quantity"] is None
    assert enriched["price"] == "200"
    assert enriched["previous_close"] is None
    assert enriched["instrument_name"] == "Apple Inc."
    assert "price" not in result["changes"][0]["fields"]

    missing_price = asyncio.run(PortfolioEnrichmentAgent(Market()).enrich([DraftPosition(symbol="AAPL")]))
    quote = missing_price["positions"][0]
    assert quote["price"] == "210" and quote["previous_close"] == "205"
    assert missing_price["changes"][0]["price_as_of"] == "2026-10-17"


def test_detailed_news_reports_every_holding_and_verified_source():
    class Research:
        async def search(self, query, max_results=3):
            if "Apple Inc" in query:
                return [Evidence(id="apple", url="https://example.org/apple", title="Apple Inc results",
                                 publisher="example.org", published_at=datetime.now(timezone.utc),
                                 content_type="retrieved_page")]
            return []

    snapshot = PortfolioSnapshot(as_of=datetime.now(timezone.utc), positions=[
        Position(symbol="AAPL", quantity=2, price=200, asset_type="stock", instrument_name="Apple Inc"),
        Position(symbol="MSFT", quantity=1, price=300, asset_type="stock", instrument_name="Microsoft Corporation")])
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=snapshot, report_length="detailed", max_searches=3),
                                           EngineDependencies(research=Research())))
    assert [(item["symbol"], item["status"]) for item in result.coverage["position_news"]] == [
        ("AAPL", "verified"), ("MSFT", "no_verified_update")]
    html, plain = render_report(result)
    assert "Apple Inc results" in html and "https://example.org/apple" in plain
    assert "MSFT: no verified recent position news found" in plain
    assert "MSFT: price $300" in plain
    assert "Market and policy developments" in plain


def test_search_limit_names_unchecked_holding_in_detailed_report():
    class Research:
        async def search(self, query, max_results=3):
            return []

    snapshot = PortfolioSnapshot(as_of=datetime.now(timezone.utc), positions=[
        Position(symbol=symbol, quantity=1, price=price, instrument_name=name)
        for symbol, price, name in [("AAPL", 300, "Apple Inc"), ("MSFT", 200, "Microsoft Corporation"),
                                    ("GOOG", 100, "Alphabet Inc")]])
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=snapshot, report_length="detailed", max_searches=2),
                                           EngineDependencies(research=Research())))
    assert result.coverage["position_news"][2]["status"] == "not_checked"
    assert any("GOOG" in issue["message"] for issue in result.coverage["issues"])
    assert "GOOG: news was not checked in this run" in render_report(result)[1]


def test_position_news_searches_are_bounded_and_concurrent():
    active = 0
    maximum = 0

    class Research:
        async def search(self, query, max_results=3):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.03)
                return []
            finally:
                active -= 1

    snapshot = PortfolioSnapshot(as_of=datetime.now(timezone.utc), positions=[
        Position(symbol=f"T{i:02}", quantity=1, price=10, instrument_name=f"Company {i}") for i in range(9)])
    result = asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=snapshot, max_searches=9),
                                           EngineDependencies(research=Research())))
    assert maximum == 4
    assert all(item["status"] == "no_verified_update" for item in result.coverage["position_news"])


def test_enrichment_route_does_not_save_draft(tmp_path, monkeypatch):
    async def fake_enrich(self, positions):
        return {"positions": [item.model_dump(mode="json") for item in positions], "changes": [], "unresolved": [item.symbol for item in positions]}

    monkeypatch.setattr(PortfolioEnrichmentAgent, "enrich", fake_enrich)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8000") as client:
        page = client.get("/")
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        response = client.post("/api/portfolio/enrich", headers={"x-csrf-token": token},
                               json={"positions": [{"symbol": "AAPL", "price": "200"}]})
        assert response.status_code == 200
        assert response.json()["positions"][0]["quantity"] is None
        assert client.get("/api/status").json()["snapshot"] is None
