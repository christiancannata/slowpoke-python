"""Outbound HTTP calls: the time a request or a job spends waiting on Stripe, a partner API, another service."""

import asyncio
import http.server
import json
import socket
import threading

import pytest
from conftest import ROOT, FakeSender, attr, line_of, split

import slowpoke
from slowpoke import _http

requests = pytest.importorskip("requests")
httpx = pytest.importorskip("httpx")

HERE = __file__


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        status = 503 if self.path.startswith("/broken") else 200
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"ok":true}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET  # noqa: N815

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def calls(sender):
    root, client = sender.only_trace()
    return root, [s for s in client if attr(s, "http.request.method") is not None]


def in_job(fn, name="sync"):
    with slowpoke.job(name):
        fn()


def test_a_requests_call_is_one_client_span_with_the_host_only(tracer, sender, server):
    def work():
        auth = {"Authorization": "Bearer sk_secret"}
        requests.get(server + "/v1/charges?customer=cus_secret", headers=auth)  # origin: requests
    in_job(work)

    root, (call,) = calls(sender)
    port = server.rsplit(":", 1)[1]
    assert call["kind"] == 3 and call["parentSpanId"] == root["spanId"] and call["traceId"] == root["traceId"]
    assert call["name"] == "GET 127.0.0.1"
    assert attr(call, "http.request.method") == "GET"
    assert attr(call, "server.address") == "127.0.0.1"
    assert attr(call, "server.port") == port
    assert attr(call, "http.response.status_code") == "200"
    assert attr(call, "code.file.path") == "tests/test_http_client.py"
    assert attr(call, "code.line.number") == str(line_of(HERE, "requests"))
    assert "status" not in call
    assert int(root["startTimeUnixNano"]) <= int(call["startTimeUnixNano"]) <= int(call["endTimeUnixNano"])
    assert int(call["endTimeUnixNano"]) <= int(root["endTimeUnixNano"])
    payload = sender.payloads[0].decode()
    for secret in ("v1/charges", "cus_secret", "sk_secret", "Bearer", "http://"):
        assert secret not in payload


def test_a_redirect_followed_by_requests_is_still_one_call(tracer, sender, server):
    in_job(lambda: requests.get(server + "/redirect"))
    _, found = calls(sender)
    assert len(found) == 1
    assert attr(found[0], "http.response.status_code") == "200"


def test_an_httpx_call_is_one_span_not_two(tracer, sender, server):
    def work():
        with httpx.Client() as client:
            client.post(server + "/v1/refunds?id=re_secret", json={"amount": 1})  # origin: httpx
    in_job(work)

    _, (call,) = calls(sender)
    assert call["name"] == "POST 127.0.0.1"
    assert attr(call, "http.response.status_code") == "200"
    assert attr(call, "code.line.number") == str(line_of(HERE, "httpx"))
    assert "re_secret" not in sender.payloads[0].decode()


def test_an_async_httpx_call_inside_a_task(tracer, sender, server):
    async def work():
        with slowpoke.job("async"):
            async with httpx.AsyncClient() as client:
                await client.get(server + "/ok")  # origin: async
    asyncio.run(work())

    _, (call,) = calls(sender)
    assert call["name"] == "GET 127.0.0.1"
    assert attr(call, "http.response.status_code") == "200"
    assert attr(call, "code.file.path") == "tests/test_http_client.py"
    assert attr(call, "code.line.number") == str(line_of(HERE, "async"))


def test_a_5xx_is_an_error(tracer, sender, server):
    in_job(lambda: requests.get(server + "/broken"))
    _, (call,) = calls(sender)
    assert call["status"] == {"code": 2}
    assert attr(call, "http.response.status_code") == "503"


def test_a_connection_that_fails_is_an_error_and_the_app_still_gets_it(tracer, sender, closed_port):
    def work():
        with pytest.raises(requests.ConnectionError):
            requests.get("http://127.0.0.1:%d/x?token=zzz" % closed_port, timeout=2)
        with pytest.raises(httpx.ConnectError):
            httpx.get("http://127.0.0.1:%d/x?token=zzz" % closed_port, timeout=2)
    in_job(work)

    _, found = calls(sender)
    assert len(found) == 2
    for call in found:
        assert call["status"] == {"code": 2}
        assert attr(call, "http.response.status_code") is None
    assert "zzz" not in sender.payloads[0].decode()


def test_calls_outside_a_trace_are_ignored(tracer, sender, server):
    requests.get(server + "/ok")
    httpx.get(server + "/ok")
    assert sender.payloads == []


def test_the_cap_counts_the_rest(sender, server):
    slowpoke.configure(sender=sender, background=False, code_root=ROOT, max_http_calls=2)
    try:
        def work():
            for _ in range(5):
                requests.get(server + "/ok")
        in_job(work)
    finally:
        slowpoke.reset()
    root, found = calls(sender)
    assert len(found) == 2
    assert attr(root, "slowpoke.dropped_http_calls") == "3"


def test_calls_to_the_agent_are_never_traced(sender, server):
    slowpoke.configure(sender=sender, background=False, code_root=ROOT, endpoint=server + "/v1/traces")
    try:
        in_job(lambda: requests.post(server + "/v1/traces", data=b"{}"))
    finally:
        slowpoke.reset()
    _, found = calls(sender)
    assert found == []


def test_the_switch_turns_it_off(sender, server, monkeypatch):
    monkeypatch.setenv("SLOWPOKE_HTTP_CLIENT", "false")
    slowpoke.configure(sender=sender, background=False, code_root=ROOT)
    try:
        in_job(lambda: (requests.get(server + "/ok"), httpx.get(server + "/ok")))
    finally:
        slowpoke.reset()
    _, found = calls(sender)
    assert found == []


def test_default_ports_are_not_sent():
    got = FakeSender()
    t = slowpoke.configure(sender=got, background=False, code_root=ROOT)
    try:
        trace = t.start_job("x")
        call = t.start_http_call("GET", "https://API.Stripe.com:443/v1/charges")
        t.finish_http_call(call, 200)
        t.finish_job(trace)
    finally:
        slowpoke.reset()
    _, (span,) = calls(got)
    assert span["name"] == "GET api.stripe.com"
    assert attr(span, "server.port") is None


def test_a_call_never_finished_ends_with_its_trace_as_an_error(tracer, sender):
    trace = tracer.start_job("x")
    tracer.start_http_call("GET", "https://slow.example/a")
    tracer.finish_job(trace)
    root, (span,) = calls(sender)
    assert span["endTimeUnixNano"] == root["endTimeUnixNano"]
    assert span["status"] == {"code": 2}


def test_installing_twice_wraps_once():
    _http.install()
    _http.install()
    assert not getattr(requests.Session.send.__wrapped__, "__wrapped__", None)
    assert not getattr(httpx.Client.send.__wrapped__, "__wrapped__", None)


def test_a_broken_tracer_never_breaks_the_call(sender, server, monkeypatch):
    t = slowpoke.configure(sender=sender, background=False, code_root=ROOT)
    try:
        def boom(*args, **kwargs):
            raise RuntimeError("tracer exploded")
        monkeypatch.setattr(t, "start_http_call", boom)
        with slowpoke.job("x"):
            assert requests.get(server + "/ok").json() == {"ok": True}
            assert json.loads(httpx.get(server + "/ok").text) == {"ok": True}
    finally:
        slowpoke.reset()
    assert len(sender.payloads) == 1
    assert split(sender.decoded()[0])[1] == []
