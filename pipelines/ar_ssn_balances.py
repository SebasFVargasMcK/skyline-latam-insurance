"""Pipeline: Argentina SSN Balances Aseguradoras.

Downloads quarterly balance-sheet CSV files from the SSN open data portal
(datosabiertos.ssn.gob.ar), filters to LoB-level rows (subramo IS NOT NULL),
and loads to Databricks (insurance_ar.ssn_balances) or DuckDB (local).

Data is available from 2024-Q3 onward.  Each run caches downloaded files so
only new quarters are fetched on subsequent runs.

Usage:
    python -m pipelines.ar_ssn_balances
    python -m pipelines.ar_ssn_balances --mode append
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from connectors.ar.ssn_balances import download, transform, load

RAW_DIR = PROJECT_ROOT / "data" / "local" / "raw" / "ar_ssn_balances"
DB_PATH  = PROJECT_ROOT / "data" / "ssn_ar.duckdb"


def run(mode: str = "replace") -> None:
    print("=== SSN Argentina Balances Pipeline ===")

    print("[1/3] Discovering and downloading quarterly CSV files ...")
    paths = download.download(RAW_DIR)
    if not paths:
        print("  ERROR: no files downloaded.")
        sys.exit(1)
    print(f"  {len(paths)} file(s) available: {[p.name for p in paths]}")

    print("[2/3] Transforming ...")
    df = transform.transform(paths)
    if df.empty:
        print("  ERROR: transform returned empty DataFrame.")
        sys.exit(1)

    n_quarters  = df["fecha_corte"].nunique()
    n_empresas  = df["cia_id"].nunique()
    n_subramos  = df["subramo_id"].nunique()
    n_cuentas   = df["cuenta_id"].nunique()
    fecha_min   = df["fecha_corte"].min()
    fecha_max   = df["fecha_corte"].max()
    lob_counts  = df["lob_l1_en"].value_counts().to_dict()

    print(f"  -> {len(df):,} rows, {len(df.columns)} columns")
    print(f"     {n_empresas} companies, {n_quarters} quarter(s) ({fecha_min} to {fecha_max})")
    print(f"     {n_subramos} subramos, {n_cuentas} accounts")
    print(f"     LoB: {lob_counts}")

    print("[3/3] Loading ...")
    load.load(df, db_path=DB_PATH, mode=mode)

    print("Done.")


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Argentina SSN Balances pipeline"
    )
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
