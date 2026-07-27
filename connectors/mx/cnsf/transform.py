"""Transform the raw CNSF Estado de Resultados Excel into a normalized DataFrame.

Output schema (matches the DuckDB table estado_resultados):
    fecha_corte_raw   VARCHAR   — original date string from the file
    fecha_corte       DATE      — parsed date
    anio_corte        INTEGER
    trimestre_corte   INTEGER   — 1–4
    entidad           VARCHAR   — insurance company name
    id_nivel          VARCHAR   — hierarchical level id
    descripcion       VARCHAR   — financial concept (line item)
    operacion         VARCHAR   — business line / branch
    importe           DOUBLE    — main amount
    desagregado       DOUBLE    — breakdown amount (may be null)
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# Map common CNSF column name variants → canonical names
_COL_MAP: dict[str, str] = {
    # fecha
    "fecha de corte": "fecha_corte_raw",
    "fechacorte": "fecha_corte_raw",
    "fecha_corte": "fecha_corte_raw",
    # entidad
    "entidad": "entidad",
    "institución": "entidad",
    "institucion": "entidad",
    "nombre": "entidad",
    # nivel
    "id nivel": "id_nivel",
    "id_nivel": "id_nivel",
    "nivel": "id_nivel",
    # descripcion
    "descripción": "descripcion",
    "descripcion": "descripcion",
    "concepto": "descripcion",
    # operacion
    "operación": "operacion",
    "operacion": "operacion",
    "ramo": "operacion",
    "tipo": "operacion",
    # importe
    "importe": "importe",
    "monto": "importe",
    "valor": "importe",
    # desagregado
    "desagregado": "desagregado",
}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [str(c).strip() for c in df.columns]
    rename = {}
    for col in df.columns:
        key = col.lower().replace("_", " ").strip()
        if key in _COL_MAP:
            rename[col] = _COL_MAP[key]
    return df.rename(columns=rename)


def _parse_quarter(date: pd.Series) -> pd.Series:
    month = date.dt.month
    return ((month - 1) // 3 + 1).astype("Int64")


def transform(src: Path) -> pd.DataFrame:
    """Read *src* (raw CNSF Excel) and return the normalized DataFrame."""
    raw = pd.read_excel(src, dtype=str)
    df = _normalize_columns(raw)

    required = {"fecha_corte_raw", "entidad", "descripcion", "operacion", "importe"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Could not map required columns: {missing}. "
            f"Found columns: {list(df.columns)}"
        )

    df["fecha_corte"] = pd.to_datetime(df["fecha_corte_raw"], dayfirst=False, errors="coerce")
    df["anio_corte"] = df["fecha_corte"].dt.year.astype("Int64")
    df["trimestre_corte"] = _parse_quarter(df["fecha_corte"])

    df["importe"] = pd.to_numeric(df["importe"], errors="coerce")
    if "desagregado" in df.columns:
        df["desagregado"] = pd.to_numeric(df["desagregado"], errors="coerce")
    else:
        df["desagregado"] = pd.NA

    if "id_nivel" not in df.columns:
        df["id_nivel"] = pd.NA

    out = df[
        [
            "fecha_corte_raw",
            "fecha_corte",
            "anio_corte",
            "trimestre_corte",
            "entidad",
            "id_nivel",
            "descripcion",
            "operacion",
            "importe",
            "desagregado",
        ]
    ].copy()

    out["entidad"] = out["entidad"].str.strip()
    out["descripcion"] = out["descripcion"].str.strip()
    out["operacion"] = out["operacion"].str.strip()

    return out.reset_index(drop=True)
