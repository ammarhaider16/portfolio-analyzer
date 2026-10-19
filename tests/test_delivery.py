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
    for title in ("What matters now", "Portfolio at a glance", "Limits of this briefing"):
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
    assert "Bottom line" in embed_html
    assert "2 of 3 holdings valued" in embed_html
    assert "What to do next" in embed_html
    assert "No action is needed right now" in embed_html


def test_report_lists_actionable_next_steps_for_coverage_issues():
    report = result().model_copy(update={"coverage": {
        "summary": "1 of 2 holdings valued", "issues": [
            {"code": "missing_prices", "message": "MSFT excluded from value and allocation; add prices.",
             "action": "portfolio"}]}})
    html, plain = render_report(report)
    assert "What to do next" in html and "What to do next" in plain
    assert "Review holdings and cash" in plain


def test_percentage_view_omits_amounts_and_narrative():
    html, plain = render_report(result(), percent_only=True)
    assert "123456" not in html + plain
    assert "Cash allocation: 0.25" in plain
    assert "Evidence" not in plain


def test_percentage_view_preserves_named_weight_maps():
    report = result()
    report.metrics["weights_pct"] = {"AAPL": 25.0}
    _, plain = render_report(report, percent_only=True)
    assert "Holding allocations / AAPL: 25.00%" in plain


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
