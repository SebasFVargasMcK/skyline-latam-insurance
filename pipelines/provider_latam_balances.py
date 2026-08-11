"""Pipeline: Provider LATAM Balance Sheets — load cross-country balance sheet data.

Source: 56 Excel files in 'Balances Generales' subfolder.
  Standard countries (AR, CL, CO, EC, PE): sheet 'Columnas', same schema as P&L.
  Mexico: sheet 'Balance General' (FECHA_CORTE, NOMBRE_CORTO, ID_NIVEL, DESCRIPCION, IMPORTE).
  Strategy: keep only December rows = year-end balance sheet snapshot.

Monetary units:
  AR / EC / PE BG : Miles de Dolares         -> x1000  = USD; local = USD x fx_rate
  CL BG           : Millones de Pesos CLP    -> x1e6   = CLP; USD   = CLP / fx_rate
  CO BG           : Millones de Pesos COP    -> x1e6   = COP; USD   = COP / fx_rate
  MX BG           : IMPORTE in MXN millions  -> x1e6   = MXN; USD   = MXN / fx_rate

FX rates:
  AR, PE : December rate from BG file Cotizacion tab.
  CL, CO : December rate from corresponding P&L file in parent directory.
  EC     : 1.0 (USD natively).
  MX     : Hardcoded annual Banxico year-end December rates.

Output:
  {catalog}.insurance_provider.latam_balances   -- normalized year-end flat table
  {catalog}.insurance_provider.vw_bg_latam_all  -- all 6 countries
  {catalog}.insurance_provider.vw_bg_{iso}      -- per-country views (7 total incl. MX)

Usage:
    python -m pipelines.provider_latam_balances
    python -m pipelines.provider_latam_balances --dry-run
    python -m pipelines.provider_latam_balances --source-dir "C:/path/to/Latino"
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_SOURCE_DIR = (
    r"C:\Users\Sebastian F Vargas\OneDrive - McKinsey & Company\Documents\Latino"
)

# ── Country mappings ──────────────────────────────────────────────────────────

_COUNTRY_ISO = {
    "ARGENTINA":      "AR",
    "CHILE":          "CL",
    "COLOMBIA SUPER": "CO",
    "COLOMBIA":       "CO",
    "ECUADOR":        "EC",
    "PERU":           "PE",
}

_COUNTRY_REGULADOR = {
    "AR": "SSN",
    "CL": "CMF",
    "CO": "SFC",
    "EC": "SCVS",
    "PE": "SBS",
    "MX": "CNSF",
}

_MONEDA_LOCAL = {
    "AR": "ARS",
    "CL": "CLP",
    "CO": "COP",
    "EC": "USD",
    "PE": "PEN",
    "MX": "MXN",
}

# MXN/USD year-end December rates (Banxico)
_MX_FX_RATES: dict[int, float] = {
    2015: 17.21,
    2016: 20.73,
    2017: 19.66,
    2018: 19.65,
    2019: 18.93,
    2020: 19.91,
    2021: 20.56,
    2022: 19.35,
    2023: 17.15,
    2024: 20.37,
    2025: 20.45,
}

# ── Period parsing ────────────────────────────────────────────────────────────

_MONTH_ES = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4,
    "may": 5, "jun": 6, "jul": 7, "ago": 8,
    "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}


def _parse_period(label: str) -> tuple[datetime.date, int, int]:
    """'dic 2024' -> (date(2024,12,31), month=12, quarter=4)"""
    parts = label.strip().lower().split()
    month = _MONTH_ES[parts[0]]
    year = int(parts[1])
    last_day = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, last_day), month, (month - 1) // 3 + 1


# ── Balance sheet account mapping ─────────────────────────────────────────────

_BS_CUENTA_CODE: dict[str, str] = {
    # Total Assets
    "ACTIVO":                                                   "total_assets",
    "TOTAL ACTIVO":                                             "total_assets",
    "ACTIVO -NIF-":                                             "total_assets",
    "Total Activo":                                             "total_assets",
    # Investments (financial)
    "INVERSIONES":                                              "total_investments",
    "TOTAL INVERSIONES FINANCIERAS":                            "total_investments",
    "INVERSIONES Y OPERACIONES CON DERIVADOS -NIF-":            "total_investments",
    "Inversiones Financiera (neto)":                            "total_investments",
    "Inversiones":                                              "total_investments",
    "INVERSIONES EN VALORES (NETO)":                            "financial_investments",
    "TOTAL INVERSIONES INMOBILIARIAS":                          "real_estate_investments",
    # Cash and equivalents
    "DISPONIBILIDADES":                                         "cash_and_equivalents",
    "EFECTIVO Y EFECTIVO EQUIVALENTE":                          "cash_and_equivalents",
    "EFECTIVO -NIF-":                                           "cash_and_equivalents",
    "CAJA Y BANCOS":                                            "cash_and_equivalents",
    "Disponibilidad":                                           "cash_and_equivalents",
    # Insurance receivables
    "CREDITOS":                                                 "insurance_receivables",
    "TOTAL CUENTAS DE SEGUROS":                                 "insurance_receivables",
    "CUENTAS POR COBRAR -NIF-":                                 "insurance_receivables",
    "DEUDORES POR PRIMAS":                                      "insurance_receivables",
    "Deudores":                                                 "insurance_receivables",
    # Reinsurance recoverables
    "PARTICIPACION DEL REASEGURO EN LAS RESERVAS TECNICAS":     "reinsurance_recoverables",
    "PARTICIPACIÓN DEL REASEGURO EN LAS RESERVAS TÉCNICAS":     "reinsurance_recoverables",
    "RESERVAS TECNICAS PARTE REASEGURADORES -NIF-":             "reinsurance_recoverables",
    "RESERVAS TÉCNICAS PARTE REASEGURADORES -NIF-":             "reinsurance_recoverables",
    "DEUDORES POR REASEGUROS Y COASEGUROS":                     "reinsurance_recoverables",
    "Activo por reservas tecnicas a cargo de reaseguradores":   "reinsurance_recoverables",
    "Activo por reservas tecnicas a cargo de reaseguradores.":  "reinsurance_recoverables",
    "Activo por reservas técnicas a cargo de reaseguradores":   "reinsurance_recoverables",
    "Activo por reservas técnicas a cargo de reaseguradores.":  "reinsurance_recoverables",
    "Reaseguradores y Reafianzadores":                          "reinsurance_recoverables",
    # Fixed assets / other
    "INMUEBLES":                                                "real_estate_assets",
    "BIENES DE USO":                                            "fixed_assets",
    "OTROS ACTIVOS":                                            "other_assets",
    "OTROS ACTIVOS -NIF-":                                      "other_assets",
    "Otros Activos":                                            "other_assets",
    # Total Liabilities
    "PASIVO":                                                   "total_liabilities",
    "TOTAL PASIVO":                                             "total_liabilities",
    "PASIVO -NIF-":                                             "total_liabilities",
    "PASIVOS":                                                  "total_liabilities",
    "Total Pasivo":                                             "total_liabilities",
    # Technical Reserves (top-level)
    "COMPROMISOS TECNICOS":                                     "technical_reserves",
    "RESERVAS TECNICAS":                                        "technical_reserves",
    "RESERVAS TÉCNICAS":                                        "technical_reserves",
    "RESERVAS TECNICAS -NIF-":                                  "technical_reserves",
    "RESERVAS TÉCNICAS -NIF-":                                  "technical_reserves",
    "Reservas Tecnicas":                                        "technical_reserves",
    "Reservas Técnicas":                                        "technical_reserves",
    # Unearned premium reserve
    "RESERVAS TECNICAS POR PRIMAS":                             "unearned_premium_reserve",
    "RESERVAS TÉCNICAS POR PRIMAS":                             "unearned_premium_reserve",
    "RESERVA DE RIESGOS EN CURSO":                              "unearned_premium_reserve",
    "DE RIESGOS EN CURSO -NIF-":                                "unearned_premium_reserve",
    "DE RIESGOS EN CURSO":                                      "unearned_premium_reserve",
    "Reserva de Riesgos en Curso":                              "unearned_premium_reserve",
    "Reserva de riesgos en curso":                              "unearned_premium_reserve",
    # Claims reserve
    "RESERVAS TECNICAS POR SINIESTROS":                         "claims_reserve",
    "RESERVAS TÉCNICAS POR SINIESTROS":                         "claims_reserve",
    "RESERVAS TECNICAS POR SINIESTROS.":                        "claims_reserve",
    "RESERVAS TÉCNICAS POR SINIESTROS.":                        "claims_reserve",
    "RESERVA DE SINIESTROS":                                    "claims_reserve",
    "RESERVAS PARA OBLIGACIONES DE SINIESTROS PENDIENTES":      "claims_reserve",
    "RESERVA PARA SINIESTROS AVISADOS -NIF-":                   "claims_reserve",
    "RESERVA PARA SINIESTROS NO AVISADOS -NIF-":                "claims_reserve",
    "Reserva para Obligaciones Pendientes de Cumplir":          "claims_reserve",
    # Life / mathematical reserve
    "RESERVA MATEMATICA":                                       "life_reserve",
    "RESERVA MATEMÁTICA":                                       "life_reserve",
    "RESERVA MATEMATICA -NIF-":                                 "life_reserve",
    "RESERVA MATEMÁTICA -NIF-":                                 "life_reserve",
    "RESERVAS MATEMATICAS":                                     "life_reserve",
    "RESERVAS MATEMÁTICAS":                                     "life_reserve",
    # Shareholders' equity (top-level)
    "PATRIMONIO NETO":                                          "shareholders_equity",
    "TOTAL PATRIMONIO":                                         "shareholders_equity",
    "PATRIMONIO -NIF-":                                         "shareholders_equity",
    "P A T R I M O N I O":                                      "shareholders_equity",
    "PATRIMONIO":                                               "shareholders_equity",
    "Total Capital":                                            "shareholders_equity",
    # Capital / paid-in
    "CAPITAL":                                                  "paid_in_capital",
    "CAPITAL PAGADO":                                           "paid_in_capital",
    "CAPITAL SOCIAL -NIF-":                                     "paid_in_capital",
    "CAPITAL SOCIAL":                                           "paid_in_capital",
    "Capital Contribuido":                                      "paid_in_capital",
    # Retained earnings / results
    "RESULTADOS ACUMULADOS":                                    "retained_earnings",
    "GANANCIAS O PERDIDAS -NIF-":                               "retained_earnings",
    "GANANCIAS O PÉRDIDAS -NIF-":                               "retained_earnings",
    "RESULTADOS":                                               "retained_earnings",
    "Capital Ganado":                                           "retained_earnings",
    # Current-year net income (appears in equity section of balance sheet)
    "DEL EJERCICIO":                                            "net_income_loss",
    "RESULTADO DEL EJERCICIO":                                  "net_income_loss",
    "UTILIDAD DEL EJERCICIO":                                   "net_income_loss",
    "RESULTADO NETO DEL EJERCICIO":                             "net_income_loss",
    "UTILIDAD NETA DEL EJERCICIO":                              "net_income_loss",
    "PERDIDA DEL EJERCICIO":                                    "net_income_loss",
    "PÉRDIDA DEL EJERCICIO":                                    "net_income_loss",
    "RESULTADO NETO":                                           "net_income_loss",
    "UTILIDAD NETA":                                            "net_income_loss",
}

# Mexico maps by ID_NIVEL (integer) to avoid ambiguous names on both sides of BS
_MX_ID_NIVEL_CODE: dict[int, str] = {
    100000000: "total_assets",
    110000000: "total_investments",
    130000000: "cash_and_equivalents",
    140000000: "insurance_receivables",
    150000000: "reinsurance_recoverables",
    170000000: "other_assets",
    200000000: "total_liabilities",
    210000000: "technical_reserves",
    211000000: "unearned_premium_reserve",
    212000000: "claims_reserve",
    300000000: "shareholders_equity",
    310000000: "paid_in_capital",
    320000000: "retained_earnings",
}

_BS_CUENTA_GROUP: dict[str, str] = {
    "total_assets":              "Assets",
    "total_investments":         "Assets",
    "financial_investments":     "Assets",
    "real_estate_investments":   "Assets",
    "real_estate_assets":        "Assets",
    "fixed_assets":              "Assets",
    "cash_and_equivalents":      "Assets",
    "insurance_receivables":     "Assets",
    "reinsurance_recoverables":  "Assets",
    "other_assets":              "Assets",
    "total_liabilities":         "Liabilities",
    "technical_reserves":        "Liabilities",
    "unearned_premium_reserve":  "Liabilities",
    "claims_reserve":            "Liabilities",
    "life_reserve":              "Liabilities",
    "shareholders_equity":       "Equity",
    "paid_in_capital":           "Equity",
    "retained_earnings":         "Equity",
    "net_income_loss":           "Profitability",
}

_BS_CUENTA_EN: dict[str, str] = {
    "total_assets":              "Total Assets",
    "total_investments":         "Total Investments",
    "financial_investments":     "Financial Investments",
    "real_estate_investments":   "Real Estate Investments",
    "real_estate_assets":        "Real Estate Assets",
    "fixed_assets":              "Fixed Assets and Equipment",
    "cash_and_equivalents":      "Cash and Cash Equivalents",
    "insurance_receivables":     "Insurance Receivables",
    "reinsurance_recoverables":  "Reinsurance Recoverables",
    "other_assets":              "Other Assets",
    "total_liabilities":         "Total Liabilities",
    "technical_reserves":        "Total Technical Reserves",
    "unearned_premium_reserve":  "Unearned Premium Reserve",
    "claims_reserve":            "Claims Reserve",
    "life_reserve":              "Life / Mathematical Reserve",
    "shareholders_equity":       "Shareholders Equity",
    "paid_in_capital":           "Paid-in Capital",
    "retained_earnings":         "Retained Earnings",
    "net_income_loss":           "Net Income / Loss",
}

# ── LoB mapping (same as P&L) ─────────────────────────────────────────────────

_LOB_MAP: dict[str, tuple[str, str]] = {
    "SUMA TODOS":          ("Total Portfolio", "Total"),
    "VIDA INDIVIDUAL":     ("Life", "Vida"),
    "VIDA GRUPO":          ("Life", "Vida"),
    "OTROS VIDA":          ("Life", "Vida"),
    "PENSIONES":           ("Pension", "Pensiones"),
    "ACCIDENTES PERSONALES": ("A&H", "Accidentes y Enfermedades"),
    "SALUD":               ("A&H", "Accidentes y Enfermedades"),
}


def _lob(ramo: str) -> tuple[str, str]:
    return _LOB_MAP.get(ramo.strip().upper(), ("P&C", "Ramos Generales"))


# ── FX helpers ────────────────────────────────────────────────────────────────

def _read_fx_from_xl(xl: pd.ExcelFile) -> float:
    """Read December FX rate from Cotizacion sheet."""
    if "Cotización" not in xl.sheet_names:
        return 1.0
    try:
        fx = xl.parse("Cotización", header=None, dtype=str)
        for _, row in fx.iterrows():
            cell0 = str(row.iloc[0]).lower().strip()
            if "dic" in cell0 or "diciembre" in cell0:
                raw = str(row.iloc[1]).replace(",", ".").strip()
                return float(raw)
    except Exception:
        pass
    return 1.0


def _read_bg_fx_rate(xl: pd.ExcelFile, iso: str, bg_path: Path, pl_dir: Path) -> float:
    """Read December FX rate for a BG file.
    AR, PE: BG file has Cotizacion tab.
    CL, CO: read from corresponding P&L file in pl_dir.
    EC: 1.0 (USD).
    MX: handled separately via _MX_FX_RATES.
    """
    if iso in ("EC", "MX"):
        return 1.0
    if iso in ("AR", "PE"):
        rate = _read_fx_from_xl(xl)
        return rate if rate != 1.0 else 1.0
    # CL and CO: read from P&L sibling file
    match = re.search(r"(\d+)\s*$", bg_path.stem)
    if not match:
        return 1.0
    year_suffix = match.group(1)
    candidates = {
        "CL": [f"Chile {year_suffix}.xlsx"],
        "CO": [f"Colombia {year_suffix}.xlsx", f"Colombia SUPER {year_suffix}.xlsx"],
    }.get(iso, [])
    for name in candidates:
        pl_path = pl_dir / name
        if pl_path.exists():
            try:
                xl_pl = pd.ExcelFile(pl_path)
                rate = _read_fx_from_xl(xl_pl)
                if rate != 1.0:
                    return rate
            except Exception:
                pass
    return 1.0


# ── Standard file processor (AR, CL, CO, EC, PE) ─────────────────────────────

def _process_standard_bg_file(path: Path, pl_dir: Path) -> pd.DataFrame:
    """Read one standard BG xlsx and return normalized year-end DataFrame."""
    try:
        xl = pd.ExcelFile(path)
    except Exception as e:
        print(f"    WARN: cannot open {path.name}: {e}")
        return pd.DataFrame()

    if "Columnas" not in xl.sheet_names:
        print(f"    WARN: no 'Columnas' sheet in {path.name}")
        return pd.DataFrame()

    df = xl.parse("Columnas", dtype=str)
    df.columns = [c.strip() for c in df.columns]

    # Accept both accented and non-accented column name variants
    col_pais    = next((c for c in df.columns if c.lower().startswith("pa")), "País")
    col_empresa = next((c for c in df.columns if "empresa" in c.lower()), "Empresas")
    col_codigo  = next((c for c in df.columns if "digo" in c.lower()), "Código")
    col_cuenta  = next((c for c in df.columns if "ndice" in c.lower() or "ndex" in c.lower() or "nombre" in c.lower()), "Nombre del Indice / Cuenta")
    col_periodo = next((c for c in df.columns if "odo" in c.lower() or "period" in c.lower()), "Período")
    col_anio    = next((c for c in df.columns if "o" == c.strip().lower() or "año" == c.strip().lower() or c.strip().lower() == "ano"), "Año")
    col_lob     = next((c for c in df.columns if "ramo" in c.lower()), "Ramos - Otra Información")
    col_valor   = next((c for c in df.columns if "valor" in c.lower()), "Valor")
    col_moneda  = next((c for c in df.columns if "moneda" in c.lower()), "Moneda")

    df = df.dropna(subset=[col_valor])
    df[col_valor] = (
        df[col_valor].astype(str)
        .str.replace(",", "", regex=False)
        .str.strip()
    )
    df = df[df[col_valor].str.match(r"^-?\d+(\.\d+)?$")]
    df[col_valor] = df[col_valor].astype(float)

    # Keep only December (year-end snapshot)
    df = df[df[col_periodo].str.lower().str.startswith("dic")]
    if df.empty:
        print(f"    WARN: no December rows in {path.name}")
        return pd.DataFrame()

    # Determine country ISO
    pais_raw = df[col_pais].str.strip().str.upper().iloc[0]
    iso = _COUNTRY_ISO.get(pais_raw)
    if iso is None:
        for k, v in _COUNTRY_ISO.items():
            if k in pais_raw or pais_raw in k:
                iso = v
                break
    if iso is None:
        print(f"    WARN: unknown country '{pais_raw}' in {path.name}")
        return pd.DataFrame()

    # Parse period
    periods = df[col_periodo].map(_parse_period)
    df["periodo"]   = periods.map(lambda t: t[0])
    df["mes"]       = periods.map(lambda t: t[1])
    df["trimestre"] = periods.map(lambda t: t[2])

    # FX rate
    fx_rate      = _read_bg_fx_rate(xl, iso, path, pl_dir)
    moneda_local = _MONEDA_LOCAL.get(iso, "USD")

    # Detect monetary unit from Moneda column
    moneda_sample = ""
    if col_moneda in df.columns:
        moneda_vals = df[col_moneda].dropna()
        if not moneda_vals.empty:
            moneda_sample = str(moneda_vals.iloc[0]).strip().lower()

    if "millones" in moneda_sample:
        # Local currency millions (CL, CO)
        valor_local = df[col_valor] * 1_000_000.0
        valor_usd   = valor_local / fx_rate if fx_rate != 0 else valor_local
    else:
        # USD thousands (AR, EC, PE) — default
        valor_usd   = df[col_valor] * 1_000.0
        valor_local = valor_usd * fx_rate

    # Account mapping
    cuenta_upper = df[col_cuenta].str.strip()
    df["cuenta_code"]  = cuenta_upper.map(_BS_CUENTA_CODE)
    df["cuenta_group"] = df["cuenta_code"].map(_BS_CUENTA_GROUP)
    df["cuenta_en"]    = df["cuenta_code"].map(_BS_CUENTA_EN)

    # LoB mapping
    lob_mapped = df[col_lob].str.strip().str.upper().map(_lob)
    df["lob_l1_en"] = lob_mapped.map(lambda t: t[0])
    df["lob_l1_es"] = lob_mapped.map(lambda t: t[1])
    df["is_total_lob"] = df[col_lob].str.strip().str.upper() == "SUMA TODOS"

    out = pd.DataFrame({
        "pais":          iso,
        "regulador":     _COUNTRY_REGULADOR.get(iso, ""),
        "empresa":       df[col_empresa].str.strip(),
        "id_empresa":    df[col_codigo].str.strip(),
        "periodo":       df["periodo"],
        "anio":          df[col_anio].str.strip().str.extract(r"(\d{4})")[0].astype("Int64"),
        "mes":           df["mes"].astype("Int64"),
        "trimestre":     df["trimestre"].astype("Int64"),
        "frecuencia":    "anual",
        "lob_source":    df[col_lob].str.strip(),
        "lob_l1_en":     df["lob_l1_en"],
        "lob_l1_es":     df["lob_l1_es"],
        "is_total_lob":  df["is_total_lob"],
        "cuenta_source": df[col_cuenta].str.strip(),
        "cuenta_code":   df["cuenta_code"],
        "cuenta_group":  df["cuenta_group"],
        "cuenta_en":     df["cuenta_en"],
        "valor":         valor_usd,
        "moneda":        "USD",
        "fx_rate":       fx_rate,
        "moneda_local":  moneda_local,
        "valor_local":   valor_local,
        "fuente_tabla":  "provider_latam_balances",
    })
    return out


# ── Mexico BG processor ───────────────────────────────────────────────────────

def _process_mx_bg_file(path: Path) -> pd.DataFrame:
    """Read Mexico SIO balance sheet file (different schema)."""
    try:
        xl = pd.ExcelFile(path)
    except Exception as e:
        print(f"    WARN: cannot open {path.name}: {e}")
        return pd.DataFrame()

    sheet = "Balance General"
    if sheet not in xl.sheet_names:
        print(f"    WARN: no 'Balance General' sheet in {path.name}")
        return pd.DataFrame()

    df = xl.parse(sheet, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    required = ["FECHA_CORTE", "NOMBRE_CORTO", "ID_NIVEL", "DESCRIPCION", "IMPORTE"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"    WARN: missing columns {missing} in {path.name}")
        return pd.DataFrame()

    # Parse IMPORTE
    df["IMPORTE"] = (
        df["IMPORTE"].astype(str)
        .str.replace(",", "", regex=False)
        .str.strip()
    )
    df = df[df["IMPORTE"].str.match(r"^-?\d+(\.\d+)?$")]
    df["IMPORTE"] = df["IMPORTE"].astype(float)

    # Parse date
    df["FECHA_CORTE"] = pd.to_datetime(df["FECHA_CORTE"], errors="coerce")
    df = df.dropna(subset=["FECHA_CORTE"])

    # Keep December only (year-end)
    df = df[df["FECHA_CORTE"].dt.month == 12]
    if df.empty:
        print(f"    WARN: no December rows in {path.name}")
        return pd.DataFrame()

    df["anio"] = df["FECHA_CORTE"].dt.year.astype("Int64")
    df["mes"]  = df["FECHA_CORTE"].dt.month.astype("Int64")
    df["trimestre"] = pd.Series(4, index=df.index, dtype="Int64")
    df["periodo"] = df["FECHA_CORTE"].dt.date

    # FX rate per year
    df["fx_rate"] = df["anio"].map(
        lambda y: _MX_FX_RATES.get(int(y), _MX_FX_RATES.get(max(_MX_FX_RATES)))
    )

    # Parse ID_NIVEL as integer for canonical mapping
    df["ID_NIVEL_int"] = pd.to_numeric(df["ID_NIVEL"], errors="coerce")

    # Account mapping via ID_NIVEL
    df["cuenta_code"]  = df["ID_NIVEL_int"].map(_MX_ID_NIVEL_CODE)
    df["cuenta_group"] = df["cuenta_code"].map(_BS_CUENTA_GROUP)
    df["cuenta_en"]    = df["cuenta_code"].map(_BS_CUENTA_EN)

    # Monetary conversion: IMPORTE is in MXN millions
    df["valor_local"] = df["IMPORTE"] * 1_000_000.0
    df["valor"]       = df["valor_local"] / df["fx_rate"].replace(0, float("nan"))

    out = pd.DataFrame({
        "pais":          "MX",
        "regulador":     "CNSF",
        "empresa":       df["NOMBRE_CORTO"].str.strip(),
        "id_empresa":    df["NOMBRE_CORTO"].str.strip(),
        "periodo":       df["periodo"],
        "anio":          df["anio"],
        "mes":           df["mes"],
        "trimestre":     df["trimestre"],
        "frecuencia":    "anual",
        "lob_source":    "TOTAL",
        "lob_l1_en":     "Total Portfolio",
        "lob_l1_es":     "Total",
        "is_total_lob":  True,
        "cuenta_source": df["DESCRIPCION"].str.strip(),
        "cuenta_code":   df["cuenta_code"],
        "cuenta_group":  df["cuenta_group"],
        "cuenta_en":     df["cuenta_en"],
        "valor":         df["valor"],
        "moneda":        "USD",
        "fx_rate":       df["fx_rate"],
        "moneda_local":  "MXN",
        "valor_local":   df["valor_local"],
        "fuente_tabla":  "provider_latam_balances",
    })
    return out


# ── Main loader ───────────────────────────────────────────────────────────────

def load_all(source_dir: str) -> pd.DataFrame:
    bg_dir  = Path(source_dir) / "Balances Generales"
    # P&L files may be in an "Estados de Resultados" subfolder (or at source root)
    pl_dir  = Path(source_dir) / "Estados de Resultados"
    if not pl_dir.exists():
        pl_dir = Path(source_dir)

    if not bg_dir.exists():
        raise FileNotFoundError(f"Balances Generales directory not found: {bg_dir}")

    all_files = sorted(bg_dir.glob("*.xlsx"))
    mx_files  = [f for f in all_files if "SIO" in f.name or "balance_general" in f.name.lower()]
    std_files = [f for f in all_files if f not in mx_files]

    print(f"  Found {len(std_files)} standard BG files + {len(mx_files)} Mexico file(s)")

    frames = []

    for f in std_files:
        print(f"  Reading {f.name} ...", end=" ", flush=True)
        df = _process_standard_bg_file(f, pl_dir)
        if not df.empty:
            print(f"{len(df):,} rows")
            frames.append(df)
        else:
            print("skipped")

    for f in mx_files:
        print(f"  Reading {f.name} ...", end=" ", flush=True)
        df = _process_mx_bg_file(f)
        if not df.empty:
            print(f"{len(df):,} rows")
            frames.append(df)
        else:
            print("skipped")

    if not frames:
        raise RuntimeError("No data loaded from any file")

    combined = pd.concat(frames, ignore_index=True)
    print(f"\n  Total rows loaded: {len(combined):,}")
    print(f"  Countries: {sorted(combined['pais'].unique())}")
    print(f"  Years: {sorted(combined['anio'].dropna().unique())}")
    mapped   = combined["cuenta_code"].notna().sum()
    unmapped = combined["cuenta_code"].isna().sum()
    print(f"  Mapped accounts: {mapped:,}  |  Unmapped (excluded from views): {unmapped:,}")

    if unmapped > 0:
        top_unmapped = (
            combined[combined["cuenta_code"].isna()]["cuenta_source"]
            .value_counts().head(20)
        )
        print("\n  Top unmapped accounts (for future mapping):")
        for name, cnt in top_unmapped.items():
            print(f"    {cnt:>6,}  {name}")

    return combined


# ── View SQL ──────────────────────────────────────────────────────────────────

_BG_BASE_SELECT = """
SELECT
  pais,
  regulador,
  empresa,
  id_empresa,
  anio,
  lob_l1_en,
  lob_l1_es,
  lob_source,
  is_total_lob,
  cuenta_code,
  cuenta_group,
  cuenta_en,
  cuenta_source,
  moneda,
  moneda_local,
  AVG(valor)                             AS valor_usd,
  ROUND(AVG(valor)       / 1000000.0, 4) AS valor_mm_usd,
  AVG(valor_local)                       AS valor_local,
  ROUND(AVG(valor_local) / 1000000.0, 4) AS valor_mm_local,
  AVG(fx_rate)                           AS fx_rate,
  COUNT(DISTINCT empresa)                AS n_empresas,
  MIN(periodo)                           AS periodo_inicio,
  MAX(periodo)                           AS periodo_fin
FROM {table}
WHERE cuenta_code IS NOT NULL
  AND valor IS NOT NULL
  AND valor != 0
  {extra_filter}
GROUP BY
  pais, regulador, empresa, id_empresa, anio,
  lob_l1_en, lob_l1_es, lob_source, is_total_lob,
  cuenta_code, cuenta_group, cuenta_en, cuenta_source,
  moneda, moneda_local
"""

_COUNTRY_META = {
    "AR": ("Argentina", "ARS", "Pesos Argentinos",      "SSN"),
    "CL": ("Chile",     "CLP", "Pesos Chilenos",        "CMF"),
    "CO": ("Colombia",  "COP", "Pesos Colombianos",     "SFC"),
    "EC": ("Ecuador",   "USD", "Dolares",               "SCVS"),
    "PE": ("Peru",      "PEN", "Soles Peruanos",        "SBS"),
    "MX": ("Mexico",    "MXN", "Pesos Mexicanos",       "CNSF"),
}


def _view_sql(catalog: str) -> list[tuple[str, str]]:
    tbl = f"{catalog}.insurance_provider.latam_balances"
    stmts = []

    # Regional view — all 6 countries
    regional = _BG_BASE_SELECT.format(table=tbl, extra_filter="")
    stmts.append(("vw_bg_latam_all", f"""
CREATE OR REPLACE VIEW {catalog}.insurance_provider.vw_bg_latam_all
COMMENT 'Year-end balance sheet — AR, CL, CO, EC, PE, MX (2015-2025). USD + local currency. Grain: country x company x year x account.'
AS {regional}"""))

    for iso, (name, currency, currency_name, reg) in _COUNTRY_META.items():
        filt = f"AND pais = '{iso}'"
        country_select = _BG_BASE_SELECT.format(table=tbl, extra_filter=filt)
        stmts.append((f"vw_bg_{iso.lower()}", f"""
CREATE OR REPLACE VIEW {catalog}.insurance_provider.vw_bg_{iso.lower()}
COMMENT 'Year-end balance sheet — {name} ({iso}), {currency} ({currency_name}). Regulator: {reg}. Grain: company x year x account.'
AS {country_select}"""))

    return stmts


# ── Upload ────────────────────────────────────────────────────────────────────

def run(
    source_dir: str,
    catalog: str,
    *,
    dry_run: bool = False,
) -> None:
    print("=== Provider LATAM Balance Sheets — Load Pipeline ===\n")

    df = load_all(source_dir)

    if dry_run:
        print("\n-- DRY RUN: sample rows --")
        cols = ["pais", "anio", "empresa", "cuenta_code", "cuenta_group", "valor", "moneda_local", "valor_local", "fx_rate"]
        print(df[cols].head(8).to_string())
        print(f"\nTarget: {catalog}.insurance_provider.latam_balances  ({len(df):,} rows)")
        print("\n-- FX rates by country and year --")
        for iso in sorted(df["pais"].unique()):
            sample = (
                df[df["pais"] == iso][["anio", "fx_rate", "moneda_local"]]
                .drop_duplicates().sort_values("anio")
            )
            print(f"\n  {iso}:")
            print(sample.to_string(index=False))
        return

    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    from connectors.shared.databricks import upload_dataframe, _client, _warehouse_id
    from databricks.sdk.service.sql import StatementState

    os.environ["DATABRICKS_SCHEMA"] = "insurance_provider"

    w = _client()
    warehouse_id = _warehouse_id(w)

    def execute(sql: str, label: str) -> list:
        r = w.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement=sql,
            wait_timeout="50s",
        )
        for _ in range(12):
            if r.status.state in (
                StatementState.SUCCEEDED, StatementState.FAILED,
                StatementState.CANCELED, StatementState.CLOSED,
            ):
                break
            time.sleep(5)
            r = w.statement_execution.get_statement(r.statement_id)
        if r.status.state != StatementState.SUCCEEDED:
            raise RuntimeError(f"{label}: {r.status.error}")
        print(f"  OK  {label}")
        return r.result.data_array or []

    execute(
        f"CREATE SCHEMA IF NOT EXISTS {catalog}.insurance_provider",
        "schema insurance_provider",
    )

    print(f"\n  Uploading {len(df):,} rows -> {catalog}.insurance_provider.latam_balances")
    upload_dataframe(
        df,
        table="latam_balances",
        catalog=catalog,
        schema="insurance_provider",
        mode="overwrite",
        batch_size=10_000,
    )

    for label, sql in _view_sql(catalog):
        execute(sql, f"CREATE VIEW {label}")

    # Verification
    rows = execute(
        f"""
        SELECT pais, anio,
               COUNT(*) AS filas,
               COUNT(DISTINCT empresa) AS empresas,
               COUNT(DISTINCT cuenta_code) AS cuentas,
               ROUND(AVG(fx_rate), 2) AS fx_rate_dic,
               MAX(moneda_local) AS moneda
        FROM {catalog}.insurance_provider.vw_bg_latam_all
        WHERE cuenta_code = 'total_assets'
        GROUP BY pais, anio
        ORDER BY pais, anio
        """,
        "row count vw_bg_latam_all",
    )
    print(f"\n  {'PAIS':<5} {'ANIO':>5} {'ROWS':>8} {'COS':>5} {'ACCTS':>6} {'FX_DIC':>10} {'MONEDA'}")
    print(f"  {'='*5} {'='*5} {'='*8} {'='*5} {'='*6} {'='*10} {'='*6}")
    for r in rows:
        print(f"  {r[0]:<5} {r[1]:>5} {int(r[2]):>8,} {int(r[3]):>5} {int(r[4]):>6} {float(r[5]):>10.2f} {r[6]}")

    print(f"\n  Table:  {catalog}.insurance_provider.latam_balances")
    print(f"  Views:  vw_bg_latam_all, vw_bg_ar, vw_bg_cl, vw_bg_co, vw_bg_ec, vw_bg_pe, vw_bg_mx")
    print("\nDone.")


def _run_views_only(catalog: str) -> None:
    """Recreate all balance sheet views without re-reading or re-uploading data."""
    print("=== Provider LATAM Balance Sheets — Recreate Views Only ===\n")
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    from connectors.shared.databricks import _client, _warehouse_id
    from databricks.sdk.service.sql import StatementState

    os.environ["DATABRICKS_SCHEMA"] = "insurance_provider"
    w = _client()
    warehouse_id = _warehouse_id(w)

    def execute(sql: str, label: str) -> None:
        r = w.statement_execution.execute_statement(
            warehouse_id=warehouse_id, statement=sql, wait_timeout="50s",
        )
        for _ in range(20):
            if r.status.state in (
                StatementState.SUCCEEDED, StatementState.FAILED,
                StatementState.CANCELED, StatementState.CLOSED,
            ):
                break
            time.sleep(5)
            r = w.statement_execution.get_statement(r.statement_id)
        if r.status.state != StatementState.SUCCEEDED:
            raise RuntimeError(f"{label}: {r.status.error}")
        print(f"  OK  {label}")

    for label, sql in _view_sql(catalog):
        execute(sql, f"CREATE VIEW {label}")
    print("\nDone.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Load provider LATAM balance sheet data into Databricks"
    )
    parser.add_argument(
        "--source-dir",
        default=DEFAULT_SOURCE_DIR,
        help="Directory containing Latino/ (Balances Generales/ subfolder expected)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print without uploading")
    parser.add_argument("--views-only", action="store_true", help="Recreate views without re-uploading data")
    args = parser.parse_args()

    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    catalog = os.environ.get("DATABRICKS_CATALOG", "prod_us_prismlatam_c30670d")

    if args.views_only:
        _run_views_only(catalog)
    else:
        run(args.source_dir, catalog, dry_run=args.dry_run)


if __name__ == "__main__":
    _cli()
