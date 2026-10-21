"""Automatic server-side price polling: batch quotes, TTL, and the quotes API."""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from betafolio.config import Settings
from betafolio.models import PortfolioSnapshot, Position
from betafolio.providers import YFinanceMarketDataProvider
from betafolio.service import QuoteService
from betafolio.storage import Store
from betafolio.web import create_app


def settings(**overrides):
    return Settings(search_provider="disabled", primary_feeds_enabled=False, **overrides)


def quote(price="120", previous_close="119", retrieved_at=None, source="yfinance"):
    return {"price": Decimal(price), "previous_close": None if previous_close is None else Decimal(previous_close),
            "trading_date": "2026-10-16", "retrieved_at": retrieved_at or datetime.now(timezone.utc).isoformat(),
            "source": source, "quote_as_of":retrieved_at, "valuation_type":"regular_session"}


class FakeProvider:
    def __init__(self, quotes=None):
        self._quotes = quotes or {}
        self.calls = 0
        self.requested = []

    async def quotes(self, symbols):
        self.calls += 1
        self.requested.append(list(symbols))
        return {symbol: value for symbol, value in self._quotes.items() if symbol in symbols}








def saved(tmp_path, **kwargs):
    store = Store(tmp_path)
    snapshot = PortfolioSnapshot(as_of=datetime(2026, 10, 1, 12, tzinfo=timezone.utc), cash="1250.50", source="manual",
                                 positions=[Position(symbol="ABC", quantity="7", price="50", previous_close="49")], **kwargs)
    store.save_snapshot(snapshot)
    return store


def test_refresh_updates_prices_but_never_quantity_cash_or_as_of(tmp_path):
    store = saved(tmp_path)
    retrieved = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)  # Friday, US market open
    provider = FakeProvider({"ABC": quote(retrieved_at=retrieved.isoformat())})
    service = QuoteService(store, provider, lambda: settings(auto_prices_enabled=True, price_refresh_minutes=15))
    asyncio.run(service.refresh(now=retrieved))
    assert provider.calls == 1
    persisted = store.snapshot()
    assert persisted.positions[0].price == Decimal("120")
    assert persisted.positions[0].previous_close == Decimal("119")
    assert persisted.positions[0].quantity == Decimal("7")
    assert persisted.cash == Decimal("1250.50")
    assert persisted.as_of == datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    assert store.get("prices_as_of")["ABC"] == {"trading_date": "2026-10-16",
                                                "retrieved_at": retrieved.isoformat(), "source": "yfinance",
                                                "quote_as_of":retrieved.isoformat(), "valuation_type":"regular_session"}
    assert service.as_of() == retrieved.isoformat()


def test_second_call_within_ttl_does_not_refetch(tmp_path):
    store = saved(tmp_path)
    retrieved = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)
    provider = FakeProvider({"ABC": quote(retrieved_at=retrieved.isoformat())})
    service = QuoteService(store, provider, lambda: settings(auto_prices_enabled=True, price_refresh_minutes=15))
    asyncio.run(service.refresh(now=retrieved))
    asyncio.run(service.refresh(now=retrieved + timedelta(seconds=59)))
    assert provider.calls == 1
    asyncio.run(service.refresh(now=retrieved + timedelta(seconds=61)))
    assert provider.calls == 2




def test_partial_quotes_leave_missing_symbols_untouched(tmp_path):
    store = Store(tmp_path)
    store.save_snapshot(PortfolioSnapshot(as_of=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
                                          positions=[Position(symbol="ABC", quantity="1", price="50"),
                                                      Position(symbol="XYZ", quantity="1", price="5")]))
    provider = FakeProvider({"ABC": quote(price="60", previous_close="59")})
    service = QuoteService(store, provider, lambda: settings(auto_prices_enabled=True))
    asyncio.run(service.refresh(now=datetime.now(timezone.utc)))
    persisted = {position.symbol: position for position in store.snapshot().positions}
    assert persisted["ABC"].price == Decimal("60")
    assert persisted["XYZ"].price == Decimal("5")  # no invented valuation for the missing symbol
    assert set(store.get("prices_as_of")) == {"ABC"}


def test_disabled_auto_prices_never_fetches(tmp_path):
    store = saved(tmp_path)
    provider = FakeProvider({"ABC": quote()})
    service = QuoteService(store, provider, lambda: settings(auto_prices_enabled=False))
    asyncio.run(service.refresh())
    assert provider.calls == 0
    assert store.snapshot().positions[0].price == Decimal("50")
    assert store.get("prices_as_of") is None


def test_provider_failure_leaves_saved_prices(tmp_path):
    store = saved(tmp_path)
    provider = FakeProvider({"ABC": quote()})
    service = QuoteService(store, provider, lambda: settings(auto_prices_enabled=True))

    async def boom(symbols):
        raise RuntimeError("offline")

    provider.quotes = boom
    asyncio.run(service.refresh())
    assert store.snapshot().positions[0].price == Decimal("50")


def test_api_quotes_returns_saved_portfolio_shape(tmp_path, monkeypatch):
    retrieved = datetime.now(timezone.utc).isoformat()

    async def quotes(self, symbols):
        return {symbol: quote(price="12.5", previous_close="12", retrieved_at=retrieved) for symbol in symbols}

    monkeypatch.setattr(YFinanceMarketDataProvider, "quotes", quotes)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8000") as client:
        token = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/").text).group(1)
        snapshot = {"as_of": "2026-10-18T12:00:00Z", "cash": "100",
                    "positions": [{"symbol": "ABC", "quantity": "2", "price": None}]}
        response = client.post("/api/portfolio", json={"snapshot": snapshot}, headers={"x-csrf-token": token})
        assert response.status_code == 200, response.text
        data = client.get("/api/quotes").json()
        assert set(data) == {"prices", "as_of", "refresh_seconds"}
        assert data["prices"]["ABC"]["price"] == "12.5"
        assert data["prices"]["ABC"]["previous_close"] == "12"
        assert data["prices"]["ABC"]["trading_date"] == "2026-10-16"
        assert data["prices"]["ABC"]["source"] == "yfinance"
        assert data["as_of"] == retrieved
        assert isinstance(data["refresh_seconds"], int) and data["refresh_seconds"] > 0
        assert client.get("/api/status").json()["prices"]["ABC"]["price"] == "12.5"
