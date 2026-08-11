"""
Insurance Audit — reconciles portal_context.json against Databricks raw data.

For each check, compares a value in the generated JSON against a fresh SQL query.
Outputs audit_report.json in the project root.

Usage:
    python -m src.insurance_audit
    python -m src.insurance_audit --context portal_context.json --out audit_report.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_DEFAULT_CTX = PROJECT_ROOT / "portal_context.json"
_DEFAULT_OUT  = PROJECT_ROOT / "audit_report.json"

# Tolerance for numeric comparisons (relative)
_REL_TOL = 0.005   # 0.5%
_ABS_TOL = 0.1     # $0.1M absolute tolerance for tiny values


def _load_env():
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _connect():
    _load_env()
    os.environ["DATABRICKS_SCHEMA"] = "insurance_provider"
    from connectors.shared.databricks import _client, _warehouse_id
    w = _client()
    wid = _warehouse_id(w)
    catalog = os.environ.get("DATABRICKS_CATALOG", "prod_us_prismlatam_c30670d")
    return w, wid, catalog


def _query(w, wid, sql: str) -> pd.DataFrame:
    from databricks.sdk.service.sql import StatementState
    r = w.statement_execution.execute_statement(
        warehouse_id=wid, statement=sql, wait_timeout="60s"
    )
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"Query failed: {r.status.error}")
    if not r.result or not r.result.data_array:
        cols = [c.name for c in r.manifest.schema.columns]
        return pd.DataFrame(columns=cols)
    cols = [c.name for c in r.manifest.schema.columns]
    return pd.DataFrame(r.result.data_array, columns=cols)


def _check(checks: list, category: str, country: str, name: str,
           desc: str, dash_val, src_val, unit: str = "usd_mm"):
    """Append a check result."""
    if dash_val is None and src_val is None:
        status = "SKIP"
        delta_pct = None
    elif dash_val is None or src_val is None:
        status = "WARN"
        delta_pct = None
    else:
        try:
            d = float(dash_val)
            s = float(src_val)
            if abs(s) < 1e-9:
                status = "PASS" if abs(d - s) < _ABS_TOL else "FAIL"
                delta_pct = None
            else:
                delta_pct = round((d - s) / abs(s), 6)
                status = "PASS" if abs(delta_pct) <= _REL_TOL else ("WARN" if abs(delta_pct) <= 0.02 else "FAIL")
        except (TypeError, ValueError):
            status = "SKIP"
            delta_pct = None

    checks.append({
        "id":              f"{country.lower()}_{category}_{name}",
        "category":        category,
        "country":         country,
        "check_name":      name,
        "description":     desc,
        "unit":            unit,
        "dashboard_value": round(float(dash_val), 6) if dash_val is not None else None,
        "source_value":    round(float(src_val), 6) if src_val is not None else None,
        "delta_pct":       delta_pct,
        "status":          status,
    })


def run(ctx_path: Path = _DEFAULT_CTX, out_path: Path = _DEFAULT_OUT):
    print(f"\n=== Insurance Audit ===")
    print(f"  Context : {ctx_path}")

    with open(ctx_path) as f:
        ctx = json.load(f)

    year = ctx["metadata"]["period"]
    countries = ctx["metadata"]["available_countries"]
    print(f"  Period  : {year}")
    print(f"  Countries: {', '.join(countries)}")

    print("  Connecting to Databricks…")
    w, wid, catalog = _connect()
    pl_view = f"{catalog}.insurance_provider.vw_latam_all"
    bs_view = f"{catalog}.insurance_provider.vw_bg_latam_all"

    checks: list[dict] = []

    # ── 1. Market GWP reconciliation (per country) ─────────────────────────────
    print("  [1/8] Market GWP reconciliation…")
    sql = f"""
    SELECT pais, SUM(valor_usd) / 1e6 AS gwp_mm
    FROM {pl_view}
    WHERE anio = {year}
      AND cuenta_code = 'gross_premiums'
      AND (
        (pais != 'MX' AND is_total_lob = true)
        OR (pais = 'MX' AND (lob_l1_en IS NULL OR lob_l1_en NOT IN ('Total Portfolio')))
      )
    GROUP BY pais
    """
    df = _query(w, wid, sql)
    df["gwp_mm"] = pd.to_numeric(df["gwp_mm"], errors="coerce")
    for _, row in df.iterrows():
        pais = row["pais"]
        if pais not in countries:
            continue
        src_val  = row["gwp_mm"]
        dash_val = (ctx["country_data"][pais]["market"]["metrics"] or {}).get("gwp_usd_mm")
        _check(checks, "gwp_market", pais, "gwp_total",
               f"{pais} {year} market GWP vs Databricks", dash_val, src_val)

    # ── 2. Company GWP spot checks (top 3 per country) ────────────────────────
    print("  [2/8] Company GWP spot checks (top 3 per country)…")
    sql = f"""
    SELECT pais, empresa, SUM(valor_usd) / 1e6 AS gwp_mm
    FROM {pl_view}
    WHERE anio = {year}
      AND cuenta_code = 'gross_premiums'
      AND (
        (pais != 'MX' AND is_total_lob = true)
        OR (pais = 'MX' AND (lob_l1_en IS NULL OR lob_l1_en NOT IN ('Total Portfolio')))
      )
    GROUP BY pais, empresa
    """
    df_co = _query(w, wid, sql)
    df_co["gwp_mm"] = pd.to_numeric(df_co["gwp_mm"], errors="coerce")
    for pais in countries:
        co_src = df_co[df_co["pais"] == pais].sort_values("gwp_mm", ascending=False).head(3)
        insurers = ctx["country_data"][pais]["insurers"][:5]
        for _, src_row in co_src.iterrows():
            co_name  = src_row["empresa"]
            src_gwp  = src_row["gwp_mm"]
            dash_ins = next((i for i in insurers if i["name"] == co_name), None)
            dash_gwp = (dash_ins["metrics"] or {}).get("gwp_usd_mm") if dash_ins else None
            safe_name = co_name.replace(" ", "_")[:20]
            _check(checks, "gwp_company", pais, safe_name,
                   f"{pais} company '{co_name}' GWP vs Databricks", dash_gwp, src_gwp)

    # ── 3. Loss ratio formula check (market level) ────────────────────────────
    print("  [3/8] Loss ratio formula checks…")
    sql = f"""
    SELECT pais,
           SUM(CASE WHEN cuenta_code = 'claims_incurred'  THEN valor_usd END) AS claims,
           SUM(CASE WHEN cuenta_code = 'gross_premiums'   THEN valor_usd END) AS gwp
    FROM {pl_view}
    WHERE anio = {year}
      AND cuenta_code IN ('claims_incurred', 'gross_premiums')
      AND (
        (pais != 'MX' AND is_total_lob = true)
        OR (pais = 'MX' AND (lob_l1_en IS NULL OR lob_l1_en NOT IN ('Total Portfolio')))
      )
    GROUP BY pais
    """
    df_lr = _query(w, wid, sql)
    for col in ("claims", "gwp"):
        df_lr[col] = pd.to_numeric(df_lr[col], errors="coerce")
    df_lr["loss_ratio"] = df_lr["claims"] / df_lr["gwp"]
    for _, row in df_lr.iterrows():
        pais = row["pais"]
        if pais not in countries:
            continue
        src_val  = row["loss_ratio"]
        dash_val = (ctx["country_data"][pais]["market"]["metrics"] or {}).get("loss_ratio_gross")
        _check(checks, "loss_ratio", pais, "gross",
               f"{pais} market gross loss ratio formula", dash_val, src_val, unit="ratio")

    # ── 4. Retention rate formula check ───────────────────────────────────────
    print("  [4/8] Retention rate formula checks…")
    sql = f"""
    SELECT pais,
           SUM(CASE WHEN cuenta_code = 'net_retained_premiums' THEN valor_usd END) AS nrp,
           SUM(CASE WHEN cuenta_code = 'gross_premiums'        THEN valor_usd END) AS gwp
    FROM {pl_view}
    WHERE anio = {year}
      AND cuenta_code IN ('net_retained_premiums', 'gross_premiums')
      AND (
        (pais != 'MX' AND is_total_lob = true)
        OR (pais = 'MX' AND (lob_l1_en IS NULL OR lob_l1_en NOT IN ('Total Portfolio')))
      )
    GROUP BY pais
    """
    df_ret = _query(w, wid, sql)
    for col in ("nrp", "gwp"):
        df_ret[col] = pd.to_numeric(df_ret[col], errors="coerce")
    df_ret["retention"] = df_ret["nrp"] / df_ret["gwp"]
    for _, row in df_ret.iterrows():
        pais = row["pais"]
        if pais not in countries:
            continue
        src_val  = row["retention"]
        dash_val = (ctx["country_data"][pais]["market"]["metrics"] or {}).get("retention_rate")
        _check(checks, "retention_rate", pais, "market",
               f"{pais} market retention rate formula", dash_val, src_val, unit="ratio")

    # ── 5. Technical margin formula check ─────────────────────────────────────
    print("  [5/8] Technical margin checks…")
    sql = f"""
    SELECT pais,
           SUM(CASE WHEN cuenta_code = 'technical_profit_loss' THEN valor_usd END) AS tp,
           SUM(CASE WHEN cuenta_code = 'gross_premiums'        THEN valor_usd END) AS gwp
    FROM {pl_view}
    WHERE anio = {year}
      AND cuenta_code IN ('technical_profit_loss', 'gross_premiums')
      AND (
        (pais != 'MX' AND is_total_lob = true)
        OR (pais = 'MX' AND (lob_l1_en IS NULL OR lob_l1_en NOT IN ('Total Portfolio')))
      )
    GROUP BY pais
    """
    df_tm = _query(w, wid, sql)
    for col in ("tp", "gwp"):
        df_tm[col] = pd.to_numeric(df_tm[col], errors="coerce")
    df_tm["tech_margin"] = df_tm["tp"] / df_tm["gwp"]
    for _, row in df_tm.iterrows():
        pais = row["pais"]
        if pais not in countries:
            continue
        src_val  = row["tech_margin"]
        dash_val = (ctx["country_data"][pais]["market"]["metrics"] or {}).get("technical_margin")
        _check(checks, "technical_margin", pais, "market",
               f"{pais} market technical margin formula", dash_val, src_val, unit="ratio")

    # ── 6. Balance sheet: total assets (market level) ─────────────────────────
    print("  [6/8] Balance sheet reconciliation…")
    sql = f"""
    SELECT pais, SUM(valor_usd) / 1e6 AS assets_mm
    FROM {bs_view}
    WHERE anio = {year}
      AND cuenta_code = 'total_assets'
    GROUP BY pais
    """
    df_bs = _query(w, wid, sql)
    df_bs["assets_mm"] = pd.to_numeric(df_bs["assets_mm"], errors="coerce")
    for _, row in df_bs.iterrows():
        pais = row["pais"]
        if pais not in countries:
            continue
        src_val  = row["assets_mm"]
        dash_val = (ctx["country_data"][pais]["market"]["metrics"] or {}).get("total_assets_usd_mm")
        _check(checks, "balance_sheet", pais, "total_assets",
               f"{pais} total assets vs Databricks BS", dash_val, src_val)

    # ── 7. LoB GWP sum vs market total ────────────────────────────────────────
    print("  [7/8] LoB GWP sum checks…")
    sql = f"""
    SELECT pais, SUM(valor_usd) / 1e6 AS lob_gwp_mm
    FROM {pl_view}
    WHERE anio = {year}
      AND cuenta_code = 'gross_premiums'
      AND is_total_lob = false
      AND lob_l1_en IS NOT NULL
      AND lob_l1_en != 'Total Portfolio'
    GROUP BY pais
    """
    df_lob = _query(w, wid, sql)
    df_lob["lob_gwp_mm"] = pd.to_numeric(df_lob["lob_gwp_mm"], errors="coerce")
    for _, row in df_lob.iterrows():
        pais = row["pais"]
        if pais not in countries:
            continue
        src_val  = row["lob_gwp_mm"]
        la = ctx["country_data"][pais].get("lob_analysis") or {}
        dash_val = la.get("total_gwp_usd_mm")
        _check(checks, "lob_gwp_sum", pais, "lob_total",
               f"{pais} LoB GWP sum vs market total", dash_val, src_val)

    # ── 8. FX rate spot check ─────────────────────────────────────────────────
    print("  [8/8] FX rate spot checks…")
    sql = f"""
    SELECT pais, AVG(fx_rate) AS avg_fx, MIN(fx_rate) AS min_fx, MAX(fx_rate) AS max_fx,
           COUNT(DISTINCT fx_rate) AS n_distinct_fx
    FROM {pl_view}
    WHERE anio = {year}
      AND cuenta_code = 'gross_premiums'
      AND is_total_lob = true
    GROUP BY pais
    """
    df_fx = _query(w, wid, sql)
    for col in ("avg_fx", "min_fx", "max_fx"):
        df_fx[col] = pd.to_numeric(df_fx[col], errors="coerce")
    fx_checks = []
    for _, row in df_fx.iterrows():
        pais = row["pais"]
        if pais not in countries:
            continue
        n_distinct = int(row.get("n_distinct_fx", 0) or 0)
        # FX should be consistent within a year (1 or very few distinct values)
        fx_consistent = n_distinct <= 2
        fx_checks.append({
            "country": pais,
            "avg_fx":  float(row["avg_fx"]) if row["avg_fx"] else None,
            "min_fx":  float(row["min_fx"]) if row["min_fx"] else None,
            "max_fx":  float(row["max_fx"]) if row["max_fx"] else None,
            "n_distinct": n_distinct,
            "consistent": fx_consistent,
        })
        status = "PASS" if fx_consistent else "WARN"
        checks.append({
            "id":              f"{pais.lower()}_fx_rate_consistency",
            "category":        "fx_rate",
            "country":         pais,
            "check_name":      "fx_consistency",
            "description":     f"{pais} FX rate consistency within {year}",
            "unit":            "fx_rate",
            "dashboard_value": None,
            "source_value":    float(row["avg_fx"]) if row["avg_fx"] else None,
            "delta_pct":       None,
            "status":          status,
            "detail":          f"avg={row['avg_fx']:.4f}, min={row['min_fx']:.4f}, max={row['max_fx']:.4f}, n_distinct={n_distinct}",
        })

    # ── Compile summary ────────────────────────────────────────────────────────
    total   = len(checks)
    passed  = sum(1 for c in checks if c["status"] == "PASS")
    warned  = sum(1 for c in checks if c["status"] == "WARN")
    failed  = sum(1 for c in checks if c["status"] == "FAIL")
    skipped = sum(1 for c in checks if c["status"] == "SKIP")

    report = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "context_file": str(ctx_path),
        "period": year,
        "countries": countries,
        "tolerances": {"relative_pct": _REL_TOL, "absolute_usd_mm": _ABS_TOL},
        "summary": {
            "total":   total,
            "passed":  passed,
            "warned":  warned,
            "failed":  failed,
            "skipped": skipped,
            "overall": "PASS" if failed == 0 and warned <= 2 else ("WARN" if failed == 0 else "FAIL"),
        },
        "checks": checks,
        "fx_detail": fx_checks,
    }

    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\n  Results: {passed} PASS | {warned} WARN | {failed} FAIL | {skipped} SKIP")
    print(f"  Overall : {report['summary']['overall']}")
    print(f"  Report  : {out_path}")

    if failed > 0:
        print("\n  FAILED CHECKS:")
        for c in checks:
            if c["status"] == "FAIL":
                print(f"    [{c['country']}] {c['description']}: dash={c['dashboard_value']}, src={c['source_value']}, delta={c['delta_pct']:.2%}")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", default=str(_DEFAULT_CTX))
    parser.add_argument("--out",     default=str(_DEFAULT_OUT))
    args = parser.parse_args()
    run(Path(args.context), Path(args.out))
