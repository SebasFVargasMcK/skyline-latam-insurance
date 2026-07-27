# skyline-latam-ingest

Data ingestion pipeline for LATAM financial regulators → Databricks.

Each connector downloads data from a regulator's portal, normalizes it, and loads it into a Databricks Delta table (or a local DuckDB file during development).

---

## Project layout

```
skyline-latam-ingest/
├── connectors/
│   ├── shared/
│   │   ├── http.py          # HTTP download helper (retries, streaming)
│   │   └── databricks.py    # Databricks upload (Unity Catalog)
│   ├── mx/
│   │   └── cnsf/
│   │       ├── download.py  # GET sio.cnsf.gob.mx/descarga-base-ASEG/{date}
│   │       ├── transform.py # Excel → normalized DataFrame
│   │       └── load.py      # DuckDB (local) or Databricks (prod)
│   └── co/
│       └── sfc/
│           └── download.py  # TODO: Colombia SFC Formato 290
├── pipelines/
│   ├── mx_cnsf.py           # download → transform → load (MX)
│   └── co_sfc.py            # placeholder (CO)
├── app/
│   └── streamlit_app.py     # Natural-language chatbot UI (DuckDB backend)
├── tests/
│   ├── mx/
│   │   ├── test_cnsf_download.py
│   │   └── test_cnsf_transform.py
│   └── co/
├── data/
│   ├── cnsf.duckdb          # local dev database (gitignored)
│   └── local/
│       ├── raw/             # downloaded Excel files (gitignored)
│       └── normalized/      # intermediate CSVs (gitignored)
├── pyproject.toml
└── .env.example
```

---

## Setup

```bash
pip install -e ".[dev]"
```

For Databricks production mode, also install:

```bash
pip install -e ".[databricks]"
```

Copy `.env.example` to `.env` and fill in values.

---

## Running a pipeline

### Mexico — CNSF Estado de Resultados

```bash
# Latest quarter-end, local DuckDB
python -m pipelines.mx_cnsf

# Specific date
python -m pipelines.mx_cnsf --date 2025-03-31

# Append rows instead of replacing
python -m pipelines.mx_cnsf --mode append
```

Set `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `DATABRICKS_CATALOG`, and `DATABRICKS_SCHEMA` in `.env` to write to Databricks instead of the local DuckDB.

### Chatbot UI

```bash
streamlit run app/streamlit_app.py
```

Requires a local [Ollama](https://ollama.com/) instance running `sqlcoder` (or set `LOCAL_MODEL` in `.env`).

---

## Tests

```bash
pytest
```

---

## Adding a new country / regulator

1. Create `connectors/{country_code}/{regulator}/` with `__init__.py`, `download.py`, `transform.py`, `load.py`.
2. Create `pipelines/{country_code}_{regulator}.py` wiring the three steps together.
3. Add tests under `tests/{country_code}/`.

The `connectors/shared/` helpers (`http.download_file`, `databricks.upload_dataframe`) are available to all connectors.

---

## CNSF API note

The SIO portal exposes a direct download endpoint — no browser automation needed:

```
GET https://sio.cnsf.gob.mx/descarga-base-ASEG/{YYYY-MM-DD}
```

To discover endpoints for other datasets, open the SIO portal in a browser, trigger a download, and inspect the Network tab. The pattern `descarga-base-{DATASET}/{date}` is consistent across sections.
