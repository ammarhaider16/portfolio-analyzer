"""Application integration checks use real SQLite/configuration and no network."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from portfolio_analyzer import service as module
from portfolio_analyzer.config import Settings, load_settings, save_settings
from portfolio_analyzer.delivery import DeliveryOutcome, SMTPConfig
from portfolio_analyzer.demo import demo_snapshot
from portfolio_analyzer.engine import EngineDependencies
from portfolio_analyzer.models import AnalysisResult, PortfolioSnapshot, Position
from portfolio_analyzer.scheduling import SchedulePolicy
from portfolio_analyzer.service import ApplicationService
from portfolio_analyzer.storage import Store


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "build_dependencies", lambda *_: EngineDependencies())
    monkeypatch.setattr(module, "read_secret", lambda *_: None)
    return ApplicationService(tmp_path, server=True)


def configure(app, **updates):
    settings = Settings(search_provider="disabled", primary_feeds_enabled=False, **updates)
    save_settings(app.directory, settings)
    return settings


def smtp(recipient="owner@example.org"):
    return SMTPConfig(host="smtp.example.org", sender="owner@example.org", recipient=recipient)


async def completed(app, **kwargs):
    run_id = app.submit(**kwargs)
    await app.tasks[run_id]
    return app.store.run(run_id)


async def test_dashboard_runs_without_email_or_schedule(app):
    configure(app)
    row = await completed(app, source="demo")
    assert row["status"] in {"success", "partial"}
    assert row["result"] and row["delivery"] is None
    assert app.settings.smtp is None and not app.settings.schedule.enabled
    await app.scheduler_tick()
    assert len(app.store.history()) == 1


@pytest.mark.parametrize("schedule_enabled,email_enabled", [(False, False), (False, True), (True, False), (True, True)])
async def test_email_and_schedule_are_independent(app, monkeypatch, schedule_enabled, email_enabled):
    configure(app, smtp=smtp() if email_enabled else None,
              schedule=SchedulePolicy(enabled=schedule_enabled), scheduled_email=email_enabled)
    app.store.save_snapshot(demo_snapshot())
    app.store.set("verified_recipient", "owner@example.org" if email_enabled else None)
    eligible = datetime(2026, 10, 16, 20, 30, tzinfo=timezone.utc)
    app.store.set("schedule_enabled_at", (eligible - timedelta(days=1)).timestamp())
    monkeypatch.setattr(module, "latest_eligible_run", lambda *_: eligible)
    calls = []

    def send(*args):
        calls.append(args)
        return DeliveryOutcome(status="sent", message="accepted")

    monkeypatch.setattr(module, "send_report", send)
    if schedule_enabled:
        await app.scheduler_tick(eligible + timedelta(hours=1))
        await asyncio.gather(*list(app.tasks.values()))
    else:
        await app.scheduler_tick(eligible)
        assert app.store.history() == []
        await completed(app, source="stored", send_email=email_enabled)
    assert len(app.store.history()) == 1
    row = app.store.run(app.store.history()[0]["id"])
    assert row["result"]
    assert len(calls) == int(email_enabled)
    assert row["delivery"] == ("sent" if email_enabled else None)


async def test_owner_budgets_cap_explicit_requests(app, monkeypatch):
    configure(app, max_searches=2, max_seconds=5, max_data_age_hours=12)
    captured = []

    async def analyze(request, _dependencies):
        captured.append(request)
        return AnalysisResult(snapshot_as_of=request.snapshot.as_of)

    monkeypatch.setattr(module, "analyze_portfolio", analyze)
    from portfolio_analyzer.models import AnalysisRequest
    request = AnalysisRequest(snapshot=demo_snapshot(), max_searches=30, max_seconds=600, max_data_age_hours=8760)
    row = await completed(app, request=request)
    assert row["status"] == "success"
    assert (captured[0].max_searches, captured[0].max_seconds, captured[0].max_data_age_hours) == (2, 5, 12)


async def test_cancellation_releases_overlap_lock_and_prevents_email(app, monkeypatch):
    configure(app)
    started = asyncio.Event()

    async def analyze(*_):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "analyze_portfolio", analyze)
    run_id = app.submit(source="demo")
    await started.wait()
    with pytest.raises(ValueError, match="already running"):
        app.submit(source="demo")
    task = app.tasks[run_id]
    assert app.cancel(run_id)
    await asyncio.gather(task, return_exceptions=True)
    row = app.store.run(run_id)
    assert row["status"] == "failed" and row["delivery"] is None
    assert app.store.create_run("after-cancel")


async def test_immediate_cancellation_does_not_leave_queued_run(app):
    configure(app)
    run_id = app.submit(source="demo")
    task = app.tasks[run_id]
    assert app.cancel(run_id)
    await asyncio.gather(task, return_exceptions=True)
    assert app.store.run(run_id)["status"] == "failed"


async def test_schedule_recovers_latest_only_and_deduplicates(app, monkeypatch):
    configure(app, schedule=SchedulePolicy(enabled=True))
    app.store.save_snapshot(demo_snapshot())
    eligible = datetime(2026, 10, 16, 20, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "latest_eligible_run", lambda *_: eligible)
    app.store.set("schedule_enabled_at", (eligible - timedelta(days=30)).timestamp())
    now = eligible + timedelta(days=2)
    await app.scheduler_tick(now)
    await asyncio.gather(*list(app.tasks.values()))
    await app.scheduler_tick(now)
    assert len(app.store.history()) == 1
    assert app.store.history()[0]["schedule_key"] == "2026-10-16"
    assert not app.store.create_run("duplicate", schedule_key="2026-10-16")


async def test_schedule_never_backfills_before_enabled_at(app, monkeypatch):
    configure(app, schedule=SchedulePolicy(enabled=True))
    eligible = datetime(2026, 10, 16, 20, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "latest_eligible_run", lambda *_: eligible)
    app.store.set("schedule_enabled_at", (eligible + timedelta(seconds=1)).timestamp())
    await app.scheduler_tick(eligible + timedelta(days=1))
    assert app.store.history() == []


async def test_recipient_verification_and_reverification(app, monkeypatch):
    configure(app, smtp=smtp())
    codes = []

    def send(_config, _password, code):
        codes.append(code)
        return DeliveryOutcome(status="sent", message="accepted")

    monkeypatch.setattr(module, "send_verification", send)
    with pytest.raises(ValueError, match="verify"):
        app.submit(source="demo", send_email=True)
    await app.begin_verification()
    state = app.store.get("verification")
    assert len(state["hash"]) == 64 and "code" not in state
    with pytest.raises(ValueError, match="Wait"):
        await app.begin_verification()
    app.verify(codes[0])
    assert app.store.get("verified_recipient") == "owner@example.org"
    row = await completed(app, source="demo")
    configure(app, smtp=smtp("new@example.org"))
    with pytest.raises(ValueError, match="Verify"):
        await app.deliver(row["id"])
    with pytest.raises(ValueError, match="verify"):
        app.submit(source="demo", send_email=True)


async def test_expired_and_exhausted_verification_codes_fail(app, monkeypatch):
    configure(app, smtp=smtp())
    codes = []

    def send(_config, _password, code):
        codes.append(code)
        return DeliveryOutcome(status="sent", message="accepted")

    monkeypatch.setattr(module, "send_verification", send)
    await app.begin_verification()
    state = app.store.get("verification")
    state["expires"] = datetime.now(timezone.utc).timestamp() - 1
    app.store.set("verification", state)
    with pytest.raises(ValueError, match="expired"):
        app.verify(codes[0])
    state["expires"] = datetime.now(timezone.utc).timestamp() + 60
    state["attempts"] = 5
    app.store.set("verification", state)
    with pytest.raises(ValueError, match="expired"):
        app.verify(codes[0])
    assert app.store.get("verified_recipient") is None


async def test_failed_delivery_keeps_report_and_allows_retry(app, monkeypatch):
    configure(app, smtp=smtp())
    app.store.set("verified_recipient", "owner@example.org")
    outcomes = iter(["failed", "sent"])
    monkeypatch.setattr(module, "send_report", lambda *_: DeliveryOutcome(status=next(outcomes), message="test"))
    row = await completed(app, source="demo", send_email=True)
    assert row["result"] and row["status"] in {"success", "partial"} and row["delivery"] == "failed"
    assert (await app.deliver(row["id"])).status == "sent"
    with pytest.raises(ValueError, match="already attempted"):
        await app.deliver(row["id"])


async def test_uncertain_delivery_is_never_retried(app, monkeypatch):
    configure(app, smtp=smtp())
    app.store.set("verified_recipient", "owner@example.org")
    monkeypatch.setattr(module, "send_report", lambda *_: DeliveryOutcome(status="uncertain", message="test"))
    row = await completed(app, source="demo", send_email=True)
    with pytest.raises(ValueError, match="already attempted"):
        await app.deliver(row["id"])


def test_startup_recovers_interrupted_work_and_smtp_acceptance(tmp_path):
    store = Store(tmp_path)
    assert store.create_run("interrupted")
    store.update_run("interrupted", status="running", delivery="sending")
    restarted = Store(tmp_path)
    restarted.recover()
    row = restarted.run("interrupted")
    assert row["status"] == "failed" and row["delivery"] == "uncertain"
    assert restarted.create_run("replacement")


def test_configuration_environment_overrides_saved_settings(tmp_path, monkeypatch):
    save_settings(tmp_path, Settings(max_searches=7))
    monkeypatch.setenv("PORTFOLIO_SETTING_MAX_SEARCHES", "2")
    assert load_settings(tmp_path).max_searches == 2


async def test_scheduled_runs_use_latest_saved_positions_and_preserve_manual_data(app, monkeypatch):
    configure(app, schedule=SchedulePolicy(enabled=True), max_data_age_hours=1)
    captured = []

    async def analyze(request, dependencies):
        captured.append(request.model_copy(deep=True))
        # Simulate pricing enrichment changing the research copy, never saved data.
        request.snapshot.positions[0].price = Decimal('999')
        return AnalysisResult(snapshot_as_of=request.snapshot.as_of)

    monkeypatch.setattr(module, 'analyze_portfolio', analyze)
    eligible = datetime(2026, 10, 16, 20, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(module, 'latest_eligible_run', lambda *_: eligible)
    app.store.set('schedule_enabled_at', (eligible - timedelta(days=1)).timestamp())
    dated = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    first = PortfolioSnapshot(as_of=dated, source='manual', positions=[
        Position(symbol='ABC', quantity='2', price=None)], cash='1250.50')
    app.store.save_snapshot(first)
    await app.scheduler_tick(eligible + timedelta(hours=1))
    await asyncio.gather(*list(app.tasks.values()))
    assert captured[0].snapshot == first
    assert app.store.snapshot() == first
    assert captured[0].include_suggestions is False
    assert captured[0].reporting_cutoff == eligible
    row = app.store.run(app.store.history()[0]['id'])
    assert any(dated.isoformat() in warning and 'not automatically synchronized' in warning
               for warning in AnalysisResult.model_validate_json(row['result']).warnings)

    second = first.model_copy(deep=True)
    second.positions[0].quantity = Decimal('7')
    second.cash = Decimal('42')
    second.as_of = dated + timedelta(days=1)
    app.store.save_snapshot(second)
    eligible += timedelta(days=3)
    await app.scheduler_tick(eligible + timedelta(hours=1))
    await asyncio.gather(*list(app.tasks.values()))
    assert len(captured) == 2
    assert captured[1].snapshot == second
    assert app.store.snapshot() == second
    assert len(app.store.history()) == 2
