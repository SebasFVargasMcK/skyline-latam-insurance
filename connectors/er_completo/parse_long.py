"""Parser for the 9-column LONG-format Estado de Resultados files.

Used by Chile (CL), Colombia (CO), Ecuador (EC), and Peru (PE).
Sheet name: 'Columnas'
Columns: País | Empresas | Código | Nombre del Indice / Cuenta |
         Período | Año | Ramos - Otra Información | Valor | Moneda

Values are already in USD thousands (pre-converted by the Latino platform).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

MES_MAP: dict[str, int] = {
    "ene": 1,  "feb": 2,  "mar": 3,  "abr": 4,
    "may": 5,  "jun": 6,  "jul": 7,  "ago": 8,
    "sep": 9,  "oct": 10, "nov": 11, "dic": 12,
}

COLUMN_RENAMES = {
    "País":                         "pais_origen",
    "Empresas":                     "empresa",
    "Código":                       "cuenta",
    "Nombre del Indice / Cuenta":   "descripcion",
    "Período":                      "periodo_str",
    "Año":                          "ano",
    "Ramos - Otra Información":     "ramo",
    "Valor":                        "valor",
    "Moneda":                       "moneda",
}


def parse(path: Path, pais: str) -> pd.DataFrame:
    """Read one 'ER completo' LONG-format xlsx and return a normalised DataFrame.

    Args:
        path: Path to the xlsx file.
        pais: 2-letter ISO country code to stamp on every row (e.g. 'CL').
    """
    df = pd.read_excel(path, sheet_name="Columnas", header=0, engine="openpyxl")
    df = df.rename(columns=COLUMN_RENAMES)

    # Derive month number from period string ("dic 2025" → 12)
    df["mes"] = (
        df["periodo_str"]
        .astype(str)
        .str.lower()
        .str.split()
        .str[0]
        .map(MES_MAP)
    )

    df["pais"] = pais
    df["ano"] = pd.to_numeric(df["ano"], errors="coerce").astype("Int64")
    df["mes"] = df["mes"].astype("Int64")
    df["cuenta"] = df["cuenta"].astype(str).str.strip()
    df["valor"] = pd.to_numeric(df["valor"], errors="coerce")

    out_cols = ["pais", "empresa", "ano", "mes", "periodo_str",
                "cuenta", "descripcion", "ramo", "valor"]
    return df[out_cols].copy()
