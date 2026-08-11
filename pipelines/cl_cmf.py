"""Pipeline: Chile CMF FECU — Seguros Generales + Seguros de Vida.

Downloads quarterly IFRS financial statements for all active companies from
both CMF portals (Generales and Vida), transforms to long format, and loads to
DuckDB (local) or Databricks (production).

Files are cached locally — re-running only fetches missing quarters.

Usage:
    python -m pipelines.cl_cmf                      # last 10 years, both tipos
    python -m pipelines.cl_cmf --years 5
    python -m pipelines.cl_cmf --tipo generales     # skip Vida
    python -m pipelines.cl_cmf --mode append        # append to existing table
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from connectors.cl.cmf import download, transform, load

RAW_DIR = PROJECT_ROOT / "data" / "local" / "raw" / "cl_fecu"
DB_PATH = PROJECT_ROOT / "data" / "cmf_cl.duckdb"


def run(
    years: int = 10,
    mode: str = "replace",
    tipos: list[str] | None = None,
) -> None:
    if tipos is None:
        tipos = ["generales", "vida"]

    print(f"=== CMF FECU Pipeline — Chile ({' + '.join(t.capitalize() for t in tipos)}) ===")

    print(f"[1/3] Downloading {years} years × {len(tipos)} tipo(s) from CMF...")
    files = download.download_range(RAW_DIR, years=years, tipos=tipos)
    if not files:
        print("  ERROR: no files downloaded.")
        sys.exit(1)

    by_tipo = {}
    for path, period, tipo in files:
        by_tipo.setdefault(tipo, []).append(period)
    for tipo, periods in by_tipo.items():
        periods_s = sorted(periods)
        print(f"  {tipo:<12} {len(periods):>3} quarters  {periods_s[0]} to {periods_s[-1]}")

    print("[2/3] Transforming...")
    parts = []
    for path, period, tipo in files:
        try:
            df = transform.transform(path)
            parts.append(df)
        except Exception as e:
            print(f"  WARN: skipping {path.name}: {e}")

    if not parts:
        print("  ERROR: no files parsed successfully.")
        sys.exit(1)

    combined = pd.concat(parts, ignore_index=True)

    # De-duplicate in case a quarter was downloaded twice (shouldn't happen with caching)
    combined = combined.drop_duplicates(
        subset=["fecha_corte", "rut", "cuenta"]
    ).reset_index(drop=True)

    n_empresas = combined["rut"].nunique()
    n_quarters = combined["fecha_corte"].nunique()
    n_tipos = combined["tipo_compania"].nunique()
    fecha_min = combined["fecha_corte"].min()
    fecha_max = combined["fecha_corte"].max()
    print(f"  -> {len(combined):,} rows, {len(combined.columns)} columns")
    print(f"     {n_empresas} companies, {n_tipos} tipo(s), {n_quarters} quarters ({fecha_min:%Y-%m-%d} to {fecha_max:%Y-%m-%d})")

    print("[3/3] Loading...")
    load.load(combined, db_path=DB_PATH, mode=mode)

    print("Done.")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Chile CMF FECU pipeline (Generales + Vida)")
    parser.add_argument(
        "--years",
        type=int,
        default=10,
        help="Number of years of history to download (default: 10).",
    )
    parser.add_argument(
        "--tipo",
        choices=["generales", "vida", "both"],
        default="both",
        help="Which company type to download (default: both).",
    )
    parser.add_argument(
        "--mode",
        choices=["replace", "append"],
        default="replace",
        help="Load mode (default: replace).",
    )
    args = parser.parse_args()
    tipos = ["generales", "vida"] if args.tipo == "both" else [args.tipo]
    run(years=args.years, mode=args.mode, tipos=tipos)


if __name__ == "__main__":
    _cli()
