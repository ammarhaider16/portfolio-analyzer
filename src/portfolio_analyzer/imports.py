"""CSV holdings import. Cash quantity is its monetary amount; price defaults to 1."""
import csv
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from .models import PortfolioSnapshot, Position


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
        positions.append(position.model_copy(update={'asset_type': position.asset_type.strip().lower() or 'stock'}))
    if not positions and cash == 0:
        raise ValueError('Portfolio contains no holdings or cash')
    return snapshot.model_copy(update={'positions': positions, 'cash': cash,
        'unsupported': [p.symbol for p in positions if p.asset_type not in {'stock', 'etf'}]})


def parse_holdings_csv(content: str, as_of: datetime) -> PortfolioSnapshot:
    if len(content) > 2_000_000:
        raise ValueError('CSV limit is 2 MB')
    if as_of.tzinfo is None:
        raise ValueError('Snapshot date must include a timezone')
    reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')))
    if not {'symbol', 'quantity'}.issubset(reader.fieldnames or []):
        raise ValueError('CSV requires symbol and quantity columns')
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
        quantity = number(row.get('quantity'), 'quantity', line)
        price = number(row.get('price'), 'price', line, True)
        kind = (row.get('asset_type') or 'stock').strip().lower()
        if kind == 'cash' or (symbol in {'CASH', 'USD'} and kind not in {'etf', 'fund', 'mutual_fund'}):
            cash += quantity * (price if price is not None else Decimal(1))
            continue
        if symbol in seen:
            raise ValueError(f'Row {line}: duplicate symbol {symbol}; combine lots first')
        seen.add(symbol)
        if kind not in {'stock', 'etf'}:
            unsupported.append(symbol)
        positions.append(Position(symbol=symbol, quantity=quantity, price=price, asset_type=kind,
                                  sector=(row.get('sector') or '').strip() or None,
                                  previous_close=number(row.get('previous_close'), 'previous_close', line, True)))
    if not positions and cash == 0:
        raise ValueError('CSV contains no holdings or cash')
    return PortfolioSnapshot(as_of=as_of, positions=positions, cash=cash, source='csv', unsupported=unsupported)
