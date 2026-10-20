"""Acceptance checks for reviewable metadata and all-position news coverage."""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_analyzer.engine import EngineDependencies, analyze_portfolio
from portfolio_analyzer.enrichment import PortfolioEnrichmentAgent
from portfolio_analyzer.models import AnalysisRequest, PortfolioSnapshot
from portfolio_analyzer.imports import DraftPosition
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


def _feed_research(monkeypatch, *rows, resolve=None):
    """A real TickerNewsFeedProvider over stubbed feed responses; never the network."""
    import portfolio_analyzer.providers as providers
    from portfolio_analyzer.providers import TickerNewsFeedProvider
    from email.utils import format_datetime
    body = '<?xml version="1.0"?><rss version="2.0"><channel>' + "".join(
        f"<item><title>{title}</title><link>{link}</link><description>{description}</description>"
        f"<pubDate>{format_datetime(datetime.now(timezone.utc) - timedelta(hours=2))}</pubDate></item>"
        for title, link, description in rows) + "</channel></rss>"

    async def fake(url, *args, **kwargs):
        return body

    async def resolve_fake(url):
        return (resolve or {}).get(url, "")

    monkeypatch.setattr(providers, "retrieve_public_text", fake)
    monkeypatch.setattr(providers, "resolve_feed_link", resolve_fake)
    return TickerNewsFeedProvider(sources=("google_news",))


def _run(research, position):
    snapshot = PortfolioSnapshot(as_of=datetime.now(timezone.utc), positions=[position])
    return asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=snapshot, max_searches=8, benchmark=""),
                                         EngineDependencies(research=research)))
