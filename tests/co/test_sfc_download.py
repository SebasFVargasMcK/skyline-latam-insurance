"""Tests for connectors/co/sfc/download.py"""

import responses as resp_mock

from connectors.co.sfc.download import CSV_EXPORT_URL, download


class TestDownload:
    @resp_mock.activate
    def test_download_hits_socrata_export_url(self, tmp_path):
        resp_mock.add(resp_mock.GET, CSV_EXPORT_URL, body=b"a_o,mes\n2023,8\n", status=200)
        result = download(tmp_path)
        assert result.exists()
        assert result.name == "formato_290.csv"

    @resp_mock.activate
    def test_download_custom_filename(self, tmp_path):
        resp_mock.add(resp_mock.GET, CSV_EXPORT_URL, body=b"a_o,mes\n2023,8\n", status=200)
        result = download(tmp_path, filename="my_file.csv")
        assert result.name == "my_file.csv"

    @resp_mock.activate
    def test_download_retries_on_failure(self, tmp_path):
        resp_mock.add(resp_mock.GET, CSV_EXPORT_URL, status=503)
        resp_mock.add(resp_mock.GET, CSV_EXPORT_URL, status=503)
        resp_mock.add(resp_mock.GET, CSV_EXPORT_URL, body=b"a_o,mes\n2023,1\n", status=200)
        result = download(tmp_path)
        assert result.exists()
