"""Load SSN Argentina balances data to DuckDB (local) or Databricks."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

TABLE = "ssn_balances"


def load_duckdb(df: pd.DataFrame, db_path: Path, *, mode: str = "replace") -> int:
    import duckdb

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
    from connectors.shared.databricks import upload_dataframe

    return upload_dataframe(
        df,
        table,
        schema="insurance_ar",
        catalog=os.environ.get("DATABRICKS_CATALOG"),
    )


def load(
    df: pd.DataFrame,
    *,
    db_path: Path | None = None,
    mode: str = "replace",
) -> None:
    if os.getenv("DATABRICKS_HOST"):
        full_name = load_databricks(df)
        print(f"Loaded {len(df):,} rows ->> {full_name}")
    else:
        if db_path is None:
            raise ValueError("db_path is required when DATABRICKS_HOST is not set")
        rows = load_duckdb(df, db_path, mode=mode)
        print(f"Loaded {rows:,} rows ->> {db_path}")
