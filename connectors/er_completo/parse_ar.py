"""Parser for the Argentina SSN WIDE-format Estado de Resultados files.

Sheet name: 'Cuadros'
Layout: 4-row stacked header, then 32 account-code rows.
  Row 0 (1-indexed row 1): company names  — repeats 4× per company
  Row 1:                   period strings — e.g. "dic 2025", "sep 2025"
  Row 2:                   "TOTALES"
  Row 3:                   "Valor"
  Col 0: account code (numeric string)
  Col 1: account description
  Cols 2+: values — one column per (company × period) combination

Total columns ≈ 760: 2 label cols + 4 periods × ~189 companies.

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

# Rows at the top of the sheet that carry the multi-level header
_HEADER_ROWS = 4


def parse(path: Path, pais: str = "AR") -> pd.DataFrame:
    """Read one Argentina ER WIDE xlsx and return a normalised long-format DataFrame.

    Args:
        path: Path to the xlsx file.
        pais: Country code (always 'AR').
    """
    raw = pd.read_excel(
        path, sheet_name="Cuadros", header=None, engine="openpyxl"
    )

    # ── Header metadata ────────────────────────────────────────────────────
    company_row = raw.iloc[0, 2:].tolist()   # company names (repeated 4× each)
    period_row  = raw.iloc[1, 2:].tolist()   # period strings

    # ── Account labels (rows after header) ────────────────────────────────
    data_block = raw.iloc[_HEADER_ROWS:].reset_index(drop=True)
    acc_codes  = data_block.iloc[:, 0].astype(str).str.strip()
    acc_descs  = data_block.iloc[:, 1].astype(str).str.strip()
    values_mat = data_block.iloc[:, 2:].reset_index(drop=True)

    # ── Melt ───────────────────────────────────────────────────────────────
    records: list[dict] = []
    n_rows = len(acc_codes)

    for col_i, (company, period_str) in enumerate(zip(company_row, period_row)):
        company    = str(company).strip()    if pd.notna(company)    else ""
        period_str = str(period_str).strip() if pd.notna(period_str) else ""
        if not company or not period_str or company == "nan" or period_str == "nan":
            continue

        parts = period_str.lower().split()
        if len(parts) != 2:
            continue
        mes_str, ano_str = parts
        mes = MES_MAP.get(mes_str)
        try:
            ano = int(ano_str)
        except ValueError:
            continue
        if mes is None:
            continue

        col_vals = values_mat.iloc[:, col_i]
        for row_i in range(n_rows):
            val = col_vals.iloc[row_i]
            records.append(
                {
                    "pais":        pais,
                    "empresa":     company,
                    "ano":         ano,
                    "mes":         mes,
                    "periodo_str": period_str,
                    "cuenta":      acc_codes.iloc[row_i],
                    "descripcion": acc_descs.iloc[row_i],
                    "ramo":        "TOTAL",
                    "valor":       float(val) if pd.notna(val) else None,
                }
            )

    df = pd.DataFrame(records)
    df["ano"] = df["ano"].astype("Int64")
    df["mes"] = df["mes"].astype("Int64")
    return df
