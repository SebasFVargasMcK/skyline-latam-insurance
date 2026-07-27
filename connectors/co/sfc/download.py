"""Download SFC Colombia Formato 290 data.

TODO: Identify the SFC download endpoint or API.
      SFC portal: https://www.superfinanciera.gov.co/
      Formato 290 covers insurance company financial statements.
"""

from __future__ import annotations

import datetime
from pathlib import Path

# Placeholder — update once the SFC endpoint is confirmed
SFC_BASE_URL = "https://www.superfinanciera.gov.co"


def download(dest_dir: Path, *, date: datetime.date | str | None = None) -> Path:
    raise NotImplementedError(
        "SFC Colombia connector is not yet implemented. "
        "Inspect network requests on the SFC portal to find the download endpoint."
    )
