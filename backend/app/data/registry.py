"""Provider registry.

Callers ask for a provider by market or by name and never import a concrete
provider class. That indirection is what makes the "no paid dependency" rule
enforceable: swapping in a paid vendor later is a registry entry plus a config
value, and the absence of one is visible in a single place.
"""

from __future__ import annotations

from collections.abc import Callable

from app.config import get_settings
from app.core.markets import get_market
from app.data.chile_provider import ChileDataProvider
from app.data.provider import DataProvider
from app.data.yfinance_provider import YFinanceProvider

__all__ = [
    "register_provider",
    "get_provider",
    "provider_for_market",
    "available_providers",
    "provider_cost_table",
]

ProviderFactory = Callable[[], DataProvider]

_FACTORIES: dict[str, ProviderFactory] = {
    "yfinance": YFinanceProvider,
    "chile-yfinance": ChileDataProvider,
}

_INSTANCES: dict[str, DataProvider] = {}


def register_provider(name: str, factory: ProviderFactory, *, replace: bool = False) -> None:
    """Register a provider factory under ``name``.

    Refuses to shadow an existing entry unless ``replace=True``, so a typo in a
    plugin cannot quietly redirect every download to a different vendor.
    """
    key = name.strip().lower()
    if key in _FACTORIES and not replace:
        raise ValueError(
            f"Provider {key!r} is already registered. Pass replace=True if that is intended."
        )
    _FACTORIES[key] = factory
    _INSTANCES.pop(key, None)


def get_provider(name: str) -> DataProvider:
    """Return the singleton instance of a registered provider."""
    key = name.strip().lower()
    if key not in _FACTORIES:
        raise KeyError(
            f"Unknown data provider {name!r}. Registered: {sorted(_FACTORIES)}"
        )
    if key not in _INSTANCES:
        _INSTANCES[key] = _FACTORIES[key]()
    return _INSTANCES[key]


def provider_for_market(market: str) -> DataProvider:
    """Default provider for a market, per ``DEFAULT_PROVIDER_<MARKET>`` in ``.env``.

    The resolved provider is checked against the market it was asked for, so a
    misconfigured ``.env`` fails here rather than downloading US tickers into
    Chilean asset rows.
    """
    code = get_market(market).code
    settings = get_settings()

    configured = {
        "USA": settings.default_provider_usa,
        "CHILE": settings.default_provider_chile,
    }.get(code)

    if not configured:
        raise KeyError(
            f"No DEFAULT_PROVIDER_{code} configured. Add it to .env and to "
            "provider_for_market()."
        )

    # "yfinance" for CHILE means the Chile-specialised subclass, which adds the
    # .SN suffix and the illiquidity flag rather than treating a Santiago ticker
    # like a NASDAQ one.
    if code == "CHILE" and configured.strip().lower() == "yfinance":
        configured = "chile-yfinance"

    provider = get_provider(configured)
    if not provider.supports_market(code):
        raise ValueError(
            f"Provider {provider.name!r} does not support market {code!r} "
            f"(it supports {sorted(provider.capabilities.markets)}). "
            f"Fix DEFAULT_PROVIDER_{code} in .env."
        )
    return provider


def available_providers() -> list[str]:
    return sorted(_FACTORIES)


def provider_cost_table() -> list[dict[str, str]]:
    """Cost and capability summary for every registered provider.

    Printed by the CLI and embedded in backtest reports so a run always states
    where its data came from and what that source cost.
    """
    rows = []
    for name in available_providers():
        caps = get_provider(name).capabilities
        rows.append(
            {
                "provider": caps.name,
                "markets": ", ".join(sorted(caps.markets)),
                "timeframes": ", ".join(t.timeframe.value for t in caps.timeframes),
                "api_key_required": "yes" if caps.requires_api_key else "no",
                "adjusted_prices": "yes" if caps.provides_adjusted_prices else "no",
                "cost": caps.cost,
                "notes": caps.notes,
            }
        )
    return rows
