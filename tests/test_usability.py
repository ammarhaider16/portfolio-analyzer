"""Acceptance checks for readable offline portfolio conclusions and coverage."""
import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from portfolio_analyzer.delivery import briefing_summary, render_report
from portfolio_analyzer.engine import EngineDependencies, _macro_matches, _recent_event, analyze_portfolio
from portfolio_analyzer.models import AnalysisRequest, Evidence, PortfolioSnapshot, Position
from portfolio_analyzer.storage import Store
from portfolio_analyzer.web import create_app


def analyze(positions, cash="0", unsupported=None, question=""):
    snapshot = PortfolioSnapshot(as_of=datetime.now(timezone.utc), positions=positions,
                                 cash=cash, unsupported=unsupported or [])
    return asyncio.run(analyze_portfolio(AnalysisRequest(snapshot=snapshot, question=question), EngineDependencies()))


def test_cash_only_has_interpretation_without_holdings_or_price_movement():
    result = analyze([], cash="10000")
    html, text = render_report(result)
    assert "Portfolio is entirely cash" in text and "Portfolio is entirely cash" in html
    assert "Largest valued holding" not in text
    assert "price contributions" not in text.lower()
    assert result.coverage["event_state"] == "not_applicable"


def test_balanced_holdings_have_a_deterministic_conclusion():
    result = analyze([Position(symbol=symbol, quantity=1, price=100, asset_type="stock")
                      for symbol in ("AAPL", "MSFT", "GOOG", "ORCL")], cash="100")
    assert any(finding.title == "How this portfolio is allocated" for finding in result.findings)
    assert "No supported portfolio insight" not in render_report(result)[1]


def test_unpriced_holding_is_named_next_to_denominator_and_limitation():
    result = analyze([Position(symbol="AAPL", quantity=1, price=100, asset_type="stock"),
                      Position(symbol="MSFT", quantity=1)], cash="100", question="What recent news matters?")
    assert result.coverage["summary"].startswith("1 of 2 holdings valued")
    assert result.coverage["missing_price_symbols"] == ["MSFT"]
    assert result.coverage["event_state"] == "unavailable"
    assert any("MSFT" in issue["message"] for issue in result.coverage["issues"])
    assert any("MSFT" in finding.detail and "valued assets" in finding.detail for finding in result.findings)
    text = render_report(result)[1]
    assert "MSFT excluded" in text
    assert "Current-event research was unavailable" in text


def test_history_uses_meaningful_summary_and_csv_template_is_downloadable(tmp_path):
    store = Store(tmp_path)
    result = analyze([], cash="100")
    assert store.create_run(result.run_id)
    store.finish(result)
    assert store.history()[0]["summary"] == briefing_summary(result)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/api/import/template")
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert response.text.startswith("ticker,price,quantity\n")


def test_official_feed_mention_is_not_mistaken_for_relevant_current_event():
    now = datetime.now(timezone.utc)
    unrelated = Evidence(id="enforcement", url="https://www.federalreserve.gov/newsevents/pressreleases/enforcement.htm",
                         title="Federal Reserve Board terminates enforcement action", publisher="Federal Reserve",
                         excerpt="Interest rates were mentioned elsewhere on the page", published_at=now)
    old = Evidence(id="old_rates", url="https://www.federalreserve.gov/newsevents/pressreleases/monetary.htm",
                   title="FOMC interest rate decision", publisher="Federal Reserve", published_at=now - timedelta(days=120))
    current = old.model_copy(update={"published_at": now - timedelta(days=2)})
    assert not _macro_matches(unrelated)
    assert _macro_matches(old)
    assert not _recent_event(old, now)
    assert _recent_event(current, now)
