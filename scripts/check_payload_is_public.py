"""Refuse to publish anything that is not public market data.

    python scripts/check_payload_is_public.py site/market.json site/index.html

Exits 1 and names the offending field if the payload has grown anything beyond prices.

Why this exists as a gate rather than a convention
--------------------------------------------------
The repository is public and this file is served from it. The whole design rests on one claim --
that the published data describes the market and not the reader -- and that claim is held up by
a single function in `app/reporting/webapp.py`. A change there that added a holding would publish
someone's positions, and every other check in the pipeline would pass: the tests would still be
green, the page would still render, the deploy would still succeed.

An **allowlist**, not a denylist. A denylist only catches leaks whose names were guessed in
advance; an allowlist turns a new field into a decision someone has to make on purpose.

`tests/test_webapp.py` asserts the same property from the other side: building the payload with
and without a recorded position must produce identical bytes. Two different checks of one rule,
because this is the rule that cannot be allowed to fail quietly.
"""

import json
import sys
from pathlib import Path

TOP_LEVEL = {
    "version",
    "generated_at",
    "as_of",
    "verification_date",
    "turnover_window",
    "liquidity_threshold",
    "strategy",
    "profiles",
    "evidence",
    "fx",
    "regions",
    "instruments",
    "skipped",
}

PER_INSTRUMENT = {
    "by_profile",
    "ema200",
    "rsi",
    "macd_hist",
    "rel_volume",
    "atr_pct",
    "from_high",
    "name",
    "region",
    "sector",
    "etf",
    "turnover",
    "thin",
    "liquidity_caveat",
    "notes",
    "d",
    "h",
    "l",
    "c",
    "e",
    "a",
}

PAGE_MUST_NOT_CONTAIN = (
    # The marker a holdings-bearing digest carries. If the page were ever built from that
    # renderer instead of the app template, this is what would be sitting in it.
    "contains personal position data",
)


def fail(message: str) -> None:
    print(f"::error::{message}")
    sys.exit(1)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2

    data_path, page_path = Path(argv[1]), Path(argv[2])
    if not data_path.exists():
        fail(f"{data_path} was not built; nothing to publish.")

    payload = json.loads(data_path.read_text(encoding="utf-8"))

    unexpected = sorted(set(payload) - TOP_LEVEL)
    if unexpected:
        fail(
            f"{data_path.name} has unexpected top-level keys: {unexpected}. If one of them "
            "carries position data, publishing this would make it world-readable. Add it to "
            "TOP_LEVEL here only after checking it describes the market and not a person."
        )

    for symbol, instrument in payload.get("instruments", {}).items():
        extra = sorted(set(instrument) - PER_INSTRUMENT)
        if extra:
            fail(f"{symbol} carries unexpected fields: {extra}")

    if page_path.exists():
        page = page_path.read_text(encoding="utf-8")
        for marker in PAGE_MUST_NOT_CONTAIN:
            if marker in page:
                fail(f"{page_path.name} contains {marker!r} and must not be published.")

    count = len(payload.get("instruments", {}))
    size = data_path.stat().st_size / 1024
    print(f"OK: {count} instruments, {size:,.0f} KB, market data only.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
