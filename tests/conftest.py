"""为插件测试套件提供共享 fixture 和本地 HTTP 服务器。"""

from __future__ import annotations

import gzip
import hashlib
import json
import socket
import threading
import time
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

pytest_plugins = ["pytester"]

GIF_BYTES = b"GIF89a-this-is-ascii-not-really-a-gif"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            return self.rfile.read(int(length))
        except (ValueError, OSError):
            return b""

    def _write(
        self,
        status: int,
        body: bytes = b"",
        content_type: str = "application/json",
        headers=(),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, headers=()) -> None:
        self._write(
            status,
            json.dumps(payload).encode("utf-8"),
            "application/json",
            headers,
        )

    def _redirect(self, status: int, location: str) -> None:
        self._write(status, b"", "text/plain", [("Location", location)])

    def _dispatch(self) -> None:
        body = self._read_body()
        path = urlsplit(self.path).path

        if path == "/echo":
            self._json(
                200,
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": body.decode("utf-8", "replace"),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "size": len(body),
                },
            )
            return

        if path == "/redirect/301":
            self._redirect(301, "/echo")
            return
        if path == "/redirect/302":
            self._redirect(302, "/echo")
            return
        if path == "/redirect/303":
            self._redirect(303, "/echo")
            return
        if path == "/redirect/307":
            self._redirect(307, "/echo")
            return
        if path == "/redirect/308":
            self._redirect(308, "/echo")
            return
        if path == "/redirect-loop":
            self._redirect(302, "/redirect-loop")
            return
        if path == "/chain":
            self._redirect(302, "/chain2")
            return
        if path == "/chain2":
            self._redirect(301, "/echo")
            return

        if path == "/gzip":
            size = int(urlsplit(self.path).query.split("=")[-1] or "1024")
            compressed = gzip.compress(b"A" * size)
            self._write(
                200,
                compressed,
                "text/plain",
                [("Content-Encoding", "gzip")],
            )
            return

        if path == "/no-content":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/not-modified":
            self.send_response(304)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/empty":
            self._write(200, b"", "text/plain")
            return

        if path == "/download":
            size = 65536
            query = dict(
                item.split("=", 1)
                for item in urlsplit(self.path).query.split("&")
                if "=" in item
            )
            size = int(query.get("size", size))
            payload = b"x" * size
            if query.get("chunked") == "1":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                half = max(1, size // 2)
                for chunk in (payload[:half], payload[half:]):
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                self._write(200, payload, "application/octet-stream")
            return

        if path == "/binary":
            self._write(200, GIF_BYTES, "image/gif")
            return

        if path == "/lines":
            self._write(200, b"hello\nworld\n", "text/plain")
            return

        if path == "/upload":
            self._json(
                200,
                {
                    "content_type": self.headers.get("Content-Type", ""),
                    "length": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                },
            )
            return

        if path == "/multipart":
            content_type = self.headers.get("Content-Type", "")
            parts = []
            if "multipart/form-data" in content_type.lower():
                pseudo = (
                    b"Content-Type: "
                    + content_type.encode("latin-1", "replace")
                    + b"\r\nMIME-Version: 1.0\r\n\r\n"
                    + body
                )
                message = BytesParser(policy=default).parsebytes(pseudo)
                for sub in message.iter_parts():
                    payload = sub.get_payload(decode=True) or b""
                    parts.append(
                        {
                            "name": sub.get_param(
                                "name", header="content-disposition"
                            ),
                            "filename": sub.get_filename(),
                            "content_type": sub.get("Content-Type"),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                    )
            self._json(200, {"parts": parts})
            return

        if path == "/slow":
            query = dict(
                item.split("=", 1)
                for item in urlsplit(self.path).query.split("&")
                if "=" in item
            )
            time.sleep(float(query.get("ms", "1000")) / 1000.0)
            self._json(200, {"ok": True})
            return

        if path.startswith("/status/"):
            status = int(path.rsplit("/", 1)[-1])
            self._json(status, {"status": status})
            return

        self._json(404, {"error": "not found"})

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "5")
        self.end_headers()


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


@pytest.fixture(scope="session")
def http_server():
    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}".format(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def read_har():
    def _read(pytester):
        out = pytester.path / "captured_traffic"
        files = sorted(out.glob("*.har"))
        assert len(files) == 1, files
        return json.loads(files[0].read_text(encoding="utf-8"))

    return _read


@pytest.fixture
def closed_port():
    """返回一个刚释放、预期无人监听的本地端口。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port
