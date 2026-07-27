"""End-to-end pipeline: Colombia SFC Formato 290.

TODO: Implement once the SFC download endpoint is identified.

Usage (future):
    python -m pipelines.co_sfc
    python -m pipelines.co_sfc --date 2025-03-31
"""

from __future__ import annotations

import argparse
import sys


def run(date: str | None = None, mode: str = "replace") -> None:
    raise NotImplementedError(
        "Colombia SFC pipeline is not yet implemented. "
        "See connectors/co/sfc/download.py."
    )


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Run the Colombia SFC ingestion pipeline")
    parser.add_argument("--date", default=None)
    parser.add_argument("--mode", choices=["replace", "append"], default="replace")
    args = parser.parse_args()
    run(date=args.date, mode=args.mode)


if __name__ == "__main__":
    _cli()
    sys.exit(0)
