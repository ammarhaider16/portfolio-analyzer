"""Report contract: editorial freedom, safe output and email privacy."""

from datetime import datetime, timezone
from portfolio_analyzer.delivery import render_report
from portfolio_analyzer.models import AnalysisResult, ReportSection


def report():
    return AnalysisResult(
        snapshot_as_of=datetime.now(timezone.utc),
        executive_summary="Cash cushions the portfolio.",
        sections=[
            ReportSection(
                heading="AI spending and your funds", content="A company announced a new investment."
            ),
            ReportSection(
                heading="Risks worth watching", content="- Overlapping fund exposure\n- Upcoming earnings"
            ),
        ],
        metrics={"cash_weight_pct": 25, "weights_pct": {"AAPL": 75}, "known_value": "123456"},
    )


def test_report_preserves_analyst_structure_without_inventing_headings():
    html, text = render_report(report())
    assert "AI spending and your funds" in html and "Risks worth watching" in text
    assert text.index("AI spending") < text.index("Risks worth watching")
    assert "What happened" not in html and "What this means" not in html
    assert "verified" not in html and "Sources &amp; coverage" not in html


def test_report_escapes_model_text_and_does_not_print_raw_urls():
    value = report()
    value.sections = [
        ReportSection(
            heading="<script>alert(1)</script>", content="<img src=x onerror=bad> https://example.org/private"
        )
    ]
    html, text = render_report(value)
    assert "<script>" not in html and "<img" not in html
    assert "&lt;script&gt;" in html
    assert "https://" not in html + text


def test_percentage_email_omits_all_narrative_and_amounts():
    html, text = render_report(report(), percent_only=True)
    assert "75.0%" in text
    assert "123456" not in html + text and "Cash cushions" not in html + text
    assert "AI spending" not in html + text


def test_old_price_cannot_be_rendered_as_a_current_day_move():
    value = report()
    value.metrics.update(
        {
            "price_session": "2026-10-19",
            "position_details": [
                {
                    "symbol": "AAPL",
                    "price": "100",
                    "previous_close": "95",
                    "current_session": False,
                    "price_session": "2026-10-16",
                }
            ],
            "daily_price_contributions": {"AAPL": "5"},
        }
    )
    _, text = render_report(value)
    assert "day change Unavailable" in text and "+5.26%" not in text


def test_report_renders_emphasis_in_lists_and_tables_without_leaking_markers():
    value = report()
    value.sections = [ReportSection(heading='Position outlook', content='- **Apple:** watch demand.\n- **Cash:** provides flexibility.\n\n| Holding | Outlook |\n| --- | --- |\n| **AAPL** | Stable |')]
    html, text = render_report(value)
    assert '<strong>Apple:</strong>' in html and '<strong>AAPL</strong>' in html
    assert '<table>' in html and '**' not in html + text
