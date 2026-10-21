"""Real SDK wire contracts and current-session pricing: the consequential boundaries."""

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
import httpx
import pandas as pd
from langchain_openai import ChatOpenAI
from betafolio.engine import EngineDependencies, analyze_portfolio
from betafolio.models import AnalysisRequest, PortfolioSnapshot, Position, Evidence
from betafolio.providers import YFinanceMarketDataProvider, expected_market_session


def test_current_quote_wins_over_previous_day_history(monkeypatch):
    import yfinance as yf

    info = {
        "regularMarketPrice": 59.21,
        "regularMarketPreviousClose": 58.82,
        "regularMarketTime": 1792440000,
        "exchangeTimezoneName": "America/New_York",
    }

    class Ticker:
        def get_info(self):
            return info

        def history(self, **kwargs):
            return pd.DataFrame({"Close": [58.82]}, index=pd.to_datetime(["2026-10-16"]))

    monkeypatch.setattr(yf, "Ticker", lambda symbol: Ticker())
    monkeypatch.setattr("betafolio.providers.expected_market_session", lambda *args: "2026-10-19")
    quotes = asyncio.run(YFinanceMarketDataProvider().quotes(["SPUS"]))
    assert quotes["SPUS"]["trading_date"] == "2026-10-19"
    assert quotes["SPUS"]["price"] == Decimal("59.21") and quotes["SPUS"]["previous_close"] == Decimal(
        "58.82"
    )
    assert quotes["SPUS"]["quote_as_of"].startswith("2026-10-19T20:00:00")


def test_calendar_selects_current_session_and_honors_weekends_and_before_open():
    assert expected_market_session(datetime(2026, 10, 19, 20, tzinfo=timezone.utc)) == "2026-10-19"
    assert expected_market_session(datetime(2026, 10, 19, 12, tzinfo=timezone.utc)) == "2026-10-16"
    assert expected_market_session(datetime(2026, 10, 18, 20, tzinfo=timezone.utc)) == "2026-10-16"


def test_actual_sdk_can_use_tools_and_finalize_in_its_own_structure(monkeypatch):
    calls = []

    class Search:
        async def search(self, *args, **kwargs):
            return [
                Evidence(
                    id="one",
                    title="Apple announces investment",
                    url="https://news.example/apple",
                    publisher="Newsroom",
                    excerpt="Apple is expanding production.",
                )
            ]

    async def retrieve(url):
        return "Apple announced an investment in production."

    monkeypatch.setattr("betafolio.providers.retrieve_public_text", retrieve)

    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            name, args = "search_news", {"query": "Apple investment", "symbol": "AAPL"}
        else:
            assert any(
                message.get("role") == "tool" and message.get("tool_call_id") == "call-1"
                for message in payload["messages"]
            )
            name, args = (
                "ResearchNarrative",
                {
                    "executive_summary": "Apple is investing in production.",
                    "sections": [
                        {
                            "heading": "Investment and portfolio exposure",
                            "content": "The announcement may shape the outlook.",
                        }
                    ],
                    "suggestions": [],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "reply",
                "object": "chat.completion",
                "created": 1,
                "model": "tool-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"call-{len(calls)}",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args)},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            model = ChatOpenAI(
                model="tool-model", api_key="test-key", http_async_client=client, max_retries=0
            )
            request = AnalysisRequest(
                snapshot=PortfolioSnapshot(
                    as_of=datetime.now(timezone.utc),
                    positions=[Position(symbol="AAPL", quantity=1, price=100)],
                )
            )
            return await analyze_portfolio(request, EngineDependencies(model=model, research=Search()))

    result = asyncio.run(exercise())
    assert len(calls) == 2 and result.executive_summary == "Apple is investing in production."
    assert result.sections[0].heading == "Investment and portfolio exposure"


def test_price_refresh_never_overwrites_a_newer_saved_observation():
    from betafolio.providers import quote_is_newer

    position = Position(
        symbol="SPUS",
        quantity=1,
        price="59.21",
        price_session="2026-10-19",
        price_as_of="2026-10-19T20:00:00Z",
    )
    assert not quote_is_newer({"trading_date": "2026-10-16", "quote_as_of": "2026-10-16T20:00:00Z"}, position)
    assert not quote_is_newer({"trading_date": "2026-10-19", "quote_as_of": None}, position)
    assert not quote_is_newer({"trading_date": "2026-10-19", "quote_as_of": "2026-10-19T19:00:00Z"}, position)
    assert quote_is_newer({"trading_date": "2026-10-19", "quote_as_of": "2026-10-19T20:01:00Z"}, position)
