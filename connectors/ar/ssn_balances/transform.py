"""Transform SSN Argentina 'Balances Aseguradoras' CSV files.

Each CSV has 8 columns:
    cia_id              INT   — internal company ID
    cia_denominacion    STR   — company name
    indice_tiempo       STR   — first day of quarter, e.g. '2024-07-01'
    subramo_id          FLOAT — LoB sub-branch ID (NaN for aggregate rows)
    subramo_descripcion STR   — LoB sub-branch label
    importe             FLOAT — amount in nominal ARS
    cuenta_id           STR   — 8-level account code, e.g. '5.01.01.01.01.01.01.00'
    cuenta_descripcion  STR   — account label

Aggregate rows (subramo_id is NaN) are excluded — they double-count the detail.

Output schema:
    fecha_corte     DATE   — last day of quarter (2024-07-01 -> 2024-09-30)
    anio            INT
    trimestre       INT    — 1-4
    cia_id          INT
    empresa         STR    — normalised company name
    subramo_id      INT
    subramo         STR    — original LoB label
    lob_l1_en       STR    — 'Life' | 'A&H' | 'P&C'
    cuenta_id       STR
    cuenta          STR    — account label
    importe         FLOAT  — nominal ARS
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Sequence

import pandas as pd

_QUARTER_ENDS = {1: (3, 31), 4: (6, 30), 7: (9, 30), 10: (12, 31)}

_LIFE_PREFIXES = ("Vida", "Retiro", "Rtas. Prev.")
_AH_PREFIXES   = ("Acc. Personales", "Salud")


def _quarter_end(dt: datetime.date) -> datetime.date:
    end_month, end_day = _QUARTER_ENDS[dt.month]
    return datetime.date(dt.year, end_month, end_day)


def _lob_l1(subramo: str) -> str:
    for prefix in _LIFE_PREFIXES:
        if subramo.startswith(prefix):
            return "Life"
    for prefix in _AH_PREFIXES:
        if subramo.startswith(prefix):
            return "A&H"
    return "P&C"


def _read_csv(path: Path) -> pd.DataFrame:
    for enc in ("utf-8", "latin-1", "utf-8-sig"):
        try:
            return pd.read_csv(path, encoding=enc, dtype=str, low_memory=False)
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Cannot decode {path.name}")


def transform(paths: Sequence[Path]) -> pd.DataFrame:
    """Parse and normalize all quarterly CSV files into a single DataFrame."""
    parts: list[pd.DataFrame] = []

    for path in paths:
        df = _read_csv(path)

        df.columns = [c.strip().lower() for c in df.columns]

        # subramo_id is a dotted string code like "1.010.99", keep non-null only
        df = df[df["subramo_id"].notna() & (df["subramo_id"].astype(str).str.strip() != "")].copy()

        if df.empty:
            continue

        df["indice_tiempo"] = pd.to_datetime(df["indice_tiempo"], errors="coerce")
        df = df.dropna(subset=["indice_tiempo"])

        df["fecha_corte"] = pd.to_datetime(
            df["indice_tiempo"].dt.date.map(_quarter_end)
        )
        df["anio"] = df["indice_tiempo"].dt.year.astype(int)
        df["trimestre"] = ((df["indice_tiempo"].dt.month - 1) // 3 + 1).astype(int)

        df["cia_id"] = pd.to_numeric(df["cia_id"], errors="coerce").astype("Int64")
        df["subramo_id"] = df["subramo_id"].astype(str).str.strip()
        df["importe"] = pd.to_numeric(df["importe"], errors="coerce")

        df["cia_denominacion"] = df["cia_denominacion"].str.strip()
        df["subramo_descripcion"] = df["subramo_descripcion"].str.strip()
        df["cuenta_id"] = df["cuenta_id"].str.strip()
        df["cuenta_descripcion"] = df["cuenta_descripcion"].str.strip()

        df["lob_l1_en"] = df["subramo_descripcion"].map(_lob_l1)

        result = df[[
            "fecha_corte", "anio", "trimestre",
            "cia_id", "cia_denominacion",
            "subramo_id", "subramo_descripcion", "lob_l1_en",
            "cuenta_id", "cuenta_descripcion",
            "importe",
        ]].rename(columns={
            "cia_denominacion": "empresa",
            "subramo_descripcion": "subramo",
            "cuenta_descripcion": "cuenta",
        })

        parts.append(result)

    if not parts:
        return pd.DataFrame(columns=[
            "fecha_corte", "anio", "trimestre", "cia_id", "empresa",
            "subramo_id", "subramo", "lob_l1_en",
            "cuenta_id", "cuenta", "importe",
        ])

    combined = pd.concat(parts, ignore_index=True)
    combined = combined.drop_duplicates(
        subset=["fecha_corte", "cia_id", "subramo_id", "cuenta_id"]
    ).reset_index(drop=True)

    return combined
