#!/usr/bin/env python3
"""Unit tests for Scripts/range-origin.py, the HTTP arm's origin.

Run with: python3 Tests/range_origin_test.py. Starts a real origin on a free
loopback port against a temporary directory; no fixtures needed.
"""
import http.client
import importlib.util
import os
import socket
import tempfile
import threading
import time
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "range_origin", os.path.join(THIS_DIR, "..", "Scripts", "range-origin.py"))
origin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(origin)


class ParseRangeTests(unittest.TestCase):
    def test_no_header_is_the_whole_body(self):
        self.assertEqual(origin.parse_range(None, 1000), (0, 999, False))

    def test_closed_range(self):
        self.assertEqual(origin.parse_range("bytes=10-19", 1000), (10, 19, True))

    def test_open_range(self):
        self.assertEqual(origin.parse_range("bytes=990-", 1000), (990, 999, True))

    def test_suffix_range(self):
        # The form a tail read (MP4 moov at the end, Matroska Cues) uses, and
        # the one a hand-written origin usually gets wrong.
        self.assertEqual(origin.parse_range("bytes=-100", 1000), (900, 999, True))

    def test_suffix_longer_than_the_file_is_the_whole_file(self):
        self.assertEqual(origin.parse_range("bytes=-5000", 1000), (0, 999, True))

    def test_end_past_the_file_is_clamped(self):
        self.assertEqual(origin.parse_range("bytes=500-5000", 1000), (500, 999, True))

    def test_start_past_the_file_is_unsatisfiable(self):
        self.assertIsNone(origin.parse_range("bytes=1000-", 1000))

    def test_zero_suffix_is_unsatisfiable(self):
        self.assertIsNone(origin.parse_range("bytes=-0", 1000))


class LiveOriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.body = bytes(range(256)) * 4096  # 1 MiB, every byte position-dependent
        with open(os.path.join(cls.tmp.name, "clip.mkv"), "wb") as f:
            f.write(cls.body)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        # 80 Mbit/s = 10 MB/s, so 1 MiB costs about 0.1 s of link time.
        cls.server = origin.Server(("127.0.0.1", cls.port),
                                   origin.make_handler(cls.tmp.name, origin.Link(80), 0.02))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, conn, path, headers=None):
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp, resp.read()

    def test_ranges_keep_alive_and_content_type(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        resp, data = self.get(conn, "/clip.mkv", {"Range": "bytes=100-199"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.getheader("Content-Range"), f"bytes 100-199/{len(self.body)}")
        self.assertEqual(resp.getheader("Content-Type"), "video/x-matroska")
        self.assertEqual(data, self.body[100:200])
        # Same connection, second request: a media server keeps it alive.
        resp, data = self.get(conn, "/clip.mkv", {"Range": "bytes=-10"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(data, self.body[-10:])
        resp, data = self.get(conn, "/clip.mkv", {"Range": f"bytes={len(self.body)}-"})
        self.assertEqual(resp.status, 416)
        conn.close()

    def test_nothing_outside_the_directory_is_served(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        resp, _ = self.get(conn, "/../" + os.path.basename(__file__))
        self.assertEqual(resp.status, 404)
        conn.close()

    def test_rate_is_shared_across_connections(self):
        # Two concurrent 1 MiB bodies on an 80 Mbit/s link need twice the
        # link time of one (about 0.21 s). A per-connection rate would finish
        # both in about 0.1 s. Compared against the link time rather than a
        # timed single fetch, which carries connection and scheduling noise.
        def fetch():
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            self.get(c, "/clip.mkv")
            c.close()

        link_seconds = len(self.body) / origin.Link(80).bytes_per_second
        start = time.monotonic()
        threads = [threading.Thread(target=fetch) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertGreater(time.monotonic() - start, 2 * link_seconds * 0.95)

if __name__ == "__main__":
    unittest.main()
