"""Tests for connectors/co/sfc/transform.py"""

import pandas as pd
import pytest

from connectors.co.sfc.transform import transform


def _write_csv(rows: list[dict], path) -> None:
    pd.DataFrame(rows).to_csv(path, index=False)


_BASE_ROW = {
    "a_o": "2023",
    "mes": "8",
    "tipo_entidad": "13",
    "codigo_entidad": "13-08",
    "nombre_entidad": "13-08 Seguros Confianza S.A.",
    "unidad_de_captura": "1",
    "nombre_unidad_de_captura": "PRIMAS RETENIDAS",
    "subcuenta": "15",
    "nombre_subcuenta": "CANC Y-O ANULAC PRIMAS EMIT DIR Y COA",
    "total": "631637",
    "subtotal_ramos": "631637",
    "automoviles": "0",
    "soat": "0",
    "vida_grupo_mes": "0",
}


class TestTransform:
    def test_periodo_is_constructed(self, tmp_path):
        src = tmp_path / "f290.csv"
        _write_csv([_BASE_ROW], src)
        df = transform(src)
        assert df["periodo"].iloc[0] == pd.Timestamp("2023-08-01")

    def test_ano_mes_are_integers(self, tmp_path):
        src = tmp_path / "f290.csv"
        _write_csv([_BASE_ROW], src)
        df = transform(src)
        assert df["ano"].iloc[0] == 2023
        assert df["mes"].iloc[0] == 8

    def test_numeric_columns_are_float(self, tmp_path):
        src = tmp_path / "f290.csv"
        _write_csv([_BASE_ROW], src)
        df = transform(src)
        assert df["total"].iloc[0] == pytest.approx(631637.0)

    def test_entity_name_is_stripped(self, tmp_path):
        row = {**_BASE_ROW, "nombre_entidad": "  Seguros Test S.A.  "}
        src = tmp_path / "f290.csv"
        _write_csv([row], src)
        df = transform(src)
        assert df["nombre_entidad"].iloc[0] == "Seguros Test S.A."

    def test_missing_id_column_raises(self, tmp_path):
        src = tmp_path / "bad.csv"
        _write_csv([{"a_o": "2023", "mes": "1"}], src)
        with pytest.raises(ValueError, match="Missing expected identifier columns"):
            transform(src)

    def test_output_columns_order(self, tmp_path):
        src = tmp_path / "f290.csv"
        _write_csv([_BASE_ROW], src)
        df = transform(src)
        assert list(df.columns[:5]) == [
            "periodo", "ano", "mes", "tipo_entidad", "codigo_entidad"
        ]
