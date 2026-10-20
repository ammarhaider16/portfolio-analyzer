"""Prepare clean input without dictating the analyst's editorial choices."""

from portfolio_analyzer.models import Evidence
from portfolio_analyzer.news import clean_text, source_hygiene


def test_input_removes_markup_and_widgets_but_keeps_opinion_and_fund_research():
    assert clean_text('<a href="https://example.org">Company &amp; market</a>') == "Company & market"
    values = [
        Evidence(
            id="a", title="Why this fund may fit a portfolio", url="https://example.org/a", publisher="A"
        ),
        Evidence(id="b", title="Fund prospectus and holdings", url="https://example.org/b", publisher="B"),
        Evidence(id="c", title="AAPL options chain", url="https://example.org/options-chain", publisher="C"),
    ]
    kept, _ = source_hygiene(values)
    assert [source.id for source in kept] == ["a", "b"]
