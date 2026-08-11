"""Transform SBS S-345 XLS files into a normalized long-format DataFrame.

S-345 layout (single sheet P008):
  row 0 : title  "Primas según Ramos por Empresa de Seguros"
  row 1 : date   "Al 31 de Diciembre del 2024"
  row 2 : unit   "( En Miles de Soles)"
  row 4 : company names in cols 1-17, col 0 = "Conceptos / Empresas", col 18 = "Total"
  row 5 : empty
  row 6+ : alternating section headers (LoB) and data rows

Section-header rows: col 0 has an uppercase label (e.g. "RAMOS GENERALES"),
  all company-value columns are NaN.
Sub-header rows within VIDA: col 0 has a mixed-case label (e.g. "Seguros de Vida"),
  again all value columns are NaN.
Data rows: col 0 has the concept (e.g. "Primas de Seguros Netas"), company cols
  contain numeric values.

Footnote rows (cols start with "Mediante" or a digit+slash) signal end of data.

Output schema (table: sbs_boletin_s345):
    fecha_corte  DATE     — December 31 of the year
    anio         INTEGER  — calendar year
    empresa      VARCHAR  — company name
    lob_l1       VARCHAR  — LoB L1 ("RAMOS GENERALES" | "RAMOS DE ACCIDENTES Y ENFERMEDADES"
                             | "RAMOS DE VIDA" | "TOTAL")
    lob_l2       VARCHAR  — LoB L2 sub-type (e.g. "Seguros de Vida"), NULL if not applicable
    cuenta       VARCHAR  — account label (e.g. "Primas de Seguros Netas")
    valor        DOUBLE   — value in thousands of PEN (soles)
"""

from __future__ import annotations

import io
import re
import warnings
from pathlib import Path
from typing import Sequence

import pandas as pd

_FOOTNOTE_RE = re.compile(r"^\s*(\d+[/)]|Mediante|A partir|\*)", re.IGNORECASE)

_MONTH_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "setiembre": 9, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def _parse_date(cell: str) -> pd.Timestamp:
    m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+del?\s+(\d{4})", str(cell), re.IGNORECASE)
    if not m:
        raise ValueError(f"Cannot parse date from: {cell!r}")
    day, month_str, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    month = _MONTH_ES.get(month_str)
    if month is None:
        raise ValueError(f"Unknown month: {month_str!r}")
    return pd.Timestamp(year, month, day)


def _is_section_header(row: pd.Series, company_cols: list[int]) -> bool:
    """True if none of the company columns contain a numeric value."""
    for c in company_cols:
        v = row.iloc[c]
        try:
            if pd.notna(v) and float(v) == float(v):
                return False
        except (TypeError, ValueError):
            pass
    return True


def _parse_file(path: Path) -> pd.DataFrame:
    raw = path.read_bytes()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = pd.read_excel(io.BytesIO(raw), engine="xlrd", sheet_name="P008", header=None)

    fecha = _parse_date(str(df.iloc[1, 0]))

    # Row 4: company headers — exclude col 0 (label), "Total*" aggregates,
    # and footnote columns like "Pacífico Seguros (2)" that appear in some years.
    header_row = df.iloc[4]
    company_cols: list[int] = []
    companies: list[str] = []
    for c in range(1, df.shape[1]):
        v = str(header_row.iloc[c]).strip()
        if not v or v.lower() == "nan":
            continue
        if v.lower().startswith("total"):
            continue
        if "(" in v:  # footnote marker, e.g. "Pacífico Seguros Generales (2)"
            continue
        company_cols.append(c)
        companies.append(v)

    lob_l1: str | None = None
    lob_l2: str | None = None
    rows: list[dict] = []

    for i in range(5, len(df)):
        label = str(df.iloc[i, 0]).strip()
        if not label or label.lower() == "nan":
            continue
        if _FOOTNOTE_RE.match(label):
            break

        if _is_section_header(df.iloc[i], company_cols):
            # Determine if this is L1 or L2:
            # L1 headers are ALL CAPS (or contain mostly upper); L2 are mixed-case
            stripped = label.strip()
            if stripped == stripped.upper() or re.match(r"RAMOS|TOTAL", stripped):
                lob_l1 = stripped.rstrip()
                lob_l2 = None
            else:
                lob_l2 = stripped
        else:
            # Data row
            for empresa, c in zip(companies, company_cols):
                raw_val = df.iloc[i, c]
                try:
                    valor = float(raw_val)
                except (TypeError, ValueError):
                    continue
                if pd.isna(valor):
                    continue
                rows.append({
                    "fecha_corte": fecha,
                    "anio": fecha.year,
                    "empresa": empresa,
                    "lob_l1": lob_l1,
                    "lob_l2": lob_l2,
                    "cuenta": label,
                    "valor": valor,
                })

    return pd.DataFrame(rows)


def transform(paths: Sequence[Path]) -> pd.DataFrame:
    """Parse a list of S-345 XLS files and return a combined long-format DataFrame."""
    parts = [_parse_file(p) for p in paths]
    if not parts:
        raise RuntimeError("No S-345 files parsed")

    out = pd.concat(parts, ignore_index=True)

    out["empresa"] = out["empresa"].str.strip()
    out["lob_l1"] = out["lob_l1"].str.strip()
    out["lob_l2"] = out["lob_l2"].where(out["lob_l2"].notna(), other=None)
    out["cuenta"] = out["cuenta"].str.strip()
    out["valor"] = pd.to_numeric(out["valor"], errors="coerce")

    return out.reset_index(drop=True)
