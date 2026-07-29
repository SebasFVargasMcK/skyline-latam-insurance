"""Pipeline: Chile CMF FECU — Seguros Generales.

Downloads quarterly IFRS financial statements for all active Seguros Generales
companies from the CMF FECU portal, transforms to long format, and loads to
DuckDB (local) or Databricks (production).

Usage:
    python -m pipelines.cl_cmf
    python -m pipelines.cl_cmf --date 2025-09-30
    python -m pipelines.cl_cmf --mode append
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from connectors.cl.cmf import download, transform, load

RAW_DIR = PROJECT_ROOT / "data" / "local" / "raw"
DB_PATH = PROJECT_ROOT / "data" / "cmf_cl.duckdb"


def run(date=None, mode: str = "replace") -> None:
    print("=== CMF FECU Pipeline — Chile (Seguros Generales) ===")

    print("[1/3] Downloading from CMF...")
    raw_file, period = download.download(RAW_DIR, date=date)
    print(f"      period: {period}  ->> {raw_file}")

    print("[2/3] Transforming...")
    df = transform.transform(raw_file)
    print(f"      ->> {len(df):,} rows, {len(df.columns)} columns")
    print(f"           {df['rut'].nunique()} companies, {df['cuenta'].nunique()} accounts")

    print("[3/3] Loading...")
    load.load(df, db_path=DB_PATH, mode=mode)

    print("Done.")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Chile CMF FECU pipeline")
    parser.add_argument(
        "--date",
        default=None,
        help="Quarter-end date to download (YYYY-MM-DD). Defaults to latest available.",
    )
    parser.add_argument(
        "--mode",
        choices=["replace", "append"],
        default="replace",
        help="Load mode (default: replace).",
    )
    args = parser.parse_args()
    run(date=args.date, mode=args.mode)


if __name__ == "__main__":
    _cli()
