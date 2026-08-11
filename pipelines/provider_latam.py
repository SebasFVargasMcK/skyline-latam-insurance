"""Pipeline: Provider LATAM — load cross-country insurance data from data provider.

Source: 55 Excel files (5 countries × 11 years, 2015–2025).
  Sheet 'Columnas': monthly cumulative figures in Miles de Dólares (thousands USD).
  Strategy: keep only December rows per file = full-year cumulative figures.

Output:
  {catalog}.insurance_provider.latam_ramos   — normalized annual flat table
  {catalog}.insurance_provider.vw_latam_all  — Genie-ready annual view

Usage:
    python -m pipelines.provider_latam
    python -m pipelines.provider_latam --dry-run
    python -m pipelines.provider_latam --source-dir "C:/path/to/Latino"
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
    r"C:\Users\Sebastian F Vargas\OneDrive - McKinsey & Company\Documents\Latino\Estados de Resultados"
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
}

# ── Period parsing ────────────────────────────────────────────────────────────

_MONTH_ES = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4,
    "may": 5, "jun": 6, "jul": 7, "ago": 8,
    "sep": 9, "oct": 10, "nov": 11, "dic": 12,
}


def _parse_period(label: str) -> tuple[datetime.date, int, int]:
    """'dic 2024' → (date(2024,12,31), month=12, quarter=4)"""
    parts = label.strip().lower().split()
    month = _MONTH_ES[parts[0]]
    year = int(parts[1])
    last_day = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, last_day), month, (month - 1) // 3 + 1


# ── Account mapping ───────────────────────────────────────────────────────────

_CUENTA_CODE: dict[str, str] = {
    # Gross / direct premiums
    "PRIMAS EMITIDAS":                    "gross_premiums",
    "PRIMA DIRECTA":                      "gross_premiums",
    "PRIMAS DIRECTAS":                    "gross_premiums",
    # Assumed / accepted reinsurance
    "PRIMAS ACEPTADAS":                   "assumed_business",
    "PRIMA ACEPTADA":                     "assumed_business",
    # Ceded
    "PRIMA CEDIDA":                       "ceded_premiums",
    # Retained / net retained
    "PRIMA RETENIDA NETA":                "net_retained_premiums",
    "PRIMA RETENIDA":                     "net_retained_premiums",
    # Earned
    "PRIMA GANADA":                       "net_earned_premiums",
    # Claims
    "COSTO TOTAL DE SINIESTROS":          "claims_incurred",
    "SINIESTRALIDAD RETENCION":           "net_claims_and_policy_obligations",
    "SINIESTRALIDAD RETENCIÓN":           "net_claims_and_policy_obligations",
    "SINIESTRALIDAD DEVENGADA RETENCION": "net_claims_and_policy_obligations",
    "SINIESTRALIDAD DEVENGADA RETENCIÓN": "net_claims_and_policy_obligations",
    "COSTO SINIESTROS NETOS":             "net_claims_and_policy_obligations",
    # Commissions / acquisition
    "COMISIONES PAGADAS A BROKERS":       "agent_commissions",
    "COMISIONES NETAS":                   "agent_commissions",
    "COMISIONES RECIBIDAS REASEGURO":     "reinsurance_commissions_received",
    "COSTO NETO DE ADQUISICION":          "net_acquisition_costs",
    "COSTO NETO DE ADQUISICIÓN":          "net_acquisition_costs",
    # Admin / operating
    "COSTO ADMINISTRATIVO NETO":          "admin_expenses",
    "GASTO ADMINISTRATIVO NETO":          "admin_expenses",
    # Financial
    "INGRESOS FINANCIEROS":               "financial_and_investment_result",
    "OTROS INGRESOS Y EGRESOS":           "financial_and_investment_result",
    "OTROS INGRESOS Y EGRESOS NETOS":     "financial_and_investment_result",
    "RESULTADO FINANCIERO":               "financial_and_investment_result",
    "RESULTADO FINANCIERO NETO":          "financial_and_investment_result",
    "INGRESOS DE INVERSIONES":            "financial_and_investment_result",
    # Profitability
    "RESULTADO TECNICO":                  "technical_profit_loss",
    "RESULTADO TÉCNICO":                  "technical_profit_loss",
    "UTILIDAD TECNICA":                   "operating_profit_loss",
    "UTILIDAD TÉCNICA":                   "operating_profit_loss",
    "UTILIDAD NETA":                      "net_income_loss",
    "RESULTADO NETO":                     "net_income_loss",
    "RESULTADO DEL EJERCICIO":            "net_income_loss",
    "UTILIDAD DEL EJERCICIO":             "net_income_loss",
    "PERDIDA DEL EJERCICIO":              "net_income_loss",
    "PÉRDIDA DEL EJERCICIO":              "net_income_loss",
}

_CUENTA_GROUP: dict[str, str] = {
    "gross_premiums":                       "Premium Flow",
    "assumed_business":                     "Premium Flow",
    "ceded_premiums":                       "Premium Flow",
    "net_retained_premiums":                "Premium Flow",
    "net_earned_premiums":                  "Premium Flow",
    "claims_incurred":                      "Claims",
    "net_claims_and_policy_obligations":    "Claims",
    "agent_commissions":                    "Acquisition",
    "reinsurance_commissions_received":     "Acquisition",
    "net_acquisition_costs":               "Acquisition",
    "admin_expenses":                       "Operating Expenses",
    "financial_and_investment_result":      "Financial",
    "technical_profit_loss":               "Profitability",
    "operating_profit_loss":               "Profitability",
    "net_income_loss":                      "Profitability",
}

_CUENTA_EN: dict[str, str] = {
    "gross_premiums":                       "Gross Written Premiums (Direct)",
    "assumed_business":                     "Assumed Reinsurance Premiums",
    "ceded_premiums":                       "Ceded Reinsurance Premiums",
    "net_retained_premiums":                "Net Retained Premiums",
    "net_earned_premiums":                  "Net Earned Premiums",
    "claims_incurred":                      "Claims Incurred (Gross)",
    "net_claims_and_policy_obligations":    "Net Claims and Policy Obligations",
    "agent_commissions":                    "Agent / Broker Commissions",
    "reinsurance_commissions_received":     "Reinsurance Commissions Received",
    "net_acquisition_costs":               "Net Acquisition Costs",
    "admin_expenses":                       "Administrative Expenses (Net)",
    "financial_and_investment_result":      "Financial and Investment Result",
    "technical_profit_loss":               "Technical Profit / Loss",
    "operating_profit_loss":               "Operating Profit / Loss",
    "net_income_loss":                      "Net Income / Loss",
}

# ── Local currency mapping ────────────────────────────────────────────────────

_MONEDA_LOCAL: dict[str, str] = {
    "AR": "ARS",
    "CL": "CLP",
    "CO": "COP",
    "EC": "USD",  # Ecuador uses USD natively
    "PE": "PEN",
}

# MXN/USD year-end December rates (Banxico) — used when building MX UNION in views
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


def _read_fx_rate(xl: pd.ExcelFile, iso: str) -> float:
    """Read December FX rate (local/USD) from Cotización sheet.
    Returns 1.0 for Ecuador (already USD) or if sheet is missing."""
    if iso == "EC":
        return 1.0
    if "Cotización" not in xl.sheet_names:
        return 1.0
    try:
        fx = xl.parse("Cotización", header=None, dtype=str)
        # Scan rows for one whose first cell contains 'dic'
        for _, row in fx.iterrows():
            cell0 = str(row.iloc[0]).lower().strip()
            if "dic" in cell0 or "diciembre" in cell0:
                raw = str(row.iloc[1]).replace(",", ".").strip()
                return float(raw)
    except Exception:
        pass
    return 1.0


# ── LoB mapping ───────────────────────────────────────────────────────────────

_LOB_MAP: dict[str, tuple[str, str]] = {
    # Total / market aggregate
    "SUMA TODOS":                                              ("Total Portfolio", "Total"),
    # Life
    "VIDA INDIVIDUAL":                                         ("Life", "Vida"),
    "VIDA GRUPO":                                              ("Life", "Vida"),
    "OTROS VIDA":                                              ("Life", "Vida"),
    # Pension
    "PENSIONES":                                               ("Pension", "Pensiones"),
    # A&H
    "ACCIDENTES PERSONALES":                                   ("A&H", "Accidentes y Enfermedades"),
    "SALUD":                                                   ("A&H", "Accidentes y Enfermedades"),
}
# Everything else defaults to P&C


def _lob(ramo: str) -> tuple[str, str]:
    return _LOB_MAP.get(ramo.strip().upper(), ("P&C", "Ramos Generales"))


# ── File processing ───────────────────────────────────────────────────────────

def _process_file(path: Path) -> pd.DataFrame:
    """Read one xlsx file and return normalized annual DataFrame."""
    try:
        xl = pd.ExcelFile(path)
    except Exception as e:
        print(f"    WARN: cannot open {path.name}: {e}")
        return pd.DataFrame()
    df = xl.parse("Columnas", dtype=str)

    # Normalize column names (strip whitespace)
    df.columns = [c.strip() for c in df.columns]

    col_pais    = "País"
    col_empresa = "Empresas"
    col_codigo  = "Código"
    col_cuenta  = "Nombre del Indice / Cuenta"
    col_periodo = "Período"
    col_anio    = "Año"
    col_lob     = "Ramos - Otra Información"
    col_valor   = "Valor"

    df = df.dropna(subset=[col_valor])

    # Parse numeric value (strip commas, convert)
    df[col_valor] = (
        df[col_valor]
        .astype(str)
        .str.replace(",", "", regex=False)
        .str.strip()
    )
    df = df[df[col_valor].str.match(r"^-?\d+(\.\d+)?$")]
    df[col_valor] = df[col_valor].astype(float)

    # Keep only December rows (full-year cumulative)
    df = df[df[col_periodo].str.lower().str.startswith("dic")]
    if df.empty:
        print(f"    WARN: no December rows in {path.name}")
        return pd.DataFrame()

    # Parse period → date
    periods = df[col_periodo].map(_parse_period)
    df["periodo"]   = periods.map(lambda t: t[0])
    df["mes"]       = periods.map(lambda t: t[1])
    df["trimestre"] = periods.map(lambda t: t[2])

    # Country
    pais_raw = df[col_pais].str.strip().str.upper().iloc[0]
    iso = _COUNTRY_ISO.get(pais_raw)
    if iso is None:
        # Try partial match
        for k, v in _COUNTRY_ISO.items():
            if k in pais_raw or pais_raw in k:
                iso = v
                break
    if iso is None:
        print(f"    WARN: unknown country '{pais_raw}' in {path.name}")
        return pd.DataFrame()

    # Account mapping
    cuenta_upper = df[col_cuenta].str.strip().str.upper()
    df["cuenta_code"]  = cuenta_upper.map(_CUENTA_CODE)
    df["cuenta_group"] = df["cuenta_code"].map(_CUENTA_GROUP)
    df["cuenta_en"]    = df["cuenta_code"].map(_CUENTA_EN)

    # LoB mapping
    lob_mapped = df[col_lob].str.strip().str.upper().map(_lob)
    df["lob_l1_en"] = lob_mapped.map(lambda t: t[0])
    df["lob_l1_es"] = lob_mapped.map(lambda t: t[1])
    df["is_total_lob"] = df[col_lob].str.strip().str.upper() == "SUMA TODOS"

    # FX rate: local currency per 1 USD (December year-end rate)
    fx_rate      = _read_fx_rate(xl, iso)
    moneda_local = _MONEDA_LOCAL.get(iso, "USD")

    valor_usd   = df[col_valor] * 1000.0          # thousands USD → actual USD
    valor_local = valor_usd * fx_rate              # actual local currency units

    # Build output DataFrame
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
        "fuente_tabla":  "provider_latam",
    })

    return out


def load_all(source_dir: str) -> pd.DataFrame:
    source = Path(source_dir)
    files = sorted(source.glob("*.xlsx"))
    if not files:
        raise FileNotFoundError(f"No xlsx files found in {source_dir}")

    print(f"  Found {len(files)} files in {source_dir}")
    frames = []
    for f in files:
        print(f"  Reading {f.name} ...", end=" ", flush=True)
        df = _process_file(f)
        if not df.empty:
            print(f"{len(df):,} rows")
            frames.append(df)
        else:
            print("skipped")

    combined = pd.concat(frames, ignore_index=True)
    print(f"\n  Total rows loaded: {len(combined):,}")
    print(f"  Countries: {sorted(combined['pais'].unique())}")
    print(f"  Years: {sorted(combined['anio'].dropna().unique())}")
    mapped = combined["cuenta_code"].notna().sum()
    unmapped = combined["cuenta_code"].isna().sum()
    print(f"  Mapped accounts: {mapped:,}  |  Unmapped (excluded from views): {unmapped:,}")
    return combined


# ── View SQL ──────────────────────────────────────────────────────────────────

_BASE_SELECT = """
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
  SUM(valor)                             AS valor_usd,
  ROUND(SUM(valor)       / 1000000.0, 4) AS valor_mm_usd,
  SUM(valor_local)                       AS valor_local,
  ROUND(SUM(valor_local) / 1000000.0, 4) AS valor_mm_local,
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
    "AR": ("Argentina",  "ARS", "Pesos Argentinos",  "SSN"),
    "CL": ("Chile",      "CLP", "Pesos Chilenos",    "CMF"),
    "CO": ("Colombia",   "COP", "Pesos Colombianos", "SFC"),
    "EC": ("Ecuador",    "USD", "Dolares",           "SCVS"),
    "PE": ("Peru",       "PEN", "Soles Peruanos",    "SBS"),
}


def _view_sql(catalog: str) -> list[tuple[str, str]]:
    tbl    = f"{catalog}.insurance_provider.latam_ramos"
    mx_tbl = f"{catalog}.insurance_mx.gold_cnsf_estado_resultados"
    stmts  = []

    # MX FX rates as inline VALUES for USD conversion
    mx_fx_values = ",\n    ".join(f"({y}, {r})" for y, r in _MX_FX_RATES.items())

    # Mexico P&L block — inline USD conversion + direct_business -> gross_premiums
    mx_block = f"""
UNION ALL
SELECT
  'MX'                                                              AS pais,
  'CNSF'                                                            AS regulador,
  g.entidad                                                         AS empresa,
  CAST(NULL AS STRING)                                              AS id_empresa,
  g.anio_corte                                                      AS anio,
  COALESCE(g.lob_group_global, g.lob_level_1_en, 'Total Portfolio') AS lob_l1_en,
  COALESCE(g.lob_level_1_es, 'Total')                               AS lob_l1_es,
  g.operacion_original                                              AS lob_source,
  g.is_total                                                        AS is_total_lob,
  CASE g.business_concept_code
    WHEN 'direct_business' THEN 'gross_premiums'
    ELSE g.business_concept_code
  END                                                               AS cuenta_code,
  g.business_concept_group                                          AS cuenta_group,
  CASE g.business_concept_code
    WHEN 'direct_business' THEN 'Gross Written Premiums (Direct)'
    ELSE g.business_concept_en
  END                                                               AS cuenta_en,
  g.descripcion_original                                            AS cuenta_source,
  'USD'                                                             AS moneda,
  'MXN'                                                             AS moneda_local,
  SUM(CAST(g.importe AS DOUBLE) / fx.rate)                          AS valor_usd,
  ROUND(SUM(CAST(g.importe AS DOUBLE) / fx.rate) / 1000000.0, 4)    AS valor_mm_usd,
  SUM(CAST(g.importe AS DOUBLE))                                    AS valor_local,
  ROUND(SUM(CAST(g.importe AS DOUBLE)) / 1000000.0, 4)              AS valor_mm_local,
  AVG(fx.rate)                                                      AS fx_rate,
  COUNT(DISTINCT g.entidad)                                         AS n_empresas,
  MIN(g.fecha_corte)                                                AS periodo_inicio,
  MAX(g.fecha_corte)                                                AS periodo_fin
FROM {mx_tbl} g
JOIN (VALUES
  {mx_fx_values}
) AS fx(anio_k, rate) ON g.anio_corte = fx.anio_k
WHERE MONTH(g.fecha_corte) = 12
  AND g.lob_level_2_en IS NULL
  AND g.operacion_original NOT LIKE '%Total%'
  AND g.business_concept_code IS NOT NULL
  AND g.importe IS NOT NULL
  AND CAST(g.importe AS DOUBLE) != 0
GROUP BY
  g.entidad, g.anio_corte,
  COALESCE(g.lob_group_global, g.lob_level_1_en, 'Total Portfolio'),
  COALESCE(g.lob_level_1_es, 'Total'),
  g.operacion_original, g.is_total,
  CASE g.business_concept_code WHEN 'direct_business' THEN 'gross_premiums' ELSE g.business_concept_code END,
  g.business_concept_group,
  CASE g.business_concept_code WHEN 'direct_business' THEN 'Gross Written Premiums (Direct)' ELSE g.business_concept_en END,
  g.descripcion_original,
  fx.rate
"""

    # Regional view — 6 countries: AR/CL/CO/EC/PE from latam_ramos + MX from CNSF
    latam_5 = _BASE_SELECT.format(table=tbl, extra_filter="")
    stmts.append(("vw_latam_all", f"""
CREATE OR REPLACE VIEW {catalog}.insurance_provider.vw_latam_all
COMMENT 'Annual insurance P&L — AR, CL, CO, EC, PE, MX (2015-2025). USD + local currency. MX: direct_business mapped to gross_premiums.'
AS
{latam_5}
{mx_block}"""))

    # Per-country views: AR/CL/CO/EC/PE from latam_ramos
    for iso, (name, currency, currency_name, reg) in _COUNTRY_META.items():
        filt = f"AND pais = '{iso}'"
        country_select = _BASE_SELECT.format(table=tbl, extra_filter=filt)
        stmts.append((f"vw_{iso.lower()}", f"""
CREATE OR REPLACE VIEW {catalog}.insurance_provider.vw_{iso.lower()}
COMMENT 'Annual insurance P&L — {name} ({iso}), {currency} ({currency_name}). Regulator: {reg}. Grain: company x year x LoB x account.'
AS {country_select}"""))

    # Mexico per-country view: reads from vw_latam_all (which includes MX via CNSF UNION)
    stmts.append(("vw_mx", f"""
CREATE OR REPLACE VIEW {catalog}.insurance_provider.vw_mx
COMMENT 'Annual insurance P&L — Mexico (MX), MXN. Regulator: CNSF. direct_business mapped to gross_premiums. USD values via Banxico year-end rates.'
AS
SELECT * FROM {catalog}.insurance_provider.vw_latam_all
WHERE pais = 'MX'"""))

    return stmts


# ── Upload ────────────────────────────────────────────────────────────────────

def run(
    source_dir: str,
    catalog: str,
    *,
    dry_run: bool = False,
) -> None:
    print("=== Provider LATAM — Load Pipeline ===\n")

    df = load_all(source_dir)

    if dry_run:
        print("\n-- DRY RUN: sample rows --")
        print(df[["pais","anio","empresa","lob_l1_en","cuenta_code","valor","moneda_local","valor_local","fx_rate"]].head(6).to_string())
        print(f"\nTarget: {catalog}.insurance_provider.latam_ramos  ({len(df):,} rows)")
        print("\n-- FX rates by file --")
        for iso in sorted(df["pais"].unique()):
            sample = df[df["pais"] == iso][["anio","fx_rate","moneda_local"]].drop_duplicates().sort_values("anio")
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

    # Set schema env var required by upload_dataframe
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

    # Create schema
    execute(
        f"CREATE SCHEMA IF NOT EXISTS {catalog}.insurance_provider",
        "schema insurance_provider",
    )

    # Upload raw data (increase batch size for speed)
    print(f"\n  Uploading {len(df):,} rows -> {catalog}.insurance_provider.latam_ramos")
    upload_dataframe(
        df,
        table="latam_ramos",
        catalog=catalog,
        schema="insurance_provider",
        mode="overwrite",
        batch_size=10_000,
    )

    # Create all views (regional + 5 country)
    for label, sql in _view_sql(catalog):
        execute(sql, f"CREATE VIEW {label}")

    # Row-count + FX rate verification
    rows = execute(
        f"""
        SELECT pais, anio,
               COUNT(*) AS filas,
               COUNT(DISTINCT empresa) AS empresas,
               COUNT(DISTINCT cuenta_code) AS cuentas,
               ROUND(AVG(fx_rate), 2) AS fx_rate_dic,
               MAX(moneda_local) AS moneda
        FROM {catalog}.insurance_provider.vw_latam_all
        WHERE is_total_lob = false
        GROUP BY pais, anio
        ORDER BY pais, anio
        """,
        "row count vw_latam_all",
    )
    print(f"\n  {'PAIS':<5} {'ANIO':>5} {'ROWS':>8} {'COS':>5} {'ACCTS':>6} {'FX_DIC':>10} {'MONEDA'}")
    print(f"  {'='*5} {'='*5} {'='*8} {'='*5} {'='*6} {'='*10} {'='*6}")
    for r in rows:
        print(f"  {r[0]:<5} {r[1]:>5} {int(r[2]):>8,} {int(r[3]):>5} {int(r[4]):>6} {float(r[5]):>10.2f} {r[6]}")

    print(f"\n  Table:  {catalog}.insurance_provider.latam_ramos")
    print(f"  Views:  vw_latam_all (6 countries incl. MX), vw_ar, vw_cl, vw_co, vw_ec, vw_pe, vw_mx")
    print("\nDone.")


def run_views_only(catalog: str) -> None:
    """Recreate all views without re-reading or re-uploading data."""
    print("=== Provider LATAM — Recreate Views Only ===\n")

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
    parser = argparse.ArgumentParser(description="Load provider LATAM data into Databricks")
    parser.add_argument(
        "--source-dir",
        default=DEFAULT_SOURCE_DIR,
        help="Directory containing the Latino/Estados de Resultados/*.xlsx files",
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
        run_views_only(catalog)
    else:
        run(args.source_dir, catalog, dry_run=args.dry_run)


if __name__ == "__main__":
    _cli()
