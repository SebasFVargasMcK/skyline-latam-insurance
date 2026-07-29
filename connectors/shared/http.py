"""Shared HTTP download helpers used by all connectors."""

import time
from pathlib import Path

import requests


def download_file(
    url: str,
    dest: Path,
    *,
    headers: dict | None = None,
    retries: int = 3,
    backoff: float = 2.0,
    timeout: int = 60,
    verify: bool = True,
) -> Path:
    """Download a file from *url* to *dest*, with retry/backoff on failure.

    Set *verify=False* for sites whose TLS certificate is signed by a CA not
    in the certifi bundle (e.g. Mexican government sites behind corporate proxy).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout,
                                stream=True, verify=verify)
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    fh.write(chunk)
            return dest
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(backoff * attempt)

    raise RuntimeError(f"Failed to download {url} after {retries} attempts") from last_error
