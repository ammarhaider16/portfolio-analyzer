from datetime import datetime, timezone
import smtplib
from unittest.mock import Mock

import pytest

from portfolio_analyzer.delivery import SMTPConfig, render_report, send_report, send_verification
from portfolio_analyzer.models import AnalysisResult, Evidence, Finding


def result():
    return AnalysisResult(snapshot_as_of=datetime.now(timezone.utc), metrics={"total_value": 123456, "cash_weight": 0.25}, findings=[Finding(title="<script>bad</script>", detail="Value $123456", evidence_ids=["2"])], sources=[Evidence(id="1", url="javascript:alert(1)", title="bad", publisher="unknown"), Evidence(id="2", url="https://example.org/?x=1&y=2", title="Evidence", publisher="Example")])


def config(**kwargs):
    return SMTPConfig(host="smtp.example.org", sender="owner@example.org", recipient="owner@example.org", **kwargs)


def test_render_safe_and_equivalent():
    html, plain = render_report(result())
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "javascript:" not in html
    assert "https://example.org/" in html and "https://example.org/" in plain
    assert "<img" not in html


def test_professional_report_has_accessible_metrics_and_risk_sections():
    report = result()
    report.metrics = {"known_value": "123456.78", "cash_weight_pct": 25}
    report.findings.append(Finding(title="Concentration", detail="Review exposure", kind="risk", evidence_ids=["2"]))
    report.warnings = ["Some prices are stale"]
    html, plain = render_report(report)
    assert '<th scope="row"' in html and "<caption" in html
    assert "$123,456.78" in html and "$123,456.78" in plain
    assert "25.00%" in html and "25.00%" in plain
    for title in ("What matters now", "Portfolio at a glance", "Data notes"):
        assert title in html and title in plain
    assert "cash_weight_pct" not in html
    assert "Status: " not in plain


def test_embed_drops_page_title_but_keeps_plain_text_and_sections():
    report = result().model_copy(update={"coverage": {
        "summary": "2 of 3 holdings valued; sourced insights available", "issues": []}})
    default_html, default_plain = render_report(report)
    embed_html, embed_plain = render_report(report, embed=True)
    assert "<h1>Betafolio briefing</h1>" in default_html
    assert "<h1>Betafolio briefing</h1>" not in embed_html
    # Email and plain-text delivery keep the page title regardless of embedding.
    assert default_plain == embed_plain
    assert default_plain.startswith("Betafolio briefing")
    assert "Briefing summary" in embed_html
    assert "Research coverage" in embed_html
    assert "2 of 3 holdings valued" in embed_html
    assert "What to do next" not in embed_html
    assert "Data notes" not in embed_html


def test_briefing_summary_distinguishes_conclusion_from_coverage():
    report = {"coverage": {"summary": "Portfolio measures ready; sourced insights available"},
              "executive_summary": {"text": "The fund changed its benchmark, affecting its exposure.",
                                    "evidence_ids": ["s1"]}}
    html, plain = render_report(report)
    assert plain.index("The fund changed") < plain.index("Research coverage")
    assert "Briefing summary" in html and "Bottom line" not in html


def test_legacy_summary_uses_measured_observation_and_honest_gap():
    report = {"metrics": {"weights_pct": {"AAPL": 32.55, "BND": 10}},
              "coverage": {"summary": "Sources failed"}}
    _, plain = render_report(report)
    assert "AAPL represents 32.5%" in plain
    assert "did not establish a sourced current-event conclusion" in plain
    assert "No events" not in plain


def test_percentage_view_does_not_leak_executive_summary_or_activity():
    report = {"metrics": {"cash_weight_pct": 25},
              "executive_summary": {"text": "Holding value $123456", "evidence_ids": []},
              "research_activity": [{"query": "owner holding value $123456"}]}
    html, plain = render_report(report, percent_only=True)
    assert "123456" not in html + plain
    assert "Briefing summary" not in plain


def test_summary_citations_render_only_safe_known_sources():
    report = {"executive_summary": {"text": "Supported development.", "evidence_ids": ["good", "bad", "missing"]},
              "sources": [{"id": "good", "title": "Fund notice", "url": "https://example.org/notice"},
                          {"id": "bad", "title": "Unsafe", "url": "javascript:alert(1)"}]}
    html, plain = render_report(report)
    assert 'href="https://example.org/notice"' in html
    assert "Summary source: Fund notice" in plain
    assert "javascript:" not in html + plain


def test_report_lists_actionable_next_steps_for_coverage_issues():
    report = result().model_copy(update={"coverage": {
        "summary": "1 of 2 holdings valued", "issues": [
            {"code": "missing_prices", "message": "MSFT excluded from value and allocation; add prices.",
             "action": "portfolio"}]}})
    html, plain = render_report(report)
    assert "Data notes" in html and "Data notes" in plain
    assert "Review holdings and cash" in plain


def test_percentage_view_omits_amounts_and_narrative():
    html, plain = render_report(result(), percent_only=True)
    assert "123456" not in html + plain
    assert "Cash allocation: 25%" in plain
    assert "Evidence" not in plain


def test_percentage_view_preserves_named_weight_maps():
    report = result()
    report.metrics["weights_pct"] = {"AAPL": 25.0}
    _, plain = render_report(report, percent_only=True)
    assert "Holding allocations / AAPL: 25%" in plain


@pytest.mark.parametrize(("timestamp", "expected"), [
    ("2026-10-19T15:43:56.123456Z", "Oct 19, 2026, 11:43 AM EDT"),
    ("2026-01-19T15:43:00Z", "Jan 19, 2026, 10:43 AM EST"),
])
def test_report_human_dates_and_dst(timestamp, expected):
    report = {"snapshot_as_of": timestamp}
    html, plain = render_report(report)
    assert expected in html and expected in plain
    assert timestamp not in html + plain
    assert "3:43 PM UTC" in render_report(report, timezone="UTC")[1]


def test_holding_table_labels_rounding_date_and_brief_change():
    report = {"report_length": "detailed", "metrics": {
        "position_details": [
            {"symbol": "AAPL", "price": "205.123", "previous_close": "200", "sector": "Tech"},
            {"symbol": "MSFT", "price": "300", "previous_close": "0"}],
        "market_data_as_of": {"AAPL": {"trading_date": "2026-10-16"}},
        "position_news": [{"symbol": "AAPL"}, {"symbol": "MSFT"}]}}
    html, plain = render_report(report)
    assert '<th scope="col">Previous close</th>' in html
    assert '<th scope="row">AAPL</th>' in html
    assert "$205.12" in html and "$205.12" in plain
    assert "+2.56%" in html and "Oct 16, 2026" in html
    assert "previous close unavailable" not in plain
    report["report_length"] = "brief"
    html, plain = render_report(report)
    assert "AAPL +2.6% vs prior close" in html and "AAPL +2.6% vs prior close" in plain
    assert "MSFT +" not in plain


def test_percentage_history_estimate_is_labeled_and_private():
    report = {"metrics": {"historical_risk": {"annualized_volatility_pct": 12.3456},
                          "known_value": 987654, "weights_pct": {"AAPL": 10.999}},
              "findings": [{"title": "Secret narrative", "detail": "$987654"}],
              "sources": [{"id": "x", "url": "https://secret.example"}]}
    html, plain = render_report(report, percent_only=True)
    assert "(historical estimate): 12.3%" in plain
    assert "AAPL: 11%" in plain
    assert "987654" not in html + plain and "secret.example" not in html + plain


def test_cluster_rendering_related_sources_order_and_opaque_link_privacy():
    report = {"metrics": {
        "position_news_display_order": ["MSFT", "AAPL"],
        "position_news": [
            {"symbol": "AAPL", "stories": []},
            {"symbol": "MSFT", "stories": [{"representative_source_id": "wire",
                "member_source_ids": ["wire", "other"], "category": "event", "evidence_grade": "headline",
                "publisher_domain": "reuters.com", "publisher_tier": "major"}],
             "additional_stories": [{"representative_source_id": "other", "category": "also"}]}],
        "setup_news_reason": "insufficient_holdings"},
        "sources": [{"id": "wire", "title": "Results beat estimates", "url": "https://news.google.com/rss/articles/opaque",
                     "published_at": "2026-10-19", "content_type": "news_feed"},
                    {"id": "other", "title": "Related result", "url": "https://example.com/result"}]}
    html, plain = render_report(report)
    assert html.index("<h3>MSFT") < html.index("<h3>AAPL")
    assert "Events" in html and "plus 1 related reports" in html and "More stories" in html
    assert "reuters.com" in html and "headline only, not verified" in html
    assert "news.google.com/rss/articles" not in plain
    assert "link available in the dashboard" in plain
    assert "at least two holdings" in plain


def test_data_notes_show_two_then_expand_and_keep_plain_text_complete():
    report = {"coverage": {"issues": [
        {"message": f"Missing data {number}"} for number in range(5)]}}
    html, plain = render_report(report)
    start = html.index('<h2>Data notes</h2>')
    assert html[start:].split('<details>')[0].count('<p>') == 2
    assert '<summary>3 more data notes</summary>' in html
    assert "Missing data 4" in plain


def test_failed_retrieval_excerpt_remains_discovery_even_with_story_metadata():
    report = {"metrics": {"position_news": [{"symbol": "AAPL", "status": "discovery_only",
        "stories": [{"representative_source_id": "failed", "member_source_ids": ["failed"],
                     "evidence_grade": "full", "category": "also"}]}]},
        "sources": [{"id": "failed", "title": "Apple earnings preview", "content_type": "search_excerpt",
                     "evidence_grade": "full", "url": "https://example.org/blocked-article"}]}
    html, plain = render_report(report)
    assert "Apple earnings preview" in html
    assert "discovery only, not verified" in html and "discovery only, not verified" in plain
    assert "full source" not in html + plain
    assert "AAPL: relevant headlines were found but not verified" in html
    assert "AAPL: relevant headlines were found but not verified" in plain


@pytest.mark.parametrize("address", ["a@b.org\r\nBcc:evil@b.org", "a@b.org,b@b.org", "Name <a@b.org>"])
def test_address_injection_rejected(address):
    with pytest.raises(ValueError):
        SMTPConfig(host="smtp", sender="owner@example.org", recipient=address)


def test_requires_verified_owner(monkeypatch):
    smtp = Mock()
    monkeypatch.setattr(smtplib, "SMTP_SSL", smtp)
    assert send_report(result(), config(), "secret", "other@example.org").status == "failed"
    smtp.assert_not_called()


def test_tls_and_multipart(monkeypatch):
    smtp = Mock()
    smtp.send_message.return_value = {}
    constructor = Mock(return_value=smtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", constructor)
    assert send_report(result(), config(), "secret", "owner@example.org").status == "sent"
    assert constructor.call_args.kwargs["context"].check_hostname
    message = smtp.send_message.call_args.args[0]
    assert message.get_content_type() == "multipart/alternative"
    assert message["Cc"] is None and message["Bcc"] is None
    assert smtp.send_message.call_args.kwargs["to_addrs"] == ["owner@example.org"]


def test_starttls_failure_never_sends(monkeypatch):
    smtp = Mock()
    smtp.starttls.side_effect = smtplib.SMTPNotSupportedError("No TLS")
    monkeypatch.setattr(smtplib, "SMTP", Mock(return_value=smtp))
    outcome = send_report(result(), config(tls_mode="starttls", username="owner"), "secret", "owner@example.org")
    assert outcome.status == "failed"
    smtp.login.assert_not_called()
    smtp.send_message.assert_not_called()


def test_uncertain_delivery_not_retried(monkeypatch):
    smtp = Mock()
    smtp.send_message.side_effect = smtplib.SMTPServerDisconnected("lost")
    monkeypatch.setattr(smtplib, "SMTP_SSL", Mock(return_value=smtp))
    assert send_report(result(), config(), "secret", "owner@example.org").status == "uncertain"
    assert smtp.send_message.call_count == 1


def test_verification_and_plain_text(monkeypatch):
    smtp = Mock()
    smtp.send_message.return_value = {}
    monkeypatch.setattr(smtplib, "SMTP_SSL", Mock(return_value=smtp))
    assert send_verification(config(), "secret", "123456").status == "sent"
    assert "123456" in smtp.send_message.call_args.args[0].get_content()
    send_report(result(), config(plain_text_only=True), "secret", "owner@example.org")
    assert smtp.send_message.call_args.args[0].get_content_type() == "text/plain"
