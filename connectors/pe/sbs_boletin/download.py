"""Download SBS Boletin Estadistico S-345 (Primas según Ramos) for December EoY snapshots.

URL pattern:
    https://intranet2.sbs.gob.pe/estadistica/financiera/{year}/Diciembre/S-345-di{year}.XLS

Files cover December of each requested year (annual EoY snapshot).  By default
this module downloads the last 10 EoY periods (2015–2024; 2025 if available).
"""

from __future__ import annotations

import time
import warnings
from pathlib import Path

import requests

_BASE = "https://intranet2.sbs.gob.pe/estadistica/financiera"
_SERIES = "S-345"
_MONTH = "Diciembre"
_ABBR = "di"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
}

DEFAULT_YEARS = list(range(2015, 2026))  # 2015–2025 inclusive


def _url(year: int) -> str:
    return f"{_BASE}/{year}/{_MONTH}/{_SERIES}-{_ABBR}{year}.XLS"


def _filename(year: int) -> str:
    return f"S-345-di{year}.XLS"


def download(
    dest_dir: Path,
    *,
    years: list[int] | None = None,
    delay: float = 0.5,
) -> list[Path]:
    """Download S-345 December files for the given years.

    Missing years (404) are silently skipped so the caller receives only
    successfully downloaded files.  Returns a list of local Paths.
    """
    if years is None:
        years = DEFAULT_YEARS

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for year in years:
        url = _url(year)
        dest = dest_dir / _filename(year)

        if dest.exists():
            paths.append(dest)
            continue

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = requests.get(url, timeout=60, verify=False, headers=_HEADERS)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            dest.write_bytes(r.content)
            paths.append(dest)
        except requests.RequestException:
            continue

        time.sleep(delay)

    return paths
