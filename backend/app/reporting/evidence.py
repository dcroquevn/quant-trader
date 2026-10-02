"""What each risk profile actually did, measured and stored rather than asserted.

Why this is a file on disk and not a table in a docstring
---------------------------------------------------------
A number typed into source code describes whatever configuration existed the day it was typed.
Change a threshold, a cost assumption or the universe, and the comment keeps claiming the old
result with total confidence. That is the failure mode this project cannot afford: the whole
point of the profiles is that someone picks one *because of* these numbers.

So the numbers come from a run, land in ``data/profile_evidence.json``, and carry:

* a **fingerprint of every profile's full parameter set** -- if a parameter changes and nobody
  re-measures, :func:`load_evidence` notices the mismatch and withholds the numbers instead of
  showing them against a configuration that no longer exists;
* the **split bounds and the universe size** the measurement used, so a reader can see what the
  sample was;
* the date it was taken.

A withheld measurement is published as "not measured for the current settings". That is a worse
page and a true one.

TEST is never read here. The profiles are compared on TRAIN and VALIDATION only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT, get_settings
from app.core.logging import get_logger
from app.strategies.profiles import RISK_PROFILES, profile_fingerprint
from app.strategies.registry import build_strategy

logger = get_logger(__name__)

__all__ = [
    "EVIDENCE_PATH",
    "EVIDENCE_SPLITS",
    "ProfileEvidence",
    "resolved_profile_params",
    "measure_profiles",
    "load_evidence",
]

EVIDENCE_PATH = PROJECT_ROOT / "data" / "profile_evidence.json"

EVIDENCE_SPLITS = ("train", "validation")
"""Both out of the two that may be read. TEST stays shut."""

REPORTED_METRICS = (
    "n_trades",
    "cagr_pct",
    "max_drawdown_pct",
    "sharpe",
    "win_rate_pct",
    "profit_factor",
    "average_win",
    "average_loss",
    "exposure_pct",
)


@dataclass(frozen=True, slots=True)
class ProfileEvidence:
    """A loaded measurement, or the reason there is not one."""

    available: bool
    reason: str = ""
    measured_on: str = ""
    universe_size: int = 0
    windows: dict[str, dict[str, str]] = field(default_factory=dict)
    by_profile: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "measured_on": self.measured_on,
            "universe_size": self.universe_size,
            "windows": self.windows,
            "by_profile": self.by_profile,
        }


def resolved_profile_params(strategy_name: str = "trend_momentum") -> dict[str, dict[str, Any]]:
    """Every profile's complete parameter set, defaults included.

    Resolved rather than stored as overrides, because the fingerprint has to change when a
    *default* moves too -- a profile that overrides nothing still behaves differently.
    """
    defaults = build_strategy(strategy_name).params.to_dict()
    return {profile.name: profile.params_for(defaults) for profile in RISK_PROFILES}


def measure_profiles(
    session,
    *,
    market: str = "USA",
    strategy_name: str = "trend_momentum",
    path: Path | None = None,
) -> dict[str, Any]:
    """Run every profile over TRAIN and VALIDATION and write the artifact.

    Costs, sizing, universe and splits are identical across profiles, so the only thing that
    differs between two rows is the rules. Position sizing in particular is held fixed: raising
    it would widen every profile's tails by arithmetic and make the comparison meaningless.
    """
    # Imported here: this module is loaded by the exporter, which should not pull the whole
    # backtesting stack in just to read a JSON file.
    from app.backtesting.runner import prepare_frames, resolve_window, run_backtest

    resolved = resolved_profile_params(strategy_name)
    settings = get_settings()

    windows: dict[str, dict[str, str]] = {}
    by_profile: dict[str, dict[str, dict[str, Any]]] = {
        name: {} for name in resolved
    }
    universe_size = 0

    for split in EVIDENCE_SPLITS:
        window = resolve_window(split)
        windows[split] = {"start": str(window.start), "end": str(window.end)}

        frames, skipped = prepare_frames(session, market, split=split)
        universe_size = max(universe_size, len(frames))
        if skipped:
            logger.info("%s: skipped %d symbol(s) for short history", split, len(skipped))

        for name, params in resolved.items():
            strategy = build_strategy(strategy_name, params)
            result = run_backtest(
                session,
                strategy,
                market,
                split=split,
                frames=frames,
                include_benchmark=False,
                label=f"profile:{name}",
            )
            metrics = result.metrics
            row = {key: metrics.get(key) for key in REPORTED_METRICS}
            # The one statistic the headline numbers hide: a sample whose profit is one trade
            # is not evidence of a strategy, and the reader should be able to see that.
            row["top_5pct_share_of_pnl"] = _tail_share(result.trades)
            row["exits"] = _exit_stats(result.trades)
            # Medians, not averages: holding periods are heavily skewed, and one trade held
            # for a year drags an average far from anything that ever happened.
            row["holding"] = _holding_medians(result.trades)
            by_profile[name][split] = row
            logger.info(
                "%s / %s: %d trades, CAGR %s, max drawdown %s",
                split, name, row["n_trades"], row["cagr_pct"], row["max_drawdown_pct"],
            )

    payload = {
        "measured_on": datetime.now(timezone.utc).date().isoformat(),
        "market": market,
        "strategy": strategy_name,
        "universe_size": universe_size,
        "fingerprint": profile_fingerprint(resolved),
        "params": resolved,
        "windows": windows,
        "splits_read": list(EVIDENCE_SPLITS),
        "sizing": {
            "risk_per_trade_pct": settings.risk_per_trade_pct,
            "max_position_size_pct": settings.max_position_size_pct,
            "max_simultaneous_positions": settings.max_simultaneous_positions,
            "note": "Identical for every profile, so a difference between rows is a "
                    "difference in rules and not in bet size.",
        },
        "by_profile": by_profile,
    }

    destination = path or EVIDENCE_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    logger.info("Wrote %s", destination)
    return payload


def _tail_share(trades) -> float | None:
    """Percentage of net profit produced by the best 5% of trades."""
    pnls = sorted((float(t.pnl) for t in trades), reverse=True)
    if len(pnls) < 20:
        return None
    total = sum(pnls)
    if total == 0:
        return None
    k = max(1, len(pnls) // 20)
    return round(100.0 * sum(pnls[:k]) / total, 1)


def _exit_rule(reason: str) -> str:
    """Group a trade's exit reason under the rule that produced it.

    The engine records a trend-break exit as the sentence that fired ("price 24.40 closed below
    EMA50 25.99"), so every one of them is a distinct string. Without grouping, the mix is a
    list of several hundred reasons each accounting for 0.2%.
    """
    text = str(reason or "unknown")
    if "closed below EMA50" in text:
        return "trend_break"
    if text.startswith("RSI "):
        return "rsi_extended"
    return text


def _exit_stats(trades) -> dict[str, dict[str, Any]]:
    """How trades ended: the share of each rule and what it typically cost or paid.

    Both halves matter and neither is enough alone. The most common ending is usually a small
    loss; the rarest is the one that pays for everything. A page that showed only the
    frequencies would make the strategy look like it loses four times out of five, and one
    that showed only the medians would hide how seldom the good ending happens.
    """
    if not trades:
        return {}

    grouped: dict[str, list[float]] = {}
    for trade in trades:
        grouped.setdefault(_exit_rule(trade.exit_reason), []).append(float(trade.pnl_pct))

    total = sum(len(v) for v in grouped.values())
    out: dict[str, dict[str, Any]] = {}
    for key, values in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        values.sort()
        mid = len(values) // 2
        median = (
            values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0
        )
        out[key] = {
            "share_pct": round(100.0 * len(values) / total, 1),
            "median_pnl_pct": round(median, 2),
            "n": len(values),
        }
    return out


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


def _holding_medians(trades) -> dict[str, Any]:
    """How long a winner ran and how long a loser lasted, in sessions.

    The gap between the two is the single clearest statement of what a trend strategy is
    trying to do: cut the losers quickly and let the winners run. A setting where the two
    numbers are close is not doing it.
    """
    wins = [float(t.bars_held) for t in trades if t.pnl > 0]
    losses = [float(t.bars_held) for t in trades if t.pnl <= 0]
    winner, loser = _median(wins), _median(losses)
    return {
        "winner_sessions": None if winner is None else round(winner, 1),
        "loser_sessions": None if loser is None else round(loser, 1),
        "n_wins": len(wins),
        "n_losses": len(losses),
    }


def load_evidence(
    *, strategy_name: str = "trend_momentum", path: Path | None = None
) -> ProfileEvidence:
    """Read the artifact, refusing it if it describes different parameters."""
    source = path or EVIDENCE_PATH
    if not source.exists():
        return ProfileEvidence(
            available=False,
            reason=(
                "No measurement has been taken. Run `app profiles --measure` to produce one; "
                "until then these settings have no measured record and the page says so "
                "rather than guessing."
            ),
        )

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ProfileEvidence(available=False, reason=f"The measurement is unreadable: {exc}")

    expected = profile_fingerprint(resolved_profile_params(strategy_name))
    found = payload.get("fingerprint")
    if found != expected:
        return ProfileEvidence(
            available=False,
            reason=(
                f"The stored measurement was taken with different parameters "
                f"({found} vs {expected}), so it does not describe these settings. "
                "Re-run `app profiles --measure`. Showing it anyway would attach real numbers "
                "to a configuration that never produced them."
            ),
        )

    return ProfileEvidence(
        available=True,
        measured_on=str(payload.get("measured_on", "")),
        universe_size=int(payload.get("universe_size", 0)),
        windows=payload.get("windows", {}),
        by_profile=payload.get("by_profile", {}),
    )


def evidence_age_days(evidence: ProfileEvidence, today: date | None = None) -> int | None:
    if not evidence.available or not evidence.measured_on:
        return None
    try:
        taken = date.fromisoformat(evidence.measured_on)
    except ValueError:
        return None
    return ((today or datetime.now(timezone.utc).date()) - taken).days
