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
            "liquidity_threshold", "strategy", "profiles", "evidence", "fx", "regions",
            "instruments", "skipped",
        }

    def test_an_instrument_carries_only_market_facts(self, built) -> None:
        payload = json.loads((built / "market.json").read_text(encoding="utf-8"))
        instrument = payload["instruments"]["GOOGL"]
        assert set(instrument) == {
            "by_profile", "ema200", "rsi", "macd_hist", "rel_volume", "atr_pct",
            "from_high", "name", "region", "sector", "etf", "turnover", "thin",
            "liquidity_caveat", "notes", "d", "h", "l", "c", "e", "a",
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
const _store = {{"quant-trader.positions.v1": "[]"}};
global.localStorage = {{
  getItem: (k) => (k in _store ? _store[k] : null),
  setItem: (k, v) => {{ _store[k] = String(v); }},
  removeItem: (k) => {{ delete _store[k]; }},
}};
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
        # Written to a file rather than passed with `node -e`: the page's JavaScript is past
        # 35 KB and Windows caps a command line at about 32 KB, so -e started failing with
        # "el nombre del archivo o la extension es demasiado largo" as the app grew.
        script_file = built.parent / "_harness.js"
        script_file.write_text(harness, encoding="utf-8")
        result = subprocess.run(
            [NODE, str(script_file)], capture_output=True, text=True, timeout=60
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


class TestRiskProfiles:
    """The dial, and the measurement that has to stay attached to it."""

    def test_every_instrument_is_evaluated_under_every_profile(self, built) -> None:
        """A missing profile would render an empty detail page, silently."""
        from app.strategies.profiles import available_profiles

        payload = json.loads((built / "market.json").read_text(encoding="utf-8"))
        names = set(available_profiles())
        assert {p["name"] for p in payload["profiles"]} == names
        for symbol, inst in payload["instruments"].items():
            assert set(inst["by_profile"]) == names, f"{symbol} is missing a profile"

    def test_a_riskier_profile_places_a_wider_stop(self, built) -> None:
        """Not a preference: it is what "riskier" means here, and it is checkable."""
        payload = json.loads((built / "market.json").read_text(encoding="utf-8"))
        inst = payload["instruments"]["GOOGL"]
        steady = inst["by_profile"]["steady"]["levels"]
        aggressive = inst["by_profile"]["aggressive"]["levels"]
        assert steady and aggressive
        assert aggressive["stop"] < steady["stop"]
        assert aggressive["target"] > steady["target"]

    def test_the_measurement_is_withheld_when_the_parameters_change(
        self, tmp_path
    ) -> None:
        """The guard that stops real numbers describing a configuration that never ran.

        Without this, editing a threshold leaves the page showing yesterday's measurement
        beside today's rules, and every number on it is both true and wrong.
        """
        from app.reporting.evidence import load_evidence

        stale = tmp_path / "evidence.json"
        stale.write_text(
            json.dumps({"fingerprint": "not-the-current-one", "by_profile": {}}),
            encoding="utf-8",
        )
        evidence = load_evidence(path=stale)
        assert not evidence.available
        assert "different parameters" in evidence.reason

    def test_a_missing_measurement_says_so_rather_than_guessing(self, tmp_path) -> None:
        from app.reporting.evidence import load_evidence

        evidence = load_evidence(path=tmp_path / "absent.json")
        assert not evidence.available
        assert "No measurement" in evidence.reason

    def test_the_published_payload_carries_no_hand_typed_base_rates(self, built) -> None:
        """Every frequency on the page has to come from the measurement.

        The four that used to be hardcoded were from an older universe, and one of them had
        drifted: the page claimed a median winner of 36 sessions where the measurement says
        26. A number nobody re-derives is a number that quietly stops being true.
        """
        payload = json.loads((built / "market.json").read_text(encoding="utf-8"))
        assert "exit_base_rates" not in payload
        html = (built / "index.html").read_text(encoding="utf-8")
        assert "21% of the time" not in html
        assert "median winner took 36" not in html


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestTheBrowserHonoursTheProfile:
    """Switching the dial has to change the verdict, not just the words next to it."""

    _run = TestBrowserArithmeticMatchesPython._run

    def test_turning_off_the_trend_break_keeps_a_wobbling_position_open(
        self, built
    ) -> None:
        """The difference that makes the riskier settings riskier.

        Under "steady" a close below the 50-day average is an exit. Under "aggressive" it is
        not, so the same position stays open and runs to its stop or its target instead. If
        the page ever applied one profile's exits to another's levels, this is what fails.
        """
        got = self._run(
            built,
            """
const inst = MKT.instruments.GOOGL;
const n = inst.c.length - 1;
// Find a bar whose close sits under the 50-day average, and open a position before it with
// levels far enough away that only the trend rule can speak.
let at = -1;
for (let i = n; i > 20; i--) {
  if (inst.e[i] != null && inst.c[i] < inst.e[i]) { at = i; break; }
}
const start = at - 5;
const pos = {symbol: "GOOGL", price: inst.c[start], shares: 1, date: inst.d[start],
             amount: 100, currency: "USD",
             stop: inst.c[start] * 0.2, target: inst.c[start] * 5};
const out = {};
["steady", "aggressive"].forEach(function (name) {
  localStorage.setItem("quant-trader.profile.v1", name);
  const ev = evaluate(pos);
  out[name] = {verdict: ev.verdict, rule: ev.fired ? ev.fired.rule : null,
               breaks: P().exit_on_trend_break};
});
out.found = at;
console.log(JSON.stringify(out));
""",
        )
        assert got["found"] > 0, "no bar closed under its 50-day average in the sample"
        assert got["steady"]["breaks"] is True
        assert got["aggressive"]["breaks"] is False
        assert got["steady"]["verdict"] == "SELL"
        assert got["steady"]["rule"] == "trend"
        assert got["aggressive"]["verdict"] == "HOLD", (
            "the aggressive setting has no trend-break exit, so this must stay open"
        )
