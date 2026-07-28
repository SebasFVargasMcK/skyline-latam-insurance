"""Transform the raw SFC Formato 290 CSV into a normalized DataFrame.

The Socrata full-export CSV uses latin-1 encoding and Title Case column names
with spaces (e.g. "Tipo Entidad"). The JSON API uses lowercase underscore names.
This transform handles both formats by normalising all names to lowercase
underscore without accents.

Output schema (stored as sfc_formato_290 in DuckDB / Databricks):
    periodo                  DATE     — first day of reporting month
    ano                      INTEGER
    mes                      INTEGER  — 1–12
    tipo_entidad             VARCHAR
    codigo_entidad           VARCHAR
    nombre_entidad           VARCHAR
    unidad_de_captura        VARCHAR  — numeric code
    nombre_unidad_de_captura VARCHAR  — e.g. "PRIMAS RETENIDAS"
    subcuenta                VARCHAR  — numeric code
    nombre_subcuenta         VARCHAR  — e.g. "PRIMAS EMITIDAS DIRECTAS"
    total                    DOUBLE
    subtotal_ramos           DOUBLE
    <one column per insurance line>  DOUBLE
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pandas as pd

# Required identifier columns (after normalisation)
_ID_COLS = [
    "ano",
    "mes",
    "tipo_entidad",
    "codigo_entidad",
    "nombre_entidad",
    "unidad_de_captura",
    "nombre_unidad_de_captura",
    "subcuenta",
    "nombre_subcuenta",
]


def _slug(text: str) -> str:
    """lowercase + strip accents + spaces/dots/hyphens → underscore."""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    return (
        ascii_str.lower()
        .replace(" ", "_")
        .replace(".", "_")
        .replace("-", "_")
        .strip("_")
    )


def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [_slug(str(c)) for c in df.columns]
    # Socrata JSON API encodes ñ as _o in field names (a_o); unify to 'ano'
    if "a_o" in df.columns:
        df = df.rename(columns={"a_o": "ano"})
    return df


def transform(src: Path) -> pd.DataFrame:
    """Read the raw Formato 290 CSV and return the normalized DataFrame."""
    # Try strict UTF-8 first; fall back to latin-1 for older exports
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            df = pd.read_csv(src, dtype=str, low_memory=False, encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"Could not decode {src} with latin-1, utf-8-sig, or utf-8")

    df = _normalise_columns(df)

    missing_id = set(_ID_COLS) - set(df.columns)
    if missing_id:
        raise ValueError(
            f"Missing expected identifier columns: {missing_id}. "
            f"Found: {list(df.columns[:15])}..."
        )

    # Build a proper date from year + month (first day of month)
    df["ano"] = pd.to_numeric(df["ano"], errors="coerce").astype("Int64")
    df["mes"] = pd.to_numeric(df["mes"], errors="coerce").astype("Int64")
    df["periodo"] = pd.to_datetime(
        df["ano"].astype(str) + "-" + df["mes"].astype(str).str.zfill(2) + "-01",
        errors="coerce",
    )

    # Cast every non-identifier column to numeric
    id_set = set(_ID_COLS) | {"periodo"}
    for col in df.columns:
        if col not in id_set:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Strip whitespace from string columns
    for col in ["nombre_entidad", "nombre_unidad_de_captura", "nombre_subcuenta"]:
        if col in df.columns:
            df[col] = df[col].str.strip()

    # Reorder: date columns first, then identifiers, then all numeric columns
    front = [
        "periodo",
        "ano",
        "mes",
        "tipo_entidad",
        "codigo_entidad",
        "nombre_entidad",
        "unidad_de_captura",
        "nombre_unidad_de_captura",
        "subcuenta",
        "nombre_subcuenta",
    ]
    remaining = [c for c in df.columns if c not in front]
    return df[front + remaining].reset_index(drop=True)
