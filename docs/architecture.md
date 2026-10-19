# Research architecture and integration

The research engine is an asynchronous Python library. Manual portfolio maintenance, the dashboard, persistence, scheduling, email, and any parent trading system sit outside it. A caller can supply holdings directly without brokerage credentials, an application account, SMTP configuration, or a scheduler.

```text
Saved manual/CSV/image portfolio or supplied snapshot
                 |
                 v
Validate holdings -> deterministic metrics -> tool-using analyst -> validated findings
                 |                                               |
                 +---------------- AnalysisResult ----------------+
                                      |
                     dashboard / JSON / optional email
```

The research engine has no trading or delivery tools. Its LangChain workflow receives bounded portfolio context, calculated metrics, and untrusted source excerpts; it cannot modify brokerage or application settings. An external auto-trading parent can call the research tool, but receives research data rather than an executable trade instruction. The parent owns its decisions, execution permissions, and execution safeguards.

## Python and LangChain interfaces

```python
from portfolio_analyzer.engine import EngineDependencies, analyze_portfolio
from portfolio_analyzer.models import AnalysisRequest

request = AnalysisRequest.model_validate_json(open("examples/request.json").read())
dependencies = EngineDependencies()  # Offline, metrics-only behavior
result = await analyze_portfolio(request, dependencies)
print(result.model_dump_json(indent=2))
```

The fixed example contains illustrative prices, not historical market observations. Replace its `snapshot.as_of` and valuations together before real analysis. Timestamps require an explicit timezone; decimal strings preserve supplied financial quantities. The executable [subagent example](../examples/subagent.py) generates a fresh, labeled demonstration snapshot and supports both interfaces without live requests:

```powershell
uv run python examples/subagent.py
uv run python examples/subagent.py --langchain
```

To add the tool to an existing LangChain parent:

```python
from portfolio_analyzer.agent import create_research_tool

research_tool = create_research_tool(dependencies)
# Supply research_tool in the parent's tools list. The tool argument is:
arguments = {"request": request.model_dump(mode="json")}
research_result = await research_tool.ainvoke(arguments)
```

The tool accepts the same request model and returns a JSON-compatible result dictionary. Direct Python invocation returns an `AnalysisResult`. Each invocation has fresh analysis state and no shared conversation history. Reuse an `EngineDependencies` instance across invocations to share its two-run concurrency semaphore; use one event loop for that instance. Provider caches contain public feed content only.

Optional injected dependencies are:

| Dependency | Interface | Included implementations |
| --- | --- | --- |
| `research` | `async search(query, max_results=3) -> list[Evidence]`; optional `async search_symbol(symbol, name, max_results=3, reporting_cutoff=None)`; optional `async retrieve(evidence) -> Evidence` | Tavily, DDGS, Federal Reserve updates, SEC current and per-company filings, keyless ticker news feeds (Google News/Nasdaq) |
| `market_data` | `async enrich(snapshot) -> PortfolioSnapshot`; optional `async history(snapshot) -> dict[symbol, dict[ISO-date, float]]` | yfinance |
| `model` | Configured LangChain chat model supporting tools | OpenAI-compatible and Anthropic integrations configured by the application |

Credentials belong to the injected providers, never to request JSON. A missing model produces a partial metrics report; providers are not enabled implicitly. The model receives the complete holding roster and lightweight initial coverage evidence, with an overall context-size cap. It chooses further queries, sources, and retrieval through registered read-only tools; source excerpts remain untrusted data. The underlying model's token limit and request retries must also be configured by its caller.

## Contracts and evidence

The generated schemas are [AnalysisRequest](schemas/analysis-request.json) and [AnalysisResult](schemas/analysis-result.json). The result has `schema_version: "1.0"`, a run identifier, timestamps, metrics, structured `coverage`, findings, sources, warnings, optional suggestions, and a status of `success`, `partial`, or `failed`. `coverage` gives valued/total holding counts, excluded symbols, research and narrative availability, and prioritized issues with actions. Consumers should validate the schema version and examine coverage and freshness before using findings.

`Evidence` carries an ID, URL, title, publisher, retrieval timestamp, optional publication timestamp, excerpt, and content type. The default content type, `search_excerpt`, means discovery evidence. `retrieved_page` and `official_feed` identify retrieved source content. `news_feed` is a dated headline plus link from a wire or aggregator feed: it establishes that the named publisher reported that headline on that date and supports only a headline-level statement, never article body detail or causation. Generated market findings require valid citations to `retrieved_page`, `official_feed`, or `news_feed` evidence; discovery snippets alone cannot support accepted findings. Per-holding coverage records `status` (`verified`, `discovery_only`, `no_verified_update`, `search_failed`, `not_checked`) and an `evidence_grade` of `full` or `headline` so a headline-only conclusion is never presented as full-text verification. Source retrieval permits public HTTPS destinations, pins the verified DNS destination, validates TLS, rejects redirects, limits response size, and blocks private or reserved addresses. Feed items never trigger page retrieval.

`reporting_cutoff` excludes sources whose known publication date is later than the cutoff. Undated pages remain explicitly uncertain. This is not a point-in-time backtesting interface: current holdings and undated sources cannot establish a historical information set.

Deterministic metrics distinguish known valuations from complete account value. Unpriced or unsupported holdings are listed; weights use known value. Daily price contributions use current quantities and previous closes, and do not represent cash-flow-adjusted account returns. ETF look-through is not implemented. Historical risk and benchmark comparisons use current weights and adjusted history on shared dates, with cash and missing histories assumed flat; their coverage and method are included. Missing history can understate risk. Benchmark comparison is withheld with an explicit warning when data is unavailable; an empty benchmark disables it.

Large stock weights produce single-company concentration observations; large ETF or fund weights instead prompt review of the fund mandate, underlying exposures, overlap, and liquidity. A fund weight alone does not establish issuer concentration or safety. Snapshot collection time is not a quote timestamp: `metrics.supplied_price_timestamp_unknown` identifies supplied valuations with unknown quote freshness. Supplemental yfinance valuations include their actual trading date, retrieval time, and daily-close label in `metrics.market_data_as_of`; they are not live quotes.

Suggestions are opt-in for each request. The investment profile requires nonblank strings for `objectives`, `horizon`, `risk_tolerance`, and `liquidity_needs`; a nonempty `allocation_targets` map of percentage values between 0 and 100 totaling at most 100; and a `restrictions` list, which may be empty. Invalid or incomplete profiles withhold suggestions while allowing the rest of the report. The request example shows the shape. `report_length` is `brief` or `detailed`; the engine limits generated findings to four or twelve respectively.

## Budgets, cancellation, and deployment boundaries

`max_searches` bounds logical discovery queries, not individual HTTP requests within an adapter. Defaults are 30 queries, 120 seconds, eight model calls, and 40 research tool calls; owner settings are ceilings for dashboard and parent-agent requests. The initial coverage pass uses at most eight queries and a third of the discovery budget when a model is enabled. The analyst chooses follow-up investigations; no fixed twelve-source shortlist limits it. Network tools allow at most four concurrent operations and reserve up to 20 seconds for finalization. Sources are registered with stable evidence IDs and grade-preserving URL deduplication, bounded to 200 records. Deterministic presentation groups stories after collection; it does not decide which sources the analyst may investigate. Detailed reports retain a coverage entry for every holding. Official feeds cache public responses for five minutes, and identified SEC access is paced. Zero findings and zero recommendations are valid outcomes.

The elapsed-time budget includes waiting for the shared concurrency semaphore. A budget timeout returns a partial report with completed calculations. Cancellation propagates as `asyncio.CancelledError`; it does not produce a misleading success. A synchronous network operation already dispatched to a worker thread may finish after cancellation, subject to its own timeout, but additional symbol requests stop. Hosts must enforce their own authenticated request quotas and reuse dependency limits when exposing the library as a service.

Public dashboard deployments require owner authentication, HTTPS, and a protected reverse proxy. Browser actions use session authentication and CSRF protection. Keep API credentials out of URLs and logs. The library itself does not authenticate callers or persist reports; those responsibilities belong to the application or embedding parent.

## Dashboard HTTP API

The application exposes the same research contract through an asynchronous run API. Configure `PORTFOLIO_SECRET_API_TOKEN` (or `PORTFOLIO_SECRET_API_TOKEN_FILE`) on the server, and send it in `Authorization: Bearer ...`. Browser sessions instead use the owner's session and CSRF protection. Public servers also require `PORTFOLIO_SERVER_MODE=1`, an HTTPS `PORTFOLIO_PUBLIC_URL`, and `PORTFOLIO_SECRET_OWNER_PASSWORD` of at least 16 characters (or its `_FILE` counterpart). Keep secrets out of request bodies, URLs, and checked-in examples.

| Request | Response |
| --- | --- |
| `POST /api/runs` with `{"analysis": <AnalysisRequest>, "send_email": false}` | HTTP 202 with `run_id`, queued status, and relative `result_url` |
| `GET /api/runs/{run_id}` | Persisted run record; `result` is `null` while incomplete, then an `AnalysisResult` object |
| `POST /api/runs/{run_id}/cancel` | Cancels an active run; the application records cancellation as a failed run without delivery |
| `DELETE /api/runs/{run_id}` | Deletes one finished report from history; active runs must be cancelled first. Browser sessions only |
| `POST /api/portfolio/enrich` with up to 30 draft positions | Read-only metadata and missing daily-close price proposals; no quantities are inferred or saved |
| `GET /api/quotes` | Latest cached automatic quotes for the saved portfolio: `prices` per symbol plus `as_of` and `refresh_seconds`. Missing symbols are omitted, never invented |
| `DELETE /api/secrets/{name}` | Removes a stored credential the owner manages; deployment-managed (`env`/`_FILE`) credentials are refused |
| `GET /api/schema/request` | Current `AnalysisRequest` JSON schema |
| `GET /api/schema/result` | Current `AnalysisResult` JSON schema |
| `GET /api/schema/submission` | Current HTTP wrapper JSON schema |

Alternatively submit `{"source":"demo","question":"Explain concentration","include_suggestions":false,"send_email":false}`. Sources are `demo` or `stored`. Stored means the latest saved user-maintained portfolio; no brokerage data is fetched. Demo runs remain offline. An explicit `analysis` object supplies its own snapshot and analysis options; owner-configured budgets remain ceilings. Leave `send_email` false when calling from a parent trading system.

```python
import json
import os
import httpx

# Token and URL are supplied by the integration environment, not the portfolio.
with httpx.Client(
    base_url=os.environ["PORTFOLIO_API_URL"],
    headers={"Authorization": "Bearer " + os.environ["PORTFOLIO_API_TOKEN"]},
    timeout=30,
) as client:
    analysis = json.load(open("examples/request.json"))
    response = client.post("/api/runs", json={"analysis": analysis, "send_email": False})
    response.raise_for_status()
    ticket = response.json()
    # Poll result_url at a modest interval until the record is terminal.
    record_response = client.get(ticket["result_url"])
    record_response.raise_for_status()
    record = record_response.json()
```

Research bearer tokens cannot change settings, import holdings, connect accounts, or request email delivery. They can submit research, read application results and schemas, and cancel runs. Delivery and configuration changes require the owner browser session.

Keep the persisted run status separate from `result.status`: a completed execution can contain a partial research result. Inspect the record's error when execution fails without a result. HTTP cancellation cancels the active research task, not any action taken separately by a parent system.


## Portfolio maintenance and screenshots

The owner can preview CSV with `POST /api/import` and save a reviewed snapshot with `POST /api/portfolio` using `{"snapshot": <PortfolioSnapshot>}`. A CSV can contain only ticker/symbol and price per share; its preview then contains null quantities that the owner must fill before saving. Instrument metadata is resolved at analysis time where reliable and remains unknown where ambiguous. CASH/USD/cash-type rows normalize into the cash balance. Saving replaces the current portfolio, and scheduled jobs read it at execution time. Updating prices for research never changes stored quantities/cash. The supplied `as_of` remains the position timestamp and is shown in reports even when prices are refreshed.

Prices are maintained automatically. The owner enters tickers, share counts, and cash; a server-side quote service (yfinance by default) refreshes daily-close prices on a TTL — about 60 seconds during US market hours, otherwise the configured interval — and persists them to the saved snapshot without changing quantities, cash, or the snapshot date. `prices_as_of` records each symbol's trading date and retrieval time, the dashboard shows a freshness label instead of an editable price box, and `/api/quotes` exposes the latest cached quotes. A manually supplied or CSV price remains only as an explicit offline fallback; valuations are never inferred from percentages or account values.

Credentials resolve through one documented precedence order: the `PORTFOLIO_SECRET_<NAME>` environment variable, then an absolute `PORTFOLIO_SECRET_<NAME>_FILE` mount, then the key saved from the dashboard, then a convenience provider alias (for example `OPENAI_API_KEY`). Canonical deployment variables are authoritative and read-only; aliases are only a fallback, so a dashboard-saved key overrides an ambient alias. Model keys are stored per provider (`model_api_key_<provider>`), so switching providers never silently reuses another provider's credential; the legacy shared `model_api_key` is read only to migrate existing installs. The dashboard reports each credential's presence, source, provider, and saved time, and never returns a stored secret value.

`POST /api/import/image` accepts `{"image_base64": "...", "content_type": "image/png", "as_of": "<timezone-aware ISO date>"}` and returns an **unsaved** draft. JPEG and WebP are also accepted. Unknown quantities/cash are null; the dashboard requires correction before a separate save request. Images are decoded with size/pixel/type checks, metadata is removed, and pixels are sent to a configured vision-capable model. Original screenshots are not written to disk. Research bearer tokens cannot use either portfolio mutation or image extraction endpoints.


## Source diagnostics and news presentation

Source adapters publish sanitized `source_outcomes` using passed/empty/skipped/failed/disabled states. `POST /api/test/search` probes configured sources independently and persists outcomes under `connection_tests.search.sources`; `/api/status` exposes these without credentials. Result coverage includes run-specific source outcomes and named skipped/failed-source issues. SEC contact validation gates every sec.gov request before DNS access.

Position-news entries retain legacy status/source IDs and add `stories` and `additional_stories`. Stories retain their representative/member evidence IDs, best evidence grade, affected symbols, publisher metadata, category, and related-report count. `metrics.position_news_display_order` controls deterministic value-based rendering; `setup_news_reason` explains skipped broad searches. Existing stored reports without these fields use a rendering fallback. Clustering and ranking do not add model calls; presentation metadata is excluded from the analyst prompt. The analyst can select additional evidence through its research tools.

`render_report` and `send_report` accept an optional timezone (default America/New_York); dashboard, CLI, and email callers pass the configured schedule timezone. Raw JSON timestamps remain ISO formatted.


## Tool-using analyst and observable activity

The LangChain analyst has `search_asset_news`, `search_research`, `retrieve_public_page`, and `lookup_market_data` tools. Source selection is explicit and capability-based; configured Federal Reserve updates, news feeds, general search, and SEC filings remain available independently. Discovery tools accept free-form focus queries. Tool outputs include evidence IDs, raw bounded excerpts, publisher/date metadata, grades, and remaining budgets. Factual findings and the optional `executive_summary` are independently checked against collected evidence; only accepted findings can support the summary. Adjustment suggestions remain explicitly requested and may be empty.

`AnalysisResult` adds `executive_summary`, `research_activity`, and `research_budget`. Running `GET /api/runs/{id}` records expose activity and budgets before final results exist; SQLite migrations add these fields without changing old results. Cancellation and startup recovery retain observed activity. Activity records describe tool calls and outcomes, never private model reasoning. Report conclusions use the executive summary or an honest concrete fallback; operational coverage is labeled separately.

Source probes distinguish Federal Reserve updates, Google News, Nasdaq, Bing News, search, and SEC. Transport follows at most three public HTTPS redirects, validates and pins DNS at every hop, and applies SEC contact rules to each destination. A blocked source remains visible while independent working sources can be used.

Public page/feed requests use four retained worker slots and close tracked sockets on cancellation, with a 15-second absolute request deadline across redirects. A per-run source-operation semaphore also bounds composite adapter fan-out to four; completed sources survive slow siblings. Third-party DDGS/yfinance SDK operations retain their own timeout behavior and may finish after cancellation.
