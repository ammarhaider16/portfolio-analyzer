"""Application connection checks and cached preflight; independent of analyst decisions."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone


def revision(app, kind):
    from .service import read_secret, resolve_model_key

    settings = app.settings
    if kind == "model":
        key = resolve_model_key(settings, app.secrets)[0]
        data = [settings.model.model_dump(), settings.model_enabled, key]
    else:
        data = [
            {
                name: getattr(settings, name)
                for name in (
                    "search_provider",
                    "primary_feeds_enabled",
                    "sec_company_filings_enabled",
                    "sec_contact",
                    "ticker_news_enabled",
                    "news_feed_sources",
                )
            },
            read_secret(app.secrets, "tavily_api_key"),
        ]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def cached(app, kind):
    result = app.store.get("connection_tests", {}).get(kind, {})
    try:
        return result if result.get("revision") == revision(app, kind) else {}
    except Exception:
        return {}


def readiness(app):
    from .service import read_secret, resolve_model_key

    settings = app.settings
    issues = []
    needs_setup = False

    def issue(kind, message, setup=False):
        nonlocal needs_setup
        needs_setup = needs_setup or setup
        issues.append(
            {"kind": kind, "message": message, "target": "model-form" if kind == "model" else "research-form"}
        )

    try:
        key = resolve_model_key(settings, app.secrets)[0]
    except Exception:
        key = None
    if not settings.model_enabled or not settings.model.model.strip() or not key:
        issue("model", "Configure and enable your research model, then test the connection.", True)
    else:
        model = cached(app, "model")
        if not model:
            issue("model", "Your model connection will be checked before research starts.")
        elif model.get("status") != "passed":
            issue("model", "Your model connection needs attention. Open Connections and test it again.", True)
    try:
        search_key = read_secret(app.secrets, "tavily_api_key")
    except Exception:
        search_key = None
    available = settings.search_provider != "disabled" and (
        settings.search_provider == "keyless"
        or bool(search_key)
        or settings.primary_feeds_enabled
        or settings.ticker_news_enabled
        and bool(settings.news_feed_sources)
        or settings.sec_company_filings_enabled
        and bool(settings.sec_contact)
    )
    if not available:
        issue("search", "Enable DDGS web search or configure another news source in Connections.", True)
    else:
        search = cached(app, "search")
        if not search:
            issue("search", "Your news sources will be checked before research starts.")
        elif not any(
            item.get("status") in {"passed", "empty"} for item in search.get("sources", {}).values()
        ):
            issue("search", "No news source is working. Open Connections and test your sources again.", True)
    return {
        "state": "needs_configuration" if needs_setup else "needs_check" if issues else "ready",
        "issues": issues,
    }


async def check(app, kind, value=None):
    from .service import probe_model_readiness, build_research

    stamp = datetime.now(timezone.utc).isoformat()
    before = revision(app, kind)
    if kind == "model":
        result = await probe_model_readiness(app.settings, app.secrets, api_key=value)
        stored = {**result, "status": "passed" if result["ok"] else "failed"}
    else:
        from .providers import probe_research_sources

        settings = app.settings
        research = build_research(settings, app.secrets, tavily_key=value)
        snapshot = app.store.snapshot()
        position = snapshot.positions[0] if snapshot and snapshot.positions else None
        async with asyncio.timeout(35):
            outcomes = await probe_research_sources(
                research,
                symbol=position.symbol if position else "AAPL",
                name=position.instrument_name if position else None,
            )
        active = {
            "search": settings.search_provider != "disabled",
            "fed": settings.search_provider != "disabled" and settings.primary_feeds_enabled,
            "sec_filings": settings.search_provider != "disabled" and settings.sec_company_filings_enabled,
            **{
                feed: settings.search_provider != "disabled"
                and settings.ticker_news_enabled
                and feed in settings.news_feed_sources
                for feed in ("google_news", "nasdaq", "bing_news")
            },
        }
        for source, enabled in active.items():
            outcomes.setdefault(
                source,
                {
                    "status": "skipped" if enabled else "disabled",
                    "reason": "Source not configured" if enabled else "Disabled",
                    "tested_at": stamp,
                },
            )
        working = any(item.get("status") in {"passed", "empty"} for item in outcomes.values())
        failed = any(item.get("status") in {"failed", "skipped"} for item in outcomes.values())
        result = {
            "ok": working,
            "message": "Source checks complete; review each source outcome.",
            "sources": outcomes,
            "tested_at": stamp,
        }
        stored = {**result, "status": "partial" if working and failed else "passed" if working else "failed"}
    if value is None:
        if before != revision(app, kind):
            return {
                **result,
                "ok": False,
                "message": "Connections changed during the check. Test the saved settings again.",
            }
        tests = app.store.get("connection_tests", {})
        tests[kind] = {**stored, "revision": before}
        app.store.set("connection_tests", tests)
    return result


async def ensure_ready(app):
    state = readiness(app)
    if state["state"] == "needs_configuration":
        raise ValueError(" ".join(item["message"] for item in state["issues"]))
    for kind in ("model", "search"):
        if not cached(app, kind):
            await check(app, kind)
        state = readiness(app)
        if state["state"] == "needs_configuration":
            raise ValueError(" ".join(item["message"] for item in state["issues"]))
    if state["state"] != "ready":
        raise ValueError("Connections changed. Check them again before running research.")
