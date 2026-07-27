"""End-to-end pipeline: Colombia SFC Formato 290 (insurance statistics).

Data source: Socrata API on datos.gov.co — no scraping needed.
    Full CSV: https://www.datos.gov.co/api/views/e967-4a8r/rows.csv?accessType=DOWNLOAD

Usage:
    python -m pipelines.co_sfc                # download full dataset, local DuckDB
    python -m pipelines.co_sfc --mode append  # append rows instead of replacing

Set DATABRICKS_HOST + DATABRICKS_TOKEN + DATABRICKS_CATALOG + DATABRICKS_SCHEMA
in .env to route the load step to Databricks instead of local DuckDB.

Set SOCRATA_APP_TOKEN to avoid Socrata anonymous rate limits (optional but recommended).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "local" / "raw"
DB_PATH = PROJECT_ROOT / "data" / "sfc.duckdb"


def run(mode: str = "replace") -> None:
    from connectors.co.sfc import download, transform, load

    print("=== SFC Pipeline — Colombia ===")

    print("[1/3] Downloading Formato 290 from datos.gov.co...")
    raw_file = download.download(RAW_DIR)
    print(f"      → {raw_file}")

    print("[2/3] Transforming...")
    df = transform.transform(raw_file)
    print(f"      → {len(df):,} rows, {len(df.columns)} columns")

    print("[3/3] Loading...")
    load.load(df, db_path=DB_PATH, mode=mode)

    print("Done.")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Run the Colombia SFC Formato 290 pipeline")
    parser.add_argument(
        "--mode",
        choices=["replace", "append"],
        default="replace",
        help="Whether to replace or append to the target table (default: replace)",
    )
    args = parser.parse_args()
    run(mode=args.mode)


if __name__ == "__main__":
    _cli()
    sys.exit(0)
