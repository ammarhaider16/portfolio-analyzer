"""Financial arithmetic stays outside the language model."""
from decimal import Decimal
import math
import statistics
from .models import PortfolioSnapshot


def calculate_metrics(snapshot: PortfolioSnapshot) -> dict:
    values: dict[str, Decimal] = {}
    sectors: dict[str, Decimal] = {}
    changes: dict[str, Decimal] = {}
    missing = []
    for position in snapshot.positions:
        if position.price is None:
            missing.append(position.symbol)
            continue
        value = position.quantity * position.price
        values[position.symbol] = values.get(position.symbol, Decimal(0)) + value
        sector = position.sector or "Unclassified"
        sectors[sector] = sectors.get(sector, Decimal(0)) + value
        if position.previous_close is not None:
            changes[position.symbol] = changes.get(position.symbol, Decimal(0)) + position.quantity * (position.price - position.previous_close)
    total = sum(values.values(), snapshot.cash)
    def percent(value):
        return float(value / total * 100) if total else 0.0
    weights = {symbol: percent(value) for symbol, value in values.items()}
    return {
        "known_value": str(total), "cash": str(snapshot.cash),
        "cash_weight_pct": percent(snapshot.cash),
        "position_values": {k: str(v) for k, v in values.items()},
        "weights_pct": weights,
        "sector_weights_pct": {k: percent(v) for k, v in sectors.items()},
        "largest_position_pct": max(weights.values(), default=0),
        "daily_price_contributions": {k: str(v) for k, v in changes.items()},
        "daily_price_change_estimate": str(sum(changes.values(), Decimal(0))) if changes else None,
        "priced_positions": len(snapshot.positions) - len(missing),
        "total_positions": len(snapshot.positions),
        "missing_prices": missing, "unsupported": snapshot.unsupported,
        "coverage_complete": not missing and not snapshot.unsupported,
    }


def historical_risk(snapshot: PortfolioSnapshot, histories: dict[str, dict[str, float]]) -> dict:
    """Estimate daily-rebalanced current-weight risk on shared historical dates."""
    metrics = calculate_metrics(snapshot)
    symbols = [symbol for symbol in metrics["weights_pct"] if symbol in histories and len(histories[symbol]) >= 21]
    if not symbols:
        raise ValueError("Insufficient history")
    dates = sorted(set.intersection(*(set(histories[symbol]) for symbol in symbols)))
    if len(dates) < 21:
        raise ValueError("Insufficient overlapping observations")
    returns = []
    for previous, current in zip(dates, dates[1:]):
        daily = 0.0
        for symbol in symbols:
            old, new = histories[symbol][previous], histories[symbol][current]
            if not math.isfinite(old) or not math.isfinite(new) or old <= 0 or new <= 0:
                raise ValueError("Invalid historical price")
            daily += metrics["weights_pct"][symbol] / 100 * (new / old - 1)
        returns.append(daily)
    value = peak = 1.0
    drawdown = 0.0
    for change in returns:
        value *= 1 + change
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    return {"annualized_volatility_pct": statistics.stdev(returns) * math.sqrt(252) * 100,
            "maximum_drawdown_pct": drawdown * 100, "observations": len(returns),
            "start": dates[0], "end": dates[-1],
            "covered_known_value_pct": sum(metrics["weights_pct"][symbol] for symbol in symbols),
            "missing_symbols": [s for s in metrics["weights_pct"] if s not in symbols],
            "method": "Current weights, daily rebalancing, adjusted closes; cash and missing history assumed flat. Partial coverage may understate risk."}


def benchmark_comparison(snapshot: PortfolioSnapshot, histories: dict[str, dict[str, float]], benchmark: str, benchmark_history: dict[str, float]) -> dict:
    """Compare current-weight historical estimate over identical observed dates."""
    metrics = calculate_metrics(snapshot)
    symbols = [symbol for symbol in metrics["weights_pct"] if symbol in histories and len(histories[symbol]) >= 21]
    if not symbols or len(benchmark_history) < 21:
        raise ValueError("Insufficient benchmark history")
    dates = sorted(set(benchmark_history).intersection(*(set(histories[s]) for s in symbols)))
    if len(dates) < 21:
        raise ValueError("Insufficient overlapping benchmark observations")
    portfolio_growth = 1.0
    for previous, current in zip(dates, dates[1:]):
        daily = 0.0
        for symbol in symbols:
            old, new = histories[symbol][previous], histories[symbol][current]
            if not math.isfinite(old) or not math.isfinite(new) or old <= 0 or new <= 0:
                raise ValueError("Invalid historical price")
            daily += metrics["weights_pct"][symbol] / 100 * (new / old - 1)
        portfolio_growth *= 1 + daily
    first, last = benchmark_history[dates[0]], benchmark_history[dates[-1]]
    if not math.isfinite(first) or not math.isfinite(last) or first <= 0 or last <= 0:
        raise ValueError("Invalid benchmark price")
    portfolio_pct = (portfolio_growth - 1) * 100
    benchmark_pct = (last / first - 1) * 100
    return {"symbol": benchmark, "start": dates[0], "end": dates[-1], "observations": len(dates) - 1,
            "current_portfolio_return_estimate_pct": portfolio_pct, "benchmark_return_pct": benchmark_pct,
            "relative_change_percentage_points": portfolio_pct - benchmark_pct,
            "covered_known_value_pct": sum(metrics["weights_pct"][s] for s in symbols),
            "method": "Current weights, daily rebalancing, adjusted closes on common dates; cash and missing history assumed flat. Not realized account performance."}
