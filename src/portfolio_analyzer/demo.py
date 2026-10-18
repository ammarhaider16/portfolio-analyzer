"""Offline demonstration data; never presented as a live account."""
from datetime import datetime, timezone
from .models import PortfolioSnapshot, Position


def demo_snapshot() -> PortfolioSnapshot:
    return PortfolioSnapshot(as_of=datetime.now(timezone.utc), source="demonstration", cash="2500", positions=[
        Position(symbol="VTI", quantity="30", price="280", previous_close="278", asset_type="etf"),
        Position(symbol="VXUS", quantity="40", price="65", previous_close="65.2", asset_type="etf"),
        Position(symbol="BND", quantity="50", price="74", previous_close="73.9", asset_type="etf"),
        Position(symbol="AAPL", quantity="10", price="225", previous_close="222", sector="Technology"),
    ])
