"""Bounded, reusable portfolio research orchestration."""
import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from .metrics import calculate_metrics
from .models import AnalysisRequest, AnalysisResult, Finding, InvestmentProfile, Position, PortfolioSnapshot
from .news import cluster_stories


_LEGAL_SUFFIX = re.compile(r"\b(?:incorporated|inc|corp(?:oration)?|company|co|ltd|limited|plc|llc|lp|holdings?|group|trust|n\.?a|s\.?a|ag|n\.?v|se)\b\.?", re.I)
# Common words that are also ticker symbols; a bare token is never sufficient evidence.
_AMBIGUOUS_SYMBOLS = {"LENS", "CASH", "USD", "ON", "IT", "ALL", "ARE", "CAN", "FOR", "NOW", "NEW", "OUT", "KEY", "SEE", "TWO"}


def _short_instrument_name(name: str | None) -> str:
    """Company name without legal suffixes, so headlines like "Apple Stock falls" still match."""
    if not name:
        return ""
    cleaned = _LEGAL_SUFFIX.sub(" ", name)
    cleaned = re.sub(r"[,\.]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _entity_matches(source, position: Position) -> bool:
    """Reject ticker collisions before excerpts reach the synthesis model."""
    title = source.title or ""
    haystack = f"{title} {source.excerpt or ''}"[:12000]
    name = position.instrument_name
    if name and len(name) >= 5 and name.casefold() in haystack.casefold():
        return True
    short = _short_instrument_name(name)
    if short and len(short) >= 4 and re.search(rf"(?<![\w]){re.escape(short)}(?![\w])", title, re.I):
        return True
    # Short/common-word tickers need the full resolved name; a bare token is unsafe.
    if not name or len(position.symbol) < 4 or position.symbol in _AMBIGUOUS_SYMBOLS:
        return False
    return bool(re.search(rf"(?<![\w]){re.escape(position.symbol)}(?![\w])", haystack, re.I))


def _macro_matches(source) -> bool:
    # A publisher name or a passing mention in the body is not a market event.
    title = source.title[:500]
    return bool(re.search(r"\b(?:FOMC|monetary policy|interest rate(?:s)?|rate (?:cut|hike|decision)|inflation|consumer price index|\bCPI\b|jobs report|employment report|unemployment rate|Treasury yields?|GDP|gross domestic product)\b", title, re.I))


def _recent_event(source, reference: datetime, days: int = 90) -> bool:
    published = source.published_at
    if published is None:
        return True
    return published >= reference - timedelta(days=days)


async def _resolve_identities(snapshot: PortfolioSnapshot, market_data, budget_seconds: float = 8) -> tuple[PortfolioSnapshot, dict, list[str]]:
    identity = {}
    warnings = []
    unresolved = []
    by_value = sorted(snapshot.positions, key=lambda item: item.quantity * (item.price or 0), reverse=True)
    lookup_order = list(dict.fromkeys(item.symbol for item in by_value[:30]))
    resolved_by_symbol = {}
    if market_data is not None and hasattr(market_data, "resolve_instrument"):
        limit = asyncio.Semaphore(4)
        async def lookup(symbol):
            async with limit:
                try:
                    return symbol, await asyncio.wait_for(market_data.resolve_instrument(symbol), timeout=min(4, budget_seconds))
                except Exception:
                    return symbol, None
        tasks = [asyncio.create_task(lookup(symbol)) for symbol in lookup_order]
        try:
            done, _ = await asyncio.wait(tasks, timeout=budget_seconds)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        for task in done:
            symbol, resolved = task.result()
            resolved_by_symbol[symbol] = resolved
    for position in snapshot.positions:
        resolved = resolved_by_symbol.get(position.symbol)
        if resolved and position.instrument_name and resolved.get("instrument_name"):
            supplied = position.instrument_name.casefold()
            provider_name = resolved["instrument_name"].casefold()
            if supplied not in provider_name and provider_name not in supplied:
                warnings.append(f"{position.symbol} name differs from market metadata; your saved identity was retained.")
                resolved = None
        if resolved and resolved.get("symbol", "").upper() == position.symbol and resolved.get("instrument_name") and resolved.get("asset_type"):
            if position.asset_type == "unknown":
                position.asset_type = resolved["asset_type"]
            elif position.asset_type != resolved["asset_type"]:
                warnings.append(f"{position.symbol} instrument type differs from market metadata; your saved classification was retained.")
            if not position.instrument_name:
                position.instrument_name = resolved["instrument_name"]
            if not position.exchange:
                position.exchange = resolved.get("exchange")
            if not position.sector:
                position.sector = resolved.get("sector")
            identity[position.symbol] = {"name": position.instrument_name, "asset_type": position.asset_type,
                                         "exchange": position.exchange, "status": "resolved", "source": resolved.get("source", "market_data")}
        else:
            identity[position.symbol] = {"name": position.instrument_name, "asset_type": position.asset_type,
                                         "exchange": position.exchange, "status": "user_supplied" if position.instrument_name or position.asset_type != "unknown" else "unresolved"}
            if not position.instrument_name:
                unresolved.append(position.symbol)
    if unresolved:
        sample = ", ".join(unresolved[:5])
        warnings.append(f"Identity for {sample}{' and others' if len(unresolved) > 5 else ''} could not be verified; entity-specific news will be withheld.")
    return snapshot, identity, warnings


async def _search_events(provider, query: str, max_results: int, cutoff, symbol: str | None = None, name: str | None = None):
    # Feed providers can address a symbol directly; the symbolic query stays the fallback.
    if symbol and hasattr(provider, "search_symbol_candidates"):
        return await provider.search_symbol_candidates(symbol, name, max_results=max_results, reporting_cutoff=cutoff)
    if not symbol and hasattr(provider, "search_events_candidates"):
        return await provider.search_events_candidates(query, max_results=max_results, reporting_cutoff=cutoff)
    if symbol and hasattr(provider, "search_symbol"):
        return await provider.search_symbol(symbol, name, max_results=max_results, reporting_cutoff=cutoff)
    if hasattr(provider, "search_events"):
        return await provider.search_events(query, max_results=max_results, reporting_cutoff=cutoff)
    return await provider.search(query, max_results=max_results)


def _coverage(result: AnalysisResult, request: AnalysisRequest, dependencies) -> dict:
    """A stable, user-facing explanation shared by dashboard, reports, and callers."""
    metrics = result.metrics
    missing = list(metrics.get("missing_prices", []))
    unsupported = list(metrics.get("unsupported", []))
    unresolved = [symbol for symbol, identity in metrics.get("instrument_identity", {}).items()
                  if identity.get("status") == "unresolved"]
    total = int(metrics.get("total_positions", 0))
    valued = int(metrics.get("priced_positions", 0))
    verified = [source for source in result.sources if source.content_type in {"retrieved_page", "official_feed", "news_feed"}]
    headline_only_evidence = bool(verified) and not any(source.content_type in {"retrieved_page", "official_feed"} for source in verified)
    position_news = metrics.get("position_news", [])
    cited = [finding for finding in result.findings if finding.evidence_ids]
    issues = []
    from .providers import source_diagnostics
    outcomes = source_diagnostics(dependencies.research) if dependencies.research is not None else {}
    for source_name, outcome in outcomes.items():
        if outcome.get("status") in {"skipped", "failed"}:
            issues.append({"code": f"source_{outcome['status']}", "message": f"{source_name}: {outcome.get('reason') or outcome['status']}", "action": "research"})
    if missing:
        issues.append({"code": "missing_prices", "message": f"{', '.join(missing[:8])} {'and others ' if len(missing) > 8 else ''}excluded from value and allocation; add prices.", "action": "portfolio"})
    if unsupported:
        issues.append({"code": "unsupported", "message": f"{', '.join(unsupported[:8])} cannot be analyzed as supported holdings.", "action": "portfolio"})
    if unresolved:
        issues.append({"code": "unresolved_identity", "message": f"Instrument identity is unverified for {', '.join(unresolved[:8])}; issuer news and company/fund classification are withheld.", "action": "portfolio"})
    not_checked = [item["symbol"] for item in position_news if item["status"] == "not_checked"]
    if not_checked and dependencies.research is not None:
        issues.append({"code": "news_not_checked", "message": f"News was not checked for {', '.join(not_checked[:8])}{' and others' if len(not_checked) > 8 else ''}; increase the search limit or time budget.", "action": "research"})
    failed_searches = [item["symbol"] for item in position_news if item["status"] == "search_failed"]
    if failed_searches and dependencies.research is not None:
        issues.append({"code": "news_search_failed", "message": f"News search failed for {', '.join(failed_searches[:8])}{' and others' if len(failed_searches) > 8 else ''}. Check the search connection; these holdings were not checked.", "action": "research"})
    if headline_only_evidence:
        issues.append({"code": "headline_only_sources", "message": "Some current-event evidence is a dated headline link rather than full article text; treat those items as reported headlines, not verified article detail.", "action": "research"})
    if total == 0:
        event_state = "not_applicable"
        narrative_state = "not_applicable"
    elif dependencies.research is None or not request.max_searches:
        event_state = "unavailable"
        issues.append({"code": "research_off", "message": "Current-event research was unavailable for this run; enable and test search.", "action": "research"})
    elif not verified:
        event_state = "no_verified_sources"
        issues.append({"code": "no_verified_sources", "message": "No verified current-event sources were found for this portfolio and run date.", "action": "research"})
    else:
        event_state = "sourced" if cited else "sources_only"
        if not cited and dependencies.model is not None:
            issues.append({"code": "no_supported_event", "message": "Sources were found, but none supported a portfolio-specific event conclusion.", "action": "research"})
    if total == 0:
        pass
    elif dependencies.model is None:
        narrative_state = "unavailable"
        issues.append({"code": "model_off", "message": "Written event interpretation is unavailable; enable and test a research model.", "action": "model"})
    else:
        narrative_state = "available" if cited else "no_supported_finding"
    summary = (f"{valued} of {total} holdings valued" if missing or unsupported else "Portfolio measures ready")
    if total == 0:
        summary = "Cash balance ready; no holdings to research"
    elif event_state == "unavailable":
        summary += "; current-event research unavailable"
    elif event_state == "no_verified_sources":
        summary += "; no verified current events found"
    elif narrative_state == "unavailable":
        summary += "; written event analysis unavailable"
    elif cited:
        summary += "; sourced insights available"
    if not_checked and dependencies.research is not None:
        summary += f"; {len(not_checked)} {'holding' if len(not_checked) == 1 else 'holdings'} not checked for news"
    return {"summary": summary, "valued_positions": valued, "total_positions": total,
            "missing_price_symbols": missing, "unsupported_symbols": unsupported,
            "unresolved_symbols": unresolved, "event_state": event_state,
            "narrative_state": narrative_state, "verified_source_count": len(verified),
            "headline_only_evidence": headline_only_evidence,
            "position_news": position_news, "source_outcomes": outcomes, "issues": issues}


@dataclass
class EngineDependencies:
    research: Any = None
    market_data: Any = None
    instrument_resolver: Any = None
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
        result = AnalysisResult(snapshot_as_of=request.snapshot.as_of, status="partial", metrics=calculate_metrics(request.snapshot), warnings=["Research time budget exhausted while waiting for capacity; supplied portfolio metrics are shown."])
        result.coverage = _coverage(result, request, dependencies)
        return result
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
            identity_budget = min(8.0, max(0.5, timeout_seconds * 0.15))
            snapshot, identities, identity_warnings = await _resolve_identities(snapshot, dependencies.instrument_resolver or dependencies.market_data, identity_budget)
            result.metrics = calculate_metrics(snapshot)
            result.metrics["instrument_identity"] = identities
            result.warnings.extend(identity_warnings)
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
            if result.metrics.get("daily_price_contributions"):
                result.warnings.append("Daily price contributions estimate moves in current holdings, not account returns; flows and intraday trades are not included.")
            if any(p.asset_type.lower() in {"etf", "fund", "mutual_fund"} for p in snapshot.positions):
                result.warnings.append("ETF underlying holdings are not included in sector or overlap analysis.")
            missing_symbols = list(dict.fromkeys([*result.metrics["missing_prices"], *result.metrics["unsupported"]]))
            exclusion = f" {', '.join(missing_symbols[:5])} {'and others are' if len(missing_symbols) > 5 else 'is' if len(missing_symbols) == 1 else 'are'} excluded." if missing_symbols else ""
            largest = result.metrics["largest_position_pct"]
            if not snapshot.positions:
                result.findings.append(Finding(title="Portfolio is entirely cash", detail=f"The saved portfolio contains ${snapshot.cash:,.2f} cash and no holdings. There are no company or fund exposures to assess until holdings are added."))
            elif not result.metrics["position_values"]:
                result.findings.append(Finding(title="Holdings cannot be valued yet", detail=f"No saved holding has a usable price. Add prices before using allocation or concentration figures.{exclusion}"))
            for symbol, weight in sorted(result.metrics["weights_pct"].items(), key=lambda item: item[1], reverse=True):
                if weight < request.position_review_threshold_pct:
                    continue
                types = {position.asset_type.lower() for position in snapshot.positions if position.symbol == symbol}
                if types <= {"stock", "equity"} and identities.get(symbol, {}).get("status") != "unresolved":
                    result.findings.append(Finding(title="Single-company concentration", kind="risk", detail=f"{symbol} represents {weight:.1f}% of valued assets, exceeding your {request.position_review_threshold_pct:g}% review threshold. Review whether this company exposure matches your objectives; the threshold is a preference, not a universal safety limit.{exclusion}", affected_symbols=[symbol]))
                elif types <= {"etf", "fund", "mutual_fund"}:
                    result.findings.append(Finding(title="Large fund allocation", detail=f"{symbol} represents {weight:.1f}% of valued assets. Review its mandate, underlying holdings, overlap, and liquidity. This fund weight alone does not establish single-company concentration; look-through data is unavailable.{exclusion}", affected_symbols=[symbol]))
                else:
                    result.findings.append(Finding(title="Large holding allocation", detail=f"{symbol} represents {weight:.1f}% of valued assets. Instrument identity is unverified, so its fund or company exposure cannot be classified yet.{exclusion}", affected_symbols=[symbol]))
            if snapshot.positions and result.metrics["position_values"] and not any(item.title in {"Single-company concentration", "Large fund allocation", "Large holding allocation"} for item in result.findings):
                count = len(result.metrics["position_values"])
                result.findings.append(Finding(title="How this portfolio is allocated", detail=f"The valued portion spans {count} {'holding' if count == 1 else 'holdings'}; the largest is {largest:.1f}% of valued assets and cash is {result.metrics['cash_weight_pct']:.1f}%. No holding crosses your {request.position_review_threshold_pct:g}% review threshold. This does not establish fund look-through or future performance.{exclusion}"))
            for sector, weight in result.metrics["sector_weights_pct"].items():
                if sector != "Unclassified" and weight >= request.sector_review_threshold_pct:
                    result.findings.append(Finding(title=f"{sector} sector concentration", kind="risk", detail=f"This sector represents {weight:.1f}% of valued assets, exceeding your {request.sector_review_threshold_pct:g}% review threshold. ETF underlying exposures are excluded.{exclusion}"))
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
            elif request.benchmark and re.search(r"\bbenchmark\b|\bSPY\b|\bcompare\b", request.question, re.I):
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
            positions = {position.symbol: position for position in snapshot.positions}
            broad_source_ids = set()
            news_by_symbol = {position.symbol: {"symbol": position.symbol, "status": "not_checked", "source_ids": [], "evidence_grade": None}
                              for position in snapshot.positions}
            result.metrics["report_length"] = request.report_length
            result.metrics["position_news"] = list(news_by_symbol.values())
            result.metrics["position_details"] = [
                {"symbol": position.symbol, "price": str(position.price) if position.price is not None else None,
                 "previous_close": str(position.previous_close) if position.previous_close is not None else None,
                 "asset_type": position.asset_type, "instrument_name": position.instrument_name,
                 "sector": position.sector} for position in snapshot.positions]
            result.metrics["setup_news_status"] = "unavailable" if dependencies.research is None or not request.max_searches else "not_checked"
            result.metrics["setup_news_reason"] = "research_disabled" if dependencies.research is None or not request.max_searches else None
            if dependencies.research is not None and request.max_searches:
                values = result.metrics["position_values"]
                focus_symbols = sorted(positions, key=lambda symbol: (symbol not in values, -float(values.get(symbol, 0)), symbol))
                result.metrics["position_news_display_order"] = focus_symbols
                seen = set()
                retrieval_failed = False
                research_failed = False
                from .providers import supports_market_news
                broad_capable = supports_market_news(dependencies.research)
                broad_search = request.max_searches >= 3 and len(focus_symbols) >= 2 and request.max_searches > len(focus_symbols) and broad_capable
                if len(focus_symbols) < 2:
                    result.metrics["setup_news_reason"] = "insufficient_holdings"
                elif not broad_capable:
                    result.metrics["setup_news_reason"] = "unsupported_provider"
                elif not broad_search:
                    result.metrics["setup_news_reason"] = "budget_exhausted"
                source_limit = 62
                async def search_position(symbol):
                    position = positions[symbol]
                    name = position.instrument_name
                    focus = "latest earnings filings material news fund holdings changes"
                    query = f'"{name}" {symbol} {focus}' if name else f'"{symbol}" ticker {focus}'
                    try:
                        candidates = await _search_events(dependencies.research, query, 12, request.reporting_cutoff, symbol=symbol, name=name)
                    except Exception:
                        return symbol, [], False, True
                    matched = []
                    failed_retrieval = False
                    # Compare against the current time so items published during this run are not dropped.
                    reference = request.reporting_cutoff or datetime.now(timezone.utc)
                    for source in candidates[:36]:
                        if source.published_at is not None and source.published_at > reference:
                            continue
                        if request.reporting_cutoff and source.published_at is None:
                            continue
                        if not _recent_event(source, reference) or not _entity_matches(source, position):
                            continue
                        # Headline feeds carry only a dated headline and link; never treat them as article text.
                        if hasattr(dependencies.research, "retrieve") and source.content_type != "news_feed":
                            try:
                                source = await dependencies.research.retrieve(source)
                            except Exception:
                                failed_retrieval = True
                        if _entity_matches(source, position):
                            matched.append(source)
                    return symbol, matched, failed_retrieval, False

                selected = focus_symbols[:request.max_searches - int(broad_search)]
                for start in range(0, len(selected), 4):
                    batch = await asyncio.gather(*(search_position(symbol) for symbol in selected[start:start + 4]))
                    for symbol, matched, failed_retrieval, failed_search in batch:
                        retrieval_failed |= failed_retrieval
                        research_failed |= failed_search
                        if failed_search:
                            news_by_symbol[symbol]["status"] = "search_failed"
                            continue
                        position = positions[symbol]
                        ranked_stories = cluster_stories(matched, symbol)
                        ordered_ids = [identifier for story in ranked_stories for identifier in story["member_source_ids"]]
                        matched_by_id = {source.id: source for source in matched}
                        for identifier in ordered_ids:
                            source = matched_by_id[identifier]
                            if source.url in seen:
                                existing = next((item for item in result.sources if item.url == source.url), None)
                                strength = {"official_feed": 2, "retrieved_page": 2, "news_feed": 1}
                                if existing and strength.get(source.content_type, 0) > strength.get(existing.content_type, 0):
                                    upgraded = source.model_copy(update={"id": existing.id})
                                    result.sources[result.sources.index(existing)] = upgraded
                                    existing = upgraded
                                if existing and _entity_matches(existing, position):
                                    news_by_symbol[symbol]["source_ids"].append(existing.id)
                                continue
                            if len(result.sources) >= source_limit:
                                break
                            result.sources.append(source)
                            seen.add(source.url)
                            news_by_symbol[symbol]["source_ids"].append(source.id)
                        ids = news_by_symbol[symbol]["source_ids"]
                        attached = [source for source in result.sources if source.id in ids]
                        stories = cluster_stories(attached, symbol)
                        news_by_symbol[symbol]["stories"] = stories[:2]
                        news_by_symbol[symbol]["additional_stories"] = stories[2:]
                        full_text = any(source.content_type in {"retrieved_page", "official_feed"} for source in attached)
                        headline = any(source.content_type == "news_feed" for source in attached)
                        news_by_symbol[symbol]["status"] = ("verified" if (full_text or headline)
                                                            else "discovery_only" if attached else "no_verified_update")
                        news_by_symbol[symbol]["evidence_grade"] = "full" if full_text else ("headline" if headline else None)
                if broad_search and len(result.sources) >= source_limit:
                    result.metrics["setup_news_reason"] = "source_limit"
                if broad_search and len(result.sources) < source_limit:
                    try:
                        broad = await _search_events(dependencies.research, "recent Federal Reserve economic policy broad stock market material developments", 3, request.reporting_cutoff)
                        reference = request.reporting_cutoff or datetime.now(timezone.utc)
                        for source in broad[:2]:
                            if source.url in seen or source.published_at and source.published_at > reference or request.reporting_cutoff and source.published_at is None:
                                continue
                            if not _macro_matches(source) or not _recent_event(source, reference):
                                continue
                            if hasattr(dependencies.research, "retrieve") and source.content_type != "news_feed":
                                try:
                                    source = await dependencies.research.retrieve(source)
                                except Exception:
                                    retrieval_failed = True
                            if not _macro_matches(source):
                                continue
                            result.sources.append(source)
                            seen.add(source.url)
                            broad_source_ids.add(source.id)
                        result.metrics["setup_news_status"] = "verified" if broad_source_ids else "no_verified_update"
                    except Exception:
                        research_failed = True
                        result.metrics["setup_news_status"] = "search_failed"
                        result.metrics["setup_news_reason"] = "request_failed"
                result.metrics["setup_news_source_ids"] = list(broad_source_ids)
                # Later lookups may upgrade a shared URL; refresh earlier holding metadata too.
                for symbol, entry in news_by_symbol.items():
                    attached = [source for source in result.sources if source.id in entry["source_ids"]]
                    if not attached:
                        continue
                    stories = cluster_stories(attached, symbol)
                    entry["stories"], entry["additional_stories"] = stories[:2], stories[2:]
                    full = any(source.content_type in {"official_feed", "retrieved_page"} for source in attached)
                    headline = any(source.content_type == "news_feed" for source in attached)
                    entry["evidence_grade"] = "full" if full else "headline" if headline else None
                    entry["status"] = "verified" if full or headline else "discovery_only"
                # A source can affect multiple holdings without being duplicated in raw evidence.
                all_stories = [story for entry in news_by_symbol.values()
                               for story in [*entry.get("stories", []), *entry.get("additional_stories", [])]]
                for story in all_stories:
                    member_ids = set(story["member_source_ids"])
                    story["affected_symbols"] = sorted({symbol for other in all_stories
                                                        if member_ids.intersection(other["member_source_ids"])
                                                        for symbol in other["affected_symbols"]})
                if retrieval_failed:
                    result.warnings.append("Some source pages could not be retrieved; their search excerpts are discovery references only.")
                if research_failed or not result.sources:
                    result.warnings.append("Recent research coverage is limited; no unsupported market explanation was inferred.")
            else:
                result.warnings.append("External research is disabled; this report contains portfolio metrics only.")
            if request.reporting_cutoff:
                result.warnings.append("Reporting cutoff filters known publication dates; undated sources and current holdings cannot establish point-in-time historical evidence.")
            if dependencies.model is not None:
                try:
                    from .agent import generate_narrative
                    safe_request = request.model_copy(update={"snapshot": snapshot, "include_suggestions": bool(request.include_suggestions and profile_complete), "investment_profile": profile.model_dump() if profile else None})
                    narrative = await generate_narrative(dependencies.model, safe_request, result.metrics, result.sources)
                    valid_ids = {s.id for s in result.sources if s.content_type in {"retrieved_page", "official_feed", "news_feed"}}
                    evidence_by_id = {source.id: source for source in result.sources}
                    for finding in narrative.findings[:4 if request.report_length == "brief" else 12]:
                        cited = [evidence_by_id[identifier] for identifier in finding.evidence_ids if identifier in valid_ids]
                        valid_entity = all(symbol in positions and any(_entity_matches(source, positions[symbol]) for source in cited)
                                           for symbol in finding.affected_symbols)
                        valid_scope = bool(finding.affected_symbols) or bool(cited) and all(source.id in broad_source_ids for source in cited) and bool(finding.portfolio_relevance)
                        valid_date = finding.event_date is None or finding.event_date <= (request.reporting_cutoff or result.created_at).date()
                        if cited and len(cited) == len(finding.evidence_ids) and valid_entity and valid_scope and valid_date:
                            result.findings.append(finding)
                        else:
                            result.warnings.append("A generated finding with invalid evidence, entity, or date references was withheld.")
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
        result.coverage = _coverage(result, request, dependencies)
        return result
    # Informational calculation notes do not by themselves indicate a degraded run.
    degraded = (dependencies.model is None or dependencies.research is None or not any(source.content_type in {"retrieved_page", "official_feed", "news_feed"} for source in result.sources)
                or not result.metrics["coverage_complete"] or any(
                    text in warning for warning in result.warnings for text in ("unavailable", "exceeds", "future", "exhausted", "withheld", "invalid")))
    result.status = "partial" if degraded else "success"
    result.coverage = _coverage(result, request, dependencies)
    return result
