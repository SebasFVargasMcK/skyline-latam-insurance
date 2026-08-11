"""Pipeline: Ecuador SCVS — Ranking financiero de compañías de seguros.

Downloads annual financial data for all registered insurance and reinsurance
companies from the SCVS ranking portal (bi_ranking.csv + bi_compania.csv),
transforms to long format, and loads to DuckDB (local) or Databricks (production).

NOTE: The supercias.gob.ec domain may be blocked on corporate networks.
Set SCVS_USE_WAYBACK=1 to use Internet Archive snapshots for development.

Usage:
    python -m pipelines.ec_scvs
    python -m pipelines.ec_scvs --wayback        # use Wayback Machine archive
    python -m pipelines.ec_scvs --mode append
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from connectors.ec.scvs import download, transform, load

RAW_DIR = PROJECT_ROOT / "data" / "local" / "raw"
DB_PATH = PROJECT_ROOT / "data" / "scvs_ec.duckdb"


def run(mode: str = "replace", wayback: bool = False) -> None:
    print("=== SCVS Ranking Pipeline — Ecuador (Compañías de Seguros) ===")

    print("[1/3] Downloading from SCVS...")
    ranking_path, compania_path = download.download(RAW_DIR, use_wayback=wayback)
    print(f"      ranking -> {ranking_path}")
    print(f"      compania -> {compania_path}")

    print("[2/3] Transforming...")
    df = transform.transform(ranking_path, compania_path)
    print(f"      -> {len(df):,} rows, {len(df.columns)} columns")
    empresas = df["nombre"].nunique()
    anios = sorted(df["anio"].unique().tolist())
    cuentas = df["cuenta"].nunique()
    print(f"         {empresas} companies, {len(anios)} years ({anios[0]}-{anios[-1]}), {cuentas} metrics")

    print("[3/3] Loading...")
    load.load(df, db_path=DB_PATH, mode=mode)

    print("Done.")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Ecuador SCVS ranking pipeline")
    parser.add_argument(
        "--wayback",
        action="store_true",
        help="Use Internet Archive snapshots instead of the live SCVS portal.",
    )
    parser.add_argument(
        "--mode",
        choices=["replace", "append"],
        default="replace",
        help="Load mode (default: replace).",
    )
    args = parser.parse_args()
    run(mode=args.mode, wayback=args.wayback)


if __name__ == "__main__":
    _cli()
