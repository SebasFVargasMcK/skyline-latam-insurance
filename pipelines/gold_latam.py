"""Pipeline: LATAM Insurance Gold — unified cross-country view.

Creates or replaces the view:
    {catalog}.insurance_gold.gold_insurance_latam

Each country block in the UNION ALL maps its source bronze/silver table to the
canonical Gold schema.  Add a new country by appending another SELECT block.

Gold schema:
    pais          VARCHAR  — ISO 2-letter country code
    regulador     VARCHAR  — regulator acronym
    empresa       VARCHAR  — company name (from source)
    id_empresa    VARCHAR  — local company ID (RUC/RUT/clave), null if unavailable
    periodo       DATE     — period-end date
    anio          INT
    mes           INT      — null for quarterly/annual data
    trimestre     INT      — null for monthly/annual data
    frecuencia    VARCHAR  — 'mensual' | 'trimestral' | 'anual'
    lob_source    VARCHAR  — original LoB label from source
    lob_l1_en     VARCHAR  — canonical LoB L1 (Life / P&C / Health / Total Portfolio)
    lob_l1_es     VARCHAR  — canonical LoB L1 in Spanish
    lob_l2_en     VARCHAR  — canonical LoB L2, null if not reported
    is_total_lob  BOOLEAN  — true = total-portfolio row (use to avoid double-counting)
    cuenta_source VARCHAR  — original account label from source
    cuenta_code   VARCHAR  — canonical account code (see vocabulary below)
    cuenta_group  VARCHAR  — Acquisition / Claims / Financial Result / Operating Expenses
                             / Premium Flow / Profitability / Balance Sheet / Ratio
    cuenta_en     VARCHAR  — canonical English account label
    valor         DOUBLE   — value in local currency
    moneda        VARCHAR  — USD | COP | MXN | CLP | PEN | ARS
    fuente_tabla  VARCHAR  — source Bronze table name

Canonical cuenta_code vocabulary
─────────────────────────────────
Premium Flow:   net_earned_premiums, gross_premiums, ceded_premiums,
                net_retained_premiums, direct_business, assumed_business
Claims:         net_claims_and_policy_obligations, claims_incurred
Acquisition:    agent_commissions, additional_agent_compensation, net_acquisition_costs
Operating Exp:  net_operating_expenses, admin_expenses
Financial:      financial_and_investment_result
Profitability:  technical_profit_loss, operating_profit_loss,
                profit_before_income_taxes, net_income_loss
Balance Sheet:  total_assets, total_liabilities, equity,
                investments, technical_reserves
Ratios:         roe, roa, current_ratio, leverage_ratio

Usage:
    python -m pipelines.gold_latam
    python -m pipelines.gold_latam --dry-run   # print SQL without executing
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _build_view_sql(catalog: str) -> str:
    return f"""
CREATE OR REPLACE VIEW {catalog}.insurance_gold.gold_insurance_latam AS

-- ── Mexico: CNSF Estado de Resultados ────────────────────────────────────────
-- Source: insurance_mx.gold_cnsf_estado_resultados
-- Grain:  company × year × LoB L1 × account
-- Dates:  2016–present  |  Frequency: annual (Q4 / December year-end only)
-- Notes:  business_concept_code already canonical; lob_group_global is the
--         canonical L1 LoB; NULL lob_group_global falls back to lob_level_1_en.
--         CNSF reports are cumulative YTD → only December (Q4) represents the
--         full calendar year.  lob_level_2_en IS NULL selects L1-level rows.
--         NOT LIKE '%Total%' drops duplicate "X Total" rows (same values as "X").
SELECT
  'MX'                                                            AS pais,
  'CNSF'                                                          AS regulador,
  entidad                                                         AS empresa,
  CAST(NULL AS STRING)                                            AS id_empresa,
  fecha_corte                                                     AS periodo,
  anio_corte                                                      AS anio,
  CAST(NULL AS INT)                                               AS mes,
  CAST(NULL AS INT)                                               AS trimestre,
  'anual'                                                         AS frecuencia,
  operacion_original                                              AS lob_source,
  COALESCE(lob_group_global, lob_level_1_en, 'Total Portfolio')   AS lob_l1_en,
  COALESCE(lob_level_1_es, 'Total')                               AS lob_l1_es,
  CAST(NULL AS STRING)                                            AS lob_l2_en,
  is_total                                                        AS is_total_lob,
  descripcion_original                                            AS cuenta_source,
  business_concept_code                                           AS cuenta_code,
  business_concept_group                                          AS cuenta_group,
  business_concept_en                                             AS cuenta_en,
  CAST(importe AS DOUBLE)                                         AS valor,
  'MXN'                                                           AS moneda,
  'gold_cnsf_estado_resultados'                                   AS fuente_tabla
FROM {catalog}.insurance_mx.gold_cnsf_estado_resultados
WHERE MONTH(fecha_corte) = 12
  AND lob_level_2_en IS NULL
  AND operacion_original NOT LIKE '%Total%'

UNION ALL

-- ── Peru: SBS Boletin Estadistico S-345 (Primas según Ramos) ─────────────────
-- Source: insurance_pe.sbs_boletin_s345
-- Grain:  company × year × LoB (L1 + L2 for Vida) × account
-- Dates:  2015–2025  |  Frequency: annual (December EoY snapshot)
-- Notes:  4 premium accounts, 3 LoB L1 groups + TOTAL. lob_l2 sub-types for Vida.
SELECT
  'PE'                                                            AS pais,
  'SBS'                                                           AS regulador,
  empresa                                                         AS empresa,
  CAST(NULL AS STRING)                                            AS id_empresa,
  fecha_corte                                                     AS periodo,
  anio                                                            AS anio,
  12                                                              AS mes,
  4                                                               AS trimestre,
  'anual'                                                         AS frecuencia,
  CONCAT_WS(' / ', lob_l1,
    CASE WHEN lob_l2 IS NOT NULL THEN lob_l2 END)                 AS lob_source,
  CASE lob_l1
    WHEN 'RAMOS GENERALES'                     THEN 'P&C'
    WHEN 'RAMOS DE ACCIDENTES Y ENFERMEDADES'  THEN 'A&H'
    WHEN 'RAMOS DE VIDA'                       THEN 'Life'
    ELSE 'Total Portfolio'
  END                                                             AS lob_l1_en,
  CASE lob_l1
    WHEN 'RAMOS GENERALES'                     THEN 'Ramos Generales'
    WHEN 'RAMOS DE ACCIDENTES Y ENFERMEDADES'  THEN 'Accidentes y Enfermedades'
    WHEN 'RAMOS DE VIDA'                       THEN 'Vida'
    ELSE 'Total'
  END                                                             AS lob_l1_es,
  CASE lob_l2
    WHEN 'Seguros de Vida'                              THEN 'Life Insurance'
    WHEN 'Seguros del Sistema Privado de Pensiones'     THEN 'Private Pension Insurance'
    ELSE NULL
  END                                                             AS lob_l2_en,
  lob_l1 = 'TOTAL'                                               AS is_total_lob,
  cuenta                                                          AS cuenta_source,
  CASE cuenta
    WHEN 'Primas de Seguros Netas'               THEN 'gross_premiums'
    WHEN 'Primas de Reaseguro Aceptado Netas'    THEN 'assumed_business'
    WHEN 'Primas Cedidas Netas'                  THEN 'ceded_premiums'
    WHEN 'Primas Retenidas'                      THEN 'net_retained_premiums'
    ELSE cuenta
  END                                                             AS cuenta_code,
  'Premium Flow'                                                  AS cuenta_group,
  CASE cuenta
    WHEN 'Primas de Seguros Netas'               THEN 'Net Written Premiums (Direct)'
    WHEN 'Primas de Reaseguro Aceptado Netas'    THEN 'Assumed Reinsurance Premiums (Net)'
    WHEN 'Primas Cedidas Netas'                  THEN 'Ceded Reinsurance Premiums (Net)'
    WHEN 'Primas Retenidas'                      THEN 'Net Retained Premiums'
    ELSE cuenta
  END                                                             AS cuenta_en,
  CAST(valor AS DOUBLE)                                           AS valor,
  'PEN'                                                           AS moneda,
  'sbs_boletin_s345'                                              AS fuente_tabla
FROM {catalog}.insurance_pe.sbs_boletin_s345

UNION ALL

-- ── Colombia: SFC Formato 290 Silver (UNPIVOT LoB) ───────────────────────────
-- Source: insurance_co.silver_sfc_formato_290
-- Grain:  company × month × LoB (ramo) × subcuenta
-- Dates:  2023-01 → present  |  Frequency: monthly
-- Notes:  nombre_entidad carries a leading SFC code (e.g. "14-26 Bbva Seguros…").
--         SUBTOTAL rows excluded (derived aggregate). Only 9 mapped subcuentas
--         are promoted to Gold; others are available in Silver.
SELECT
  'CO'                                                            AS pais,
  'SFC'                                                           AS regulador,
  REGEXP_REPLACE(nombre_entidad, '^\\\\d{{2}}-\\\\d{{2,3}}\\\\s+', '')   AS empresa,
  REGEXP_EXTRACT(nombre_entidad, '^(\\\\d{{2}}-\\\\d{{2,3}})')          AS id_empresa,
  periodo                                                         AS periodo,
  ano                                                             AS anio,
  mes                                                             AS mes,
  CAST(NULL AS INT)                                               AS trimestre,
  'mensual'                                                       AS frecuencia,
  CONCAT(lob_group, ' / ', ramo)                                  AS lob_source,
  lob_group                                                       AS lob_l1_en,
  CASE lob_group
    WHEN 'P&C'  THEN 'Ramos Generales'
    WHEN 'Life' THEN 'Vida'
    WHEN 'A&H'  THEN 'Accidentes y Enfermedades'
    ELSE lob_group
  END                                                             AS lob_l1_es,
  CAST(NULL AS STRING)                                            AS lob_l2_en,
  FALSE                                                           AS is_total_lob,
  nombre_subcuenta                                                AS cuenta_source,
  CASE nombre_subcuenta
    WHEN 'PRIMAS EMITIDAS DIRECTAS'               THEN 'gross_premiums'
    WHEN 'PRIMAS DEVENGADAS'                      THEN 'net_earned_premiums'
    WHEN 'RETENCION NETA DE LA COMPA#IA'          THEN 'net_retained_premiums'
    WHEN 'SINIESTROS LIQUIDADOS'                  THEN 'claims_incurred'
    WHEN 'SINIESTROS CTA COMPA#IA'                THEN 'net_claims_and_policy_obligations'
    WHEN 'RESULTADO TECNICO'                      THEN 'technical_profit_loss'
    WHEN 'UTILIDAD ANTES DE IMPUESTOS'            THEN 'profit_before_income_taxes'
    WHEN 'RESULTADOS DEL EJERCICIO'               THEN 'net_income_loss'
    WHEN 'REMUNERACION A FAVOR DE INTERMEDIA'     THEN 'agent_commissions'
    WHEN 'GASTOS DE ADMINISTRACION DIRECTOS RAMO' THEN 'admin_expenses'
  END                                                             AS cuenta_code,
  CASE nombre_subcuenta
    WHEN 'PRIMAS EMITIDAS DIRECTAS'               THEN 'Premium Flow'
    WHEN 'PRIMAS DEVENGADAS'                      THEN 'Premium Flow'
    WHEN 'RETENCION NETA DE LA COMPA#IA'          THEN 'Premium Flow'
    WHEN 'SINIESTROS LIQUIDADOS'                  THEN 'Claims'
    WHEN 'SINIESTROS CTA COMPA#IA'                THEN 'Claims'
    WHEN 'RESULTADO TECNICO'                      THEN 'Profitability'
    WHEN 'UTILIDAD ANTES DE IMPUESTOS'            THEN 'Profitability'
    WHEN 'RESULTADOS DEL EJERCICIO'               THEN 'Profitability'
    WHEN 'REMUNERACION A FAVOR DE INTERMEDIA'     THEN 'Acquisition'
    WHEN 'GASTOS DE ADMINISTRACION DIRECTOS RAMO' THEN 'Operating Expenses'
  END                                                             AS cuenta_group,
  CASE nombre_subcuenta
    WHEN 'PRIMAS EMITIDAS DIRECTAS'               THEN 'Gross Written Premiums (Direct)'
    WHEN 'PRIMAS DEVENGADAS'                      THEN 'Net Earned Premiums'
    WHEN 'RETENCION NETA DE LA COMPA#IA'          THEN 'Net Retained Premiums'
    WHEN 'SINIESTROS LIQUIDADOS'                  THEN 'Claims Incurred (Settled)'
    WHEN 'SINIESTROS CTA COMPA#IA'                THEN 'Net Claims and Policy Obligations'
    WHEN 'RESULTADO TECNICO'                      THEN 'Technical Profit / Loss'
    WHEN 'UTILIDAD ANTES DE IMPUESTOS'            THEN 'Profit Before Income Taxes'
    WHEN 'RESULTADOS DEL EJERCICIO'               THEN 'Net Income / Loss'
    WHEN 'REMUNERACION A FAVOR DE INTERMEDIA'     THEN 'Agent Commissions'
    WHEN 'GASTOS DE ADMINISTRACION DIRECTOS RAMO' THEN 'Direct Administrative Expenses'
  END                                                             AS cuenta_en,
  CAST(valor AS DOUBLE)                                           AS valor,
  'COP'                                                           AS moneda,
  'silver_sfc_formato_290'                                        AS fuente_tabla
FROM {catalog}.insurance_co.silver_sfc_formato_290
WHERE nombre_subcuenta IN (
    'PRIMAS EMITIDAS DIRECTAS',
    'PRIMAS DEVENGADAS',
    'RETENCION NETA DE LA COMPA#IA',
    'SINIESTROS LIQUIDADOS',
    'SINIESTROS CTA COMPA#IA',
    'RESULTADO TECNICO',
    'UTILIDAD ANTES DE IMPUESTOS',
    'RESULTADOS DEL EJERCICIO',
    'REMUNERACION A FAVOR DE INTERMEDIA',
    'GASTOS DE ADMINISTRACION DIRECTOS RAMO'
)

UNION ALL

-- ── Chile: CMF FECU (Generales + Vida) ───────────────────────────────────────
-- Source: insurance_cl.cmf_fecu
-- Grain:  company × quarter × FECU account
-- Dates:  2017-Q1 → present  |  Frequency: quarterly
-- Notes:  Chilean law requires separate companies for Generales (P&C) and
--         Vida (Life), so tipo_compania IS the LoB — no LoB column in FECU.
--         Only rows with a mapped cuenta_code are promoted to Gold.
SELECT
  'CL'                                                            AS pais,
  'CMF'                                                           AS regulador,
  razon_social                                                    AS empresa,
  rut                                                             AS id_empresa,
  fecha_corte                                                     AS periodo,
  ano                                                             AS anio,
  CAST(NULL AS INT)                                               AS mes,
  trimestre                                                       AS trimestre,
  'trimestral'                                                    AS frecuencia,
  tipo_compania                                                   AS lob_source,
  CASE
    WHEN tipo_compania = 'CIAS. DE SEGUROS GENERALES' THEN 'P&C'
    ELSE 'Life'
  END                                                             AS lob_l1_en,
  CASE
    WHEN tipo_compania = 'CIAS. DE SEGUROS GENERALES' THEN 'Ramos Generales'
    ELSE 'Vida'
  END                                                             AS lob_l1_es,
  CAST(NULL AS STRING)                                            AS lob_l2_en,
  FALSE                                                           AS is_total_lob,
  descripcion                                                     AS cuenta_source,
  CASE cuenta
    WHEN '5.31.11.10' THEN 'gross_premiums'
    WHEN '5.31.11.20' THEN 'assumed_business'
    WHEN '5.31.11.30' THEN 'ceded_premiums'
    WHEN '5.31.11.00' THEN 'net_retained_premiums'
    WHEN '5.31.13.10' THEN 'claims_incurred'
    WHEN '5.31.13.00' THEN 'net_claims_and_policy_obligations'
    WHEN '5.31.15.10' THEN 'agent_commissions'
    WHEN '5.31.20.00' THEN 'admin_expenses'
    WHEN '5.31.30.00' THEN 'financial_and_investment_result'
    WHEN '5.31.40.00' THEN 'technical_profit_loss'
    WHEN '5.31.70.00' THEN 'profit_before_income_taxes'
    WHEN '5.22.32.00' THEN 'net_income_loss'
    WHEN '5.10.00.00' THEN 'total_assets'
    WHEN '5.21.00.00' THEN 'total_liabilities'
    WHEN '5.22.00.00' THEN 'equity'
  END                                                             AS cuenta_code,
  CASE cuenta
    WHEN '5.31.11.10' THEN 'Premium Flow'
    WHEN '5.31.11.20' THEN 'Premium Flow'
    WHEN '5.31.11.30' THEN 'Premium Flow'
    WHEN '5.31.11.00' THEN 'Premium Flow'
    WHEN '5.31.13.10' THEN 'Claims'
    WHEN '5.31.13.00' THEN 'Claims'
    WHEN '5.31.15.10' THEN 'Acquisition'
    WHEN '5.31.20.00' THEN 'Operating Expenses'
    WHEN '5.31.30.00' THEN 'Financial'
    WHEN '5.31.40.00' THEN 'Profitability'
    WHEN '5.31.70.00' THEN 'Profitability'
    WHEN '5.22.32.00' THEN 'Profitability'
    WHEN '5.10.00.00' THEN 'Balance Sheet'
    WHEN '5.21.00.00' THEN 'Balance Sheet'
    WHEN '5.22.00.00' THEN 'Balance Sheet'
  END                                                             AS cuenta_group,
  descripcion                                                     AS cuenta_en,
  CAST(valor AS DOUBLE)                                           AS valor,
  'CLP'                                                           AS moneda,
  'cmf_fecu'                                                      AS fuente_tabla
FROM {catalog}.insurance_cl.cmf_fecu
WHERE cuenta IN (
    '5.31.11.10', '5.31.11.20', '5.31.11.30', '5.31.11.00',
    '5.31.13.10', '5.31.13.00',
    '5.31.15.10', '5.31.20.00', '5.31.30.00',
    '5.31.40.00', '5.31.70.00', '5.22.32.00',
    '5.10.00.00', '5.21.00.00', '5.22.00.00'
)
  AND valor IS NOT NULL

UNION ALL

-- ── Argentina: SSN Balances Aseguradoras ─────────────────────────────────────
-- Source: insurance_ar.ssn_balances
-- Grain:  company × quarter × subramo (LoB) × account
-- Dates:  2024-Q3 → present  |  Frequency: quarterly
-- Notes:  subramo is the LoB sub-branch (54 distinct values). 4 key cuenta_ids
--         mapped to canonical codes. importe in nominal ARS.
SELECT
  'AR'                                                            AS pais,
  'SSN'                                                           AS regulador,
  empresa                                                         AS empresa,
  CAST(cia_id AS STRING)                                          AS id_empresa,
  fecha_corte                                                     AS periodo,
  anio                                                            AS anio,
  CAST(NULL AS INT)                                               AS mes,
  trimestre                                                       AS trimestre,
  'trimestral'                                                    AS frecuencia,
  subramo                                                         AS lob_source,
  lob_l1_en                                                       AS lob_l1_en,
  CASE lob_l1_en
    WHEN 'P&C'  THEN 'Ramos Generales'
    WHEN 'Life' THEN 'Vida'
    WHEN 'A&H'  THEN 'Accidentes y Enfermedades'
    ELSE lob_l1_en
  END                                                             AS lob_l1_es,
  CAST(NULL AS STRING)                                            AS lob_l2_en,
  FALSE                                                           AS is_total_lob,
  cuenta                                                          AS cuenta_source,
  CASE cuenta_id
    WHEN '5.01.01.01.01.01.01.00' THEN 'gross_premiums'
    WHEN '4.01.03.03.03.01.00.00' THEN 'ceded_premiums'
    WHEN '5.01.01.01.01.04.01.00' THEN 'assumed_business'
    WHEN '4.01.01.01.01.01.00.00' THEN 'claims_incurred'
  END                                                             AS cuenta_code,
  CASE cuenta_id
    WHEN '5.01.01.01.01.01.01.00' THEN 'Premium Flow'
    WHEN '4.01.03.03.03.01.00.00' THEN 'Premium Flow'
    WHEN '5.01.01.01.01.04.01.00' THEN 'Premium Flow'
    WHEN '4.01.01.01.01.01.00.00' THEN 'Claims'
  END                                                             AS cuenta_group,
  CASE cuenta_id
    WHEN '5.01.01.01.01.01.01.00' THEN 'Gross Written Premiums (Direct)'
    WHEN '4.01.03.03.03.01.00.00' THEN 'Ceded Reinsurance Premiums'
    WHEN '5.01.01.01.01.04.01.00' THEN 'Assumed Reinsurance Premiums'
    WHEN '4.01.01.01.01.01.00.00' THEN 'Claims Paid (Direct)'
  END                                                             AS cuenta_en,
  CAST(importe AS DOUBLE)                                         AS valor,
  'ARS'                                                           AS moneda,
  'ssn_balances'                                                  AS fuente_tabla
FROM {catalog}.insurance_ar.ssn_balances
WHERE cuenta_id IN (
    '5.01.01.01.01.01.01.00',
    '4.01.03.03.03.01.00.00',
    '5.01.01.01.01.04.01.00',
    '4.01.01.01.01.01.00.00'
)
  AND importe IS NOT NULL
  AND importe != 0

-- ── [PLACEHOLDER: Ecuador SCVS] ─────────────────────────────────────────────
-- Source: insurance_ec.scvs_ranking
-- TODO: add after Silver + cuenta_code mapping for EC (ratios only)
"""


def run(catalog: str, *, dry_run: bool = False) -> None:
    print("=== LATAM Insurance Gold — Deploy View ===")

    sql = _build_view_sql(catalog)

    if dry_run:
        print("\n-- DRY RUN: SQL that would be executed --")
        print(sql)
        return

    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementState

    w = WorkspaceClient()
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID") or _find_warehouse(w)

    def execute(statement: str, label: str) -> list:
        r = w.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement=statement,
            wait_timeout="50s",
        )
        if r.status.state != StatementState.SUCCEEDED:
            for _ in range(8):
                time.sleep(5)
                r = w.statement_execution.get_statement(r.statement_id)
                if r.status.state == StatementState.SUCCEEDED:
                    break
        if r.status.state != StatementState.SUCCEEDED:
            raise RuntimeError(f"{label}: {r.status.error}")
        print(f"  OK {label}")
        return r.result.data_array or []

    execute(
        f"CREATE SCHEMA IF NOT EXISTS {catalog}.insurance_gold",
        "schema insurance_gold",
    )
    execute(sql, "CREATE OR REPLACE VIEW gold_insurance_latam")

    rows = execute(
        f"""
        SELECT pais, COUNT(*) AS filas,
               COUNT(DISTINCT empresa) AS empresas,
               MIN(periodo) AS desde, MAX(periodo) AS hasta,
               COUNT(DISTINCT cuenta_code) AS cuentas,
               COUNT(DISTINCT lob_l1_en) AS lobs
        FROM {catalog}.insurance_gold.gold_insurance_latam
        GROUP BY pais ORDER BY pais
        """,
        "row count check",
    )

    print()
    print(f"  {'PAIS':<6} {'ROWS':>10} {'COMPANIES':>10} {'FROM':<12} {'TO':<12} {'ACCOUNTS':>9} {'LOBS':>5}")
    print(f"  {'='*6} {'='*10} {'='*10} {'='*12} {'='*12} {'='*9} {'='*5}")
    for r in rows:
        print(
            f"  {r[0]:<6} {int(r[1]):>10,} {int(r[2]):>10} "
            f"{str(r[3]):<12} {str(r[4]):<12} {int(r[5]):>9} {int(r[6]):>5}"
        )
    print(f"\n  View: {catalog}.insurance_gold.gold_insurance_latam")
    print("Done.")


def _find_warehouse(w) -> str:
    warehouses = list(w.warehouses.list())
    if not warehouses:
        raise RuntimeError("No SQL warehouses found")
    return warehouses[0].id


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Deploy LATAM Insurance Gold view")
    parser.add_argument("--dry-run", action="store_true", help="Print SQL without executing")
    args = parser.parse_args()

    catalog = os.environ.get("DATABRICKS_CATALOG", "prod_us_prismlatam_c30670d")
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    catalog = os.environ.get("DATABRICKS_CATALOG", catalog)

    run(catalog, dry_run=args.dry_run)


if __name__ == "__main__":
    _cli()
