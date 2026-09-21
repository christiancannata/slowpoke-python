import contextvars
import json
import math
import os
import re
import sys
import time
from urllib.parse import urlsplit

VERSION = "0.1.8"

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
    __slots__ = ("kind", "name", "start", "end", "error", "trace_id", "span_id", "attributes", "queries", "dropped", "task",
                 "http", "dropped_http")

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
        self.http = []
        self.dropped_http = 0


class Tracer:
    """Collects one trace per HTTP request or job and turns it into OTLP/JSON for the agent: a SERVER
    (or CONSUMER) span and one CLIENT span per query, with the SQL as the driver received it
    (placeholders, never parameter values) and the application line that ran it, plus one CLIENT span
    per outbound HTTP call, which names the remote host and nothing else of the URL.

    Every public method swallows its own errors: observability must never break the app."""

    def __init__(self, origin, submit, service="app", version=VERSION, max_queries=500, max_sql_length=10000,
                 clock=time.time, ids=_random_id, http_client=True, max_http_calls=200, agent_endpoint=None):
        self.origin = origin
        self.submit = submit
        self.service = service
        self.version = version
        self.max_queries = max(0, int(max_queries))
        self.max_sql_length = max(1, int(max_sql_length))
        self.clock = clock
        self.ids = ids
        self.http_client = bool(http_client)
        self.max_http_calls = max(0, int(max_http_calls))
        target = _target(agent_endpoint) if agent_endpoint else None
        self._agent = target[:2] if target else None

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

    def finish_request(self, trace, route, path, status, host=None):
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
            # The host this request was answered for. With a web server in front on another
            # machine, it is the only thing that says its access log and this trace are the same
            # requests, so that nobody counts them twice.
            if host:
                trace.attributes.append(_kv("server.address", _hostname(host)))
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

    # ------------------------------------------------------------ outbound HTTP calls

    def start_http_call(self, method, url):
        """An outbound call is leaving, from the thread or task that makes it. Returns the handle to give
        finish_http_call, or None when there is nothing to record. Only the host of the URL is kept."""
        trace = _current.get()
        if trace is None or trace.end is not None:
            return None  # same rule as queries: outside requests and jobs nothing is recorded
        try:
            target = _target(url)
            if target is None or target[:2] == self._agent:
                return None
            if len(trace.http) >= self.max_http_calls:
                trace.dropped_http += 1
                return None
            # method, host, port (None when the scheme's default), start, end, status, error, origin, trace
            call = [str(method or "GET").upper(), target[0], target[2], self.clock(), None, None, False,
                    self.origin.find(trace.task), trace]
            trace.http.append(call)
            return call
        except Exception:
            return None

    def finish_http_call(self, call, status):
        """The call ended with a response (`status`) or failed (None). A 5xx is a failure too."""
        if call is None or call[4] is not None or call[8].end is not None:
            return  # a call still open when its trace ended was closed there, as an error
        try:
            status = int(status) if status else None
            call[5] = status
            call[6] = status is None or status >= 500
            call[4] = self.clock()
        except Exception:
            pass

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
        if t.dropped_http > 0:
            root["attributes"].append(_kv("slowpoke.dropped_http_calls", t.dropped_http))
        if t.error:
            root["status"] = {"code": 2}
        spans = [root]
        for method, host, port, start, end, status, error, origin, _ in list(t.http):
            attributes = [_kv("http.request.method", method), _kv("server.address", host)]
            if port is not None:
                attributes.append(_kv("server.port", port))
            if status is not None:
                attributes.append(_kv("http.response.status_code", status))
            _origin(attributes, origin)
            span = {
                "traceId": t.trace_id,
                "spanId": self.ids(8),
                "parentSpanId": t.span_id,
                "name": method + " " + host,
                "kind": CLIENT,
                "startTimeUnixNano": _nanos(start),
                "endTimeUnixNano": _nanos(end if end is not None else t.end),
                "attributes": attributes,
            }
            if error or end is None:
                span["status"] = {"code": 2}
            spans.append(span)
        for sql, system, start, end, origin in list(t.queries):
            attributes = [_kv("db.system.name", _DB_SYSTEMS.get(system, system)), _kv("db.query.text", sql)]
            _origin(attributes, origin)
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


def _hostname(host):
    """The host without the port, lowercased: Shop.Example.com:8443 is shop.example.com, and the
    first of a comma-separated list is the one the request was addressed to."""
    host = str(host).split(",")[0].strip().lower()
    i = host.rfind(":")
    if i > 0 and "]" not in host[i:]:
        host = host[:i]
    return host


def _origin(attributes, origin):
    if origin is not None:
        attributes.append(_kv("code.file.path", origin[0]))
        if origin[1] is not None:
            attributes.append(_kv("code.line.number", origin[1]))


def _target(url):
    """(host, port, port or None when it is the scheme's default) of a URL, or None. The path, the query
    string and any credentials in it are never looked at again."""
    parts = urlsplit(str(url))
    host = parts.hostname
    if not host:
        return None
    default = 443 if parts.scheme.lower() in ("https", "wss") else 80
    port = parts.port or default
    return host.lower(), port, None if port == default else port


def _nanos(seconds):
    # 64-bit integers travel as strings. Microseconds, as the PHP packages, and rounded the same
    # way: Python's round() goes to the even digit (round(0.5) is 0), math.floor(x + 0.5) does not,
    # and the same measure must produce the same trace in every language.
    return "%d000" % math.floor(seconds * 1e6 + 0.5)


def _kv(key, value):
    if isinstance(value, int) and not isinstance(value, bool):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}
