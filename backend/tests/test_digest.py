"""The static digest, and the privacy boundary it enforces.

Most of these are about what the file must *not* contain. The digest is the artifact a scheduled
job publishes to GitHub Pages, Pages on a free plan serves from a public repository, and position
sizes and entry prices are precisely the data that should not be world-readable. The default is
the enforcement, so the default is what gets tested.

The rest assert the language rules the whole project runs on: no forecast, no probability, no
"this will rise". A digest is the surface most likely to be skimmed, which makes it the surface
where a misleading phrase does the most damage.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.database.models import Asset, Bar
from app.portfolio.holdings import open_holding
from app.reporting.digest import collect_digest_data, render_digest


@pytest.fixture
def populated(session):
    """Universe synced with enough synthetic history for the scanner to run."""
    from app.data.engine import DataEngine

    DataEngine(session).sync_universe()

    index = pd.bdate_range(end=pd.Timestamp("2026-09-29"), periods=400)
    for symbol in ("SPY", "AAPL", "SQM", "ECH", "TSM", "AAXJ"):
        asset = session.query(Asset).filter_by(symbol=symbol, market_code="USA").one()
        closes = 100.0 + 0.1 * np.arange(400, dtype=float)
        for stamp, close in zip(index, closes):
            session.add(
                Bar(
                    asset_id=asset.id,
                    timeframe="1D",
                    ts=stamp.to_pydatetime(),
                    open=close,
                    high=close * 1.01,
                    low=close * 0.99,
                    close=close,
                    adj_close=close,
                    volume=5_000_000.0,
                )
            )
    session.flush()
    return session


class TestPrivacy:
    def test_holdings_are_excluded_by_default(self, populated) -> None:
        """The default is the enforcement. A public Pages site inherits it."""
        open_holding(populated, "SQM", 50, 47.30, opened_on=date(2026, 6, 1))
        data = collect_digest_data(populated)
        assert data["holdings"] is None

        page = render_digest(data)
        assert "47.3" not in page
        assert "Your positions" not in page

    def test_holdings_appear_only_when_asked_for(self, populated) -> None:
        open_holding(populated, "SQM", 50, 47.30, opened_on=date(2026, 6, 1))
        data = collect_digest_data(populated, include_holdings=True)
        assert data["holdings"] is not None

        page = render_digest(data)
        assert "Your positions" in page
        assert "47.3" in page

    def test_a_personal_digest_carries_a_do_not_publish_warning(self, populated) -> None:
        """The guard string the publish workflow greps for.

        The workflow fails the build if it finds this, so the wording is load-bearing: change it
        here and the CI guard stops working.
        """
        open_holding(populated, "SQM", 50, 47.30, opened_on=date(2026, 6, 1))
        page = render_digest(collect_digest_data(populated, include_holdings=True))
        assert "contains personal position data" in page
        assert "world-readable" in page

    def test_a_market_only_digest_does_not_trip_the_guard(self, populated) -> None:
        open_holding(populated, "SQM", 50, 47.30, opened_on=date(2026, 6, 1))
        page = render_digest(collect_digest_data(populated))
        assert "contains personal position data" not in page


class TestLanguage:
    def test_it_says_a_score_is_not_a_probability(self, populated) -> None:
        page = render_digest(collect_digest_data(populated))
        assert "not a probability" in page

    def test_it_never_claims_an_instrument_will_move(self, populated) -> None:
        page = render_digest(collect_digest_data(populated)).lower()
        # The page contains "will rise or fall" inside an explicit denial; strip the denial
        # before checking, so the assertion catches a genuine claim rather than the disclaimer.
        stripped = page.replace("says any instrument\nwill rise or fall", "")
        stripped = stripped.replace("will rise or fall", "")
        for forbidden in ("will rise", "will fall", "expected to reach", "price target of"):
            assert forbidden not in stripped, f"the digest implies {forbidden!r}"

    def test_it_states_the_strategy_is_not_shown_to_be_profitable(self, populated) -> None:
        page = render_digest(collect_digest_data(populated))
        assert "not been shown to be profitable" in page

    def test_it_states_the_usd_denomination(self, populated) -> None:
        """A Chilean ADR's return is the Chilean move plus the currency move."""
        page = render_digest(collect_digest_data(populated))
        assert "priced in USD" in page
        assert "currency move" in page

    def test_it_says_no_orders_are_placed(self, populated) -> None:
        page = render_digest(collect_digest_data(populated))
        assert "places no orders" in page


class TestContent:
    def test_every_region_gets_a_section(self, populated) -> None:
        page = render_digest(collect_digest_data(populated))
        for region in ("United States", "Chile", "Emerging Asia"):
            assert f"<h2>{region}</h2>" in page

    def test_each_region_names_its_own_benchmark(self, populated) -> None:
        """Not SPY for all three, which is what a market-keyed lookup would have given."""
        data = collect_digest_data(populated)
        symbols = {e["region"]: e["benchmark"].symbol for e in data["regions"]}
        assert symbols == {
            "United States": "SPY",
            "Chile": "ECH",
            "Emerging Asia": "AAXJ",
        }

    def test_data_freshness_is_stated_prominently(self, populated) -> None:
        """A digest built on stale data looks identical to one built on today's."""
        page = render_digest(collect_digest_data(populated))
        assert "Data as of" in page
        assert "this line is how you tell" in page

    def test_it_is_a_single_self_contained_file(self, populated) -> None:
        """No external reference of any kind: it has to work from a file:// URL with no network.

        Inline script is allowed and this assertion used to forbid it, which was stricter than
        the rule it protected. What breaks the page is a dependency it cannot reach -- a CDN, a
        stylesheet, a fetch -- not a handler defined in the document itself.
        """
        page = render_digest(collect_digest_data(populated))
        assert "fetch(" not in page
        assert "<link" not in page.lower()
        assert "http://" not in page and "https://" not in page
        assert page.startswith("<!doctype html>")

    def test_it_renders_on_a_phone(self, populated) -> None:
        page = render_digest(collect_digest_data(populated))
        assert 'name="viewport"' in page
        assert "@media (max-width:640px)" in page

    def test_a_missing_price_is_not_rendered_as_zero(self, populated) -> None:
        """A zero return and an unknown one are different facts."""
        from app.reporting.digest import _pct, _price

        assert "n/a" in _pct(None)
        assert "n/a" in _price(None)
        assert "0.00" not in _pct(None)

    def test_prices_keep_two_decimals(self, populated) -> None:
        """Rounding to whole units turned ENIC's 4.12 into "4"."""
        from app.reporting.digest import _price

        assert _price(4.12) == "4.12"


class TestPositionBlock:
    """The one part of the page about the reader's own money.

    These assert the things that were wrong when it was first built and the things that would be
    misleading if they regressed.
    """

    @pytest.fixture
    def with_position(self, populated):
        from datetime import date

        from app.portfolio.holdings import open_holding

        open_holding(
            populated,
            "AAPL",
            0.0579,
            351.44,
            opened_on=date(2026, 9, 25),
            stop_price=327.17,
            take_profit_price=394.81,
            entry_amount=20.36,
            broker="fintual",
        )
        return populated

    def test_the_position_is_marked_even_when_the_rule_cannot_be_evaluated(
        self, with_position
    ) -> None:
        """Two different questions that were sharing one answer.

        On the day you buy there is no later bar, so the exit rule cannot be evaluated -- but
        the position can still be marked against the newest stored close. Conflating them
        printed "n/a" for a value the system had.
        """
        page = render_digest(collect_digest_data(with_position, include_holdings=True))
        assert "WORTH NOW" in page.upper()
        assert "20.36 USD" in page, "the cash actually spent should be shown"

    def test_the_chart_draws_the_stop_and_the_target(self, with_position) -> None:
        page = render_digest(collect_digest_data(with_position, include_holdings=True))
        assert "stop 327" in page
        assert "target 395" in page or "target 394" in page
        assert "you 351" in page

    def test_the_chart_is_touchable(self, with_position) -> None:
        """A phone has no hover, so the readout is driven by pointer events on wide columns."""
        page = render_digest(collect_digest_data(with_position, include_holdings=True))
        assert 'class="hit"' in page
        assert "pointerenter" in page and "pointerdown" in page
        assert 'class="pcdata"' in page

    def test_the_exit_rules_are_explained_with_their_real_frequencies(
        self, with_position
    ) -> None:
        """The target is the least common exit, and the page has to say so.

        The intuitive reading -- that a target marks where the price is expected to turn --
        leads somewhere different from the rule, which is a pre-committed exit at three times
        the risk.
        """
        page = render_digest(collect_digest_data(with_position, include_holdings=True))
        assert "39.8%" in page and "33.2%" in page and "21.4%" in page
        assert "target is not a prediction" in page.lower()
        assert "trend break" in page.lower()

    def test_the_trend_line_is_drawn(self, with_position) -> None:
        """It is the exit that fires most often; a chart without it misstates the odds."""
        page = render_digest(collect_digest_data(with_position, include_holdings=True))
        assert 'stroke-dasharray="2 3"' in page

    def test_a_position_with_no_stop_says_so(self, populated) -> None:
        from datetime import date

        from app.portfolio.holdings import open_holding

        open_holding(populated, "AAPL", 1, 200.0, opened_on=date(2026, 9, 25))
        page = render_digest(collect_digest_data(populated, include_holdings=True))
        assert "No stop is recorded" in page
        assert "set-levels" in page

    def test_the_page_stays_self_contained(self, with_position) -> None:
        """Inline script is fine; a fetch or an external file is not.

        The digest has to open from a file:// URL and from a phone with no network, which is
        what makes it reach the user at all.
        """
        page = render_digest(collect_digest_data(with_position, include_holdings=True))
        assert "<link" not in page.lower()
        assert "fetch(" not in page
        assert "src=" not in page.lower().replace('src="data:', "")
