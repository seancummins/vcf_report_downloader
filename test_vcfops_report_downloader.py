import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import vcfops_report_downloader as downloader


class ArgumentTests(unittest.TestCase):
    def test_ini_options_are_loaded(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as config:
            config.write(
                "[vcf-ops]\n"
                "host = ops.example.test\nuser = user\npassword = pass\n"
                "outdir = /tmp/reports\nformat = csv\nunsafe = true\n"
                "lookback_hours = 48\n"
            )
            config_path = config.name
        try:
            with patch.object(sys, "argv", ["script", "-c", config_path]):
                params = downloader.parse_arguments_and_config()
            self.assertEqual(params["format"], "csv")
            self.assertTrue(params["unsafe"])
            self.assertEqual(params["lookback_hours"], 48)
        finally:
            os.unlink(config_path)

    def test_defaults_remain_pdf_safe_and_24_hours(self):
        argv = ["script", "-H", "host", "-u", "user", "-p", "pass", "-o", "/tmp"]
        with patch.object(sys, "argv", argv):
            params = downloader.parse_arguments_and_config()
        self.assertEqual(params["format"], "pdf")
        self.assertFalse(params["unsafe"])
        self.assertEqual(params["lookback_hours"], 24)


class DownloadTests(unittest.TestCase):
    @patch.object(downloader, "get_report_definitions", return_value={"definition": "Test Report"})
    @patch.object(downloader.requests, "get")
    def test_csv_download_uses_csv_header_query_and_extension(self, mock_get, _definitions):
        completion = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y")
        listing = Mock()
        listing.json.return_value = {"reports": [{
            "id": "report", "completionTime": completion,
            "reportDefinitionId": "definition"
        }]}
        listing.raise_for_status.return_value = None

        download = Mock()
        download.__enter__ = Mock(return_value=download)
        download.__exit__ = Mock(return_value=False)
        download.raise_for_status.return_value = None
        download.iter_content.return_value = [b"a,b\n1,2\n"]
        mock_get.side_effect = [listing, download]

        with tempfile.TemporaryDirectory() as outdir:
            downloader.download_recent_reports(
                "host", "token", outdir, False, report_format="csv", lookback_hours=48
            )
            request = mock_get.call_args_list[1]
            self.assertEqual(request.kwargs["headers"]["Accept"], "text/csv")
            self.assertEqual(request.kwargs["params"], {"format": "CSV"})
            self.assertTrue(os.listdir(outdir)[0].endswith(".csv"))


if __name__ == "__main__":
    unittest.main()
