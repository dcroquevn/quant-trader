"""Runner tests: the split guard, warm-up handling, and end-to-end wiring.

``resolve_window`` is the most safety-critical function in Phase 2. It is the single
place that turns a named data partition into dates, and it is what stops the Phase 4
optimiser from reaching test data. A leak there does not produce an error — it produces a
believable out-of-sample number that is nothing of the kind.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.backtesting.runner import load_features, resolve_window, run_backtest
from app.core.exceptions import DataLeakageError, InsufficientDataError
from app.core.universe import find_asset
from app.database import repository as repo
from app.strategies.registry import build_strategy
from tests.conftest import FakeProvider, make_bars


def store(session, symbols: list[str], market: str = "USA", *, bars: int = 900, seed: int = 5):
    """Persist long histories for real universe symbols so a backtest can run."""
    from app.data.engine import DataEngine

    repo.sync_markets(session)
    specs = [find_asset(s, market).with_provider_symbol("fake", s) for s in symbols]
    # 900 business days from 2016 covers warm-up plus the train split.
    index = pd.bdate_range("2015-06-01", periods=bars, tz="UTC", name="ts")
    frames = {}
    for i, spec in enumerate(specs):
        frame = make_bars(len(index), seed=seed + i, annual_drift=0.15)
        frame.index = index
        frames[spec.symbol] = frame

    engine = DataEngine(session, provider=FakeProvider(frames))
    for spec in specs:
        # Explicit start: the default fetch window is the last ten years, which would
        # exclude a short fixture dated 2015 entirely and fail with "no rows".
        result = engine.download_symbol(
            spec, incremental=False, start=date(2015, 1, 1), end=date(2026, 12, 31)
        )
        assert result.ok, f"fixture failed for {spec.symbol}: {result.error}"
    session.commit()


# --------------------------------------------------------------------------- #
# The split guard
# --------------------------------------------------------------------------- #


class TestResolveWindow:
    def test_train_is_open(self) -> None:
        window = resolve_window("train")
        assert window.split == "train"
        assert window.start < window.end

    def test_validation_is_open(self) -> None:
        assert resolve_window("validation").split == "validation"

    def test_test_is_refused_by_default(self) -> None:
        """The core guarantee. Reading TEST during a search destroys the only
        out-of-sample estimate there is, so the default must be refusal."""
        with pytest.raises(DataLeakageError) as excinfo:
            resolve_window("test")

        message = str(excinfo.value)
        assert "TEST" in message
        assert "second validation set" in message
        assert "finalising" in message

    def test_test_opens_only_when_finalising(self) -> None:
        window = resolve_window("test", finalising=True)
        assert window.split == "test"

    def test_finalising_does_not_change_other_splits(self) -> None:
        """The flag is about permission, not about which dates come back."""
        assert resolve_window("train") == resolve_window("train", finalising=True)

    def test_splits_do_not_overlap(self) -> None:
        train = resolve_window("train")
        validation = resolve_window("validation")
        test = resolve_window("test", finalising=True)
        assert train.end < validation.start
        assert validation.end < test.start

    def test_full_uses_the_whole_history(self) -> None:
        window = resolve_window("full")
        assert window.split == "full"
        assert window.start.year < 2000

    def test_explicit_dates_narrow_full(self) -> None:
        window = resolve_window("full", start=date(2020, 1, 1), end=date(2021, 1, 1))
        assert window.start == date(2020, 1, 1)
        assert window.end == date(2021, 1, 1)

    def test_a_split_cannot_be_widened_past_its_bounds(self) -> None:
        """Asking for 2010 inside the train split must not reach outside it.

        Otherwise ``--split train --start 2010`` would quietly read data the split does
        not contain, and the partition would stop meaning anything.
        """
        bounds = resolve_window("train")
        widened = resolve_window("train", start=date(2010, 1, 1), end=date(2030, 1, 1))
        assert widened.start == bounds.start
        assert widened.end == bounds.end

    def test_a_split_can_be_narrowed(self) -> None:
        window = resolve_window("train", start=date(2018, 1, 1), end=date(2019, 1, 1))
        assert window.start == date(2018, 1, 1)
        assert window.end == date(2019, 1, 1)

    def test_a_non_overlapping_range_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="does not overlap"):
            resolve_window("train", start=date(2030, 1, 1), end=date(2031, 1, 1))

    def test_unknown_split_is_rejected(self) -> None:
        with pytest.raises(KeyError, match="Unknown split"):
            resolve_window("holdout")

    def test_describe_states_the_partition_and_dates(self) -> None:
        text = resolve_window("train").describe()
        assert "train" in text
        assert "to" in text


# --------------------------------------------------------------------------- #
# Feature loading
# --------------------------------------------------------------------------- #


class TestLoadFeatures:
    def test_computes_features_for_stored_symbols(self, session) -> None:
        store(session, ["AAPL", "MSFT"])
        frames, skipped = load_features(session, ["AAPL", "MSFT"], "USA")

        assert set(frames) == {"AAPL", "MSFT"}
        assert skipped == []
        assert "rsi_14" in frames["AAPL"].columns
        assert "ema_200" in frames["AAPL"].columns

    def test_skips_symbols_with_too_little_history(self, session) -> None:
        """Including them with NaN features would produce HOLDs that look considered."""
        store(session, ["AAPL"], bars=900, seed=5)
        store(session, ["MSFT"], bars=50, seed=99)

        frames, skipped = load_features(session, ["AAPL", "MSFT"], "USA")
        assert "AAPL" in frames
        assert "MSFT" not in frames
        assert any("MSFT" in note for note in skipped)
        assert any("needs 252" in note for note in skipped)

    def test_raises_when_nothing_is_usable(self, session) -> None:
        repo.sync_markets(session)
        with pytest.raises(InsufficientDataError, match="download-data"):
            load_features(session, ["AAPL"], "USA")

    def test_output_carries_no_forward_columns(self, session) -> None:
        """A backtest input must never include outcome labels."""
        store(session, ["AAPL"])
        frames, _ = load_features(session, ["AAPL"], "USA")
        assert not [c for c in frames["AAPL"].columns if c.startswith("forward_")]


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


class TestRunBacktest:
    def test_produces_metrics_and_a_result(self, session) -> None:
        store(session, ["AAPL", "MSFT", "NVDA"])
        result = run_backtest(
            session,
            build_strategy("trend_momentum"),
            "USA",
            split="train",
            include_benchmark=False,
        )

        assert result.config.split == "train"
        assert result.metrics
        assert "sharpe" in result.metrics
        assert "total_return_pct" in result.metrics
        assert len(result.equity_curve) > 100

    def test_refuses_the_test_split_without_finalising(self, session) -> None:
        store(session, ["AAPL"])
        with pytest.raises(DataLeakageError):
            run_backtest(
                session, build_strategy("trend_momentum"), "USA", split="test"
            )

    def test_warmup_history_is_loaded_before_the_window(self, session) -> None:
        """Indicators must be warm on the window's first traded bar.

        Slicing to the window and computing features afterwards would leave the first 252
        bars with NaN features, silently disabling the strategy for a year.
        """
        store(session, ["AAPL", "MSFT"])
        result = run_backtest(
            session,
            build_strategy("trend_momentum"),
            "USA",
            split="train",
            include_benchmark=False,
        )

        window = resolve_window("train")
        # Trading is confined to the window even though earlier bars were loaded.
        assert result.start_date.date() >= window.start
        assert result.end_date.date() <= window.end

    def test_respects_an_explicit_symbol_list(self, session) -> None:
        store(session, ["AAPL", "MSFT", "NVDA"])
        result = run_backtest(
            session,
            build_strategy("trend_momentum"),
            "USA",
            split="train",
            symbols=["AAPL"],
            include_benchmark=False,
        )
        assert result.universe == ["AAPL"]

    def test_skipped_symbols_are_recorded_as_a_limitation(self, session) -> None:
        """The universe actually traded must be visible when it is smaller than asked."""
        store(session, ["AAPL", "MSFT"], bars=900, seed=5)
        store(session, ["NVDA"], bars=40, seed=77)

        result = run_backtest(
            session,
            build_strategy("trend_momentum"),
            "USA",
            split="train",
            symbols=["AAPL", "MSFT", "NVDA"],
            include_benchmark=False,
        )
        assert any("insufficient history" in item for item in result.limitations)
        assert "NVDA" not in result.universe

    def test_limitations_always_include_survivorship_and_costs(self, session) -> None:
        store(session, ["AAPL"])
        result = run_backtest(
            session, build_strategy("trend_momentum"), "USA",
            split="train", include_benchmark=False,
        )
        joined = " ".join(result.limitations).lower()
        assert "survivorship" in joined
        assert "transaction costs" in joined

    def test_benchmark_is_unavailable_without_stored_benchmark_bars(self, session) -> None:
        """A missing benchmark must say so, never silently compare against nothing."""
        store(session, ["AAPL"])
        result = run_backtest(
            session, build_strategy("trend_momentum"), "USA",
            split="train", include_benchmark=True,
        )
        assert result.benchmark_metrics["available"] is False
        assert "SPY" in result.benchmark_metrics["reason"]

    def test_capital_override_is_honoured(self, session) -> None:
        store(session, ["AAPL"])
        result = run_backtest(
            session, build_strategy("trend_momentum"), "USA",
            split="train", initial_capital=25_000.0, include_benchmark=False,
        )
        assert result.config.initial_capital == 25_000.0
        assert result.equity_curve.iloc[0] == pytest.approx(25_000.0, rel=0.01)

    def test_result_is_json_serialisable(self, session) -> None:
        import json

        store(session, ["AAPL"])
        result = run_backtest(
            session, build_strategy("trend_momentum"), "USA",
            split="train", include_benchmark=False,
        )
        json.dumps(result.metrics)
        json.dumps([t.to_dict() for t in result.trades])
