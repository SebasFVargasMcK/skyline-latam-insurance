"""Pipeline: Estado de Resultados Completo — all 5 countries (AR, CL, CO, EC, PE).

Reads the full income-statement xlsx files from the Latino platform export folder,
normalises them to a shared long-format schema, and loads each country into a
separate Delta table in Databricks:

    {catalog}.insurance_ar.er_completo
    {catalog}.insurance_cl.er_completo
    {catalog}.insurance_co.er_completo
    {catalog}.insurance_ec.er_completo
    {catalog}.insurance_pe.er_completo

Output schema per table:
    pais         STRING   — 2-letter ISO code
    empresa      STRING   — company name as filed
    ano          BIGINT   — fiscal year
    mes          BIGINT   — month number (1-12; quarter-end months for CL/AR)
    periodo_str  STRING   — original period label ("dic 2025", "ene 2021", …)
    cuenta       STRING   — account / line-item code
    descripcion  STRING   — account description
    ramo         STRING   — line of business or "TOTAL"
    valor        DOUBLE   — value in USD thousands

Usage:
    python -m pipelines.er_completo_ingest
    python -m pipelines.er_completo_ingest --country CL CO
    python -m pipelines.er_completo_ingest --dry-run       # parse only, no upload
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from connectors.er_completo import parse_long, parse_ar
from connectors.shared.databricks import upload_dataframe

# ── File source ────────────────────────────────────────────────────────────────
INPUT_DIR = Path(
    r"C:\Users\Sebastian F Vargas\OneDrive - McKinsey & Company"
    r"\Documents\Latino\Estado de Resultados Completo"
)

# ── Country config ─────────────────────────────────────────────────────────────
#   key: 2-letter code
#   value: (file_prefix, parser_module, databricks_schema)
COUNTRY_CONFIG: dict[str, tuple[str, str, str]] = {
    "AR": ("Argentina", "ar",   "insurance_ar"),
    "CL": ("Chile",     "long", "insurance_cl"),
    "CO": ("Colombia",  "long", "insurance_co"),
    "EC": ("Ecuador",   "long", "insurance_ec"),
    "PE": ("Peru",      "long", "insurance_pe"),
}

TABLE_NAME = "er_completo"
DEDUP_KEYS = ["pais", "empresa", "ano", "mes", "cuenta", "ramo"]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_country(pais: str) -> pd.DataFrame:
    prefix, parser_type, _ = COUNTRY_CONFIG[pais]
    files = sorted(INPUT_DIR.glob(f"{prefix} ER ??.xlsx"))
    if not files:
        raise FileNotFoundError(
            f"No files found for {pais} in {INPUT_DIR} (pattern: '{prefix} ER ??.xlsx')"
        )

    parts: list[pd.DataFrame] = []
    for f in files:
        year_suffix = f.stem.split()[-1]  # "15", "16", …
        try:
            if parser_type == "ar":
                df = parse_ar.parse(f, pais=pais)
            else:
                df = parse_long.parse(f, pais=pais)
            parts.append(df)
            print(f"    OK  {f.name:<28}  {len(df):>7,} rows")
        except Exception as exc:
            print(f"    ERR {f.name:<28}  ERROR: {exc}")

    if not parts:
        raise RuntimeError(f"All files failed for {pais}.")

    combined = pd.concat(parts, ignore_index=True)
    before = len(combined)
    combined = combined.drop_duplicates(subset=DEDUP_KEYS).reset_index(drop=True)
    if len(combined) < before:
        print(f"    → dedup removed {before - len(combined):,} rows")
    return combined


def _print_summary(pais: str, df: pd.DataFrame) -> None:
    print(
        f"    {len(df):>9,} rows | "
        f"{df['empresa'].nunique():>4} companies | "
        f"{df['cuenta'].nunique():>3} accounts | "
        f"years {df['ano'].min()}–{df['ano'].max()}"
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def run(countries: list[str], dry_run: bool = False) -> None:
    print("=== Estado de Resultados Completo — ingestion pipeline ===")
    print(f"  Source : {INPUT_DIR}")
    print(f"  Countries : {', '.join(countries)}")
    print(f"  Mode : {'DRY RUN (parse only)' if dry_run else 'UPLOAD to Databricks'}")
    print()

    for pais in countries:
        _, _, schema = COUNTRY_CONFIG[pais]
        print(f"[{pais}] Parsing files...")
        df = _parse_country(pais)
        _print_summary(pais, df)

        if dry_run:
            print(f"[{pais}] Dry run — skipping upload.\n")
            continue

        print(f"[{pais}] Uploading -> {schema}.{TABLE_NAME} ...")
        full_name = upload_dataframe(df, table=TABLE_NAME, schema=schema)
        print(f"[{pais}] Done -> {full_name}\n")

    print("=== All done ===")


def _load_env() -> None:
    """Load .env from project root (same pattern used by gold_latam.py)."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _cli() -> None:
    all_codes = list(COUNTRY_CONFIG.keys())
    parser = argparse.ArgumentParser(
        description="Ingest Estado de Resultados Completo files into Databricks."
    )
    parser.add_argument(
        "--country",
        nargs="+",
        choices=all_codes,
        default=all_codes,
        metavar="CODE",
        help=f"Countries to ingest (default: all). Choices: {all_codes}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse files and show stats without uploading to Databricks.",
    )
    args = parser.parse_args()
    _load_env()
    run(countries=args.country, dry_run=args.dry_run)


if __name__ == "__main__":
    _cli()
