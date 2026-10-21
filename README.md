# Betafolio

Betafolio is both a standalone portfolio research agent and a local browser command center for portfolios, provider connections, research runs, reports, and agent traces. Maintain holdings and cash, then ask a tool-using analyst to research your portfolio and write a briefing. The reusable research engine can also run from Python or as a subagent inside a larger application.

## Quickstart

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.12, then:

```powershell
uv sync --frozen --python 3.12
uv run betafolio serve
```

Open http://127.0.0.1:8000. The dashboard starts without a model connection; configure and test a model and at least one working research source before requesting research. The demonstration portfolio can run offline and uses illustrative values.

## Use the research desk

In **Holdings & cash**, enter each ticker and share quantity, then enter cash as a monetary amount. Prices update automatically every 60 seconds; the interval can be configured from 15 to 86,400 seconds. Uploaded prices are ignored. CSV, XLSX, and XLS imports need ticker and quantity columns; review the editable draft and supply any missing quantities before saving. Excel imports use the first visible worksheet. Cash can be imported as a cash quantity, which represents its monetary value.

Configure a model and test its connection in **Connections**. OpenAI, Anthropic, and custom OpenAI-compatible endpoints are supported. Then test research sources: DDGS web search is the automatic search backend; Tavily is optional, as are Federal Reserve updates, Google News, Nasdaq news, and SEC company filings. At least one model and one working news/research source are required. Betafolio checks connections before research and blocks a run if setup is incomplete. Checks are cached until their configuration or credential changes.

Start a run from **Portfolio**, follow its status, and return to its briefing in research history. Open **Activity** to select a run and review the observed agent calls and outcomes. Activity traces do not include private model reasoning. Optional scheduling and email are configured separately.

## Developer quickstart

Install the development dependencies and run the app:

```powershell
uv sync --frozen --group dev --python 3.12
uv run betafolio serve
```

The core engine can be called directly or wrapped as a LangChain research tool:

```python
from betafolio import AnalysisRequest, EngineDependencies, analyze_portfolio
from betafolio.agent import create_research_tool

request = AnalysisRequest.model_validate(your_request_dict)
dependencies = EngineDependencies()
result = await analyze_portfolio(request, dependencies)
tool = create_research_tool(dependencies)
```

The research tool returns evidence and analysis for a parent agent; any external execution decision belongs to that parent. See [architecture](docs/architecture.md), [subagent example](examples/subagent.py), and generated [request](docs/schemas/analysis-request.json) and [result](docs/schemas/analysis-result.json) schemas.

Useful commands:

```powershell
uv run betafolio analyze --demo --offline
uv run betafolio analyze --request examples/request.json
uv run betafolio analyze --format text
uv run betafolio diagnostics
uv run betafolio schema request
```

For native preview bundles, build on the target OS and architecture (cross-compilation is not supported):

```powershell
uv sync --frozen --group packaging --python 3.12
uv run --no-sync python scripts/build-executables.py
```

The executable workflow is manually dispatched and uploads CI artifacts; it does not publish a public release automatically. Native macOS builds are configured for arm64 and x86_64. Those builds have not yet been verified on macOS.

## Security and privacy

Portfolio data and reports are stored locally in SQLite under the platform user-data directory. Set `PORTFOLIO_DATA_DIR` or pass `--data-dir` to choose another location. Protect the directory and backups with OS permissions and disk encryption.

Provider secrets are stored in the operating system credential store. Environment variables with the `PORTFOLIO_SECRET_` prefix and absolute `PORTFOLIO_SECRET_*_FILE` paths can supply secrets instead; nonsecret settings use `PORTFOLIO_SETTING_*`. Model keys are stored per provider. External models receive portfolio context and research content; search providers receive ticker/focus queries. Imported files are parsed locally. Betafolio has no brokerage connection, order interface, or trading tools.

See [architecture](docs/architecture.md) for operational controls. Optional server mode requires owner authentication and an HTTPS public origin. Do not expose port 8000 directly to the internet.

## Troubleshooting

- **Research cannot start:** open Connections, configure and test the model, then enable and test at least one source that returns a passed or empty result.
- **Prices are missing:** check the ticker and network access; quote retrieval is best effort and does not use prices from uploaded files.
- **The app stops when the console closes:** keep the console open while using the browser desk. Scheduled runs also require Betafolio to be running and the computer awake.
- **A preview is blocked by the operating system:** bundles are unsigned private previews. Only use the platform's documented approval steps for software from a distributor you trust.
