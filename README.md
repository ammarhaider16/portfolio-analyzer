# Portfolio Analyzer

A personal portfolio dashboard with read-only research. Maintain holdings and cash manually, review CSV or screenshot imports, and run analyses on demand or on an optional schedule. There is no brokerage login, synchronization, or trading integration.

## Start locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```powershell
uv sync --frozen --python 3.12
uv run portfolio-analyzer serve
```

Open **http://127.0.0.1:8000**. The demonstration portfolio runs offline without a model key, email, or schedule; its prices are illustrative.

## Maintain your portfolio

**Overview → Holdings & cash** is your portfolio dashboard. Add, edit, and remove positions; set the snapshot date; and enter cash in its separate field. Save before generating a briefing. Unsaved changes are preserved during dashboard refresh and block manual runs until saved or discarded.

Under **Imports & models**, choose either:

- **CSV:** preview a file, review/edit its positions and cash in the dashboard, then save. See [examples/holdings.csv](examples/holdings.csv). Required columns are `symbol,quantity`; optional columns are `price,asset_type,sector,previous_close`. CASH, USD, and cash-type rows are recognized as cash. Cash uses quantity × price, with price defaulting to 1. Quoted currency amounts such as `"$1,250.50"` are supported. Missing prices remain unknown.
- **Screenshot:** upload PNG, JPEG, or WebP up to 5 MB. Explicitly authorize sending the image to your configured model. A vision-capable model is required; the Colgate text-model preset might not support images. Extraction creates an unsaved draft. Unreadable quantities and cash remain blank and must be filled in before saving. Review every value: image extraction can make mistakes. Crop out account numbers and unrelated personal details. Original image bytes are not saved by this application.

Cash-only portfolios are supported. Duplicate holding rows must be combined before saving. The current portfolio is always the **latest saved** draft, not a partially edited import. Saving an edit replaces the current portfolio. Previous reports keep their original results.

Cash-like ticker symbols explicitly marked as ETFs remain holdings, so an ETF named USD is not converted into cash.

Scheduled runs use the most recently saved quantities and cash at the start of the run. Positions never synchronize automatically. Reports show the saved snapshot date and warn about stale holdings. When optional market data is enabled, current available daily closing prices are fetched for analysis without changing saved quantities, cash, or the original uploaded prices. Missing data is labeled; fresh holdings are never inferred from old uploads.

## Configure research

1. Configure and test a model in **Imports & models**. Colgate LiteLLM defaults to `openai/gpt-oss-120b` at `https://litellm.colgate.edu`. OpenAI, Anthropic, and custom OpenAI-compatible endpoints are supported.
2. Select Tavily search with a free-tier key, keyless DDGS search, or disable external research. Provider quotas still apply.
3. Optionally enable yfinance for daily closing prices and historical estimates. It is unofficial and intended for personal use.
4. Optionally supplement broad search with Federal Reserve/BLS feeds, identified SEC current-filings discovery, or a paid Alpha Vantage news key.
5. Generate a briefing from **My latest saved portfolio**. The saved report appears in Research history.

Model API access needs a key with access to the selected model. No model is enabled by default. Missing or failed providers leave clearly labeled partial reports. Web snippets are discovery references; accepted generated findings require retrieved or official evidence. Search coverage is not a guaranteed events calendar, and coincident news does not establish causation.

Configure report detail, benchmark, review thresholds, research/time budgets, maximum snapshot age, and retention under **Preferences**. Adjustment ideas require a complete investment profile and an explicit per-run request. That checkbox resets after a run; scheduled reports never request adjustment ideas.

V1 covers stocks, ETFs, and cash. Unsupported or unpriced holdings are shown. Weights use known valued assets. Price contributions use current quantities and previous closes, not cash-flow-adjusted account returns. Historical volatility, drawdown, and benchmark comparisons estimate today's portfolio using shared historical dates; they do not reconstruct the user's realized experience. Missing histories can understate risk. Complete ETF look-through, options/crypto analytics, transaction accounting, tax-lot advice, and trading are outside scope.

## Optional email and scheduling

Both features are entirely optional and independent. Scheduling can save dashboard reports without email; a manual report can be emailed without a schedule.

Email uses your mailbox's SMTP service, certificate-validated implicit TLS or required STARTTLS, and one verified owner recipient. Use an app password when supported. Changing the recipient clears verification and scheduled delivery. Messages contain HTML plus equivalent plain text, with plain-text-only and percentage-only options. The latter omits amounts, narrative, and sources to avoid leaking amounts through prose. No CC, BCC, tracking pixels, or model-selected recipients are supported.

Scheduling defaults to 30 minutes after the NYSE close, accounting for holidays, early closes, and daylight saving changes. It reads the latest saved portfolio each run. Your computer must be awake and the app running. Restart recovery processes only the latest eligible missed session since scheduling was enabled. Overlapping and duplicate session runs are blocked. Uncertain email acceptance is not automatically retried.

Windows startup is opt-in after `uv sync`:

```powershell
powershell -File scripts/enable-startup.ps1
powershell -File scripts/enable-startup.ps1 -Disable
```

Use one dashboard instance per data directory.

## Immediate runs and subagents

```powershell
uv run portfolio-analyzer analyze --demo --offline
uv run portfolio-analyzer analyze --request examples/request.json
uv run portfolio-analyzer analyze --format text
uv run portfolio-analyzer diagnostics
uv run portfolio-analyzer diagnostics --live
```

The request example contains illustrative prices and a fixed date: replace valuations and their timestamp together. `--offline` disables external providers. `--send` explicitly requests delivery to the configured verified owner. Without `--request` or `--demo`, the CLI reads the latest saved portfolio.

The core is reusable without the dashboard or storage:

```python
from portfolio_analyzer import AnalysisRequest, EngineDependencies, analyze_portfolio
from portfolio_analyzer.agent import create_research_tool

request = AnalysisRequest.model_validate(your_request_dict)
dependencies = EngineDependencies()  # Inject configured providers for network research
result = await analyze_portfolio(request, dependencies)
tool = create_research_tool(dependencies)
data = await tool.ainvoke({"request": request.model_dump(mode="json")})
```

An external trading parent receives research, evidence, coverage, and optional ideas. It owns any execution decision and safeguards. This component has no order interface or trading tools. See [architecture](docs/architecture.md), [runnable examples](examples/subagent.py), and generated [request](docs/schemas/analysis-request.json)/[result](docs/schemas/analysis-result.json) schemas.

## Settings and privacy

Configuration and reports live in the platform user-data directory; override with `PORTFOLIO_DATA_DIR` or `--data-dir`. Retention defaults to 90 days. Financial data and reports are stored locally in SQLite; protect the directory and backups with OS permissions and disk encryption.

API keys use a supported OS credential store with no plaintext fallback. Headless servers use environment or mounted secrets. Use `PORTFOLIO_SECRET_<NAME>` or an absolute `PORTFOLIO_SECRET_<NAME>_FILE` path for `MODEL_API_KEY`, `TAVILY_API_KEY`, `ALPHA_VANTAGE_API_KEY`, `SMTP_PASSWORD`, `OWNER_PASSWORD`, and `API_TOKEN`. Corresponding model presets also accept `LITELLM_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY`. Externally managed credentials cannot be changed in the dashboard.

Nonsecret overrides use `PORTFOLIO_SETTING_<FIELD>`; use JSON for nested objects, booleans, and numbers. They appear read-only in the UI. Per-run requests cannot raise application budgets or weaken security controls.

External models receive bounded portfolio/profile context and evidence; the account identifier is omitted. Screenshot extraction sends sanitized pixels to the selected provider only after the upload action. Search providers receive ticker/focus queries. Do not put secrets in questions. The app does not enable external tracing. HTML includes no remote images or scripts.

The primary local target is Windows; Linux/macOS credential storage requires a supported OS keyring. Manual portfolios and the offline demo do not need a keyring. Old broker settings are ignored, and no stored brokerage credentials are read.

## Private server deployment

The Docker image uses frozen uv dependencies, a nonroot user, and persistent `/data`. Compose binds only to loopback and requires an HTTPS reverse proxy. Do not expose port 8000 directly.

Create a private `deployment-secrets` directory with files named `owner_password`, `api_token`, `model_api_key`, `tavily_api_key`, and `smtp_password`. The owner password must have at least 16 characters; use a long random API token. Optional provider files can be empty. Keep permissions restrictive and never commit them.

Set `PORTFOLIO_PUBLIC_URL` to your exact HTTPS origin, then run `docker compose up --build -d`. Sign in and configure providers; edit mounted credential files outside the dashboard. For non-Docker hosting, set `PORTFOLIO_SERVER_MODE=1`, the HTTPS origin, and owner secret, then run `uv run portfolio-analyzer serve --server --host 0.0.0.0` behind a protected reverse proxy.

Browser mutations require owner sessions and CSRF protection. Research clients use `Authorization: Bearer <API_TOKEN>` over HTTPS. Research tokens can request/read/cancel analyses; they cannot modify portfolios/settings, upload screenshots, or send email.

## Verification

```powershell
uv sync --frozen --group dev --python 3.12
uv run pytest -q
uv run ruff check src tests examples
uv run python examples/subagent.py --langchain
```

Tests use fixtures/mocked external responses. `diagnostics --live` and connection-test buttons explicitly consume configured provider access. Screenshot model capability, provider entitlements, and real inbox delivery require your credentials. An optional browser test uses Node.js and installed Chrome: start a dashboard with a test data directory, then run `node scripts/browser-smoke.mjs`; set `CHROME_PATH` if necessary. Screenshots go under ignored `.tools`.

