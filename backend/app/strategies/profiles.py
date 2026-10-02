"""Named risk settings for :mod:`app.strategies.trend_momentum`.

A dial, not a menu of recommendations
-------------------------------------
Each profile is the same strategy with different numbers. They were chosen by reasoning
about *which direction each parameter moves risk*, and deliberately **not** by trying
combinations and keeping whichever scored best -- that procedure produces a setting fitted
to the sample it was chosen on, and the resulting number is no longer an estimate of
anything.

The three levers that widen both tails:

**Turning off the trend-break exit.** In the TRAIN sample this exit ended 40% of trades at a
median of -1.0%. It is the cheap exit: it closes a position the moment it wobbles, which also
closes the ones that were about to work. Switch it off and trades run to the stop or the
target instead, so losses get larger and so do wins.

**A wider stop and a further target.** Three ATR instead of two means fewer stop-outs and a
bigger one when it happens. A 5R target means a rarer, larger win.

**Looser entries, and only volatile instruments.** More trades, taken on weaker evidence, in
names that move more. More exposure in both directions.

What is not a lever here: position size. Risking 3% of capital per trade instead of 1% widens
both tails by arithmetic, tells you nothing about the rules, and is a decision about your own
money rather than about the strategy. It stays fixed across all profiles so a comparison
between them is a comparison of rules.

The numbers are measured, not asserted
--------------------------------------
Nothing in this file claims a profile is better. :mod:`app.reporting.evidence` reads what each
one actually did over TRAIN and VALIDATION from an artifact produced by
``app profiles --measure``, and that artifact carries a fingerprint of the parameters it was
measured with. If a parameter here changes and the measurement is not re-run, the fingerprint stops matching and
the evidence is withheld rather than shown against the wrong configuration.

TEST is never opened by any of this.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

__all__ = [
    "RiskProfile",
    "RISK_PROFILES",
    "DEFAULT_PROFILE",
    "get_profile",
    "available_profiles",
    "profile_fingerprint",
]


@dataclass(frozen=True, slots=True)
class RiskProfile:
    """One named set of parameter overrides, with what it is for."""

    name: str
    title: str
    summary: str
    """One sentence a reader sees next to the name. States the trade-off, not a verdict."""

    overrides: dict[str, Any]

    def params_for(self, defaults: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(self.overrides) - set(defaults))
        if unknown:
            raise ValueError(
                f"Profile {self.name!r} overrides parameters that do not exist: {unknown}. "
                "A typo here would silently run the default configuration under this name."
            )
        return {**defaults, **self.overrides}


RISK_PROFILES: tuple[RiskProfile, ...] = (
    RiskProfile(
        name="steady",
        title="Steady",
        summary=(
            "Closes a position as soon as the trend wobbles. The most common ending, and "
            "usually a small loss. Smallest drawdowns of the three; also the smallest wins."
        ),
        overrides={},
    ),
    RiskProfile(
        name="bold",
        title="Bold",
        summary=(
            "No trend-break exit, so a trade runs to its stop or its target. A wider stop "
            "and a further target: fewer, larger outcomes in both directions."
        ),
        overrides={
            "exit_on_trend_break": False,
            "stop_atr_multiple": 2.5,
            "take_profit_r_multiple": 4.0,
            "max_holding_bars": 90,
        },
    ),
    RiskProfile(
        name="aggressive",
        title="Aggressive",
        summary=(
            "Every lever at once: no trend break, a three-ATR stop, a 5R target, weaker "
            "entry conditions and only instruments that move. The widest tails on both sides."
        ),
        overrides={
            "exit_on_trend_break": False,
            "stop_atr_multiple": 3.0,
            "take_profit_r_multiple": 5.0,
            "max_holding_bars": 120,
            "require_macd_positive": False,
            "min_relative_volume": 1.0,
            "rsi_min": 35.0,
            "rsi_max": 80.0,
            "min_atr_pct": 1.5,
            "max_atr_pct": 12.0,
        },
    ),
)

DEFAULT_PROFILE = "steady"

_BY_NAME = {profile.name: profile for profile in RISK_PROFILES}


def get_profile(name: str | None) -> RiskProfile:
    key = (name or DEFAULT_PROFILE).strip().lower()
    if key not in _BY_NAME:
        raise KeyError(
            f"Unknown risk profile {name!r}. Available: {sorted(_BY_NAME)}"
        )
    return _BY_NAME[key]


def available_profiles() -> list[str]:
    return [profile.name for profile in RISK_PROFILES]


def profile_fingerprint(resolved: dict[str, dict[str, Any]]) -> str:
    """A short digest of every profile's full parameter set.

    Stored alongside a measurement so the numbers cannot outlive the parameters they were
    measured with. Changing any threshold changes this, and the evidence is then withheld
    instead of being shown against a configuration that no longer exists.
    """
    blob = json.dumps(resolved, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]
