"""Tests for connectors/mx/cnsf/download.py"""

import datetime

import pytest
import responses as resp_mock

from connectors.mx.cnsf.download import BASE_URL, download, latest_quarter_end


class TestLatestQuarterEnd:
    def test_q1_end(self):
        assert latest_quarter_end(datetime.date(2025, 4, 15)) == datetime.date(2025, 3, 31)

    def test_q2_end(self):
        assert latest_quarter_end(datetime.date(2025, 7, 1)) == datetime.date(2025, 6, 30)

    def test_q3_end(self):
        assert latest_quarter_end(datetime.date(2025, 10, 5)) == datetime.date(2025, 9, 30)

    def test_q4_end(self):
        assert latest_quarter_end(datetime.date(2025, 12, 31)) == datetime.date(2025, 12, 31)

    def test_jan_wraps_to_prior_year_q4(self):
        assert latest_quarter_end(datetime.date(2025, 1, 10)) == datetime.date(2024, 12, 31)


class TestDownload:
    @resp_mock.activate
    def test_download_uses_correct_url(self, tmp_path):
        date = datetime.date(2025, 3, 31)
        url = f"{BASE_URL}/{date.isoformat()}"
        resp_mock.add(resp_mock.GET, url, body=b"PK\x03\x04fake-xlsx", status=200)

        result = download(tmp_path, date=date)

        assert result.exists()
        assert result.name == "estado_resultados_sio.xlsx"

    @resp_mock.activate
    def test_download_string_date(self, tmp_path):
        url = f"{BASE_URL}/2024-12-31"
        resp_mock.add(resp_mock.GET, url, body=b"PK\x03\x04", status=200)
        result = download(tmp_path, date="2024-12-31")
        assert result.exists()

    @resp_mock.activate
    def test_download_retries_on_failure(self, tmp_path):
        date = datetime.date(2025, 3, 31)
        url = f"{BASE_URL}/{date.isoformat()}"
        resp_mock.add(resp_mock.GET, url, status=503)
        resp_mock.add(resp_mock.GET, url, status=503)
        resp_mock.add(resp_mock.GET, url, body=b"PK\x03\x04ok", status=200)

        result = download(tmp_path, date=date)
        assert result.exists()
