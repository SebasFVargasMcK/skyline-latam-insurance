"""Pipeline: Argentina SSN — Estados Patrimoniales y de Resultados.

Downloads the latest quarterly financial statements (balance sheet + P&L)
for all ~186 active Argentine insurance companies from the SSN portal,
transforms to long format, and loads to DuckDB (local) or Databricks (production).

Usage:
    python -m pipelines.ar_ssn
    python -m pipelines.ar_ssn --mode append
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from connectors.ar.ssn import download, transform, load

RAW_DIR = PROJECT_ROOT / "data" / "local" / "raw"
DB_PATH = PROJECT_ROOT / "data" / "ssn_ar.duckdb"


def run(mode: str = "replace") -> None:
    print("=== SSN Estados Patrimoniales Pipeline — Argentina ===")

    print("[1/3] Downloading from SSN...")
    raw_file, period = download.download(RAW_DIR)
    print(f"      period: {period}  ->> {raw_file}")

    print("[2/3] Transforming...")
    df = transform.transform(raw_file)
    print(f"      ->> {len(df):,} rows, {len(df.columns)} columns")
    empresas = df["empresa"].nunique()
    cuentas = df["cuenta"].nunique()
    hojas = df["hoja"].unique().tolist()
    print(f"           {empresas} companies, {cuentas} accounts, sheets: {hojas}")

    print("[3/3] Loading...")
    load.load(df, db_path=DB_PATH, mode=mode)

    print("Done.")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Argentina SSN Estados Patrimoniales pipeline")
    parser.add_argument(
        "--mode",
        choices=["replace", "append"],
        default="replace",
        help="Load mode (default: replace).",
    )
    args = parser.parse_args()
    run(mode=args.mode)


if __name__ == "__main__":
    _cli()
