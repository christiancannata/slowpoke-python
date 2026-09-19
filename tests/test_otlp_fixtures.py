"""spec/python_otlp_fixtures.json holds payloads exactly as this package sends them, with what the agent
must read from each one. The Go receiver test replays them (internal/agent/python_otlp_fixtures_test.go):
change both together. Regenerate with UPDATE_FIXTURES=1 ./bin/test 3.13 tests/test_otlp_fixtures.py

Statements and route templates are written the way each framework and driver produces them: Django's
%s, psycopg2's %(name)s, sqlite3's ?, Django's <int:pk>, Flask's <int:order_id>, FastAPI's {order_id}.
"""

import json
import os

import pytest
from conftest import ROOT

from slowpoke._tracer import Tracer
from slowpoke.django import route_template

FILE = os.path.join(os.path.dirname(os.path.dirname(ROOT)), "spec", "python_otlp_fixtures.json")


class Harness:
    def __init__(self):
        self.now = 1760000000.0
        self.at = None
        self.payloads = []
        self.n = 0

        def ids(size):
            self.n += 1
            return ("%x" % self.n).rjust(size * 2, "a" if size == 16 else "b")

        origin = type("Origin", (), {"find": lambda _, task=None: self.at})()
        self.tracer = Tracer(origin=origin, submit=lambda t: self.payloads.append(self.tracer.encode(t)),
                             service="shop", version="fixture", clock=lambda: self.now, ids=ids)

    def query(self, sql, ms, system, at, advance=0.002):
        self.now += advance
        self.at = at
        self.tracer.record_query(sql, ms / 1000.0, system)


def q(statement, n, origin, fingerprint, n_plus_one=False):
    return {"statement": statement, "n": n, "origin": origin, "fingerprint": fingerprint, "n_plus_one": n_plus_one}


def scenarios():
    out = []

    h = Harness()
    trace = h.tracer.start_request("GET")
    h.query('SELECT "shop_order"."id", "shop_order"."customer_id" FROM "shop_order" ORDER BY "shop_order"."id" ASC',
            12.5, "sqlite", ("shop/views.py", 9), advance=0.02)
    n_plus_one = ('SELECT "shop_customer"."id", "shop_customer"."name" FROM "shop_customer" '
                  'WHERE "shop_customer"."id" = %s LIMIT 21')
    for _ in range(6):
        h.query(n_plus_one, 0.8, "sqlite", ("shop/views.py", 10))
    h.now += 0.01
    h.tracer.finish_request(trace, "orders/", "/orders/", 200)
    out.append({
        "name": "Django: N+1 with %s placeholders",
        "payload": h.payloads[0],
        "expect": {"route": "GET /orders/", "status": 200, "requests": 1, "source": "otlp:shop", "queries": [
            q('SELECT "shop_order"."id", "shop_order"."customer_id" FROM "shop_order" ORDER BY "shop_order"."id" ASC',
              1, "shop/views.py:9", "select shop_order.id, shop_order.customer_id from shop_order order by shop_order.id asc"),
            q(n_plus_one, 6, "shop/views.py:10",
              "select shop_customer.id, shop_customer.name from shop_customer where shop_customer.id = ? limit ?", True),
        ]},
    })

    h = Harness()
    trace = h.tracer.start_request("GET")
    h.query('SELECT "shop_order"."total" FROM "shop_order" WHERE "shop_order"."id" = %s LIMIT 1', 1.1, "postgresql",
            ("shop/views.py", 15))
    h.tracer.finish_request(trace, "api/orders/<int:pk>/", "/api/orders/999/", 404)
    out.append({
        "name": "Django: included route with a converter, 404 raised by the view",
        "payload": h.payloads[0],
        "expect": {"route": "GET /api/orders/{pk}/", "status": 404, "requests": 1, "source": "otlp:shop", "queries": [
            q('SELECT "shop_order"."total" FROM "shop_order" WHERE "shop_order"."id" = %s LIMIT 1', 1, "shop/views.py:15",
              "select shop_order.total from shop_order where shop_order.id = ? limit ?"),
        ]},
    })

    h = Harness()
    trace = h.tracer.start_request("GET")
    h.tracer.finish_request(trace, route_template(r"^legacy/(?P<slug>[-\w]+)/$"), "/legacy/big-sale/", 200)
    out.append({
        "name": "Django: re_path route turned into a template",
        "payload": h.payloads[0],
        "expect": {"route": "GET /legacy/{slug}/", "status": 200, "requests": 1, "source": "otlp:shop", "queries": []},
    })

    h = Harness()
    trace = h.tracer.start_request("PUT")
    h.query("UPDATE orders SET status=%(status)s WHERE orders.id = %(id_1)s", 3.5, "postgresql",
            ("shop/orders.py", 40), advance=0.02)
    h.tracer.finish_request(trace, "/api/orders/<int:order_id>", "/api/orders/981", 500)
    out.append({
        "name": "Flask + SQLAlchemy over psycopg2: %(name)s placeholders, server error",
        "payload": h.payloads[0],
        "expect": {"route": "PUT /api/orders/{order_id}", "status": 500, "requests": 1, "source": "otlp:shop",
                   "queries": [q("UPDATE orders SET status=%(status)s WHERE orders.id = %(id_1)s", 1, "shop/orders.py:40",
                                 "update orders set status = ? where orders.id = ?")]},
    })

    h = Harness()
    trace = h.tracer.start_request("GET")
    sql = "SELECT orders.id, orders.total \nFROM orders \nWHERE orders.id = ?"
    h.query(sql, 0.4, "sqlite", ("app/api.py", 22))
    h.tracer.finish_request(trace, "/orders/{order_id}", "/orders/7", 200)
    out.append({
        "name": "FastAPI + async SQLAlchemy over aiosqlite: qmark placeholders",
        "payload": h.payloads[0],
        "expect": {"route": "GET /orders/{order_id}", "status": 200, "requests": 1, "source": "otlp:shop", "queries": [
            q(sql, 1, "app/api.py:22", "select orders.id, orders.total from orders where orders.id = ?")]},
    })

    h = Harness()
    trace = h.tracer.start_request("GET")
    h.tracer.finish_request(trace, None, "/.env?probe=1", 404)
    out.append({
        "name": "no matching route: the path stands in for the route",
        "payload": h.payloads[0],
        "expect": {"route": "GET /.env", "status": 404, "requests": 1, "source": "otlp:shop", "queries": []},
    })

    h = Harness()
    job = h.tracer.start_job("import_orders", "nightly")
    h.query("SELECT name FROM customers WHERE id = %s", 1.0, "mysql", ("shop/jobs.py", 31))
    h.query("SELECT name FROM customers WHERE id = ?", 1.0, "sqlite", ("shop/jobs.py", 35))
    h.query("SELECT name FROM customers WHERE id = %(id)s", 1.0, "postgresql", ("shop/jobs.py", 38))
    h.query("DELETE FROM django_session WHERE expire_date < %s", 2.0, "mysql", None)
    h.now += 0.1
    h.tracer.finish_job(job)
    out.append({
        "name": "job: one statement written with three driver placeholder styles is one query",
        "payload": h.payloads[0],
        "expect": {"route": "job import_orders", "status": 0, "requests": 1, "source": "",
                   "job": {"kind": "job", "name": "import_orders", "runs": 1, "failed": 0},
                   "queries": [
                       q("SELECT name FROM customers WHERE id = %s", 3, "shop/jobs.py:31",
                         "select name from customers where id = ?"),
                       q("DELETE FROM django_session WHERE expire_date < %s", 1, "",
                         "delete from django_session where expire_date < ?"),
                   ]},
    })

    # Cron: nobody is waiting for it, so nobody notices when it doubles.
    h = Harness()
    command = h.tracer.start_command("close_orders")
    h.query("UPDATE orders SET closed_at = %s WHERE closed_at IS NULL", 1200.0, "postgresql",
            ("shop/management/commands/close_orders.py", 52))
    h.now += 0.05
    h.tracer.finish_job(command, failed=True)
    out.append({
        "name": "management command run by cron, and it failed",
        "payload": h.payloads[0],
        "expect": {"route": "command close_orders", "status": 0, "requests": 1, "source": "",
                   "job": {"kind": "command", "name": "close_orders", "runs": 1, "failed": 1},
                   "queries": [
                       q("UPDATE orders SET closed_at = %s WHERE closed_at IS NULL", 1,
                         "shop/management/commands/close_orders.py:52",
                         "update orders set closed_at = ? where closed_at is null"),
                   ]},
    })
    return out


def test_payloads_match_the_shared_fixtures():
    cases = json.loads(json.dumps(scenarios()))
    if os.environ.get("UPDATE_FIXTURES"):
        with open(FILE, "w") as f:
            json.dump(cases, f, indent=4, ensure_ascii=False)
            f.write("\n")
    if not os.path.isfile(FILE):
        pytest.skip("spec/ is only in the Slowpoke repository")
    with open(FILE) as f:
        assert json.load(f) == cases
