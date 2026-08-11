"""Download CMF FECU financial statements for Seguros Generales and Seguros de Vida.

CMF publishes quarterly IFRS financial statements via an HTML form.  There are
two separate portals:

  Generales: https://www.cmfchile.cl/institucional/estadisticas/seg_gen_fecu_index.php
  Vida:      https://www.cmfchile.cl/institucional/estadisticas/seg_vida_fecu_index.php

Requesting sociedad[]=0 (TODOS) with xls=y returns a single binary XLS
containing all active companies for the requested quarter.

This module exposes two entry points:
  download()       — single quarter, one tipo (original behaviour, kept for back-compat)
  download_range() — all quarters in a year range, both tipos, with local caching
"""

from __future__ import annotations

import datetime
import time
import warnings
from pathlib import Path

import requests

# ── URL configuration per company type ───────────────────────────────────────

_TIPOS = {
    "generales": {
        "index": "https://www.cmfchile.cl/institucional/estadisticas/seg_gen_fecu_index.php",
        "data":  "https://www.cmfchile.cl/institucional/estadisticas/seg_gen_fecu1.php",
        "abbr":  "sg",
        "tiposociedad": "A",
    },
    "vida": {
        "index": "https://www.cmfchile.cl/institucional/estadisticas/seg_vida_fecu_index.php",
        "data":  "https://www.cmfchile.cl/institucional/estadisticas/seg_vida_fecu1.php",
        "abbr":  "sv",
        "tiposociedad": "V",
    },
}

_QUARTER_LAST_DAYS = {3: 31, 6: 30, 9: 30, 12: 31}
_PREV_QUARTER = {3: (12, -1), 6: (3, 0), 9: (6, 0), 12: (9, 0)}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9",
}


# ── Quarter helpers ───────────────────────────────────────────────────────────

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


def _iter_quarters(from_date: datetime.date, to_date: datetime.date) -> list[datetime.date]:
    """Return all quarter-end dates in [from_date, to_date], newest first."""
    quarters = []
    cur = latest_quarter_end(to_date)
    while cur >= from_date:
        quarters.append(cur)
        prev_month, year_delta = _PREV_QUARTER[cur.month]
        cur = datetime.date(cur.year + year_delta, prev_month, _QUARTER_LAST_DAYS[prev_month])
    return quarters


# ── Core HTTP helpers ─────────────────────────────────────────────────────────

def _make_session(tipo_cfg: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update(_HEADERS)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        session.get(
            tipo_cfg["index"],
            params={"lang": "es", "tiposociedad": tipo_cfg["tiposociedad"]},
            verify=False,
            timeout=30,
        )
    return session


def _fetch_quarter(
    session: requests.Session,
    tipo_cfg: dict,
    year: int,
    month: int,
) -> bytes | None:
    payload = {
        "tiposociedad": tipo_cfg["tiposociedad"],
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
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        resp = session.post(
            tipo_cfg["data"],
            params={"auth": "", "send": "", "control": "Berlin39", "lang": "es", "vigente": ""},
            data=payload,
            verify=False,
            timeout=60,
            headers={"Referer": f"{tipo_cfg['index']}?lang=es&tiposociedad={tipo_cfg['tiposociedad']}"},
        )
    resp.raise_for_status()

    if b"No se encuentran datos" in resp.content or len(resp.content) < 10_000:
        return None

    magic_ok = resp.content[:4] == b"\xd0\xcf\x11\xe0" or b"SGFECUB" in resp.content[:300]
    if not magic_ok:
        return None

    return resp.content


def _count_companies(xls_bytes: bytes) -> int:
    import io
    import pandas as pd

    try:
        probe = pd.read_excel(io.BytesIO(xls_bytes), engine="xlrd", header=None, nrows=40)
        count = 0
        for i in range(6, len(probe)):
            cell = str(probe.iloc[i, 1])
            if cell in ("nan", "") or "Total" in str(probe.iloc[i, 0]):
                break
            count += 1
        return count
    except Exception:
        return 0


# ── Public API ────────────────────────────────────────────────────────────────

def download(
    dest_dir: Path,
    *,
    date: datetime.date | str | None = None,
    filename: str = "cmf_fecu_sg.xls",
    min_companies: int = 5,
) -> tuple[Path, datetime.date]:
    """Download FECU bulk XLS for Seguros Generales (single quarter, back-compat)."""
    if date is None:
        start = latest_quarter_end()
    elif isinstance(date, str):
        start = datetime.date.fromisoformat(date)
        start = latest_quarter_end(start)
    else:
        start = date

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    tipo_cfg = _TIPOS["generales"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        session = _make_session(tipo_cfg)

    year, month = start.year, start.month
    for _ in range(6):
        content = _fetch_quarter(session, tipo_cfg, year, month)
        if content is not None and _count_companies(content) >= min_companies:
            dest = dest_dir / filename
            dest.write_bytes(content)
            return dest, datetime.date(year, month, _QUARTER_LAST_DAYS[month])
        prev_month, year_delta = _PREV_QUARTER[month]
        month = prev_month
        year += year_delta

    raise RuntimeError("No CMF FECU data with enough companies found in the last 6 quarters")


def download_range(
    dest_dir: Path,
    *,
    years: int = 10,
    min_companies: int = 3,
    delay: float = 0.5,
    tipos: list[str] | None = None,
) -> list[tuple[Path, datetime.date, str]]:
    """Download FECU for all quarters in the last *years* years, for both tipos.

    Files are cached locally — already-present files are skipped.  Returns a list
    of (path, quarter_end_date, tipo) for every successfully downloaded file.

    tipos defaults to ['generales', 'vida'].  Pass ['generales'] to skip Vida.
    """
    if tipos is None:
        tipos = ["generales", "vida"]

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    to_date = latest_quarter_end()
    from_date = datetime.date(to_date.year - years + 1, 1, 1)
    quarters = _iter_quarters(from_date, to_date)

    results: list[tuple[Path, datetime.date, str]] = []

    for tipo_name in tipos:
        tipo_cfg = _TIPOS[tipo_name]
        abbr = tipo_cfg["abbr"]

        session = _make_session(tipo_cfg)

        for q in quarters:
            fname = f"cmf_fecu_{abbr}_{q.year}Q{(q.month - 1) // 3 + 1}.xls"
            dest = dest_dir / fname

            if dest.exists():
                results.append((dest, q, tipo_name))
                continue

            content = _fetch_quarter(session, tipo_cfg, q.year, q.month)
            if content is None or _count_companies(content) < min_companies:
                time.sleep(delay)
                continue

            dest.write_bytes(content)
            results.append((dest, q, tipo_name))
            time.sleep(delay)

    return results
