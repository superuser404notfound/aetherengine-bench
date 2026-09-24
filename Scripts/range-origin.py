#!/usr/bin/env python3
"""The HTTP arm's origin: serves Fixtures/ over HTTP with byte ranges, a fixed
latency and one link rate shared across every connection.

    python3 Scripts/range-origin.py <dir> <port> <mbps> <latency-ms>

Media servers deliver over HTTP, and an engine's network path is different
code from its file path (AetherEngine reads a file:// URL with FileIOReader
and an http one with AVIOReader and its range window). A loopback server that
answers instantly and at memory speed would not be a link, though, so two
knobs make it one:

  * a latency paid before EVERY response header, the round trip every
    request crosses;
  * a rate advanced under one lock on a virtual clock, so concurrent
    connections share it. A per-connection sleep would hand a second reader
    free bandwidth, the opposite of a real link.

It answers `bytes=a-b`, `bytes=a-` and the suffix form `bytes=-n` with 206 and
`Content-Range`, speaks HTTP/1.1 with keep-alive like a real media server, and
is threaded, so a stream probe on a second connection is never starved behind
a long body on the first. The same shape as AetherEngine's Scripts/slowrange.py,
which the #620 measurement ran on.

Every served body is logged to stderr as one line (path, range, bytes, whether
the client closed early), so a session can say how much each engine pulled.
"""
import mimetypes
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNK = 64 * 1024

mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/mp4", ".mp4")


class Link:
    """One shared virtual clock: every byte on every connection advances it."""

    def __init__(self, mbps):
        self.bytes_per_second = mbps * 1_000_000 / 8
        self._lock = threading.Lock()
        self._free_at = time.monotonic()

    def pace(self, nbytes):
        with self._lock:
            now = time.monotonic()
            if self._free_at < now:
                self._free_at = now
            self._free_at += nbytes / self.bytes_per_second
            wait = self._free_at - now
        if wait > 0:
            time.sleep(wait)


def parse_range(header, size):
    """(start, end, partial) for a Range header, or None if unsatisfiable.
    No header, or one this parser does not recognise, is the whole body."""
    if not header:
        return 0, size - 1, False
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header)
    if not m:
        return 0, size - 1, False
    a, b = m.group(1), m.group(2)
    if a == "" and b == "":
        return 0, size - 1, False
    if a == "":
        n = int(b)
        if n == 0:
            return None
        return max(0, size - n), size - 1, True
    start = int(a)
    end = min(int(b), size - 1) if b else size - 1
    if start >= size or start > end:
        return None
    return start, end, True


def make_handler(root, link, latency):
    root = os.path.realpath(root)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _resolve(self):
            name = self.path.split("?", 1)[0].lstrip("/")
            path = os.path.realpath(os.path.join(root, name))
            if not path.startswith(root + os.sep) or not os.path.isfile(path):
                return None
            return path

        def _headers(self, status, length, path, size, content_range=None):
            time.sleep(latency)
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if content_range:
                self.send_header("Content-Range", content_range)
            self.end_headers()

        def _not_found(self):
            time.sleep(latency)
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):
            path = self._resolve()
            if path is None:
                return self._not_found()
            size = os.path.getsize(path)
            self._headers(200, size, path, size)

        def do_GET(self):
            path = self._resolve()
            if path is None:
                return self._not_found()
            size = os.path.getsize(path)
            parsed = parse_range(self.headers.get("Range"), size)
            if parsed is None:
                time.sleep(latency)
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end, partial = parsed
            length = end - start + 1
            if partial:
                self._headers(206, length, path, size, f"bytes {start}-{end}/{size}")
            else:
                self._headers(200, length, path, size)
            sent = 0
            closed = False
            with open(path, "rb") as f:
                f.seek(start)
                while sent < length:
                    buf = f.read(min(CHUNK, length - sent))
                    if not buf:
                        break
                    link.pace(len(buf))
                    try:
                        self.wfile.write(buf)
                    except (BrokenPipeError, ConnectionResetError):
                        closed = True
                        self.close_connection = True
                        break
                    sent += len(buf)
            sys.stderr.write(f"{time.strftime('%H:%M:%S')} {os.path.basename(path)} {start}-{end} "
                             f"sent={sent}{' CLOSED' if closed else ''}\n")
            sys.stderr.flush()

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if len(sys.argv) != 5:
        sys.exit("usage: range-origin.py <dir> <port> <mbps> <latency-ms>")
    root, port, mbps, latency_ms = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
    if mbps <= 0 or latency_ms < 0:
        sys.exit("range-origin.py: mbps must be positive and latency-ms non-negative")
    server = Server(("127.0.0.1", port), make_handler(root, Link(mbps), latency_ms / 1000.0))
    sys.stderr.write(f"origin {root} on :{port}, {mbps:g} Mbit/s shared, {latency_ms:g} ms latency\n")
    sys.stderr.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
