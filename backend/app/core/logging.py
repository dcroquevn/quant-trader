"""Logging setup.

Two sinks: a rich console for humans and a rotating file for the audit trail.

The file sink matters beyond debugging. Section 38 of the project brief requires
that every trading decision be recorded with its reasoning, so a signal can be
reconstructed months later -- including the indicator values that produced it
and how much historical evidence stood behind it. ``log_decision`` writes that
structured record.
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from typing import Any

from rich.logging import RichHandler

from app.config import get_settings

_CONFIGURED = False

DECISION_LOGGER_NAME = "quant_trader.decisions"


def setup_logging(level: str | None = None, *, force: bool = False) -> None:
    """Configure root logging. Idempotent unless ``force=True``."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    settings = get_settings()
    resolved_level = (level or settings.log_level).upper()

    root = logging.getLogger()
    root.setLevel(resolved_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = RichHandler(
        rich_tracebacks=True,
        show_path=False,
        omit_repeated_times=False,
    )
    console.setFormatter(logging.Formatter("%(message)s", datefmt="%H:%M:%S"))
    root.addHandler(console)

    log_path = settings.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(file_handler)

    # yfinance and urllib3 are chatty at INFO and drown out our own output.
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("peewee").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Module logger, configuring logging on first use."""
    setup_logging()
    return logging.getLogger(name)


def log_decision(
    *,
    symbol: str,
    market: str,
    action: str,
    reasons: list[str],
    features: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Record one trading decision and the reasoning behind it.

    Parameters
    ----------
    action:
        ``"BUY"``, ``"SELL"`` or ``"HOLD"``.
    reasons:
        Human-readable conditions that fired, e.g.
        ``["price > EMA200", "RSI 31 below oversold threshold 35"]``.
    features:
        Indicator values at decision time, for later reconstruction.
    evidence:
        Historical-analogue statistics backing the decision, e.g.
        ``{"observations": 127, "median_20d_return_pct": 3.4}``. Absent or thin
        evidence is itself worth recording.
    """
    logger = get_logger(DECISION_LOGGER_NAME)
    payload = {
        "symbol": symbol,
        "market": market,
        "action": action,
        "reasons": reasons,
        "features": features or {},
        "evidence": evidence or {},
    }
    logger.info("DECISION %s", json.dumps(payload, default=str, sort_keys=True))
