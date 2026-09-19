import asyncio
import os
import threading
import time

import pytest

django = pytest.importorskip("django")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apps.django_shop.settings")
django.setup()

from apps.django_shop import views  # noqa: E402
from apps.django_shop.models import Customer, Order  # noqa: E402
from conftest import ROOT, ThrowingSender, attr, line_of, rel  # noqa: E402
from django.db import connection, connections  # noqa: E402
from django.test import AsyncClient, Client  # noqa: E402

import slowpoke  # noqa: E402
from slowpoke.django import route_template  # noqa: E402

VIEWS = views.__file__


@pytest.fixture(scope="module", autouse=True)
def schema():
    with connection.schema_editor() as editor:
        editor.create_model(Customer)
        editor.create_model(Order)
    for i in range(6):
        c = Customer.objects.create(name="Customer %d" % i, email="c%d@secret.example" % i)
        Order.objects.create(customer=c, total=10 * i)
    yield


@pytest.fixture
def client(tracer):
    return Client(raise_request_exception=False)


def queries_by_origin(queries):
    out = {}
    for q in queries:
        out.setdefault((attr(q, "code.file.path"), attr(q, "code.line.number")), []).append(attr(q, "db.query.text"))
    return out


def test_route_status_and_queries_with_their_application_line(client, sender):
    assert client.get("/orders/").status_code == 200
    root, queries = sender.only_trace()
    assert root["kind"] == 2
    assert attr(root, "http.route") == "/orders/"
    assert attr(root, "http.request.method") == "GET"
    assert attr(root, "http.response.status_code") == "200"
    by_origin = queries_by_origin(queries)
    assert set(by_origin) == {
        (rel(VIEWS), str(line_of(VIEWS, "list"))),
        (rel(VIEWS), str(line_of(VIEWS, "n+1"))),
    }, by_origin
    assert rel(VIEWS) == "tests/apps/django_shop/views.py"
    # N+1: the same statement six times in one trace, from one line
    (repeated,) = [sqls for sqls in by_origin.values() if len(sqls) >= 5]
    assert len(repeated) == 6 and len(set(repeated)) == 1
    # Django hands the driver %s placeholders: the agent normalizes them
    assert "%s" in repeated[0]
    for q in queries:
        assert attr(q, "db.system.name") == "sqlite"
        assert q["parentSpanId"] == root["spanId"]
        assert int(root["startTimeUnixNano"]) <= int(q["startTimeUnixNano"]) <= int(q["endTimeUnixNano"]) <= int(
            root["endTimeUnixNano"])


def test_included_route_template_and_404_from_the_view(client, sender):
    assert client.get("/api/orders/3/").status_code == 200
    assert client.get("/api/orders/999/").status_code == 404
    roots = [p["resourceSpans"][0]["scopeSpans"][0]["spans"][0] for p in sender.decoded()]
    assert [attr(r, "http.route") for r in roots] == ["/api/orders/<int:pk>/", "/api/orders/<int:pk>/"]
    assert [attr(r, "http.response.status_code") for r in roots] == ["200", "404"]


def test_regex_route(client, sender):
    assert client.get("/legacy/big-sale/").status_code == 200
    root, (q,) = sender.only_trace()
    assert attr(root, "http.route") == "/legacy/{slug}/"
    assert attr(q, "code.line.number") == str(line_of(VIEWS, "legacy"))


def test_unmatched_path(client, sender):
    assert client.get("/nope?token=sekrit").status_code == 404
    root, queries = sender.only_trace()
    assert attr(root, "url.path") == "/nope" and attr(root, "http.route") is None
    assert b"sekrit" not in sender.payloads[0]


def test_server_error(client, sender):
    assert client.get("/boom/").status_code == 500
    root, (q,) = sender.only_trace()
    assert attr(root, "http.response.status_code") == "500" and root["status"] == {"code": 2}
    assert attr(q, "code.line.number") == str(line_of(VIEWS, "boom"))


def test_parameter_values_never_leave_the_app(client, sender):
    assert client.get("/customers/lookup", {"email": "c2@secret.example"}).status_code == 200
    _, (q,) = sender.only_trace()
    assert "%s" in attr(q, "db.query.text")
    assert b"secret" not in sender.payloads[0] and b"Customer 2" not in sender.payloads[0]


def test_async_view_and_async_middleware(tracer, sender):
    async def go():
        return await AsyncClient().get("/async/orders/")

    assert asyncio.run(go()).status_code == 200
    root, (q,) = sender.only_trace()
    assert attr(root, "http.route") == "/async/orders/"
    assert attr(q, "code.file.path") == rel(VIEWS)
    assert attr(q, "code.line.number") == str(line_of(VIEWS, "async"))


def test_queries_outside_requests_are_not_recorded(tracer, sender):
    Order.objects.count()
    assert sender.payloads == []


def test_job_outside_requests(tracer, sender):
    with slowpoke.job("recount", queue="cron"):
        Order.objects.count()  # origin: job
    root, (q,) = sender.only_trace()
    assert root["kind"] == 5 and root["name"] == "recount"
    assert (attr(q, "code.file.path"), attr(q, "code.line.number")) == (
        "tests/test_django.py", str(line_of(__file__, "job")))


def test_connections_opened_later_in_other_threads(tracer, sender):
    def run():
        with slowpoke.job("thread"):
            Customer.objects.count()  # origin: thread
        connections.close_all()

    th = threading.Thread(target=run)
    th.start()
    th.join()
    _, (q,) = sender.only_trace()
    assert attr(q, "code.line.number") == str(line_of(__file__, "thread"))


def test_every_statement_counted_once(tracer, sender):
    from slowpoke.django import install

    install()
    install()
    with slowpoke.job("twice"):
        Order.objects.count()
    _, queries = sender.only_trace()
    assert len(queries) == 1


def test_disabled_sends_nothing(monkeypatch):
    monkeypatch.setenv("SLOWPOKE_ENABLED", "false")
    slowpoke.reset()
    try:
        assert Client().get("/orders/").status_code == 200
        assert slowpoke.get_tracer() is None
    finally:
        slowpoke.reset()


def test_an_unreachable_or_slow_agent_never_slows_the_request(monkeypatch):
    import socket

    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(8)  # accepts connections and never answers
    for endpoint in ["http://127.0.0.1:1/v1/traces", "http://127.0.0.1:%d/v1/traces" % silent.getsockname()[1]]:
        monkeypatch.setenv("SLOWPOKE_OTLP_ENDPOINT", endpoint)
        monkeypatch.setenv("SLOWPOKE_TIMEOUT", "2")  # even with a generous budget the request never waits
        slowpoke.reset()
        try:
            started = time.monotonic()
            for _ in range(5):
                assert Client().get("/orders/").status_code == 200
            assert time.monotonic() - started < 1.5
        finally:
            slowpoke.reset()
    silent.close()


def test_a_failing_sender_never_breaks_the_request():
    slowpoke.configure(sender=ThrowingSender(), background=False, code_root=ROOT)
    try:
        assert Client().get("/orders/").status_code == 200
    finally:
        slowpoke.reset()


def test_route_template_conversion():
    assert route_template("orders/<int:pk>/") == "orders/<int:pk>/"
    assert route_template(r"^legacy/(?P<slug>[-\w]+)/$") == "legacy/{slug}/"
    assert route_template(r"^a/(?P<y>[0-9]{4})/(?P<m>(0[1-9]|1[0-2]))\.json\Z") == "a/{y}/{m}.json"
    assert route_template(r"^files/(.+)$") == "files/{param}"
    assert route_template("") == ""


def test_code_root_defaults_to_base_dir(monkeypatch):
    from django.conf import settings

    slowpoke.reset()
    monkeypatch.delenv("SLOWPOKE_CODE_ROOT", raising=False)
    try:
        from slowpoke.django import install

        install()
        tracer = slowpoke.get_tracer()
        assert tracer.origin.root == os.path.join(str(settings.BASE_DIR), "")
        assert tracer.service == os.path.basename(str(settings.BASE_DIR))
    finally:
        slowpoke.reset()


def test_a_management_command_is_a_trace_of_its_own(tracer, sender):
    """Cron is where the slow work hides: nobody waits for the nightly command, so nobody notices
    when it takes four minutes instead of forty seconds."""
    from django.core.management import call_command

    call_command("close_orders")

    root, queries = sender.only_trace()
    assert root["kind"] == 5 and root["name"] == "close_orders"
    assert attr(root, "slowpoke.kind") == "command"
    assert root.get("status") is None
    assert len(queries) == 1
    assert attr(queries[0], "code.file.path") == rel(
        os.path.join(os.path.dirname(__file__), "apps/django_shop/management/commands/close_orders.py"))


def test_a_command_that_raises_is_a_failed_run(tracer, sender):
    from django.core.management import call_command

    with pytest.raises(ValueError):
        call_command("close_orders", "--fail")

    root, _ = sender.only_trace()
    assert root["status"] == {"code": 2}


def test_the_commands_that_never_end_are_left_alone(tracer, sender):
    """runserver and the worker commands would hold one trace open for as long as the process
    lives, and every request or task inside it would be lost."""
    from django.core.management import call_command

    from slowpoke.django import LONG_RUNNING

    assert "runworker" in LONG_RUNNING and "runserver" in LONG_RUNNING
    call_command("runworker")
    assert sender.payloads == []
