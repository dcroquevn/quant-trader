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
