"""Application orchestration around the independently reusable research engine."""
import asyncio
import hashlib
import os
import secrets as random_secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .config import load_settings
from .delivery import send_report, send_verification
from .engine import EngineDependencies, analyze_portfolio
from .models import AnalysisRequest, AnalysisResult
from .providers import DDGSResearchProvider, PrimaryFeedResearchProvider, TavilyResearchProvider, YFinanceMarketDataProvider
from .scheduling import latest_eligible_run
from .secrets import SecretStore, SecretStorageError
from .storage import Store


def read_secret(store, name):
    aliases = {"model_api_key": ("LITELLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"), "tavily_api_key": ("TAVILY_API_KEY",), "alpha_vantage_api_key": ("ALPHA_VANTAGE_API_KEY",)}
    for alias in aliases.get(name, ()):
        if os.environ.get(alias):
            return os.environ[alias]
    try:
        return store.get(name)
    except SecretStorageError:
        return None


def create_model(settings, secret_store):
    preferred = {"colgate": "LITELLM_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(settings.model.provider)
    try:
        stored = secret_store.get("model_api_key")
    except SecretStorageError:
        stored = None
    key = stored or (os.environ.get(preferred) if preferred else None)
    if not key:
        raise ValueError("Add a model API key or configure its environment variable")
    cfg = settings.model
    if cfg.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=cfg.model, api_key=key, temperature=cfg.temperature, max_tokens=cfg.max_tokens, timeout=30, max_retries=1)
    from langchain_openai import ChatOpenAI
    base = {"colgate": "https://litellm.colgate.edu", "openai": "https://api.openai.com/v1"}.get(cfg.provider, cfg.base_url)
    return ChatOpenAI(model=cfg.model, api_key=key, base_url=base, temperature=cfg.temperature, max_tokens=cfg.max_tokens, timeout=30, max_retries=1)


class SupplementalResearch:
    def __init__(self, primary, supplemental):
        self.primary, self.supplemental = primary, supplemental

    async def search(self, query, max_results=3):
        results = []
        for provider in (self.primary, self.supplemental):
            if provider:
                try:
                    results.extend(await provider.search(query, max_results=max_results))
                except Exception:
                    if not results and provider is self.supplemental:
                        raise
        return list({source.url: source for source in results}.values())[:max_results]

    async def retrieve(self, source):
        return await self.primary.retrieve(source)


def build_dependencies(settings, secret_store):
    research = None
    if settings.search_provider == "tavily":
        key = read_secret(secret_store, "tavily_api_key")
        if key:
            research = TavilyResearchProvider(key)
    elif settings.search_provider == "keyless":
        research = DDGSResearchProvider()
    if settings.primary_feeds_enabled and settings.search_provider != "disabled":
        research = PrimaryFeedResearchProvider(fallback=research, sec_user_agent=settings.sec_contact or None)
    if settings.alpha_vantage_enabled:
        from .providers import AlphaVantageResearchProvider
        key = read_secret(secret_store, "alpha_vantage_api_key")
        if key:
            research = SupplementalResearch(research or PrimaryFeedResearchProvider(), AlphaVantageResearchProvider(key))
    model = create_model(settings, secret_store) if settings.model_enabled else None
    return EngineDependencies(research=research, market_data=YFinanceMarketDataProvider() if settings.market_data_enabled else None, model=model)


class ApplicationService:
    def __init__(self, directory: Path, server=False):
        self.directory = directory
        self.store = Store(directory)
        self.secrets = SecretStore(directory, server=server)
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
            outcome = await asyncio.to_thread(send_report, AnalysisResult.model_validate_json(row["result"]), settings.smtp, password, verified)
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

    async def scheduler_loop(self):
        while not self.stopping:
            try:
                await self.scheduler_tick()
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
