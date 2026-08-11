"""Pipeline: Colombia SFC Formato 290 — Bronze → Silver (UNPIVOT LoB columns).

The Bronze table (sfc_formato_290) stores one row per
(period × company × subcuenta) with 49 separate columns, one per ramo (LoB).

Silver normalises this into long format:
    (period × company × subcuenta × ramo)  →  valor

A lob_group column classifies each ramo into P&C / Life / A&H.

Silver is created as a Databricks table via pure SQL (UNPIVOT), so no data
leaves the warehouse.

Usage:
    python -m pipelines.co_sfc_silver
    python -m pipelines.co_sfc_silver --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── LoB classification ────────────────────────────────────────────────────────

_PC_RAMOS = [
    "automoviles", "soat", "cumplimiento",
    "responsabilidad_civil_mes", "incendio_mes", "terremoto_mes",
    "sustraccion_mes", "transporte_mes", "corriente_debil_mes",
    "todo_riesgo_contratista_mes", "manejo_mes", "lucro_cesante_mes",
    "montaje_rotura_maquina_mes", "aviacion_mes", "navegacion_y_casco_mes",
    "minas_y_petroleos_mes", "vidrios_mes", "credito_comercial_mes",
    "credito_a_exportacion_mes", "agricola_mes", "semovientes_mes",
    "desempleo_mes", "hogar_mes", "excequias_mes", "ope_no_ramos_mes",
    "decenal", "agropecuario_mes",
    "cumplimiento_entidades_estatales",
    "cumplimiento_empresas_servicios_publicos",
    "cumplimiento_emp__industriales_y_comerciales_del_estado",
    "cumplimiento_disposiciones_legales",
    "cumplimiento_causiones_judiciales",
    "cumplimiento_arrendamiento",
    "cumplimiento_particulares",
]

_LIFE_RAMOS = [
    "colectivo_vida_mes", "educativo_mes", "vida_grupo_mes",
    "vida_indivudual_mes", "prevision_invali_sobrev_mes",
    "pensiones_ley_100_mes", "pensiones_voluntarias_mes",
    "pension_conmuta_pension_mes", "pat_aut_fdo_pen_volunta_mes",
    "rentas_voluntarias_mes", "beps_mes",
]

_AH_RAMOS = [
    "accidentes_personales_mes", "salud_mes",
    "enfermedades_alto_costo_mes", "riesgos_profesionales_mes",
]

_ALL_RAMOS = _PC_RAMOS + _LIFE_RAMOS + _AH_RAMOS


def _lob_group_case() -> str:
    pc = ", ".join(f"'{r}'" for r in _PC_RAMOS)
    life = ", ".join(f"'{r}'" for r in _LIFE_RAMOS)
    ah = ", ".join(f"'{r}'" for r in _AH_RAMOS)
    return (
        f"CASE\n"
        f"    WHEN ramo IN ({pc}) THEN 'P&C'\n"
        f"    WHEN ramo IN ({life}) THEN 'Life'\n"
        f"    WHEN ramo IN ({ah}) THEN 'A&H'\n"
        f"    ELSE 'Other'\n"
        f"  END"
    )


def _build_silver_sql(catalog: str) -> str:
    unpivot_cols = ",\n    ".join(_ALL_RAMOS)
    lob_case = _lob_group_case()

    return f"""
CREATE OR REPLACE TABLE {catalog}.insurance_co.silver_sfc_formato_290 AS
SELECT
  u.periodo,
  u.ano,
  u.mes,
  u.tipo_entidad,
  u.codigo_entidad,
  u.nombre_entidad,
  u.subcuenta,
  u.nombre_subcuenta,
  u.ramo,
  {lob_case} AS lob_group,
  u.valor
FROM {catalog}.insurance_co.sfc_formato_290
UNPIVOT (valor FOR ramo IN (
    {unpivot_cols}
)) u
WHERE u.valor IS NOT NULL
  AND u.valor != 0
"""


def run(catalog: str, *, dry_run: bool = False) -> None:
    print("=== Colombia SFC Formato 290 — Bronze -> Silver ===")

    sql = _build_silver_sql(catalog)

    if dry_run:
        print("\n-- DRY RUN --")
        print(sql)
        return

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.sql import StatementState

    w = WorkspaceClient()
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "931f762656f3229e")

    def execute(statement: str, label: str) -> list:
        r = w.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement=statement,
            wait_timeout="50s",
        )
        for _ in range(24):
            if r.status.state == StatementState.SUCCEEDED:
                break
            time.sleep(5)
            r = w.statement_execution.get_statement(r.statement_id)
        if r.status.state != StatementState.SUCCEEDED:
            raise RuntimeError(f"{label}: {r.status.error}")
        print(f"  OK {label}")
        return r.result.data_array or []

    execute(sql, "CREATE silver_sfc_formato_290 (UNPIVOT — may take ~30s)")

    rows = execute(
        f"""
        SELECT
          lob_group,
          COUNT(*) AS filas,
          COUNT(DISTINCT nombre_entidad) AS empresas,
          COUNT(DISTINCT ramo) AS ramos,
          MIN(periodo) AS desde,
          MAX(periodo) AS hasta
        FROM {catalog}.insurance_co.silver_sfc_formato_290
        GROUP BY lob_group ORDER BY lob_group
        """,
        "row count by LoB group",
    )

    total_rows = execute(
        f"SELECT COUNT(*) FROM {catalog}.insurance_co.silver_sfc_formato_290",
        "total row count",
    )

    print()
    print(f"  {'LOB_GROUP':<8} {'ROWS':>10} {'COMPANIES':>10} {'RAMOS':>6} {'FROM':<12} {'TO':<12}")
    print(f"  {'='*8} {'='*10} {'='*10} {'='*6} {'='*12} {'='*12}")
    for r in rows:
        print(
            f"  {r[0]:<8} {int(r[1]):>10,} {int(r[2]):>10} "
            f"{int(r[3]):>6} {str(r[4]):<12} {str(r[5]):<12}"
        )
    print(f"\n  Total: {int(total_rows[0][0]):,} rows")
    print(f"  Table: {catalog}.insurance_co.silver_sfc_formato_290")
    print("Done.")


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Colombia SFC Silver transform")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    catalog = os.environ.get("DATABRICKS_CATALOG", "prod_us_prismlatam_c30670d")
    run(catalog, dry_run=args.dry_run)


if __name__ == "__main__":
    _cli()
