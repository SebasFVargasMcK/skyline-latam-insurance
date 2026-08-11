"""Pipeline: Peru SBS Boletin Estadistico — S-345 Primas según Ramos.

Downloads December EoY files for the last 10 years (2015-2025), transforms
each XLS into long format (empresa × LoB × cuenta), and loads the combined
DataFrame to DuckDB locally or Databricks in production.

Usage:
    python -m pipelines.pe_sbs_boletin
    python -m pipelines.pe_sbs_boletin --years 2020 2021 2022
    python -m pipelines.pe_sbs_boletin --mode append
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from connectors.pe.sbs_boletin import download, transform, load

RAW_DIR = PROJECT_ROOT / "data" / "local" / "raw" / "pe_boletin"
DB_PATH = PROJECT_ROOT / "data" / "pe_boletin.duckdb"


def run(mode: str = "replace", years: list[int] | None = None) -> None:
    print("=== SBS Boletin S-345 Pipeline — Peru (Primas segun Ramos) ===")

    print("[1/3] Downloading EoY files from SBS...")
    paths = download.download(RAW_DIR, years=years)
    if not paths:
        print("  ERROR: no files downloaded — check connectivity to intranet2.sbs.gob.pe")
        sys.exit(1)
    years_downloaded = sorted(int(p.stem.replace("S-345-di", "")) for p in paths)
    print(f"  Downloaded {len(paths)} files: {years_downloaded[0]}-{years_downloaded[-1]}")

    print("[2/3] Transforming...")
    df = transform.transform(paths)
    n_empresas = df["empresa"].nunique()
    n_years = df["anio"].nunique()
    year_range = f"{df['anio'].min()}-{df['anio'].max()}"
    lobs = sorted(df["lob_l1"].dropna().unique().tolist())
    print(f"  -> {len(df):,} rows, {len(df.columns)} columns")
    print(f"     {n_empresas} companies, {n_years} years ({year_range})")
    print(f"     LoB groups: {lobs}")

    print("[3/3] Loading...")
    load.load(df, db_path=DB_PATH, mode=mode)

    print("Done.")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Peru SBS Boletin S-345 pipeline")
    parser.add_argument(
        "--years",
        nargs="+",
        type=int,
        metavar="YEAR",
        help="Specific years to download (default: 2015-2025)",
    )
    parser.add_argument(
        "--mode",
        choices=["replace", "append"],
        default="replace",
        help="Load mode (default: replace)",
    )
    args = parser.parse_args()
    run(mode=args.mode, years=args.years)


if __name__ == "__main__":
    _cli()
