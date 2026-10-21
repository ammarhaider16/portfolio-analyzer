"""The tools supply prepared context and preserve an observable run trace."""

import asyncio
from datetime import datetime, timezone
from betafolio.models import AnalysisRequest, PortfolioSnapshot, Evidence
from betafolio.research_runtime import ResearchRuntime


def test_news_tool_prepares_article_content_without_a_grade_checklist(monkeypatch):
    class Search:
        async def search(self, *args, **kwargs):
            return [
                Evidence(
                    id="one",
                    title="Company expands investment",
                    url="https://news.example/article",
                    publisher="Newsroom",
                    excerpt="Publisher summary.",
                )
            ]

    async def retrieve(url):
        return "The company announced a new investment in production."

    monkeypatch.setattr("betafolio.providers.retrieve_public_text", retrieve)

    async def exercise():
        runtime = ResearchRuntime(
            AnalysisRequest(snapshot=PortfolioSnapshot(as_of=datetime.now(timezone.utc))),
            [],
            research=Search(),
        )
        result = await runtime.tools()[0].ainvoke({"query": "company investment"})
        assert result[0]["content"] == "The company announced a new investment in production."
        assert "supports_factual_findings" not in result[0] and "content_type" not in result[0]
        assert [event["status"] for event in runtime.activity] == ["started", "passed"]
        assert {tool.name for tool in runtime.tools()} == {"search_news", "read_article", "get_market_data"}

    asyncio.run(exercise())
