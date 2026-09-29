"""Performance metrics.

Every figure here describes a **historical sample**. None is an expectation, and the
naming avoids words like "expected" for that reason — ``expectancy`` is the one
exception, because it is the standard term for a realised per-trade average, and its
docstring says so explicitly.

Choices that differ between implementations, stated rather than assumed
----------------------------------------------------------------------
**Returns are computed from the equity curve**, not from trade P&L. The equity curve
includes the cost of sitting in cash and the drag of unrealised drawdowns, which a
trade-list-only calculation misses entirely.

**Sharpe and Sortino use a configurable risk-free rate, defaulting to zero.** Zero is
not neutral — it inflates Sharpe whenever rates are meaningfully positive — so the rate
is a parameter and the value used is reported alongside the result.

**Sortino's denominator is downside deviation over all periods**, not only the negative
ones. Dividing by the count of negative periods instead is a common variant that makes
a strategy with few but severe losses look better than it is.

**Maximum drawdown is measured on the equity curve**, peak to trough, including
unrealised losses. A trade-based drawdown understates what an account actually
experienced.

**CAGR needs at least a year of data to mean anything.** Annualising a three-month
sample produces a headline number that is arithmetically correct and practically
meaningless, so :func:`cagr` returns ``None`` below a configurable minimum rather than
printing something impressive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

__all__ = [
    "TRADING_DAYS_PER_YEAR",
    "MIN_DAYS_FOR_CAGR",
    "compute_metrics",
    "compute_benchmark_metrics",
    "drawdown_series",
    "max_drawdown",
    "monthly_returns",
    "annual_returns",
    "rolling_sharpe",
]

TRADING_DAYS_PER_YEAR = 252.0

MIN_DAYS_FOR_CAGR = 365
"""Below one calendar year, annualising is not reported.

A 40% gain over two months annualises to roughly 900%. That number is arithmetically
correct and tells you nothing, so it is withheld rather than printed with a caveat
nobody reads.
"""


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #


def _returns(equity: pd.Series) -> pd.Series:
    """Simple period returns from an equity curve, with the leading NaN dropped."""
    return equity.pct_change(fill_method=None).dropna()


def total_return_pct(equity: pd.Series) -> float | None:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return None
    return float((equity.iloc[-1] / equity.iloc[0] - 1.0) * 100.0)


def cagr_pct(equity: pd.Series, *, min_days: int = MIN_DAYS_FOR_CAGR) -> float | None:
    """Compound annual growth rate, or ``None`` when the sample is too short.

    Also ``None`` when the account was wiped out: a negative or zero final equity has
    no real-valued growth rate, and returning -100% would imply a recoverable position.
    """
    if len(equity) < 2 or equity.iloc[0] <= 0 or equity.iloc[-1] <= 0:
        return None

    days = (equity.index[-1] - equity.index[0]).days
    if days < min_days:
        return None

    years = days / 365.25
    return float(((equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0) * 100.0)


def annualised_volatility_pct(
    equity: pd.Series, periods_per_year: float = TRADING_DAYS_PER_YEAR
) -> float | None:
    returns = _returns(equity)
    if len(returns) < 2:
        return None
    return float(returns.std(ddof=1) * math.sqrt(periods_per_year) * 100.0)


def sharpe_ratio(
    equity: pd.Series,
    *,
    risk_free_rate: float = 0.0,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
) -> float | None:
    """Annualised Sharpe ratio.

    ``risk_free_rate`` is an annual decimal (0.04 = 4%). Zero is the default but is not
    a neutral choice: with positive rates it credits the strategy for returns it could
    have had risk-free, so the value used is reported with the result.

    ``None`` when volatility is zero — a constant equity curve has no Sharpe, and
    returning infinity or a large number would be worse than admitting that.
    """
    returns = _returns(equity)
    if len(returns) < 2:
        return None

    volatility = returns.std(ddof=1)
    if volatility == 0 or not np.isfinite(volatility):
        return None

    period_rf = risk_free_rate / periods_per_year
    excess = returns - period_rf
    return float(excess.mean() / volatility * math.sqrt(periods_per_year))


def sortino_ratio(
    equity: pd.Series,
    *,
    risk_free_rate: float = 0.0,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
) -> float | None:
    """Annualised Sortino ratio: excess return over *downside* deviation.

    The downside deviation divides by the total number of periods, not by the count of
    negative ones. The alternative flatters a strategy whose losses are rare but severe,
    which is precisely the profile that blows up.
    """
    returns = _returns(equity)
    if len(returns) < 2:
        return None

    period_rf = risk_free_rate / periods_per_year
    excess = returns - period_rf
    downside = excess.clip(upper=0.0)

    denominator = math.sqrt(float((downside**2).mean()))
    if denominator == 0 or not np.isfinite(denominator):
        return None
    return float(excess.mean() / denominator * math.sqrt(periods_per_year))


def drawdown_series(equity: pd.Series) -> pd.Series:
    """Percentage drawdown from the running peak, at every point. Always ``<= 0``."""
    if equity.empty:
        return pd.Series(dtype=float)
    running_peak = equity.cummax()
    return (equity / running_peak - 1.0) * 100.0


def max_drawdown(equity: pd.Series) -> dict[str, Any]:
    """Worst peak-to-trough decline, with its dates and recovery status."""
    if len(equity) < 2:
        return {"max_drawdown_pct": None, "peak_date": None, "trough_date": None,
                "recovery_date": None, "drawdown_days": None}

    drawdowns = drawdown_series(equity)
    trough_date = drawdowns.idxmin()
    worst = float(drawdowns.loc[trough_date])

    before_trough = equity.loc[:trough_date]
    peak_date = before_trough.idxmax()
    peak_value = float(before_trough.loc[peak_date])

    after = equity.loc[trough_date:]
    recovered = after[after >= peak_value]
    recovery_date = recovered.index[0] if len(recovered) else None

    return {
        "max_drawdown_pct": worst,
        "peak_date": peak_date.isoformat(),
        "trough_date": trough_date.isoformat(),
        # None means the drawdown had not been recovered by the end of the sample --
        # materially different from a fast recovery, and worth showing as such.
        "recovery_date": recovery_date.isoformat() if recovery_date is not None else None,
        "drawdown_days": int((trough_date - peak_date).days),
    }


def calmar_ratio(equity: pd.Series) -> float | None:
    """CAGR divided by the absolute maximum drawdown."""
    growth = cagr_pct(equity)
    worst = max_drawdown(equity)["max_drawdown_pct"]
    if growth is None or worst is None or worst == 0:
        return None
    return float(growth / abs(worst))


def exposure_pct(snapshots: list[dict]) -> float | None:
    """Average share of equity invested, across all bars.

    Low exposure with a good return is a different (and usually better) result than high
    exposure with the same return, because the idle capital was available for something
    else. Reporting return without exposure hides that.
    """
    if not snapshots:
        return None
    values = [s.get("exposure_pct", 0.0) for s in snapshots]
    return float(np.mean(values))


def turnover_pct(trades: list, equity: pd.Series) -> float | None:
    """Annualised traded notional as a percentage of average equity.

    The cost driver. A strategy with a small edge per trade and high turnover pays that
    edge to the broker, and this is the number that shows it.
    """
    if not trades or len(equity) < 2:
        return None

    traded = sum(t.entry_price * t.quantity + t.exit_price * t.quantity for t in trades)
    average_equity = float(equity.mean())
    if average_equity <= 0:
        return None

    days = max(1, (equity.index[-1] - equity.index[0]).days)
    years = days / 365.25
    if years <= 0:
        return None
    return float(traded / average_equity / years * 100.0)


# --------------------------------------------------------------------------- #
# Trade statistics
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TradeStats:
    n_trades: int
    win_rate_pct: float | None
    profit_factor: float | None
    average_win: float | None
    average_loss: float | None
    expectancy: float | None
    best_trade: float | None
    worst_trade: float | None
    average_holding_days: float | None
    average_bars_held: float | None
    n_wins: int
    n_losses: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_trades": self.n_trades,
            "n_wins": self.n_wins,
            "n_losses": self.n_losses,
            "win_rate_pct": self.win_rate_pct,
            "profit_factor": self.profit_factor,
            "average_win": self.average_win,
            "average_loss": self.average_loss,
            "expectancy": self.expectancy,
            "best_trade": self.best_trade,
            "worst_trade": self.worst_trade,
            "average_holding_days": self.average_holding_days,
            "average_bars_held": self.average_bars_held,
        }


def trade_statistics(trades: list) -> TradeStats:
    """Per-trade statistics, all computed on **net** P&L.

    Using gross P&L here is a common and flattering mistake: it inflates win rate and
    profit factor by exactly the friction the strategy actually paid.
    """
    if not trades:
        return TradeStats(0, None, None, None, None, None, None, None, None, None, 0, 0)

    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]

    gross_profit = float(wins.sum()) if wins.size else 0.0
    gross_loss = float(-losses.sum()) if losses.size else 0.0

    return TradeStats(
        n_trades=len(trades),
        n_wins=int(wins.size),
        n_losses=int(losses.size),
        win_rate_pct=float(wins.size / len(trades) * 100.0),
        # None rather than infinity when there were no losses: a sample with zero losing
        # trades has an undefined profit factor, and printing "inf" invites someone to
        # sort by it.
        profit_factor=float(gross_profit / gross_loss) if gross_loss > 0 else None,
        average_win=float(wins.mean()) if wins.size else None,
        average_loss=float(losses.mean()) if losses.size else None,
        # Realised average P&L per trade over this sample. NOT a forecast of the next
        # trade, despite the conventional name.
        expectancy=float(pnls.mean()),
        best_trade=float(pnls.max()),
        worst_trade=float(pnls.min()),
        average_holding_days=float(np.mean([t.holding_period_days for t in trades])),
        average_bars_held=float(np.mean([t.bars_held for t in trades])),
    )


# --------------------------------------------------------------------------- #
# Period breakdowns
# --------------------------------------------------------------------------- #


def monthly_returns(equity: pd.Series) -> pd.Series:
    """Month-end percentage returns of the equity curve."""
    if len(equity) < 2:
        return pd.Series(dtype=float)
    monthly = equity.resample("ME").last()
    return (monthly.pct_change(fill_method=None).dropna() * 100.0).rename("monthly_return_pct")


def annual_returns(equity: pd.Series) -> pd.Series:
    """Calendar-year percentage returns.

    The first and last years are almost always partial. They are not annualised, and a
    report must label them as partial rather than comparing them with full years.
    """
    if len(equity) < 2:
        return pd.Series(dtype=float)
    yearly = equity.resample("YE").last()
    first = pd.Series([equity.iloc[0]], index=[equity.index[0]])
    combined = pd.concat([first, yearly])
    return (combined.pct_change(fill_method=None).dropna() * 100.0).rename("annual_return_pct")


def rolling_sharpe(
    equity: pd.Series,
    window: int = 126,
    *,
    risk_free_rate: float = 0.0,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
) -> pd.Series:
    """Rolling annualised Sharpe over ``window`` bars (default ~6 months).

    More informative than a single number: a strategy whose Sharpe was 2.0 for three
    years and -0.5 since is not a Sharpe-1.0 strategy.
    """
    returns = _returns(equity)
    if len(returns) < window:
        return pd.Series(dtype=float)

    period_rf = risk_free_rate / periods_per_year
    excess = returns - period_rf
    mean = excess.rolling(window, min_periods=window).mean()
    std = returns.rolling(window, min_periods=window).std(ddof=1)
    return (mean / std * math.sqrt(periods_per_year)).rename("rolling_sharpe")


# --------------------------------------------------------------------------- #
# Top level
# --------------------------------------------------------------------------- #


def compute_metrics(
    equity: pd.Series,
    trades: list,
    snapshots: list[dict] | None = None,
    *,
    risk_free_rate: float = 0.0,
    periods_per_year: float = TRADING_DAYS_PER_YEAR,
) -> dict[str, Any]:
    """Every metric from one run, as a JSON-serialisable dict.

    ``None`` appears wherever a metric is genuinely undefined for the sample — too few
    bars, zero volatility, no losing trades. That is deliberate: a null is honest, a
    zero would be read as a measurement.
    """
    if equity.empty:
        return {"error": "empty equity curve", "n_trades": len(trades)}

    stats = trade_statistics(trades)
    drawdown = max_drawdown(equity)

    metrics: dict[str, Any] = {
        "initial_equity": float(equity.iloc[0]),
        "final_equity": float(equity.iloc[-1]),
        "total_return_pct": total_return_pct(equity),
        "cagr_pct": cagr_pct(equity),
        "annualised_volatility_pct": annualised_volatility_pct(equity, periods_per_year),
        "sharpe": sharpe_ratio(
            equity, risk_free_rate=risk_free_rate, periods_per_year=periods_per_year
        ),
        "sortino": sortino_ratio(
            equity, risk_free_rate=risk_free_rate, periods_per_year=periods_per_year
        ),
        "calmar": calmar_ratio(equity),
        "exposure_pct": exposure_pct(snapshots or []),
        "turnover_pct": turnover_pct(trades, equity),
        "n_bars": int(len(equity)),
        "sample_days": int((equity.index[-1] - equity.index[0]).days),
        "risk_free_rate_used": risk_free_rate,
        **drawdown,
        **stats.to_dict(),
    }

    if metrics["cagr_pct"] is None and metrics["sample_days"] < MIN_DAYS_FOR_CAGR:
        metrics["cagr_note"] = (
            f"Sample is {metrics['sample_days']} days, below the {MIN_DAYS_FOR_CAGR}-day "
            "minimum for annualising. Annualising a short sample produces a large number "
            "that means nothing."
        )

    return metrics


def compute_benchmark_metrics(
    benchmark_close: pd.Series,
    equity_index: pd.DatetimeIndex,
    initial_capital: float,
    *,
    risk_free_rate: float = 0.0,
    label: str = "",
    caveats: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Buy-and-hold metrics for a benchmark, on the strategy's own calendar.

    The benchmark is reindexed onto ``equity_index`` and forward-filled, so both curves
    are measured over identical periods. Comparing a strategy's 2,500 daily observations
    against a benchmark's 2,510 would make the two return figures span different
    windows.

    ``caveats`` travels with the result, because for Chile the benchmark is a
    USD-denominated proxy and the comparison is not apples-to-apples.
    """
    if benchmark_close.empty or len(equity_index) < 2:
        return {"available": False, "reason": "no benchmark data", "label": label}

    # Require the two ranges to actually intersect before aligning. Without this check,
    # forward-filling carries the benchmark's last observation across an arbitrary gap:
    # a series ending in 2010 compared against a 2024 strategy yields a perfectly flat
    # benchmark with zero return and zero volatility, against which any positive return
    # looks like alpha. A null benchmark is safe; an invented one is not.
    benchmark_start, benchmark_end = benchmark_close.index.min(), benchmark_close.index.max()
    equity_start, equity_end = equity_index.min(), equity_index.max()

    if benchmark_end < equity_start or benchmark_start > equity_end:
        return {
            "available": False,
            "reason": (
                f"benchmark covers {benchmark_start.date()} to {benchmark_end.date()}, "
                f"which does not intersect the strategy's {equity_start.date()} to "
                f"{equity_end.date()}. Forward-filling across that gap would fabricate "
                "a flat benchmark."
            ),
            "label": label,
        }

    aligned = benchmark_close.reindex(
        benchmark_close.index.union(equity_index)
    ).ffill().reindex(equity_index).dropna()

    if len(aligned) < 2:
        return {
            "available": False,
            "reason": (
                "benchmark does not overlap the strategy's date range; with a US-listed "
                "proxy for a Chilean strategy the trading calendars differ"
            ),
            "label": label,
        }

    # Partial coverage is usable but must be visible: a benchmark that only spans half
    # the strategy's history produces a return measured over a different window.
    covered = benchmark_close[
        (benchmark_close.index >= equity_start) & (benchmark_close.index <= equity_end)
    ]
    coverage_fraction = len(covered) / len(equity_index) if len(equity_index) else 0.0

    curve = (aligned / aligned.iloc[0]) * initial_capital
    metrics = compute_metrics(curve, trades=[], risk_free_rate=risk_free_rate)
    all_caveats = list(caveats)
    if coverage_fraction < 0.9:
        all_caveats.append(
            f"The benchmark has its own bars on only {coverage_fraction:.0%} of the "
            "strategy's sessions; the rest were forward-filled, which understates its "
            "volatility and drawdown."
        )

    metrics.update(
        {
            "available": True,
            "label": label,
            "caveats": all_caveats,
            "n_benchmark_bars": int(len(aligned)),
            "benchmark_own_bars": int(len(covered)),
            "coverage_fraction": round(coverage_fraction, 4),
        }
    )
    return metrics


def compare_to_benchmark(
    strategy_metrics: dict[str, Any], benchmark_metrics: dict[str, Any]
) -> dict[str, Any]:
    """Differences on the headline figures, with nulls preserved.

    Subtracting a ``None`` yields ``None`` rather than treating the missing value as
    zero, which would report a difference that was never measured.
    """
    if not benchmark_metrics.get("available"):
        return {"available": False, "reason": benchmark_metrics.get("reason", "unavailable")}

    def delta(key: str) -> float | None:
        a, b = strategy_metrics.get(key), benchmark_metrics.get(key)
        if a is None or b is None:
            return None
        return float(a - b)

    return {
        "available": True,
        "excess_total_return_pct": delta("total_return_pct"),
        "excess_cagr_pct": delta("cagr_pct"),
        "sharpe_difference": delta("sharpe"),
        "sortino_difference": delta("sortino"),
        # A less negative drawdown is better, so a positive value here means the
        # strategy drew down less than the benchmark.
        "drawdown_difference_pct": delta("max_drawdown_pct"),
        "volatility_difference_pct": delta("annualised_volatility_pct"),
    }
