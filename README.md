# Betafolio

A personal portfolio dashboard with read-only research. Maintain holdings and cash manually, review CSV or Excel imports, and run analyses on demand or on an optional schedule. There is no brokerage login, synchronization, or trading integration.

## Start locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```powershell
uv sync --frozen --python 3.12
uv run portfolio-analyzer serve
```

Open **http://127.0.0.1:8000**. The demonstration portfolio runs offline without a model key, email, or schedule; its prices are illustrative.

## Maintain your portfolio

**Overview → Holdings & cash** is your portfolio dashboard. Enter each holding's ticker and number of shares, plus cash in its separate field; prices are not typed. The server polls market data on a schedule and fills each position's price automatically, showing a "Prices updated …" freshness label and the last retrieval time. Save before generating a briefing. Unsaved changes are preserved during dashboard refresh and block manual runs until saved or discarded; automatic price updates never overwrite your editable fields or mark the editor unsaved.

Use **Fill missing details** on a draft to look up exact-ticker instrument names, types, sectors, and exchanges. It proposes changes for review and never fills share counts, replaces values you entered, or saves the portfolio automatically. The button supports up to 30 holdings at a time and requires network access to the market-data provider. It can also attach an offline daily-close quote for a holding that has none yet.

Under **Import & research**, upload a file:

- **CSV or Excel:** drag a `.csv`, `.xlsx` or `.xls` file into the import area, or choose a file. Include ticker and price columns; shares are optional at import and must be supplied before saving. Excel reads the first visible worksheet. Files are limited to 5 MB and 1,000 holdings. Imports create editable drafts; review shares and cash before saving.

Cash-only portfolios are supported. Duplicate holding rows must be combined before saving. The current portfolio is always the **latest saved** draft, not a partially edited import. Saving an edit replaces the current portfolio. Previous reports keep their original results.

Cash-like ticker symbols explicitly marked as ETFs remain holdings, so an ETF named USD is not converted into cash.

Scheduled runs use the most recently saved quantities and cash at the start of the run. Quantities and cash never synchronize automatically, but prices do: when automatic pricing is enabled the server polls quotes in the background and shortly after a run, refreshing the saved snapshot's prices without changing quantity, cash, or the snapshot date. A manual or CSV price is an explicit offline fallback used until the next automatic update. Reports show the saved snapshot date and warn about stale holdings. When research-time market data is enabled, current available daily closing prices are also fetched for analysis without changing saved quantities, cash, or the original uploaded prices. Missing data is labeled; fresh holdings are never inferred from old uploads.

## Configure research

1. Configure and test a model in **Import & research**. OpenAI, Anthropic and any custom OpenAI-compatible endpoint are supported. Enter the endpoint, model identifier and API key for your provider.

2. Keyless search, Federal Reserve updates, and keyless ticker news feeds (Google News/Nasdaq) are enabled by default for saved portfolios. SEC per-company filings (8-K/10-Q/10-K) are optional and start off without an identifying contact. Add a contact such as `Your name contact@example.com` beside the SEC checkbox to enable them; you can turn them off afterward. For more dependable news discovery, choose Tavily and add a key; you can also disable external research. Provider quotas still apply.

3. Prices update automatically: the server polls market data in the background (every minute while US markets are open, otherwise every 15 minutes by default) and via the dashboard's `GET /api/quotes` poll. Tune this with the `auto_prices_enabled` and `price_refresh_minutes` settings. Optionally enable yfinance for research-time daily closing prices and historical estimates. It is unofficial and intended for personal use.

4. Optionally supplement broad search with Federal Reserve updates or identified SEC current-filings discovery.

5. Generate a briefing from **My saved portfolio**. The finished conclusions appear below the portfolio summary and remain available in Research history. The dashboard shows whether portfolio measures, current-event search, and written analysis are configured and last tested.

The analyst chooses its own research approach, queries, report headings and emphasis. Three tools support it: news search with prepared readable content, article reading, and market-data lookup. The briefing includes an executive summary, daily position outlook, relevant news and important considerations. Brief and detailed preferences guide the amount of detail.

Internal backstops default to ten minutes, 100 searches, 64 model calls and 200 tool calls. They are service configuration, not dashboard counters. The Activity view lets you select a run and inspect its agent trace, including tool queries and outcomes, without private model reasoning. Overview shows a compact status while research runs.

**Test saved search** checks each configured source separately and shows passed, empty, skipped, failed, or disabled outcomes. An empty response means the request worked but found no matching updates. Federal Reserve, Google News, and Nasdaq have separate outcomes, so a blocked feed cannot hide another feed that works. SEC requests with a missing or invalid identifying contact are skipped and named in run coverage. Changing research settings invalidates the saved source checks.

The analyst chooses its report structure. The renderer typesets paragraphs, emphasis, lists and tables while escaping raw HTML. Report headings use an editorial serif, with clear sans-serif body text and tabular numbers. Source grades and forced story templates are not part of the report.

Dates use the timezone selected in Schedule & email, including when scheduling is off. A positions table accompanies the briefing. Percentage-only email keeps amounts and narrative out of the message.

Model API access needs a key with access to the selected tool-capable model. No model is enabled by default. News preparation supplies publisher, publication date and readable content; the analyst decides what matters and explains material uncertainty in ordinary language.

Model readiness (the setup test behind `POST /api/model/readiness`) sends two one-line probe requests and no portfolio data: one asks the model to call a trivial tool, one asks for a tiny JSON object. Each check reports `ok`, `weak`, or `fail`. A `weak` tool check means the analyst may gather little evidence and reports stay thin; a `weak` JSON check means briefing writes spend extra calls fixing formatting and the written analysis is more likely to be lost. For research runs, tool-calling and structured-output reliability matter more than raw model size: prefer models advertised with tool/function calling and JSON output. `fail` on either check points at the endpoint, API key, or model name first.

The dashboard and email use the same report, preserving model-chosen headings and prose. Sources and operational details remain available in the run record rather than filling the briefing. Current prices use timestamped regular-session quotes; daily-history fallback keeps its actual session date. Older or undated prices never become today’s returns, and an older response cannot overwrite a newer saved quote. The expected US session respects market opens, weekends and holidays. Adjustment ideas require an explicit request; scheduled runs do not request them.

**Research history** keeps your recent briefings in a bounded, scrollable list. Delete an individual report, or use **Clear finished** to remove completed runs; reports also age out after the retention window set under Preferences. Notices can be dismissed with the × control, and the model endpoint honors the base URL you configure for the selected provider.

Saved API and SMTP secrets appear as masked, configured credentials in settings. Use **Change secret** to enter a replacement (an unchanged secret is never sent back to the browser), **Remove key** to delete a key you saved, and **Test this key** to verify a typed value without saving it. Model keys are stored per provider, so switching providers does not reuse another provider's key.

V1 covers stocks, ETFs, and cash. Unsupported or unpriced holdings are shown. Weights use known valued assets. Price contributions use current quantities and previous closes, not cash-flow-adjusted account returns. Historical volatility, drawdown, and benchmark comparisons estimate today's portfolio using shared historical dates; they do not reconstruct the user's realized experience. Missing histories can understate risk. Complete ETF look-through, options/crypto analytics, transaction accounting, tax-lot advice, and trading are outside scope.

## Optional email and scheduling

Both features are entirely optional and independent. Scheduling can save dashboard reports without email; a manual report can be emailed without a schedule.

Email uses your mailbox's SMTP service, certificate-validated implicit TLS or required STARTTLS, and one verified owner recipient. The dashboard offers Gmail and iCloud Mail presets with provider instructions; other SMTP settings remain editable. Use an app password when supported. Outlook.com requires OAuth2 and is not supported by this password-based SMTP form.

Why SMTP rather than an inbox-connected API: the agent must deliver scheduled, unattended briefings without you opening the app, and SMTP with an app password needs no OAuth scopes or inbox read access. Security is respected end to end: your app password is stored only in the OS credential store (no plaintext fallback), the connection requires certificate-validated TLS (implicit on port 465 or required STARTTLS on 587), and messages go only to your single owner-verified recipient — no CC/BCC, tracking pixels, or model-selected recipients. For Gmail in a Google Workspace domain, an admin may need to allow SMTP AUTH; see Google Workspace's guide to sending email from an app: <https://knowledge.workspace.google.com/admin/gmail/send-email-from-a-printer-scanner-or-app>.

Changing the recipient clears verification and scheduled delivery. Messages contain HTML plus equivalent plain text, with plain-text-only and percentage-only options. The latter omits amounts, narrative, and sources to avoid leaking amounts through prose. No CC, BCC, tracking pixels, or model-selected recipients are supported.

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

API keys use a supported OS credential store with no plaintext fallback. Model keys are stored per provider (`PORTFOLIO_SECRET_MODEL_API_KEY_OPENAI`, `..._ANTHROPIC`, `..._CUSTOM`) so switching provider never silently reuses another provider's key. Other credentials use `PORTFOLIO_SECRET_<NAME>` for `TAVILY_API_KEY`, `SMTP_PASSWORD`, `OWNER_PASSWORD`, and `API_TOKEN`.

Every consumer resolves secrets with one precedence rule, highest first: a `PORTFOLIO_SECRET_<NAME>` environment variable, then an absolute `PORTFOLIO_SECRET_<NAME>_FILE` mounted file, then the key saved from the dashboard, then a documented convenience alias (`LITELLM_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `TAVILY_API_KEY`). Canonical `PORTFOLIO_SECRET_*` values are authoritative and read-only; convenience aliases are only a fallback, so a key saved in the dashboard overrides an ambient alias (for example a stale `LITELLM_API_KEY` left in your shell) — the model card reports which alias is supplying a key and lets you replace it.

The dashboard shows each saved key, when it was saved, and which provider it belongs to. You can remove a saved key, and "Test this key" checks a typed value without saving it; "Test saved model/search" checks the persisted configuration and requires a prior save. If the OS keyring is unavailable (for example a headless Linux/WSL host), a banner names the exact environment variables to set (`PORTFOLIO_SECRET_<NAME>` or an absolute `PORTFOLIO_SECRET_<NAME>_FILE`).

Nonsecret overrides use `PORTFOLIO_SETTING_<FIELD>`; use JSON for nested objects, booleans, and numbers. They appear read-only in the UI. Per-run requests cannot raise application budgets or weaken security controls.

External models receive portfolio/profile context and research content; the account identifier is omitted. Imported files are parsed locally. Search providers receive ticker/focus queries. Do not put secrets in questions. The app does not enable external tracing. HTML includes no remote images or scripts.

The primary local target is Windows; Linux/macOS credential storage requires a supported OS keyring. Manual portfolios and the offline demo do not need a keyring. No brokerage integration or brokerage credentials are required.

## Private server deployment

The Docker image uses frozen uv dependencies, a nonroot user, and persistent `/data`. Compose binds only to loopback and requires an HTTPS reverse proxy. Do not expose port 8000 directly.

Create a private `deployment-secrets` directory with files named `owner_password`, `api_token`, `model_api_key_custom`, `tavily_api_key`, and `smtp_password`. The owner password must have at least 16 characters; use a long random API token. Optional provider files can be empty. The Compose example mounts a custom endpoint model key; for another provider, use its `PORTFOLIO_SECRET_MODEL_API_KEY_<PROVIDER>_FILE` variable and matching secret file. Keep permissions restrictive and never commit them.

Set `PORTFOLIO_PUBLIC_URL` to your exact HTTPS origin, then run `docker compose up --build -d`. Sign in and configure providers; edit mounted credential files outside the dashboard. For non-Docker hosting, set `PORTFOLIO_SERVER_MODE=1`, the HTTPS origin, and owner secret, then run `uv run portfolio-analyzer serve --server --host 0.0.0.0` behind a protected reverse proxy.

Browser mutations require owner sessions and CSRF protection. Research clients use `Authorization: Bearer <API_TOKEN>` over HTTPS. Research tokens can request/read/cancel analyses; they cannot modify portfolios/settings, import portfolio files, or send email.

## Verification

```powershell
uv sync --frozen --group dev --python 3.12
uv run pytest -q
uv run ruff check src tests examples
uv run python examples/subagent.py --langchain
```

Tests use fixtures/mocked external responses. `diagnostics --live` and connection-test buttons explicitly consume configured provider access. Provider entitlements and real inbox delivery require your credentials. An optional browser test uses Node.js and installed Chrome: start a dashboard with a test data directory, then run `node scripts/browser-smoke.mjs`; set `CHROME_PATH` if necessary. Screenshots go under ignored `.tools`.

Model request timeout is configurable under advanced model settings (90 seconds by default). The overall research budget still applies. Research readiness checks the tool-result conversation and the actual briefing schema; it does not guarantee that every source will answer.
