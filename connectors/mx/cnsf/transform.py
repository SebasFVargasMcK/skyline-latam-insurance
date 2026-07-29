"""Transform the raw CNSF SIO Asegurados Excel into a normalized DataFrame.

The SIO endpoint (descarga-base-ASEG) returns quarterly counts of active
insured policies and claims per Mexican state and insurance branch, covering
Q2 2015 to present.

Output schema (table: cnsf_asegurados):
    fecha_corte        DATE      — quarter-end date (e.g. 2025-12-31)
    anio               INTEGER
    trimestre          INTEGER   — 1–4
    estado             VARCHAR   — Mexican state name
    ramo               VARCHAR   — insurance branch (e.g. "Automóviles")
    num_asegurados     INTEGER   — active insured policies at quarter-end
    num_siniestros     INTEGER   — claims filed in the quarter
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def transform(src: Path) -> pd.DataFrame:
    """Read *src* (raw CNSF SIO Excel) and return the normalized DataFrame."""
    df = pd.read_excel(src, dtype=str)

    required = {"FECHA_CORTE", "DESC_ENTIDADFEDERATIVA", "DESC_RAMO",
                "NUM_ASEG_VIG", "NUM_SIN"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Unexpected CNSF column layout. Missing: {missing}. "
            f"Found: {list(df.columns)}"
        )

    out = pd.DataFrame()
    out["fecha_corte"] = pd.to_datetime(df["FECHA_CORTE"], errors="coerce")
    out["anio"] = out["fecha_corte"].dt.year.astype("Int64")
    out["trimestre"] = ((out["fecha_corte"].dt.month - 1) // 3 + 1).astype("Int64")
    out["estado"] = df["DESC_ENTIDADFEDERATIVA"].str.strip()
    out["ramo"] = df["DESC_RAMO"].str.strip()
    out["num_asegurados"] = pd.to_numeric(df["NUM_ASEG_VIG"], errors="coerce").astype("Int64")
    out["num_siniestros"] = pd.to_numeric(df["NUM_SIN"], errors="coerce").astype("Int64")

    return out.reset_index(drop=True)
