"""Business-level analysis checks; report wording belongs to the analyst."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from betafolio.engine import EngineDependencies, analyze_portfolio
from betafolio.models import (
    AnalysisRequest,
    PortfolioSnapshot,
    Position,
    ResearchNarrative,
    ReportSection,
)


def snapshot():
    return PortfolioSnapshot(
        as_of=datetime.now(timezone.utc), cash=100, positions=[Position(symbol="AAPL", quantity=2, price=100)]
    )


def test_offline_portfolio_keeps_holdings_and_cash():
    request = AnalysisRequest(snapshot=snapshot())
    result = asyncio.run(analyze_portfolio(request))
    assert result.metrics["known_value"] == "300"
    assert result.executive_summary and request.snapshot.positions[0].quantity == 2


def test_bad_market_data_cannot_poison_saved_values():
    class Market:
        async def enrich(self, value):
            value.positions[0].price = Decimal("NaN")
            return value

    result = asyncio.run(
        analyze_portfolio(AnalysisRequest(snapshot=snapshot()), EngineDependencies(market_data=Market()))
    )
    assert result.metrics["known_value"] == "300" and result.status != "failed"


def test_analyst_report_is_not_rewritten_or_rejected_for_missing_citations(monkeypatch):
    async def narrative(*args, **kwargs):
        return ResearchNarrative(
            executive_summary="The fund mix matters more than one headline.",
            sections=[
                ReportSection(
                    heading="A connected view", content="The positions share an exposure worth watching."
                )
            ],
        )

    monkeypatch.setattr("betafolio.agent.generate_narrative", narrative)
    result = asyncio.run(
        analyze_portfolio(AnalysisRequest(snapshot=snapshot()), EngineDependencies(model=object()))
    )
    assert result.executive_summary == "The fund mix matters more than one headline."
    assert result.sections[0].heading == "A connected view"
