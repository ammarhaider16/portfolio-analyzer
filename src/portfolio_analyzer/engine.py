"""Bounded, reusable portfolio research orchestration."""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from .metrics import calculate_metrics
from .models import AnalysisRequest, AnalysisResult, Finding, InvestmentProfile, Position, PortfolioSnapshot


@dataclass
class EngineDependencies:
    research: Any = None
    market_data: Any = None
    model: Any = None
    hard_search_limit: int = 30
    hard_seconds_limit: int = 600
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2), repr=False)


async def analyze_portfolio(request: AnalysisRequest, dependencies: EngineDependencies | None = None) -> AnalysisResult:
    """Analyze one isolated request. Cancellation propagates to the caller."""
    request = AnalysisRequest.model_validate(request)
    dependencies = dependencies or EngineDependencies()
    request = request.model_copy(update={"max_searches": min(request.max_searches, max(0, dependencies.hard_search_limit)), "max_seconds": min(request.max_seconds, max(1, dependencies.hard_seconds_limit))})
    started = asyncio.get_running_loop().time()
    try:
        async with asyncio.timeout(request.max_seconds):
            await dependencies.semaphore.acquire()
    except TimeoutError:
        return AnalysisResult(snapshot_as_of=request.snapshot.as_of, status="partial", metrics=calculate_metrics(request.snapshot), warnings=["Research time budget exhausted while waiting for capacity; supplied portfolio metrics are shown."])
    try:
        remaining = max(0.001, request.max_seconds - (asyncio.get_running_loop().time() - started))
        return await _analyze_portfolio(request, dependencies, remaining)
    finally:
        dependencies.semaphore.release()


async def _analyze_portfolio(request: AnalysisRequest, dependencies: EngineDependencies, timeout_seconds: float) -> AnalysisResult:
    result = AnalysisResult(snapshot_as_of=request.snapshot.as_of)
    snapshot = request.snapshot.model_copy(deep=True)
    result.metrics = calculate_metrics(snapshot)
    if snapshot.source == "demonstration":
        result.warnings.append("DEMONSTRATION PORTFOLIO: holdings and prices are illustrative and do not represent a live account or current market data.")
    supplied_prices = [position.symbol for position in snapshot.positions if position.price is not None]
    price_metadata = {}
    try:
        async with asyncio.timeout(timeout_seconds):
            if dependencies.market_data is not None:
                try:
                    if hasattr(dependencies.market_data, "enrich_with_metadata"):
                        snapshot, price_metadata = await dependencies.market_data.enrich_with_metadata(snapshot)
                    else:
                        snapshot = await dependencies.market_data.enrich(snapshot)
                    result.metrics = calculate_metrics(snapshot)
                except Exception:
                    result.warnings.append("Supplemental market data is unavailable; supplied valuations were used.")
            supplied_prices = [symbol for symbol in supplied_prices if symbol not in price_metadata]
            result.metrics["market_data_as_of"] = price_metadata
            result.metrics["supplied_price_timestamp_unknown"] = supplied_prices
            if supplied_prices:
                result.warnings.append("Supplied price timestamps are unknown; snapshot collection time does not verify quote freshness.")
            if price_metadata:
                result.warnings.append("Supplemental valuations use daily closing prices, not live quotes; each trading date and retrieval time appear in market_data_as_of.")
                from datetime import date
                for symbol, metadata in price_metadata.items():
                    if (datetime.now(timezone.utc).date() - date.fromisoformat(metadata["trading_date"])).days * 24 > request.max_data_age_hours:
                        result.warnings.append(f"Supplemental closing price for {symbol} exceeds the permitted data age (trading date {metadata['trading_date']}).")
            age = (datetime.now(timezone.utc) - snapshot.as_of).total_seconds() / 3600
            if age > request.max_data_age_hours:
                result.warnings.append("Portfolio snapshot exceeds the permitted data age; refresh holdings before relying on this report.")
            if age < -0.1:
                result.warnings.append("Portfolio snapshot is dated in the future; verify its timestamp.")
            if not result.metrics["coverage_complete"]:
                result.warnings.append("Coverage is partial. Weights use known valued assets only; unpriced and unsupported holdings are excluded.")
            result.warnings.append("Daily price contributions estimate moves in current holdings, not account returns; flows and intraday trades are not included.")
            if any(p.asset_type.lower() in {"etf", "fund", "mutual_fund"} for p in snapshot.positions):
                result.warnings.append("ETF underlying holdings are not included in sector or overlap analysis.")
            largest = result.metrics["largest_position_pct"]
            result.findings.append(Finding(title="Portfolio overview", detail=f"Known portfolio value: {result.metrics['known_value']}. Cash: {result.metrics['cash_weight_pct']:.1f}%. Largest position: {largest:.1f}% of known value."))
            for symbol, weight in sorted(result.metrics["weights_pct"].items(), key=lambda item: item[1], reverse=True):
                if weight < request.position_review_threshold_pct:
                    continue
                types = {position.asset_type.lower() for position in snapshot.positions if position.symbol == symbol}
                if types <= {"stock", "equity"}:
                    result.findings.append(Finding(title="Single-company concentration", kind="risk", detail=f"{symbol} represents {weight:.1f}% of known value, exceeding your {request.position_review_threshold_pct:g}% review threshold. Review whether this company exposure matches your objectives; the threshold is a preference, not a universal safety limit."))
                elif types <= {"etf", "fund", "mutual_fund"}:
                    result.findings.append(Finding(title="Large fund allocation", detail=f"{symbol} represents {weight:.1f}% of known value. Review its mandate, underlying holdings, overlap, and liquidity. This fund weight alone does not establish single-company concentration; look-through data is unavailable."))
                else:
                    result.findings.append(Finding(title="Large holding allocation", detail=f"{symbol} represents {weight:.1f}% of known value. Verify its instrument type and underlying exposures before interpreting concentration."))
            for sector, weight in result.metrics["sector_weights_pct"].items():
                if sector != "Unclassified" and weight >= request.sector_review_threshold_pct:
                    result.findings.append(Finding(title=f"{sector} sector concentration", kind="risk", detail=f"This sector represents {weight:.1f}% of known value, exceeding your {request.sector_review_threshold_pct:g}% review threshold. ETF underlying exposures are excluded."))
            if dependencies.market_data is not None and hasattr(dependencies.market_data, "history"):
                try:
                    from .metrics import historical_risk, benchmark_comparison
                    history = await dependencies.market_data.history(snapshot)
                    result.metrics["historical_risk"] = historical_risk(snapshot, history)
                    result.warnings.append("Historical risk estimates use today's holdings and available adjusted history, not your realized investment experience.")
                    if request.benchmark:
                        benchmark_snapshot = PortfolioSnapshot(as_of=snapshot.as_of, positions=[Position(symbol=request.benchmark, quantity=1)])
                        benchmark_history = await dependencies.market_data.history(benchmark_snapshot)
                        result.metrics["benchmark_comparison"] = benchmark_comparison(snapshot, history, request.benchmark, benchmark_history.get(request.benchmark, {}))
                except Exception:
                    result.warnings.append("Historical risk or benchmark comparison unavailable; missing historical figures were not inferred.")
            elif request.benchmark:
                result.warnings.append(f"Benchmark {request.benchmark} comparison unavailable; historical market data is not enabled.")
            if request.previous_snapshot:
                result.warnings.append("Previous snapshot supplied; cash-flow-adjusted returns are withheld because complete transaction history is unavailable.")
            profile = None
            try:
                if request.investment_profile:
                    profile = InvestmentProfile.model_validate(request.investment_profile)
            except ValueError:
                pass
            profile_complete = profile is not None
            if request.include_suggestions and not profile_complete:
                result.warnings.append("Adjustment suggestions require a complete investment profile (objectives, horizon, risk_tolerance, liquidity_needs, allocation_targets, restrictions) and were withheld.")
            if dependencies.research is not None and request.max_searches:
                ordered = sorted(result.metrics["position_values"], key=lambda s: float(result.metrics["position_values"][s]), reverse=True)
                seen = set()
                for symbol in ordered[:request.max_searches]:
                    try:
                        focus = request.question[:500] or "investor relations latest earnings material risks"
                        sources = await dependencies.research.search(f"{symbol} {focus}", max_results=3)
                        for source in sources[:3]:
                            if len(result.sources) >= 12:
                                break
                            if source.published_at is not None and source.published_at > (request.reporting_cutoff or result.created_at):
                                continue
                            if source.url not in seen:
                                if hasattr(dependencies.research, "retrieve"):
                                    try:
                                        source = await dependencies.research.retrieve(source)
                                    except Exception:
                                        result.warnings.append("Some source pages could not be retrieved; their search excerpts are discovery references only.")
                                result.sources.append(source)
                                seen.add(source.url)
                        if len(result.sources) >= 12:
                            break
                    except Exception:
                        result.warnings.append(f"Research unavailable for {symbol}; no market explanation inferred.")
            else:
                result.warnings.append("External research is disabled; this report contains portfolio metrics only.")
            if request.reporting_cutoff:
                result.warnings.append("Reporting cutoff filters known publication dates; undated sources and current holdings cannot establish point-in-time historical evidence.")
            if dependencies.model is not None:
                try:
                    from .agent import generate_narrative
                    safe_request = request.model_copy(update={"snapshot": snapshot, "include_suggestions": bool(request.include_suggestions and profile_complete), "investment_profile": profile.model_dump() if profile else None})
                    narrative = await generate_narrative(dependencies.model, safe_request, result.metrics, result.sources)
                    valid_ids = {s.id for s in result.sources if s.content_type in {"retrieved_page", "official_feed"}}
                    for finding in narrative.findings[:4 if request.report_length == "brief" else 12]:
                        if finding.evidence_ids and set(finding.evidence_ids) <= valid_ids:
                            result.findings.append(finding)
                        else:
                            result.warnings.append("A generated finding with invalid evidence references was withheld.")
                    if safe_request.include_suggestions:
                        result.suggestions = narrative.suggestions
                except Exception:
                    result.warnings.append("Model analysis unavailable or invalid; deterministic metrics remain available.")
            else:
                result.warnings.append("No model configured; narrative synthesis is unavailable.")
    except TimeoutError:
        result.warnings.append("Research time budget exhausted; completed results are shown.")
    except Exception:
        result.status = "failed"
        result.error = "Analysis could not complete. Check provider connections and input coverage."
        return result
    # Informational calculation notes do not by themselves indicate a degraded run.
    degraded = (dependencies.model is None or dependencies.research is None or not any(source.content_type in {"retrieved_page", "official_feed"} for source in result.sources)
                or not result.metrics["coverage_complete"] or any(
                    text in warning for warning in result.warnings for text in ("unavailable", "exceeds", "future", "exhausted", "withheld", "invalid")))
    result.status = "partial" if degraded else "success"
    return result
