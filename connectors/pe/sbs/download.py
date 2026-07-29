"""Download SBS S-203 financial statements for all Peruvian insurance companies.

SBS publishes monthly balance sheet + income statement data via direct XLS download.
URL pattern: https://intranet2.sbs.gob.pe/estadistica/financiera/{YYYY}/{Month}/S-203-{abbrev}{YYYY}.XLS

Files are available from 1998 onwards. The latest month typically lags the current
date by 1–2 months.

Source: https://www.sbs.gob.pe/app/stats/EstadisticaSistemaFinancieroResultados.asp?c=S-203
"""

from __future__ import annotations

import datetime
import warnings
from pathlib import Path

import requests

_BASE_URL = "https://intranet2.sbs.gob.pe/estadistica/financiera"

_MONTHS = {
    1:  ("Enero",      "en"),
    2:  ("Febrero",    "fe"),
    3:  ("Marzo",      "ma"),
    4:  ("Abril",      "ab"),
    5:  ("Mayo",       "my"),
    6:  ("Junio",      "jn"),
    7:  ("Julio",      "jl"),
    8:  ("Agosto",     "ag"),
    9:  ("Setiembre",  "se"),
    10: ("Octubre",    "oc"),
    11: ("Noviembre",  "no"),
    12: ("Diciembre",  "di"),
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _month_end(year: int, month: int) -> datetime.date:
    if month == 12:
        return datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
    return datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)


def _build_url(year: int, month: int) -> str:
    month_name, abbrev = _MONTHS[month]
    year_str = f"{year:04d}" if year >= 2000 else f"{year % 100:02d}"
    filename = f"S-203-{abbrev}{year_str}.XLS"
    return f"{_BASE_URL}/{year:04d}/{month_name}/{filename}"


def latest_available(reference: datetime.date | None = None) -> tuple[int, int]:
    """Return (year, month) of the latest likely-published SBS S-203 file."""
    ref = reference or datetime.date.today()
    # SBS typically publishes with ~1 month lag; try current month, walk back
    year, month = ref.year, ref.month
    for _ in range(4):
        if _fetch_url(_build_url(year, month)) is not None:
            return year, month
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    raise RuntimeError("Could not find a recent SBS S-203 file (checked 4 months back)")


def _fetch_url(url: str) -> bytes | None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            r = requests.get(url, verify=False, timeout=30, headers=_HEADERS)
        except requests.RequestException:
            return None
    if r.status_code != 200:
        return None
    if len(r.content) < 5_000:
        return None
    if r.content[:4] != b"\xd0\xcf\x11\xe0":
        return None
    return r.content


def download(
    dest_dir: Path,
    *,
    year: int | None = None,
    month: int | None = None,
    filename: str = "sbs_s203.xls",
) -> tuple[Path, datetime.date]:
    """Download S-203 XLS for the given year/month (default: latest available).

    Returns (path_to_file, period_end_date).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if year is None or month is None:
        year, month = latest_available()

    url = _build_url(year, month)
    content = _fetch_url(url)
    if content is None:
        raise RuntimeError(f"Could not download SBS S-203 for {year}-{month:02d}: {url}")

    dest = dest_dir / filename
    dest.write_bytes(content)
    return dest, _month_end(year, month)
