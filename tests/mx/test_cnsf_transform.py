"""Tests for connectors/mx/cnsf/transform.py"""

import io

import pandas as pd
import pytest

from connectors.mx.cnsf.transform import transform


def _make_excel(rows: list[dict], path) -> None:
    pd.DataFrame(rows).to_excel(path, index=False)


class TestTransform:
    def test_canonical_columns(self, tmp_path):
        src = tmp_path / "test.xlsx"
        _make_excel(
            [
                {
                    "fecha_corte": "2025-03-31",
                    "entidad": "AXA Seguros",
                    "id_nivel": "1",
                    "descripcion": "Utilidad (Pérdida) de la Operación",
                    "operacion": "Vida",
                    "importe": 1234.56,
                    "desagregado": 100.0,
                }
            ],
            src,
        )
        df = transform(src)
        assert list(df.columns) == [
            "fecha_corte_raw",
            "fecha_corte",
            "anio_corte",
            "trimestre_corte",
            "entidad",
            "id_nivel",
            "descripcion",
            "operacion",
            "importe",
            "desagregado",
        ]
        assert df["anio_corte"].iloc[0] == 2025
        assert df["trimestre_corte"].iloc[0] == 1
        assert df["importe"].iloc[0] == pytest.approx(1234.56)

    def test_alias_columns(self, tmp_path):
        src = tmp_path / "alias.xlsx"
        _make_excel(
            [
                {
                    "Fecha de Corte": "31/12/2024",
                    "Institución": "Chubb Seguros México",
                    "Descripción": "Primas Emitidas",
                    "Operación": "Daños",
                    "Importe": 999.0,
                }
            ],
            src,
        )
        df = transform(src)
        assert df["entidad"].iloc[0] == "Chubb Seguros México"

    def test_missing_required_column_raises(self, tmp_path):
        src = tmp_path / "bad.xlsx"
        _make_excel([{"fecha_corte": "2025-03-31", "entidad": "X"}], src)
        with pytest.raises(ValueError, match="required columns"):
            transform(src)
