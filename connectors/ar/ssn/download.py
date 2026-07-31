"""Download SSN Estados Patrimoniales y de Resultados for all Argentine insurance companies.

SSN publishes quarterly financial statements on argentina.gob.ar. The XLSX URL follows
the pattern:
    https://www.argentina.gob.ar/sites/default/files/ssn_{YYYYMM}_estados_patrimoniales.xlsx

Quarterly periods: March (03), June (06), September (09), December (12).
Exception: for Q1 of some years SSN used "01" instead of "03" in the filename;
this module probes both suffixes automatically.

Frequency: quarterly.
Data covers ~186 companies in wide format across 5 financial sheets.

Source: https://www.argentina.gob.ar/superintendencia-de-seguros/estadisticas/estados-patrimoniales
"""

from __future__ import annotations

import datetime
import warnings
from pathlib import Path

import requests

_BASE_URL = "https://www.argentina.gob.ar/sites/default/files"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Quarter-end months and their last days
_QUARTERS = [(3, 31), (6, 30), (9, 30), (12, 31)]


def _month_end(year: int, month: int) -> datetime.date:
    for q_month, q_day in _QUARTERS:
        if q_month == month:
            return datetime.date(year, month, q_day)
    raise ValueError(f"Month {month} is not a quarter-end month")


def _candidate_urls(year: int, month: int) -> list[str]:
    """Return candidate XLSX URLs for a given year/quarter-end month.

    Q1 (March) sometimes uses '01' instead of '03' in the filename;
    probe both to handle all historical periods.
    """
    urls = [f"{_BASE_URL}/ssn_{year:04d}{month:02d}_estados_patrimoniales.xlsx"]
    if month == 3:
        # Fallback for years where SSN used '01' instead of '03'
        urls.append(f"{_BASE_URL}/ssn_{year:04d}01_estados_patrimoniales.xlsx")
    return urls


def _probe(url: str) -> bytes | None:
    """Try to download *url*; return bytes if it's a valid XLSX, None otherwise."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            r = requests.get(url, verify=False, timeout=30, headers=_HEADERS)
        except requests.RequestException:
            return None
    if r.status_code != 200:
        return None
    # XLSX files start with the ZIP magic bytes
    if r.content[:4] != b"PK\x03\x04":
        return None
    return r.content


def _latest_quarter(reference: datetime.date | None = None) -> tuple[int, int]:
    """Return (year, quarter_end_month) of the most recently published period."""
    ref = reference or datetime.date.today()

    # Walk back up to 5 quarters from the most recently completed one
    quarters = []
    year = ref.year
    for q_month, _ in reversed(_QUARTERS):
        end = datetime.date(year, q_month, _QUARTERS[_QUARTERS.index((q_month, _))][1])
        if end <= ref:
            quarters.append((year, q_month))
    # Add previous year's quarters as fallback
    prev_year = year - 1
    for q_month, _ in reversed(_QUARTERS):
        quarters.append((prev_year, q_month))

    for y, m in quarters[:5]:
        for url in _candidate_urls(y, m):
            if _probe(url) is not None:
                return y, m

    raise RuntimeError("Could not find a recent SSN Estados Patrimoniales XLSX (checked 5 quarters)")


def download(
    dest_dir: Path,
    *,
    filename: str = "ssn_estados_patrimoniales.xlsx",
) -> tuple[Path, datetime.date]:
    """Download the most recent SSN Estados Patrimoniales XLSX.

    Returns (path_to_file, quarter_end_date).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    year, month = _latest_quarter()
    content = None
    used_url = None
    for url in _candidate_urls(year, month):
        content = _probe(url)
        if content is not None:
            used_url = url
            break

    if content is None:
        raise RuntimeError(f"Failed to download SSN XLSX for {year}-{month:02d}")

    dest = dest_dir / filename
    dest.write_bytes(content)
    return dest, _month_end(year, month)
