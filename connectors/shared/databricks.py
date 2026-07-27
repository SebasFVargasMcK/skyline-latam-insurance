"""Databricks upload helpers shared by all connectors.

Requires environment variables:
    DATABRICKS_HOST  — e.g. https://adb-<id>.azuredatabricks.net
    DATABRICKS_TOKEN — personal access token or service principal secret
    DATABRICKS_CATALOG, DATABRICKS_SCHEMA — Unity Catalog target
"""

import os

import pandas as pd


def _get_spark():
    """Return an active SparkSession (works both in Databricks and locally via databricks-connect)."""
    try:
        from pyspark.sql import SparkSession  # type: ignore

        return SparkSession.builder.getOrCreate()
    except ImportError as exc:
        raise RuntimeError(
            "pyspark is not installed. Run: pip install pyspark databricks-connect"
        ) from exc


def upload_dataframe(
    df: pd.DataFrame,
    table: str,
    *,
    catalog: str | None = None,
    schema: str | None = None,
    mode: str = "overwrite",
) -> str:
    """Write *df* to a Databricks Unity Catalog table.

    Returns the fully-qualified table name that was written.
    """
    catalog = catalog or os.environ["DATABRICKS_CATALOG"]
    schema = schema or os.environ["DATABRICKS_SCHEMA"]
    full_name = f"{catalog}.{schema}.{table}"

    spark = _get_spark()
    sdf = spark.createDataFrame(df)
    sdf.write.format("delta").mode(mode).saveAsTable(full_name)
    return full_name
