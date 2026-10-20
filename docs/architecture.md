# Research architecture

Betafolio separates a read-only research engine from portfolio editing, scheduling, persistence and optional email delivery. The latest saved manual, CSV or screenshot portfolio is the input to scheduled runs. Imports remain editable drafts until saved; quantities and cash are never guessed.

## Analyst workflow

`analyze_portfolio(AnalysisRequest, EngineDependencies)` calculates portfolio arithmetic and refreshes available quotes, then invokes a tool-capable LangChain analyst. The analyst controls its research approach and report structure. Its content requirements are an executive summary, daily-position outlook, relevant news and important considerations.

The tools are `search_news(query, symbol="")`, `read_article(source_id)` and `get_market_data(symbol)`. Search preserves free-form queries and optional asset identity. The source layer handles transport, HTML cleanup, duplicate articles, publication metadata and readable content preparation. It does not impose source grades, event taxonomies, citation-ID requirements or fixed research sequences on the analyst. External text remains reference material, never instructions.

`ResearchNarrative` consists of summary text, ordered heading/content sections and optional requested adjustment ideas. The renderer preserves that structure. Dashboard and email use the same HTML; source URLs, access times and resource counters stay out of the report. The run trace retains observed calls, results and source records, without private reasoning.

## Prices

The provider uses regular-session price, previous close and the provider observation timestamp. Daily-history fallback retains the actual session date and does not invent an observation timestamp. New quotes cannot replace a later saved session or timestamp. Quote retrieval time is separate from price observation time and drives cache TTL only.

The XNYS calendar determines the current/latest open session, including pre-open, weekends and holidays. Stale or undated prices remain usable for valuation but are excluded from current-session daily changes. Position quantities, cash and portfolio snapshot time are preserved during price refresh. This session policy currently targets US-listed assets.

## Interfaces

```python
from portfolio_analyzer.engine import EngineDependencies, analyze_portfolio
from portfolio_analyzer.models import AnalysisRequest

request = AnalysisRequest.model_validate_json(open("examples/request.json").read())
result = await analyze_portfolio(request, EngineDependencies())
print(result.model_dump_json(indent=2))
```

An empty dependency set provides offline calculations. `build_dependencies(settings, secrets)` enables configured providers. `create_research_tool(dependencies)` exposes the same engine as a read-only LangChain subagent; a trading parent owns any execution decisions. See `examples/subagent.py` and the generated JSON schemas. Result schema version 2.0 replaces findings and source-bound summary objects with text and model-chosen sections; development builds do not keep that obsolete report contract.

`POST /api/runs` accepts a supplied `analysis` or the latest `stored` portfolio. `GET /api/runs/{id}` exposes progress and results. Research API tokens cannot mutate portfolios, import images, configure the app or send email. The dashboard owner can import/edit/save and explicitly request delivery. Email and scheduling are optional.

## Operational controls

Defaults are 600 seconds, 100 discovery searches, 64 model calls and 200 tool calls. Owner/service ceilings remain enforceable for hosted use, but limits are absent from the normal dashboard and model context. A finalization reserve allows writing from collected information. The optional writer receives the research conversation. Readiness exercises a real tool-result conversation and the same report schema used by runs. No model-family-specific request options are forced.

Cancellation propagates; interrupted work never implies successful completion. Provider errors are sanitized and retained in the trace. Network operations use bounded shared workers, DNS/IP validation, redirect revalidation, request timeouts and source-specific access rules. Private-network addresses are blocked. SEC access requires an identifying contact. SMTP uses validated TLS and a single verified owner recipient. Secrets stay in OS credentials or deployment-managed files, never browser values or run results.

## Validation

See `tests/README.md` for test scope. Core checks cover financial arithmetic, quote dates, the actual SDK tool wire contract, safe report rendering, editable imports, authorization, source transport, secret isolation and scheduled delivery semantics. UI browser smoke checks exercise portfolio drafts, saved secrets, the visible report and responsive layout. Tests do not enforce analyst wording, heading choices, source grades or a fixed number of research calls.
