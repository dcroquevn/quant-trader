"""Recording real positions, and the arithmetic on them.

These assert two different kinds of thing and it is worth separating them:

* The arithmetic -- P&L, percentages, holding periods. Ordinary correctness.
* The refusals -- a second open lot in one symbol, a stop above entry, an exit before an entry.
  Each of those would produce a number that looks fine and is wrong, which is worse than an
  error, so the refusals are the load-bearing tests here.

And one that is neither: ``test_few_trades_are_reported_as_no_evidence``. Real money makes a
result feel like proof in a way a backtest does not, so the wording that says four trades
establish nothing is part of the contract.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.database.models import Holding
from app.portfolio.holdings import (
    close_holding,
    list_holdings,
    open_holding,
    realised_performance,
)


@pytest.fixture
def stocked(session):
    """A session with the universe synced, so holdings can reference real assets."""
    from app.data.engine import DataEngine

    DataEngine(session).sync_universe()
    return session


class TestOpening:
    def test_records_what_was_paid(self, stocked) -> None:
        holding = open_holding(
            stocked, "SQM", 50, 47.30, opened_on=date(2026, 6, 1), broker="fintual"
        )
        assert holding.symbol == "SQM"
        assert holding.entry_price == 47.30
        assert holding.quantity == 50
        assert holding.currency == "USD"
        assert holding.market == "USA"
        assert holding.is_open

    def test_region_travels_with_the_holding(self, stocked) -> None:
        """So a report can group by exposure without re-looking-up every symbol."""
        assert open_holding(stocked, "SQM", 1, 47.0).region == "Chile"
        assert open_holding(stocked, "TSM", 1, 400.0).region == "Emerging Asia"
        assert open_holding(stocked, "AAPL", 1, 300.0).region == "United States"

    def test_symbol_must_be_in_the_universe(self, stocked) -> None:
        """An unrecorded instrument would be stored and then never checked.

        The watch needs stored bars and a features pipeline. A holding in something the system
        does not model is the worst outcome available: the user believes it is being watched.
        """
        with pytest.raises(KeyError, match="not in the declared universe"):
            open_holding(stocked, "NOTATICKER", 1, 10.0)

    def test_santiago_tickers_are_no_longer_recordable(self, stocked) -> None:
        """They were removed from the universe, so this fails at the lookup."""
        with pytest.raises(KeyError):
            open_holding(stocked, "SQM-B", 1, 40000.0)

    def test_second_lot_in_one_symbol_is_refused(self, stocked) -> None:
        """Averaging two lots needs accounting this does not have.

        Silently summing them would misreport the entry price, and therefore the P&L, the
        percentage and every excursion figure derived from it.
        """
        open_holding(stocked, "SQM", 10, 47.0)
        with pytest.raises(ValueError, match="already has an open holding"):
            open_holding(stocked, "SQM", 10, 48.0)

    def test_a_closed_lot_does_not_block_a_new_one(self, stocked) -> None:
        holding = open_holding(stocked, "SQM", 10, 47.0, opened_on=date(2026, 1, 5))
        close_holding(stocked, holding.id, 50.0, closed_on=date(2026, 2, 5))
        again = open_holding(stocked, "SQM", 10, 52.0, opened_on=date(2026, 3, 5))
        assert again.id != holding.id

    def test_stop_above_entry_is_refused(self, stocked) -> None:
        """It would trigger on the first bar, which is always a typo rather than a plan."""
        with pytest.raises(ValueError, match="at or above the entry price"):
            open_holding(stocked, "SQM", 10, 47.0, stop_price=48.0)

    def test_target_below_entry_is_refused(self, stocked) -> None:
        with pytest.raises(ValueError, match="at or below the entry price"):
            open_holding(stocked, "SQM", 10, 47.0, take_profit_price=46.0)

    @pytest.mark.parametrize("quantity,price", [(0, 47.0), (-5, 47.0), (10, 0), (10, -1)])
    def test_nonsense_quantities_and_prices_are_refused(
        self, stocked, quantity, price
    ) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            open_holding(stocked, "SQM", quantity, price)


class TestClosing:
    def test_pnl_is_net_of_the_fees_reported(self, stocked) -> None:
        holding = open_holding(
            stocked, "SQM", 100, 50.0, opened_on=date(2026, 1, 10), entry_fees=5.0
        )
        closed = close_holding(
            stocked, holding.id, 55.0, closed_on=date(2026, 3, 11), exit_fees=5.0
        )
        assert closed.gross_pnl == pytest.approx(500.0)
        assert closed.pnl == pytest.approx(490.0)
        assert closed.holding_period_days == 60

    def test_percentage_is_on_the_cost_basis_including_the_entry_fee(self, stocked) -> None:
        """Dividing by the entry price alone would flatter every trade by the fee."""
        holding = open_holding(stocked, "SQM", 100, 50.0, entry_fees=100.0)
        closed = close_holding(stocked, holding.id, 55.0, exit_fees=0.0)
        # 400 net on 5,100 committed, not 500 on 5,000.
        assert closed.pnl_pct == pytest.approx(400.0 / 5100.0 * 100.0)

    def test_a_loss_is_reported_as_a_loss(self, stocked) -> None:
        holding = open_holding(stocked, "ECH", 10, 40.0)
        closed = close_holding(stocked, holding.id, 36.0, exit_fees=1.0)
        assert closed.pnl == pytest.approx(-41.0)
        assert closed.pnl_pct is not None and closed.pnl_pct < 0

    def test_closing_twice_is_refused(self, stocked) -> None:
        holding = open_holding(stocked, "SQM", 10, 47.0)
        close_holding(stocked, holding.id, 50.0)
        with pytest.raises(ValueError, match="already closed"):
            close_holding(stocked, holding.id, 51.0)

    def test_exit_before_entry_is_refused(self, stocked) -> None:
        holding = open_holding(stocked, "SQM", 10, 47.0, opened_on=date(2026, 6, 1))
        with pytest.raises(ValueError, match="before the entry date"):
            close_holding(stocked, holding.id, 50.0, closed_on=date(2026, 5, 1))

    def test_closing_compares_dates_not_datetimes(self, stocked) -> None:
        """SQLite does not round-trip a UTC offset, so the stored value comes back naive.

        Comparing it against an aware ``datetime.now`` raised TypeError on every sale. The
        schema's own docstring warns that an offset is not preserved; this was the first code
        to depend on one being.
        """
        holding = open_holding(stocked, "SQM", 10, 47.0, opened_on=date(2026, 6, 1))
        stocked.expire_all()
        closed = close_holding(stocked, holding.id, 50.0, closed_on=date(2026, 6, 1))
        assert closed.holding_period_days == 0

    def test_unknown_holding_raises(self, stocked) -> None:
        with pytest.raises(KeyError, match="No holding with id"):
            close_holding(stocked, 99999, 50.0)


class TestRealisedPerformance:
    def test_no_trades_says_so_plainly(self, stocked) -> None:
        performance = realised_performance(stocked)
        assert performance["n_closed"] == 0
        assert "nothing here can say" in performance["evidence"].lower()

    def test_arithmetic_across_several_trades(self, stocked) -> None:
        for symbol, entry, exit_price in [("SQM", 50.0, 55.0), ("ECH", 40.0, 36.0)]:
            holding = open_holding(stocked, symbol, 10, entry)
            close_holding(stocked, holding.id, exit_price)
        performance = realised_performance(stocked)
        assert performance["n_closed"] == 2
        assert performance["net_pnl"] == pytest.approx(10.0)  # +50 and -40
        assert performance["n_winners"] == 1
        assert performance["n_losers"] == 1

    def test_few_trades_are_reported_as_no_evidence(self, stocked) -> None:
        """Real money feels like proof in a way a backtest does not. It is not.

        Four real trades are a smaller sample than any backtest in this project and establish
        less, not more. The wording that says so is part of the contract, not decoration.
        """
        for i in range(3):
            holding = open_holding(stocked, ["SQM", "ECH", "TSM"][i], 10, 50.0)
            close_holding(stocked, holding.id, 60.0)
        evidence = realised_performance(stocked)["evidence"].lower()
        assert "too few" in evidence
        assert "not evidence" in evidence or "randomness" in evidence

    def test_thirty_trades_still_refuses_to_claim_an_edge(self, stocked) -> None:
        """Even at a sample where statistics become discussable, the note stays honest."""
        from app.portfolio.holdings import _evidence_statement

        assert "not independent" in _evidence_statement(40, 25)
        assert "confidence interval" in _evidence_statement(20, 12)


class TestListing:
    def test_open_and_closed_are_distinguishable(self, stocked) -> None:
        open_one = open_holding(stocked, "SQM", 10, 47.0)
        closed_one = open_holding(stocked, "ECH", 10, 40.0)
        close_holding(stocked, closed_one.id, 42.0)

        everything = list_holdings(stocked)
        assert {h.id for h in everything} == {open_one.id, closed_one.id}

        just_open = list_holdings(stocked, include_closed=False)
        assert [h.id for h in just_open] == [open_one.id]

    def test_holdings_are_not_backtest_trades(self, stocked) -> None:
        """Different tables, deliberately. A modelled fill and a real one must not average."""
        from app.database.models import Trade

        open_holding(stocked, "SQM", 10, 47.0)
        assert Holding.__tablename__ != Trade.__tablename__
        assert stocked.query(Trade).count() == 0
