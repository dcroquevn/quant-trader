"""Exception hierarchy.

Every failure mode the project handles deliberately gets a named class, so call
sites can distinguish "the vendor is rate-limiting us, back off and retry" from
"this ticker does not exist, stop asking". A bare ``except Exception`` anywhere
in the data path is a bug.
"""

from __future__ import annotations


class QuantTraderError(Exception):
    """Base class for every error this project raises on purpose."""


# --------------------------------------------------------------------------- #
# Data layer
# --------------------------------------------------------------------------- #


class DataError(QuantTraderError):
    """Base class for data acquisition and validation failures."""


class ProviderError(DataError):
    """A data provider failed in a way that is not the caller's fault."""


class ProviderUnavailableError(ProviderError):
    """The provider could not be reached: DNS failure, timeout, 5xx."""


class RateLimitError(ProviderError):
    """The provider rate-limited us. Callers should back off and retry."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SymbolNotFoundError(DataError):
    """No provider candidate resolved to a real instrument.

    Distinct from ``ProviderUnavailableError``: retrying will not help.
    """

    def __init__(self, symbol: str, provider: str, tried: tuple[str, ...] = ()) -> None:
        tried_str = ", ".join(tried) if tried else "(none)"
        super().__init__(
            f"Symbol {symbol!r} did not resolve on provider {provider!r}. "
            f"Candidates tried: {tried_str}"
        )
        self.symbol = symbol
        self.provider = provider
        self.tried = tried


class EmptyDataError(DataError):
    """The provider answered successfully but returned no bars."""


class CorruptDataError(DataError):
    """Bars violate basic invariants (negative price, high < low, NaN OHLC)."""


class StaleDataError(DataError):
    """The newest available bar is older than the configured tolerance."""


class UnsupportedTimeframeError(DataError):
    """The provider does not offer the requested timeframe."""


# --------------------------------------------------------------------------- #
# Research layer
# --------------------------------------------------------------------------- #


class InsufficientDataError(QuantTraderError):
    """Not enough observations to compute something honestly.

    Raised instead of returning a number derived from a handful of points. The
    projection engine converts this into an explicit "Insufficient historical
    evidence" result rather than a misleading estimate.
    """


class DataLeakageError(QuantTraderError):
    """An operation tried to read data it is not allowed to see.

    The canonical case: an optimiser reaching into the TEST split. This is a
    hard error, never a warning, because a leaked result is worse than no
    result -- it looks valid.
    """


class LookaheadBiasError(QuantTraderError):
    """A computation used information that was not available at that timestamp."""


# --------------------------------------------------------------------------- #
# Execution layer
# --------------------------------------------------------------------------- #


class ExecutionError(QuantTraderError):
    """Base class for order-handling failures."""


class LiveTradingDisabledError(ExecutionError):
    """Something tried to route a real order while live trading is disabled."""


class DuplicateOrderError(ExecutionError):
    """An order with the same client id / dedupe key was already submitted."""


class OrderRejectedError(ExecutionError):
    """The broker or paper engine refused the order."""


class MarketClosedError(ExecutionError):
    """An order was submitted outside the venue's regular session."""


class KillSwitchActiveError(ExecutionError):
    """A risk limit tripped; no new positions may be opened."""


class RiskLimitError(ExecutionError):
    """A proposed order would breach a configured risk limit."""
