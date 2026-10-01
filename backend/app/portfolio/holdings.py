"""Recording what was actually bought and sold, and what it actually made.

The workflow this serves is manual on both ends. The user buys through their broker's app, tells
this system what they bought, and the system watches the strategy's exit rule against it. When
the rule fires the user is told; if they sell, they tell the system that too, and the P&L
computed here is from the prices they actually got.

Nothing in this module places an order or can. That is not a limitation waiting to be lifted --
it is the design. See ``app/execution`` for the adapter interface a future broker integration
would implement.

Two deliberate refusals
-----------------------
**No implied position size.** ``open_holding`` takes the quantity that was bought. It does not
suggest one. The backtester's risk sizing is calibrated on modelled fills with no market impact,
and on the thin instruments in this universe -- CCU at 1.7M USD a day -- that calibration is
optimistic in a way nothing here measures.

**No entry price inference.** The user supplies what they paid. Filling it from the bar's close
would silently replace a real number with a modelled one, which is the exact confusion this
table exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.universe import find_asset
from app.database import repository as repo
from app.database.models import Holding

logger = get_logger(__name__)

__all__ = [
    "HoldingSummary",
    "open_holding",
    "close_holding",
    "get_holding",
    "list_holdings",
    "open_holdings",
    "realised_performance",
    "export_positions",
    "import_positions",
    "POSITIONS_FILE",
]


def _as_utc(value: date | datetime) -> datetime:
    """Normalise to a timezone-aware UTC datetime.

    Dates become midnight UTC. The schema's convention is UTC throughout, and a naive local
    datetime mixed in here is the fastest route to an off-by-one-session comparison against a
    bar index.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class HoldingSummary:
    """A holding plus whatever the latest stored bar says about it.

    ``unrealised_pnl`` is deliberately marked as *gross*: the exit's fees are not knowable
    until the exit happens, and this project's cost model is a placeholder in any case.
    """

    id: int
    symbol: str
    region: str
    currency: str
    quantity: float
    entry_price: float
    opened_on: datetime
    strategy_name: str
    stop_price: float | None
    take_profit_price: float | None
    broker: str
    entry_note: str

    last_price: float | None = None
    last_bar_on: datetime | None = None
    unrealised_pnl_gross: float | None = None
    unrealised_pnl_pct: float | None = None
    exit_signal_on: datetime | None = None
    exit_signal_reason: str = ""
    last_checked_on: datetime | None = None
    liquidity_caveat: str = ""

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.quantity

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "region": self.region,
            "currency": self.currency,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "cost_basis": round(self.cost_basis, 2),
            "opened_on": self.opened_on.date().isoformat(),
            "strategy": self.strategy_name,
            "stop_price": self.stop_price,
            "take_profit_price": self.take_profit_price,
            "broker": self.broker,
            "entry_note": self.entry_note,
            "last_price": self.last_price,
            "last_bar_on": self.last_bar_on.date().isoformat() if self.last_bar_on else None,
            "unrealised_pnl_gross": (
                round(self.unrealised_pnl_gross, 2)
                if self.unrealised_pnl_gross is not None
                else None
            ),
            "unrealised_pnl_pct": (
                round(self.unrealised_pnl_pct, 2)
                if self.unrealised_pnl_pct is not None
                else None
            ),
            "exit_signal_on": (
                self.exit_signal_on.date().isoformat() if self.exit_signal_on else None
            ),
            "exit_signal_reason": self.exit_signal_reason,
            "last_checked_on": (
                self.last_checked_on.date().isoformat() if self.last_checked_on else None
            ),
            "liquidity_caveat": self.liquidity_caveat,
        }


def open_holding(
    session: Session,
    symbol: str,
    quantity: float,
    entry_price: float,
    *,
    opened_on: date | datetime | None = None,
    strategy_name: str = "trend_momentum",
    strategy_params: dict[str, Any] | None = None,
    stop_price: float | None = None,
    take_profit_price: float | None = None,
    entry_fees: float = 0.0,
    broker: str = "",
    note: str = "",
    market: str | None = None,
) -> Holding:
    """Record a purchase the user already made.

    ``symbol`` must be in the declared universe. That is a real constraint rather than
    defensiveness: the watch needs stored bars and a features pipeline for the instrument, and
    an unknown symbol would be recorded and then never checked -- the worst possible outcome
    for a position the user believes is being watched.

    Raises ``ValueError`` for a second open holding in the same symbol. Two open positions in
    one instrument need average-cost or lot-level accounting to report P&L correctly, and this
    has neither; silently summing them would misreport every number downstream.
    """
    if quantity <= 0:
        raise ValueError(f"Quantity must be positive, got {quantity}")
    if entry_price <= 0:
        raise ValueError(f"Entry price must be positive, got {entry_price}")

    spec = find_asset(symbol, market)
    asset = repo.get_asset(session, spec.symbol, spec.market)
    if asset is None:
        repo.upsert_asset(session, spec)
        session.flush()
        asset = repo.get_asset(session, spec.symbol, spec.market)
    assert asset is not None

    existing = session.scalar(
        select(Holding).where(
            Holding.asset_id == asset.id, Holding.closed_on.is_(None)
        )
    )
    if existing is not None:
        raise ValueError(
            f"{spec.symbol} already has an open holding (id {existing.id}, "
            f"{existing.quantity:g} @ {existing.entry_price:g} from "
            f"{existing.opened_on.date()}). Close it before opening another: this system "
            "has no lot-level accounting, so two open lots in one instrument would make "
            "every P&L figure for it wrong."
        )

    if stop_price is not None and stop_price >= entry_price:
        raise ValueError(
            f"Stop {stop_price:g} is at or above the entry price {entry_price:g}, so it "
            "would trigger immediately. A stop above entry on a long position is almost "
            "always a typo."
        )
    if take_profit_price is not None and take_profit_price <= entry_price:
        raise ValueError(
            f"Take-profit {take_profit_price:g} is at or below the entry price "
            f"{entry_price:g}, so it would trigger immediately."
        )

    holding = Holding(
        asset_id=asset.id,
        symbol=spec.symbol,
        market=spec.market,
        currency=spec.currency,
        region=spec.region,
        broker=broker,
        opened_on=_as_utc(opened_on or datetime.now(timezone.utc)),
        entry_price=float(entry_price),
        quantity=float(quantity),
        entry_fees=float(entry_fees),
        strategy_name=strategy_name,
        strategy_params=strategy_params,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        entry_note=note,
    )
    session.add(holding)
    session.flush()
    logger.info(
        "Recorded holding %s: %g %s @ %g", holding.id, quantity, spec.symbol, entry_price
    )
    return holding


def close_holding(
    session: Session,
    holding_id: int,
    exit_price: float,
    *,
    closed_on: date | datetime | None = None,
    exit_fees: float = 0.0,
    note: str = "",
) -> Holding:
    """Record a sale and compute the real P&L.

    The figures written here are the only ones in this project that are not modelled. Every
    other performance number comes from a backtest with an assumed fill, an assumed commission
    and no market impact. These come from what the user was actually paid.
    """
    holding = session.get(Holding, holding_id)
    if holding is None:
        raise KeyError(f"No holding with id {holding_id}")
    if holding.closed_on is not None:
        raise ValueError(
            f"Holding {holding_id} ({holding.symbol}) was already closed on "
            f"{holding.closed_on.date()} at {holding.exit_price:g}."
        )
    if exit_price <= 0:
        raise ValueError(f"Exit price must be positive, got {exit_price}")

    closed = _as_utc(closed_on or datetime.now(timezone.utc))
    # On dates, not datetimes: SQLite does not round-trip the offset, so `opened_on` comes back
    # naive and comparing it against an aware value raises. Both are day-granularity facts
    # anyway -- nobody records the minute they sold.
    if closed.date() < holding.opened_on.date():
        raise ValueError(
            f"Exit date {closed.date()} is before the entry date "
            f"{holding.opened_on.date()}."
        )

    gross = (exit_price - holding.entry_price) * holding.quantity
    fees = holding.entry_fees + float(exit_fees)

    holding.closed_on = closed
    holding.exit_price = float(exit_price)
    holding.exit_fees = float(exit_fees)
    holding.gross_pnl = gross
    holding.pnl = gross - fees
    # Percent is on the cost basis including entry fees, which is what the money actually
    # committed was. Dividing by entry_price alone would flatter every trade by the fee.
    basis = holding.entry_price * holding.quantity + holding.entry_fees
    holding.pnl_pct = (holding.pnl / basis) * 100.0 if basis else None
    holding.holding_period_days = (closed.date() - holding.opened_on.date()).days
    holding.exit_note = note

    session.flush()
    logger.info(
        "Closed holding %s (%s): net %.2f %s over %s days",
        holding.id,
        holding.symbol,
        holding.pnl,
        holding.currency,
        holding.holding_period_days,
    )
    return holding


def get_holding(session: Session, holding_id: int) -> Holding | None:
    return session.get(Holding, holding_id)


def list_holdings(session: Session, *, include_closed: bool = True) -> list[Holding]:
    stmt = select(Holding).order_by(Holding.opened_on.desc(), Holding.id.desc())
    if not include_closed:
        stmt = stmt.where(Holding.closed_on.is_(None))
    return list(session.scalars(stmt))


def open_holdings(session: Session) -> list[Holding]:
    return list_holdings(session, include_closed=False)


def realised_performance(session: Session) -> dict[str, Any]:
    """What the closed holdings actually did.

    Reported without a Sharpe ratio, a win rate presented as an edge, or an annualised return.
    With a handful of manual trades none of those means anything: a win rate over four trades
    has a confidence interval wide enough to contain every hypothesis, and annualising a
    two-month sample multiplies noise by six.

    What is here is arithmetic on real money, plus a statement of how little it establishes.
    """
    closed = [h for h in list_holdings(session) if h.closed_on is not None]
    if not closed:
        return {
            "n_closed": 0,
            "evidence": (
                "No closed holdings yet. Nothing here can say whether this strategy works "
                "for you."
            ),
        }

    pnls = [h.pnl for h in closed if h.pnl is not None]
    wins = [p for p in pnls if p > 0]
    total = sum(pnls)
    by_currency: dict[str, float] = {}
    for holding in closed:
        if holding.pnl is not None:
            by_currency[holding.currency] = by_currency.get(holding.currency, 0.0) + holding.pnl

    return {
        "n_closed": len(closed),
        "net_pnl": round(total, 2),
        "net_pnl_by_currency": {k: round(v, 2) for k, v in by_currency.items()},
        "n_winners": len(wins),
        "n_losers": len(pnls) - len(wins),
        "best": round(max(pnls), 2) if pnls else None,
        "worst": round(min(pnls), 2) if pnls else None,
        "median_holding_days": (
            sorted(h.holding_period_days for h in closed if h.holding_period_days is not None)[
                len(closed) // 2
            ]
            if any(h.holding_period_days is not None for h in closed)
            else None
        ),
        "total_fees": round(sum(h.entry_fees + h.exit_fees for h in closed), 2),
        "evidence": _evidence_statement(len(closed), len(wins)),
    }


def _evidence_statement(n_closed: int, n_winners: int) -> str:
    """How much these results establish. Usually: nothing.

    This exists because the numbers above are seductive in a way backtest numbers are not --
    they are real money, so they feel like proof. Four real trades are a smaller sample than
    any backtest in this project and establish less, not more.
    """
    if n_closed < 5:
        return (
            f"{n_closed} closed trade(s). This is far too few to indicate anything about the "
            "strategy. A run of winners or losers this short is what randomness looks like. "
            "These figures record what happened to your money; they are not evidence."
        )
    if n_closed < 30:
        return (
            f"{n_closed} closed trades, {n_winners} of them profitable. Still too few to "
            "separate skill from luck: at this sample size the confidence interval around a "
            "win rate spans most of the range between 'useless' and 'good'. Treat it as a "
            "record, not a verdict."
        )
    return (
        f"{n_closed} closed trades, {n_winners} profitable. Enough to be worth comparing "
        "against what the backtest predicted over the same period -- and if the two disagree, "
        "the backtest is the one to distrust. Note that this sample is not independent of the "
        "backtest: the strategy was chosen because it looked good on that history."
    )


# --------------------------------------------------------------------------- #
# Durable export: the only state no provider can rebuild
# --------------------------------------------------------------------------- #

POSITIONS_FILE = "data/positions.json"
"""Where the portable copy of the holdings lives.

Everything else in the database can be re-downloaded in minutes. These cannot: no provider knows
what the user bought. The GitHub Actions cache that normally carries the database is evicted after
about a week of disuse, so the holdings also live in a small committed file that survives it.

JSON rather than the SQLite file itself: a few hundred bytes instead of ten megabytes, diffable in
a pull request, and readable by a human who wants to check what the system thinks they own.
"""


def export_positions(session: Session) -> list[dict[str, Any]]:
    """Every holding as plain data, ordered so the output is stable across runs.

    Stable ordering matters because this gets committed: an unstable order would produce a diff on
    every run and make a real change impossible to spot.
    """
    rows = []
    for holding in sorted(list_holdings(session), key=lambda h: (h.opened_on, h.symbol)):
        rows.append(
            {
                "symbol": holding.symbol,
                "market": holding.market,
                "quantity": holding.quantity,
                "entry_price": holding.entry_price,
                "opened_on": holding.opened_on.date().isoformat(),
                "entry_fees": holding.entry_fees,
                "strategy_name": holding.strategy_name,
                "strategy_params": holding.strategy_params,
                "stop_price": holding.stop_price,
                "take_profit_price": holding.take_profit_price,
                "broker": holding.broker,
                "entry_note": holding.entry_note,
                "closed_on": (
                    holding.closed_on.date().isoformat() if holding.closed_on else None
                ),
                "exit_price": holding.exit_price,
                "exit_fees": holding.exit_fees,
                "exit_note": holding.exit_note,
            }
        )
    return rows


def import_positions(session: Session, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Recreate holdings from an export. Idempotent.

    Idempotent because the daily job runs this every time: a holding already present, matched on
    symbol and entry date, is skipped rather than duplicated. Without that, a week of scheduled
    runs would turn one position into seven and every P&L figure derived from them would be wrong.

    Rows that fail validation are counted and skipped rather than aborting the import, so one bad
    record cannot cost the user every other position.
    """
    existing = {
        (h.symbol, h.opened_on.date().isoformat()) for h in list_holdings(session)
    }
    result = {"added": 0, "skipped": 0, "failed": 0}

    for row in rows:
        key = (row.get("symbol"), row.get("opened_on"))
        if key in existing:
            result["skipped"] += 1
            continue
        try:
            holding = open_holding(
                session,
                row["symbol"],
                float(row["quantity"]),
                float(row["entry_price"]),
                opened_on=date.fromisoformat(row["opened_on"]),
                strategy_name=row.get("strategy_name") or "trend_momentum",
                strategy_params=row.get("strategy_params"),
                stop_price=row.get("stop_price"),
                take_profit_price=row.get("take_profit_price"),
                entry_fees=float(row.get("entry_fees") or 0.0),
                broker=row.get("broker") or "",
                note=row.get("entry_note") or "",
                market=row.get("market"),
            )
            if row.get("closed_on") and row.get("exit_price"):
                close_holding(
                    session,
                    holding.id,
                    float(row["exit_price"]),
                    closed_on=date.fromisoformat(row["closed_on"]),
                    exit_fees=float(row.get("exit_fees") or 0.0),
                    note=row.get("exit_note") or "",
                )
            result["added"] += 1
            existing.add(key)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Skipped position %s: %s", row.get("symbol"), exc)
            result["failed"] += 1

    return result
