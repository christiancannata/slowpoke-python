import socket
import time

import pytest

flask = pytest.importorskip("flask")

from apps import flask_shop, sa_shop  # noqa: E402
from conftest import ROOT, ThrowingSender, attr, line_of, rel, split  # noqa: E402

import slowpoke  # noqa: E402

SHOP = sa_shop.__file__


@pytest.fixture(scope="module")
def engine():
    engine = sa_shop.make_engine()
    with engine.begin() as conn:
        sa_shop.seed(conn)
    yield engine
    slowpoke.sqlalchemy.uninstrument(engine)
    engine.dispose()


@pytest.fixture(scope="module")
def app(engine):
    return flask_shop.create_app(engine)


@pytest.fixture
def client(app, tracer):
    return app.test_client()


def roots(sender):
    return [split(p)[0] for p in sender.decoded()]


def test_route_status_and_n_plus_one_with_application_lines(client, sender):
    assert client.get("/orders").status_code == 200
    root, queries = sender.only_trace()
    assert root["kind"] == 2 and root["name"] == "GET /orders"
    assert attr(root, "http.route") == "/orders"
    assert attr(root, "http.response.status_code") == "200"
    lines = [(attr(q, "code.file.path"), attr(q, "code.line.number")) for q in queries]
    assert lines == [(rel(SHOP), str(line_of(SHOP, "sa list")))] + [(rel(SHOP), str(line_of(SHOP, "sa n+1")))] * 6
    assert len({attr(q, "db.query.text") for q in queries[1:]}) == 1


def test_route_template_with_converter_and_view_404(client, sender):
    assert client.put("/api/orders/3").status_code == 200
    assert client.get("/api/orders/999").status_code == 404
    r = roots(sender)
    assert [attr(x, "http.route") for x in r] == ["/api/orders/<int:order_id>"] * 2
    assert [attr(x, "http.request.method") for x in r] == ["PUT", "GET"]
    assert [attr(x, "http.response.status_code") for x in r] == ["200", "404"]
    _, (q,) = split(sender.decoded()[0])
    assert attr(q, "code.file.path") == rel(SHOP)


def test_blueprint_route(client, sender):
    assert client.get("/admin/stats/daily").status_code == 200
    root, _ = sender.only_trace()
    assert attr(root, "http.route") == "/admin/stats/<name>"


def test_unmatched_path(client, sender):
    assert client.get("/nope?token=sekrit").status_code == 404
    root, queries = sender.only_trace()
    assert attr(root, "url.path") == "/nope" and attr(root, "http.route") is None and queries == []
    assert b"sekrit" not in sender.payloads[0]


def test_unhandled_exception_is_a_500(app, client, sender):
    app.testing = False
    try:
        assert client.get("/boom").status_code == 500
    finally:
        app.testing = True
    root, (q,) = sender.only_trace()
    assert attr(root, "http.response.status_code") == "500" and root["status"] == {"code": 2}


def test_a_hook_answering_before_the_view_is_still_traced(client, sender):
    assert client.get("/orders?blocked=1").status_code == 403
    root, queries = sender.only_trace()
    assert attr(root, "http.response.status_code") == "403" and queries == []


def test_parameter_values_never_leave_the_app(client, sender):
    assert client.get("/customers/lookup", query_string={"email": "c2@secret.example"}).data == b"Customer 2"
    assert b"secret" not in sender.payloads[0] and b"Customer 2" not in sender.payloads[0]


def test_one_trace_per_request(client, sender):
    client.get("/api/orders/1")
    client.get("/api/orders/2")
    ids = [r["traceId"] for r in roots(sender)]
    assert len(ids) == 2 and ids[0] != ids[1]


def test_disabled_sends_nothing(app, monkeypatch):
    monkeypatch.setenv("SLOWPOKE_ENABLED", "false")
    slowpoke.reset()
    try:
        assert app.test_client().get("/orders").status_code == 200
        assert slowpoke.get_tracer() is None
    finally:
        slowpoke.reset()


def test_an_unreachable_or_slow_agent_never_slows_the_request(app, monkeypatch):
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(8)
    for endpoint in ["http://127.0.0.1:1/v1/traces", "http://127.0.0.1:%d/v1/traces" % silent.getsockname()[1]]:
        monkeypatch.setenv("SLOWPOKE_OTLP_ENDPOINT", endpoint)
        monkeypatch.setenv("SLOWPOKE_TIMEOUT", "2")
        slowpoke.reset()
        try:
            client = app.test_client()
            started = time.monotonic()
            for _ in range(5):
                assert client.get("/orders").status_code == 200
            assert time.monotonic() - started < 1.5
        finally:
            slowpoke.reset()
    silent.close()


def test_a_failing_sender_never_breaks_the_request(app):
    slowpoke.configure(sender=ThrowingSender(), background=False, code_root=ROOT)
    try:
        assert app.test_client().get("/orders").status_code == 200
    finally:
        slowpoke.reset()


def test_init_app_twice_records_once(app, client, sender):
    slowpoke.flask.init_app(app)
    assert client.get("/api/orders/1").status_code == 200
    _, queries = sender.only_trace()
    assert len(queries) == 1
