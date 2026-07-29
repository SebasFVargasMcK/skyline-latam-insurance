"""Transform the raw CMF FECU XLS into a normalized long-format DataFrame.

The raw XLS has one row per company and ~218 financial account columns.
This transform melts it into long format: one row per (company, account, quarter).

Output schema (table: cmf_fecu_sg):
    fecha_corte   DATE     — quarter-end date (e.g. 2025-12-31)
    ano           INTEGER
    trimestre     INTEGER  — 1–4
    rut           VARCHAR  — company RUT (e.g. "76.212.519-6")
    razon_social  VARCHAR  — shortened company name
    tipo_compania VARCHAR  — e.g. "CIAS. DE SEGUROS GENERALES"
    cuenta        VARCHAR  — IFRS account code (e.g. "5.31.11.10")
    descripcion   VARCHAR  — account description (e.g. "Prima directa")
    valor         DOUBLE   — amount in thousands of CLP
"""

from __future__ import annotations

import io
import re
import warnings
from pathlib import Path

import pandas as pd

_QUARTER_LAST_DAYS = {3: 31, 6: 30, 9: 30, 12: 31}

# Regex to split "   5.31.11.10 Prima directa" → ("5.31.11.10", "Prima directa")
_ACCOUNT_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)\s+(.*)")


def _parse_account(raw: str) -> tuple[str, str]:
    m = _ACCOUNT_RE.match(raw.strip())
    if m:
        return m.group(1), m.group(2).strip()
    return raw.strip(), raw.strip()


def _parse_fecha(val: str) -> pd.Timestamp | None:
    """Parse '12 / 2025' → Timestamp(2025-12-31)."""
    m = re.match(r"(\d+)\s*/\s*(\d+)", str(val))
    if m:
        month, year = int(m.group(1)), int(m.group(2))
        day = _QUARTER_LAST_DAYS.get(month, 30)
        try:
            return pd.Timestamp(year, month, day)
        except ValueError:
            return pd.NaT
    return pd.NaT


def transform(src: Path) -> pd.DataFrame:
    """Read raw FECU XLS and return the normalized long-format DataFrame."""
    raw = src.read_bytes()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df_raw = pd.read_excel(io.BytesIO(raw), engine="xlrd", header=None)

    # Row 5 (0-indexed) contains the column headers
    headers = df_raw.iloc[5].tolist()

    # Locate the "Total" summary row so we exclude it and footnotes
    total_idx = None
    for i in range(len(df_raw) - 1, 5, -1):
        cell = str(df_raw.iloc[i, 0])
        if "Total" in cell and "Seguros" in cell:
            total_idx = i
            break
    if total_idx is None:
        total_idx = len(df_raw)  # fallback: use all rows

    # Company data rows: 6 → total_idx (exclusive)
    data = df_raw.iloc[6:total_idx].copy()
    data.columns = headers

    # The first four columns are always: Fecha, RUT, Razón social, Tipo de compañía
    meta_cols = headers[:4]
    account_cols = [c for c in headers[4:] if pd.notna(c)]

    # Drop rows where RUT is missing (blank separator rows)
    rut_col = meta_cols[1]
    data = data.dropna(subset=[rut_col])

    # Melt to long format
    melted = data.melt(
        id_vars=meta_cols,
        value_vars=account_cols,
        var_name="cuenta_raw",
        value_name="valor",
    )

    # Parse account code + description
    parsed = melted["cuenta_raw"].apply(_parse_account)
    melted["cuenta"] = [p[0] for p in parsed]
    melted["descripcion"] = [p[1] for p in parsed]

    # Parse date
    fecha_col = meta_cols[0]
    melted["fecha_corte"] = melted[fecha_col].apply(_parse_fecha)
    melted["ano"] = melted["fecha_corte"].dt.year.astype("Int64")
    melted["trimestre"] = ((melted["fecha_corte"].dt.month - 1) // 3 + 1).astype("Int64")

    # Rename metadata columns to canonical names
    razon_col, tipo_col = meta_cols[2], meta_cols[3]
    melted = melted.rename(columns={
        rut_col: "rut",
        razon_col: "razon_social",
        tipo_col: "tipo_compania",
    })

    # Select and order output columns
    out = melted[[
        "fecha_corte", "ano", "trimestre",
        "rut", "razon_social", "tipo_compania",
        "cuenta", "descripcion", "valor",
    ]].copy()

    # Clean strings
    for col in ("rut", "razon_social", "tipo_compania", "cuenta", "descripcion"):
        out[col] = out[col].astype(str).str.strip()

    # Cast valor to numeric (some cells may be strings or NaN)
    out["valor"] = pd.to_numeric(out["valor"], errors="coerce")

    # Drop rows with no meaningful data
    out = out.dropna(subset=["rut", "cuenta", "fecha_corte"])
    out = out[out["rut"] != "nan"]

    return out.reset_index(drop=True)
