"""Entry point.

``python -m app`` is the CLI; ``uvicorn app.main:app`` serves the API. Both are
re-exported here so a single module name works for either, which keeps deployment
tooling from having to know the difference.
"""

from __future__ import annotations

from app.api.main import app
from app.__main__ import app as cli

__all__ = ["app", "cli", "main"]


def main() -> None:
    """Run the CLI."""
    cli()


if __name__ == "__main__":
    main()
