"""End-to-end pipeline: Mexico CNSF Estado de Resultados.

Usage:
    python -m pipelines.mx_cnsf                   # latest quarter-end, local DuckDB
    python -m pipelines.mx_cnsf --date 2025-03-31  # specific date
    python -m pipelines.mx_cnsf --mode append      # append instead of replace

Set DATABRICKS_HOST + DATABRICKS_TOKEN + DATABRICKS_CATALOG + DATABRICKS_SCHEMA
to route the load step to Databricks instead of local DuckDB.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "local" / "raw"
DB_PATH = PROJECT_ROOT / "data" / "cnsf.duckdb"


def run(date: str | None = None, mode: str = "replace") -> None:
    from connectors.mx.cnsf import download, transform, load

    print("=== CNSF Pipeline — Mexico ===")

    print(f"[1/3] Downloading... (date={date or 'latest'})")
    raw_file = download.download(RAW_DIR, date=date)
    print(f"      ->> {raw_file}")

    print("[2/3] Transforming...")
    df = transform.transform(raw_file)
    print(f"      ->> {len(df):,} rows, {len(df.columns)} columns")

    print("[3/3] Loading...")
    load.load(df, db_path=DB_PATH, mode=mode)

    print("Done.")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Run the Mexico CNSF ingestion pipeline")
    parser.add_argument("--date", default=None, help="Reporting date YYYY-MM-DD (default: latest quarter-end)")
    parser.add_argument("--mode", choices=["replace", "append"], default="replace")
    args = parser.parse_args()
    run(date=args.date, mode=args.mode)


if __name__ == "__main__":
    _cli()
    sys.exit(0)
