"""Transform SBS S-203 XLS into a normalized long-format DataFrame.

The XLS has two sheets:
  P001 — Estado de Situación Financiera (Balance Sheet)
  P002 — Estado de Resultados (Income Statement)

Each sheet has ~18 companies laid out in horizontal blocks:
  row 4 : company names (in the MN or ME column of each block)
  row 5 : sub-headers "MN", "ME", "TOTAL" for each company
  row 6+ : account names in col 0 (repeated every 13 cols for layout),
            values in the MN/ME/TOTAL columns for each company.

Output schema (table: sbs_s203):
    fecha_corte  DATE     — last day of the month (e.g. 2026-06-30)
    anio         INTEGER
    mes          INTEGER
    empresa      VARCHAR  — company name (e.g. "RIMAC")
    hoja         VARCHAR  — "balance_general" or "estado_resultados"
    cuenta       VARCHAR  — line item (e.g. "Caja y Bancos")
    mn           DOUBLE   — moneda nacional (thousands of PEN)
    me           DOUBLE   — moneda extranjera (thousands of PEN equivalent)
    total        DOUBLE   — total (thousands of PEN)
"""

from __future__ import annotations

import io
import re
import warnings
from pathlib import Path

import pandas as pd

_MONTH_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "setiembre": 9, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

_SHEET_NAMES = {
    "P001": "balance_general",
    "P002": "estado_resultados",
}

# Footnote markers — stop reading data rows when col 0 matches these
_FOOTNOTE_RE = re.compile(r"^\s*(\d+/|Mediante|A partir|1/|2/|3/|4/|5/)", re.IGNORECASE)


def _parse_date(row1_val: str) -> pd.Timestamp:
    """Parse 'Al 30 de Junio del 2026' → Timestamp(2026-06-30)."""
    m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+del?\s+(\d{4})", str(row1_val), re.IGNORECASE)
    if not m:
        raise ValueError(f"Cannot parse date from: {row1_val!r}")
    day, month_str, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    month = _MONTH_ES.get(month_str)
    if month is None:
        raise ValueError(f"Unknown month: {month_str!r}")
    return pd.Timestamp(year, month, day)


def _find_data_end(df: pd.DataFrame) -> int:
    """Return the last row index (inclusive) that contains account data."""
    last_data = 5
    for i in range(6, len(df)):
        cell = str(df.iloc[i, 0]).strip()
        if not cell or cell == "nan":
            continue
        if _FOOTNOTE_RE.match(cell):
            break
        last_data = i
    return last_data


def _build_company_map(df: pd.DataFrame) -> list[tuple[str, int, int, int]]:
    """Return [(empresa, mn_col, me_col, total_col), ...] from header rows."""
    row4 = df.iloc[4].tolist()
    row5 = df.iloc[5].tolist()

    # Find all TOTAL column positions from row 5
    total_cols = [j for j, v in enumerate(row5) if str(v).strip() == "TOTAL"]

    companies = []
    for t_col in total_cols:
        mn_col = t_col - 2
        me_col = t_col - 1

        # Find company name in row 4: scan [mn_col, mn_col+1, me_col, t_col]
        name = None
        for search_col in range(mn_col, t_col + 1):
            v = str(row4[search_col]).strip() if search_col < len(row4) else "nan"
            if v and v != "nan" and v != "Activo":
                name = v
                break
        if name is None:
            continue

        # Clean company name: strip footnote markers like "1/" and trailing spaces
        name = re.sub(r"\s*\d+/$", "", name).strip()
        companies.append((name, mn_col, me_col, t_col))

    return companies


def _parse_sheet(df: pd.DataFrame, hoja: str, fecha: pd.Timestamp) -> pd.DataFrame:
    """Parse one sheet and return long-format DataFrame."""
    companies = _build_company_map(df)
    data_end = _find_data_end(df)

    # Account label columns (col 0 repeated every 13 cols for layout)
    label_cols = [0] + [j for j in range(13, df.shape[1], 13) if j < df.shape[1]]

    rows = []
    for i in range(6, data_end + 1):
        # Get account name from col 0 (most reliable)
        account = str(df.iloc[i, 0]).strip()
        if not account or account == "nan":
            continue
        account = account.strip()

        for empresa, mn_col, me_col, t_col in companies:
            mn_val = df.iloc[i, mn_col] if mn_col < df.shape[1] else None
            me_val = df.iloc[i, me_col] if me_col < df.shape[1] else None
            tot_val = df.iloc[i, t_col] if t_col < df.shape[1] else None

            rows.append({
                "fecha_corte": fecha,
                "anio": fecha.year,
                "mes": fecha.month,
                "empresa": empresa,
                "hoja": hoja,
                "cuenta": account,
                "mn": mn_val,
                "me": me_val,
                "total": tot_val,
            })

    return pd.DataFrame(rows)


def transform(src: Path) -> pd.DataFrame:
    """Read raw SBS S-203 XLS and return the normalized long-format DataFrame."""
    raw = src.read_bytes()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sheets = pd.read_excel(io.BytesIO(raw), engine="xlrd", sheet_name=None, header=None)

    parts = []
    fecha = None

    for sheet_key, hoja_name in _SHEET_NAMES.items():
        if sheet_key not in sheets:
            continue
        df = sheets[sheet_key]

        if fecha is None:
            fecha = _parse_date(str(df.iloc[1, 0]))

        part = _parse_sheet(df, hoja_name, fecha)
        parts.append(part)

    out = pd.concat(parts, ignore_index=True)

    # Cast numeric columns
    for col in ("mn", "me", "total"):
        out[col] = pd.to_numeric(out[col], errors="coerce")

    # Normalize empresa strings
    out["empresa"] = out["empresa"].str.strip()
    out["cuenta"] = out["cuenta"].str.strip()

    return out.reset_index(drop=True)
