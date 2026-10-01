"""The GitHub Actions job summary, which is the only surface a private repo can show on a phone.

Worth testing rather than eyeballing, because it runs unattended: a crash here happens *after* the
Telegram alerts have gone out, so the run looks half-broken for a reason unrelated to the alerts.
Two defects found this way already — nullable prices formatted with ``:.2f``, and the watch
skipping the summary entirely when no positions were recorded.

The signal branch is exercised with a stub rather than by arranging real bars that produce a BUY.
What is being tested is the rendering, and making the market cooperate would test the strategy
instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.__main__ import _write_market_summary, _write_step_summary
from app.portfolio.watch import WatchOutcome, WatchReport


@dataclass
class _Spec:
    symbol: str
    is_thinly_traded: bool = False


@dataclass
class _Row:
    symbol: str
    action: str
    score: float
    price: float | None
    return_20d: float | None
    market: str = "USA"
    tradable: bool = True
    blocked_reason: str = ""


@dataclass
class _Benchmark:
    symbol: str


def _digest_data(rows: list[_Row], *, declared: int | None = None) -> dict:
    """``declared`` larger than ``len(rows)`` is the partial-download state."""
    return {
        "generated_at": datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc),
        "stalest": ("AAPL", datetime(2026, 9, 29).date()),
        "regions": [
            {
                "region": "Emerging Asia",
                "specs": [object()] * (declared if declared is not None else len(rows)),
                "rows": rows,
                "n_buy": sum(1 for r in rows if r.action == "BUY"),
                "n_sell": sum(1 for r in rows if r.action == "SELL"),
                "benchmark": _Benchmark("AAXJ"),
            }
        ],
    }


@pytest.fixture
def summary_file(tmp_path, monkeypatch):
    target = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    return target


class TestMarketSummary:
    def test_nothing_is_written_outside_actions(self, tmp_path, monkeypatch):
        """The flag has to be harmless locally, or nobody will leave it on."""
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        # No target set, so this must be a no-op rather than an error.
        _write_market_summary(object(), "trend_momentum")

    def test_signals_are_rendered_with_their_caveats(self, summary_file, monkeypatch):
        rows = [
            _Row("TSM", "BUY", 0.86, 456.94, 12.3),
            _Row("THD", "BUY", 0.71, 72.33, 1.1),
            _Row("EWY", "SELL", 0.43, 187.10, -4.2),
        ]
        monkeypatch.setattr(
            "app.reporting.digest.collect_digest_data",
            lambda *a, **k: _digest_data(rows),
        )
        monkeypatch.setattr(
            "app.__main__.find_asset",
            lambda symbol, market=None: _Spec(symbol, is_thinly_traded=symbol == "THD"),
        )
        _write_market_summary(object(), "trend_momentum")
        text = summary_file.read_text(encoding="utf-8")

        assert "3 signal(s) today" in text
        assert "**TSM**" in text and "456.94" in text
        assert "thinly traded" in text, "THD's illiquidity must reach the summary"
        # Matched around the markdown emphasis: the summary writes "**not** a probability".
        assert "a probability of" in text, "the score disclaimer is not optional"
        assert "not a forecast" in text

    def test_a_nullable_price_does_not_crash_the_run(self, summary_file, monkeypatch):
        """This was a real defect. A missing close would raise while formatting, after the
        Telegram alerts had already been sent."""
        rows = [_Row("VNM", "BUY", 0.71, None, None)]
        monkeypatch.setattr(
            "app.reporting.digest.collect_digest_data",
            lambda *a, **k: _digest_data(rows),
        )
        monkeypatch.setattr(
            "app.__main__.find_asset", lambda symbol, market=None: _Spec(symbol)
        )
        _write_market_summary(object(), "trend_momentum")
        assert "n/a" in summary_file.read_text(encoding="utf-8")

    def test_a_quiet_day_says_so_without_sounding_broken(self, summary_file, monkeypatch):
        """Most days nothing fires, and the reader has to know that is normal."""
        monkeypatch.setattr(
            "app.reporting.digest.collect_digest_data", lambda *a, **k: _digest_data([])
        )
        _write_market_summary(object(), "trend_momentum")
        text = summary_file.read_text(encoding="utf-8")
        assert "No entry or exit conditions hold" in text
        assert "normal" in text
        assert "915 entries" in text, "the claim must cite the measured figure"

    def test_a_partial_download_is_not_reported_as_a_full_scan(
        self, summary_file, monkeypatch
    ):
        """Found by running the workflow against an empty database.

        With 4 of 42 instruments downloaded the summary printed "21" for the region, which reads
        as "21 checked, nothing fired" when the truth was "2 checked, 19 unknown". The reassuring
        reading was the false one.
        """
        rows = [_Row("TSM", "HOLD", 0.5, 456.94, 1.0), _Row("EWY", "HOLD", 0.5, 187.1, 1.0)]
        monkeypatch.setattr(
            "app.reporting.digest.collect_digest_data",
            lambda *a, **k: _digest_data(rows, declared=21),
        )
        monkeypatch.setattr(
            "app.__main__.find_asset", lambda symbol, market=None: _Spec(symbol)
        )
        _write_market_summary(object(), "trend_momentum")
        text = summary_file.read_text(encoding="utf-8")

        assert "2 of 21" in text
        assert "19 instrument(s) could not be evaluated" in text
        assert "would not have been seen" in text, (
            "the consequence has to be stated, not just the count"
        )

    def test_a_complete_scan_does_not_raise_a_false_alarm(self, summary_file, monkeypatch):
        """A warning on every run is a warning on none."""
        rows = [_Row("TSM", "HOLD", 0.5, 456.94, 1.0)]
        monkeypatch.setattr(
            "app.reporting.digest.collect_digest_data", lambda *a, **k: _digest_data(rows)
        )
        monkeypatch.setattr(
            "app.__main__.find_asset", lambda symbol, market=None: _Spec(symbol)
        )
        _write_market_summary(object(), "trend_momentum")
        text = summary_file.read_text(encoding="utf-8")
        assert "could not be evaluated" not in text
        assert "| 1 |" in text

    def test_stale_data_is_flagged(self, summary_file, monkeypatch):
        data = _digest_data([])
        data["stalest"] = ("AAPL", datetime(2026, 9, 1).date())
        monkeypatch.setattr("app.reporting.digest.collect_digest_data", lambda *a, **k: data)
        _write_market_summary(object(), "trend_momentum")
        text = summary_file.read_text(encoding="utf-8")
        assert ":warning:" in text
        assert "29 days back" in text

    def test_the_currency_caveat_is_always_present(self, summary_file, monkeypatch):
        monkeypatch.setattr(
            "app.reporting.digest.collect_digest_data", lambda *a, **k: _digest_data([])
        )
        _write_market_summary(object(), "trend_momentum")
        assert "USD" in summary_file.read_text(encoding="utf-8")


class TestWatchSummary:
    def test_an_empty_watch_still_reports_that_it_ran(self, summary_file):
        """"The watch ran and you hold nothing" and "the watch never ran" need distinguishing.

        Only one of them requires action, and an empty summary looks like the second.
        """
        _write_step_summary(
            WatchReport(ran_at=datetime(2026, 9, 30, tzinfo=timezone.utc), channel="console")
        )
        text = summary_file.read_text(encoding="utf-8")
        assert "The watch ran" in text
        assert "No positions are recorded" in text
        assert "app buy" in text

    def test_a_trigger_reports_when_it_fired(self, summary_file):
        outcome = WatchOutcome(
            holding_id=1,
            symbol="ECH",
            region="Chile",
            quantity=10,
            entry_price=40.81,
            opened_on=datetime(2026, 8, 5).date(),
            exit_triggered=True,
            exit_reason="strategy_signal",
            exit_triggered_on=datetime(2026, 9, 10).date(),
            sessions_since_trigger=13,
            unrealised_pnl_pct=-7.03,
            last_price=37.94,
        )
        _write_step_summary(
            WatchReport(
                ran_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
                outcomes=[outcome],
                channel="telegram",
            )
        )
        text = summary_file.read_text(encoding="utf-8")
        assert "**ECH**" in text
        assert "2026-09-10" in text
        assert "13" in text, "sessions since the trigger must be visible"
        assert "does not mean" in text, "the interpretation caveat is not optional"

    def test_an_unwatchable_position_is_escalated(self, summary_file):
        """A position the user believes is watched, that is not, is the worst failure here."""
        outcome = WatchOutcome(
            holding_id=2,
            symbol="SQM",
            region="Chile",
            quantity=50,
            entry_price=47.30,
            opened_on=datetime(2026, 6, 1).date(),
            usable=False,
            problem="the newest stored bar is 2026-09-01, 29 days old.",
        )
        _write_step_summary(
            WatchReport(
                ran_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
                outcomes=[outcome],
                channel="console",
            )
        )
        text = summary_file.read_text(encoding="utf-8")
        assert "Could not be checked" in text
        assert "not** being watched" in text
        assert "29 days old" in text

    def test_nothing_is_written_outside_actions(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        _write_step_summary(
            WatchReport(ran_at=datetime(2026, 9, 30, tzinfo=timezone.utc), channel="console")
        )
