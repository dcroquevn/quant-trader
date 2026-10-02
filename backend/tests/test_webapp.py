"""The published payload, and the browser arithmetic that reads it.

Two things are being protected here.

**The privacy split.** The repository is public, so `market.json` is world-readable. It must
carry prices and nothing about anybody. A field that identifies a person would silently undo the
whole reason the app computes verdicts in the browser, and nothing else would notice.

**The duplicated exit rules.** Your position is private, so the verdict has to be computed on the
device that holds it -- which means the rules exist twice, in Python and in JavaScript. Two
implementations of one rule drift. These tests run the page's own JavaScript, extracted from the
published HTML, against the Python engine on the same inputs. If someone edits a number in either
one, this is what fails.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")


@pytest.fixture
def built(session, tmp_path):
    """The real exporter, run against a database with synthetic but complete history."""
    import numpy as np
    import pandas as pd

    from app.data.engine import DataEngine
    from app.database.models import Asset, Bar
    from app.reporting.webapp import write_app

    DataEngine(session).sync_universe()

    index = pd.bdate_range(end=pd.Timestamp("2026-10-01"), periods=400)
    t = np.arange(len(index))
    closes = 100.0 * (1.0 + 0.0013) ** t * (1.0 + 0.035 * np.sin(t / 22.0 * 2 * np.pi + 1.8))

    for symbol in ("GOOGL", "SPY", "ECH", "AAXJ"):
        asset = session.query(Asset).filter_by(symbol=symbol, market_code="USA").one()
        for stamp, close in zip(index, closes):
            session.add(
                Bar(
                    asset_id=asset.id,
                    timeframe="1D",
                    ts=stamp.to_pydatetime(),
                    open=float(close),
                    high=float(close) * 1.010,
                    low=float(close) * 0.990,
                    close=float(close),
                    adj_close=float(close),
                    volume=9_000_000.0,
                )
            )
    session.flush()

    out = tmp_path / "site"
    write_app(session, out)
    return out


class TestPayloadCarriesNothingPersonal:
    """`market.json` is served from a public repository."""

    def test_recording_a_position_cannot_change_the_published_file(
        self, built, session
    ) -> None:
        """The strongest statement of the privacy rule, and the easiest to check.

        If the output is byte-identical before and after a position exists, then nothing about
        the position can be in it. That is stronger than hunting for leaked strings -- and
        searching for them does not work: "0.0579" and "351.44" both occur naturally in a file
        full of prices, and "holding" appears inside `max_holding_bars`. A substring test here
        would fail on coincidence and pass on a leak with a different name.
        """
        from datetime import date

        from app.portfolio.holdings import open_holding
        from app.reporting.webapp import build_market_payload

        before = json.dumps(build_market_payload(session), sort_keys=True)
        open_holding(session, "GOOGL", 0.0579, 351.44, opened_on=date(2026, 10, 1),
                     broker="fintual", note="a private note")
        after = json.dumps(build_market_payload(session), sort_keys=True)

        assert before == after, "the published payload changed when a position was recorded"

    def test_the_top_level_keys_are_an_allowlist(self, built) -> None:
        """A new key is a decision, not an accident: this fails until someone reviews it."""
        payload = json.loads((built / "market.json").read_text(encoding="utf-8"))
        assert set(payload) == {
            "version", "generated_at", "as_of", "verification_date", "turnover_window",
            "liquidity_threshold", "strategy", "exit_base_rates", "fx", "regions",
            "instruments", "skipped",
        }

    def test_an_instrument_carries_only_market_facts(self, built) -> None:
        payload = json.loads((built / "market.json").read_text(encoding="utf-8"))
        instrument = payload["instruments"]["GOOGL"]
        assert set(instrument) == {
            "name", "region", "sector", "etf", "turnover", "thin", "liquidity_caveat",
            "notes", "signal", "score", "reasons", "d", "h", "l", "c", "e", "a",
        }


class TestPayloadShape:
    def test_the_rule_parameters_travel_with_the_data(self, built) -> None:
        """Hardcoding them in the JavaScript is how the two implementations would drift."""
        payload = json.loads((built / "market.json").read_text(encoding="utf-8"))
        strategy = payload["strategy"]
        assert strategy["stop_atr_multiple"] == 2.0
        assert strategy["take_profit_r_multiple"] == 3.0
        assert strategy["max_holding_bars"] == 60

    def test_the_series_are_aligned(self, built) -> None:
        payload = json.loads((built / "market.json").read_text(encoding="utf-8"))
        for symbol, inst in payload["instruments"].items():
            n = len(inst["d"])
            for key in ("h", "l", "c", "e", "a"):
                assert len(inst[key]) == n, f"{symbol}.{key} is not aligned with its dates"

    def test_the_page_fetches_only_its_own_data_file(self, built) -> None:
        """Published from a public repo; an external fetch would be a third party reading it."""
        html = (built / "index.html").read_text(encoding="utf-8")
        fetches = re.findall(r"fetch\(([^)]*)\)", html)
        assert fetches, "the page must load its data"
        for call in fetches:
            assert "market.json" in call
        assert "http://" not in html and "https://" not in html


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestBrowserArithmeticMatchesPython:
    """The page's own JavaScript, run against the Python engine's answers."""

    def _run(self, built: Path, script: str) -> dict:
        html = (built / "index.html").read_text(encoding="utf-8")
        start = html.rindex("<script>") + len("<script>")
        code = html[start : html.rindex("</script>")]

        # The page's boot code touches document/fetch/localStorage; stub just enough for the
        # pure functions to be defined, then call them.
        harness = f"""
const noop = () => {{}};
const el = {{
  addEventListener: noop, appendChild: noop, classList: {{add: noop, remove: noop, toggle: noop}},
  querySelector: () => el, querySelectorAll: () => [], style: {{}}, dataset: {{}},
  setAttribute: noop, getAttribute: () => "", value: "", textContent: "", innerHTML: "",
}};
global.document = {{
  getElementById: () => el, querySelector: () => el, querySelectorAll: () => [],
  createElement: () => el, addEventListener: noop,
}};
global.window = {{scrollTo: noop}};
global.localStorage = {{getItem: () => "[]", setItem: noop}};
global.fetch = () => new Promise(() => {{}});
global.alert = noop;
global.confirm = () => true;
global.Blob = function () {{}};
global.URL = {{createObjectURL: () => ""}};
global.FileReader = function () {{}};

{code}

MKT = JSON.parse(require("fs").readFileSync({json.dumps(str(built / "market.json"))}, "utf8"));
{script}
"""
        result = subprocess.run(
            [NODE, "-e", harness], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr[-2000:]
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_the_levels_match_the_strategy(self, built, session) -> None:
        """`levelsFor` must produce the same stop and target as `propose_levels`."""
        from app.backtesting.runner import load_features
        from app.strategies.registry import build_strategy

        frames, _ = load_features(session, ["GOOGL"], "USA")
        expected = build_strategy("trend_momentum").propose_levels(frames["GOOGL"])
        assert expected is not None

        got = self._run(
            built,
            'const lv = levelsFor(MKT.instruments.GOOGL, MKT.as_of);'
            'console.log(JSON.stringify({stop: lv.stop, target: lv.target}));',
        )
        # The payload rounds prices to four decimals to keep it loadable on a phone, so the
        # browser computes from slightly coarser inputs than Python did. A tenth of a cent is
        # far inside the two decimals a price is quoted to; what this guards against is the two
        # implementations disagreeing about the *rule*, not about floating point.
        assert got["stop"] == pytest.approx(expected[0], abs=1e-3)
        assert got["target"] == pytest.approx(expected[1], abs=1e-3)

    def test_a_position_under_its_average_reads_SELL(self, built) -> None:
        """The verdict the whole page exists to produce."""
        got = self._run(
            built,
            """
const inst = MKT.instruments.GOOGL;
const n = inst.c.length - 1;
// A position opened on the last bar, with a stop far below so only the trend rule can speak.
const pos = {symbol: "GOOGL", price: inst.c[n], shares: 1, date: inst.d[n],
             amount: inst.c[n], currency: "USD",
             stop: inst.c[n] * 0.5, target: inst.c[n] * 2};
const ev = evaluate(pos);
console.log(JSON.stringify({
  verdict: ev.verdict, under: inst.e[n] != null && inst.c[n] < inst.e[n], why: ev.why
}));
""",
        )
        if got["under"]:
            assert got["verdict"] == "SELL"
            assert "50-day" in got["why"]
        else:
            assert got["verdict"] == "HOLD"

    def test_a_stop_breach_is_found_on_the_bar_it_happened(self, built) -> None:
        got = self._run(
            built,
            """
const inst = MKT.instruments.GOOGL;
const start = inst.d.length - 40;
// A stop just above the lowest low after the entry: it must fire, and on that bar.
let lowest = Infinity, lowestAt = null;
for (let i = start + 1; i < inst.l.length; i++) {
  if (inst.l[i] < lowest) { lowest = inst.l[i]; lowestAt = inst.d[i]; }
}
const pos = {symbol: "GOOGL", price: inst.c[start], shares: 1, date: inst.d[start],
             amount: 100, currency: "USD", stop: lowest * 1.001, target: 1e9};
const ev = evaluate(pos);
console.log(JSON.stringify({verdict: ev.verdict, rule: ev.fired && ev.fired.rule,
                            at: ev.fired && ev.fired.at, expectedAt: lowestAt}));
""",
        )
        assert got["verdict"] == "SELL"
        assert got["rule"] == "stop"
        # The first breach, not the lowest: an earlier bar may already have crossed it.
        assert got["at"] <= got["expectedAt"]

    def test_the_entry_bar_cannot_trigger_its_own_exit(self, built) -> None:
        """The entry price already reflects that session; marking it would exit on day zero."""
        got = self._run(
            built,
            """
const inst = MKT.instruments.GOOGL;
const n = inst.c.length - 1;
const pos = {symbol: "GOOGL", price: inst.c[n], shares: 1, date: inst.d[n],
             amount: 100, currency: "USD",
             stop: inst.h[n] + 1, target: inst.l[n] - 1};
const ev = evaluate(pos);
console.log(JSON.stringify({fired: ev.fired ? ev.fired.rule : null}));
""",
        )
        assert got["fired"] is None, "the entry bar triggered its own exit"
