import socket
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("aiosqlite")

from apps import fastapi_shop, sa_shop  # noqa: E402
from conftest import ROOT, ThrowingSender, attr, line_of, rel, split  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import slowpoke  # noqa: E402
import slowpoke.sqlalchemy  # noqa: E402

SHOP = sa_shop.__file__


@pytest.fixture(scope="module")
def engines(tmp_path_factory):
    db = str(tmp_path_factory.mktemp("fastapi") / "shop.db")
    engine = create_engine("sqlite:///" + db, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        sa_shop.seed(conn)
    async_engine = create_async_engine("sqlite+aiosqlite:///" + db, poolclass=NullPool)
    slowpoke.sqlalchemy.instrument(engine)
    slowpoke.sqlalchemy.instrument(async_engine)
    yield engine, async_engine
    slowpoke.sqlalchemy.uninstrument(engine)
    slowpoke.sqlalchemy.uninstrument(async_engine)
    engine.dispose()


@pytest.fixture(scope="module")
def app(engines):
    return fastapi_shop.create_app(*engines)


@pytest.fixture
def client(app, tracer):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def lines(queries):
    return [(attr(q, "code.file.path"), attr(q, "code.line.number")) for q in queries]


def test_sync_endpoint_in_the_threadpool(client, sender):
    assert client.get("/orders").status_code == 200
    root, queries = sender.only_trace()
    assert root["kind"] == 2 and root["name"] == "GET /orders"
    assert attr(root, "http.route") == "/orders" and attr(root, "http.response.status_code") == "200"
    assert lines(queries) == [(rel(SHOP), str(line_of(SHOP, "sa list")))] + [
        (rel(SHOP), str(line_of(SHOP, "sa n+1")))] * 6


def test_async_endpoint_with_async_engine(client, sender):
    assert client.get("/async/orders").status_code == 200
    root, queries = sender.only_trace()
    assert attr(root, "http.route") == "/async/orders"
    assert lines(queries) == [(rel(SHOP), str(line_of(SHOP, "sa async list")))] + [
        (rel(SHOP), str(line_of(SHOP, "sa async n+1")))] * 6
    assert {attr(q, "db.query.text") for q in queries[1:]} == {
        "SELECT customers.name \nFROM customers \nWHERE customers.id = ?"}


def test_route_template_and_404_from_the_endpoint(client, sender):
    assert client.put("/orders/3").status_code == 200
    assert client.get("/orders/999").status_code == 404
    roots = [split(p)[0] for p in sender.decoded()]
    assert [attr(r, "http.route") for r in roots] == ["/orders/{order_id}"] * 2
    assert [attr(r, "http.request.method") for r in roots] == ["PUT", "GET"]
    assert [attr(r, "http.response.status_code") for r in roots] == ["200", "404"]


def test_router_prefix_and_mounted_app(client, sender):
    assert client.get("/api/items/abc").status_code == 200
    assert client.get("/sub/reports/2026").status_code == 200
    roots = [split(p)[0] for p in sender.decoded()]
    assert [attr(r, "http.route") for r in roots] == ["/api/items/{item_id}", "/sub/reports/{year}"]
    assert len(split(sender.decoded()[1])[1]) == 1


def test_unmatched_path(client, sender):
    assert client.get("/nope?token=sekrit").status_code == 404
    root, queries = sender.only_trace()
    assert attr(root, "url.path") == "/nope" and attr(root, "http.route") is None and queries == []
    assert b"sekrit" not in sender.payloads[0]


def test_unhandled_exception_is_a_500(client, sender):
    assert client.get("/boom").status_code == 500
    root, (q,) = sender.only_trace()
    assert attr(root, "http.response.status_code") == "500" and root["status"] == {"code": 2}
    assert attr(root, "http.route") == "/boom"


def test_parameter_values_never_leave_the_app(client, sender):
    assert client.get("/lookup", params={"email": "c2@secret.example"}).json() == "Customer 2"
    assert b"secret" not in sender.payloads[0] and b"Customer 2" not in sender.payloads[0]


def test_lifespan_and_queries_outside_requests(client, sender, engines):
    sa_shop.customer_names(engines[0])
    assert sender.payloads == []


def test_plain_starlette_routes(engines, tracer, sender):
    app = fastapi_shop.create_starlette_app(engines[0])
    with TestClient(app) as c:
        assert c.get("/hello/ada").text == "0"
        assert c.get("/admin/users/7").text == "nested"
    roots = [split(p)[0] for p in sender.decoded()]
    assert [attr(r, "http.route") for r in roots] == ["/hello/{name}", "/admin/users/{user_id:int}"]
    assert lines(split(sender.decoded()[0])[1]) == [(rel(SHOP), str(line_of(SHOP, "sa orm")))]


def test_disabled_sends_nothing(app, monkeypatch):
    monkeypatch.setenv("SLOWPOKE_ENABLED", "false")
    slowpoke.reset()
    try:
        with TestClient(app) as c:
            assert c.get("/orders").status_code == 200
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
            with TestClient(app) as c:
                started = time.monotonic()
                for _ in range(5):
                    assert c.get("/async/orders").status_code == 200
                assert time.monotonic() - started < 1.5
        finally:
            slowpoke.reset()
    silent.close()


def test_a_failing_sender_never_breaks_the_request(app):
    slowpoke.configure(sender=ThrowingSender(), background=False, code_root=ROOT)
    try:
        with TestClient(app) as c:
            assert c.get("/orders").status_code == 200
    finally:
        slowpoke.reset()
