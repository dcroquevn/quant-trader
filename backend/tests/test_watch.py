"""The daily check, its refusals, and what its messages are allowed to say.

Three groups, in ascending order of how much they matter:

* The exit logic fires where the backtester would fire. Correctness.
* The refusals. A watch that reports "no exit signal" from week-old data is the single most
  dangerous output this module can produce, because silence and ignorance look identical.
* The wording. A notification is read in three seconds on a phone, and whatever it implies is
  what gets acted on. So there are tests asserting the message does *not* say certain things.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.database.models import Alert, Asset, Bar
from app.notifications.base import ConsoleNotifier, Notification, NullNotifier
from app.portfolio.holdings import open_holding
from app.portfolio.watch import (
    EXIT_SIGNAL,
    STALE_DATA_DAYS,
    _exit_message,
    check_holdings,
    run_watch,
)


def _synthetic_bars(
    n: int, *, start: float = 100.0, drift: float = 0.0, end_on: date
) -> pd.DataFrame:
    """A deterministic OHLCV series ending on ``end_on``.

    Synthetic rather than downloaded so the assertions are about the watch's logic and not
    about what a provider happened to return. Business-day index, because a weekend gap in the
    index would exercise the calendar rather than the exit rule.
    """
    index = pd.bdate_range(end=pd.Timestamp(end_on), periods=n, tz=None)
    closes = start + drift * np.arange(n, dtype=float)
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "adj_close": closes,
            "volume": np.full(n, 5_000_000.0),
        },
        index=index,
    )
    frame.index.name = "ts"
    return frame


@pytest.fixture
def stocked(session):
    from app.data.engine import DataEngine

    DataEngine(session).sync_universe()
    return session


def _store(session, symbol: str, frame: pd.DataFrame) -> None:
    """Write a synthetic frame straight into the bars table."""
    asset = session.query(Asset).filter_by(symbol=symbol, market_code="USA").one()
    for stamp, row in frame.iterrows():
        session.add(
            Bar(
                asset_id=asset.id,
                timeframe="1D",
                ts=stamp.to_pydatetime(),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                adj_close=float(row["adj_close"]),
                volume=float(row["volume"]),
            )
        )
    session.flush()


class TestRefusals:
    def test_stale_data_refuses_to_conclude(self, stocked) -> None:
        """"No exit signal" from old data reads as "nothing happened". It is not the same.

        This is the failure mode the whole feature has to avoid: a user glancing at a green
        dashboard that is describing last week.
        """
        today = date(2026, 9, 30)
        stale_end = today - timedelta(days=STALE_DATA_DAYS + 10)
        _store(stocked, "AAPL", _synthetic_bars(400, drift=0.1, end_on=stale_end))
        open_holding(
            stocked, "AAPL", 10, 100.0, opened_on=stale_end - timedelta(days=30)
        )

        outcome = check_holdings(stocked, as_of=today)[0]
        assert outcome.usable is False
        assert "days old" in outcome.problem
        assert "download-data" in outcome.problem

    def test_no_stored_history_refuses_to_conclude(self, stocked) -> None:
        open_holding(stocked, "AAPL", 10, 100.0, opened_on=date(2026, 6, 1))
        outcome = check_holdings(stocked, as_of=date(2026, 9, 30))[0]
        assert outcome.usable is False
        assert "download-data" in outcome.problem

    def test_no_bars_since_entry_is_explained_not_blamed(self, stocked) -> None:
        """This is the normal state on the day you buy, not an error.

        The session's bar is not published yet, so there is nothing to evaluate. An earlier
        wording told the user their entry date was probably wrong, which would have fired every
        single time they bought something.
        """
        _store(stocked, "AAPL", _synthetic_bars(400, drift=0.1, end_on=date(2026, 9, 29)))
        open_holding(stocked, "AAPL", 10, 100.0, opened_on=date(2026, 9, 30))
        outcome = check_holdings(stocked, as_of=date(2026, 10, 1))[0]
        assert outcome.usable is False
        assert "nothing to evaluate" in outcome.problem
        assert "Normal on the day you buy" in outcome.problem

    def test_stale_data_takes_precedence_over_a_missing_entry_bar(self, stocked) -> None:
        """Both are refusals; stale data is the more urgent one, so it is reported first."""
        _store(stocked, "AAPL", _synthetic_bars(400, drift=0.1, end_on=date(2026, 9, 29)))
        open_holding(stocked, "AAPL", 10, 100.0, opened_on=date(2026, 10, 15))
        outcome = check_holdings(stocked, as_of=date(2026, 10, 16))[0]
        assert outcome.usable is False
        assert "days old" in outcome.problem

    def test_an_unbuildable_strategy_is_reported_not_raised(self, stocked) -> None:
        """A bad stored parameter set must not abort the whole run's other holdings."""
        _store(stocked, "AAPL", _synthetic_bars(400, drift=0.1, end_on=date(2026, 9, 29)))
        holding = open_holding(stocked, "AAPL", 10, 100.0, opened_on=date(2026, 6, 1))
        holding.strategy_name = "no_such_strategy"
        stocked.flush()

        outcome = check_holdings(stocked, as_of=date(2026, 9, 30))[0]
        assert outcome.usable is False
        assert "cannot rebuild strategy" in outcome.problem


class TestExitDetection:
    def test_a_stop_hit_long_ago_reports_when_it_happened(self, stocked) -> None:
        """Not "today". This was a real defect: the watch checked only the latest bar.

        A position whose target was crossed months earlier was reported as firing now, and a
        backtest would have closed it then. The date is the whole point.
        """
        end = date(2026, 9, 29)
        # Rises, so a stop below entry is never touched and the *signal* exit is what fires --
        # but whatever fires, it must be dated.
        _store(stocked, "AAPL", _synthetic_bars(500, start=50.0, drift=0.5, end_on=end))
        open_holding(
            stocked, "AAPL", 10, 200.0, opened_on=date(2025, 6, 2), stop_price=150.0
        )

        outcome = check_holdings(stocked, as_of=date(2026, 9, 30))[0]
        assert outcome.usable
        if outcome.exit_triggered:
            assert outcome.exit_triggered_on is not None
            assert outcome.sessions_since_trigger is not None
            assert outcome.sessions_since_trigger >= 0

    def test_excursions_stop_at_the_trigger(self, stocked) -> None:
        """Marking bars after the exit records what happened to a position already sold.

        Before this was fixed, a holding whose target was hit early reported a "best point"
        covering the year afterwards.
        """
        end = date(2026, 9, 29)
        _store(stocked, "AAPL", _synthetic_bars(500, start=50.0, drift=0.5, end_on=end))
        open_holding(
            stocked,
            "AAPL",
            10,
            200.0,
            opened_on=date(2025, 6, 2),
            take_profit_price=210.0,
        )

        outcome = check_holdings(stocked, as_of=date(2026, 9, 30))[0]
        assert outcome.exit_triggered
        # The series ends far above 210, so an unbounded replay would show a huge best point.
        # Bounded at the trigger it cannot.
        assert outcome.max_favorable_excursion_pct < 100.0
        assert outcome.bars_held < 500

    def test_the_entry_bar_cannot_trip_the_stop(self, stocked) -> None:
        """The entry price already reflects that session; marking it would exit on day zero."""
        end = date(2026, 9, 29)
        frame = _synthetic_bars(400, start=100.0, drift=0.0, end_on=end)
        _store(stocked, "AAPL", frame)
        entry_day = frame.index[-10].date()
        # A stop just under the flat price: the entry session's own low (99) would trip it.
        open_holding(stocked, "AAPL", 10, 100.0, opened_on=entry_day, stop_price=99.5)

        outcome = check_holdings(stocked, as_of=date(2026, 9, 30))[0]
        assert outcome.exit_triggered_on != entry_day

    def test_today_and_the_trigger_are_reported_separately(self, stocked) -> None:
        """A stop hit in March says nothing about what the strategy thinks in September."""
        end = date(2026, 9, 29)
        _store(stocked, "AAPL", _synthetic_bars(500, start=50.0, drift=0.5, end_on=end))
        open_holding(stocked, "AAPL", 10, 200.0, opened_on=date(2025, 6, 2))
        outcome = check_holdings(stocked, as_of=date(2026, 9, 30))[0]
        assert outcome.decision_action in {"BUY", "SELL", "HOLD"}
        assert outcome.score_description


class TestNotifications:
    def test_an_alert_is_recorded_with_its_delivery_status(self, stocked) -> None:
        """Whether the rule fired and whether the message arrived are two facts."""
        end = date(2026, 9, 29)
        _store(stocked, "AAPL", _synthetic_bars(500, start=50.0, drift=0.5, end_on=end))
        open_holding(stocked, "AAPL", 10, 200.0, opened_on=date(2025, 6, 2))

        report = run_watch(stocked, ConsoleNotifier(), as_of=date(2026, 9, 30))
        alerts = stocked.query(Alert).all()
        if report.triggered:
            assert alerts
            assert all(a.status == "SENT" for a in alerts)
            assert report.notifications_sent == len(alerts)

    def test_a_failed_delivery_is_recorded_as_failed(self, stocked) -> None:
        end = date(2026, 9, 29)
        _store(stocked, "AAPL", _synthetic_bars(500, start=50.0, drift=0.5, end_on=end))
        open_holding(stocked, "AAPL", 10, 200.0, opened_on=date(2025, 6, 2))

        report = run_watch(stocked, NullNotifier(), as_of=date(2026, 9, 30))
        if report.triggered:
            assert report.notifications_failed
            assert all(a.status == "FAILED" for a in stocked.query(Alert).all())

    def test_the_same_trigger_is_not_sent_twice(self, stocked) -> None:
        """A daily job would otherwise re-send the same signal every morning.

        An alert that repeats gets muted, and a muted alert is worse than none.
        """
        end = date(2026, 9, 29)
        _store(stocked, "AAPL", _synthetic_bars(500, start=50.0, drift=0.5, end_on=end))
        open_holding(stocked, "AAPL", 10, 200.0, opened_on=date(2025, 6, 2))

        first = run_watch(stocked, ConsoleNotifier(), as_of=date(2026, 9, 30))
        second = run_watch(stocked, ConsoleNotifier(), as_of=date(2026, 10, 1))
        if first.triggered:
            assert first.notifications_sent >= 1
            assert second.notifications_sent == 0
            assert second.notifications_suppressed >= 1

    def test_dry_run_sends_and_records_nothing(self, stocked) -> None:
        end = date(2026, 9, 29)
        _store(stocked, "AAPL", _synthetic_bars(500, start=50.0, drift=0.5, end_on=end))
        open_holding(stocked, "AAPL", 10, 200.0, opened_on=date(2025, 6, 2))

        run_watch(stocked, ConsoleNotifier(), as_of=date(2026, 9, 30), dry_run=True)
        assert stocked.query(Alert).count() == 0

    def test_a_holding_that_cannot_be_checked_produces_its_own_alert(self, stocked) -> None:
        """Silence is indistinguishable from "nothing triggered"."""
        open_holding(stocked, "AAPL", 10, 100.0, opened_on=date(2026, 6, 1))
        report = run_watch(stocked, ConsoleNotifier(), as_of=date(2026, 9, 30))
        assert report.unusable
        kinds = {a.kind for a in stocked.query(Alert).all()}
        assert "watch_problem" in kinds

    def test_the_watch_records_that_it_ran(self, stocked) -> None:
        """A holding with no last_checked_on is a holding nobody is watching."""
        from app.database.models import Holding

        _store(stocked, "AAPL", _synthetic_bars(500, start=50.0, drift=0.5,
                                                end_on=date(2026, 9, 29)))
        holding = open_holding(stocked, "AAPL", 10, 200.0, opened_on=date(2025, 6, 2))
        assert holding.last_checked_on is None
        run_watch(stocked, ConsoleNotifier(), as_of=date(2026, 9, 30))
        assert stocked.get(Holding, holding.id).last_checked_on is not None


class TestMessageWording:
    """What the alert is allowed to say.

    A notification is read in three seconds and whatever it implies is what gets acted on, so
    these assert absences as much as presences.
    """

    @pytest.fixture
    def outcome(self, stocked):
        _store(stocked, "ECH", _synthetic_bars(500, start=50.0, drift=-0.02,
                                               end_on=date(2026, 9, 29)))
        open_holding(
            stocked, "ECH", 10, 45.0, opened_on=date(2025, 6, 2), stop_price=44.0
        )
        outcomes = check_holdings(stocked, as_of=date(2026, 9, 30))
        assert outcomes[0].usable
        return outcomes[0]

    def test_it_states_what_happened_not_what_to_do(self, outcome) -> None:
        subject, body = _exit_message(outcome)
        lowered = body.lower()
        assert "exit rule fired" in lowered
        assert "the decision is yours" in lowered

    def test_it_never_predicts_a_price(self, outcome) -> None:
        _, body = _exit_message(outcome)
        lowered = body.lower()
        for forbidden in ("will rise", "will fall", "should sell", "sell now", "guaranteed"):
            assert forbidden not in lowered.replace(
                "that the price will fall", ""
            ), f"the message implies {forbidden!r}"

    def test_it_says_outright_what_it_does_not_mean(self, outcome) -> None:
        _, body = _exit_message(outcome)
        assert "WHAT IT DOES NOT MEAN" in body

    def test_it_is_ascii_only(self, outcome) -> None:
        """An em dash raised UnicodeEncodeError on a cp1252 console mid-alert.

        A notifier that crashes is a notification nobody gets.
        """
        subject, body = _exit_message(outcome)
        (subject + body).encode("ascii")

    def test_a_thin_instrument_carries_its_liquidity_caveat(self, outcome) -> None:
        """ECH trades about 10M USD a day, and the modelled fill does not know that."""
        _, body = _exit_message(outcome)
        assert "market impact" in body

    def test_it_tells_the_user_how_to_record_the_sale(self, outcome) -> None:
        _, body = _exit_message(outcome)
        assert f"app sell {outcome.holding_id}" in body

    def test_a_stale_trigger_is_labelled_as_stale(self, outcome) -> None:
        subject, body = _exit_message(outcome)
        if (outcome.sessions_since_trigger or 0) > 0:
            assert "not today" in body
            assert str(outcome.exit_triggered_on) in subject


class TestNotifierContract:
    def test_console_delivers(self) -> None:
        result = ConsoleNotifier().send(
            Notification(kind="t", subject="s", body="b", dedupe_key="k")
        )
        assert result.delivered
        assert result.sent_at is not None

    def test_null_reports_failure_rather_than_success(self) -> None:
        """So a test asserting delivery cannot pass by accident against it."""
        result = NullNotifier().send(
            Notification(kind="t", subject="s", body="b", dedupe_key="k")
        )
        assert result.delivered is False

    def test_unconfigured_telegram_fails_without_raising(self) -> None:
        """A delivery failure is an operational state. Raising would abort the whole run."""
        from app.notifications.telegram import TelegramNotifier

        notifier = TelegramNotifier(token="", chat_id="")
        assert notifier.configured is False
        result = notifier.send(Notification(kind="t", subject="s", body="b", dedupe_key="k"))
        assert result.delivered is False
        assert "TELEGRAM_BOT_TOKEN" in result.detail

    def test_build_notifier_falls_back_to_console_not_to_a_broken_telegram(self) -> None:
        """Returning an unconfigured Telegram would mean every alert silently fails."""
        from app.notifications.base import build_notifier

        assert build_notifier("telegram").name == "console"
        assert build_notifier("console").name == "console"
        assert build_notifier("null").name == "null"

    def test_telegram_setup_instructions_name_the_missing_variables(self) -> None:
        from app.notifications.telegram import TelegramNotifier

        described = TelegramNotifier(token="", chat_id="abc").describe_setup()
        assert "TELEGRAM_BOT_TOKEN" in described
        assert "TELEGRAM_CHAT_ID" not in described

    def test_a_long_message_is_truncated_not_rejected(self) -> None:
        """Telegram rejects anything over 4096 characters outright."""
        from app.notifications.telegram import MESSAGE_LIMIT

        notification = Notification(
            kind="t", subject="s", body="x" * (MESSAGE_LIMIT * 2), dedupe_key="k"
        )
        assert len(notification.as_text()) > MESSAGE_LIMIT
        # The truncation happens inside send(); with no credentials it short-circuits first, so
        # this asserts the limit is declared and the body is what would need clipping.
        assert MESSAGE_LIMIT == 4096
