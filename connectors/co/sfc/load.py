"""Load normalized SFC Formato 290 data into DuckDB (local) or Databricks (production)."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd

TABLE = "sfc_formato_290"


def load_duckdb(df: pd.DataFrame, db_path: Path, *, mode: str = "replace") -> int:
    """Write *df* to the local DuckDB file at *db_path*."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        if mode == "replace":
            con.execute(f"DROP TABLE IF EXISTS {TABLE}")
            con.execute(f"CREATE TABLE {TABLE} AS SELECT * FROM df")
        else:
            try:
                con.execute(f"INSERT INTO {TABLE} SELECT * FROM df")
            except duckdb.CatalogException:
                con.execute(f"CREATE TABLE {TABLE} AS SELECT * FROM df")
        return len(df)
    finally:
        con.close()


def load_databricks(df: pd.DataFrame, *, table: str = TABLE) -> str:
    """Write *df* to a Databricks Delta table.

    Requires DATABRICKS_CATALOG and DATABRICKS_SCHEMA env vars.
    Returns the fully-qualified table name.
    """
    from connectors.shared.databricks import upload_dataframe

    return upload_dataframe(df, table)


def load(
    df: pd.DataFrame,
    *,
    db_path: Path | None = None,
    mode: str = "replace",
) -> None:
    """Route to DuckDB (local dev) or Databricks (production).

    Set DATABRICKS_HOST env var to enable Databricks mode.
    """
    if os.getenv("DATABRICKS_HOST"):
        full_name = load_databricks(df)
        print(f"Loaded {len(df):,} rows → {full_name}")
    else:
        if db_path is None:
            raise ValueError("db_path is required when DATABRICKS_HOST is not set")
        rows = load_duckdb(df, db_path, mode=mode)
        print(f"Loaded {rows:,} rows → {db_path}")
