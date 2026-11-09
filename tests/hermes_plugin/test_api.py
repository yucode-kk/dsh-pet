"""api.request 的传输层测试：走真本机 HTTP 服务，不 mock urllib。"""

from __future__ import annotations

import json
import socket
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from hermes.hermes_adapter import api


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 - 静音，别污染测试输出
        return

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.records.append(
            {
                "method": self.command,
                "path": self.path,
                "token": self.headers.get("x-dsh-pet-token"),
                "content_type": self.headers.get("Content-Type"),
                "accept": self.headers.get("Accept"),
                "body": body,
            }
        )
        if self.server.status >= 400:
            self.send_response(self.server.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = b"" if self.server.payload is None else json.dumps(self.server.payload).encode("utf-8")
        self.send_response(self.server.status)
        if raw:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _serve
    do_POST = _serve
    do_DELETE = _serve


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.records = []
    httpd.payload = {"ok": True}
    httpd.status = 200
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def port_of(server) -> int:
    return server.server_address[1]


def test_get_health_skips_route_prefix_and_token(server):
    server.payload = {"name": "dsh-pet-standalone", "pid": 123}
    status, body = api.request(port_of(server), "GET", "/health", with_route=False)

    assert status == 200
    assert body == {"name": "dsh-pet-standalone", "pid": 123}
    record = server.records[-1]
    assert record["path"] == "/health"
    assert record["token"] is None
    assert record["body"] == b""
    assert record["accept"] == "application/json"


def test_post_work_status_carries_token_and_json(server):
    server.payload = {"ok": True}
    status, body = api.request(
        port_of(server),
        "POST",
        "/work-status",
        token="tok-secret",
        payload={"state": "working", "task": "工具 terminal"},
    )

    assert (status, body) == (200, {"ok": True})
    record = server.records[-1]
    assert record["path"] == api.ROUTE_SEGMENT + "/work-status"
    assert record["method"] == "POST"
    assert record["token"] == "tok-secret"
    assert record["content_type"] == "application/json"
    assert json.loads(record["body"]) == {"state": "working", "task": "工具 terminal"}


def test_delete_with_empty_payload_is_sent_as_object(server):
    server.payload = {"ok": True}
    status, _ = api.request(port_of(server), "DELETE", "/work-status", token="t", payload={})

    assert status == 200
    record = server.records[-1]
    assert record["method"] == "DELETE"
    assert json.loads(record["body"]) == {}


def test_empty_response_body_becomes_none(server):
    server.payload = None
    status, body = api.request(port_of(server), "POST", "/quit", token="t", payload={})

    assert (status, body) == (200, None)


def test_http_error_propagates(server):
    server.status = 404
    with pytest.raises(urllib.error.HTTPError):
        api.request(port_of(server), "GET", "/nope")


def test_connection_refused_raises_oserror():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    with pytest.raises(OSError):
        api.request(port, "GET", "/health", with_route=False, timeout=0.3)


def test_loopback_request_never_uses_http_proxy(server, monkeypatch):
    """环境里有代理也不能把回环请求接走——这是 api.py 里空 ProxyHandler 的意义。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")
    server.payload = {"ok": True}

    status, body = api.request(port_of(server), "GET", "/health", with_route=False)

    assert status == 200
    assert body == {"ok": True}