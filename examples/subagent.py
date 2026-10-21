"""Offline library and parent-agent tool examples. No credentials or live calls.

Run: uv run python examples/subagent.py
Run: uv run python examples/subagent.py --langchain
"""
import argparse
import asyncio
import json

from betafolio.agent import create_research_tool
from betafolio.demo import demo_snapshot
from betafolio.engine import EngineDependencies, analyze_portfolio
from betafolio.models import AnalysisRequest


async def main(use_tool: bool = False) -> None:
    request = AnalysisRequest(
        snapshot=demo_snapshot(),
        question="Explain concentration and identify gaps in the available research.",
        max_searches=0,
        max_seconds=30,
    )
    # Reuse dependencies across calls to share concurrency limits. No external
    # providers are installed here, so this example performs no network access.
    dependencies = EngineDependencies(hard_search_limit=4, hard_seconds_limit=30)
    if use_tool:
        research_tool = create_research_tool(dependencies)
        # A LangChain parent may include research_tool in its tools list.
        # Calling the tool directly demonstrates the same serialized contract.
        result = await research_tool.ainvoke({"request": request.model_dump(mode="json")})
    else:
        result = (await analyze_portfolio(request, dependencies)).model_dump(mode="json")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--langchain", action="store_true", help="Invoke the LangChain tool adapter")
    args = parser.parse_args()
    asyncio.run(main(args.langchain))
