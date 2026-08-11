"""Databricks upload helpers shared by all connectors.

Uses the Databricks SQL Execution API (no file uploads, no cluster access needed):
  1. CREATE OR REPLACE TABLE … (schema inferred from DataFrame dtypes).
  2. INSERT INTO … VALUES … in batches of ~1 000 rows.
  3. Verify row count.

Required env vars:
    DATABRICKS_HOST     e.g. https://dbc-xxxx.cloud.databricks.com
    DATABRICKS_TOKEN    personal access token (dapi…)
    DATABRICKS_CATALOG  Unity Catalog catalog name
    DATABRICKS_SCHEMA   target schema / database name

Optional env vars:
    DATABRICKS_WAREHOUSE_ID   SQL warehouse id (auto-discovered if not set)
"""

from __future__ import annotations

import math
import os
import time
from typing import Any

import pandas as pd


def _client():
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()


def _warehouse_id(w) -> str:
    wid = os.getenv("DATABRICKS_WAREHOUSE_ID")
    if wid:
        return wid
    warehouses = list(w.warehouses.list())
    if not warehouses:
        raise RuntimeError(
            "No SQL warehouses found. Set DATABRICKS_WAREHOUSE_ID in your .env."
        )
    return warehouses[0].id


# ---------------------------------------------------------------------------
# SQL type mapping
# ---------------------------------------------------------------------------

def _spark_type(series: pd.Series) -> str:
    dtype = series.dtype
    dtype_str = str(dtype)
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "DATE"
    if dtype_str in ("Int8", "Int16", "Int32", "Int64",
                     "UInt8", "UInt16", "UInt32", "UInt64"):
        return "BIGINT"
    if pd.api.types.is_integer_dtype(dtype):
        return "BIGINT"
    if pd.api.types.is_float_dtype(dtype):
        return "DOUBLE"
    return "STRING"


def _create_ddl(df: pd.DataFrame, full_name: str) -> str:
    col_defs = ",\n".join(
        f"    `{col}` {_spark_type(df[col])}" for col in df.columns
    )
    return f"CREATE OR REPLACE TABLE {full_name} (\n{col_defs}\n) USING DELTA"


# ---------------------------------------------------------------------------
# Value formatter  (runtime-type-driven, no dtype pre-scan needed)
# ---------------------------------------------------------------------------

def _fmt(v: Any) -> str:
    """Format a Python scalar as a SQL literal."""
    import datetime as _dt

    if v is None:
        return "NULL"
    try:
        if pd.isna(v):
            return "NULL"
    except (TypeError, ValueError):
        pass

    if isinstance(v, pd.Timestamp):
        return f"DATE '{v.strftime('%Y-%m-%d')}'"

    if isinstance(v, _dt.date):
        return f"DATE '{v.isoformat()}'"

    if isinstance(v, str):
        s = v.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{s}'"

    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return "NULL"
        return f"{v:.10g}"

    # int, numpy int64, pd.NA already handled above
    return str(v)


def _build_rows(batch_df: pd.DataFrame) -> list[str]:
    rows = []
    for tup in batch_df.itertuples(index=False, name=None):
        vals = ", ".join(_fmt(v) for v in tup)
        rows.append(f"({vals})")
    return rows


# ---------------------------------------------------------------------------
# SQL execution helpers
# ---------------------------------------------------------------------------

def _run_sql(w, warehouse_id: str, statement: str, poll_interval_s: int = 3) -> None:
    from databricks.sdk.service.sql import StatementState

    TERMINAL = {
        StatementState.SUCCEEDED,
        StatementState.FAILED,
        StatementState.CANCELED,
        StatementState.CLOSED,
    }

    result = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=statement,
        wait_timeout="50s",
    )
    while result.status.state not in TERMINAL:
        time.sleep(poll_interval_s)
        result = w.statement_execution.get_statement(result.statement_id)

    if result.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(
            f"SQL execution failed ({result.status.state}): {result.status.error}"
        )


def _run_sql_scalar(w, warehouse_id: str, statement: str) -> Any:
    """Execute a SQL statement that returns a single value."""
    from databricks.sdk.service.sql import StatementState

    TERMINAL = {
        StatementState.SUCCEEDED,
        StatementState.FAILED,
        StatementState.CANCELED,
        StatementState.CLOSED,
    }

    result = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id,
        statement=statement,
        wait_timeout="50s",
    )
    while result.status.state not in TERMINAL:
        time.sleep(3)
        result = w.statement_execution.get_statement(result.statement_id)

    if result.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(
            f"SQL execution failed ({result.status.state}): {result.status.error}"
        )
    return result.result.data_array[0][0] if result.result and result.result.data_array else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_dataframe(
    df: pd.DataFrame,
    table: str,
    *,
    catalog: str | None = None,
    schema: str | None = None,
    mode: str = "overwrite",
    batch_size: int = 5_000,
) -> str:
    """Write *df* to a Databricks Unity Catalog Delta table via SQL batch INSERT.

    Steps:
        1. CREATE OR REPLACE TABLE with schema inferred from dtypes.
        2. INSERT INTO … VALUES … in batches of *batch_size* rows.
        3. Verify final row count.

    Returns the fully-qualified table name.
    """
    catalog = catalog or os.environ["DATABRICKS_CATALOG"]
    schema = schema or os.environ["DATABRICKS_SCHEMA"]
    full_name = f"`{catalog}`.`{schema}`.`{table}`"

    w = _client()
    wid = _warehouse_id(w)
    print(f"      warehouse: {wid}")

    col_list = ", ".join(f"`{c}`" for c in df.columns)

    # Step 1: Create table
    print(f"      creating table {full_name} ...")
    _run_sql(w, wid, _create_ddl(df, full_name))

    # Step 2: Batch INSERT
    n = len(df)
    n_batches = math.ceil(n / batch_size)
    print(f"      inserting {n:,} rows in {n_batches} batches of {batch_size} ...")

    for i in range(n_batches):
        batch = df.iloc[i * batch_size : (i + 1) * batch_size]
        rows_sql = _build_rows(batch)
        insert_sql = (
            f"INSERT INTO {full_name} ({col_list}) VALUES\n"
            + ",\n".join(rows_sql)
        )
        _run_sql(w, wid, insert_sql)
        pct = (i + 1) / n_batches * 100
        elapsed_batches = i + 1
        print(
            f"      [{elapsed_batches:>4}/{n_batches}]  {pct:5.1f}%"
            f"  (~{(n_batches - elapsed_batches) * 2}s remaining)",
            end="\r",
            flush=True,
        )

    print()

    # Step 3: Verify
    count = _run_sql_scalar(w, wid, f"SELECT COUNT(*) FROM {full_name}")
    print(f"      verified: {count} rows in {full_name}")

    return f"{catalog}.{schema}.{table}"
