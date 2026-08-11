"""Pipeline: build insurance_provider.vw_er_completo_all from er_completo tables.

Reads the 5 er_completo Delta tables (AR/CL/CO/EC/PE) already in Databricks,
applies account + LoB mapping in SQL, and creates a view that matches the
schema of vw_latam_all.  Mexico data is still sourced from the CNSF gold table.

Output view:
  {catalog}.insurance_provider.vw_er_completo_all

The motor's _pull_pl switches to this view; _pull_pl_lob stays on vw_latam_all
(which has LoB breakdown for AR from the older latam_ramos source).

Usage:
    python -m pipelines.er_completo_provider              # create view
    python -m pipelines.er_completo_provider --dry-run    # print SQL only
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Account mapping ────────────────────────────────────────────────────────────
# Keyed on UPPER(TRIM(descripcion)) as it appears in the er_completo tables.
# Only non-overlapping codes: PRIMAS EMITIDAS (= direct + assumed) is the GWP
# total; PRIMA DIRECTA and PRIMA ACEPTADA are sub-components and are left unmapped
# to avoid double-counting.

_CUENTA_CODE: dict[str, str] = {
    # Premiums
    "PRIMAS EMITIDAS":                       "gross_premiums",
    "PRIMA GANADA":                          "net_earned_premiums",
    "PRIMA CEDIDA":                          "ceded_premiums",
    "PRIMA RETENIDA NETA":                   "net_retained_premiums",
    "PRIMA RETENIDA":                        "net_retained_premiums",
    # Claims
    "COSTO TOTAL DE SINIESTROS INCURRIDOS":  "claims_incurred",
    "COSTO TOTAL DE SINIESTROS INCU":        "claims_incurred",   # truncated in some CL/PE files
    "COSTO TOTAL SINIESTROS NETOS":          "claims_incurred",
    "SINIESTRALIDAD RETENCION":              "net_claims_and_policy_obligations",
    "SINIESTRALIDAD RETENCIÓN":              "net_claims_and_policy_obligations",  # accented
    "SINIESTRALIDAD DEVENGADA RETENCION":    "net_claims_and_policy_obligations",
    "SINIESTRALIDAD DEVENGADA RETENCIÓN":    "net_claims_and_policy_obligations",  # accented
    "COSTO SINIESTROS NETOS":               "net_claims_and_policy_obligations",
    # Commissions / acquisition
    "COMISIONES PAGADAS A BROKERS":          "agent_commissions",
    "COMISIONES PAGADAS":                    "agent_commissions",
    "COMISIONES NETAS":                      "agent_commissions",
    "COMISIONES RECIBIDAS DE REASEGURADORES": "reinsurance_commissions_received",
    "COMISIONES RECIBIDAS REASEG.":          "reinsurance_commissions_received",
    "COSTO NETO DE ADQUISICION":             "net_acquisition_costs",
    "COSTO NETO DE ADQUISICIÓN":             "net_acquisition_costs",   # accented (CL/CO/EC/PE)
    # Admin
    "COSTO ADMINISTRATIVO NETO":             "admin_expenses",
    "GASTOS ADMINISTRATIVOS":                "admin_expenses",
    "GASTOS ADMINSTRATIVOS":                 "admin_expenses",   # typo in CL/EC/PE
    "GASTOS ADMINISTRATIVOS NETOS":          "admin_expenses",
    "GASTOS ADMINSTRATIVOS NETOS":           "admin_expenses",   # typo + "netos"
    # Financial
    "INGRESOS FINANCIEROS":                  "financial_and_investment_result",
    # Profitability
    "RESULTADO TECNICO":                     "technical_profit_loss",
    "RESULTADO TÉCNICO":                     "technical_profit_loss",   # accented (CL/CO/EC/PE/AR)
    "UTILIDAD TECNICA":                      "operating_profit_loss",
    "UTILIDAD NETA":                         "net_income_loss",
}

_CUENTA_GROUP: dict[str, str] = {
    "gross_premiums":                     "Premium Flow",
    "net_earned_premiums":                "Premium Flow",
    "ceded_premiums":                     "Premium Flow",
    "net_retained_premiums":              "Premium Flow",
    "claims_incurred":                    "Claims",
    "net_claims_and_policy_obligations":  "Claims",
    "agent_commissions":                  "Acquisition",
    "reinsurance_commissions_received":   "Acquisition",
    "net_acquisition_costs":              "Acquisition",
    "admin_expenses":                     "Operating Expenses",
    "financial_and_investment_result":    "Financial",
    "technical_profit_loss":              "Profitability",
    "operating_profit_loss":              "Profitability",
    "net_income_loss":                    "Profitability",
}

_CUENTA_EN: dict[str, str] = {
    "gross_premiums":                     "Gross Written Premiums",
    "net_earned_premiums":                "Net Earned Premiums",
    "ceded_premiums":                     "Ceded Reinsurance Premiums",
    "net_retained_premiums":              "Net Retained Premiums",
    "claims_incurred":                    "Claims Incurred (Gross)",
    "net_claims_and_policy_obligations":  "Net Claims and Policy Obligations",
    "agent_commissions":                  "Agent / Broker Commissions",
    "reinsurance_commissions_received":   "Reinsurance Commissions Received",
    "net_acquisition_costs":              "Net Acquisition Costs",
    "admin_expenses":                     "Administrative Expenses (Net)",
    "financial_and_investment_result":    "Financial and Investment Result",
    "technical_profit_loss":              "Technical Profit / Loss",
    "operating_profit_loss":              "Operating Profit / Loss",
    "net_income_loss":                    "Net Income / Loss",
}

# ── LoB mapping ────────────────────────────────────────────────────────────────
# ramo values that mean "total portfolio" (is_total_lob = TRUE)
# AR parser emits "TOTAL"; CL/CO/EC/PE long-format files use "TOTALES"; fallback "SUMA TODOS"
_TOTAL_RAMOS = frozenset({"SUMA TODOS", "TOTAL", "TOTALES"})

# Non-total ramo values → (lob_l1_en, lob_l1_es)
_LOB_MAP: dict[str, tuple[str, str]] = {
    "VIDA INDIVIDUAL":      ("Life",    "Vida"),
    "VIDA GRUPO":           ("Life",    "Vida"),
    "OTROS VIDA":           ("Life",    "Vida"),
    "PENSIONES":            ("Pension", "Pensiones"),
    "ACCIDENTES PERSONALES":("A&H",     "Accidentes y Enfermedades"),
    "SALUD":                ("A&H",     "Accidentes y Enfermedades"),
}
_DEFAULT_LOB = ("P&C", "Ramos Generales")

# MX FX rates (Banxico year-end December)
_MX_FX_RATES: dict[int, float] = {
    2015: 17.21, 2016: 20.73, 2017: 19.66, 2018: 19.65, 2019: 18.93,
    2020: 19.91, 2021: 20.56, 2022: 19.35, 2023: 17.15, 2024: 20.37,
    2025: 20.45,
}


# ── SQL helpers ────────────────────────────────────────────────────────────────

def _case(col: str, mapping: dict, default: str = "NULL") -> str:
    """Build a SQL CASE WHEN expression."""
    lines = [f"CASE UPPER(TRIM({col}))"]
    for key, val in mapping.items():
        lines.append(f"  WHEN '{key.replace(chr(39), chr(39)*2)}' THEN '{val.replace(chr(39), chr(39)*2)}'")
    lines.append(f"  ELSE {default}")
    lines.append("END")
    return "\n".join(lines)


def build_view_sql(catalog: str) -> str:
    """Return CREATE OR REPLACE VIEW SQL for vw_er_completo_all."""

    cc_case = _case("descripcion", _CUENTA_CODE)
    cg_case = _case("descripcion", {d: _CUENTA_GROUP[c] for d, c in _CUENTA_CODE.items()})
    ce_case = _case("descripcion", {d: _CUENTA_EN[c]    for d, c in _CUENTA_CODE.items()})

    lob_en_map = {**{r: "Total Portfolio" for r in _TOTAL_RAMOS},
                  **{k: v[0] for k, v in _LOB_MAP.items()}}
    lob_es_map = {**{r: "Total"           for r in _TOTAL_RAMOS},
                  **{k: v[1] for k, v in _LOB_MAP.items()}}

    lob_en_case   = _case("ramo", lob_en_map, default="'P&C'")
    lob_es_case   = _case("ramo", lob_es_map, default="'Ramos Generales'")
    total_vals    = ", ".join(f"'{r}'" for r in sorted(_TOTAL_RAMOS))
    mx_fx_values  = ",\n    ".join(f"({y}, {r})" for y, r in _MX_FX_RATES.items())
    mx_tbl        = f"`{catalog}`.`insurance_mx`.`gold_cnsf_estado_resultados`"

    return f"""CREATE OR REPLACE VIEW `{catalog}`.`insurance_provider`.`vw_er_completo_all`
COMMENT 'Annual P&L — AR/CL/CO/EC/PE from er_completo (richer accounts) + MX from CNSF. USD. Grain: company x year x LoB x account.'
AS
WITH er_union AS (
  SELECT pais, empresa, CAST(ano AS BIGINT) AS anio, CAST(mes AS BIGINT) AS mes,
         descripcion, ramo, CAST(valor AS DOUBLE) AS valor
  FROM `{catalog}`.`insurance_ar`.`er_completo`
  UNION ALL
  SELECT pais, empresa, CAST(ano AS BIGINT), CAST(mes AS BIGINT), descripcion, ramo, CAST(valor AS DOUBLE)
  FROM `{catalog}`.`insurance_cl`.`er_completo`
  UNION ALL
  SELECT pais, empresa, CAST(ano AS BIGINT), CAST(mes AS BIGINT), descripcion, ramo, CAST(valor AS DOUBLE)
  FROM `{catalog}`.`insurance_co`.`er_completo`
  UNION ALL
  SELECT pais, empresa, CAST(ano AS BIGINT), CAST(mes AS BIGINT), descripcion, ramo, CAST(valor AS DOUBLE)
  FROM `{catalog}`.`insurance_ec`.`er_completo`
  UNION ALL
  SELECT pais, empresa, CAST(ano AS BIGINT), CAST(mes AS BIGINT), descripcion, ramo, CAST(valor AS DOUBLE)
  FROM `{catalog}`.`insurance_pe`.`er_completo`
),
er_mapped AS (
  SELECT
    pais,
    CASE pais
      WHEN 'AR' THEN 'SSN'  WHEN 'CL' THEN 'CMF'
      WHEN 'CO' THEN 'SFC'  WHEN 'EC' THEN 'SCVS'
      WHEN 'PE' THEN 'SBS'
    END                                   AS regulador,
    empresa,
    CAST(NULL AS STRING)                  AS id_empresa,
    anio,
    {lob_en_case}                         AS lob_l1_en,
    {lob_es_case}                         AS lob_l1_es,
    ramo                                  AS lob_source,
    CASE WHEN UPPER(TRIM(ramo)) IN ({total_vals}) THEN TRUE ELSE FALSE END AS is_total_lob,
    {cc_case}                             AS cuenta_code,
    {cg_case}                             AS cuenta_group,
    {ce_case}                             AS cuenta_en,
    descripcion                           AS cuenta_source,
    'USD'                                 AS moneda,
    CASE pais
      WHEN 'AR' THEN 'ARS'  WHEN 'CL' THEN 'CLP'
      WHEN 'CO' THEN 'COP'  WHEN 'EC' THEN 'USD'
      WHEN 'PE' THEN 'PEN'
    END                                   AS moneda_local,
    valor * 1000.0                        AS valor_usd
  FROM er_union
  WHERE mes = 12
    AND valor IS NOT NULL
    AND valor != 0
)
SELECT
  em.pais, em.regulador, em.empresa, em.id_empresa, em.anio,
  em.lob_l1_en, em.lob_l1_es, em.lob_source, em.is_total_lob,
  em.cuenta_code, em.cuenta_group, em.cuenta_en, em.cuenta_source,
  em.moneda, em.moneda_local,
  SUM(em.valor_usd)                              AS valor_usd,
  ROUND(SUM(em.valor_usd) / 1000000.0, 4)        AS valor_mm_usd,
  SUM(em.valor_usd)                              AS valor_local,
  ROUND(SUM(em.valor_usd) / 1000000.0, 4)        AS valor_mm_local,
  1.0                                            AS fx_rate,
  COUNT(DISTINCT em.empresa)                     AS n_empresas,
  MAKE_DATE(em.anio, 12, 31)                     AS periodo_inicio,
  MAKE_DATE(em.anio, 12, 31)                     AS periodo_fin
FROM er_mapped em
WHERE em.cuenta_code IS NOT NULL
GROUP BY
  em.pais, em.regulador, em.empresa, em.id_empresa, em.anio,
  em.lob_l1_en, em.lob_l1_es, em.lob_source, em.is_total_lob,
  em.cuenta_code, em.cuenta_group, em.cuenta_en, em.cuenta_source,
  em.moneda, em.moneda_local

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
    WHEN 'direct_business' THEN 'Gross Written Premiums'
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
  CASE g.business_concept_code WHEN 'direct_business' THEN 'Gross Written Premiums' ELSE g.business_concept_en END,
  g.descripcion_original,
  fx.rate"""


# ── Execution helpers ─────────────────────────────────────────────────────────

def _load_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _execute(w, wid, sql: str, label: str) -> list:
    from databricks.sdk.service.sql import StatementState
    TERMINAL = {StatementState.SUCCEEDED, StatementState.FAILED,
                StatementState.CANCELED, StatementState.CLOSED}
    r = w.statement_execution.execute_statement(
        warehouse_id=wid, statement=sql, wait_timeout="50s"
    )
    for _ in range(30):
        if r.status.state in TERMINAL:
            break
        time.sleep(4)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"{label}: {r.status.error}")
    print(f"  OK  {label}")
    return r.result.data_array or []


# ── Main ──────────────────────────────────────────────────────────────────────

def run(catalog: str, dry_run: bool = False) -> None:
    sql = build_view_sql(catalog)

    if dry_run:
        print("=== er_completo_provider — DRY RUN ===")
        print(f"View: {catalog}.insurance_provider.vw_er_completo_all")
        print(f"\nSQL ({len(sql):,} chars):\n")
        print(sql[:3000], "..." if len(sql) > 3000 else "")
        return

    print("=== er_completo_provider — Creating view ===")
    from connectors.shared.databricks import _client, _warehouse_id

    w   = _client()
    wid = _warehouse_id(w)

    _execute(w, wid,
             f"CREATE SCHEMA IF NOT EXISTS `{catalog}`.`insurance_provider`",
             "schema insurance_provider")

    _execute(w, wid, sql, "CREATE VIEW vw_er_completo_all")

    # Spot-check: row counts by country + year
    rows = _execute(w, wid, f"""
        SELECT pais, anio,
               COUNT(*) AS rows,
               COUNT(DISTINCT empresa) AS companies,
               COUNT(DISTINCT cuenta_code) AS accounts
        FROM `{catalog}`.`insurance_provider`.`vw_er_completo_all`
        WHERE is_total_lob = true
        GROUP BY pais, anio
        ORDER BY pais, anio
    """, "row count check")

    print(f"\n  {'PAIS':<5} {'ANIO':>5} {'ROWS':>8} {'COMPANIES':>10} {'ACCOUNTS':>9}")
    print(f"  {'-'*5} {'-'*5} {'-'*8} {'-'*10} {'-'*9}")
    for r in rows:
        print(f"  {r[0]:<5} {r[1]:>5} {int(r[2]):>8,} {int(r[3]):>10} {int(r[4]):>9}")

    print(f"\n  View ready: {catalog}.insurance_provider.vw_er_completo_all")
    print("Done.")


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Build vw_er_completo_all from er_completo Delta tables."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print SQL without creating the view.")
    args = parser.parse_args()
    _load_env()
    catalog = os.environ.get("DATABRICKS_CATALOG", "prod_us_prismlatam_c30670d")
    run(catalog, dry_run=args.dry_run)


if __name__ == "__main__":
    _cli()
