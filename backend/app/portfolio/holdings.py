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
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.universe import find_asset
from app.database import repository as repo
from app.database.models import Holding

logger = get_logger(__name__)

FX_SYMBOLS = {"CLP": "USDCLP=X"}
"""Yahoo tickers for the currencies a purchase may be denominated in.

USD needs no entry: the instruments are all USD-denominated, so a USD amount converts by 1.
"""


class PriceUnavailableError(RuntimeError):
    """No price could be established, so no share count can be derived.

    Raised rather than defaulted, because every available default is a lie: zero shares records
    nothing, one share records something the user did not do, and an arbitrary price produces a
    P&L that looks real.
    """


__all__ = [
    "HoldingSummary",
    "FX_SYMBOLS",
    "PriceUnavailableError",
    "resolve_quantity",
    "assess_entry",
    "EntryQuality",
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


def latest_close(session: Session, symbol: str, market: str | None = None) -> float | None:
    """The newest stored close for an instrument, or None if nothing is stored.

    Deliberately not a live quote: this project has no real-time feed, and pretending otherwise
    would make an estimated entry price look better than it is.
    """
    from app.data.engine import DataEngine

    try:
        bars = DataEngine(session).load(symbol, market, trim_carried_forward=True)
    except (KeyError, LookupError):
        return None
    if bars.empty or bars["close"].dropna().empty:
        return None
    return float(bars["close"].dropna().iloc[-1])


def fx_rate_to_usd(session: Session, currency: str) -> float | None:
    """Units of ``currency`` per USD, from the day's close. None if unavailable.

    A daily close, not the rate a broker applied at the moment of the trade. The difference is
    typically small and occasionally is not; it is stored on the holding so it can be corrected
    rather than argued about.
    """
    currency = currency.upper()
    if currency == "USD":
        return 1.0

    ticker = FX_SYMBOLS.get(currency)
    if ticker is None:
        return None

    from app.data.provider import Timeframe
    from app.data.registry import get_provider

    try:
        frame = get_provider("yfinance").fetch_bars(
            ticker,
            Timeframe.D1,
            start=datetime.now(timezone.utc) - timedelta(days=14),
            end=datetime.now(timezone.utc),
        )
    except Exception as exc:  # provider failures are expected and must not abort a recording
        logger.warning("Could not fetch %s: %s", ticker, exc)
        return None
    if frame.empty or frame["close"].dropna().empty:
        return None
    return float(frame["close"].dropna().iloc[-1])


@dataclass(frozen=True, slots=True)
class ResolvedEntry:
    """What a cash amount works out to, and what had to be assumed to get there."""

    quantity: float
    price: float
    price_estimated: bool
    amount: float | None
    currency: str
    fx_rate: float | None
    amount_usd: float | None

    def describe(self) -> str:
        """One line the CLI and the workflow summary can both print verbatim."""
        parts = []
        if self.amount is not None:
            if self.currency != "USD":
                parts.append(
                    f"{self.amount:,.2f} {self.currency} at {self.fx_rate:,.2f} per USD "
                    f"= {self.amount_usd:,.2f} USD"
                )
            else:
                parts.append(f"{self.amount:,.2f} USD")
        parts.append(
            f"{self.quantity:.6f} shares at {self.price:,.4f}"
            + (" (ESTIMATED from the latest close)" if self.price_estimated else "")
        )
        return " -> ".join(parts)


def resolve_quantity(
    session: Session,
    symbol: str,
    *,
    quantity: float | None = None,
    amount: float | None = None,
    price: float | None = None,
    currency: str = "USD",
    market: str | None = None,
) -> ResolvedEntry:
    """Work out shares and price from whatever the user supplied.

    Accepts a share count or a cash amount, never both -- a caller who gave both meant one of
    them, and silently preferring one would record a position they did not take.

    When ``price`` is omitted the latest stored close is used and the result is flagged. That
    flag travels with the holding forever, because a P&L computed against an assumed entry is
    not the same kind of number as one computed against a real fill.
    """
    if (quantity is None) == (amount is None):
        raise ValueError(
            "Give either a share quantity or a cash amount, not both and not neither. "
            "A cash amount is the usual case for a fractional purchase."
        )

    currency = (currency or "USD").upper()
    estimated = price is None
    if price is None:
        price = latest_close(session, symbol, market)
        if price is None:
            raise PriceUnavailableError(
                f"No stored price for {symbol} and none supplied, so the share count cannot be "
                f"derived. Pass the price you paid, or run "
                f"`python -m app download-data --symbols {symbol}` first."
            )
    if price <= 0:
        raise ValueError(f"Price must be positive, got {price}")

    if quantity is not None:
        return ResolvedEntry(
            quantity=float(quantity),
            price=float(price),
            price_estimated=estimated,
            amount=None,
            currency="USD",
            fx_rate=None,
            amount_usd=None,
        )

    if amount is None or amount <= 0:
        raise ValueError(f"Amount must be positive, got {amount}")

    rate = fx_rate_to_usd(session, currency)
    if rate is None:
        raise ValueError(
            f"No exchange rate available for {currency}. Supported: "
            f"{', '.join(['USD', *FX_SYMBOLS])}. Record the amount in USD instead."
        )

    amount_usd = float(amount) / rate
    return ResolvedEntry(
        quantity=amount_usd / float(price),
        price=float(price),
        price_estimated=estimated,
        amount=float(amount),
        currency=currency,
        fx_rate=rate,
        amount_usd=amount_usd,
    )



RR_DEGRADATION_THRESHOLD = 0.25
"""Relative drop in reward-to-risk that is worth interrupting the user over.

A quarter. Below that the difference is inside the noise of where a stop would sit anyway; above
it, the trade being taken is visibly not the trade that was signalled.
"""

RR_FLOOR = 2.0
"""Falling under this having started above it is reported regardless of the relative drop."""


@dataclass(frozen=True, slots=True)
class EntryQuality:
    """How the price paid compares with the price the levels were derived from."""

    paid: float
    reference: float
    stop: float
    target: float

    @property
    def slippage_pct(self) -> float:
        return (self.paid / self.reference - 1.0) * 100.0

    @property
    def reward_risk_signalled(self) -> float | None:
        risk = self.reference - self.stop
        return (self.target - self.reference) / risk if risk > 0 else None

    @property
    def reward_risk_paid(self) -> float | None:
        risk = self.paid - self.stop
        return (self.target - self.paid) / risk if risk > 0 else None

    @property
    def is_material(self) -> bool:
        """Whether the change is worth interrupting over."""
        before, after = self.reward_risk_signalled, self.reward_risk_paid
        if before is None or after is None or before <= 0:
            return True  # the stop is at or above the fill: always worth saying
        if after < RR_FLOOR <= before:
            return True
        return (before - after) / before >= RR_DEGRADATION_THRESHOLD

    def describe(self) -> str:
        """Plain text for the CLI, the workflow summary and the alert. ASCII only."""
        before, after = self.reward_risk_signalled, self.reward_risk_paid
        if after is not None and after <= 0:
            return (
                f"You paid {self.paid:,.2f}, which is at or above the target {self.target:,.2f}, "
                "or at or below the stop. The levels from the signal do not describe this "
                "position at all."
            )
        lines = [
            f"You paid {self.paid:,.2f}; the signal was computed from {self.reference:,.2f} "
            f"({self.slippage_pct:+.2f}%).",
            f"Risk to the stop {self.stop:,.2f} is now "
            f"{(self.paid - self.stop) / self.paid * 100:.2f}% and reward to the target "
            f"{self.target:,.2f} is {(self.target - self.paid) / self.paid * 100:.2f}%.",
        ]
        if before and after:
            lines.append(
                f"Reward-to-risk {before:.2f}x at the signal price, {after:.2f}x at yours."
            )
        lines.append(
            "The levels do not move with your fill -- the stop is where the strategy's "
            "volatility estimate puts it -- so a worse entry costs the whole difference. "
            "Whether this is still worth holding is your call; nothing here can answer it."
        )
        return " ".join(lines)


def assess_entry(
    paid: float, reference: float, stop: float | None, target: float | None
) -> EntryQuality | None:
    """Compare a fill against the price its levels were derived from.

    Returns None when there is nothing to compare -- no levels, or no reference price -- rather
    than inventing a baseline.
    """
    if not stop or not target or not reference or reference <= 0:
        return None
    return EntryQuality(paid=float(paid), reference=float(reference), stop=stop, target=target)


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
    entry_amount: float | None = None,
    entry_amount_currency: str = "USD",
    entry_fx_rate: float | None = None,
    entry_price_estimated: bool = False,
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
        entry_amount=entry_amount,
        entry_amount_currency=(entry_amount_currency or "USD").upper(),
        entry_fx_rate=entry_fx_rate,
        entry_price_estimated=bool(entry_price_estimated),
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
                "entry_amount": holding.entry_amount,
                "entry_amount_currency": holding.entry_amount_currency,
                "entry_fx_rate": holding.entry_fx_rate,
                "entry_price_estimated": holding.entry_price_estimated,
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
                entry_amount=row.get("entry_amount"),
                entry_amount_currency=row.get("entry_amount_currency") or "USD",
                entry_fx_rate=row.get("entry_fx_rate"),
                entry_price_estimated=bool(row.get("entry_price_estimated")),
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
