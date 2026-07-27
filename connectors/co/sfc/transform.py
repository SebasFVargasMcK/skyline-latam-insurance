"""Transform the raw SFC Formato 290 CSV into a normalized DataFrame.

The Socrata API returns a wide-format CSV where each row is:
    entity × capture_unit × subaccount × year/month

with one numeric column per insurance line (automoviles, vida_grupo, etc.).

Output schema (stored as sfc_formato_290 in DuckDB / Databricks):
    periodo                  DATE     — first day of reporting month
    ano                      INTEGER
    mes                      INTEGER  — 1–12
    tipo_entidad             VARCHAR
    codigo_entidad           VARCHAR
    nombre_entidad           VARCHAR
    unidad_de_captura        VARCHAR  — numeric code
    nombre_unidad_de_captura VARCHAR  — e.g. "PRIMAS RETENIDAS"
    subcuenta                VARCHAR  — numeric code
    nombre_subcuenta         VARCHAR  — e.g. "PRIMAS EMITIDAS DIRECTAS"
    total                    DOUBLE
    subtotal_ramos           DOUBLE
    <one column per insurance line>  DOUBLE
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# Columns that are identifiers (kept as-is after typing)
_ID_COLS = [
    "a_o",
    "mes",
    "tipo_entidad",
    "codigo_entidad",
    "nombre_entidad",
    "unidad_de_captura",
    "nombre_unidad_de_captura",
    "subcuenta",
    "nombre_subcuenta",
]

# Columns that must be cast to numeric
_NUMERIC_COLS = [
    "total",
    "subtotal_ramos",
    "automoviles",
    "soat",
    "cumplimiento",
    "responsabilidad_civil_mes",
    "incendio_mes",
    "terremoto_mes",
    "sustraccion_mes",
    "transporte_mes",
    "corriente_debil_mes",
    "todo_riesgo_contratista_mes",
    "manejo_mes",
    "lucro_cesante_mes",
    "montaje_rotura_maquina_mes",
    "aviacion_mes",
    "navegacion_y_casco_mes",
    "minas_y_petroleos_mes",
    "vidrios_mes",
    "credito_comercial_mes",
    "credito_a_exportacion_mes",
    "agricola_mes",
    "semovientes_mes",
    "desempleo_mes",
    "hogar_mes",
    "excequias_mes",
    "accidentes_personales_mes",
    "colectivo_vida_mes",
    "educativo_mes",
    "vida_grupo_mes",
    "salud_mes",
    "enfermedades_alto_costo_mes",
    "vida_indivudual_mes",
    "prevision_invali_sobrev_mes",
    "riesgos_profesionales_mes",
    "pensiones_ley_100_mes",
    "pensiones_voluntarias_mes",
    "pension_conmuta_pension_mes",
    "pat_aut_fdo_pen_volunta_mes",
    "rentas_voluntarias_mes",
    "beps_mes",
    "agropecuario_mes",
    "ope_no_ramos_mes",
    "decenal",
    "cumplimiento_entidades_estatales",
    "cumplimiento_empresas_servicios_p_blicos",
    "cumplimiento_emp_industriales_y_comerciales_del_estado",
    "cumplimiento_disposiciones_legales",
    "cumplimiento_causiones_judiciales",
    "cumplimiento_arrendamiento",
    "cumplimiento_particulares",
]


def transform(src: Path) -> pd.DataFrame:
    """Read the raw Formato 290 CSV and return the normalized DataFrame."""
    df = pd.read_csv(src, dtype=str, low_memory=False)

    # Normalise column names (strip whitespace, lowercase)
    df.columns = [c.strip().lower() for c in df.columns]

    # The API uses 'a_o' for year (Socrata encodes ñ → _o in field names)
    if "a_o" not in df.columns and "año" in df.columns:
        df = df.rename(columns={"año": "a_o"})

    missing_id = set(_ID_COLS) - set(df.columns)
    if missing_id:
        raise ValueError(f"Missing expected identifier columns: {missing_id}")

    # Build a proper date from year + month (first day of month)
    df["ano"] = pd.to_numeric(df["a_o"], errors="coerce").astype("Int64")
    df["mes_num"] = pd.to_numeric(df["mes"], errors="coerce").astype("Int64")
    df["periodo"] = pd.to_datetime(
        df["ano"].astype(str) + "-" + df["mes_num"].astype(str).str.zfill(2) + "-01",
        errors="coerce",
    )

    # Cast all numeric columns; any present in the file get cast, extras are ignored
    for col in _NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Any numeric columns the API added after this code was written
    known = set(_ID_COLS) | set(_NUMERIC_COLS) | {"a_o", "mes", "ano", "mes_num", "periodo"}
    extra_numeric = [c for c in df.columns if c not in known]
    for col in extra_numeric:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Rename identifier columns to canonical names
    df = df.rename(columns={"a_o": "ano_raw", "mes": "mes_raw"})

    # Strip whitespace from string columns
    for col in ["nombre_entidad", "nombre_unidad_de_captura", "nombre_subcuenta"]:
        if col in df.columns:
            df[col] = df[col].str.strip()

    # Reorder: date columns first, then identifiers, then numerics
    front = [
        "periodo",
        "ano",
        "mes_num",
        "tipo_entidad",
        "codigo_entidad",
        "nombre_entidad",
        "unidad_de_captura",
        "nombre_unidad_de_captura",
        "subcuenta",
        "nombre_subcuenta",
    ]
    remaining = [c for c in df.columns if c not in front and c not in ("ano_raw", "mes_raw")]
    df = df[front + remaining].rename(columns={"mes_num": "mes"})

    return df.reset_index(drop=True)
