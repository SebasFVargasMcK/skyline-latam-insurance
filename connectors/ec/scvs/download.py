"""Download SCVS ranking data for Ecuadorian insurance companies.

The SCVS (Superintendencia de Compañías, Valores y Seguros) publishes annual
financial data for all registered companies via a public ranking portal:

    https://appscvsmovil.supercias.gob.ec/ranking/

Two CSV files are required:
  bi_ranking.csv   — annual financial metrics per company (~300 MB, all sectors)
  bi_compania.csv  — company directory: expediente, RUC, name, province

Both are filtered here to insurance/reinsurance companies (CIIU K651x / K652x).

NOTE: The supercias.gob.ec domain may be unreachable from certain corporate
networks. Pass use_wayback=True (or set env var SCVS_USE_WAYBACK=1) to fall
back to Internet Archive snapshots for development/testing.

Frequency: annual (the ranking file is updated once per year).
"""

from __future__ import annotations

import io
import os
import warnings
from pathlib import Path

import pandas as pd
import requests

_BASE = "https://appscvsmovil.supercias.gob.ec/ranking/recursos"

# Internet Archive snapshots used when the live domain is unreachable
_WAYBACK_RANKING = (
    "https://web.archive.org/web/20260327023315/"
    "https://appscvsmovil.supercias.gob.ec/ranking/recursos/bi_ranking.csv"
)
_WAYBACK_COMPANIA = (
    "https://web.archive.org/web/20240712235023/"
    "https://appscvsmovil.supercias.gob.ec/ranking/recursos/bi_compania.csv"
)

# Insurance carriers only:
#   K6511.xx = life insurance companies (aseguradoras de vida)
#   K6512.02 = general/combined insurance companies (aseguradoras generales)
#   K6520.xx = reinsurers
# Excluded: K6512.01 = insurance agents/brokers (agencias colocadoras)
#           K6530.xx = pension funds (fondos de pensiones)
def _is_insurance_ciiu(ciiu: str) -> bool:
    c = str(ciiu).strip()
    return c.startswith("K6511") or c.startswith("K6512.02") or c.startswith("K6520")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
}

_CHUNK_ROWS = 50_000


def _use_wayback() -> bool:
    return bool(int(os.environ.get("SCVS_USE_WAYBACK", "0")))


def _get(url: str, *, timeout: int = 60) -> requests.Response:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = requests.get(url, timeout=timeout, verify=False, headers=_HEADERS)
    r.raise_for_status()
    return r


def _download_ranking_filtered(wayback: bool) -> pd.DataFrame:
    """Stream bi_ranking.csv to a temp buffer, then chunk-filter to insurance rows."""
    import tempfile

    url = _WAYBACK_RANKING if wayback else f"{_BASE}/bi_ranking.csv"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        resp = requests.get(url, timeout=300, verify=False, headers=_HEADERS, stream=True)
    resp.raise_for_status()

    # Stream to a temp file to avoid holding 328 MB in memory as a BytesIO
    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
        tmp_path = tmp.name
        for chunk in resp.iter_content(chunk_size=1 << 20):  # 1 MB chunks
            tmp.write(chunk)

    try:
        parts: list[pd.DataFrame] = []
        reader = pd.read_csv(tmp_path, chunksize=_CHUNK_ROWS, low_memory=False)
        for chunk in reader:
            if "ciiu_n6" not in chunk.columns:
                continue
            mask = chunk["ciiu_n6"].astype(str).map(_is_insurance_ciiu)
            filtered = chunk[mask]
            if not filtered.empty:
                parts.append(filtered)
    finally:
        import os as _os
        _os.unlink(tmp_path)

    if not parts:
        raise RuntimeError(
            "No insurance companies found in bi_ranking.csv "
            "(expected CIIU K6511.xx, K6512.02, K6520.xx)"
        )
    return pd.concat(parts, ignore_index=True)


def _download_compania(wayback: bool) -> pd.DataFrame:
    url = _WAYBACK_COMPANIA if wayback else f"{_BASE}/bi_compania.csv"
    r = _get(url, timeout=120)
    return pd.read_csv(io.BytesIO(r.content), low_memory=False)


def download(
    dest_dir: Path,
    *,
    use_wayback: bool | None = None,
    ranking_filename: str = "scvs_ranking.csv",
    compania_filename: str = "scvs_compania.csv",
) -> tuple[Path, Path]:
    """Download and save the filtered ranking and company CSV files.

    Returns (ranking_path, compania_path).
    If use_wayback is None, checks the SCVS_USE_WAYBACK environment variable.
    """
    if use_wayback is None:
        use_wayback = _use_wayback()

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    source = "Wayback Machine" if use_wayback else "appscvsmovil.supercias.gob.ec"
    print(f"      source: {source}")

    ranking_df = _download_ranking_filtered(use_wayback)
    compania_df = _download_compania(use_wayback)

    ranking_path = dest_dir / ranking_filename
    compania_path = dest_dir / compania_filename

    ranking_df.to_csv(ranking_path, index=False)
    compania_df.to_csv(compania_path, index=False)

    return ranking_path, compania_path
