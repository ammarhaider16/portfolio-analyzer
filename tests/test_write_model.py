"""The optional writer changes finalization, not the researcher."""

import asyncio
from portfolio_analyzer.models import AnalysisRequest, PortfolioSnapshot, ResearchNarrative
from portfolio_analyzer.engine import EngineDependencies, analyze_portfolio
from datetime import datetime, timezone


def test_engine_passes_primary_and_optional_writer_separately(monkeypatch):
    primary, writer = object(), object()
    seen = []

    async def narrative(model, *args, **kwargs):
        seen.append((model, kwargs["write_model"]))
        return ResearchNarrative(executive_summary="A briefing.")

    monkeypatch.setattr("portfolio_analyzer.agent.generate_narrative", narrative)
    asyncio.run(
        analyze_portfolio(
            AnalysisRequest(snapshot=PortfolioSnapshot(as_of=datetime.now(timezone.utc))),
            EngineDependencies(model=primary, write_model=writer),
        )
    )
    assert seen == [(primary, writer)]
