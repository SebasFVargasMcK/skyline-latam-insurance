"""Download SFC Colombia Formato 290 insurance statistics.

The dataset is published on Colombia's open data portal (datos.gov.co),
which exposes a Socrata SODA REST API — no browser automation needed:

    Full CSV export (no pagination):
        GET https://www.datos.gov.co/api/views/e967-4a8r/rows.csv?accessType=DOWNLOAD

    JSON with pagination (useful for incremental loads):
        GET https://www.datos.gov.co/resource/e967-4a8r.json?$limit=50000&$offset=0

An optional Socrata app token removes rate-limiting:
    Set SOCRATA_APP_TOKEN in your .env file.

Dataset: Información estadística y financiera por ramos de seguros — Formato 290
Source:  https://www.datos.gov.co/d/e967-4a8r
"""

from __future__ import annotations

import os
from pathlib import Path

from connectors.shared.http import download_file

# Socrata dataset identifiers
_SOCRATA_DOMAIN = "www.datos.gov.co"
_DATASET_ID = "e967-4a8r"

# Full-export URL — downloads the complete dataset as a single CSV
CSV_EXPORT_URL = (
    f"https://{_SOCRATA_DOMAIN}/api/views/{_DATASET_ID}/rows.csv?accessType=DOWNLOAD"
)

# JSON endpoint base (used for paginated/incremental access)
JSON_ENDPOINT = f"https://{_SOCRATA_DOMAIN}/resource/{_DATASET_ID}.json"


def _headers() -> dict:
    """Add Socrata app token if available to avoid anonymous rate limits."""
    token = os.getenv("SOCRATA_APP_TOKEN")
    return {"X-App-Token": token} if token else {}


def download(
    dest_dir: Path,
    *,
    filename: str = "formato_290.csv",
) -> Path:
    """Download the full Formato 290 CSV to *dest_dir*.

    Returns the path to the saved file.
    """
    dest = Path(dest_dir) / filename
    return download_file(CSV_EXPORT_URL, dest, headers=_headers())
