import asyncio
import contextvars
import threading

import pytest
from conftest import FakeSender, attr, split

import slowpoke
from slowpoke._config import Config
from slowpoke._tracer import Tracer, current


class Clock:
    def __init__(self):
        self.now = 1760000000.0

    def __call__(self):
        return self.now


class FixedOrigin:
    def __init__(self):
        self.at = ("shop/views.py", 12)

    def find(self, task=None):
        return self.at


def make(**options):
    sender = FakeSender()
    clock = Clock()
    origin = FixedOrigin()
    options.setdefault("service", "shop")
    t = Tracer(origin=origin, submit=lambda trace: sender.send(t.encode_json(trace)), clock=clock, **options)
    return t, sender, clock, origin


def test_request_trace():
    t, sender, clock, origin = make()
    trace = t.start_request("get")
    clock.now += 0.02
    t.record_query("SELECT * FROM orders WHERE id = %s", 0.005, "sqlite")
    clock.now += 0.01
    t.finish_request(trace, "orders/<int:pk>/", "/orders/3/?token=x", 200)

    root, queries = sender.only_trace()
    assert root["kind"] == 2
    assert root["name"] == "GET /orders/<int:pk>/"
    assert attr(root, "http.route") == "/orders/<int:pk>/"
    assert attr(root, "http.request.method") == "GET"
    assert attr(root, "http.response.status_code") == "200"
    assert attr(root, "url.path") is None
    assert "status" not in root
    assert root["startTimeUnixNano"] == "1760000000000000000"
    assert root["endTimeUnixNano"] == "1760000000030000000"
    (q,) = queries
    assert q["traceId"] == root["traceId"] and q["parentSpanId"] == root["spanId"]
    assert len(root["traceId"]) == 32 and len(root["spanId"]) == 16 and q["spanId"] != root["spanId"]
    assert q["name"] == "SELECT"
    assert attr(q, "db.system.name") == "sqlite"
    assert attr(q, "db.query.text") == "SELECT * FROM orders WHERE id = %s"
    assert attr(q, "code.file.path") == "shop/views.py"
    assert attr(q, "code.line.number") == "12"
    assert q["startTimeUnixNano"] == "1760000000015000000"
    assert q["endTimeUnixNano"] == "1760000000020000000"
    resource = sender.decoded()[0]["resourceSpans"][0]
    assert attr(resource["resource"], "service.name") == "shop"
    assert attr(resource["resource"], "telemetry.sdk.language") == "python"
    assert current() is None


def test_unmatched_route_sends_the_path_without_query_string():
    t, sender, clock, _ = make()
    trace = t.start_request("GET")
    t.finish_request(trace, None, "/nope?token=sekrit", 404)
    root, _ = sender.only_trace()
    assert attr(root, "url.path") == "/nope"
    assert attr(root, "http.route") is None
    assert root["name"] == "GET"
    assert b"sekrit" not in sender.payloads[0]


def test_server_errors_mark_the_span():
    t, sender, _, _ = make()
    t.finish_request(t.start_request("POST"), "/boom", "/boom", 500)
    root, _ = sender.only_trace()
    assert root["status"] == {"code": 2}


def test_query_start_never_before_the_trace():
    t, sender, clock, _ = make()
    trace = t.start_request("GET")
    clock.now += 0.001
    t.record_query("select 1", 5.0, "sqlite")
    t.finish_request(trace, "/", "/", 200)
    _, (q,) = sender.only_trace()
    assert q["startTimeUnixNano"] == "1760000000000000000"


def test_queries_outside_a_trace_or_after_it_are_ignored():
    t, sender, _, _ = make()
    t.record_query("select 1", 0.001, "sqlite")
    trace = t.start_request("GET")
    t.finish_request(trace, "/", "/", 200)
    t.record_query("select 2", 0.001, "sqlite")
    _, queries = sender.only_trace()
    assert queries == []


def test_query_without_origin_or_line():
    t, sender, _, origin = make()
    trace = t.start_request("GET")
    origin.at = None
    t.record_query("select 1", 0.001, "sqlite")
    origin.at = ("templates/orders.html", None)
    t.record_query("select 2", 0.001, "sqlite")
    t.finish_request(trace, "/", "/", 200)
    _, (a, b) = sender.only_trace()
    assert attr(a, "code.file.path") is None
    assert attr(b, "code.file.path") == "templates/orders.html" and attr(b, "code.line.number") is None


def test_limits():
    t, sender, _, _ = make(max_queries=3, max_sql_length=10)
    trace = t.start_request("GET")
    for _ in range(5):
        t.record_query("select * from a_very_long_table", 0.001, "postgresql")
    t.finish_request(trace, "/", "/", 200)
    root, queries = sender.only_trace()
    assert len(queries) == 3
    assert attr(queries[0], "db.query.text") == "select * f"
    assert attr(root, "slowpoke.dropped_queries") == "2"


def test_job_trace_and_jobs_inside_a_request():
    t, sender, clock, _ = make()
    job = t.start_job("send_invoices", "emails")
    assert t.start_job("nested", None) is None  # belongs to the running job
    t.record_query("select * from invoices", 0.2, "mysql")
    clock.now += 0.3
    t.finish_job(job, failed=True)
    root, (q,) = sender.only_trace()
    assert root["kind"] == 5 and root["name"] == "send_invoices"
    assert attr(root, "messaging.destination.name") == "emails"
    assert root["status"] == {"code": 2}
    assert q["parentSpanId"] == root["spanId"]

    request = t.start_request("GET")
    assert t.start_job("inline", None) is None
    t.finish_request(request, "/", "/", 200)
    assert len(sender.payloads) == 2


def test_a_new_request_replaces_a_stale_trace():
    t, sender, _, _ = make()
    t.start_request("GET")  # never finished: an exception outside our hooks
    trace = t.start_request("GET")
    t.record_query("select 1", 0.001, "sqlite")
    t.finish_request(trace, "/", "/", 200)
    _, queries = sender.only_trace()
    assert len(queries) == 1


def test_db_system_names():
    t, sender, _, _ = make()
    trace = t.start_request("GET")
    for driver in ["postgresql", "mssql", "mysql", "mariadb", "sqlite", "oracle"]:
        t.record_query("select 1", 0.001, driver)
    t.finish_request(trace, "/", "/", 200)
    _, queries = sender.only_trace()
    assert [attr(q, "db.system.name") for q in queries] == [
        "postgresql", "microsoft.sql_server", "mysql", "mariadb", "sqlite", "oracle"]


def test_statement_name():
    t, sender, _, _ = make()
    trace = t.start_request("GET")
    for sql in ["  with x as (select 1) select * from x", "(select 1)", "\nUPDATE t SET a = 1", ""]:
        t.record_query(sql, 0.001, "sqlite")
    t.finish_request(trace, "/", "/", 200)
    _, queries = sender.only_trace()
    assert [q["name"] for q in queries] == ["WITH", "SELECT", "UPDATE", "QUERY"]


def test_broken_origin_or_submit_never_raise():
    t, sender, _, origin = make()

    class Broken:
        def find(self, task=None):
            raise RuntimeError("frames")

    t.origin = Broken()
    trace = t.start_request("GET")
    t.record_query("select 1", 0.001, "sqlite")

    def explode(trace):
        raise RuntimeError("queue")

    t.submit = explode
    t.finish_request(trace, "/", "/", 200)


def test_threads_have_separate_traces_and_copied_contexts_share_one():
    t, sender, _, _ = make()
    barrier = threading.Barrier(2)

    def request(n):
        trace = t.start_request("GET")
        barrier.wait()
        for _ in range(n):
            t.record_query("select %d" % n, 0.001, "sqlite")
        # a worker thread started with this context adds to the same trace
        ctx = contextvars.copy_context()
        worker = threading.Thread(target=ctx.run, args=(t.record_query, "select from worker", 0.001, "sqlite"))
        worker.start()
        worker.join()
        t.finish_request(trace, "/%d" % n, "/", 200)

    threads = [threading.Thread(target=request, args=(n,)) for n in (1, 2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    counts = sorted(len(split(p)[1]) for p in sender.decoded())
    assert counts == [2, 3]


def test_asyncio_tasks_have_separate_traces():
    t, sender, _, _ = make()

    async def request(n):
        trace = t.start_request("GET")
        await asyncio.sleep(0.01)
        for _ in range(n):
            t.record_query("select %d" % n, 0.001, "sqlite")
            await asyncio.sleep(0)
        t.finish_request(trace, "/%d" % n, "/", 200)

    async def main():
        await asyncio.gather(request(1), request(4))

    asyncio.run(main())
    counts = sorted(len(split(p)[1]) for p in sender.decoded())
    assert counts == [1, 4]


def test_config_from_environment():
    c = Config.from_env({})
    assert c.enabled is True
    assert c.endpoint == "http://127.0.0.1:4318/v1/traces"
    assert (c.timeout, c.max_queries, c.max_sql_length) == (0.1, 500, 10000)
    assert c.service is None and c.code_root is None

    c = Config.from_env({
        "SLOWPOKE_ENABLED": "false", "SLOWPOKE_OTLP_ENDPOINT": "http://agent:4318/v1/traces", "SLOWPOKE_SERVICE": "shop",
        "SLOWPOKE_MAX_QUERIES": "50", "SLOWPOKE_CODE_ROOT": "/srv/app", "SLOWPOKE_TIMEOUT": "0.3",
    })
    assert (c.enabled, c.endpoint, c.service, c.max_queries, c.code_root, c.timeout) == (
        False, "http://agent:4318/v1/traces", "shop", 50, "/srv/app", 0.3)
    for off in ["0", "no", "off", "FALSE", " false "]:
        assert Config.from_env({"SLOWPOKE_ENABLED": off}).enabled is False
    # nonsense falls back to defaults instead of breaking the boot
    c = Config.from_env({"SLOWPOKE_MAX_QUERIES": "lots", "SLOWPOKE_TIMEOUT": "-1"})
    assert (c.max_queries, c.timeout) == (500, 0.1)


def test_global_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("SLOWPOKE_ENABLED", "false")
    slowpoke.reset()
    try:
        assert slowpoke.get_tracer() is None
        monkeypatch.setenv("SLOWPOKE_ENABLED", "true")
        monkeypatch.setenv("SLOWPOKE_OTLP_ENDPOINT", "http://collector.example.com/v1/traces")
        slowpoke.reset()
        assert slowpoke.get_tracer() is None  # public endpoint: nothing may be sent, so nothing is recorded
        monkeypatch.delenv("SLOWPOKE_OTLP_ENDPOINT")
        slowpoke.reset()
        monkeypatch.chdir(tmp_path)
        t = slowpoke.get_tracer()
        assert t is not None and t is slowpoke.get_tracer()
        assert t.service == tmp_path.name and t.origin.root == str(tmp_path) + "/"
        # framework defaults apply only where the environment is silent
        slowpoke.set_defaults(code_root="/srv/app", service="shop")
        assert slowpoke.get_tracer().service == "shop"
        monkeypatch.setenv("SLOWPOKE_SERVICE", "billing")
        slowpoke.reset()
        slowpoke.set_defaults(code_root="/srv/app", service="shop")
        assert slowpoke.get_tracer().service == "billing"
        assert slowpoke.get_tracer().origin.root == "/srv/app/"
    finally:
        slowpoke.reset()


def test_job_helper(tracer, sender):
    @slowpoke.job("nightly", queue="cron")
    def nightly():
        tracer.record_query("select 1", 0.001, "sqlite")
        return 7

    assert nightly() == 7
    with pytest.raises(ValueError):
        with slowpoke.job("failing"):
            raise ValueError("x")

    @slowpoke.job()
    async def async_job():
        tracer.record_query("select 2", 0.001, "sqlite")

    asyncio.run(async_job())
    roots = [split(p)[0] for p in sender.decoded()]
    assert [r["name"] for r in roots] == ["nightly", "failing", "test_tracer.test_job_helper.<locals>.async_job"]
    assert [r.get("status") for r in roots] == [None, {"code": 2}, None]


def test_job_helper_when_disabled(monkeypatch):
    monkeypatch.setenv("SLOWPOKE_ENABLED", "false")
    slowpoke.reset()
    try:
        with slowpoke.job("x"):
            pass
    finally:
        slowpoke.reset()


def test_a_trace_says_what_it_is():
    """The agent files jobs and cron under Jobs, and only this attribute tells them apart from an
    endpoint. Without it a nightly command looked like a route named after itself."""
    t, sender, clock, _ = make()
    t.finish_job(t.start_job("send_invoices", "emails"), failed=False)
    t.finish_job(t.start_command("import_orders"), failed=False)
    request = t.start_request("GET")
    t.finish_request(request, "/orders", "/orders", 200)

    kinds = [attr(split(p)[0], "slowpoke.kind") for p in sender.decoded()]
    assert kinds == ["job", "command", None]


def test_command_helper(tracer, sender):
    @slowpoke.command("import_orders")
    def nightly():
        tracer.record_query("select 1", 0.001, "sqlite")

    nightly()
    with pytest.raises(ValueError):
        with slowpoke.command("broken_cron"):
            raise ValueError("x")

    roots = [split(p)[0] for p in sender.decoded()]
    assert [r["name"] for r in roots] == ["import_orders", "broken_cron"]
    assert [attr(r, "slowpoke.kind") for r in roots] == ["command", "command"]
    assert [r.get("status") for r in roots] == [None, {"code": 2}]


def test_microseconds_are_rounded_the_same_way_in_every_language():
    """Python's round() goes to the even digit: round(0.5) is 0, and the PHP packages use
    floor(x + 0.5). The same measure must produce the same trace in every language."""
    from slowpoke._tracer import _nanos

    assert _nanos(1760000000.0000005) == "1760000000000001000"
    assert _nanos(1760000000.0000015) == "1760000000000002000"
