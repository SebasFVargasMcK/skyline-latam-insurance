"""Download CNSF Estado de Resultados from the SIO portal.

The SIO exposes a direct download endpoint — no browser automation needed:
    GET https://sio.cnsf.gob.mx/descarga-base-ASEG/{YYYY-MM-DD}

Pass a specific date or call download_latest() to resolve the most recent
quarter-end automatically (March 31, June 30, September 30, December 31).
"""

from __future__ import annotations

import datetime
from pathlib import Path

from connectors.shared.http import download_file

BASE_URL = "https://sio.cnsf.gob.mx/descarga-base-ASEG"

# Quarter-end months and their last days
_QUARTER_ENDS = {3: 31, 6: 30, 9: 30, 12: 31}


def latest_quarter_end(reference: datetime.date | None = None) -> datetime.date:
    """Return the most recently completed quarter-end date."""
    ref = reference or datetime.date.today()
    quarter_month = (ref.month - 1) // 3 * 3  # last quarter-end month
    if quarter_month == 0:
        quarter_month = 12
        year = ref.year - 1
    else:
        year = ref.year
    return datetime.date(year, quarter_month, _QUARTER_ENDS[quarter_month])


def download(
    dest_dir: Path,
    *,
    date: datetime.date | str | None = None,
    filename: str = "estado_resultados_sio.xlsx",
) -> Path:
    """Download the CNSF income-statement base file to *dest_dir*.

    Args:
        dest_dir: Directory where the file is saved.
        date:     Reporting date (defaults to the latest completed quarter-end).
        filename: Override the saved filename.

    Returns:
        Path to the downloaded file.
    """
    if date is None:
        reporting_date = latest_quarter_end()
    elif isinstance(date, str):
        reporting_date = datetime.date.fromisoformat(date)
    else:
        reporting_date = date

    url = f"{BASE_URL}/{reporting_date.isoformat()}"
    dest = Path(dest_dir) / filename
    # sio.cnsf.gob.mx uses a Mexican government CA not included in certifi
    return download_file(url, dest, verify=False)
