import contextvars
import json
import math
import os
import re
import sys
import time

VERSION = "0.1.3"

SERVER = 2
CLIENT = 3
CONSUMER = 5

# The trace of the request or job running in this thread or asyncio task. Worker threads and
# sync_to_async/run_in_threadpool copy the context, so their queries land in the same trace object.
_current = contextvars.ContextVar("slowpoke_trace", default=None)

_STATEMENT = re.compile(r"[\s(]*([A-Za-z]+)")
_DB_SYSTEMS = {"postgres": "postgresql", "mssql": "microsoft.sql_server", "sqlserver": "microsoft.sql_server"}


def current():
    return _current.get()


def _random_id(size):
    return os.urandom(size).hex()


class Trace:
    __slots__ = ("kind", "name", "start", "end", "error", "trace_id", "span_id", "attributes", "queries", "dropped", "task")

    def __init__(self, kind, name, start, trace_id, span_id):
        self.kind = kind
        self.name = name
        self.start = start
        self.end = None
        self.error = False
        self.trace_id = trace_id
        self.span_id = span_id
        self.attributes = []
        self.queries = []
        self.dropped = 0
        self.task = _running_task()


class Tracer:
    """Collects one trace per HTTP request or job and turns it into OTLP/JSON for the agent: a SERVER
    (or CONSUMER) span and one CLIENT span per query, with the SQL as the driver received it
    (placeholders, never parameter values) and the application line that ran it.

    Every public method swallows its own errors: observability must never break the app."""

    def __init__(self, origin, submit, service="app", version=VERSION, max_queries=500, max_sql_length=10000,
                 clock=time.time, ids=_random_id):
        self.origin = origin
        self.submit = submit
        self.service = service
        self.version = version
        self.max_queries = max(0, int(max_queries))
        self.max_sql_length = max(1, int(max_sql_length))
        self.clock = clock
        self.ids = ids

    # ------------------------------------------------------------ requests

    def start_request(self, method):
        try:
            method = str(method).upper()
            trace = Trace(SERVER, method, self.clock(), self.ids(16), self.ids(8))
            trace.attributes.append(_kv("http.request.method", method))
            _current.set(trace)
            return trace
        except Exception:
            return None

    def finish_request(self, trace, route, path, status):
        if trace is None or trace.end is not None:
            return
        try:
            trace.end = self.clock()
            if route:
                route = "/" + str(route).lstrip("/")
                trace.name = trace.name + " " + route
                trace.attributes.append(_kv("http.route", route))
            else:
                trace.attributes.append(_kv("url.path", "/" + str(path or "").split("?", 1)[0].lstrip("/")))
            status = int(status)
            trace.attributes.append(_kv("http.response.status_code", status))
            trace.error = status >= 500
        except Exception:
            trace.end = trace.end or self.clock()
        self._done(trace)

    # ------------------------------------------------------------ jobs

    def start_job(self, name, queue=None):
        return self._start_background(name, "job", queue)

    def start_command(self, name):
        """A command run by cron: nobody waits for it, which is why nobody notices when it doubles."""
        return self._start_background(name, "command", None)

    def _start_background(self, name, kind, queue):
        running = _current.get()
        if running is not None and running.end is None:
            return None  # work run inside a request or another job belongs to it
        try:
            trace = Trace(CONSUMER, str(name), self.clock(), self.ids(16), self.ids(8))
            # The agent files it under Jobs by this attribute, instead of among the endpoints.
            trace.attributes.append(_kv("slowpoke.kind", kind))
            if queue:
                trace.attributes.append(_kv("messaging.destination.name", str(queue)))
            _current.set(trace)
            return trace
        except Exception:
            return None

    def finish_job(self, trace, failed=False):
        if trace is None or trace.end is not None:
            return
        trace.end = self.clock()
        trace.error = bool(failed)
        self._done(trace)

    # ------------------------------------------------------------ queries

    def record_query(self, sql, seconds, system):
        """Called right after a statement ran, from the thread that ran it."""
        trace = _current.get()
        if trace is None or trace.end is not None:
            return  # outside requests and jobs, or after the response
        try:
            if len(trace.queries) >= self.max_queries:
                trace.dropped += 1
                return
            end = self.clock()
            sql = sql if isinstance(sql, str) else str(sql)
            trace.queries.append((
                sql[:self.max_sql_length],
                system,
                max(trace.start, end - max(0.0, seconds)),
                end,
                self.origin.find(trace.task),
            ))
        except Exception:
            pass  # never let observability break the query that was just run

    # ------------------------------------------------------------ sending

    def _done(self, trace):
        trace.task = None  # the queued trace must not keep the request's coroutine alive
        try:
            if _current.get() is trace:
                _current.set(None)
        except Exception:
            pass
        try:
            self.submit(trace)
        except Exception:
            pass  # the agent is optional: a missing or broken one costs a trace, nothing else

    def encode(self, t):
        """The OTLP/JSON payload of a finished trace. Runs on the background thread."""
        root = {
            "traceId": t.trace_id,
            "spanId": t.span_id,
            "name": t.name,
            "kind": t.kind,
            "startTimeUnixNano": _nanos(t.start),
            "endTimeUnixNano": _nanos(t.end),
            "attributes": list(t.attributes),
        }
        if t.dropped > 0:
            root["attributes"].append(_kv("slowpoke.dropped_queries", t.dropped))
        if t.error:
            root["status"] = {"code": 2}
        spans = [root]
        for sql, system, start, end, origin in list(t.queries):
            attributes = [_kv("db.system.name", _DB_SYSTEMS.get(system, system)), _kv("db.query.text", sql)]
            if origin is not None:
                attributes.append(_kv("code.file.path", origin[0]))
                if origin[1] is not None:
                    attributes.append(_kv("code.line.number", origin[1]))
            m = _STATEMENT.match(sql)
            spans.append({
                "traceId": t.trace_id,
                "spanId": self.ids(8),
                "parentSpanId": t.span_id,
                "name": m.group(1).upper() if m else "QUERY",
                "kind": CLIENT,
                "startTimeUnixNano": _nanos(start),
                "endTimeUnixNano": _nanos(end),
                "attributes": attributes,
            })
        return {"resourceSpans": [{
            "resource": {"attributes": [
                _kv("service.name", self.service),
                _kv("telemetry.sdk.name", "slowpoke-python"),
                _kv("telemetry.sdk.language", "python"),
                _kv("telemetry.sdk.version", self.version),
            ]},
            "scopeSpans": [{
                "scope": {"name": "slowpoke", "version": self.version},
                "spans": spans,
            }],
        }]}

    def encode_json(self, trace):
        text = json.dumps(self.encode(trace), ensure_ascii=False, separators=(",", ":"))
        return text.encode("utf-8", "replace")


def _running_task():
    asyncio = sys.modules.get("asyncio")
    if asyncio is None:
        return None
    try:
        return asyncio.current_task() if asyncio._get_running_loop() is not None else None
    except Exception:
        return None


def _nanos(seconds):
    # 64-bit integers travel as strings. Microseconds, as the PHP packages, and rounded the same
    # way: Python's round() goes to the even digit (round(0.5) is 0), math.floor(x + 0.5) does not,
    # and the same measure must produce the same trace in every language.
    return "%d000" % math.floor(seconds * 1e6 + 0.5)


def _kv(key, value):
    if isinstance(value, int) and not isinstance(value, bool):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}
