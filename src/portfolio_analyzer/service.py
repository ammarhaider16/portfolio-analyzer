"""Application orchestration around the independently reusable research engine."""
import asyncio
import hashlib
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
    "colgate": "model_api_key_colgate",
    "openai": "model_api_key_openai",
    "anthropic": "model_api_key_anthropic",
    "custom": "model_api_key_custom",
}
# Documented deployment aliases accepted for each provider.
MODEL_PROVIDER_ALIASES = {
    "colgate": ("LITELLM_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "custom": (),
}
SECRET_ALIASES = {
    "tavily_api_key": ("TAVILY_API_KEY",),
    "alpha_vantage_api_key": ("ALPHA_VANTAGE_API_KEY",),
}
# The shared pre-Phase-3 name, kept readable only to migrate existing installs.
LEGACY_MODEL_SLOT = "model_api_key"


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
    if name == LEGACY_MODEL_SLOT:
        # Diagnostic helper (CLI): report where any model credential lives.
        for provider, slot in MODEL_PROVIDER_SLOTS.items():
            value, _ = resolve_secret(store, slot, MODEL_PROVIDER_ALIASES.get(provider, ()))
            if value:
                return value
        return resolve_secret(store, LEGACY_MODEL_SLOT, ("LITELLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"))[0]
    return resolve_secret(store, name, SECRET_ALIASES.get(name, ()))[0]


def resolve_model_key(settings, secret_store):
    """Resolve the model key for the selected provider only.

    The active provider's dedicated slot (and its documented alias) is checked
    first, then the legacy shared ``model_api_key`` for migration. Switching
    provider therefore never silently reuses another provider's stored key.
    Returns ``(value, source, used_name)``.
    """
    provider = settings.model.provider
    slot = MODEL_PROVIDER_SLOTS[provider]
    value, source = resolve_secret(secret_store, slot, MODEL_PROVIDER_ALIASES.get(provider, ()))
    if value:
        return value, source, slot
    value, source = resolve_secret(secret_store, LEGACY_MODEL_SLOT)
    if value:
        return value, source, LEGACY_MODEL_SLOT
    return None, None, slot


def model_env_names(provider):
    """The exact environment variable names honored for a provider's key."""
    names = ["PORTFOLIO_SECRET_" + MODEL_PROVIDER_SLOTS[provider].upper()]
    names.extend(MODEL_PROVIDER_ALIASES.get(provider, ()))
    names.append("PORTFOLIO_SECRET_MODEL_API_KEY")
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
    active_aliases = MODEL_PROVIDER_ALIASES.get(settings.model.provider, ()) if used != LEGACY_MODEL_SLOT else ()
    secrets[LEGACY_MODEL_SLOT] = describe_secret(store, used, (value, source), provider=settings.model.provider,
                                                 saved_at_name=used, env_names=model_env_names(settings.model.provider),
                                                 aliases=active_aliases, server=server)
    for name in ("tavily_api_key", "alpha_vantage_api_key", "smtp_password"):
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
    # Honor the endpoint the owner configured; fall back to the provider default when blank.
    provider_defaults = {"colgate": "https://litellm.colgate.edu",
                         "openai": "https://api.openai.com/v1",
                         "anthropic": "https://api.anthropic.com"}
    base = (cfg.base_url or provider_defaults.get(cfg.provider, "")).rstrip("/")
    if cfg.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=cfg.model, api_key=key, temperature=cfg.temperature, max_tokens=cfg.max_tokens, timeout=30, max_retries=1)
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=cfg.model, api_key=key, base_url=base, temperature=cfg.temperature, max_tokens=cfg.max_tokens, timeout=30, max_retries=1)


def build_research(settings, secret_store, tavily_key=None, alpha_key=None):
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
        if settings.alpha_vantage_enabled:
            from .providers import AlphaVantageResearchProvider
            key = read_secret(secret_store, "alpha_vantage_api_key") if alpha_key is None else alpha_key
            if key:
                providers.append(AlphaVantageResearchProvider(key))
    return CompositeResearchProvider(providers) if providers else None


def build_dependencies(settings, secret_store, model_key=None, tavily_key=None, alpha_key=None):
    research = build_research(settings, secret_store, tavily_key=tavily_key, alpha_key=alpha_key)
    model = create_model(settings, secret_store, api_key=model_key) if settings.model_enabled else None
    return EngineDependencies(research=research, market_data=YFinanceMarketDataProvider() if settings.market_data_enabled else None,
                              instrument_resolver=YFinanceMarketDataProvider(refresh_existing=False), model=model)


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
        stamps = [entry.get("retrieved_at") for entry in self.metadata().values() if entry.get("retrieved_at")]
        return max(stamps) if stamps else None

    def is_fresh(self, now: datetime | None = None) -> bool:
        as_of = self.as_of()
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
            held = set(symbols)
            metadata = {symbol: entry for symbol, entry in self.metadata().items() if symbol in held}
            for symbol, quote in quotes.items():
                metadata[symbol] = {"trading_date": quote.get("trading_date"),
                                    "retrieved_at": quote.get("retrieved_at"),
                                    "source": quote.get("source", "yfinance")}
            current = self.store.snapshot()
            if current is not None:
                for position in current.positions:
                    quote = quotes.get(position.symbol)
                    if quote and quote.get("price") is not None:
                        position.price = Decimal(str(quote["price"]))
                        if quote.get("previous_close") is not None:
                            position.previous_close = Decimal(str(quote["previous_close"]))
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

    def request_for(self, snapshot, **overrides):
        settings = self.settings
        defaults = {name: getattr(settings, name) for name in ("investment_profile", "benchmark", "max_searches", "max_seconds", "max_data_age_hours", "position_review_threshold_pct", "sector_review_threshold_pct", "report_length")}
        defaults.update(overrides)
        # Owner budgets are hard ceilings for dashboard/API callers.
        defaults["max_searches"] = min(defaults["max_searches"], settings.max_searches)
        defaults["max_seconds"] = min(defaults["max_seconds"], settings.max_seconds)
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
                        raise ValueError("Save holdings manually, import a CSV or screenshot, or select the demonstration portfolio")
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
                              "source": entry.get("source")}
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
