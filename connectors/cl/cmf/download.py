"""Download CMF FECU financial statements for all Seguros Generales companies.

CMF publishes quarterly IFRS financial statements via an HTML form that accepts
POST parameters. Requesting sociedad[]=0 (TODOS) with xls=y returns a single
binary XLS containing all ~25 active companies for the requested quarter.

Frequency: quarterly — March 31 / June 30 / September 30 / December 31.
The latest published quarter typically lags the current date by 1–2 quarters.

Source: https://www.cmfchile.cl/institucional/estadisticas/seg_gen_fecu_index.php
"""

from __future__ import annotations

import datetime
import warnings
from pathlib import Path

import requests

_INDEX_URL = "https://www.cmfchile.cl/institucional/estadisticas/seg_gen_fecu_index.php"
_DATA_URL = "https://www.cmfchile.cl/institucional/estadisticas/seg_gen_fecu1.php"

_QUARTER_LAST_DAYS = {3: 31, 6: 30, 9: 30, 12: 31}
_PREV_QUARTER = {3: (12, -1), 6: (3, 0), 9: (6, 0), 12: (9, 0)}  # month -> (prev_month, year_delta)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9",
}


def latest_quarter_end(reference: datetime.date | None = None) -> datetime.date:
    """Return the most recently completed quarter-end on or before *reference*."""
    ref = reference or datetime.date.today()
    for year_offset in (0, -1):
        year = ref.year + year_offset
        for month in (12, 9, 6, 3):
            end = datetime.date(year, month, _QUARTER_LAST_DAYS[month])
            if end <= ref:
                return end
    raise RuntimeError("Could not determine latest quarter-end")


def download(
    dest_dir: Path,
    *,
    date: datetime.date | str | None = None,
    filename: str = "cmf_fecu_sg.xls",
    min_companies: int = 5,
) -> tuple[Path, datetime.date]:
    """Download FECU bulk XLS for all Seguros Generales companies.

    Starts at *date* (default: latest completed quarter-end) and walks back
    up to 6 quarters until a period where at least *min_companies* companies
    have reported.  CMF publishes quarterly data gradually; the most recent
    quarter often has only a handful of early filers.

    Returns (path_to_file, reporting_date).
    """
    if date is None:
        start = latest_quarter_end()
    elif isinstance(date, str):
        start = datetime.date.fromisoformat(date)
        start = latest_quarter_end(start)
    else:
        start = date

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        session = requests.Session()
        session.headers.update(_HEADERS)
        session.get(
            _INDEX_URL,
            params={"lang": "es", "tiposociedad": "A"},
            verify=False,
            timeout=30,
        )

        year, month = start.year, start.month
        for _ in range(6):
            content = _fetch_quarter(session, year, month)
            if content is not None and _count_companies(content) >= min_companies:
                dest = dest_dir / filename
                dest.write_bytes(content)
                return dest, datetime.date(year, month, _QUARTER_LAST_DAYS[month])
            prev_month, year_delta = _PREV_QUARTER[month]
            month = prev_month
            year += year_delta

    raise RuntimeError("No CMF FECU data with enough companies found in the last 6 quarters")


def _count_companies(xls_bytes: bytes) -> int:
    """Return the number of company data rows in the XLS (cheap probe, no full parse)."""
    import io
    import pandas as pd

    try:
        probe = pd.read_excel(io.BytesIO(xls_bytes), engine="xlrd", header=None, nrows=40)
        # Row 5 is header; rows 6+ are companies until the "Total" summary row
        count = 0
        for i in range(6, len(probe)):
            cell = str(probe.iloc[i, 1])  # RUT column
            if cell in ("nan", "") or "Total" in str(probe.iloc[i, 0]):
                break
            count += 1
        return count
    except Exception:
        return 0


def _fetch_quarter(session: requests.Session, year: int, month: int) -> bytes | None:
    """POST to CMF and return raw XLS bytes, or None if no data for that period."""
    payload = {
        "tiposociedad": "A",
        "sociedad[]": "0",
        "mes1": f"{month:02d}",
        "anno1": str(year),
        "mes2": f"{month:02d}",
        "anno2": str(year),
        "anual": "n",
        "porc": "0",
        "xls": "y",
        "imageField": "Consultar",
    }
    resp = session.post(
        _DATA_URL,
        params={"auth": "", "send": "", "control": "Berlin39", "lang": "es", "vigente": ""},
        data=payload,
        verify=False,
        timeout=60,
        headers={"Referer": f"{_INDEX_URL}?lang=es&tiposociedad=A"},
    )
    resp.raise_for_status()

    # No-data responses are small HTML pages
    if b"No se encuentran datos" in resp.content or len(resp.content) < 10_000:
        return None

    # Validate it looks like a binary XLS (OLE compound doc or has CMF sheet marker)
    magic_ok = resp.content[:4] == b"\xd0\xcf\x11\xe0" or b"SGFECUB" in resp.content[:300]
    if not magic_ok:
        return None

    return resp.content
