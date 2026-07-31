"""Transform SSN Estados Patrimoniales XLSX into a normalized long-format DataFrame.

The XLSX has 5 data sheets:
  1 Act, Pas y PN          — Balance sheet (assets, liabilities, equity)
  2 EDR                    — Income statement summary
  3 Res Técnico            — Technical result breakdown (2-row merged header)
  4 Res Técnico Seg Dir.   — Technical result of direct insurance
  5 Res Financiero         — Financial result

Each sheet has:
  row 2: section group headers (ACTIVO / PASIVO / RESULTADO TÉCNICO etc.)
  row 3: column headers  (row 4 for sheet 3 which has an extra sub-header row)
  rows 4-9: market totals and entity-type subtotals (excluded from output)
  row 10+ (or 11 for sheet 3): individual company data

This transform melts each sheet to long format and combines all sheets.

Output schema (table: ssn_estados_patrimoniales):
    fecha_corte   DATE     — quarter-end (e.g. 2026-03-31)
    anio          INTEGER
    trimestre     INTEGER  — 1–4
    nro           INTEGER  — company sequence number
    nj            VARCHAR  — entity type: A (SA), C (Coop/Mutual), E (Branch), O (Official)
    empresa       VARCHAR  — company name
    hoja          VARCHAR  — sheet slug
    cuenta        VARCHAR  — financial account name
    valor         DOUBLE   — thousands of ARS
"""

from __future__ import annotations

import io
import re
import warnings
from pathlib import Path

import pandas as pd

_QUARTER_MAP = {3: 1, 6: 2, 9: 3, 12: 4}

# Map raw sheet name → canonical slug (robust to encoding issues)
_SHEET_SLUGS = {
    "1 Act, Pas y PN":              "activo_pasivo_pn",
    "1 Act, Pas y PN":              "activo_pasivo_pn",
    "2 EDR":                        "estado_resultados",
    "3 Res Técnico":                "resultado_tecnico",
    "4 Res Técnico Seg Directos":   "resultado_tecnico_seg_directos",
    "5 Res Financiero":             "resultado_financiero",
}

def _slug_for_sheet(name: str) -> str:
    if "Act" in name and "Pas" in name:
        return "activo_pasivo_pn"
    if "EDR" in name:
        return "estado_resultados"
    if "Financiero" in name:
        return "resultado_financiero"
    if "Seg" in name and "Directos" in name:
        return "resultado_tecnico_seg_directos"
    if "cnico" in name or "Tecnico" in name.replace("é", "e"):
        return "resultado_tecnico"
    return name.lower().replace(" ", "_")


def _parse_date_from_index(sheets: dict) -> pd.Timestamp:
    """Extract quarter-end date from the INDICE sheet row 1."""
    idx_key = next((k for k in sheets if "NDICE" in k.upper() or "INDICE" in k.upper()), None)
    if idx_key:
        df = sheets[idx_key]
        val = str(df.iloc[1, 0])
        # "Balances al 31 de marzo de 2026"
        m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", val, re.IGNORECASE)
        if m:
            day, month_str, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
            month_map = {
                "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
                "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9,
                "octubre": 10, "noviembre": 11, "diciembre": 12,
            }
            month = month_map.get(month_str)
            if month:
                return pd.Timestamp(year, month, day)
    raise ValueError(f"Cannot parse date from INDICE sheet: {val!r}")


def _clean_header(raw: str) -> str:
    """Normalize a column header: strip whitespace, collapse newlines."""
    return re.sub(r"\s+", " ", str(raw).strip()).strip()


def _build_column_names(df: pd.DataFrame, header_row: int, section_row: int = 2) -> list[str]:
    """Build deduplicated column names from the header row.

    Missing/nan names fall back to the section header row value from the same column.
    Duplicate names get a _2, _3 suffix.
    """
    raw_names = [_clean_header(v) for v in df.iloc[header_row].tolist()]
    section_names = [_clean_header(v) for v in df.iloc[section_row].tolist()]

    result = []
    seen: dict[str, int] = {}

    for i, name in enumerate(raw_names):
        # Fallback to section header if this cell is blank
        if name in ("nan", ""):
            fallback = section_names[i] if i < len(section_names) else ""
            name = fallback if fallback not in ("nan", "") else f"col_{i}"

        # Deduplicate
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1

        result.append(name)

    return result


def _first_company_row(df: pd.DataFrame) -> int:
    """Return the row index of the first individual company (col 0 = integer 1)."""
    for i in range(len(df)):
        try:
            n = int(float(str(df.iloc[i, 0])))
            if n == 1:
                return i
        except (ValueError, TypeError):
            pass
    raise ValueError("Could not find first company row (N°=1)")


def _header_row(df: pd.DataFrame) -> int:
    """Return the row index containing the column headers (the row where col 0 looks like 'N°')."""
    for i in range(len(df)):
        val = str(df.iloc[i, 0]).strip()
        # The header row has N°, N, or similar in col 0
        if val.startswith("N") and len(val) <= 3:
            return i
    # Fallback: one row before the first company row
    return max(0, _first_company_row(df) - 1)


def _parse_sheet(df: pd.DataFrame, hoja: str, fecha: pd.Timestamp) -> pd.DataFrame:
    """Parse one sheet and return long-format DataFrame (individual companies only)."""
    first_row = _first_company_row(df)
    hdr_row = _header_row(df)
    section_row = max(hdr_row - 1, 2)
    col_names = _build_column_names(df, hdr_row, section_row)

    # Company data rows
    data = df.iloc[first_row:].copy()
    data.columns = col_names[:len(data.columns)]

    # Keep only rows where col 0 (N°) is a valid positive integer
    nro_col = col_names[0]
    def _is_company(val) -> bool:
        try:
            return int(float(str(val))) > 0
        except (ValueError, TypeError):
            return False

    data = data[data[nro_col].apply(_is_company)].copy()

    # ID columns
    meta_cols = col_names[:3]  # N°, NJ, Denominación
    value_cols = [c for c in col_names[3:] if c not in ("nan", "") and not c.startswith("col_")]

    # Drop value columns that are entirely NaN
    data = data.dropna(subset=value_cols, how="all")

    melted = data.melt(
        id_vars=meta_cols,
        value_vars=value_cols,
        var_name="cuenta",
        value_name="valor",
    )

    nro_name, nj_name, emp_name = meta_cols

    melted["fecha_corte"] = fecha
    melted["anio"] = fecha.year
    melted["trimestre"] = _QUARTER_MAP.get(fecha.month, 0)
    melted["hoja"] = hoja
    melted = melted.rename(columns={nro_name: "nro", nj_name: "nj", emp_name: "empresa"})

    melted["nro"] = pd.to_numeric(melted["nro"], errors="coerce").astype("Int64")
    melted["valor"] = pd.to_numeric(melted["valor"], errors="coerce")
    melted["empresa"] = melted["empresa"].astype(str).str.strip()
    melted["nj"] = melted["nj"].astype(str).str.strip()

    return melted[[
        "fecha_corte", "anio", "trimestre",
        "nro", "nj", "empresa",
        "hoja", "cuenta", "valor",
    ]]


def transform(src: Path) -> pd.DataFrame:
    """Read raw SSN Estados Patrimoniales XLSX and return long-format DataFrame."""
    raw = src.read_bytes()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None, header=None)

    fecha = _parse_date_from_index(sheets)

    parts = []
    for sheet_name, df in sheets.items():
        if "NDICE" in sheet_name.upper():
            continue
        hoja = _slug_for_sheet(sheet_name)
        try:
            part = _parse_sheet(df, hoja, fecha)
            parts.append(part)
        except Exception as exc:
            # Log but don't fail; one bad sheet shouldn't abort the whole load
            import warnings as _w
            _w.warn(f"Skipping sheet {sheet_name!r}: {exc}")

    if not parts:
        raise RuntimeError("No data sheets could be parsed from the SSN XLSX")

    out = pd.concat(parts, ignore_index=True)
    out = out.dropna(subset=["empresa", "cuenta"])
    out = out[out["empresa"].str.strip() != ""]
    return out.reset_index(drop=True)
