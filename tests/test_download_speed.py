"""Essential core and CLI regressions using a loopback HTTP server."""

from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, call, patch

import core


PAYLOAD = bytes(range(256)) * 600  # Binary data larger than one read block.
SCRIPT = Path(__file__).resolve().parents[1] / "download_speed.py"
_RESPONSES = {
    "/redirect": (302, {"Location": "/body"}, PAYLOAD),
    "/chain": (302, {"Location": "/redirect"}, PAYLOAD),
    "/not-found": (404, {}, b""),
    "/slow-body": (200, {"Content-Length": "1", "Connection": "close"}, b""),
    "/truncated": (200, {"Content-Length": str(len(PAYLOAD) + 10), "Connection": "close"}, PAYLOAD),
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.requests.append(self.path)
        status, headers, body = _RESPONSES.get(self.path, (200, {}, PAYLOAD))
        self.send_response(status)
        for name, value in {"Content-Length": str(len(body)), **headers}.items():
            self.send_header(name, value)
        self.end_headers()
        if self.path == "/slow-body":
            self.wfile.flush()
            self.server.release_body.wait(timeout=5)
        else:
            self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.requests = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.thread.join, timeout=5)
        cls.addClassCleanup(cls.server.shutdown)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    def setUp(self):
        self.server.requests.clear()
        self.server.release_body = threading.Event()
        self.addCleanup(self.server.release_body.set)
        self.enterContext(patch("urllib.request.getproxies", return_value={}))

    def run_script(self, path, *arguments):
        url = path if "://" in path else self.base_url + path
        return subprocess.run(
            [sys.executable, str(SCRIPT), url, *arguments],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"},
        )

    def test_redirect_bodies_are_counted_and_counter_resets(self):
        with patch("core.perf_counter", side_effect=[index / 2 for index in range(20)]):
            result = core.run_benchmark(self.base_url + "/chain", 2)
        self.assertEqual(result, core.BenchmarkResult(10, 4_608_000, 5))
        self.assertEqual(self.server.requests, ["/chain", "/redirect", "/body"] * 10)

    def test_incomplete_response_stops_without_retries(self):
        with self.assertRaisesRegex(core.DownloadError, "Request 1/10.*Incomplete"):
            core.run_benchmark(self.base_url + "/truncated", 2)
        self.assertEqual(self.server.requests, ["/truncated"])

    def test_script_downloads_ten_complete_responses(self):
        completed = self.run_script("/body")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.server.requests, ["/body"] * 10)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(len(completed.stdout.splitlines()), 3)
        self.assertNotIn("bytes/s", completed.stdout)
        for fragment in ("Average request time:", f"Total downloaded: {len(PAYLOAD) * 10} bytes", "MiB/s"):
            self.assertIn(fragment, completed.stdout)

    def test_script_errors_have_no_report_or_traceback(self):
        for path, arguments, code, message, requests in (
            ("/not-found", (), 0, "HTTP 404", 1),
            ("ftp://example.com/file", (), 2, "HTTP(S)", 0),
            ("/body", ("--help",), 2, "unrecognized arguments", 0),
            ("/body", ("-h",), 2, "unrecognized arguments", 0),
            ("/body", ("--timeout", "nan"), 2, "timeout must be in", 0),
            ("/body", ("--timeout", "0"), 2, "timeout must be in", 0),
            ("/body", ("--timeout", "1e300"), 2, "timeout must be in (0, 86400] seconds", 0),
            ("/slow-body", ("--timeout", "0.5"), 0, "timed out", 1),
        ):
            with self.subTest(path=path):
                self.server.requests.clear()
                completed = self.run_script(path, *arguments)
                self.assertEqual((completed.returncode, completed.stdout), (code, ""))
                self.assertIn(message, completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)
                self.assertEqual(len(self.server.requests), requests)
                self.assertEqual("Request 1/10" in completed.stderr, bool(requests))


class CoreTests(unittest.TestCase):
    def test_body_is_read_in_bounded_blocks_until_eof(self):
        response = Mock(status=200)
        response.getheader.return_value = None
        response.read.side_effect = [b"ab", b"c", b""]
        self.assertEqual(core._read_body(response), 3)
        self.assertEqual(response.read.call_args_list, [call(core.CHUNK_SIZE)] * 3)

    def test_aggregate_statistics_for_different_sizes_and_durations(self):
        samples = [(core.MIB, 1), (9 * core.MIB, 3)] * 5
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("core.download_once", side_effect=samples) as download:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = core.run_benchmark("https://example.com/file", 30)
        self.assertEqual(download.call_count, 10)
        self.assertEqual(result, core.BenchmarkResult(10, 50 * core.MIB, 20))
        self.assertEqual(result.average_seconds, 2)
        self.assertEqual(result.speed_bytes_per_second, 2.5 * core.MIB)
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
