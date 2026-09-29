"""The data engine: acquisition, incremental refresh and integrity checks.

This is the layer between vendors and the database. Its contract:

* Bars in the database are **UTC-stamped, validated and de-duplicated**.
* Re-running a download is **idempotent** -- the same window twice produces the
  same rows, not duplicates.
* An **incremental refresh re-fetches a small overlap** rather than starting
  exactly where it left off, because the most recent bar may have been provisional
  when it was first stored.
* **Nothing is ever interpolated.** A gap stays a gap. Fabricated prices are the
  single most expensive kind of bug in this project, because they produce results
  that look fine.

Gap detection deserves a note. There is no free, reliable holiday calendar for the
Bolsa de Santiago, so this engine cannot distinguish "the exchange was closed" from
"the vendor lost a day". It therefore reports *candidate* gaps -- missing weekdays --
and leaves interpretation to a human. Claiming to know which is which would be a
lie dressed as a feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import (
    DataError,
    EmptyDataError,
    StaleDataError,
    SymbolNotFoundError,
)
from app.core.logging import get_logger
from app.core.markets import Market, get_market
from app.core.universe import (
    BENCHMARKS,
    DEFAULT_UNIVERSE,
    AssetSpec,
    find_asset,
    universe_for_market,
)
from app.data.provider import DataProvider, Timeframe
from app.data.registry import provider_for_market
from app.database import repository as repo
from app.database.models import Asset

logger = get_logger(__name__)

__all__ = [
    "DownloadResult",
    "GapReport",
    "DataEngine",
    "trim_carried_forward_tail",
    "STALE_QUOTE_RUN_LIMIT",
    "ZERO_VOLUME_ALERT_FRACTION",
]


# Re-fetch this many days on an incremental update. The newest stored bar may
# have been mid-session and provisional; overlapping re-writes it with the
# settled values. Three days also covers a weekend.
INCREMENTAL_OVERLAP_DAYS = 5

ZERO_VOLUME_WINDOW = 20
"""Recent bars examined for a degraded volume feed."""

ZERO_VOLUME_ALERT_FRACTION = 0.5
"""Fraction of recent bars with zero volume before the feed is called degraded.

Observed on 2026-09-27: 17 of the last 20 real Chilean bars had a genuine price range
and zero reported volume, while all US instruments were clean. Those bars are not
carried-forward quotes -- the price moved -- so they are kept, but every volume-derived
feature becomes zero or undefined and a strategy with a volume gate silently stops
firing. Half is well above the ~2% historical base rate for these instruments.
"""

STALE_QUOTE_RUN_LIMIT = 3
"""Trailing carried-forward bars before the audit flags a series as notable.

This is a *reporting* threshold, not a trading gate. Freshness is decided by
trimming the invented tail and applying the ordinary age tolerance to the last real
print, so a single quiet session is handled correctly without any threshold at all
-- see :meth:`DataEngine.assert_fresh`.

The value exists so ``GapReport.has_findings`` does not flag every thinly traded
Chilean name over one or two quiet days. Observed on 2026-09-27: Yahoo served
49 consecutive identical zero-volume bars for SQM-B.SN and ANDINA-B.SN and 5 for the
other 16 Chilean tickers. One or two in a row is a genuinely quiet instrument; three
or more is the vendor filling a hole.
"""


@dataclass(slots=True)
class DownloadResult:
    """Outcome of one symbol/timeframe download.

    ``ok=False`` with a populated ``error`` is a normal, expected outcome for
    free providers, not an exception to swallow. The CLI prints every failure.
    """

    symbol: str
    market: str
    timeframe: str
    ok: bool
    provider: str = ""
    provider_symbol: str = ""
    bars_written: int = 0
    bars_skipped: int = 0
    first_bar: datetime | None = None
    last_bar: datetime | None = None
    error: str = ""

    @property
    def status(self) -> str:
        if not self.ok:
            return "FAILED"
        if self.bars_written == 0:
            return "UP-TO-DATE"
        return "OK"


@dataclass(slots=True)
class GapReport:
    """Candidate gaps in one asset's stored series."""

    symbol: str
    market: str
    timeframe: str
    stored_bars: int
    first_bar: datetime | None
    last_bar: datetime | None
    missing_weekdays: list[date] = field(default_factory=list)
    duplicate_timestamps: list[datetime] = field(default_factory=list)

    largest_gap_sessions: int = 0
    """Longest run of consecutive *missing sessions*, ignoring weekends.

    Counted in sessions rather than calendar days on purpose. Five absent
    weekdays spanning a weekend are three calendar days plus two, which reads as
    two small gaps when it is really one week-long hole -- and the size of the
    hole is what decides whether the series is usable.
    """

    recent_zero_volume_pct: float = 0.0
    """Share of the last ``ZERO_VOLUME_WINDOW`` bars reporting zero volume, in percent.

    Separate from ``stale_quote_run`` and measuring a different failure: those bars are
    flat *and* volumeless, these have real price movement with no volume. The second kind
    silently disables every volume-based rule instead of looking like missing data.
    """

    volume_feed_degraded: bool = False

    is_stale: bool = False
    stale_by_days: int = 0

    stale_quote_run: int = 0
    """Trailing bars that are flat with zero volume -- a carried-forward quote.

    Distinct from ``is_stale``, and the more dangerous of the two. ``is_stale``
    means the newest bar is old; this means the newest bar is *dated today and is
    not real*. Yahoo repeats the last traded price for Chilean tickers that have
    not printed, so the series looks current while carrying no information. A
    signal generated on such a bar is acting on a quote nobody offered.
    """

    @property
    def has_findings(self) -> bool:
        return bool(
            self.missing_weekdays
            or self.duplicate_timestamps
            or self.is_stale
            or self.stale_quote_run >= STALE_QUOTE_RUN_LIMIT
            or self.volume_feed_degraded
        )

    def summary(self) -> str:
        if self.stored_bars == 0:
            return "no data stored"
        parts = [f"{self.stored_bars} bars"]
        if self.missing_weekdays:
            parts.append(
                f"{len(self.missing_weekdays)} missing weekdays "
                f"(longest run {self.largest_gap_sessions} sessions)"
            )
        if self.duplicate_timestamps:
            parts.append(f"{len(self.duplicate_timestamps)} duplicate timestamps")
        if self.is_stale:
            parts.append(f"stale by {self.stale_by_days}d")
        if self.stale_quote_run:
            parts.append(f"{self.stale_quote_run} trailing carried-forward quotes")
        if self.volume_feed_degraded:
            parts.append(
                f"volume feed degraded ({self.recent_zero_volume_pct:.0f}% of recent "
                "bars report zero volume)"
            )
        return "; ".join(parts)


class DataEngine:
    """Download, store and audit market data."""

    def __init__(self, session: Session, *, provider: DataProvider | None = None) -> None:
        self._session = session
        self._forced_provider = provider
        self._settings = get_settings()

    # ------------------------------------------------------------------ #
    # Universe
    # ------------------------------------------------------------------ #

    def sync_universe(self, specs: tuple[AssetSpec, ...] | None = None) -> int:
        """Write markets and asset rows into the database. Idempotent.

        Benchmarks are included: they must be downloadable even though they are
        never traded.
        """
        repo.sync_markets(self._session)
        source = specs if specs is not None else DEFAULT_UNIVERSE + BENCHMARKS

        seen: set[tuple[str, str]] = set()
        count = 0
        for spec in source:
            key = (spec.symbol, spec.market)
            if key in seen:
                continue
            seen.add(key)
            repo.upsert_asset(self._session, spec)
            count += 1

        logger.info("Synced %d assets across markets into the database", count)
        return count

    def _provider_for(self, market: str) -> DataProvider:
        return self._forced_provider or provider_for_market(market)

    def resolve(self, spec: AssetSpec, *, force: bool = False) -> tuple[str, str]:
        """Resolve ``spec`` to a working provider ticker. Returns ``(provider, ticker)``.

        The result is cached on the asset row, so resolution costs one request per
        instrument for the lifetime of the database rather than one per download.
        ``force=True`` re-probes, which is what to do when a vendor renames
        something.

        Raises
        ------
        SymbolNotFoundError
            No candidate resolved. For instruments with no declared candidates at
            all -- the IPSA index, for example -- this is immediate and explains
            that the provider simply does not carry it.
        """
        provider = self._provider_for(spec.market)
        row = repo.get_asset(self._session, spec.symbol, spec.market)

        if (
            not force
            and row is not None
            and row.provider == provider.name
            and row.provider_symbol
        ):
            return provider.name, row.provider_symbol

        # An empty mapping, or one that declares nothing for this provider's ticker
        # namespace, means the provider does not carry the instrument. That is a
        # definite answer -- the IPSA index, for instance -- so fail immediately
        # instead of probing the canonical symbol and hoping.
        namespace = provider.symbol_namespace
        candidates = spec.provider_symbols.get(namespace, ())
        if not candidates:
            raise SymbolNotFoundError(spec.symbol, provider.name, ())

        resolved = provider.resolve_symbol(candidates, spec.symbol)

        if row is None:
            row = repo.upsert_asset(self._session, spec)
        row.provider = provider.name
        row.provider_symbol = resolved
        row.resolution_checked_at = datetime.now(timezone.utc)
        self._session.flush()
        return provider.name, resolved

    # ------------------------------------------------------------------ #
    # Download
    # ------------------------------------------------------------------ #

    def download_symbol(
        self,
        spec: AssetSpec,
        timeframe: "str | Timeframe" = Timeframe.D1,
        *,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
        incremental: bool = True,
    ) -> DownloadResult:
        """Download and store bars for one instrument.

        With ``incremental=True`` and bars already stored, the fetch starts
        ``INCREMENTAL_OVERLAP_DAYS`` before the newest stored bar and ``start`` is
        ignored. Pass ``incremental=False`` to force a full re-download.

        Never raises for data problems -- every failure is returned as a
        ``DownloadResult`` with ``ok=False``. A universe download of 30 symbols
        should not abort because one vendor ticker went missing.
        """
        tf = Timeframe.parse(timeframe)
        result = DownloadResult(
            symbol=spec.symbol, market=spec.market, timeframe=tf.value, ok=False
        )

        try:
            provider_name, provider_symbol = self.resolve(spec)
        except DataError as exc:
            result.error = str(exc)
            return result

        result.provider = provider_name
        result.provider_symbol = provider_symbol

        row = repo.get_asset(self._session, spec.symbol, spec.market)
        if row is None:
            row = repo.upsert_asset(self._session, spec)

        fetch_start = start
        if incremental:
            latest = repo.latest_bar_date(self._session, row.id, tf.value)
            if latest is not None:
                fetch_start = latest - timedelta(days=INCREMENTAL_OVERLAP_DAYS)
                logger.debug(
                    "%s: incremental refresh from %s (newest stored %s)",
                    spec.symbol,
                    fetch_start.date(),
                    latest.date(),
                )

        provider = self._provider_for(spec.market)
        try:
            frame = provider.fetch_bars(
                provider_symbol,
                tf,
                start=fetch_start,
                end=end,
                canonical_symbol=spec.symbol,
            )
        except EmptyDataError as exc:
            # Nothing new is the normal outcome of refreshing an up-to-date symbol.
            if incremental and repo.bar_count(self._session, row.id, tf.value) > 0:
                result.ok = True
                result.error = ""
                logger.debug("%s: nothing new (%s)", spec.symbol, exc)
                return result
            result.error = str(exc)
            return result
        except DataError as exc:
            result.error = str(exc)
            return result
        except Exception as exc:  # noqa: BLE001 -- one bad symbol must not stop the run
            result.error = f"{type(exc).__name__}: {exc}"
            logger.warning("Unexpected failure downloading %s: %s", spec.symbol, exc)
            return result

        written, skipped = repo.upsert_bars(
            self._session,
            row.id,
            tf.value,
            frame,
            source=provider_name,
            is_adjusted="adj_close" in frame.columns and frame["adj_close"].notna().any(),
        )

        result.ok = True
        result.bars_written = written
        result.bars_skipped = skipped
        result.first_bar = repo.earliest_bar_date(self._session, row.id, tf.value)
        result.last_bar = repo.latest_bar_date(self._session, row.id, tf.value)

        if skipped:
            logger.warning(
                "%s: dropped %d invalid bars (not repaired -- see repository.upsert_bars)",
                spec.symbol,
                skipped,
            )
        return result

    def download_universe(
        self,
        market: str | None = None,
        timeframe: "str | Timeframe" = Timeframe.D1,
        *,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
        incremental: bool = True,
        include_benchmarks: bool = True,
        symbols: list[str] | None = None,
    ) -> list[DownloadResult]:
        """Download many instruments, continuing past individual failures.

        Each symbol is committed as soon as it lands. A full-universe download over
        a free provider takes minutes and can be interrupted -- by a rate limit, a
        dropped connection, or Ctrl-C. Committing once at the end would throw away
        every symbol already fetched, which then has to be re-downloaded from a
        vendor that is rate-limiting precisely because of the retry.
        """
        specs = self._select_specs(market, symbols, include_benchmarks)
        results: list[DownloadResult] = []

        for index, spec in enumerate(specs, start=1):
            logger.info(
                "[%d/%d] %s (%s)", index, len(specs), spec.symbol, spec.market
            )
            result = self.download_symbol(
                spec, timeframe, start=start, end=end, incremental=incremental
            )
            results.append(result)

            try:
                self._session.commit()
            except Exception:
                # A commit failure is about this symbol's rows, not the batch.
                # Roll back so the session stays usable for the remaining symbols.
                self._session.rollback()
                result.ok = False
                result.error = "commit failed; rows for this symbol were discarded"
                logger.exception("Commit failed after %s; continuing", spec.symbol)

            if not result.ok:
                logger.warning("  -> FAILED: %s", result.error)
            else:
                logger.info("  -> %s (%d bars written)", result.status, result.bars_written)

        ok = sum(1 for r in results if r.ok)
        logger.info("Download finished: %d/%d succeeded", ok, len(results))
        return results

    def _select_specs(
        self,
        market: str | None,
        symbols: list[str] | None,
        include_benchmarks: bool,
    ) -> list[AssetSpec]:
        if symbols:
            return [find_asset(s, market) for s in symbols]

        if market is None:
            specs = list(DEFAULT_UNIVERSE)
            if include_benchmarks:
                specs += [b for b in BENCHMARKS if b.provider_symbols]
        else:
            code = get_market(market).code
            specs = list(universe_for_market(code))
            if include_benchmarks:
                from app.core.universe import benchmark_for_market

                bench = benchmark_for_market(code)
                if bench.available and bench.symbol:
                    try:
                        spec = find_asset(bench.symbol)
                    except KeyError:
                        spec = None
                    if spec is not None and not any(
                        s.symbol == spec.symbol and s.market == spec.market for s in specs
                    ):
                        specs.append(spec)

        # Drop instruments no provider can serve (the IPSA index), so a universe
        # download does not report a failure for something known to be absent.
        return [s for s in specs if s.provider_symbols]

    # ------------------------------------------------------------------ #
    # Integrity
    # ------------------------------------------------------------------ #

    def audit_symbol(
        self,
        spec: AssetSpec,
        timeframe: "str | Timeframe" = Timeframe.D1,
        *,
        as_of: datetime | None = None,
    ) -> GapReport:
        """Audit one stored series for gaps, duplicates and staleness."""
        tf = Timeframe.parse(timeframe)
        row = repo.get_asset(self._session, spec.symbol, spec.market)
        report = GapReport(
            symbol=spec.symbol,
            market=spec.market,
            timeframe=tf.value,
            stored_bars=0,
            first_bar=None,
            last_bar=None,
        )
        if row is None:
            return report

        frame = repo.load_bars(self._session, row.id, tf.value, use_adjusted=False)
        report.stored_bars = len(frame)
        if frame.empty:
            return report

        report.first_bar = frame.index[0].to_pydatetime()
        report.last_bar = frame.index[-1].to_pydatetime()

        # load_bars de-duplicates by construction; a duplicate here would mean the
        # unique constraint was bypassed, which is worth screaming about.
        dupes = frame.index[frame.index.duplicated()]
        report.duplicate_timestamps = [ts.to_pydatetime() for ts in dupes]

        if tf is Timeframe.D1:
            market = get_market(spec.market)
            present = set(frame.index.normalize().date)
            expected = pd.date_range(
                frame.index[0].normalize(), frame.index[-1].normalize(), freq="D"
            )
            missing = [
                d.date()
                for d in expected
                if market.is_trading_day(d.date()) and d.date() not in present
            ]
            report.missing_weekdays = missing
            report.largest_gap_sessions = _largest_session_run(missing, market)

        # Index series are exempt. A level series legitimately has no volume and no
        # intraday range, so the flat-and-volumeless test -- correct for an equity --
        # would mark every IPSA bar as fabricated and refuse the benchmark entirely.
        # The check is about detecting a vendor filling a hole in a *traded*
        # instrument, and an index is not traded.
        if spec.asset_class == "index":
            report.stale_quote_run = 0
        else:
            report.stale_quote_run = _trailing_stale_quote_run(frame)

        # Measured on the bars that survive trimming, so the flat tail does not inflate
        # it -- the two failures are counted separately on purpose.
        real_bars, _ = trim_carried_forward_tail(frame)
        if "volume" in real_bars.columns and len(real_bars):
            recent = real_bars["volume"].tail(ZERO_VOLUME_WINDOW)
            if len(recent):
                fraction = float((recent.fillna(0.0) <= 0).mean())
                report.recent_zero_volume_pct = round(fraction * 100.0, 2)
                report.volume_feed_degraded = fraction >= ZERO_VOLUME_ALERT_FRACTION

        reference = as_of or datetime.now(timezone.utc)
        age_days = (reference - report.last_bar).days
        tolerance = self._settings.stale_data_max_age_days
        if age_days > tolerance:
            report.is_stale = True
            report.stale_by_days = age_days - tolerance

        return report

    def audit_universe(
        self,
        market: str | None = None,
        timeframe: "str | Timeframe" = Timeframe.D1,
    ) -> list[GapReport]:
        specs = self._select_specs(market, None, include_benchmarks=True)
        return [self.audit_symbol(spec, timeframe) for spec in specs]

    def assert_fresh(
        self,
        spec: AssetSpec,
        timeframe: "str | Timeframe" = Timeframe.D1,
        *,
        as_of: datetime | None = None,
    ) -> None:
        """Raise ``StaleDataError`` if the newest bar is older than tolerated.

        Called before generating live signals. Acting on a stale series is how a
        paper bot ends up trading last week's setup at today's price.
        """
        report = self.audit_symbol(spec, timeframe, as_of=as_of)
        if report.stored_bars == 0:
            raise StaleDataError(
                f"No stored bars for {spec.symbol} ({spec.market}, {report.timeframe}); "
                "run a download before generating signals."
            )

        reference = as_of or datetime.now(timezone.utc)
        tolerance = self._settings.stale_data_max_age_days

        # Freshness is judged on the last *real* print, not the last stored row. A
        # carried-forward tail makes a series look current, so testing the stored
        # last bar would clear a name whose genuine trading stopped weeks ago --
        # which is the SQM-B case exactly. Strip the invented rows, then apply the
        # ordinary age tolerance to what remains. This is stricter than the blanket
        # refusal it replaces for names that really are dead, and more permissive
        # for names that merely had a couple of quiet sessions.
        if report.stale_quote_run:
            asset = repo.get_asset(self._session, spec.symbol, spec.market)
            frame = repo.load_bars(
                self._session,
                asset.id,
                Timeframe.parse(timeframe).value,
                use_adjusted=False,
            )
            trimmed, dropped = trim_carried_forward_tail(frame)
            if trimmed.empty:
                raise StaleDataError(
                    f"{spec.symbol} ({spec.market}) has no real prints at all: every "
                    f"one of its {dropped} stored bars is flat with zero volume."
                )
            last_real = trimmed.index[-1].to_pydatetime()
            age = (reference - last_real).days
            if age > tolerance:
                raise StaleDataError(
                    f"{spec.symbol} ({spec.market}) last traded {last_real:%Y-%m-%d}, "
                    f"{age} day(s) ago, beyond the {tolerance}-day tolerance. The "
                    f"{dropped} bar(s) after that date are flat with zero volume: the "
                    "vendor carried the last price forward, so the series looks "
                    "current but is not. Refusing to signal on a quote nobody offered."
                )
            return

        if report.is_stale:
            raise StaleDataError(
                f"{spec.symbol} ({spec.market}) newest bar is {report.last_bar:%Y-%m-%d}, "
                f"{report.stale_by_days} day(s) beyond the {tolerance}-day tolerance."
            )

    def coverage(self, timeframe: "str | Timeframe" = Timeframe.D1) -> pd.DataFrame:
        """Per-asset coverage table: what is actually stored right now."""
        return repo.coverage_report(self._session, Timeframe.parse(timeframe).value)

    def load(
        self,
        symbol: str,
        market: str | None = None,
        timeframe: "str | Timeframe" = Timeframe.D1,
        *,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
        use_adjusted: bool = True,
        trim_carried_forward: bool = False,
    ) -> pd.DataFrame:
        """Load a stored series by canonical symbol.

        ``trim_carried_forward=True`` drops the trailing run of vendor-invented flat
        zero-volume bars. It defaults to False so a plain load still shows exactly
        what is stored -- an audit has to be able to see the fiction. Signal
        generation and feature extraction should pass True. See
        :func:`trim_carried_forward_tail`.

        The number of bars removed is recorded on the returned frame under
        ``frame.attrs["carried_forward_dropped"]``, so a caller can report it rather
        than silently showing an "as of" date that differs from the database.

        ``symbol`` must be in the declared universe. To load an instrument that is
        not -- a one-off, or a spec built at runtime -- call :meth:`load_spec`.
        """
        return self.load_spec(
            find_asset(symbol, market),
            timeframe,
            start=start,
            end=end,
            use_adjusted=use_adjusted,
            trim_carried_forward=trim_carried_forward,
        )

    def load_spec(
        self,
        spec: AssetSpec,
        timeframe: "str | Timeframe" = Timeframe.D1,
        *,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
        use_adjusted: bool = True,
        trim_carried_forward: bool = False,
    ) -> pd.DataFrame:
        """Load stored bars for an explicit :class:`AssetSpec`.

        The core of :meth:`load`, separated so callers holding a spec do not have to
        round-trip through a symbol lookup that would reject anything outside the
        declared universe.
        """
        row = repo.get_asset(self._session, spec.symbol, spec.market)
        tf = Timeframe.parse(timeframe).value

        if row is None:
            empty = repo.load_bars(self._session, -1, tf)
            empty.attrs["carried_forward_dropped"] = 0
            return empty

        frame = repo.load_bars(
            self._session,
            row.id,
            tf,
            start=start,
            end=end,
            use_adjusted=use_adjusted,
        )

        dropped = 0
        # Index levels are flat and volumeless by nature; trimming them would delete
        # the whole series. See audit_symbol for the same exemption.
        if trim_carried_forward and not frame.empty and spec.asset_class != "index":
            frame, dropped = trim_carried_forward_tail(frame)
            if dropped:
                logger.info(
                    "%s (%s): dropped %d trailing carried-forward bar(s); last real "
                    "bar is now %s",
                    spec.symbol,
                    spec.market,
                    dropped,
                    frame.index[-1].date() if not frame.empty else "none",
                )
        frame.attrs["carried_forward_dropped"] = dropped
        return frame


def trim_carried_forward_tail(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop trailing carried-forward bars. Returns ``(trimmed, n_dropped)``.

    Removes the flat zero-volume run at the end of a series -- the bars the vendor
    invented to keep the series looking current. Everything earlier is untouched:
    an isolated dead session mid-history is a real fact about that day.

    This is the one place in the project where rows are deliberately removed rather
    than kept, so the justification has to be narrow. It is that these rows are not
    observations at all. A gap is informative and must survive; a fabricated price
    dated to today is worse than a gap, because every rolling indicator treats it as
    evidence and every freshness check based on timestamps waves it through.

    Trimming is also what makes the staleness check meaningful for Chilean names:
    removing SQM-B's 49 invented bars reveals that its last real print was ten weeks
    ago, which is the fact a caller actually needs.
    """
    dropped = _trailing_stale_quote_run(frame)
    if dropped == 0:
        return frame, 0
    if dropped >= len(frame):
        # Every bar is flat and volume-less; there is nothing real to keep.
        return frame.iloc[0:0], len(frame)
    return frame.iloc[:-dropped], dropped


def _trailing_stale_quote_run(frame: pd.DataFrame) -> int:
    """Count trailing bars that are flat (O=H=L=C) with zero volume.

    Walks backwards from the newest bar and stops at the first bar that shows any
    intraday range or any volume. Counting only the *trailing* run matters: an
    isolated dead session in the middle of a history is a fact about that day,
    while a run at the end means the newest data is not real and anything acting on
    it is acting on nothing.
    """
    if frame.empty:
        return 0

    required = {"open", "high", "low", "close"}
    if not required <= set(frame.columns):
        return 0

    volume = frame["volume"] if "volume" in frame.columns else None
    run = 0
    for i in range(len(frame) - 1, -1, -1):
        row = frame.iloc[i]
        flat = row["open"] == row["high"] == row["low"] == row["close"]
        dead = True if volume is None else (float(volume.iloc[i] or 0.0) <= 0.0)
        if flat and dead:
            run += 1
        else:
            break
    return run


def _largest_session_run(days: list[date], market: Market) -> int:
    """Longest run of consecutive missing *sessions*, treating weekends as contiguous.

    Two missing days count as consecutive sessions when every calendar day
    strictly between them is a non-trading day. So Thursday through the following
    Wednesday, with the weekend in between, is one run of five -- which is what a
    human reading the report needs to know.
    """
    if not days:
        return 0

    ordered = sorted(days)
    longest = current = 1
    for previous, nxt in zip(ordered, ordered[1:]):
        gap = [
            previous + timedelta(days=offset)
            for offset in range(1, (nxt - previous).days)
        ]
        if all(not market.is_trading_day(day) for day in gap):
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest
