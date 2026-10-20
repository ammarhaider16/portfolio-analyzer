"""Portfolio arithmetic and current quotes support an autonomous research analyst."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .metrics import calculate_metrics
from .models import AnalysisRequest, AnalysisResult, PortfolioSnapshot, ReportSection


async def _search_events(
    provider, query: str, max_results: int, cutoff, symbol: str | None = None, name: str | None = None
):
    from .providers import CompositeResearchProvider, source_network_slot

    if isinstance(provider, CompositeResearchProvider):
        return await _dispatch_search_events(provider, query, max_results, cutoff, symbol, name)
    async with source_network_slot():
        return await _dispatch_search_events(provider, query, max_results, cutoff, symbol, name)


async def _dispatch_search_events(provider, query, max_results, cutoff, symbol=None, name=None):
    if symbol:
        from .providers import search_asset_query

        return await search_asset_query(provider, symbol, name, query, max_results, cutoff)
    if not symbol and hasattr(provider, "search_events_candidates"):
        return await provider.search_events_candidates(
            query, max_results=max_results, reporting_cutoff=cutoff
        )
    if symbol and hasattr(provider, "search_symbol"):
        return await provider.search_symbol(symbol, name, max_results=max_results, reporting_cutoff=cutoff)
    if hasattr(provider, "search_events"):
        return await provider.search_events(query, max_results=max_results, reporting_cutoff=cutoff)
    return await provider.search(query, max_results=max_results)


@dataclass
class EngineDependencies:
    market_data: Any = None
    instrument_resolver: Any = None
    research: Any = None
    model: Any = None
    history_enabled: bool = False
    hard_search_limit: int = 500
    hard_seconds_limit: int = 1800
    hard_model_limit: int = 256
    hard_tool_limit: int = 1000
    activity_sink: Any = None
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2), repr=False)


async def analyze_portfolio(request, dependencies=None):
    from .providers import source_network_scope

    request = AnalysisRequest.model_validate(request)
    dependencies = dependencies or EngineDependencies()
    request = request.model_copy(
        update={
            name: min(getattr(request, name), getattr(dependencies, hard))
            for name, hard in (
                ("max_searches", "hard_search_limit"),
                ("max_seconds", "hard_seconds_limit"),
                ("max_model_calls", "hard_model_limit"),
                ("max_tool_calls", "hard_tool_limit"),
            )
        }
    )
    result = AnalysisResult(snapshot_as_of=request.snapshot.as_of)
    deadline = asyncio.get_running_loop().time() + request.max_seconds
    try:
        async with asyncio.timeout(request.max_seconds):
            async with dependencies.semaphore:
                with source_network_scope(limit=4):
                    await _analyze(request, dependencies, result, deadline)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        result.status = "partial"
        result.warnings.append(
            "Research stopped before the briefing was complete. Your saved portfolio is available below."
        )
    except Exception:
        result.status = "partial"
        result.error = "The briefing could not be completed. Please try again."
    if not result.metrics:
        result.metrics = calculate_metrics(request.snapshot)
    if not result.executive_summary:
        result.executive_summary = _portfolio_summary(result.metrics)
    result.coverage = {
        "narrative_state": "available"
        if result.research_budget.get("analysis_written")
        else "not_written"
        if dependencies.model
        else "unavailable",
        "valued_positions": result.metrics.get("priced_positions", 0),
        "total_positions": len(request.snapshot.positions),
    }
    return result


def _portfolio_summary(metrics):
    value = float(metrics.get("known_value") or 0)
    return f"Your saved portfolio is valued at ${value:,.2f}, including {metrics.get('cash_weight_pct', 0):.1f}% in cash."


async def _analyze(request, dependencies, result, deadline):
    from .providers import expected_market_session

    if request.snapshot.source == "demonstration":
        result.warnings.append(
            "DEMONSTRATION PORTFOLIO: illustrative holdings and prices, not current market data."
        )
    snapshot = request.snapshot.model_copy(deep=True)
    price_metadata = {}
    if dependencies.market_data:
        try:
            candidate = snapshot.model_copy(deep=True)
            if hasattr(dependencies.market_data, "enrich_with_metadata"):
                candidate, price_metadata = await dependencies.market_data.enrich_with_metadata(candidate)
            else:
                candidate = await dependencies.market_data.enrich(candidate)
            snapshot = PortfolioSnapshot.model_validate(candidate.model_dump())
        except Exception:
            price_metadata = {}
            result.warnings.append("Current prices could not be refreshed; saved values are shown.")
    resolver = dependencies.instrument_resolver or dependencies.market_data
    if resolver and hasattr(resolver, "resolve_instrument"):

        async def fill(position):
            if position.instrument_name and position.asset_type != "unknown":
                return
            try:
                async with asyncio.timeout(5):
                    data = await resolver.resolve_instrument(position.symbol)
                if data and data.get("symbol") == position.symbol:
                    for name in ("instrument_name", "sector", "exchange"):
                        if not getattr(position, name):
                            setattr(position, name, data.get(name))
                    if position.asset_type == "unknown":
                        position.asset_type = data.get("asset_type", "unknown")
            except Exception:
                pass

        await asyncio.gather(*(fill(position) for position in snapshot.positions))
    session = expected_market_session()
    dated = []
    for position in snapshot.positions:
        metadata = price_metadata.get(position.symbol, {})
        observed = metadata.get("trading_date") or (
            position.price_session.isoformat() if position.price_session else None
        )
        current = observed == session
        dated.append(
            {
                "symbol": position.symbol,
                "price": str(position.price) if position.price is not None else None,
                "previous_close": str(position.previous_close)
                if position.previous_close is not None
                else None,
                "price_session": observed,
                "current_session": current,
                "asset_type": position.asset_type,
                "instrument_name": position.instrument_name,
            }
        )
        if not current:
            # Keep valuations; an unknown/old quote cannot become a current-session return.
            position.previous_close = None
    result.metrics = calculate_metrics(snapshot)
    result.metrics.update(
        {
            "position_details": dated,
            "market_data_as_of": price_metadata,
            "price_session": session,
            "report_length": request.report_length,
            "question": request.question,
            "daily_change_complete": all(row["current_session"] and row["previous_close"] for row in dated),
        }
    )
    stale = [row["symbol"] for row in dated if not row["current_session"]]
    if stale:
        result.metrics["price_note"] = (
            "Current-session prices are unavailable for "
            + ", ".join(stale)
            + "; their daily moves are not shown."
        )
    if (
        dependencies.history_enabled
        and dependencies.market_data
        and hasattr(dependencies.market_data, "history")
    ):
        try:
            from .metrics import historical_risk

            async with asyncio.timeout(15):
                result.metrics["historical_risk"] = historical_risk(
                    snapshot, await dependencies.market_data.history(snapshot)
                )
        except Exception:
            pass
    if dependencies.model:
        from .agent import generate_narrative

        narrative = await generate_narrative(
            dependencies.model,
            request.model_copy(update={"snapshot": snapshot}),
            result.metrics,
            result.sources,
            research=dependencies.research,
            market_data=dependencies.market_data,
            activity=result.research_activity,
            budget=result.research_budget,
            activity_sink=dependencies.activity_sink,
            deadline=deadline,
        )
        result.executive_summary = narrative.executive_summary
        result.sections = narrative.sections
        result.suggestions = narrative.suggestions if request.include_suggestions else []
        if not result.research_budget.get("analysis_written"):
            result.status = "partial"
    else:
        result.executive_summary = _portfolio_summary(result.metrics)
        move = result.metrics.get("daily_price_change_estimate")
        outlook = (
            f"The current-session change in priced positions is ${float(move):+,.2f}."
            if move is not None
            else "Current-session price changes are unavailable."
        )
        result.sections = [ReportSection(heading="Position outlook", content=outlook)]
        if dependencies.research and request.max_searches:
            from .research_runtime import ResearchRuntime

            runtime = ResearchRuntime(
                request,
                result.sources,
                research=dependencies.research,
                activity=result.research_activity,
                budget=result.research_budget,
                activity_sink=dependencies.activity_sink,
                deadline=deadline,
            )
            news = await runtime.prepare_news(
                await _search_events(
                    dependencies.research,
                    " ".join(position.symbol for position in snapshot.positions) + " recent portfolio news",
                    12,
                    request.reporting_cutoff,
                )
            )
            if news:
                result.sections.append(
                    ReportSection(
                        heading="Relevant news",
                        content="\n".join("- " + item["title"] + " — " + item["publisher"] for item in news),
                    )
                )
        considerations = []
        for symbol, weight in result.metrics["weights_pct"].items():
            if weight >= request.position_review_threshold_pct:
                considerations.append(
                    f"{symbol} accounts for {weight:.1f}% of portfolio value; consider its concentration and underlying exposures."
                )
        if considerations:
            result.sections.append(
                ReportSection(heading="Portfolio considerations", content="\n".join(considerations))
            )
