"""Strategy registry.

Strategies are looked up by name so the CLI, API and optimiser never import a concrete
class. Phase 4 will construct hundreds of parameter variants through
:func:`build_strategy`, which is why parameter validation happens at construction
rather than at first use — a bad combination should fail before a backtest consumes
minutes of CPU.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.strategies.base import Strategy, StrategyParams
from app.strategies.trend_momentum import TrendMomentumStrategy

__all__ = [
    "register_strategy",
    "get_strategy_class",
    "build_strategy",
    "available_strategies",
    "strategy_catalog",
]

StrategyFactory = Callable[..., Strategy]

_STRATEGIES: dict[str, type[Strategy]] = {
    TrendMomentumStrategy.name: TrendMomentumStrategy,
}


def register_strategy(cls: type[Strategy], *, replace: bool = False) -> None:
    """Register a strategy class under its ``name``."""
    key = cls.name.strip().lower()
    if key in _STRATEGIES and not replace:
        raise ValueError(
            f"Strategy {key!r} is already registered. Pass replace=True if intended."
        )
    _STRATEGIES[key] = cls


def get_strategy_class(name: str) -> type[Strategy]:
    key = name.strip().lower()
    if key not in _STRATEGIES:
        raise KeyError(
            f"Unknown strategy {name!r}. Registered: {sorted(_STRATEGIES)}"
        )
    return _STRATEGIES[key]


def build_strategy(name: str, params: dict[str, Any] | StrategyParams | None = None) -> Strategy:
    """Instantiate a strategy, validating its parameters immediately.

    ``params`` may be a dict (from the CLI, an API request or an optimiser trial) or an
    already-built params object. A dict with an unknown key raises rather than being
    silently ignored — a typo in a parameter name would otherwise produce a run of the
    *default* configuration reported under the name of the intended one.
    """
    cls = get_strategy_class(name)

    if params is None:
        return cls()
    if isinstance(params, StrategyParams):
        return cls(params)

    params_cls = type(cls.default_params())
    return cls(params_cls.from_dict(params))


def available_strategies() -> list[str]:
    return sorted(_STRATEGIES)


def strategy_catalog() -> list[dict[str, Any]]:
    """Name, description and default parameters for every registered strategy."""
    catalog = []
    for key in available_strategies():
        cls = _STRATEGIES[key]
        defaults = cls.default_params()
        catalog.append(
            {
                "name": cls.name,
                "version": cls.version,
                "description": cls.description,
                "default_params": defaults.to_dict(),
            }
        )
    return catalog
