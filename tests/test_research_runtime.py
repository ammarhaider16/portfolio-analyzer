import asyncio
from datetime import datetime, timedelta, timezone

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from portfolio_analyzer.agent import generate_narrative
from portfolio_analyzer.metrics import calculate_metrics
from portfolio_analyzer.models import AnalysisRequest, Evidence, PortfolioSnapshot, Position
from portfolio_analyzer.research_runtime import ResearchRuntime


class ScriptedAnalyst(BaseChatModel):
    script: list[dict]
    seen: list = []
    cursor: int = 0

    @property
    def _llm_type(self):
        return "scripted-research-analyst"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        call = self.script[self.cursor]
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{
            "name": call["name"], "args": call["args"], "id": f"step-{self.cursor}", "type": "tool_call"}]))])


def request(**kwargs):
    return AnalysisRequest(snapshot=PortfolioSnapshot(as_of=datetime.now(timezone.utc), positions=[
        Position(symbol="AAPL", quantity=1, price=100, instrument_name="Apple Inc", asset_type="stock")]),
        **kwargs)


def test_model_chooses_tool_follows_lead_switches_failed_source_and_cites(monkeypatch):
    class Search:
        async def search(self, query, max_results=12):
            return [Evidence(id="lead", url="https://publisher.example/apple", title="Apple Inc earnings",
                             publisher="publisher.example", excerpt="Apple Inc disclosed results")]
    async def retrieve(url):
        assert url == "https://publisher.example/apple"
        return "Apple Inc disclosed its quarterly results and updated guidance."
    monkeypatch.setattr("portfolio_analyzer.providers.retrieve_public_text", retrieve)
    monkeypatch.setattr("portfolio_analyzer.providers.validate_public_url", lambda url: url)
    model = ScriptedAnalyst(script=[
        {"name": "search_research", "args": {"query": "Apple results", "source": "unavailable-source"}},
        {"name": "search_research", "args": {"query": "Apple quarterly earnings", "source": "all"}},
        {"name": "retrieve_public_page", "args": {"url": "https://publisher.example/apple", "evidence_id": "lead"}},
        {"name": "ResearchNarrative", "args": {"findings": [{"title": "Apple results", "detail": "Apple reported results",
             "evidence_ids": ["lead"], "affected_symbols": ["AAPL"]}], "suggestions": []}},
    ])
    req = request()
    evidence, activity, budget = [], [], {}
    result = asyncio.run(generate_narrative(model, req, calculate_metrics(req.snapshot), evidence,
                                            research=Search(), activity=activity, budget=budget))
    assert result.findings[0].evidence_ids == ["lead"]
    assert evidence[0].content_type == "retrieved_page"
    assert budget["queries"] == 2 and budget["tool_calls"] == 3 and budget["model_calls"] == 4
    assert any(event["status"] == "failed" for event in activity)
    assert any(event["tool"] == "retrieve_public_page" and event["status"] == "passed" for event in activity)
    assert '"status": "failed"' in str(model.seen[1][-1].content)


def test_zero_findings_and_suggestions_valid_and_complete_roster_preserved():
    req = request(max_model_calls=1)
    req.snapshot.positions.append(Position(symbol="NOPE", quantity=1, instrument_name="Unpriced Holding"))
    model = ScriptedAnalyst(script=[{"name": "ResearchNarrative", "args": {"findings": [], "suggestions": []}}])
    result = asyncio.run(generate_narrative(model, req, calculate_metrics(req.snapshot), []))
    assert result.findings == [] and result.suggestions == []
    assert "Unpriced Holding" in str(model.seen[0])


def test_tool_budget_and_deadline_prevent_network_and_preserve_evidence():
    class Search:
        async def search(self, *args, **kwargs):
            raise AssertionError("Network must not run")
    async def run():
        existing = Evidence(id="old", url="https://publisher.example/old", title="Apple", publisher="publisher.example")
        runtime = ResearchRuntime(request(max_tool_calls=0), [existing], research=Search())
        tool = next(tool for tool in runtime.tools() if tool.name == "search_research")
        outcome = await tool.ainvoke({"query": "Apple"})
        assert outcome["status"] == "budget_exhausted"
        assert runtime.sources == [existing]
        runtime = ResearchRuntime(request(), [], research=Search(), deadline=asyncio.get_running_loop().time() + 1)
        outcome = await next(tool for tool in runtime.tools() if tool.name == "search_research").ainvoke({"query": "Apple"})
        assert outcome["status"] == "budget_exhausted"
    asyncio.run(run())


def test_activity_callback_is_observable_not_reasoning():
    events = []
    async def run():
        runtime = ResearchRuntime(request(), [], activity_sink=lambda event, budget: events.append((event, budget)))
        tool = next(tool for tool in runtime.tools() if tool.name == "lookup_market_data")
        outcome = await tool.ainvoke({"symbol": "AAPL"})
        assert outcome["status"] == "failed"
    asyncio.run(run())
    assert [event["status"] for event, _ in events] == ["started", "failed"]
    assert all("reasoning" not in event for event, _ in events)


def test_query_limit_stops_followup_and_model_limit_preserves_completed_evidence():
    class Search:
        count = 0
        async def search(self, *args, **kwargs):
            self.count += 1
            return [Evidence(id="lead", url="https://publisher.example/apple", title="Apple Inc",
                             publisher="publisher.example")]
    async def run():
        provider = Search()
        runtime = ResearchRuntime(request(max_searches=1), [], research=provider)
        tool = next(tool for tool in runtime.tools() if tool.name == "search_research")
        assert (await tool.ainvoke({"query": "first lead"}))["status"] == "passed"
        assert (await tool.ainvoke({"query": "follow-up"}))["status"] == "budget_exhausted"
        assert provider.count == 1
        # A model that fails to finalize cannot evade the middleware call ceiling.
        model = ScriptedAnalyst(script=[{"name": "search_research", "args": {"query": "Apple"}}])
        sources, budget = [], {}
        req = request(max_model_calls=1)
        narrative = await generate_narrative(model, req, calculate_metrics(req.snapshot), sources,
                                             research=provider, budget=budget)
        assert narrative.findings == []
        assert sources and budget["model_calls"] == 1 and budget["exhausted"]
    asyncio.run(run())


def test_cancellation_preserves_registered_evidence_and_reports_observable_status():
    async def run():
        started = asyncio.Event()
        class Search:
            async def search(self, *args, **kwargs):
                started.set()
                await asyncio.sleep(60)
        source = Evidence(id="saved", url="https://publisher.example", title="Apple Inc", publisher="publisher.example")
        runtime = ResearchRuntime(request(), [source], research=Search())
        tool = next(tool for tool in runtime.tools() if tool.name == "search_research")
        task = asyncio.create_task(tool.ainvoke({"query": "Apple"}))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Cancellation must propagate")
        assert runtime.sources == [source]
        assert runtime.activity[-1]["status"] == "cancelled"
    asyncio.run(run())


def test_page_retrieval_by_url_preserves_discovered_publication_metadata(monkeypatch):
    async def fetch(url):
        return 'Apple Inc reported quarterly results.'
    monkeypatch.setattr('portfolio_analyzer.providers.retrieve_public_text', fetch)

    async def run():
        original = Evidence(id='dated', url='https://publisher.example/apple', title='Apple Inc earnings',
                            publisher='Publisher', published_at=datetime.now(timezone.utc), content_type='news_feed')
        runtime = ResearchRuntime(request(), [original], research=object())
        tool = next(tool for tool in runtime.tools() if tool.name == 'retrieve_public_page')
        result = await tool.ainvoke({'url': original.url})
        assert result['status'] == 'passed'
        assert runtime.sources[0].content_type == 'retrieved_page'
        assert runtime.sources[0].id == original.id
        assert runtime.sources[0].published_at == original.published_at
        assert runtime.sources[0].title == original.title
    asyncio.run(run())


def test_background_and_discovery_evidence_do_not_become_verified_current_news(monkeypatch):
    from portfolio_analyzer.engine import analyze_portfolio, EngineDependencies
    from portfolio_analyzer.models import ResearchNarrative
    now = datetime.now(timezone.utc)
    async def narrative(model, request, metrics, sources, **kwargs):
        sources.extend([
            Evidence(id="background", url="https://publisher.example/2018", title="Apple Inc fund history",
                     publisher="publisher.example", published_at=now - timedelta(days=1000), content_type="retrieved_page"),
            Evidence(id="macro", url="https://publisher.example/discovery", title="Federal Reserve interest rate decision",
                     publisher="publisher.example", published_at=now, content_type="search_excerpt"),
        ])
        kwargs["activity"].append({"tool": "search_asset_news", "symbol": "AAPL", "status": "failed"})
        return ResearchNarrative()
    class Search:
        async def search(self, *args, **kwargs):
            return []
    monkeypatch.setattr("portfolio_analyzer.agent.generate_narrative", narrative)
    result = asyncio.run(analyze_portfolio(request(max_searches=1, benchmark=""),
                                           EngineDependencies(model=object(), research=Search())))
    assert len(result.sources) == 2  # Background remains available as background.
    assert result.metrics["position_news"][0]["status"] == "search_failed"
    assert result.metrics["position_news"][0]["source_ids"] == []
    assert result.metrics["setup_news_status"] == "discovery_only"
    assert result.metrics["setup_news_source_ids"] == []


def test_headline_only_findings_and_summary_cannot_claim_article_body(monkeypatch):
    from portfolio_analyzer.engine import analyze_portfolio, EngineDependencies
    from portfolio_analyzer.models import ResearchNarrative, Finding, BriefingSummary
    async def narrative(model, request, metrics, sources, **kwargs):
        sources.append(Evidence(id="headline", url="https://publisher.example/apple", title="Apple Inc reports earnings",
                                publisher="Reuters", published_at=datetime.now(timezone.utc), content_type="news_feed"))
        return ResearchNarrative(findings=[Finding(title="Profit jumped 999%", detail="Invented body numbers caused a stock rally",
                                                   affected_symbols=["AAPL"], evidence_ids=["headline"],
                                                   portfolio_relevance="Earnings may affect this holding")],
                                 executive_summary=BriefingSummary(text="Invented body figures rose 999%", evidence_ids=["headline"]))
    class Search:
        async def search(self, *args, **kwargs):
            return []
    monkeypatch.setattr("portfolio_analyzer.agent.generate_narrative", narrative)
    result = asyncio.run(analyze_portfolio(request(max_searches=1, benchmark=""),
                                           EngineDependencies(model=object(), research=Search())))
    sourced = next(finding for finding in result.findings if finding.evidence_ids)
    assert sourced.detail == "Reuters reported: Apple Inc reports earnings."
    assert sourced.portfolio_relevance.startswith("Interpretation:")
    assert "999" not in sourced.title + sourced.detail + result.executive_summary.text
    assert "Article details remain unverified" in result.executive_summary.text


def test_schema_repair_explains_field_type_without_echoing_invalid_input():
    model = ScriptedAnalyst(script=[
        {"name": "ResearchNarrative", "args": {"findings": "UNTRUSTED-PRIVATE-INPUT"}},
        {"name": "ResearchNarrative", "args": {"findings": [], "suggestions": []}},
    ])
    req, activity = request(max_model_calls=2), []
    result = asyncio.run(generate_narrative(model, req, calculate_metrics(req.snapshot), [], activity=activity))
    assert result.findings == []
    feedback = next(event["reason"] for event in activity if event["status"] == "schema_repair")
    assert "findings: list_type" in feedback
    assert "UNTRUSTED-PRIVATE-INPUT" not in feedback
