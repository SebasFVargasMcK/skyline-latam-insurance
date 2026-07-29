"""Pipeline: Peru SBS S-203 — Financial Statements per Insurance Company.

Downloads the monthly Estado de Situación Financiera + Estado de Resultados
for all active insurance companies from the SBS portal, transforms to long
format, and loads to DuckDB (local) or Databricks (production).

Usage:
    python -m pipelines.pe_sbs
    python -m pipelines.pe_sbs --year 2025 --month 12
    python -m pipelines.pe_sbs --mode append
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from connectors.pe.sbs import download, transform, load

RAW_DIR = PROJECT_ROOT / "data" / "local" / "raw"
DB_PATH = PROJECT_ROOT / "data" / "sbs_pe.duckdb"


def run(year: int | None = None, month: int | None = None, mode: str = "replace") -> None:
    print("=== SBS S-203 Pipeline — Peru (Empresas de Seguros) ===")

    print("[1/3] Downloading from SBS...")
    raw_file, period = download.download(RAW_DIR, year=year, month=month)
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
    parser = argparse.ArgumentParser(description="Peru SBS S-203 pipeline")
    parser.add_argument("--year", type=int, default=None, help="Year (e.g. 2025)")
    parser.add_argument("--month", type=int, default=None, help="Month number 1-12")
    parser.add_argument(
        "--mode",
        choices=["replace", "append"],
        default="replace",
        help="Load mode (default: replace).",
    )
    args = parser.parse_args()
    run(year=args.year, month=args.month, mode=args.mode)


if __name__ == "__main__":
    _cli()
