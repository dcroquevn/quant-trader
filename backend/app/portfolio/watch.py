"""The daily check: does the strategy's exit rule now fire on anything the user holds?

This is the whole point of Phase 6. The user buys manually, this runs once a day, and when the
rule that the backtest measured says exit, the user gets told -- with the reason, so they can
disagree.

Why it replays instead of just asking the strategy
--------------------------------------------------
A position's exit does not depend only on today's bar. A trailing stop depends on the highest
close *since entry*, so the level that binds today is a function of every bar in between. Asking
the strategy about today alone would miss it, and hard-coding a fresh stop from today's ATR
would be checking a different rule than the one the backtest measured.

So the watch rebuilds the position: it constructs the same
:class:`~app.backtesting.portfolio.Position` the backtester would hold and replays every bar from
entry forward through the same ``update_marks``, ``advance_trailing_stop`` and
:meth:`~app.backtesting.engine.Backtester._resolve_price_exit`, **stopping at the first bar that
triggers an exit**. Reusing those rather than reimplementing them is deliberate: an alert that
fires on slightly different logic than the backtest measured is an alert with no evidence behind
it.

Why it stops rather than checking only today
--------------------------------------------
An earlier version checked the latest bar alone. A position opened in March 2025 with a
take-profit six ATR above entry had that level crossed months later, and the watch reported "the
take-profit level was reached" -- which reads as *today*. The backtester would have closed that
position when it happened; the one the watch described had not existed for a year. It also kept
marking excursions past the exit, so the "best point while held" covered a year the position
would not have been open for.

The trigger date is now reported, along with how many sessions ago it was, because an exit that
fired this morning and one that fired eleven months ago call for different reactions and only one
of them is news.

What this does not claim
------------------------
An alert means *the exit rule fired*. It does not mean the price will fall, that selling is
correct, or that the strategy is right. The message says so in those words, because a
notification is read in three seconds on a phone and whatever it implies is what the user will
act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtesting.engine import (
    EXIT_STOP,
    EXIT_TARGET,
    EXIT_TRAILING,
    Backtester,
)
from app.backtesting.portfolio import Position
from app.backtesting.runner import WARMUP_CALENDAR_DAYS, load_features
from app.core.exceptions import InsufficientDataError
from app.core.logging import get_logger
from app.core.universe import find_asset
from app.database.models import Alert, Holding
from app.notifications.base import Notification, Notifier
from app.portfolio.holdings import open_holdings
from app.strategies.base import Action, Strategy
from app.strategies.registry import build_strategy

logger = get_logger(__name__)

__all__ = [
    "WatchOutcome",
    "WatchReport",
    "check_holdings",
    "run_watch",
    "EXIT_SIGNAL",
    "STALE_DATA_DAYS",
]

EXIT_SIGNAL = "strategy_signal"
"""Reason code for an exit the strategy asked for, as opposed to a price level being hit."""

STALE_DATA_DAYS = 5
"""How old the latest stored bar may be before the watch refuses to draw a conclusion.

A silent "no exit signal" computed from week-old data is the most dangerous output this module
could produce: the user would read it as "nothing has happened" when it means "I do not know".
"""


@dataclass(slots=True)
class WatchOutcome:
    """What the exit rule says about one holding today."""

    holding_id: int
    symbol: str
    region: str
    quantity: float
    entry_price: float
    opened_on: date

    exit_triggered: bool = False
    exit_reason: str = ""
    """One of the engine's reason codes, or :data:`EXIT_SIGNAL`."""

    reference_price: float | None = None
    """Where the rule says the exit would have happened. Not a price the user will get."""

    exit_triggered_on: date | None = None
    """The bar the exit rule first fired on. Not necessarily the latest bar."""

    sessions_since_trigger: int | None = None
    """Stored bars between the trigger and the latest one.

    Zero means it fired on the most recent bar. Anything larger means the rule fired earlier
    and nothing acted on it -- either the watch was not running or the user chose to hold.
    """

    last_price: float | None = None
    last_bar_on: date | None = None
    unrealised_pnl_gross: float | None = None
    unrealised_pnl_pct: float | None = None

    effective_stop: float | None = None
    take_profit_price: float | None = None
    decision_action: str = ""
    decision_reasons: tuple[str, ...] = ()
    score_description: str = ""

    max_adverse_excursion_pct: float | None = None
    max_favorable_excursion_pct: float | None = None
    bars_held: int = 0

    usable: bool = True
    problem: str = ""
    """Why no conclusion could be drawn. Non-empty means ``exit_triggered`` is meaningless."""

    liquidity_caveat: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "holding_id": self.holding_id,
            "symbol": self.symbol,
            "region": self.region,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "opened_on": self.opened_on.isoformat(),
            "exit_triggered": self.exit_triggered,
            "exit_reason": self.exit_reason,
            "reference_price": self.reference_price,
            "exit_triggered_on": (
                self.exit_triggered_on.isoformat() if self.exit_triggered_on else None
            ),
            "sessions_since_trigger": self.sessions_since_trigger,
            "last_price": self.last_price,
            "last_bar_on": self.last_bar_on.isoformat() if self.last_bar_on else None,
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
            "effective_stop": self.effective_stop,
            "take_profit_price": self.take_profit_price,
            "decision_action": self.decision_action,
            "decision_reasons": list(self.decision_reasons),
            "score_description": self.score_description,
            "max_adverse_excursion_pct": (
                round(self.max_adverse_excursion_pct, 2)
                if self.max_adverse_excursion_pct is not None
                else None
            ),
            "max_favorable_excursion_pct": (
                round(self.max_favorable_excursion_pct, 2)
                if self.max_favorable_excursion_pct is not None
                else None
            ),
            "bars_held": self.bars_held,
            "usable": self.usable,
            "problem": self.problem,
            "liquidity_caveat": self.liquidity_caveat,
        }


@dataclass(slots=True)
class WatchReport:
    """Everything one watch run concluded, and what it could not conclude."""

    ran_at: datetime
    outcomes: list[WatchOutcome] = field(default_factory=list)
    notifications_sent: int = 0
    notifications_failed: int = 0
    notifications_suppressed: int = 0
    channel: str = ""

    @property
    def triggered(self) -> list[WatchOutcome]:
        return [o for o in self.outcomes if o.usable and o.exit_triggered]

    @property
    def unusable(self) -> list[WatchOutcome]:
        return [o for o in self.outcomes if not o.usable]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran_at": self.ran_at.isoformat(),
            "n_holdings": len(self.outcomes),
            "n_triggered": len(self.triggered),
            "n_unusable": len(self.unusable),
            "channel": self.channel,
            "notifications": {
                "sent": self.notifications_sent,
                "failed": self.notifications_failed,
                "suppressed_as_duplicate": self.notifications_suppressed,
            },
            "outcomes": [o.to_dict() for o in self.outcomes],
        }


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Trigger:
    """The first exit the replay found, and where it was."""

    reason: str
    reference_price: float
    on: date
    index: int
    """Position of the triggering bar within the replayed slice, for the sessions-ago count."""


def _replay(
    holding: Holding, frame: pd.DataFrame, strategy: Strategy
) -> tuple[Position, _Trigger | None, int]:
    """Walk the bars since entry as the backtester would, stopping at the first exit.

    Returns ``(position, trigger, n_replayed)``. The position reflects state as of the trigger
    bar, or the latest bar if nothing triggered -- never past the trigger, because marking bars
    after an exit records what happened to a position that would already have been sold.

    The ordering matches :meth:`~app.backtesting.engine.Backtester.run`: marks first, then the
    trailing stop, then price exits, then the signal. A signal exit is checked only when no
    price exit fired on the same bar, because in the engine the price exit closes the position
    before the signal is read.
    """
    position = Position(
        symbol=holding.symbol,
        market=holding.market,
        currency=holding.currency,
        quantity=holding.quantity,
        entry_price=holding.entry_price,
        entry_date=holding.opened_on,
        entry_commission=holding.entry_fees,
        entry_slippage=0.0,
        entry_reason=holding.entry_note,
        stop_price=holding.stop_price,
        take_profit_price=holding.take_profit_price,
    )

    trailing_multiple = float(
        getattr(strategy.params, "trailing_stop_atr_multiple", 0.0) or 0.0
    )
    entry_day = holding.opened_on.date()

    # Strictly after the entry day: the entry bar is already reflected in the entry price, and
    # marking it would let the entry session's own low trip the stop on day zero.
    since_entry = frame[frame.index.date > entry_day]
    if since_entry.empty:
        return position, None, 0

    prepared = strategy.prepare(since_entry)

    for i, (stamp, bar) in enumerate(since_entry.iterrows()):
        position.update_marks(float(bar["high"]), float(bar["low"]), float(bar["close"]))
        if trailing_multiple > 0 and "atr_14" in bar and pd.notna(bar["atr_14"]):
            position.advance_trailing_stop(float(bar["atr_14"]), trailing_multiple)

        resolved = Backtester._resolve_price_exit(position, bar)
        if resolved is not None:
            reason, reference = resolved
            return position, _Trigger(reason, reference, stamp.date(), i), len(since_entry)

        decision = strategy.evaluate_prepared(prepared, i, in_position=True)
        if decision.action is Action.SELL:
            # The engine fills a signal exit at the *next* bar's open. Using this bar's close
            # as the reference is the closest honest stand-in and is labelled as a reference,
            # not as a price anyone would get.
            return (
                position,
                _Trigger(EXIT_SIGNAL, float(bar["close"]), stamp.date(), i),
                len(since_entry),
            )

    return position, None, len(since_entry)


def _describe_reason(code: str) -> str:
    """Plain language for a reason code, for a message read on a phone."""
    return {
        EXIT_STOP: "the stop loss level was reached",
        EXIT_TRAILING: "the trailing stop level was reached",
        EXIT_TARGET: "the take-profit level was reached",
        EXIT_SIGNAL: "the strategy's exit conditions came true",
    }.get(code, code)


def check_holdings(
    session: Session,
    *,
    holdings: list[Holding] | None = None,
    as_of: date | None = None,
) -> list[WatchOutcome]:
    """Evaluate the exit rule against every open holding. No notifications, no writes.

    Separated from :func:`run_watch` so the evaluation can be tested, and inspected from the
    dashboard, without sending anything.
    """
    targets = holdings if holdings is not None else open_holdings(session)
    today = as_of or datetime.now(timezone.utc).date()
    outcomes: list[WatchOutcome] = []

    for holding in targets:
        spec = find_asset(holding.symbol, holding.market)
        outcome = WatchOutcome(
            holding_id=holding.id,
            symbol=holding.symbol,
            region=holding.region,
            quantity=holding.quantity,
            entry_price=holding.entry_price,
            opened_on=holding.opened_on.date(),
            take_profit_price=holding.take_profit_price,
            liquidity_caveat=spec.liquidity_caveat,
        )

        try:
            strategy: Strategy = build_strategy(
                holding.strategy_name, holding.strategy_params
            )
        except (KeyError, TypeError, ValueError) as exc:
            outcome.usable = False
            outcome.problem = (
                f"cannot rebuild strategy {holding.strategy_name!r} with the parameters "
                f"stored at entry: {exc}"
            )
            outcomes.append(outcome)
            continue

        # Warm-up before the entry date, not before today: the indicators have to be warm on
        # the first bar after entry or the early part of the replay is evaluated on NaNs.
        warmup_start = holding.opened_on.date() - timedelta(days=WARMUP_CALENDAR_DAYS)
        try:
            frames, skipped = load_features(
                session,
                [holding.symbol],
                holding.market,
                warmup_start=warmup_start,
                end=today,
            )
        except InsufficientDataError as exc:
            # load_features raises when *no* symbol clears the bar minimum, which for a
            # single-symbol call means this one did not. Caught rather than propagated: one
            # un-downloaded holding must not stop the others being checked, and the user needs
            # to be told which position is unwatched rather than seeing a traceback.
            frames, skipped = {}, [str(exc)]

        if holding.symbol not in frames:
            outcome.usable = False
            outcome.problem = (
                f"not enough stored history to evaluate: {skipped[0] if skipped else 'no bars'}. "
                f"Run `python -m app download-data --symbols {holding.symbol}`."
            )
            outcomes.append(outcome)
            continue

        frame = frames[holding.symbol]
        last_bar_date = frame.index[-1].date()
        outcome.last_bar_on = last_bar_date
        outcome.last_price = float(frame["close"].iloc[-1])

        staleness = (today - last_bar_date).days
        if staleness > STALE_DATA_DAYS:
            outcome.usable = False
            outcome.problem = (
                f"the newest stored bar is {last_bar_date}, {staleness} days old. Refusing to "
                "report 'no exit signal' from stale data -- that reads as 'nothing has "
                "happened' when it means 'I do not know'. Run "
                "`python -m app download-data`."
            )
            outcomes.append(outcome)
            continue

        if last_bar_date < holding.opened_on.date():
            outcome.usable = False
            outcome.problem = (
                f"no stored bars since the entry date: the newest is {last_bar_date} and the "
                f"position opened {holding.opened_on.date()}. Normal on the day you buy -- the "
                "session's bar is not published yet -- so there is simply nothing to evaluate "
                "until tomorrow's download. If it persists, the entry date is wrong."
            )
            outcomes.append(outcome)
            continue

        position, trigger, n_replayed = _replay(holding, frame, strategy)
        outcome.effective_stop = position.effective_stop
        outcome.bars_held = position.bars_held
        outcome.max_adverse_excursion_pct = position.max_adverse_excursion_pct
        outcome.max_favorable_excursion_pct = position.max_favorable_excursion_pct
        outcome.unrealised_pnl_gross = position.unrealised_pnl(outcome.last_price)
        outcome.unrealised_pnl_pct = position.unrealised_pnl_pct(outcome.last_price)

        if trigger is not None:
            outcome.exit_triggered = True
            outcome.exit_reason = trigger.reason
            outcome.reference_price = trigger.reference_price
            outcome.exit_triggered_on = trigger.on
            outcome.sessions_since_trigger = n_replayed - 1 - trigger.index

        # Today's verdict, reported alongside the trigger. The two can disagree -- a stop hit
        # in March says nothing about what the strategy thinks in September -- and showing both
        # is more use than picking one.
        decision = strategy.evaluate(frame, in_position=True)
        outcome.decision_action = decision.action.value
        outcome.decision_reasons = decision.reasons
        outcome.score_description = decision.describe_score()

        outcomes.append(outcome)

    return outcomes


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #


def _exit_message(outcome: WatchOutcome) -> tuple[str, str]:
    """The alert's subject and body. ASCII only.

    Every phrasing choice here is about not overstating. "The exit rule fired" is a fact about
    this software. "Sell" would be advice, and "the price will fall" would be a claim about the
    future that nothing in this project supports. A notification is read in three seconds and
    whatever it implies is what gets acted on.
    """
    stale = (outcome.sessions_since_trigger or 0) > 0
    when = "today" if not stale else f"on {outcome.exit_triggered_on}"
    subject = f"[quant-trader] Exit rule fired {when}: {outcome.symbol}"

    pnl_line = "unrealised P&L unknown"
    if outcome.unrealised_pnl_pct is not None and outcome.unrealised_pnl_gross is not None:
        pnl_line = (
            f"{outcome.unrealised_pnl_pct:+.2f}% "
            f"({outcome.unrealised_pnl_gross:+,.2f} gross, before selling costs)"
        )

    lines = [
        f"{outcome.symbol} -- the strategy's exit rule fired {when}: "
        f"{_describe_reason(outcome.exit_reason)}.",
        "",
    ]

    if stale:
        lines += [
            f"NOTE: this triggered on {outcome.exit_triggered_on}, "
            f"{outcome.sessions_since_trigger} trading session(s) ago -- not today. A "
            "backtest would have closed the position then, so everything below describes a "
            "position the strategy considers already exited. If you are still holding it, you "
            "are past the rule, which is a decision but not this strategy's.",
            "",
        ]

    lines += [
        f"You hold: {outcome.quantity:g} @ {outcome.entry_price:g} "
        f"since {outcome.opened_on}",
        f"Last close: {outcome.last_price:g} on {outcome.last_bar_on}",
        f"Unrealised: {pnl_line}",
    ]

    if outcome.effective_stop is not None:
        lines.append(f"Stop level in force: {outcome.effective_stop:.4g}")
    if outcome.take_profit_price is not None:
        lines.append(f"Take-profit level: {outcome.take_profit_price:.4g}")
    if outcome.max_adverse_excursion_pct is not None:
        span = "up to the trigger" if stale else "while held"
        lines.append(
            f"Worst point {span}: {outcome.max_adverse_excursion_pct:.2f}% "
            f"(best: {outcome.max_favorable_excursion_pct:+.2f}%)"
        )

    lines += ["", f"What the strategy says about today ({outcome.decision_action}):"]
    lines += [f"  - {reason}" for reason in (outcome.decision_reasons or ("no reason given",))[:6]]

    lines += [
        "",
        "WHAT THIS MEANS: a rule this project backtested has triggered on your position.",
        "WHAT IT DOES NOT MEAN: that the price will fall, that selling is correct, or that",
        "the strategy is right. It is one rule's opinion, measured on history that may not",
        "repeat. The decision is yours.",
    ]

    if outcome.liquidity_caveat:
        lines += ["", f"Note: {outcome.liquidity_caveat}"]

    lines += [
        "",
        f"If you sell, record it:  python -m app sell {outcome.holding_id} "
        "--price <what you got>",
    ]
    return subject, "\n".join(lines)


def _problem_message(outcomes: list[WatchOutcome]) -> tuple[str, str]:
    """One alert for everything the watch could not evaluate.

    Sent because silence is indistinguishable from "nothing triggered". A holding the user
    believes is being watched, that is not being watched, is the worst failure mode this
    feature has.
    """
    subject = f"[quant-trader] Could not check {len(outcomes)} holding(s)"
    lines = [
        "The daily check could not reach a conclusion for these positions. They are NOT",
        "being watched until this is fixed:",
        "",
    ]
    for outcome in outcomes:
        lines.append(f"  {outcome.symbol} (holding {outcome.holding_id}): {outcome.problem}")
    return subject, "\n".join(lines)


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


def run_watch(
    session: Session,
    notifier: Notifier,
    *,
    as_of: date | None = None,
    dry_run: bool = False,
) -> WatchReport:
    """Check every open holding, alert on what fired, and record what was sent.

    Idempotent by day. The dedupe key is the holding plus the bar date that triggered, so
    running this every morning does not re-send Monday's signal all week. That matters more
    than it sounds: a repeating alert gets muted, and a muted alert is worse than none.

    ``dry_run`` evaluates and reports without sending or writing.
    """
    ran_at = datetime.now(timezone.utc)
    outcomes = check_holdings(session, as_of=as_of)
    report = WatchReport(ran_at=ran_at, outcomes=outcomes, channel=notifier.name)

    if dry_run:
        return report

    for outcome in outcomes:
        holding = session.get(Holding, outcome.holding_id)
        if holding is None:
            continue
        holding.last_checked_on = ran_at
        if outcome.usable and outcome.exit_triggered and holding.exit_signal_on is None:
            # Recorded on the holding as well as in the alert, so the dashboard can show a
            # position as "exit rule fired" even if the notification failed to deliver.
            holding.exit_signal_on = pd.Timestamp(
                outcome.exit_triggered_on
            ).to_pydatetime().replace(tzinfo=timezone.utc)
            holding.exit_signal_reason = outcome.exit_reason

    for outcome in (o for o in outcomes if o.usable and o.exit_triggered):
        subject, body = _exit_message(outcome)
        _dispatch(
            session,
            notifier,
            report,
            Notification(
                kind="exit_signal",
                subject=subject,
                body=body,
                # Keyed on the *trigger* bar, not the latest one. Keying on the latest
                # would re-send a months-old trigger every single day as the data moved
                # forward, which is how an alert gets muted.
                dedupe_key=f"exit:{outcome.holding_id}:{outcome.exit_triggered_on}",
                symbol=outcome.symbol,
                holding_id=outcome.holding_id,
                metadata={"reason": outcome.exit_reason},
            ),
        )

    unusable = [o for o in outcomes if not o.usable]
    if unusable:
        subject, body = _problem_message(unusable)
        _dispatch(
            session,
            notifier,
            report,
            Notification(
                kind="watch_problem",
                subject=subject,
                body=body,
                # Keyed on the day and the affected holdings, so a problem that persists is
                # re-raised each day it persists but not several times a day.
                dedupe_key=(
                    f"problem:{(as_of or ran_at.date())}:"
                    + ",".join(str(o.holding_id) for o in unusable)
                ),
            ),
        )

    session.flush()
    return report


def _dispatch(
    session: Session, notifier: Notifier, report: WatchReport, notification: Notification
) -> None:
    """Send one notification unless it has already gone out, and record the attempt."""
    already_sent = session.scalar(
        select(Alert.id).where(Alert.dedupe_key == notification.dedupe_key)
    )
    if already_sent is not None:
        report.notifications_suppressed += 1
        logger.info("Suppressed duplicate alert %s", notification.dedupe_key)
        return

    result = notifier.send(notification)
    session.add(
        Alert(
            dedupe_key=notification.dedupe_key,
            kind=notification.kind,
            holding_id=notification.holding_id,
            symbol=notification.symbol,
            subject=notification.subject,
            body=notification.body,
            channel=result.channel,
            status="SENT" if result.delivered else "FAILED",
            failure_reason="" if result.delivered else result.detail,
            sent_at=result.sent_at,
        )
    )
    if result.delivered:
        report.notifications_sent += 1
    else:
        report.notifications_failed += 1
        logger.warning(
            "Alert %s was not delivered: %s", notification.dedupe_key, result.detail
        )
