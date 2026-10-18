"""Scheduled analysis refreshes valuations, never the user's stored positions."""
from datetime import datetime, timezone

import pandas as pd

from portfolio_analyzer.models import PortfolioSnapshot, Position
from portfolio_analyzer.providers import YFinanceMarketDataProvider


async def test_prices_refresh_without_changing_manual_quantities_or_cash(monkeypatch):
    import yfinance as yf
    prices = pd.DataFrame({"Close": [119.0, 120.0]}, index=pd.to_datetime(["2026-10-15", "2026-10-16"]))

    class Ticker:
        def history(self, **kwargs):
            return prices

    monkeypatch.setattr(yf, "Ticker", lambda symbol: Ticker())
    saved = PortfolioSnapshot(as_of=datetime(2026, 10, 1, tzinfo=timezone.utc), cash="1250.50", source="manual",
                              positions=[Position(symbol="ABC", quantity="7", price="50", previous_close="49")])
    updated, metadata = await YFinanceMarketDataProvider().enrich_with_metadata(saved)
    assert updated.positions[0].price == 120
    assert updated.positions[0].previous_close == 119
    assert updated.positions[0].quantity == saved.positions[0].quantity == 7
    assert updated.cash == saved.cash
    assert updated.as_of == saved.as_of
    assert saved.positions[0].price == 50
    assert metadata["ABC"]["trading_date"] == "2026-10-16"


async def test_missing_quote_does_not_invent_a_fresh_valuation(monkeypatch):
    import yfinance as yf

    class Ticker:
        def history(self, **kwargs):
            return pd.DataFrame()

    monkeypatch.setattr(yf, "Ticker", lambda symbol: Ticker())
    saved = PortfolioSnapshot(as_of=datetime.now(timezone.utc), positions=[Position(symbol="ABC", quantity=1, price=50)])
    updated, metadata = await YFinanceMarketDataProvider().enrich_with_metadata(saved)
    assert updated.positions[0].price == 50
    assert metadata == {}
