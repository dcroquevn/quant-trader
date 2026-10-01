"""The portable positions file.

This is the only state in the project that nothing can rebuild. Price history re-downloads in
minutes; what the user bought is known only to them and to this file. It is also the file a
scheduled job imports on every run, so the property that matters most is not that it round-trips
once but that importing it repeatedly changes nothing.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.portfolio.holdings import (
    close_holding,
    export_positions,
    import_positions,
    list_holdings,
    open_holding,
)


@pytest.fixture
def stocked(session):
    from app.data.engine import DataEngine

    DataEngine(session).sync_universe()
    return session


class TestRoundTrip:
    def test_an_open_position_survives(self, stocked) -> None:
        open_holding(
            stocked,
            "SQM",
            50,
            47.30,
            opened_on=date(2026, 6, 1),
            stop_price=44.0,
            take_profit_price=55.0,
            entry_fees=1.2,
            broker="fintual",
            note="scanner said BUY",
        )
        rows = export_positions(stocked)

        for holding in list_holdings(stocked):
            stocked.delete(holding)
        stocked.flush()
        assert list_holdings(stocked) == []

        assert import_positions(stocked, rows) == {"added": 1, "skipped": 0, "failed": 0}
        restored = list_holdings(stocked)[0]
        assert restored.symbol == "SQM"
        assert restored.quantity == 50
        assert restored.entry_price == 47.30
        assert restored.stop_price == 44.0
        assert restored.take_profit_price == 55.0
        assert restored.entry_fees == 1.2
        assert restored.broker == "fintual"
        assert restored.opened_on.date() == date(2026, 6, 1)

    def test_a_closed_position_keeps_its_pnl(self, stocked) -> None:
        """The exit has to survive too, or the realised record is lost on an eviction."""
        holding = open_holding(stocked, "ECH", 10, 40.0, opened_on=date(2026, 1, 5))
        close_holding(stocked, holding.id, 44.0, closed_on=date(2026, 3, 5), exit_fees=0.8)
        expected_pnl = list_holdings(stocked)[0].pnl

        rows = export_positions(stocked)
        for h in list_holdings(stocked):
            stocked.delete(h)
        stocked.flush()

        import_positions(stocked, rows)
        restored = list_holdings(stocked)[0]
        assert restored.closed_on is not None
        assert restored.exit_price == 44.0
        assert restored.pnl == pytest.approx(expected_pnl)


class TestIdempotency:
    def test_importing_twice_adds_nothing(self, stocked) -> None:
        """The daily job runs this every time.

        Without the skip, a week of scheduled runs would turn one position into seven and every
        P&L figure derived from them would be wrong -- silently, because each individual run
        looks like it worked.
        """
        open_holding(stocked, "SQM", 50, 47.30, opened_on=date(2026, 6, 1))
        rows = export_positions(stocked)

        assert import_positions(stocked, rows)["skipped"] == 1
        assert import_positions(stocked, rows)["skipped"] == 1
        assert len(list_holdings(stocked)) == 1

    def test_a_new_position_is_added_alongside_existing_ones(self, stocked) -> None:
        open_holding(stocked, "SQM", 50, 47.30, opened_on=date(2026, 6, 1))
        rows = export_positions(stocked)
        rows.append(
            {
                "symbol": "TSM",
                "market": "USA",
                "quantity": 10,
                "entry_price": 400.0,
                "opened_on": "2026-07-01",
                "strategy_name": "trend_momentum",
            }
        )
        result = import_positions(stocked, rows)
        assert result == {"added": 1, "skipped": 1, "failed": 0}
        assert {h.symbol for h in list_holdings(stocked)} == {"SQM", "TSM"}


class TestRobustness:
    def test_one_bad_row_does_not_cost_the_others(self, stocked) -> None:
        """A single malformed record must not abort the import of every valid one."""
        rows = [
            {
                "symbol": "NOTATICKER",
                "quantity": 1,
                "entry_price": 1.0,
                "opened_on": "2026-06-01",
            },
            {
                "symbol": "SQM",
                "market": "USA",
                "quantity": 50,
                "entry_price": 47.30,
                "opened_on": "2026-06-01",
            },
        ]
        result = import_positions(stocked, rows)
        assert result["failed"] == 1
        assert result["added"] == 1
        assert [h.symbol for h in list_holdings(stocked)] == ["SQM"]

    def test_export_order_is_stable(self, stocked) -> None:
        """The file gets committed; an unstable order would diff on every run.

        A diff on every run makes a real change impossible to spot, which defeats the point of
        keeping it in version control.
        """
        open_holding(stocked, "TSM", 10, 400.0, opened_on=date(2026, 7, 1))
        open_holding(stocked, "SQM", 50, 47.30, opened_on=date(2026, 6, 1))
        open_holding(stocked, "ECH", 10, 40.0, opened_on=date(2026, 6, 1))

        first = export_positions(stocked)
        assert [r["symbol"] for r in first] == ["ECH", "SQM", "TSM"]
        assert export_positions(stocked) == first

    def test_an_empty_export_is_valid(self, stocked) -> None:
        assert export_positions(stocked) == []
        assert import_positions(stocked, []) == {"added": 0, "skipped": 0, "failed": 0}


class TestFillQuality:
    """Where a fill landed in its day's range.

    The only part of a trade the user can improve: whether the strategy works is an open
    question, but whether a purchase went through near the day's high is a fact they can change
    with a limit order. It also settles whether a broker added a spread, which otherwise has no
    answer.
    """

    @pytest.fixture
    def with_bars(self, stocked):
        import numpy as np
        import pandas as pd

        from app.database.models import Asset, Bar

        asset = stocked.query(Asset).filter_by(symbol="GOOGL", market_code="USA").one()
        index = pd.bdate_range(end=pd.Timestamp("2026-10-01"), periods=5)
        for i, stamp in enumerate(index):
            stocked.add(
                Bar(
                    asset_id=asset.id,
                    timeframe="1D",
                    ts=stamp.to_pydatetime(),
                    open=340.0,
                    high=353.22,
                    low=335.51,
                    close=338.24,
                    adj_close=338.24,
                    volume=33_000_000.0,
                )
            )
        stocked.flush()
        return stocked

    def test_a_fill_inside_the_range_rules_out_a_markup(self, with_bars) -> None:
        from app.portfolio.holdings import assess_fill

        quality = assess_fill(with_bars, "GOOGL", 351.44, date(2026, 10, 1))
        assert quality is not None
        assert quality.inside_range
        assert quality.percentile == pytest.approx(90, abs=1)
        assert "No broker markup" in quality.describe()

    def test_it_quantifies_what_a_worse_entry_cost(self, with_bars) -> None:
        from app.portfolio.holdings import assess_fill

        quality = assess_fill(with_bars, "GOOGL", 351.44, date(2026, 10, 1))
        assert quality.cost_vs_low == pytest.approx(4.75, abs=0.05)
        assert "4.75% less" in quality.describe()

    def test_a_fill_above_the_high_is_named_as_a_markup(self, with_bars) -> None:
        """The market never traded there, so the difference came from somewhere else."""
        from app.portfolio.holdings import assess_fill

        quality = assess_fill(with_bars, "GOOGL", 360.0, date(2026, 10, 1))
        assert not quality.inside_range
        text = quality.describe()
        assert "above the whole range" in text
        assert "spread or a fee" in text

    def test_a_missing_bar_returns_none_rather_than_a_nearby_day(self, with_bars) -> None:
        """A different session's range would answer a different question and look like an answer."""
        from app.portfolio.holdings import assess_fill

        assert assess_fill(with_bars, "GOOGL", 351.44, date(2020, 1, 2)) is None

    def test_it_is_ascii_only(self, with_bars) -> None:
        from app.portfolio.holdings import assess_fill

        assess_fill(with_bars, "GOOGL", 351.44, date(2026, 10, 1)).describe().encode("ascii")
