"""quant-trader -- research, backtesting and paper-trading platform.

Two markets: US equities and the Bolsa de Comercio de Santiago. Runs entirely
locally on free data sources and SQLite.

Read before using any result this produces
------------------------------------------
This system measures historical behaviour. It does not forecast. Nothing in it
establishes that a strategy is profitable, and it is deliberately built to make
a weak strategy *look* weak: transaction costs are mandatory, the optimiser is
walled off from test data, and indicator values are checked for look-ahead bias.

Live order routing is not implemented. ``LIVE_TRADING=true`` is rejected at
startup rather than ignored.
"""

__version__ = "0.1.0"
__phase__ = "1 -- data foundation, database and indicators"
