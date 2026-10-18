from datetime import datetime, timezone
from decimal import Decimal
import pytest
from portfolio_analyzer.imports import parse_holdings_csv


def test_import_cash_and_coverage():
    result = parse_holdings_csv('symbol,quantity,price,asset_type\nABC,2,10,stock\nUSD,25,,cash\nUSD,3,2,cash\nBTC,1,5,crypto', datetime.now(timezone.utc))
    assert result.cash == Decimal('31')
    assert result.positions[0].quantity == 2
    assert result.unsupported == ['BTC']


@pytest.mark.parametrize('content', ['symbol,quantity\nX,NaN', 'symbol,quantity\nX,-1', 'symbol,quantity\nX,1\nX,2', 'quantity\n2'])
def test_reject_invalid_import(content):
    with pytest.raises(ValueError):
        parse_holdings_csv(content, datetime.now(timezone.utc))


def test_require_dated_snapshot():
    with pytest.raises(ValueError, match='timezone'):
        parse_holdings_csv('symbol,quantity\nX,2', datetime.now())


def test_cash_aliases_and_formatted_currency_are_separate_from_holdings():
    snapshot = parse_holdings_csv('symbol,quantity,price\nABC,2,"$1,234.50"\nCASH,"$2,000.25",\nUSD,10,', datetime.now(timezone.utc))
    assert snapshot.cash == Decimal('2010.25')
    assert [p.symbol for p in snapshot.positions] == ['ABC']
    assert snapshot.positions[0].price == Decimal('1234.50')


def test_unknown_price_is_preserved():
    snapshot = parse_holdings_csv('symbol,quantity,price\nABC,2,', datetime.now(timezone.utc))
    assert snapshot.positions[0].price is None


@pytest.mark.parametrize('amount', ['"1,23"', '$-5', 'Infinity', '1e999999', '$', '(10)'])
def test_invalid_currency_amount_rejected(amount):
    with pytest.raises(ValueError, match='finite nonnegative'):
        parse_holdings_csv(f'symbol,quantity\nCASH,{amount}', datetime.now(timezone.utc))
def test_explicit_fund_type_preserves_real_cash_like_ticker():
    from datetime import datetime, timezone
    snapshot = parse_holdings_csv('symbol,quantity,price,asset_type\nUSD,3,40,etf\nCASH,100,,cash', datetime.now(timezone.utc))
    assert snapshot.cash == 100
    assert snapshot.positions[0].symbol == 'USD'
    assert snapshot.positions[0].quantity == 3

