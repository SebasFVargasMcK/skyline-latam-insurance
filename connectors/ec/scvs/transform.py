"""Transform SCVS ranking CSVs into a normalized long-format DataFrame.

Inputs:
  scvs_ranking.csv   — filtered bi_ranking.csv (insurance CIIU only)
  scvs_compania.csv  — bi_compania.csv (full company directory)

The ranking file has one row per (company, year) with ~54 financial columns.
This module merges on `expediente`, then melts the numeric metric columns
into long format so each row is one (company, year, metric) observation.

Output schema (table: scvs_ranking):
    anio         INTEGER  — calendar year
    expediente   INTEGER  — SCVS company registry ID
    ruc          VARCHAR  — tax ID (RUC)
    nombre       VARCHAR  — company name
    provincia    VARCHAR  — registered province
    ciiu_n6      VARCHAR  — CIIU Rev 4 code (e.g. K6512.02)
    cuenta       VARCHAR  — metric name (e.g. activos, patrimonio, roe)
    valor        DOUBLE   — metric value (USD or ratio)
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# Columns that identify a row — excluded from the melt
_ID_COLS = ["anio", "expediente", "ruc", "nombre", "provincia", "ciiu_n6"]

# Columns from bi_ranking that are not useful metrics
_DROP_COLS = {
    "posicion_general",
    "cia_imvalores",
    "id_estado_financiero",
    "cod_segmento",
    "ciiu_n1",
}


def transform(ranking_path: Path, compania_path: Path) -> pd.DataFrame:
    """Merge ranking + company data and return long-format DataFrame."""
    ranking = pd.read_csv(ranking_path, low_memory=False)
    compania = pd.read_csv(compania_path, low_memory=False, usecols=["expediente", "ruc", "nombre", "provincia"])

    # Merge company info
    df = ranking.merge(compania, on="expediente", how="left")

    # Coerce year to int
    df["anio"] = pd.to_numeric(df["anio"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["anio", "ciiu_n6"])

    # Identify value columns: numeric, not in ID/drop sets
    all_id = set(_ID_COLS) | _DROP_COLS
    value_cols = [
        c for c in df.columns
        if c not in all_id
        and pd.api.types.is_numeric_dtype(df[c])
    ]

    # Ensure ID cols exist (compania merge may add ruc/nombre/provincia)
    for col in ["ruc", "nombre", "provincia"]:
        if col not in df.columns:
            df[col] = None

    melted = df.melt(
        id_vars=_ID_COLS,
        value_vars=value_cols,
        var_name="cuenta",
        value_name="valor",
    )

    melted = melted.dropna(subset=["valor"])
    melted["anio"] = melted["anio"].astype(int)
    melted["expediente"] = pd.to_numeric(melted["expediente"], errors="coerce").astype("Int64")
    melted["valor"] = pd.to_numeric(melted["valor"], errors="coerce")
    melted["nombre"] = melted["nombre"].astype(str).str.strip()
    melted["ruc"] = melted["ruc"].astype(str).str.strip()
    melted["provincia"] = melted["provincia"].astype(str).str.strip()
    melted["ciiu_n6"] = melted["ciiu_n6"].astype(str).str.strip()

    return melted[_ID_COLS + ["cuenta", "valor"]].reset_index(drop=True)
