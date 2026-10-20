"""Read-only portfolio draft enrichment agent backed by exact-symbol market tools."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from .models import PortfolioSnapshot, Position
from .providers import YFinanceMarketDataProvider
from .imports import DraftPosition


class PortfolioEnrichmentAgent:
    """Propose trusted missing fields; never infer quantities or save a portfolio."""

    def __init__(self, market_data=None):
        self.market_data = market_data or YFinanceMarketDataProvider(refresh_existing=False)

    async def enrich(self, positions: list[DraftPosition]) -> dict:
        if len(positions) > 30:
            raise ValueError("Fill details supports up to 30 holdings at a time; split larger imports for review")
        limit = asyncio.Semaphore(4)

        async def lookup(item):
            async with limit:
                updated = item.model_copy(deep=True)
                filled = []
                source = None
                price_as_of = None
                try:
                    identity = await asyncio.wait_for(self.market_data.resolve_instrument(item.symbol), timeout=8)
                except Exception:
                    identity = None
                if identity and identity.get("symbol", "").upper() == item.symbol:
                    source = identity.get("source", "market data")
                    for field in ("instrument_name", "exchange", "sector"):
                        if not getattr(updated, field) and identity.get(field):
                            setattr(updated, field, identity[field])
                            filled.append(field)
                    if updated.asset_type == "unknown" and identity.get("asset_type"):
                        updated.asset_type = identity["asset_type"]
                        filled.append("asset_type")
                if updated.price is None:
                    probe = PortfolioSnapshot(as_of=datetime.now(timezone.utc), positions=[
                        Position(symbol=item.symbol, quantity=Decimal(1))
                    ])
                    try:
                        priced, metadata = await asyncio.wait_for(self.market_data.enrich_with_metadata(probe), timeout=12)
                    except Exception:
                        priced, metadata = probe, {}
                    quote = priced.positions[0]
                    if updated.price is None and quote.price is not None:
                        updated.price = quote.price
                        filled.append("price")
                    if updated.price is not None and updated.previous_close is None and quote.previous_close is not None:
                        updated.previous_close = quote.previous_close
                        filled.append("previous_close")
                    if metadata.get(item.symbol):
                        source = metadata[item.symbol].get("source", source)
                        price_as_of = metadata[item.symbol].get("trading_date")
                return updated, {"symbol": item.symbol, "fields": filled, "source": source,
                                 "price_as_of": price_as_of}

        results = await asyncio.gather(*(lookup(item) for item in positions))
        return {"positions": [item.model_dump(mode="json") for item, _ in results],
                "changes": [change for _, change in results if change["fields"]],
                "unresolved": [item.symbol for item, change in results if not change["fields"]]}
