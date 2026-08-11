"""Download SSN Argentina 'Balances Aseguradoras' quarterly CSV files.

SSN publishes quarterly balance sheets via the open data portal
https://datosabiertos.ssn.gob.ar/dataset/balances (CKAN dataset
1ecd32c6-ea38-486f-8502-b9b55ae680eb).

Each resource is a quarterly CSV (~40 MB) named balances-YYYYMMDD.csv where
YYYYMMDD is the first day of the quarter (e.g. 20240701 for 2024-Q3).

The CKAN package_show API is used to discover all available quarters, so
the connector is forward-compatible as new quarters are published.
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

_CKAN_API = (
    "https://datosabiertos.ssn.gob.ar/api/3/action/package_show"
    "?id=1ecd32c6-ea38-486f-8502-b9b55ae680eb"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _list_resources(session: requests.Session) -> list[dict]:
    """Return CKAN resource list for the SSN balances dataset."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = session.get(_CKAN_API, headers=_HEADERS, timeout=30, verify=False)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"CKAN API error: {data.get('error')}")
    return data["result"]["resources"]


def download(
    dest_dir: Path,
    *,
    delay: float = 1.0,
) -> list[Path]:
    """Download all available quarterly CSV files into *dest_dir*.

    Files that already exist are skipped (cached).  Returns list of Paths to
    all available files (downloaded + cached).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(_HEADERS)

    resources = _list_resources(session)

    csv_resources = [
        r for r in resources
        if r.get("format", "").upper() == "CSV"
        or (r.get("url", "").lower().endswith(".csv"))
    ]

    if not csv_resources:
        raise RuntimeError("No CSV resources found in SSN balances dataset")

    results: list[Path] = []

    for res in sorted(csv_resources, key=lambda r: r.get("name", "")):
        url = res["url"]

        # Skip Google Drive links — blocked by corporate proxies
        if "drive.google.com" in url or "docs.google.com" in url:
            continue

        name = res.get("name") or Path(url).name
        if not name.lower().endswith(".csv"):
            name = name + ".csv"
        if not name.startswith("balances-"):
            name = "balances-" + name

        dest = dest_dir / name
        if dest.exists():
            results.append(dest)
            continue

        print(f"  Downloading {name} ...")
        r = session.get(url, headers=_HEADERS, timeout=120, stream=True, verify=False)
        r.raise_for_status()

        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)

        results.append(dest)
        time.sleep(delay)

    return results
