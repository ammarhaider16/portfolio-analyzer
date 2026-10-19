"""CSV holdings import. Cash quantity is its monetary amount; price defaults to 1."""
import csv
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import Field

from .models import Contract, PortfolioSnapshot
from .screenshot_import import DraftPosition


class CsvImportDraft(Contract):
    """An unsaved import may lack share counts; it is never a portfolio snapshot."""

    as_of: datetime
    positions: list[DraftPosition] = Field(default_factory=list, max_length=1000)
    cash: Decimal = Decimal(0)
    source: Literal['csv'] = 'csv'
    account_id: str = 'manual'
    unsupported: list[str] = Field(default_factory=list)


def parse_amount(value: str) -> Decimal:
    """Accept plain decimals or conventional dollar amounts without losing precision."""
    value = value.strip()
    if value.startswith('$'):
        value = value[1:].strip()
    if not re.fullmatch(r'(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?|\.\d+', value):
        raise ValueError('Amount must be a finite nonnegative number')
    return Decimal(value.replace(',', ''))


def validate_portfolio_snapshot(snapshot: PortfolioSnapshot) -> PortfolioSnapshot:
    """Normalize cash aliases and reject ambiguous duplicate holdings."""
    positions, seen, cash = [], set(), snapshot.cash
    for position in snapshot.positions:
        kind = position.asset_type.strip().lower()
        if kind == 'cash' or (position.symbol in {'CASH', 'USD'} and kind not in {'etf', 'fund', 'mutual_fund'}):
            cash += position.quantity * (position.price if position.price is not None else Decimal(1))
            continue
        if position.symbol in seen:
            raise ValueError(f'Duplicate symbol {position.symbol}; combine lots first')
        seen.add(position.symbol)
        positions.append(position.model_copy(update={'asset_type': position.asset_type.strip().lower() or 'unknown'}))
    if not positions and cash == 0:
        raise ValueError('Portfolio contains no holdings or cash')
    return snapshot.model_copy(update={'positions': positions, 'cash': cash,
        'unsupported': [p.symbol for p in positions if p.asset_type not in {'stock', 'etf', 'unknown'}]})


def parse_holdings_csv(content: str, as_of: datetime) -> CsvImportDraft:
    if len(content) > 2_000_000:
        raise ValueError('CSV limit is 2 MB')
    if as_of.tzinfo is None:
        raise ValueError('Snapshot date must include a timezone')
    reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')))
    aliases = {'ticker': 'symbol', 'shares': 'quantity', 'share count': 'quantity',
               'current price': 'price', 'share price': 'price'}
    reader.fieldnames = [aliases.get(name.strip().lower(), name.strip().lower()) for name in (reader.fieldnames or [])]
    if 'symbol' not in reader.fieldnames or not ({'price', 'quantity'} & set(reader.fieldnames)):
        raise ValueError('CSV needs a ticker/symbol column and a price column (or quantity in older files)')
    if len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError('CSV has duplicate columns after matching ticker, price, and quantity names')
    positions, unsupported, cash, seen = [], [], Decimal(0), set()
    def number(value, field, line, optional=False):
        if (value is None or not value.strip()) and optional:
            return None
        try:
            result = parse_amount(value)
            if not result.is_finite() or result < 0:
                raise ValueError()
            return result
        except (InvalidOperation, ValueError, AttributeError):
            raise ValueError(f'Row {line}: {field} must be a finite nonnegative number') from None
    for line, row in enumerate(reader, 2):
        if None in row:
            raise ValueError(f'Row {line}: too many columns; quote values containing commas')
        if line > 1001:
            raise ValueError('CSV limit is 1,000 rows')
        symbol = (row.get('symbol') or '').strip().upper()
        if not symbol or len(symbol) > 32:
            raise ValueError(f'Row {line}: provide a symbol of at most 32 characters')
        quantity = number(row.get('quantity'), 'quantity', line, True)
        price = number(row.get('price'), 'price', line, True)
        kind = (row.get('asset_type') or 'unknown').strip().lower()
        if kind == 'cash' or (symbol in {'CASH', 'USD'} and kind not in {'etf', 'fund', 'mutual_fund'}):
            if quantity is None and price is None:
                raise ValueError(f'Row {line}: cash needs an amount in quantity or price')
            cash += (quantity if quantity is not None else Decimal(1)) * (price if price is not None else Decimal(1))
            continue
        if symbol in seen:
            raise ValueError(f'Row {line}: duplicate symbol {symbol}; combine lots first')
        seen.add(symbol)
        if kind not in {'stock', 'etf', 'unknown'}:
            unsupported.append(symbol)
        positions.append(DraftPosition(symbol=symbol, quantity=quantity, price=price, asset_type=kind,
                                  sector=(row.get('sector') or '').strip() or None,
                                  previous_close=number(row.get('previous_close'), 'previous_close', line, True)))
    if not positions and cash == 0:
        raise ValueError('CSV contains no holdings or cash')
    return CsvImportDraft(as_of=as_of, positions=positions, cash=cash, unsupported=unsupported)
