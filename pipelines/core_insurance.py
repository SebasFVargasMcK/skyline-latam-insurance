"""Pipeline: LATAM Insurance Core — chatbot-ready presentation layer.

Creates two views in the insurance_core schema on top of gold_insurance_latam:

  vw_insurance_detail   — row-level cleaned view (filtered + labeled)
                          grain: pais × empresa × periodo × lob × cuenta_code
                          adds periodo_label, valor_mm, moneda_nota

  vw_insurance_annual   — pre-aggregated annual totals
                          grain: pais × empresa × anio × lob × cuenta_code
                          SUM across all months/quarters within a year
                          adds n_periodos so partial years are transparent

  vw_cnsf_mx            — Mexico-only slice of vw_insurance_annual
                          used as the single source for the Databricks Genie agent

Usage:
    python -m pipelines.core_insurance
    python -m pipelines.core_insurance --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _build_sql(catalog: str) -> list[tuple[str, str]]:
    detail = f"""
CREATE OR REPLACE VIEW {catalog}.insurance_core.vw_insurance_detail AS
SELECT
  pais,
  regulador,
  empresa,
  id_empresa,
  periodo,
  anio,
  mes,
  trimestre,
  frecuencia,
  lob_l1_en,
  lob_l1_es,
  lob_l2_en,
  is_total_lob,
  cuenta_code,
  cuenta_group,
  cuenta_en,
  valor,
  moneda,
  ROUND(valor / 1000000.0, 4)                                     AS valor_mm,
  CASE frecuencia
    WHEN 'anual'      THEN CAST(anio AS STRING)
    WHEN 'trimestral' THEN CONCAT(CAST(anio AS STRING), '-Q',
                           CAST(trimestre AS STRING))
    WHEN 'mensual'    THEN CONCAT(CAST(anio AS STRING), '-',
                           LPAD(CAST(mes AS STRING), 2, '0'))
    ELSE CAST(periodo AS STRING)
  END                                                             AS periodo_label,
  'Values in local currency — do not sum across countries without FX conversion.'
                                                                  AS moneda_nota
FROM {catalog}.insurance_gold.gold_insurance_latam
WHERE cuenta_code IS NOT NULL
  AND valor IS NOT NULL
  AND valor != 0
"""

    annual = f"""
CREATE OR REPLACE VIEW {catalog}.insurance_core.vw_insurance_annual AS
SELECT
  pais,
  regulador,
  empresa,
  id_empresa,
  anio,
  lob_l1_en,
  lob_l1_es,
  lob_l2_en,
  is_total_lob,
  cuenta_code,
  cuenta_group,
  cuenta_en,
  moneda,
  SUM(valor)                        AS valor,
  ROUND(SUM(valor) / 1000000.0, 4)  AS valor_mm,
  COUNT(DISTINCT periodo)            AS n_periodos,
  MIN(periodo)                       AS periodo_inicio,
  MAX(periodo)                       AS periodo_fin
FROM {catalog}.insurance_core.vw_insurance_detail
GROUP BY
  pais, regulador, empresa, id_empresa, anio,
  lob_l1_en, lob_l1_es, lob_l2_en, is_total_lob,
  cuenta_code, cuenta_group, cuenta_en, moneda
"""

    mx = f"""
CREATE OR REPLACE VIEW {catalog}.insurance_core.vw_cnsf_mx (
  pais          COMMENT 'ISO country code — always MX in this view.',
  regulador     COMMENT 'Regulatory body — always CNSF (Comision Nacional de Seguros y Fianzas).',
  empresa       COMMENT 'Legal name of the insurance company as registered with CNSF.',
  id_empresa    COMMENT 'CNSF company identifier (null — not available in this source).',
  anio          COMMENT 'Calendar year of the annual aggregate (e.g. 2024).',
  lob_l1_en     COMMENT 'Line of business L1 in English. Values: Life & Pensions, Property & Casualty, Accident & Health, Surety / Bonds, Total Portfolio.',
  lob_l1_es     COMMENT 'Line of business L1 in Spanish. Values: Vida, Daños, Accidentes y Enfermedades, Fianzas, Total.',
  lob_l2_en     COMMENT 'Line of business sub-category L2. NULL when not reported.',
  is_total_lob  COMMENT 'TRUE = total-portfolio row. Exclude when analyzing individual LoBs to avoid double-counting.',
  cuenta_code   COMMENT 'Canonical account code. Key values: gross_premiums, net_retained_premiums, ceded_premiums, assumed_business, net_earned_premiums, claims_incurred, net_claims_and_policy_obligations, agent_commissions, technical_profit_loss, financial_and_investment_result.',
  cuenta_group  COMMENT 'Account category: Premium Flow, Claims, Acquisition, Operating Expenses, Profitability.',
  cuenta_en     COMMENT 'English description of the financial account.',
  moneda        COMMENT 'Currency — always MXN (Mexican Pesos) in this view.',
  valor         COMMENT 'Financial value in nominal MXN. Prefer valor_mm for readability.',
  valor_mm      COMMENT 'Financial value in millions of MXN. Use this column for all calculations.',
  n_periodos    COMMENT 'Monthly periods summed into this annual row. Full year = 12. Less than 12 = partial year.',
  periodo_inicio COMMENT 'First monthly period included in this annual aggregate.',
  periodo_fin   COMMENT 'Last monthly period included in this annual aggregate.'
)
COMMENT 'Annual financials for all CNSF-regulated insurers in Mexico, 2016-2025. Grain: company x year x LoB x account. Currency: MXN. Always filter is_total_lob = false when analyzing specific lines of business.'
AS
SELECT * FROM {catalog}.insurance_core.vw_insurance_annual
WHERE pais = 'MX'
"""

    return [
        ("schema insurance_core",       f"CREATE SCHEMA IF NOT EXISTS {catalog}.insurance_core"),
        ("vw_insurance_detail",          detail),
        ("vw_insurance_annual",          annual),
        ("vw_cnsf_mx",                   mx),
    ]


def run(catalog: str, *, dry_run: bool = False) -> None:
    print("=== LATAM Insurance Core — Deploy Views ===")

    steps = _build_sql(catalog)

    if dry_run:
        print("\n-- DRY RUN --")
        for label, sql in steps:
            print(f"\n-- {label} --")
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

    for label, sql in steps:
        execute(sql, label)

    # Row-count summary for both views
    for view, grain_cols in [
        ("vw_insurance_detail", "pais, anio"),
        ("vw_insurance_annual", "pais, anio"),
    ]:
        rows = execute(
            f"""
            SELECT pais, anio, COUNT(*) AS filas,
                   COUNT(DISTINCT empresa) AS empresas,
                   COUNT(DISTINCT lob_l1_en) AS lobs,
                   COUNT(DISTINCT cuenta_code) AS cuentas
            FROM {catalog}.insurance_core.{view}
            GROUP BY pais, anio
            ORDER BY pais, anio
            """,
            f"count {view}",
        )
        print(f"\n  [{view}]")
        print(f"  {'PAIS':<6} {'ANIO':>5} {'ROWS':>9} {'COMPANIES':>10} {'LOBS':>5} {'CUENTAS':>8}")
        print(f"  {'='*6} {'='*5} {'='*9} {'='*10} {'='*5} {'='*8}")
        for r in rows:
            print(f"  {r[0]:<6} {r[1]:>5} {int(r[2]):>9,} {int(r[3]):>10} {int(r[4]):>5} {int(r[5]):>8}")

    print(f"\n  {catalog}.insurance_core.vw_insurance_detail")
    print(f"  {catalog}.insurance_core.vw_insurance_annual")
    print("Done.")


def _find_warehouse(w) -> str:
    warehouses = list(w.warehouses.list())
    if not warehouses:
        raise RuntimeError("No SQL warehouses found")
    return warehouses[0].id


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Deploy LATAM Insurance Core views")
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
