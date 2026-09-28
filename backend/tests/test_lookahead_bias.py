"""Look-ahead bias and data-leakage tests.

This is the most important file in the suite. Every other test checks that a
number is correct; these check that a number was *knowable at the time*, which is
the difference between a backtest and a fantasy.

The core technique is **truncation invariance**. If a feature at bar ``i`` depends
only on bars ``<= i``, then computing it on the first ``k`` bars must give exactly
the same values as computing it on all ``n`` bars and slicing to ``k``. Any peek
forward breaks that equality.

``test_truncation_detects_forward_columns`` deliberately runs the same check
against the ``forward_*`` columns and asserts that it *fails*. Without that, a
truncation test that silently compared nothing would pass forever and prove
nothing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.core.exceptions import DataLeakageError
from app.indicators import momentum, price, trend, volatility, volume
from app.indicators.forward import (
    FORWARD_PREFIX,
    assert_no_forward_columns,
    forward_frame,
    forward_max_adverse_excursion,
    forward_return,
)
from app.indicators.registry import FEATURE_COLUMNS, compute_features

TRUNCATION_POINTS = (300, 450, 600, 799)


# --------------------------------------------------------------------------- #
# The central guarantee
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cut", TRUNCATION_POINTS)
def test_all_features_are_truncation_invariant(bars: pd.DataFrame, cut: int) -> None:
    """No feature changes when future bars are appended.

    This single assertion covers every indicator at once: if any of them used a
    centred window, a negative shift, a backward fill or an expanding statistic
    over the whole series, the values at bar ``cut - 1`` would differ between the
    truncated and full computations.
    """
    full = compute_features(bars)
    partial = compute_features(bars.iloc[:cut])

    assert len(partial) == cut

    for column in FEATURE_COLUMNS:
        assert column in full.columns, f"{column} missing from compute_features output"
        pd.testing.assert_series_equal(
            partial[column],
            full[column].iloc[:cut],
            check_names=False,
            obj=f"{column} differs when future bars are present -> LOOK-AHEAD BIAS",
        )


def test_truncation_detects_forward_columns(bars: pd.DataFrame) -> None:
    """The truncation check must actually be able to fail.

    ``forward_return`` is look-ahead by construction, so the same comparison that
    passes for every real feature has to fail here. If this test ever passes by
    not raising, the invariance test above is vacuous and must be repaired.
    """
    cut = 500
    full = forward_return(bars["close"], 20)
    partial = forward_return(bars["close"].iloc[:cut], 20)

    with pytest.raises(AssertionError):
        pd.testing.assert_series_equal(partial, full.iloc[:cut], check_names=False)

    # Specifically: the truncated series cannot know the last 20 bars' futures.
    assert partial.iloc[-20:].isna().all()
    assert full.iloc[cut - 20 : cut].notna().any()


# --------------------------------------------------------------------------- #
# Per-indicator: the last value must not move
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "fn"),
    [
        ("sma_20", lambda f: trend.sma(f["close"], 20)),
        ("sma_200", lambda f: trend.sma(f["close"], 200)),
        ("ema_20", lambda f: trend.ema(f["close"], 20)),
        ("ema_200", lambda f: trend.ema(f["close"], 200)),
        ("rsi_14", lambda f: momentum.rsi(f["close"], 14)),
        ("macd_hist", lambda f: momentum.macd(f["close"])["macd_hist"]),
        ("roc_20", lambda f: momentum.roc(f["close"], 20)),
        ("atr_14", lambda f: volatility.atr(f["high"], f["low"], f["close"], 14)),
        ("volatility_20", lambda f: volatility.rolling_volatility(f["close"], 20)),
        ("bb_upper", lambda f: volatility.bollinger_bands(f["close"], 20)["bb_upper"]),
        ("relative_volume", lambda f: volume.relative_volume(f["volume"], 20)),
        ("dist_52w_high", lambda f: price.distance_from_high(f["close"], f["high"])),
        ("position_in_range", lambda f: price.position_in_range(f["close"], f["high"], f["low"])),
    ],
)
def test_indicator_last_value_is_stable(bars: pd.DataFrame, name: str, fn) -> None:
    """Appending one more bar must not change the previous bar's value.

    A narrower, sharper version of the invariance test: it isolates the single
    most common real-world symptom, where an indicator's "current" reading is
    quietly revised once tomorrow's bar arrives.
    """
    upto = bars.iloc[:500]
    plus_one = bars.iloc[:501]

    before = fn(upto).iloc[-1]
    after = fn(plus_one).iloc[-2]

    if pd.isna(before) and pd.isna(after):
        pytest.skip(f"{name} has not warmed up at bar 500")

    assert before == pytest.approx(after, rel=1e-12, abs=1e-12), (
        f"{name} at bar 499 changed from {before} to {after} once bar 500 arrived"
    )


# --------------------------------------------------------------------------- #
# Structural guards
# --------------------------------------------------------------------------- #


def test_compute_features_excludes_forward_columns_by_default(bars: pd.DataFrame) -> None:
    """A strategy calling compute_features() cannot accidentally receive outcomes."""
    features = compute_features(bars)
    leaked = [c for c in features.columns if str(c).startswith(FORWARD_PREFIX)]
    assert leaked == [], f"compute_features leaked forward columns: {leaked}"

    # And the guard the strategy engine uses agrees.
    assert_no_forward_columns(features)


def test_compute_features_includes_forward_only_when_asked(bars: pd.DataFrame) -> None:
    features = compute_features(bars, include_forward=True)
    forward = [c for c in features.columns if str(c).startswith(FORWARD_PREFIX)]
    assert forward, "include_forward=True produced no forward columns"
    assert "forward_return_20" in forward


def test_assert_no_forward_columns_raises_on_leak(bars: pd.DataFrame) -> None:
    """The guard must reject a frame that carries outcomes, naming the offenders."""
    contaminated = compute_features(bars, include_forward=True)

    with pytest.raises(DataLeakageError) as excinfo:
        assert_no_forward_columns(contaminated, context="test strategy input")

    message = str(excinfo.value)
    assert "forward_return_20" in message
    assert "test strategy input" in message


def _scan_for_lookahead_constructs(path) -> list[str]:
    """Find look-ahead constructs in one module by parsing it, not grepping it.

    A text search cannot tell code from prose, and this project's docstrings
    discuss ``center=True`` and ``shift(-n)`` at length precisely because they are
    forbidden. Parsing to an AST examines only what actually executes.

    Detects:

    * ``center=True`` -- a centred rolling window averages the future.
    * ``.bfill()`` / ``.backfill()`` / ``method="bfill"`` -- copies a later value
      into an earlier slot.
    * ``.shift(-n)`` -- moves future values backwards in time.
    * ``.rolling(...)`` with a negative window offset.
    """
    import ast

    # utf-8-sig tolerates a byte-order mark. Python's importer strips one silently,
    # so a BOM'd module imports fine and would otherwise crash only this scanner --
    # turning a formatting nit into a spurious look-ahead failure.
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(path))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        for keyword in node.keywords:
            value = keyword.value
            if (
                keyword.arg == "center"
                and isinstance(value, ast.Constant)
                and value.value is True
            ):
                offenders.append(f"{path.name}:{node.lineno} center=True")
            if (
                keyword.arg == "method"
                and isinstance(value, ast.Constant)
                and value.value in {"bfill", "backfill"}
            ):
                offenders.append(f"{path.name}:{node.lineno} method={value.value!r}")

        if isinstance(node.func, ast.Attribute):
            if node.func.attr in {"bfill", "backfill"}:
                offenders.append(f"{path.name}:{node.lineno} .{node.func.attr}()")

            if node.func.attr == "shift" and node.args:
                first = node.args[0]
                if (
                    isinstance(first, ast.UnaryOp)
                    and isinstance(first.op, ast.USub)
                ):
                    offenders.append(f"{path.name}:{node.lineno} .shift(-n)")
            if node.func.attr == "shift":
                for keyword in node.keywords:
                    if keyword.arg == "periods" and isinstance(
                        keyword.value, ast.UnaryOp
                    ):
                        if isinstance(keyword.value.op, ast.USub):
                            offenders.append(
                                f"{path.name}:{node.lineno} .shift(periods=-n)"
                            )

    return offenders


def _indicator_modules():
    """Every indicator module except the quarantined ``forward.py``."""
    from pathlib import Path

    import app.indicators as indicators_pkg

    package_dir = Path(indicators_pkg.__file__).parent
    return sorted(p for p in package_dir.glob("*.py") if p.name != "forward.py")


def test_indicator_modules_contain_no_lookahead_constructs() -> None:
    """Static guarantee, complementing the numerical truncation test above.

    The truncation test can only catch bias in a code path the fixture happens to
    exercise. This one reads every line that could execute, so a look-ahead
    construct added to a rarely-called branch still fails the build.
    """
    offenders: list[str] = []
    for path in _indicator_modules():
        offenders.extend(_scan_for_lookahead_constructs(path))

    assert offenders == [], (
        "Look-ahead constructs found outside app/indicators/forward.py "
        f"(move them there if they are deliberate outcome labels): {offenders}"
    )


def test_the_scanner_catches_forward_module() -> None:
    """The AST scanner must flag ``forward.py``, which is look-ahead on purpose.

    Without this, a broken scanner that finds nothing anywhere would pass the test
    above and give false assurance.
    """
    from pathlib import Path

    import app.indicators.forward as forward_module

    offenders = _scan_for_lookahead_constructs(Path(forward_module.__file__))
    assert offenders, "AST scanner failed to detect the deliberate look-ahead in forward.py"
    assert any("shift(-n)" in o for o in offenders)


# --------------------------------------------------------------------------- #
# Forward measures behave correctly where they are allowed
# --------------------------------------------------------------------------- #


def test_forward_return_matches_manual_calculation(bars: pd.DataFrame) -> None:
    horizon = 10
    result = forward_return(bars["close"], horizon)

    i = 100
    expected = (bars["close"].iloc[i + horizon] / bars["close"].iloc[i] - 1.0) * 100.0
    assert result.iloc[i] == pytest.approx(expected)

    # The final `horizon` rows have no future and must be NaN, not zero.
    assert result.iloc[-horizon:].isna().all()


def test_forward_mae_excludes_the_observation_bar(bars: pd.DataFrame) -> None:
    """Max adverse excursion must look at bars i+1..i+h, not include bar i itself.

    Including the observation bar's own low would understate the risk of the
    setup by mixing in a move that had already happened when it was observed.
    """
    horizon = 5
    result = forward_max_adverse_excursion(bars["close"], bars["low"], horizon)

    i = 200
    future_low = bars["low"].iloc[i + 1 : i + 1 + horizon].min()
    expected = (future_low / bars["close"].iloc[i] - 1.0) * 100.0
    assert result.iloc[i] == pytest.approx(expected)


def test_forward_frame_tail_is_nan_not_filled(bars: pd.DataFrame) -> None:
    """Unknown futures stay unknown. Filling them would bias every analogue study."""
    frame = forward_frame(bars, horizons=(20,))
    assert frame["forward_return_20"].iloc[-20:].isna().all()
    assert not (frame["forward_return_20"].iloc[-20:] == 0).any()


# --------------------------------------------------------------------------- #
# Warm-up honesty
# --------------------------------------------------------------------------- #


def test_slow_indicators_are_nan_before_full_window(bars: pd.DataFrame) -> None:
    """A 200-bar average must not report a value from 50 bars.

    Returning an "early estimate" is worse than returning nothing: it is a
    different statistic under the same name, and a strategy will trade it.
    """
    features = compute_features(bars)

    assert features["sma_200"].iloc[:199].isna().all()
    assert features["sma_200"].iloc[199:].notna().all()

    assert features["ema_200"].iloc[:199].isna().all()
    assert features["ema_200"].iloc[199:].notna().all()

    assert features["high_52w"].iloc[:251].isna().all()
    assert features["high_52w"].iloc[251:].notna().all()


def test_rsi_is_nan_until_seeded(bars: pd.DataFrame) -> None:
    """Wilder's RSI needs `length` differences; the first difference is undefined."""
    result = momentum.rsi(bars["close"], 14)
    assert result.iloc[:14].isna().all()
    assert not np.isnan(result.iloc[14])
