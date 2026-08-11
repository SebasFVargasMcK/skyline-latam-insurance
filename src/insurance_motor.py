"""
Insurance Motor — generates portal_context.json for the LATAM insurance dashboard.

Reads from:
  {catalog}.insurance_provider.vw_er_completo_all (P&L — richer accounts from full income-statement files)
  {catalog}.insurance_provider.vw_latam_all       (LoB breakdown — AR/CL/CO/EC/PE + MX)
  {catalog}.insurance_provider.vw_bg_latam_all    (Balance sheet, year-end)

Outputs:
  portal_context.json  — unified JSON powering the region/country toggle dashboard.

Structure:
  contract       — version metadata
  metadata       — available countries, years, currency
  region         — LATAM view: countries as entities (6 data points per metric/year)
  country_data   — per-country view: insurers as entities
  quality        — data quality flags per country
  contract_validation

Usage:
    python -m src.insurance_motor
    python -m src.insurance_motor --year 2025 --out portal_context.json
    python -m src.insurance_motor --dry-run   (no Databricks, uses cached parquet if available)
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

_DEFAULT_OUT = PROJECT_ROOT / "portal_context.json"
_DEFAULT_YEAR = 2025
_HISTORY_YEARS = 5          # include last N years in annual history
_MAX_INSURERS_PER_COUNTRY = 60   # cap for the insurer list in the JSON

_COUNTRY_META = {
    "AR": {"name": "Argentina",  "regulator": "SSN",  "currency_local": "ARS"},
    "CL": {"name": "Chile",      "regulator": "CMF",  "currency_local": "CLP"},
    "CO": {"name": "Colombia",   "regulator": "SFC",  "currency_local": "COP"},
    "EC": {"name": "Ecuador",    "regulator": "SCVS", "currency_local": "USD"},
    "MX": {"name": "Mexico",     "regulator": "CNSF", "currency_local": "MXN"},
    "PE": {"name": "Peru",       "regulator": "SBS",  "currency_local": "PEN"},
}

_COUNTRY_COLORS = {
    "AR": "#58a6ff",   # blue
    "CL": "#40d19b",   # green
    "CO": "#f0b84d",   # gold
    "EC": "#ff5f67",   # red
    "MX": "#8b7cff",   # purple
    "PE": "#2dd4bf",   # teal
}

# Accounts used for derived metrics — only these are pulled from the DB
_KEY_ACCOUNTS = [
    "gross_premiums",
    "net_retained_premiums",
    "net_earned_premiums",
    "ceded_premiums",
    "assumed_business",
    "claims_incurred",
    "net_claims_and_policy_obligations",
    "agent_commissions",
    "net_acquisition_costs",
    "reinsurance_commissions_received",
    "admin_expenses",
    "technical_profit_loss",
    "operating_profit_loss",
    "financial_and_investment_result",
    "net_income_loss",
]

_GROUPS_CONFIG_PATH = PROJECT_ROOT / "config" / "insurer_groups.json"
_MARKET_TOTAL_NAMES = frozenset({"TOTAL DE MERCADO", "TOTALES", "TOTAL MERCADO", "MERCADO TOTAL"})

_BS_ACCOUNTS = [
    "total_assets",
    "total_investments",
    "technical_reserves",
    "unearned_premium_reserve",
    "claims_reserve",
    "shareholders_equity",
    "paid_in_capital",
    "retained_earnings",
    # Some countries (AR, CL, EC, PE) include P&L accounts in balance sheet uploads
    "net_income_loss",
    "financial_and_investment_result",
]

# ── Data quality: sanity bounds per derived metric ────────────────────────────
# Values outside [lo, hi] are nulled out (flagged in quality section)
_METRIC_BOUNDS = {
    "retention_rate":       (0.0,  1.5),
    "cession_rate":         (0.0,  1.5),
    "loss_ratio_gross":     (0.0,  3.0),
    "loss_ratio_net":       (0.0,  3.0),
    "combined_ratio_gross": (0.0,  4.0),
    "commission_ratio":     (0.0,  1.0),
    "admin_expense_ratio":  (0.0,  1.0),
    "technical_margin":    (-2.0,  1.0),
}


# ── Helpers ───────────────────────────────────────────────────────────────────

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
        warehouse_id=wid, statement=sql, wait_timeout="50s"
    )
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"Query failed: {r.status.error}")
    if not r.result or not r.result.data_array:
        cols = [c.name for c in r.manifest.schema.columns]
        return pd.DataFrame(columns=cols)
    cols = [c.name for c in r.manifest.schema.columns]
    return pd.DataFrame(r.result.data_array, columns=cols)


def _safe(val, lo, hi):
    """Return val if within [lo, hi], else None."""
    if val is None or (isinstance(val, float) and (val != val)):
        return None
    return float(val) if lo <= val <= hi else None


def _pct(num, den, bounds=None):
    """Safe ratio, returns None on zero/missing denominator."""
    if den is None or den == 0 or num is None:
        return None
    v = float(num) / float(den)
    if bounds:
        return _safe(v, *bounds)
    return v


def _round(v, digits=4):
    if v is None:
        return None
    return round(float(v), digits)


# ── Pull raw data from Databricks ─────────────────────────────────────────────

def _pull_pl(w, wid, catalog: str, min_year: int, max_year: int) -> pd.DataFrame:
    """P&L totals: company × year × account.
    Reads from vw_er_completo_all (richer account coverage from full income-statement files).
    Non-MX: use is_total_lob=true rows (pre-aggregated market total).
    MX:     aggregate LoB-level rows; exclude 'Total Portfolio' to avoid double-counting.
    """
    accts = "', '".join(_KEY_ACCOUNTS)
    sql = f"""
    SELECT pais, empresa, anio, cuenta_code,
           SUM(valor_usd) AS valor_usd,
           SUM(valor_local) AS valor_local,
           AVG(fx_rate) AS fx_rate,
           MAX(moneda_local) AS moneda_local
    FROM {catalog}.insurance_provider.vw_er_completo_all
    WHERE anio BETWEEN {min_year} AND {max_year}
      AND cuenta_code IN ('{accts}')
      AND (
        (pais != 'MX' AND is_total_lob = true)
        OR (pais = 'MX' AND (lob_l1_en IS NULL OR lob_l1_en NOT IN ('Total Portfolio')))
      )
    GROUP BY pais, empresa, anio, cuenta_code
    """
    df = _query(w, wid, sql)
    for col in ("valor_usd", "valor_local", "fx_rate"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["anio"] = pd.to_numeric(df["anio"], errors="coerce").astype("Int64")
    return df


def _pull_pl_lob(w, wid, catalog: str, min_year: int, max_year: int) -> pd.DataFrame:
    """P&L by LoB: company × year × lob × gross_premiums only."""
    sql = f"""
    SELECT pais, empresa, anio, lob_l1_en, lob_l1_es,
           SUM(CASE WHEN cuenta_code = 'gross_premiums' THEN valor_usd END) AS gwp_usd,
           SUM(CASE WHEN cuenta_code = 'net_retained_premiums' THEN valor_usd END) AS net_premiums_usd,
           SUM(CASE WHEN cuenta_code = 'claims_incurred' THEN valor_usd END) AS claims_usd
    FROM {catalog}.insurance_provider.vw_latam_all
    WHERE is_total_lob = false
      AND lob_l1_en IS NOT NULL
      AND lob_l1_en != 'Total Portfolio'
      AND anio BETWEEN {min_year} AND {max_year}
      AND cuenta_code IN ('gross_premiums', 'net_retained_premiums', 'claims_incurred')
    GROUP BY pais, empresa, anio, lob_l1_en, lob_l1_es
    """
    df = _query(w, wid, sql)
    for col in ("gwp_usd", "net_premiums_usd", "claims_usd"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["anio"] = pd.to_numeric(df["anio"], errors="coerce").astype("Int64")
    return df


def _pull_bs(w, wid, catalog: str, min_year: int, max_year: int) -> pd.DataFrame:
    """Balance sheet: company × year × account."""
    accts = "', '".join(_BS_ACCOUNTS)
    sql = f"""
    SELECT pais, empresa, anio, cuenta_code,
           AVG(valor_usd) AS valor_usd
    FROM {catalog}.insurance_provider.vw_bg_latam_all
    WHERE anio BETWEEN {min_year} AND {max_year}
      AND cuenta_code IN ('{accts}')
    GROUP BY pais, empresa, anio, cuenta_code
    """
    df = _query(w, wid, sql)
    df["valor_usd"] = pd.to_numeric(df["valor_usd"], errors="coerce")
    df["anio"] = pd.to_numeric(df["anio"], errors="coerce").astype("Int64")
    return df


# ── Metric computation ────────────────────────────────────────────────────────

def _pivot_pl(df_pl: pd.DataFrame) -> pd.DataFrame:
    """Pivot P&L from long to wide (one row per pais/empresa/anio)."""
    return df_pl.pivot_table(
        index=["pais", "empresa", "anio"],
        columns="cuenta_code",
        values="valor_usd",
        aggfunc="sum",
    ).reset_index()


def _pivot_bs(df_bs: pd.DataFrame) -> pd.DataFrame:
    return df_bs.pivot_table(
        index=["pais", "empresa", "anio"],
        columns="cuenta_code",
        values="valor_usd",
        aggfunc="mean",
    ).reset_index()


def _get(df, col, default=None):
    return df.get(col, pd.Series([default] * len(df), index=df.index))


def _compute_metrics(wide_pl: pd.DataFrame, wide_bs: pd.DataFrame) -> pd.DataFrame:
    """Merge P&L + BS and compute all derived metrics. Returns one row per entity/year.

    net_income_loss and financial_and_investment_result may appear in both views
    (P&L view for CO/MX; balance sheet upload for AR/CL/EC/PE). Rename BS copies
    before merging so pandas doesn't suffix-collide, then coalesce: prefer the
    P&L value when non-null/non-zero, fall back to the BS value.
    """
    _bs = wide_bs.rename(columns={
        "net_income_loss":                "_bs_net_income_loss",
        "financial_and_investment_result": "_bs_financial_result",
    })
    df = wide_pl.merge(_bs, on=["pais", "empresa", "anio"], how="left")

    gp  = _get(df, "gross_premiums")
    nrp = _get(df, "net_retained_premiums")
    nep = _get(df, "net_earned_premiums")
    cp  = _get(df, "ceded_premiums")
    ci  = _get(df, "claims_incurred")
    nc  = _get(df, "net_claims_and_policy_obligations")
    ac  = _get(df, "agent_commissions")
    nac = _get(df, "net_acquisition_costs")
    ae  = _get(df, "admin_expenses")
    tp  = _get(df, "technical_profit_loss")
    op  = _get(df, "operating_profit_loss")

    # Coalesce: P&L source preferred; fall back to balance sheet upload when P&L is null/zero
    _fi_pl = _get(df, "financial_and_investment_result")
    _fi_bs = _get(df, "_bs_financial_result")
    fi = _fi_pl.where(_fi_pl.notna() & (_fi_pl != 0), _fi_bs)

    _ni_pl = _get(df, "net_income_loss")
    _ni_bs = _get(df, "_bs_net_income_loss")
    ni = _ni_pl.where(_ni_pl.notna() & (_ni_pl != 0), _ni_bs)

    ta  = _get(df, "total_assets")
    ti  = _get(df, "total_investments")
    tr  = _get(df, "technical_reserves")
    eq  = _get(df, "shareholders_equity")

    out = df[["pais", "empresa", "anio"]].copy()

    # Premium metrics (in USD millions)
    out["gwp_usd_mm"]             = (gp  / 1e6).round(4)
    out["net_premiums_usd_mm"]    = (nrp / 1e6).round(4)
    out["ceded_premiums_usd_mm"]  = (cp  / 1e6).round(4)
    out["net_earned_usd_mm"]      = (nep / 1e6).round(4)

    # Ratios
    out["retention_rate"]         = (nrp / gp).clip(0, 1.5).where(gp > 0)
    out["cession_rate"]           = (cp  / gp).clip(0, 1.5).where(gp > 0)

    is_mx = df["pais"] == "MX"

    # Loss ratio
    # MX (CNSF): net_claims / net_retained_premiums (Costo Neto Siniestralidad / Primas Retención)
    # Other:     claims_incurred / net_retained_premiums (same denominator per regulator convention)
    out["loss_ratio_gross"] = pd.Series(dtype=float, index=df.index)
    out.loc[~is_mx, "loss_ratio_gross"] = (ci[~is_mx] / nrp[~is_mx]).clip(0, 3.0).where(nrp[~is_mx] > 0)
    out.loc[is_mx,  "loss_ratio_gross"] = (nc[is_mx]  / nrp[is_mx] ).clip(0, 3.0).where(nrp[is_mx]  > 0)

    # net_claims is unreliable for CO (unit mismatch) — bound tightly
    out["loss_ratio_net"]         = (nc  / nep).clip(0, 2.5).where(nep > 0)
    out["loss_ratio_net"]         = out["loss_ratio_net"].where(out["loss_ratio_net"] < 2.0)

    # Expense ratios (on net retained premiums / NPE)
    # MX (CNSF): Costo de Adquisicion / NPE,  Gastos de Administracion / NPE
    # Other: same formula, same accounts
    out["commission_ratio"]       = (ac  / nrp).clip(0, 1.0).where(nrp > 0)
    out["net_acq_cost_ratio"]     = (nac / nrp).clip(0, 1.0).where(nrp > 0)
    out["admin_expense_ratio"]    = (ae  / nrp).clip(0, 1.0).where(nrp > 0)

    # Combined ratio — components-first, then algebraic fallback from technical result.
    # MX: net_claims/nrp + net_acq/nrp + admin/nrp (admin absent from gold layer → always null here)
    # Other: claims/gwp + net_acq/nrp + admin/nrp
    out["combined_ratio_gross"]   = (
        out["loss_ratio_gross"] + out["net_acq_cost_ratio"] + out["admin_expense_ratio"]
    ).clip(0, 4.0)
    # Algebraic fallback: CoR = 1 − (technical_result / net_retained_premiums)
    # Used when components are partially missing but technical_profit_loss is available.
    # Applies to MX (no admin_expenses in gold layer) and any country still missing components.
    _cor_algebraic = (1.0 - (tp / nrp)).clip(0, 4.0).where(nrp > 0)
    out["combined_ratio_gross"] = out["combined_ratio_gross"].where(
        out["combined_ratio_gross"].notna(), _cor_algebraic
    )

    # Profitability
    out["technical_result_usd_mm"] = (tp / 1e6).round(4)
    out["operating_result_usd_mm"] = (op / 1e6).round(4)
    out["technical_margin"]         = (tp / gp).clip(-2, 1).where(gp > 0)

    # Financial / investment
    out["financial_result_usd_mm"]  = (fi / 1e6).round(4)
    out["net_income_usd_mm"]         = (ni / 1e6).round(4)

    # Balance sheet (USD millions)
    out["total_assets_usd_mm"]      = (ta / 1e6).round(4)
    out["total_investments_usd_mm"]  = (ti / 1e6).round(4)
    out["technical_reserves_usd_mm"] = (tr / 1e6).round(4)
    out["equity_usd_mm"]             = (eq / 1e6).round(4)

    # Solvency / leverage
    out["asset_leverage"]            = (ta / eq).clip(0, 50).where(eq > 0)
    out["reserves_to_premiums"]      = (tr / gp).clip(0, 10).where(gp > 0)

    # ROE (only meaningful where net_income is available)
    out["roe"]                       = (ni / eq).clip(-2, 2).where(eq.abs() > 1e6)

    return out


def _load_insurer_groups() -> dict:
    """Load config/insurer_groups.json → {pais: {empresa: group_name}}."""
    if not _GROUPS_CONFIG_PATH.exists():
        return {}
    data = json.loads(_GROUPS_CONFIG_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _build_group_metrics(
    df_pl: pd.DataFrame,
    df_bs: pd.DataFrame,
    groups_map: dict,
) -> pd.DataFrame:
    """Aggregate P&L and BS dollar amounts to group level, then re-compute all metrics.

    Dollar amounts are summed across companies in the same parent group.
    Ratios are re-derived from aggregated totals so they are economically
    correct (e.g. group loss ratio = total group claims / total group GWP).
    Companies not in groups_map remain as themselves (solo operators).
    """
    pl = df_pl[~df_pl["empresa"].str.strip().str.upper().isin(_MARKET_TOTAL_NAMES)].copy()
    bs = df_bs[~df_bs["empresa"].str.strip().str.upper().isin(_MARKET_TOTAL_NAMES)].copy()

    # Build flat lookup: (pais, empresa) → group_name
    grp_lookup = {
        (pais, empresa): group
        for pais, mappings in groups_map.items()
        for empresa, group in mappings.items()
    }

    pl["empresa"] = [
        grp_lookup.get((r.pais, r.empresa), r.empresa)
        for r in pl.itertuples(index=False)
    ]
    bs["empresa"] = [
        grp_lookup.get((r.pais, r.empresa), r.empresa)
        for r in bs.itertuples(index=False)
    ]

    grp_pl = (
        pl.groupby(["pais", "empresa", "anio", "cuenta_code"], as_index=False)
        .agg(valor_usd=("valor_usd", "sum"))
    )
    grp_bs = (
        bs.groupby(["pais", "empresa", "anio", "cuenta_code"], as_index=False)
        .agg(valor_usd=("valor_usd", "sum"))
    )

    wide_pl = _pivot_pl(grp_pl)
    wide_bs = _pivot_bs(grp_bs)
    return _compute_metrics(wide_pl, wide_bs)


# ── JSON builders ─────────────────────────────────────────────────────────────

def _to_float(v):
    """Convert pandas scalar to Python float or None."""
    if v is None or (isinstance(v, float) and v != v):
        return None
    try:
        f = float(v)
        return None if f != f else round(f, 6)
    except (TypeError, ValueError):
        return None


def _metrics_dict(row: pd.Series, keys: list[str]) -> dict:
    return {k: _to_float(row.get(k)) for k in keys}


_INSURER_METRIC_KEYS = [
    "gwp_usd_mm", "net_premiums_usd_mm", "ceded_premiums_usd_mm", "net_earned_usd_mm",
    "retention_rate", "cession_rate",
    "loss_ratio_gross", "loss_ratio_net",
    "commission_ratio", "net_acq_cost_ratio", "admin_expense_ratio",
    "combined_ratio_gross",
    "technical_result_usd_mm", "technical_margin",
    "financial_result_usd_mm", "net_income_usd_mm",
    "total_assets_usd_mm", "total_investments_usd_mm",
    "technical_reserves_usd_mm", "equity_usd_mm",
    "asset_leverage", "reserves_to_premiums", "roe",
]

_METRIC_LABELS = {
    "gwp_usd_mm":              {"label_en": "Gross Written Premiums",      "unit": "usd_million", "group": "premiums",    "direction": "up"},
    "net_premiums_usd_mm":     {"label_en": "Net Retained Premiums",        "unit": "usd_million", "group": "premiums",    "direction": "up"},
    "ceded_premiums_usd_mm":   {"label_en": "Ceded Reinsurance Premiums",   "unit": "usd_million", "group": "premiums",    "direction": "neutral"},
    "net_earned_usd_mm":       {"label_en": "Net Earned Premiums",          "unit": "usd_million", "group": "premiums",    "direction": "up"},
    "retention_rate":          {"label_en": "Retention Rate",               "unit": "ratio",       "group": "premiums",    "direction": "up"},
    "cession_rate":            {"label_en": "Cession Rate",                 "unit": "ratio",       "group": "premiums",    "direction": "down"},
    "loss_ratio_gross":        {"label_en": "Loss Ratio (Gross)",           "unit": "ratio",       "group": "underwriting","direction": "down"},
    "loss_ratio_net":          {"label_en": "Loss Ratio (Net)",             "unit": "ratio",       "group": "underwriting","direction": "down"},
    "commission_ratio":        {"label_en": "Commission Ratio",             "unit": "ratio",       "group": "underwriting","direction": "down"},
    "net_acq_cost_ratio":      {"label_en": "Net Acquisition Cost Ratio",   "unit": "ratio",       "group": "underwriting","direction": "down"},
    "admin_expense_ratio":     {"label_en": "Admin Expense Ratio",          "unit": "ratio",       "group": "underwriting","direction": "down"},
    "combined_ratio_gross":    {"label_en": "Combined Ratio (Gross)",       "unit": "ratio",       "group": "underwriting","direction": "down"},
    "technical_result_usd_mm": {"label_en": "Technical Result",             "unit": "usd_million", "group": "profitability","direction": "up"},
    "technical_margin":        {"label_en": "Technical Margin",             "unit": "ratio",       "group": "profitability","direction": "up"},
    "financial_result_usd_mm": {"label_en": "Financial & Investment Result","unit": "usd_million", "group": "profitability","direction": "up"},
    "net_income_usd_mm":       {"label_en": "Net Income",                   "unit": "usd_million", "group": "profitability","direction": "up"},
    "total_assets_usd_mm":     {"label_en": "Total Assets",                 "unit": "usd_million", "group": "balance_sheet","direction": "up"},
    "total_investments_usd_mm":{"label_en": "Total Investments",            "unit": "usd_million", "group": "balance_sheet","direction": "up"},
    "technical_reserves_usd_mm":{"label_en": "Technical Reserves",         "unit": "usd_million", "group": "balance_sheet","direction": "neutral"},
    "equity_usd_mm":           {"label_en": "Shareholders' Equity",         "unit": "usd_million", "group": "balance_sheet","direction": "up"},
    "asset_leverage":          {"label_en": "Asset Leverage (Assets/Equity)","unit": "multiple",   "group": "balance_sheet","direction": "neutral"},
    "reserves_to_premiums":    {"label_en": "Reserves to Premiums",         "unit": "multiple",    "group": "balance_sheet","direction": "neutral"},
    "roe":                     {"label_en": "Return on Equity (ROE)",        "unit": "ratio",       "group": "profitability","direction": "up"},
}


def _build_insurer_profile(row: pd.Series, mkt_row: pd.Series | None) -> dict:
    mkt_gwp = _to_float(mkt_row["gwp_usd_mm"]) if mkt_row is not None else None
    gwp     = _to_float(row["gwp_usd_mm"])
    share   = _pct(gwp, mkt_gwp) if mkt_gwp and mkt_gwp > 0 else None

    return {
        "name":          str(row["empresa"]),
        "alias":         str(row["empresa"]),
        "is_market":     bool(row.get("_is_market", False)),
        "period":        int(row["anio"]),
        "market_share":  _round(share, 4),
        "metrics":       _metrics_dict(row, _INSURER_METRIC_KEYS),
    }


def _build_insurer_lob(df_lob: pd.DataFrame, pais: str, empresa: str, year: int) -> list[dict]:
    """Per-company LoB breakdown for the focus year."""
    sub = df_lob[
        (df_lob["pais"] == pais) &
        (df_lob["empresa"] == empresa) &
        (df_lob["anio"] == year)
    ]
    if sub.empty:
        return []
    total_gwp = sub["gwp_usd"].sum()
    result = []
    for _, row in sub.sort_values("gwp_usd", ascending=False).iterrows():
        gwp = _to_float(row["gwp_usd"])
        nrp = _to_float(row.get("net_premiums_usd"))
        ci  = _to_float(row.get("claims_usd"))
        result.append({
            "lob_key":          str(row["lob_l1_en"]),
            "lob_label_en":     str(row["lob_l1_en"]),
            "lob_label_es":     str(row["lob_l1_es"]),
            "gwp_usd_mm":       _round(gwp / 1e6 if gwp else None, 4),
            "gwp_mix":          _round(_pct(gwp, total_gwp), 4),
            "retention_rate":   _round(_pct(nrp, gwp), 4),
            "loss_ratio_gross": _round(_pct(ci, gwp), 4),
        })
    return result


def _build_annual_history(metrics_df: pd.DataFrame, entity_col: str) -> list[dict]:
    """Build annual history list for a set of entities."""
    history = []
    for year, grp in metrics_df.groupby("anio"):
        year_entry = {"year": int(year), "entities": []}
        for _, row in grp.iterrows():
            mkt_gwp = None  # caller can add market total if needed
            year_entry["entities"].append({
                "key":     str(row[entity_col]),
                "metrics": _metrics_dict(row, _INSURER_METRIC_KEYS),
            })
        history.append(year_entry)
    return sorted(history, key=lambda x: x["year"])


def _build_lob_analysis(df_lob: pd.DataFrame, pais: str, year: int) -> dict:
    """Build LoB analysis for a country × year."""
    sub = df_lob[(df_lob["pais"] == pais) & (df_lob["anio"] == year)].copy()
    if sub.empty:
        return {"available": False}

    # Market total by LoB (sum all companies)
    mkt = sub.groupby(["lob_l1_en", "lob_l1_es"], as_index=False).agg(
        gwp_usd=("gwp_usd", "sum"),
        net_premiums_usd=("net_premiums_usd", "sum"),
        claims_usd=("claims_usd", "sum"),
    )
    total_gwp = mkt["gwp_usd"].sum()

    lob_list = []
    for _, row in mkt.sort_values("gwp_usd", ascending=False).iterrows():
        gwp = _to_float(row["gwp_usd"])
        nrp = _to_float(row["net_premiums_usd"])
        ci  = _to_float(row["claims_usd"])
        lob_list.append({
            "lob_key":         str(row["lob_l1_en"]),
            "lob_label_en":    str(row["lob_l1_en"]),
            "lob_label_es":    str(row["lob_l1_es"]),
            "gwp_usd_mm":      _round(gwp / 1e6 if gwp else None, 4),
            "market_mix":      _round(_pct(gwp, total_gwp), 4),
            "retention_rate":  _round(_pct(nrp, gwp), 4),
            "loss_ratio_gross":_round(_pct(ci,  gwp), 4),
        })

    # Prior year for VPP
    sub_prior = df_lob[(df_lob["pais"] == pais) & (df_lob["anio"] == year - 1)]
    if not sub_prior.empty:
        mkt_prior = sub_prior.groupby("lob_l1_en")["gwp_usd"].sum()
        total_gwp_prior = mkt_prior.sum()
        for entry in lob_list:
            key   = entry["lob_key"]
            gp_now   = (entry["gwp_usd_mm"] or 0) * 1e6
            gp_prior = mkt_prior.get(key, 0)
            share_prior = _pct(gp_prior, total_gwp_prior)
            yoy = _pct(gp_now - gp_prior, gp_prior)
            entry["gwp_yoy"]     = _round(yoy, 4)
            entry["vpp"]         = _round(_pct(gp_prior, total_gwp_prior) * yoy if share_prior and yoy is not None else None, 4)

    return {
        "available": True,
        "year":      year,
        "total_gwp_usd_mm": _round(total_gwp / 1e6, 4),
        "lobs":      lob_list,
    }


def _build_rankings(metrics_df: pd.DataFrame, year: int, keys: list[str]) -> dict:
    """Build ranking tables for specified metric keys."""
    _EXCL = {"TOTAL DE MERCADO", "TOTALES", "TOTAL MERCADO", "MERCADO TOTAL"}
    sub = metrics_df[
        (metrics_df["anio"] == year) &
        (~metrics_df["empresa"].str.upper().isin(_EXCL))
    ].copy()
    rankings = {}
    for key in keys:
        if key not in sub.columns:
            continue
        col_data = sub[["empresa", key]].dropna(subset=[key])
        ascending = _METRIC_LABELS.get(key, {}).get("direction") == "down"
        ranked = col_data.sort_values(key, ascending=ascending).reset_index(drop=True)
        rankings[key] = [
            {
                "rank":    i + 1,
                "name":    str(row["empresa"]),
                "alias":   str(row["empresa"]),
                "value":   _to_float(row[key]),
                "universe": len(ranked),
            }
            for i, row in ranked.iterrows()
        ]
    return rankings


# ── Country-level section ─────────────────────────────────────────────────────

def _build_country(
    pais: str,
    year: int,
    metrics_df: pd.DataFrame,
    df_lob: pd.DataFrame,
    history_years: list[int],
    group_metrics: "pd.DataFrame | None" = None,
) -> dict:
    meta = _COUNTRY_META[pais]
    sub  = metrics_df[metrics_df["pais"] == pais].copy()

    # Market aggregate per year (sum companies)
    pl_agg_cols = [c for c in _INSURER_METRIC_KEYS if c.endswith("_mm") or c in
                   ("retention_rate", "cession_rate")]
    mkt_by_year = {}
    for yr, grp in sub.groupby("anio"):
        mkt_row = {}
        mkt_row["pais"]    = pais
        mkt_row["empresa"] = "__MARKET__"
        mkt_row["anio"]    = yr
        mkt_row["_is_market"] = True
        for col in [c for c in _INSURER_METRIC_KEYS if "_mm" in c]:
            mkt_row[col] = grp[col].sum() if col in grp.columns else None
        gp  = mkt_row.get("gwp_usd_mm", 0) or 0
        nrp = mkt_row.get("net_premiums_usd_mm", 0) or 0
        nep = mkt_row.get("net_earned_usd_mm", 0) or 0
        ci_mm = grp["loss_ratio_gross"].dropna().mul(grp["net_premiums_usd_mm"].fillna(0)).sum() if "loss_ratio_gross" in grp.columns else None
        mkt_row["retention_rate"]       = _pct(nrp, gp, (0, 1.5))
        mkt_row["cession_rate"]         = _pct(mkt_row.get("ceded_premiums_usd_mm"), gp, (0, 1.5))
        mkt_row["loss_ratio_gross"]     = _safe(_pct(ci_mm, nrp), 0, 3.0) if ci_mm else None
        mkt_row["commission_ratio"]     = _safe(_pct(mkt_row.get("net_acq_cost_ratio"), 1), 0, 1.0)  # placeholder
        comm_mm = grp.get("net_acq_cost_ratio", pd.Series(dtype=float)).dropna().mul(grp["net_premiums_usd_mm"].fillna(0)).sum()
        adm_mm  = grp.get("admin_expense_ratio", pd.Series(dtype=float)).dropna().mul(grp["net_premiums_usd_mm"].fillna(0)).sum()
        mkt_row["net_acq_cost_ratio"]   = _pct(comm_mm, nrp, (0, 1.0))
        mkt_row["admin_expense_ratio"]  = _pct(adm_mm,  nrp, (0, 1.0))
        tp_mm = mkt_row.get("technical_result_usd_mm", 0) or 0
        mkt_row["technical_margin"]     = _pct(tp_mm, gp, (-2, 1))
        mkt_row["combined_ratio_gross"] = (
            (mkt_row.get("loss_ratio_gross") or 0)
            + (mkt_row.get("net_acq_cost_ratio") or 0)
            + (mkt_row.get("admin_expense_ratio") or 0)
        ) or None
        eq_mm = mkt_row.get("equity_usd_mm", 0) or 0
        ni_mm = mkt_row.get("net_income_usd_mm", 0) or 0
        ta_mm = mkt_row.get("total_assets_usd_mm", 0) or 0
        mkt_row["roe"]            = _pct(ni_mm, eq_mm, (-2, 2)) if eq_mm > 1 else None
        mkt_row["asset_leverage"] = _safe(_pct(ta_mm, eq_mm), 0, 50) if eq_mm > 1 else None
        tr_mm = mkt_row.get("technical_reserves_usd_mm", 0) or 0
        mkt_row["reserves_to_premiums"] = _pct(tr_mm, gp, (0, 10)) if gp > 0 else None
        mkt_by_year[int(yr)] = pd.Series(mkt_row)

    mkt_now = mkt_by_year.get(year)

    # Insurers for the focus year, sorted by GWP
    # Exclude regulator pre-aggregated market-total rows (e.g. "TOTAL DE MERCADO")
    _MARKET_TOTAL_NAMES = {"TOTAL DE MERCADO", "TOTALES", "TOTAL MERCADO", "MERCADO TOTAL"}
    sub_now = sub[
        (sub["anio"] == year) &
        (~sub["empresa"].str.upper().isin(_MARKET_TOTAL_NAMES))
    ].copy()
    if "gwp_usd_mm" in sub_now.columns:
        sub_now = sub_now.sort_values("gwp_usd_mm", ascending=False)

    insurers = []
    for _, row in sub_now.head(_MAX_INSURERS_PER_COUNTRY).iterrows():
        profile = _build_insurer_profile(row, mkt_now)
        profile["lob_breakdown"] = _build_insurer_lob(df_lob, pais, str(row["empresa"]), year)
        insurers.append(profile)

    # Market profile
    market_profile = None
    if mkt_now is not None:
        market_profile = _build_insurer_profile(mkt_now, None)
        market_profile["is_market"] = True
        market_profile["name"]  = f"{meta['name']} Market"
        market_profile["alias"] = "Market"

    # Annual history (market totals)
    history = []
    for yr in sorted(history_years):
        mkt_yr = mkt_by_year.get(yr)
        if mkt_yr is None:
            continue
        prior_yr = mkt_by_year.get(yr - 1)
        mkt_gwp     = _to_float(mkt_yr.get("gwp_usd_mm"))
        prior_gwp   = _to_float(prior_yr.get("gwp_usd_mm")) if prior_yr is not None else None
        history.append({
            "year": yr,
            "market": {
                "metrics":  _metrics_dict(mkt_yr, _INSURER_METRIC_KEYS),
                "gwp_yoy":  _round(_pct(mkt_gwp - prior_gwp, prior_gwp)) if mkt_gwp and prior_gwp else None,
                "n_insurers": int(sub[sub["anio"] == yr]["empresa"].nunique()),
            },
        })

    # Company history (only for focus_insurers = top 7 by GWP in current year)
    top7 = list(sub_now.head(7)["empresa"].values) if "empresa" in sub_now.columns else []
    company_history = []
    for yr in sorted(history_years):
        yr_grp = sub[sub["anio"] == yr]
        for co in top7:
            co_row = yr_grp[yr_grp["empresa"] == co]
            if co_row.empty:
                continue
            company_history.append({
                "year":    yr,
                "company": str(co),
                "metrics": _metrics_dict(co_row.iloc[0], _INSURER_METRIC_KEYS),
            })

    # Rankings
    ranking_keys = ["gwp_usd_mm", "loss_ratio_gross", "combined_ratio_gross",
                    "technical_margin", "retention_rate", "total_assets_usd_mm"]
    rankings = _build_rankings(sub, year, ranking_keys)

    # Grouped insurers and rankings (parent-group aggregation)
    grouped_insurers: list = []
    grouped_rankings: dict = {}
    if group_metrics is not None:
        grp_sub = group_metrics[
            (group_metrics["pais"] == pais) &
            (~group_metrics["empresa"].str.strip().str.upper().isin(_MARKET_TOTAL_NAMES))
        ].copy()
        grp_sub_now = grp_sub[grp_sub["anio"] == year].copy()
        if not grp_sub_now.empty and "gwp_usd_mm" in grp_sub_now.columns:
            grp_mkt_gwp = float(grp_sub_now["gwp_usd_mm"].sum())
            grp_sub_now = grp_sub_now.sort_values("gwp_usd_mm", ascending=False)
            for _, row in grp_sub_now.iterrows():
                gwp   = _to_float(row["gwp_usd_mm"])
                share = _pct(gwp, grp_mkt_gwp) if grp_mkt_gwp > 0 else None
                grouped_insurers.append({
                    "name":         str(row["empresa"]),
                    "alias":        str(row["empresa"]),
                    "is_market":    False,
                    "period":       int(row["anio"]),
                    "market_share": _round(share, 4),
                    "metrics":      _metrics_dict(row, _INSURER_METRIC_KEYS),
                })
            grouped_rankings = _build_rankings(grp_sub, year, ranking_keys)

    # LoB analysis
    lob_analysis = _build_lob_analysis(df_lob, pais, year)

    return {
        "key":             pais,
        "name":            meta["name"],
        "regulator":       meta["regulator"],
        "currency_local":  meta["currency_local"],
        "color":           _COUNTRY_COLORS[pais],
        "period":          year,
        "n_insurers":      int(sub_now["empresa"].nunique()),
        "market":          market_profile,
        "insurers":        insurers,
        "annual_history":  history,
        "company_history": company_history,
        "rankings":          rankings,
        "grouped_insurers":  grouped_insurers,
        "grouped_rankings":  grouped_rankings,
        "lob_analysis":      lob_analysis,
    }


# ── Regional section ──────────────────────────────────────────────────────────

def _build_region(
    all_countries: list[dict],
    metrics_df: pd.DataFrame,
    history_years: list[int],
) -> dict:
    """Build regional view: countries as entities."""

    def _country_row(cd: dict, year: int) -> dict | None:
        mkt = cd.get("market")
        if mkt is None:
            return None
        gwp = (mkt.get("metrics") or {}).get("gwp_usd_mm")
        return {
            "key":   cd["key"],
            "name":  cd["name"],
            "regulator":  cd["regulator"],
            "color": cd.get("color"),
            "n_insurers": cd.get("n_insurers"),
            "metrics": mkt.get("metrics"),
        }

    # Current year snapshot
    region_now = []
    total_gwp_now = 0.0
    for cd in all_countries:
        row = _country_row(cd, cd["period"])
        if row:
            gwp_val = (row["metrics"] or {}).get("gwp_usd_mm") or 0
            total_gwp_now += gwp_val
            region_now.append(row)

    for row in region_now:
        gwp_val = (row["metrics"] or {}).get("gwp_usd_mm") or 0
        row["latam_share"] = _round(_pct(gwp_val, total_gwp_now), 4)

    region_now.sort(key=lambda r: (r["metrics"] or {}).get("gwp_usd_mm") or 0, reverse=True)

    # Annual history by country
    region_history = []
    for yr in sorted(history_years):
        yr_entry = {"year": yr, "total_gwp_usd_mm": None, "countries": []}
        yr_total = 0.0
        for cd in all_countries:
            hist_yr = next((h for h in cd.get("annual_history", []) if h["year"] == yr), None)
            if hist_yr is None:
                continue
            gwp_mm = (hist_yr["market"]["metrics"] or {}).get("gwp_usd_mm") or 0
            yr_total += gwp_mm
            yr_entry["countries"].append({
                "key":     cd["key"],
                "name":    cd["name"],
                "metrics": hist_yr["market"]["metrics"],
                "gwp_yoy": hist_yr["market"].get("gwp_yoy"),
            })
        yr_entry["total_gwp_usd_mm"] = _round(yr_total, 4)
        region_history.append(yr_entry)

    return {
        "period":          max(cd["period"] for cd in all_countries),
        "total_gwp_usd_mm":_round(total_gwp_now, 4),
        "n_countries":     len(region_now),
        "countries":       region_now,
        "annual_history":  region_history,
    }


# ── Quality checks ────────────────────────────────────────────────────────────

def _quality_check(all_countries: list[dict]) -> tuple[dict, list[str], list[str]]:
    errors, warnings = [], []
    country_quality = {}
    for cd in all_countries:
        key = cd["key"]
        mkt = cd.get("market") or {}
        metrics = mkt.get("metrics") or {}
        flags = []
        gwp = metrics.get("gwp_usd_mm") or 0
        if gwp <= 0:
            flags.append("no_gwp_data")
            errors.append(f"{key}: GWP is zero or missing for period {cd['period']}")
        lr = metrics.get("loss_ratio_gross")
        if lr and lr > 2.0:
            flags.append("loss_ratio_implausible")
            warnings.append(f"{key}: gross loss ratio {lr:.1%} appears implausible")
        cr = metrics.get("combined_ratio_gross")
        if cr and cr > 3.0:
            flags.append("combined_ratio_implausible")
            warnings.append(f"{key}: combined ratio {cr:.1%} appears implausible")
        country_quality[key] = {"flags": flags, "ok": len([f for f in flags if "implausible" not in f]) == 0}

    return country_quality, errors, warnings


# ── Main ──────────────────────────────────────────────────────────────────────

def run(year: int = _DEFAULT_YEAR, out_path: Path = _DEFAULT_OUT, dry_run: bool = False):
    print(f"\n=== Insurance Motor v1.0 ===")
    print(f"  Target year : {year}")
    print(f"  Output      : {out_path}")

    history_years = list(range(year - _HISTORY_YEARS + 1, year + 1))
    min_year      = history_years[0]

    if dry_run:
        print("  [DRY RUN] Loading cached data from parquet if available, else aborting.")
        cache = out_path.parent / ".motor_cache"
        pl_f  = cache / "pl.parquet"
        lob_f = cache / "lob.parquet"
        bs_f  = cache / "bs.parquet"
        if not all(f.exists() for f in [pl_f, lob_f, bs_f]):
            print("  No cache found. Run without --dry-run first.")
            sys.exit(1)
        df_pl  = pd.read_parquet(pl_f)
        df_lob = pd.read_parquet(lob_f)
        df_bs  = pd.read_parquet(bs_f)
    else:
        print("  Connecting to Databricks…")
        w, wid, catalog = _connect()
        print(f"  Warehouse   : {wid}")
        print(f"  Catalog     : {catalog}")
        print(f"  Pulling P&L ({min_year}-{year})…", end=" ", flush=True)
        df_pl  = _pull_pl(w, wid, catalog, min_year, year)
        print(f"{len(df_pl):,} rows")
        print(f"  Pulling LoB ({min_year}-{year})…", end=" ", flush=True)
        df_lob = _pull_pl_lob(w, wid, catalog, min_year, year)
        print(f"{len(df_lob):,} rows")
        print(f"  Pulling BS  ({min_year}-{year})…", end=" ", flush=True)
        df_bs  = _pull_bs(w, wid, catalog, min_year, year)
        print(f"{len(df_bs):,} rows")

        # Cache for dry-run reuse
        cache = out_path.parent / ".motor_cache"
        cache.mkdir(exist_ok=True)
        df_pl.to_parquet(cache / "pl.parquet",  index=False)
        df_lob.to_parquet(cache / "lob.parquet", index=False)
        df_bs.to_parquet(cache / "bs.parquet",  index=False)

    print("  Computing metrics…")
    wide_pl  = _pivot_pl(df_pl)
    wide_bs  = _pivot_bs(df_bs)
    metrics  = _compute_metrics(wide_pl, wide_bs)

    print("  Loading insurer groups config…")
    groups_map    = _load_insurer_groups()
    group_metrics = None
    if groups_map:
        print("  Computing group-level metrics…")
        group_metrics = _build_group_metrics(df_pl, df_bs, groups_map)

    # FX rates: country → year → avg annual rate (local / USD)
    fx_df = (
        df_pl.groupby(["pais", "anio"])["fx_rate"]
        .mean()
        .reset_index()
        .rename(columns={"fx_rate": "rate"})
    )
    fx_rates: dict = {}
    for _, row in fx_df.iterrows():
        pais_k = str(row["pais"])
        anio_k = int(row["anio"])
        rate   = round(float(row["rate"]), 4) if pd.notna(row["rate"]) else None
        fx_rates.setdefault(pais_k, {})[anio_k] = rate
    # Ecuador uses USD natively — force rate to 1.0 for all years
    if "EC" in fx_rates:
        fx_rates["EC"] = {yr: 1.0 for yr in fx_rates["EC"]}

    countries_available = sorted(metrics["pais"].unique())
    print(f"  Countries   : {', '.join(countries_available)}")

    print("  Building country sections…")
    all_country_data = []
    for pais in countries_available:
        if pais not in _COUNTRY_META:
            print(f"    WARN: {pais} not in COUNTRY_META, skipping")
            continue
        print(f"    {pais}…", end=" ", flush=True)
        cd = _build_country(pais, year, metrics, df_lob, history_years, group_metrics)
        all_country_data.append(cd)
        print(f"  {cd['n_insurers']} insurers, {len(cd['lob_analysis'].get('lobs', []))} LoB")

    print("  Building regional section…")
    region = _build_region(all_country_data, metrics, history_years)

    print("  Running quality checks…")
    cq, errors, warnings = _quality_check(all_country_data)
    publication_allowed  = len(errors) == 0

    context = {
        "contract": {
            "name":           "pfic_insurance_portal_context",
            "version":        "1.0",
            "layout_version": "pfic_insurance_dashboard_v1",
            "engine_version": "1.0",
            "generated_at":   datetime.datetime.utcnow().isoformat() + "Z",
        },
        "metadata": {
            "period":              year,
            "currency":            "USD",
            "unit":                "USD million",
            "available_years":     history_years,
            "available_countries": countries_available,
            "country_meta":        {
                k: {**_COUNTRY_META[k], "color": _COUNTRY_COLORS[k]}
                for k in countries_available if k in _COUNTRY_META
            },
            "fx_rates": fx_rates,
        },
        "views": {
            "available": ["LATAM"] + countries_available,
            "default":   "LATAM",
        },
        "metric_contract": {
            "metric_catalog": _METRIC_LABELS,
        },
        "region":       region,
        "country_data": {cd["key"]: cd for cd in all_country_data},
        "quality": {
            "publication_allowed": publication_allowed,
            "status": "PASS" if publication_allowed else "WARN",
            "country_flags":       cq,
        },
        "sources": {
            "tables": [
                f"insurance_provider.vw_latam_all",
                f"insurance_provider.vw_bg_latam_all",
            ],
            "regulators": {k: v["regulator"] for k, v in _COUNTRY_META.items()},
            "methodology_notes": [
                "All monetary values converted to USD using year-average exchange rates from source data.",
                "fx_rates dict provides local/USD rates per country per year for client-side currency toggle.",
                "Balance sheet values averaged across reported periods within the year.",
                "Loss ratio computed as Claims Incurred / Gross Written Premiums.",
                "Combined ratio = Loss Ratio + Net Acquisition Cost Ratio + Admin Expense Ratio.",
                "Retention rate = Net Retained Premiums / Gross Written Premiums.",
                "Market benchmark = sum of all companies reporting for each country.",
                "ROE = Net Income / Shareholders Equity (available for CO and MX only).",
                "VPP = prior-year market share × current-year YoY growth rate.",
            ],
        },
        "contract_validation": {
            "status":   "PASS" if not errors else "WARN",
            "errors":   errors,
            "warnings": warnings,
        },
    }

    print(f"  Writing {out_path}…", end=" ", flush=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    size_mb = out_path.stat().st_size / 1e6
    print(f"{size_mb:.2f} MB")

    print(f"\n  LATAM total GWP {year}: USD {region['total_gwp_usd_mm']:,.0f}M")
    for cd in all_country_data:
        mkt_m = (cd["market"] or {}).get("metrics") or {}
        gwp = mkt_m.get("gwp_usd_mm") or 0
        sh  = next((r["latam_share"] for r in region["countries"] if r["key"] == cd["key"]), None)
        print(f"    {cd['key']} ({cd['name']:12s}) — ${gwp:>10,.0f}M GWP  {(sh or 0)*100:>5.1f}% of LATAM  {cd['n_insurers']} co.")

    print(f"\nDone. Validation: {context['contract_validation']['status']}")
    if warnings:
        for w_msg in warnings:
            print(f"  WARN: {w_msg}")

    return context


def _cli():
    parser = argparse.ArgumentParser(description="LATAM Insurance Motor — generate portal_context.json")
    parser.add_argument("--year",    type=int,  default=_DEFAULT_YEAR, help="Target year (default 2025)")
    parser.add_argument("--out",     type=Path, default=_DEFAULT_OUT,  help="Output JSON path")
    parser.add_argument("--dry-run", action="store_true",              help="Use cached parquet, no Databricks")
    args = parser.parse_args()
    run(year=args.year, out_path=args.out, dry_run=args.dry_run)


if __name__ == "__main__":
    _cli()
