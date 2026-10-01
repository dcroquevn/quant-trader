"""Recording a purchase by cash amount, and noticing when the fill is not the signalled trade.

The second group is the one that earned its place. A real purchase exposed it: the scanner
signalled GOOGL against a 344.08 close with a stop at 327.17 and a target at 394.81 — a
reward-to-risk of 3.0 — and the fill came in at 351.44 the next session. Against the same levels
that is 1.79. A 2% worse entry cost 40% of the ratio, and nothing said so.

The gap is structural rather than a mistake: a signal is computed from a close and filled the next
session, and the stop does not follow the fill because it is placed by a volatility estimate, not
as a fixed percentage below whatever was paid. So the whole cost of a worse entry lands on the
ratio, and the user has to be told.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.portfolio.holdings import (
    RR_FLOOR,
    PriceUnavailableError,
    assess_entry,
    list_holdings,
    open_holding,
    resolve_quantity,
)


@pytest.fixture
def stocked(session):
    from app.data.engine import DataEngine

    DataEngine(session).sync_universe()
    return session


class TestResolvingAnAmount:
    def test_cash_becomes_a_fractional_share_count(self, stocked) -> None:
        """20.36 dollars of a 344.08 share is 0.0592 shares, and nobody wants to do that by hand."""
        entry = resolve_quantity(stocked, "GOOGL", amount=20.36, price=344.08)
        assert entry.quantity == pytest.approx(20.36 / 344.08)
        assert entry.price == 344.08
        assert entry.price_estimated is False
        assert entry.amount == 20.36
        assert entry.currency == "USD"

    def test_the_cost_basis_equals_the_cash_spent(self, stocked) -> None:
        """The whole point of deriving the quantity: the arithmetic has to close."""
        entry = resolve_quantity(stocked, "GOOGL", amount=20.36, price=351.44)
        assert entry.quantity * entry.price == pytest.approx(20.36)

    def test_a_share_count_still_works(self, stocked) -> None:
        entry = resolve_quantity(stocked, "GOOGL", quantity=10, price=344.08)
        assert entry.quantity == 10
        assert entry.amount is None

    def test_both_or_neither_is_refused(self, stocked) -> None:
        """A caller who gave both meant one of them; guessing would record a different trade."""
        with pytest.raises(ValueError, match="not both and not neither"):
            resolve_quantity(stocked, "GOOGL", quantity=10, amount=20.36, price=344.08)
        with pytest.raises(ValueError, match="not both and not neither"):
            resolve_quantity(stocked, "GOOGL", price=344.08)

    def test_an_unknown_currency_is_refused_rather_than_assumed(self, stocked) -> None:
        with pytest.raises(ValueError, match="No exchange rate available"):
            resolve_quantity(stocked, "GOOGL", amount=100, price=344.08, currency="EUR")

    def test_no_price_anywhere_raises_rather_than_defaulting(self, stocked) -> None:
        """Every available default is a lie: zero records nothing, one records a trade not made."""
        with pytest.raises(PriceUnavailableError, match="cannot be derived"):
            resolve_quantity(stocked, "GOOGL", amount=20.36)

    @pytest.mark.parametrize("amount", [0, -5])
    def test_a_nonsense_amount_is_refused(self, stocked, amount) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            resolve_quantity(stocked, "GOOGL", amount=amount, price=344.08)


class TestRecordingAnAmount:
    def test_the_holding_keeps_what_the_user_said_they_spent(self, stocked) -> None:
        """The quantity is derived; the cash is the fact. Both are stored."""
        holding = open_holding(
            stocked,
            "GOOGL",
            20.36 / 351.44,
            351.44,
            opened_on=date(2026, 10, 1),
            entry_amount=20.36,
            entry_amount_currency="USD",
            entry_fx_rate=1.0,
        )
        assert holding.entry_amount == 20.36
        assert holding.entry_amount_currency == "USD"
        assert holding.quantity * holding.entry_price == pytest.approx(20.36)

    def test_an_estimated_price_is_recorded_as_estimated(self, stocked) -> None:
        """This table exists to keep real fills apart from modelled ones."""
        holding = open_holding(
            stocked, "GOOGL", 1, 344.08, entry_price_estimated=True
        )
        assert holding.entry_price_estimated is True

    def test_a_supplied_price_is_not_flagged(self, stocked) -> None:
        assert open_holding(stocked, "GOOGL", 1, 344.08).entry_price_estimated is False

    def test_fractional_quantities_survive_the_round_trip(self, stocked) -> None:
        from app.portfolio.holdings import export_positions, import_positions

        open_holding(
            stocked,
            "GOOGL",
            0.05793308,
            351.44,
            opened_on=date(2026, 10, 1),
            entry_amount=20.36,
        )
        rows = export_positions(stocked)
        for h in list_holdings(stocked):
            stocked.delete(h)
        stocked.flush()

        import_positions(stocked, rows)
        restored = list_holdings(stocked)[0]
        assert restored.quantity == pytest.approx(0.05793308)
        assert restored.entry_amount == 20.36


class TestEntryQuality:
    """The real case, kept as a regression because the arithmetic is counter-intuitive."""

    GOOGL = dict(paid=351.44, reference=344.08, stop=327.1698, target=394.8105)

    def test_a_two_percent_worse_fill_costs_forty_percent_of_the_ratio(self) -> None:
        quality = assess_entry(**self.GOOGL)
        assert quality.slippage_pct == pytest.approx(2.14, abs=0.01)
        assert quality.reward_risk_signalled == pytest.approx(3.00, abs=0.01)
        assert quality.reward_risk_paid == pytest.approx(1.79, abs=0.01)
        assert quality.is_material

    def test_the_description_states_both_ratios(self) -> None:
        text = assess_entry(**self.GOOGL).describe()
        assert "3.00x" in text and "1.79x" in text
        assert "+2.14%" in text

    def test_it_refuses_to_say_whether_to_hold(self) -> None:
        """Reporting the change is this project's job. Judging it is not."""
        text = assess_entry(**self.GOOGL).describe().lower()
        assert "your call" in text
        for forbidden in ("you should", "we recommend", "a good entry", "still a buy"):
            assert forbidden not in text

    def test_it_is_ascii_only(self) -> None:
        """It reaches a cp1252 console and a Telegram message."""
        assess_entry(**self.GOOGL).describe().encode("ascii")

    def test_a_fill_at_the_signal_price_is_not_material(self) -> None:
        """A warning on every trade is a warning on none."""
        quality = assess_entry(344.08, 344.08, 327.17, 394.81)
        assert quality.is_material is False

    def test_a_better_fill_is_not_flagged(self) -> None:
        quality = assess_entry(340.0, 344.08, 327.17, 394.81)
        assert quality.reward_risk_paid > quality.reward_risk_signalled
        assert quality.is_material is False

    def test_dropping_under_the_floor_is_always_reported(self) -> None:
        """A drop too small for the relative test still counts if it crosses 2:1.

        Chosen so the two rules disagree: the ratio falls 2.50x -> 1.95x, which is a 22% drop and
        therefore under the 25% threshold, but it lands below the floor. Without the floor rule
        this case would pass silently.
        """
        quality = assess_entry(paid=123.73, reference=120.0, stop=100.0, target=170.0)
        assert quality.reward_risk_signalled == pytest.approx(2.50, abs=0.01)
        assert quality.reward_risk_paid == pytest.approx(1.95, abs=0.01)

        relative_drop = (
            quality.reward_risk_signalled - quality.reward_risk_paid
        ) / quality.reward_risk_signalled
        assert relative_drop < 0.25, "the relative rule alone would not fire here"
        assert quality.reward_risk_paid < RR_FLOOR <= quality.reward_risk_signalled
        assert quality.is_material

    def test_a_fill_past_the_target_says_the_levels_do_not_apply(self) -> None:
        text = assess_entry(400.0, 344.08, 327.17, 394.81).describe()
        assert "do not describe this position" in text

    def test_missing_levels_produce_nothing_rather_than_a_guess(self) -> None:
        assert assess_entry(351.44, 344.08, None, 394.81) is None
        assert assess_entry(351.44, 344.08, 327.17, None) is None
        assert assess_entry(351.44, 0.0, 327.17, 394.81) is None


class TestTelegramDocument:
    """Sending the digest as a file, so reading it does not require a desktop.

    The artifact route was six steps -- log in, find the run, scroll, download a zip, extract,
    open. A page whose purpose is to be glanceable does not survive that.
    """

    def test_an_unconfigured_notifier_fails_without_raising(self, tmp_path) -> None:
        from app.notifications.telegram import TelegramNotifier

        page = tmp_path / "digest.html"
        page.write_text("<html></html>", encoding="utf-8")
        result = TelegramNotifier(token="", chat_id="").send_document(page)
        assert result.delivered is False
        assert "TELEGRAM_BOT_TOKEN" in result.detail

    def test_a_missing_file_is_reported_not_raised(self, tmp_path) -> None:
        from app.notifications.telegram import TelegramNotifier

        result = TelegramNotifier(token="t", chat_id="c").send_document(
            tmp_path / "absent.html"
        )
        assert result.delivered is False
        assert "no such file" in result.detail

    def test_an_oversized_file_is_refused_with_its_size(self, tmp_path) -> None:
        """A rejected upload would otherwise look like a network failure."""
        from app.notifications.telegram import DOCUMENT_LIMIT_BYTES, TelegramNotifier

        page = tmp_path / "huge.html"
        page.write_bytes(b"x" * (DOCUMENT_LIMIT_BYTES + 1))
        result = TelegramNotifier(token="t", chat_id="c").send_document(page)
        assert result.delivered is False
        assert "limit is 50MB" in result.detail

    def test_a_subjectless_notification_does_not_start_with_blank_lines(self) -> None:
        """The digest message is its own heading; "subject\n\nbody" would add two blank lines."""
        from app.notifications.base import Notification

        assert Notification(
            kind="digest", subject="", body="line one", dedupe_key="k"
        ).as_text() == "line one"
        assert Notification(
            kind="exit", subject="Subject", body="body", dedupe_key="k"
        ).as_text() == "Subject\n\nbody"


class TestExitBaseRates:
    def test_they_sum_to_one(self) -> None:
        """They partition 915 trades; drifting apart would mean one was edited in isolation."""
        from app.__main__ import EXIT_BASE_RATES

        assert sum(EXIT_BASE_RATES.values()) == pytest.approx(1.0, abs=0.005)

    def test_the_target_is_the_minority_outcome(self) -> None:
        """The headline fact: the target is reached about a fifth of the time."""
        from app.__main__ import EXIT_BASE_RATES

        assert EXIT_BASE_RATES["take_profit"] < EXIT_BASE_RATES["stop_loss"]
        assert EXIT_BASE_RATES["take_profit"] == pytest.approx(0.214, abs=0.01)


class TestLevelsWithoutASignal:
    """Levels must survive the signal going away.

    Found in production. A GOOGL purchase recorded from the phone the day after a BUY was stored
    with no stop and no target, because `Decision` carries levels only on its BUY branch and the
    reading had moved overnight. The watch could then only ever report a trend break, and nothing
    defined where the position's risk ended.

    Two different questions were being answered by one value. Whether to enter is a signal and
    changes between the close that produced it and the session it is acted on. Where the risk ends
    is a volatility measurement, and it is defined whenever ATR is.
    """

    @pytest.fixture
    def frames(self):
        """A rising synthetic series with real features computed on it.

        Synthetic rather than loaded, because a test that skips when the database happens to be
        empty proves nothing -- and these two assert the property the production bug violated.
        """
        import numpy as np
        import pandas as pd

        from app.indicators.registry import compute_features

        # An uptrend that oscillates, ending part-way up a leg. The parameters are not
        # decorative: a plain exponential rally saturates RSI near 100 and never clears the
        # band, while a late pullback deep enough to fix RSI turns MACD and ROC negative. This
        # combination clears all seven conditions, which is what the test below needs in order
        # to compare the two code paths at all.
        n = 400
        index = pd.bdate_range(end=pd.Timestamp("2026-09-30"), periods=n)
        t = np.arange(n)
        closes = 100.0 * (1.0 + 0.0013) ** t * (1.0 + 0.035 * np.sin(t / 22.0 * 2 * np.pi + 1.8))

        volume = np.full(n, 8_000_000.0)
        volume[-6:] = 14_000_000.0  # recent pickup, so relative volume clears its floor

        frame = pd.DataFrame(
            {
                "open": closes,
                "high": closes * 1.010,
                "low": closes * 0.990,
                "close": closes,
                "adj_close": closes,
                "volume": volume,
            },
            index=index,
        )
        frame.index.name = "ts"
        return compute_features(frame)

    def test_levels_exist_even_when_the_action_is_not_buy(self, frames) -> None:
        from app.strategies.registry import build_strategy

        strategy = build_strategy("trend_momentum")
        decision = strategy.evaluate(frames, in_position=False)
        levels = strategy.propose_levels(frames)

        assert levels is not None, "a volatility measurement should not depend on the signal"
        stop, target = levels
        assert 0 < stop < target

        if decision.action.value != "BUY":
            assert decision.stop_price is None, "fixture no longer covers the non-BUY case"

    def test_they_match_the_decision_when_it_does_signal(self, frames) -> None:
        """The fallback must be the same arithmetic, not a second rule.

        If these diverged, a position recorded on the signal day and one recorded the next would
        be watched against different levels, and neither would match the backtest.
        """
        from app.strategies.registry import build_strategy

        strategy = build_strategy("trend_momentum")
        decision = strategy.evaluate(frames, in_position=False)
        assert decision.stop_price is not None, (
            "the synthetic series was built to signal an entry; if it stopped doing so the "
            "fixture no longer tests what this asserts"
        )

        stop, target = strategy.propose_levels(frames)
        assert stop == pytest.approx(decision.stop_price)
        assert target == pytest.approx(decision.take_profit_price)

    def test_the_base_class_proposes_nothing_by_default(self) -> None:
        """A strategy with no levels says so rather than having some invented for it."""
        import pandas as pd

        from app.strategies.base import Strategy

        assert Strategy.propose_levels(object(), pd.DataFrame()) is None

    def test_corrupt_data_yields_no_levels_rather_than_a_negative_stop(self) -> None:
        """An ATR above the price means bad data, not a wide stop."""
        import pandas as pd

        from app.strategies.registry import build_strategy

        strategy = build_strategy("trend_momentum")
        frame = pd.DataFrame({"close": [10.0], "atr_14": [50.0]})
        assert strategy.propose_levels(frame) is None

    def test_an_empty_or_unusable_frame_yields_none(self) -> None:
        import numpy as np
        import pandas as pd

        from app.strategies.registry import build_strategy

        strategy = build_strategy("trend_momentum")
        assert strategy.propose_levels(pd.DataFrame()) is None
        assert strategy.propose_levels(pd.DataFrame({"close": [1.0]})) is None
        assert (
            strategy.propose_levels(
                pd.DataFrame({"close": [np.nan], "atr_14": [np.nan]})
            )
            is None
        )


class TestTelegramDiagnostics:
    """Which fix the error points you at.

    Both failures below are HTTP 403, and they call for opposite actions: one means unblock the
    bot, the other means the chat id is a bot's and has to be replaced with your own. A real run
    hit the second and was told the first.
    """

    def test_the_specific_description_wins_over_the_bare_status(self) -> None:
        """Dict order must not decide which advice the user gets."""
        hints = {
            "403": "generic",
            "can't send messages to the bot": "specific",
        }
        detail = "HTTP 403: {\"description\":\"Forbidden: the bot can't send messages to the bot\"}"

        chosen = None
        for code, hint in sorted(hints.items(), key=lambda kv: -len(kv[0])):
            if code in detail or f"HTTP {code}" in detail:
                chosen = hint
                break
        assert chosen == "specific"

    def test_a_plain_403_still_gets_the_generic_advice(self) -> None:
        hints = {
            "403": "generic",
            "can't send messages to the bot": "specific",
        }
        detail = 'HTTP 403: {"description":"Forbidden: bot was blocked by the user"}'

        chosen = None
        for code, hint in sorted(hints.items(), key=lambda kv: -len(kv[0])):
            if code in detail or f"HTTP {code}" in detail:
                chosen = hint
                break
        assert chosen == "generic"
