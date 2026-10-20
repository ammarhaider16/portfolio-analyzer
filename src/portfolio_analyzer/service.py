"""Application orchestration around the independently reusable research engine."""
import asyncio
import hashlib
import json
import os
import secrets as random_secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import load_settings
from .delivery import send_report, send_verification
from .engine import EngineDependencies, analyze_portfolio
from .models import AnalysisRequest, AnalysisResult
from .providers import (CompositeResearchProvider, DDGSResearchProvider, PrimaryFeedResearchProvider,
                        SecFilingsResearchProvider, TavilyResearchProvider, TickerNewsFeedProvider,
                        YFinanceMarketDataProvider)
from .scheduling import latest_eligible_run
from .secrets import SecretStore, SecretStorageError
from .storage import Store


# Per-provider model key slots. Names must match SecretStore's identifier rule
# ([a-zA-Z][a-zA-Z0-9_]*) so they map cleanly to PORTFOLIO_SECRET_<NAME>.
MODEL_PROVIDER_SLOTS = {
    "openai": "model_api_key_openai",
    "anthropic": "model_api_key_anthropic",
    "custom": "model_api_key_custom",
}
# Documented deployment aliases accepted for each provider.
MODEL_PROVIDER_ALIASES = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "custom": ("LITELLM_API_KEY",),
}
SECRET_ALIASES = {
    "tavily_api_key": ("TAVILY_API_KEY",),
}
# Virtual dashboard key; persisted credentials always use a provider-specific slot.
ACTIVE_MODEL_KEY = "model_api_key"


def safe_error_reason(exc, secret=None):
    """A short, secret-free category describing why a provider call failed."""
    from pydantic import ValidationError
    if isinstance(exc, ValidationError):
        return "the model returned an invalid briefing shape; check structured tool-output support"
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401:
        return "the endpoint rejected the API key (HTTP 401). Check that the key belongs to the selected provider"
    if status == 403:
        return "the endpoint refused access (HTTP 403). Check the key's permissions and model access"
    if status == 404:
        return "the endpoint or model was not found (HTTP 404). Check the base URL and model identifier"
    if status == 429:
        return "the provider rate limit was reached (HTTP 429)"
    if status == 400:
        body = getattr(exc, "body", {})
        item = body.get("error", body) if isinstance(body, dict) else {}
        parameter = item.get("param") if isinstance(item, dict) else None
        if parameter in {"temperature", "max_tokens", "max_completion_tokens", "reasoning_effort"}:
            return f"the model rejected the {parameter} setting; adjust advanced model settings"
        if isinstance(parameter, str) and parameter.startswith(("tools", "response_format")):
            return "the endpoint rejected the research tool schema; choose a model supporting function tools"
        return "the endpoint rejected the research request (HTTP 400); check model capabilities and settings"
    if isinstance(status, int) and status >= 500:
        return f"the provider returned a server error (HTTP {status})"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in type(exc).__name__.lower():
        return "the provider request timed out; check the connection and retry"
    return "the provider request failed; check the selected model, credentials, and connection"


def _read_stored(store, name):
    try:
        return store.get(name)
    except SecretStorageError:
        return None


def resolve_secret(store, name, aliases=()):
    """Resolve one secret through the single documented precedence rule.

    Highest precedence first:
      1. The ``PORTFOLIO_SECRET_<NAME>`` environment variable (deployment managed).
      2. An absolute ``PORTFOLIO_SECRET_<NAME>_FILE`` mounted file (deployment managed).
      3. The OS keyring value saved from the dashboard.
      4. A convenience provider alias environment variable (for example ``OPENAI_API_KEY``).

    Canonical ``PORTFOLIO_SECRET_*`` values are authoritative and read-only. Convenience
    aliases are only a fallback, so a key saved in the dashboard overrides an ambient alias
    (such as a stale ``LITELLM_API_KEY`` left in the shell) without being locked out.

    Returns ``(value, source)`` where ``source`` is ``"env"``, ``"file"``, ``"keyring"``,
    ``"alias"``, or ``None`` when the secret is not configured. ``create_model``,
    ``build_dependencies`` and ``/api/status`` all resolve through this one helper so the
    UI, presence indicators, and actual usage can never disagree.
    """
    key = "PORTFOLIO_SECRET_" + name.upper()
    if key in os.environ:
        return os.environ[key], "env"
    if key + "_FILE" in os.environ:
        value = _read_stored(store, name)
        return (value, "file") if value else (None, None)
    value = _read_stored(store, name)
    if value:
        return value, "keyring"
    for alias in aliases:
        if os.environ.get(alias):
            return os.environ[alias], "alias"
    return (None, None)


def read_secret(store, name):
    """Return only the resolved value for a secret (thin wrapper over ``resolve_secret``)."""
    return resolve_secret(store, name, SECRET_ALIASES.get(name, ()))[0]


def resolve_model_key(settings, secret_store):
    """Resolve the model key for the selected provider only.

    Only the active provider's dedicated slot and documented alias are checked.
    Returns ``(value, source, used_name)``.
    """
    provider = settings.model.provider
    slot = MODEL_PROVIDER_SLOTS[provider]
    value, source = resolve_secret(secret_store, slot, MODEL_PROVIDER_ALIASES.get(provider, ()))
    if value:
        return value, source, slot
    return None, None, slot


def model_env_names(provider):
    """The exact environment variable names honored for a provider's key."""
    names = ["PORTFOLIO_SECRET_" + MODEL_PROVIDER_SLOTS[provider].upper()]
    names.extend(MODEL_PROVIDER_ALIASES.get(provider, ()))
    return list(dict.fromkeys(names))


def secret_env_names(name, aliases=()):
    return list(dict.fromkeys(["PORTFOLIO_SECRET_" + name.upper(), *aliases]))


def winning_env_name(name, aliases=()):
    """The deployment variable that currently supplies a secret, if any."""
    key = "PORTFOLIO_SECRET_" + name.upper()
    if key in os.environ:
        return key
    if key + "_FILE" in os.environ:
        return key + "_FILE"
    for alias in aliases:
        if os.environ.get(alias):
            return alias
    return None


def describe_secret(store, name, resolved, *, provider, saved_at_name, env_names, aliases=(), server=False):
    value, source = resolved
    return {"present": bool(value), "source": source,
            "saved_at": store.saved_at(saved_at_name) if (source == "keyring" and saved_at_name) else None,
            "provider": provider, "editable": (not server) and source not in ("env", "file"),
            "managed_by": winning_env_name(name, aliases) if source in ("env", "file", "alias") else None,
            "env": list(env_names)}


def secret_status(settings, store, server=False):
    """Structured status for every credential the dashboard can manage."""
    secrets = {}
    for provider, slot in MODEL_PROVIDER_SLOTS.items():
        aliases = MODEL_PROVIDER_ALIASES.get(provider, ())
        resolved = resolve_secret(store, slot, aliases)
        secrets[slot] = describe_secret(store, slot, resolved, provider=provider, saved_at_name=slot,
                                        env_names=model_env_names(provider), aliases=aliases, server=server)
    value, source, used = resolve_model_key(settings, store)
    active_aliases = MODEL_PROVIDER_ALIASES.get(settings.model.provider, ())
    secrets[ACTIVE_MODEL_KEY] = describe_secret(store, used, (value, source), provider=settings.model.provider,
                                                 saved_at_name=used, env_names=model_env_names(settings.model.provider),
                                                 aliases=active_aliases, server=server)
    for name in ("tavily_api_key", "smtp_password"):
        aliases = SECRET_ALIASES.get(name, ())
        resolved = resolve_secret(store, name, aliases)
        secrets[name] = describe_secret(store, name, resolved, provider=None, saved_at_name=name,
                                        env_names=secret_env_names(name, aliases), aliases=aliases, server=server)
    return secrets


def create_model(settings, secret_store, api_key=None):
    key = api_key
    if key is None:
        key, _source, _used = resolve_model_key(settings, secret_store)
    if not key:
        raise ValueError("Add a model API key or configure its environment variable")
    cfg = settings.model
    selected_model = cfg.model
    base = cfg.base_url
    if cfg.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=selected_model, api_key=key, base_url=base, temperature=cfg.temperature, max_tokens=cfg.max_tokens, timeout=cfg.request_timeout_seconds, max_retries=0)
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=selected_model, api_key=key, base_url=base, temperature=cfg.temperature, max_tokens=cfg.max_tokens, timeout=cfg.request_timeout_seconds, max_retries=0)


def build_research(settings, secret_store, tavily_key=None):
    fallback = None
    if settings.search_provider == "tavily":
        key = read_secret(secret_store, "tavily_api_key") if tavily_key is None else tavily_key
        if key:
            fallback = TavilyResearchProvider(key)
    elif settings.search_provider == "keyless":
        fallback = DDGSResearchProvider()
    providers = []
    if settings.search_provider != "disabled":
        sec_user_agent = settings.sec_contact or None
        if settings.primary_feeds_enabled:
            providers.append(PrimaryFeedResearchProvider(fallback=fallback, sec_user_agent=sec_user_agent))
        elif fallback is not None:
            providers.append(fallback)
        if settings.sec_company_filings_enabled:
            providers.append(SecFilingsResearchProvider(sec_user_agent=sec_user_agent))
        if settings.ticker_news_enabled:
            providers.append(TickerNewsFeedProvider(sources=settings.news_feed_sources))
    for provider in providers:
        # Contract §5 freshness window: items older than max_data_age_hours drop when a newer
        # item for the same symbol exists (30-day hard rule always applies).
        provider.max_data_age_hours = settings.max_data_age_hours
    return CompositeResearchProvider(providers) if providers else None


def build_dependencies(settings, secret_store, model_key=None, tavily_key=None):
    research = build_research(settings, secret_store, tavily_key=tavily_key)
    model = create_model(settings, secret_store, api_key=model_key) if settings.model_enabled else None
    return EngineDependencies(research=research, market_data=YFinanceMarketDataProvider() if settings.auto_prices_enabled or settings.market_data_enabled else None,
                              history_enabled=settings.market_data_enabled,
                              instrument_resolver=YFinanceMarketDataProvider(refresh_existing=False), model=model)


# --- Model readiness probe (FR-15, contract §8) -----------------------------
# A tool/result round trip and the real briefing schema; no portfolio data or key material in prompts.

READINESS_TOOL_PROMPT = "Call the record_probe_note tool with the note ready."
SECTION_TITLE_LIMIT = 20


def probe_tool():
    """A trivial tool whose only purpose is to see whether the model calls tools."""
    from langchain_core.tools import StructuredTool

    def record_probe_note(note: str) -> str:
        """Record one probe note. Call this tool with the note 'ready'."""
        return note

    return StructuredTool.from_function(record_probe_note)


def readiness_ok(tool_calling, structured_output):
    """Pass only after the research call and structured finalization both work."""
    return tool_calling == structured_output == "ok"


def readiness_detail(tool_calling, structured_output, tool_reason="", structured_reason=""):
    """One short, plain-language sentence a normal user can act on; never key material."""
    if tool_calling == "fail" and structured_output == "fail":
        return (f"The model answered neither readiness probe — {tool_reason or structured_reason or 'the model did not answer'}. "
                "Check the model endpoint, API key, and model name before running research.")
    if tool_calling == "fail":
        return (f"The tool-calling check could not complete — {tool_reason or 'the model did not answer'}. "
                "Research runs need tool calls to gather evidence; check the model endpoint, API key, and model name.")
    if structured_output == "fail":
        return (f"The structured-output check failed — {structured_reason or 'the model did not answer'}. "
                "Written briefings need JSON output; prefer a model advertised with JSON output.")
    if tool_calling == "weak" and structured_output == "weak":
        return ("This model is weak at both tool calling and structured output — reports may stay thin and spend calls fixing formatting. "
                "A model advertised with tool calling and JSON output is recommended for research runs.")
    if tool_calling == "weak":
        return "The model did not make the requested tool call. Check tool support for the model and gateway."
    if structured_output == "weak":
        return "The model called the briefing tool but omitted its summary. Choose a model that completes structured tool responses."
    return "The model completed a tool-result round trip and returned the briefing schema. Longer research runs still depend on source access and the time budget."


async def _probe_tool_calling(model):
    try:
        bound = model.bind_tools([probe_tool()], tool_choice="any")
        response = await bound.ainvoke(READINESS_TOOL_PROMPT)
    except Exception as exc:
        return "fail", safe_error_reason(exc), []
    calls = getattr(response, "tool_calls", None) or []
    if len(calls) != 1 or calls[0].get("name") != "record_probe_note" or calls[0].get("args") != {"note": "ready"}:
        return "weak", "", []
    from langchain_core.messages import HumanMessage, ToolMessage
    return "ok", "", [HumanMessage(content=READINESS_TOOL_PROMPT), response,
                         ToolMessage(content="ready", tool_call_id=calls[0]["id"], name="record_probe_note")]


async def _probe_structured_output(model, history):
    from .models import ResearchNarrative
    from langchain_core.messages import HumanMessage
    try:
        # Exercise the same nested output schema, required tool choice and tool-result
        # conversation as the actual agent, rather than an unrelated JSON text reply.
        bound = model.bind_tools([ResearchNarrative], tool_choice="any")
        response = await bound.ainvoke([*history, HumanMessage(content=
            'Finalize with the ResearchNarrative tool. Set executive_summary to "ready", '
            'sections to [], and suggestions to []. No portfolio research is needed.')])
        calls = getattr(response, "tool_calls", None) or []
        if len(calls) != 1 or calls[0].get("name") != "ResearchNarrative":
            return "fail", "the model did not call the required briefing output tool"
        narrative = ResearchNarrative.model_validate(calls[0]["args"])
        if not narrative.executive_summary:
            return "weak", ""
    except Exception as exc:
        return "fail", safe_error_reason(exc)
    return "ok", ""


async def probe_model_readiness(settings, secret_store, model=None, api_key=None):
    """Probe a research tool/result round trip and the actual briefing schema, without portfolio data."""
    if model is None:
        model = create_model(settings, secret_store, api_key=api_key)
    tool_calling, tool_reason, history = await _probe_tool_calling(model)
    structured_output, structured_reason = await _probe_structured_output(model, history)
    return {"ok": readiness_ok(tool_calling, structured_output),
            "tool_calling": tool_calling, "structured_output": structured_output,
            "detail": readiness_detail(tool_calling, structured_output, tool_reason, structured_reason)}


def run_outcome_state(result_json):
    """Honest per-run outcome fields for one history entry (FR-10/FR-13).

    Additive keys only: ``narrative_state`` (the stored ``AnalysisResult.coverage``
    value the dashboard badge vocabulary keys off), ``finding_titles`` (capped at
    ``SECTION_TITLE_LIMIT`` so history payloads stay small, for "what's new since
    your last briefing" diffs), and ``analysis_written``, the machine-readable
    flag that is False whenever the stored result says the analyst wrote nothing
    (``narrative_state == "not_written"`` per contract §2). Missing or unreadable
    results read as no written analysis.
    """
    try:
        result = json.loads(result_json) if result_json else None
    except (TypeError, ValueError):
        result = None
    if not isinstance(result, dict):
        return {"narrative_state": None, "section_titles": [], "analysis_written": False, "question": ""}
    state = (result.get("coverage") or {}).get("narrative_state")
    budget = result.get("research_budget") or {}
    titles = [str(section.get("heading") or "") for section in result.get("sections") or []]
    question = str((result.get("metrics") or {}).get("question")
                   or (result.get("request") or {}).get("question") or "").strip()
    return {"narrative_state": state, "section_titles": titles[:SECTION_TITLE_LIMIT],
            "analysis_written": state != "not_written" and budget.get("analysis_written") is not False,
            "question": question[:500]}


class QuoteService:
    """Automatic server-side valuation refresh for the saved portfolio.

    Prices are fetched in a batch, written onto the saved snapshot's price fields
    only (never quantity, cash, or as_of), and recorded in a persisted
    ``prices_as_of`` map so freshness survives a restart. A short TTL keeps
    polling responsive while US equities trade and relaxed otherwise.
    """

    KEY = "prices_as_of"
    market_hours_ttl = 60

    def __init__(self, store, provider, settings_provider):
        self.store = store
        self.provider = provider
        self.settings_provider = settings_provider
        self._cache = None
        self._lock = asyncio.Lock()

    def metadata(self) -> dict:
        if self._cache is None:
            self._cache = self.store.get(self.KEY, {}) or {}
        return self._cache

    @staticmethod
    def _market_open(now: datetime) -> bool:
        local = now.astimezone(ZoneInfo("America/New_York"))
        if local.weekday() >= 5:
            return False
        minutes = local.hour * 60 + local.minute
        return 9 * 60 + 30 <= minutes < 16 * 60

    def ttl_seconds(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        if self._market_open(now):
            return self.market_hours_ttl
        return max(1, int(self.settings_provider().price_refresh_minutes)) * 60

    def as_of(self) -> str | None:
        stamps = [entry.get("quote_as_of") for entry in self.metadata().values() if entry.get("quote_as_of")]
        return max(stamps) if stamps else None

    def is_fresh(self, now: datetime | None = None) -> bool:
        stamps = [entry.get('retrieved_at') for entry in self.metadata().values() if entry.get('retrieved_at')]
        as_of = max(stamps) if stamps else None
        if not as_of:
            return False
        try:
            retrieved = datetime.fromisoformat(as_of)
        except ValueError:
            return False
        if retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return (now - retrieved).total_seconds() < self.ttl_seconds(now)

    async def refresh(self, force: bool = False, now: datetime | None = None) -> dict:
        """Refresh stored prices when enabled and stale; never raises to callers."""
        settings = self.settings_provider()
        if not force and not settings.auto_prices_enabled:
            return self.metadata()
        snapshot = self.store.snapshot()
        symbols = list(dict.fromkeys(position.symbol for position in snapshot.positions)) if snapshot else []
        if not symbols:
            return self.metadata()
        now = now or datetime.now(timezone.utc)
        if not force and self.is_fresh(now):
            return self.metadata()
        async with self._lock:
            if not force and self.is_fresh(now):
                return self.metadata()
            try:
                quotes = await self.provider.quotes(symbols)
            except Exception:
                return self.metadata()
            if not quotes:
                return self.metadata()
            from .providers import valid_market_price
            quotes = {symbol: {**quote, "price": valid_market_price(quote.get("price")),
                               "previous_close": valid_market_price(quote.get("previous_close"))}
                      for symbol, quote in quotes.items() if symbol in symbols
                      and valid_market_price(quote.get("price")) is not None}
            if not quotes:
                return self.metadata()
            from .providers import quote_is_newer
            current = self.store.snapshot()
            if current is not None:
                positions = {position.symbol:position for position in current.positions}
                quotes = {symbol:quote for symbol,quote in quotes.items()
                          if symbol in positions and quote_is_newer(quote,positions[symbol])}
            held = set(symbols)
            metadata = {symbol: entry for symbol, entry in self.metadata().items() if symbol in held}
            for symbol, quote in quotes.items():
                metadata[symbol] = {"trading_date": quote.get("trading_date"),
                                    "retrieved_at": quote.get("retrieved_at"),
                                    "source": quote.get("source", "yfinance"), "quote_as_of":quote.get("quote_as_of"),
                                    "valuation_type":quote.get("valuation_type")}
            current = self.store.snapshot()
            if current is not None:
                for position in current.positions:
                    quote = quotes.get(position.symbol)
                    if quote and quote.get("price") is not None:
                        position.price = Decimal(str(quote["price"]))
                        position.previous_close = quote.get("previous_close")
                        from datetime import date
                        position.price_session = date.fromisoformat(quote['trading_date']) if quote.get('trading_date') else None
                        position.price_as_of = datetime.fromisoformat(quote['quote_as_of']) if quote.get('quote_as_of') else None
                        position.price_source = quote.get('source')
                self.store.save_snapshot(current)
            self.store.set(self.KEY, metadata)
            self._cache = metadata
            return metadata


class ApplicationService:
    def __init__(self, directory: Path, server=False):
        self.directory = directory
        self.store = Store(directory)
        self.secrets = SecretStore(directory, server=server)
        self.quotes = QuoteService(self.store, YFinanceMarketDataProvider(), lambda: self.settings)
        self.tasks = {}
        self.stopping = False

    @property
    def settings(self):
        return load_settings(self.directory)

    def history(self, limit=30):
        """Run history for the dashboard: newest first, plus per-run outcome state.

        The outcome keys are additive. The one value guard protects contract §2:
        a run whose stored result says the analyst wrote nothing never exposes
        ``status: "success"`` (it reads ``"partial"`` instead), so an unwritten
        briefing cannot look like a clean success.
        """
        entries = self.store.history(limit=limit)
        for entry in entries:
            row = self.store.run(entry["id"]) or {}
            entry.update(run_outcome_state(row.get("result")))
            if not entry["analysis_written"] and entry.get("status") == "success":
                entry["status"] = "partial"
        return entries

    def request_for(self, snapshot, **overrides):
        settings = self.settings
        defaults = {name: getattr(settings, name) for name in ("investment_profile", "benchmark", "max_searches", "max_model_calls", "max_tool_calls", "max_seconds", "max_data_age_hours", "position_review_threshold_pct", "sector_review_threshold_pct", "report_length")}
        defaults.update(overrides)
        # Owner budgets are hard ceilings for dashboard/API callers.
        defaults["max_searches"] = min(defaults["max_searches"], settings.max_searches)
        defaults["max_seconds"] = min(defaults["max_seconds"], settings.max_seconds)
        defaults["max_model_calls"] = min(defaults["max_model_calls"], settings.max_model_calls)
        defaults["max_tool_calls"] = min(defaults["max_tool_calls"], settings.max_tool_calls)
        defaults["max_data_age_hours"] = min(defaults["max_data_age_hours"], settings.max_data_age_hours)
        return AnalysisRequest(snapshot=snapshot, **defaults)

    def submit(self, request=None, source="stored", send_email=False, schedule_key=None, overrides=None):
        settings = self.settings
        if send_email and (not settings.smtp or self.store.get("verified_recipient") != settings.smtp.recipient):
            raise ValueError("Configure email and verify its recipient before sending")
        run_id = str(uuid4())
        if not self.store.create_run(run_id, schedule_key=schedule_key, email_requested=send_email):
            raise ValueError("An analysis is already running, or this market session has already been processed")
        task = asyncio.create_task(self.execute(run_id, request, source, send_email, overrides or {}))
        self.tasks[run_id] = task
        def completed(done):
            self.tasks.pop(run_id, None)
            if done.cancelled():
                self.store.update_run(run_id, status="failed", error="Run cancelled; no report was emailed.")
        task.add_done_callback(completed)
        return run_id

    async def execute(self, run_id, request, source, send_email, overrides):
        self.store.update_run(run_id, status="running")
        try:
            settings = self.settings
            if request is None:
                if source == "demo":
                    from .demo import demo_snapshot
                    snapshot = demo_snapshot()
                else:
                    snapshot = self.store.snapshot()
                    if snapshot is None:
                        raise ValueError("Save holdings manually, import a CSV or Excel file, or select the demonstration portfolio")
                request = self.request_for(snapshot, **overrides)
            else:
                request = self.request_for(**request.model_dump(exclude={"snapshot"}), snapshot=request.snapshot)
            startup_warning = None
            try:
                dependencies = EngineDependencies() if source == "demo" else build_dependencies(settings, self.secrets)
            except Exception:
                safe_settings = settings.model_copy(update={"model_enabled": False})
                dependencies = build_dependencies(safe_settings, self.secrets)
                startup_warning = "Model connection could not be initialized; review model settings. Metrics and available research are shown."
            dependencies.activity_sink = lambda event, budget: self.store.append_activity(run_id, event, budget)
            result = await analyze_portfolio(request, dependencies)
            result.run_id = run_id
            if source == "stored":
                result.warnings.insert(0, f"Positions use your latest saved portfolio, dated {request.snapshot.as_of.isoformat()}. Holdings are not automatically synchronized; upload or edit your portfolio when positions change.")
            if self.store.run(run_id)["schedule_key"] and request.reporting_cutoff and (result.created_at - request.reporting_cutoff).total_seconds() > 1800:
                result.warnings.insert(0, "Delayed scheduled report: this is the latest missed market session, generated after the scheduled time. Holdings reflect their labeled snapshot date.")
            if startup_warning:
                result.warnings.append(startup_warning)
                if result.status == "success":
                    result.status = "partial"
            self.store.finish(result)
            if send_email and result.status != "failed":
                await self.deliver(run_id)
        except asyncio.CancelledError:
            self.store.update_run(run_id, status="failed", error="Run cancelled; no report was emailed.")
            raise
        except Exception as exc:
            # Do not persist provider errors that might include URLs or credentials.
            message = str(exc) if isinstance(exc, ValueError) else "Run failed. Check connections and try again."
            self.store.update_run(run_id, status="failed", error=message[:250])

    def cancel(self, run_id):
        task = self.tasks.get(run_id)
        if task:
            task.cancel()
            self.store.update_run(run_id, status="failed", error="Run cancelled; no report was emailed.")
            return True
        return False

    async def deliver(self, run_id):
        row = self.store.run(run_id)
        settings = self.settings
        if not row or not row["result"] or row["status"] == "failed":
            raise ValueError("A completed report is required")
        if row["delivery"] in {"sent", "sending", "uncertain"}:
            raise ValueError("Delivery already attempted; check its status before sending another copy")
        if not settings.smtp:
            raise ValueError("Email is not configured")
        verified = self.store.get("verified_recipient")
        if verified != settings.smtp.recipient:
            raise ValueError("Verify the recipient first")
        password = read_secret(self.secrets, "smtp_password") or ""
        if not self.store.claim_delivery(run_id):
            raise ValueError("Delivery already attempted; check its status before sending another copy")
        try:
            outcome = await asyncio.to_thread(send_report, AnalysisResult.model_validate_json(row["result"]), settings.smtp, password, verified,
                                             settings.schedule.timezone)
        except BaseException:
            self.store.update_run(run_id, delivery="uncertain")
            raise
        self.store.update_run(run_id, delivery=outcome.status)
        return outcome

    async def begin_verification(self):
        config = self.settings.smtp
        if not config:
            raise ValueError("Save email settings first")
        previous = self.store.get("verification", {})
        now = datetime.now(timezone.utc)
        if previous and now.timestamp() - previous.get("sent_at", 0) < 60:
            raise ValueError("Wait a minute before requesting another code")
        code = str(random_secrets.randbelow(1_000_000)).zfill(6)
        self.store.set("verification", {"recipient": config.recipient, "hash": hashlib.sha256(code.encode()).hexdigest(), "expires": (now + timedelta(minutes=10)).timestamp(), "attempts": 0, "sent_at": now.timestamp()})
        outcome = await asyncio.to_thread(send_verification, config, read_secret(self.secrets, "smtp_password") or "", code)
        if outcome.status != "sent":
            self.store.set("verification", {})
        return outcome

    def verify(self, code):
        state = self.store.get("verification", {})
        state["attempts"] = state.get("attempts", 0) + 1
        self.store.set("verification", state)
        if state.get("attempts", 6) > 5 or state.get("expires", 0) < datetime.now(timezone.utc).timestamp() or not random_secrets.compare_digest(state.get("hash", ""), hashlib.sha256(code.encode()).hexdigest()):
            raise ValueError("Invalid or expired code. Request a new one after checking the address.")
        if not self.settings.smtp or state["recipient"] != self.settings.smtp.recipient:
            raise ValueError("Recipient settings changed; request a new code")
        self.store.set("verified_recipient", state["recipient"])
        self.store.set("verification", {})

    async def scheduler_tick(self, now=None):
        settings = self.settings
        if not settings.schedule.enabled:
            return
        now = now or datetime.now(timezone.utc)
        eligible = await asyncio.to_thread(latest_eligible_run, now, settings.schedule.delay_minutes, settings.schedule.timezone)
        enabled_at = self.store.get("schedule_enabled_at")
        if not enabled_at or eligible.timestamp() < enabled_at:
            return
        key = eligible.date().isoformat()
        if self.store.has_schedule(key):
            return
        try:
            self.submit(source="stored", send_email=settings.scheduled_email, schedule_key=key,
                        overrides={"question": f"Explain the market session associated with the scheduled briefing at {eligible.isoformat()}. Separate later developments from session drivers.", "include_suggestions": False, "reporting_cutoff": eligible})
        except ValueError:
            pass

    async def refresh_quotes(self, force: bool = False):
        """Best-effort quote refresh; never lets market-data failures reach callers."""
        try:
            return await self.quotes.refresh(force=force)
        except Exception:
            return self.quotes.metadata()

    def quote_payload(self) -> dict:
        """Shape automatic quotes for the saved portfolio (missing symbols omitted)."""
        metadata = self.quotes.metadata()
        snapshot = self.store.snapshot()
        positions = {position.symbol: position for position in snapshot.positions} if snapshot else {}
        prices = {}
        for symbol, entry in metadata.items():
            position = positions.get(symbol)
            if position is None:
                continue
            prices[symbol] = {"price": str(position.price) if position.price is not None else None,
                              "previous_close": str(position.previous_close) if position.previous_close is not None else None,
                              "trading_date": entry.get("trading_date"), "retrieved_at": entry.get("retrieved_at"),
                              "source": entry.get("source"), "price_as_of":entry.get("quote_as_of"),
                              "price_session":entry.get("trading_date"), "price_source":entry.get("source")}
        return {"prices": prices, "as_of": self.quotes.as_of(), "refresh_seconds": self.quotes.ttl_seconds()}

    async def scheduler_loop(self):
        while not self.stopping:
            try:
                await self.scheduler_tick()
                await self.refresh_quotes()
                self.store.prune(self.settings.retention_days)
            except Exception:
                self.store.set("scheduler_error", "Scheduled run could not start; check holdings, email verification, and connections.")
            await asyncio.sleep(20)

    async def close(self):
        self.stopping = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
