"""Django integration: add "slowpoke.django" to INSTALLED_APPS and
"slowpoke.django.SlowpokeMiddleware" at the top of MIDDLEWARE."""

import functools
import os
import time

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.db import connections
from django.db.backends.signals import connection_created

import slowpoke
from slowpoke._tracer import current

_state = {"signal": False, "commands": False}

# Commands that never end: traced as one run they would hold a trace open for as long as the
# process lives, and the requests or tasks inside them would be lost. Names from Django itself and
# from the workers people usually run with manage.py.
LONG_RUNNING = (
    "runserver", "runserver_plus", "testserver", "runworker", "shell", "dbshell", "test",
    "qcluster", "rqworker", "rqscheduler", "process_tasks", "run_huey", "celery", "mail_queue",
)


def install():
    """Hooks every database connection, the ones already open and the ones opened later in other threads
    or async contexts. Idempotent: a statement is never recorded twice."""
    try:
        from django.conf import settings

        base = getattr(settings, "BASE_DIR", None)
        slowpoke.set_defaults(code_root=str(base) if base else None)
        if not _state["signal"]:
            connection_created.connect(_on_connection_created, dispatch_uid="slowpoke")
            _state["signal"] = True
        _wrap_open_connections()
        _trace_commands()
    except Exception:
        pass


def _trace_commands():
    """One trace per management command, which on a server means cron. SLOWPOKE_COMMANDS=false
    turns it off; the commands that never end are left alone whatever it says."""
    if _state["commands"] or os.environ.get("SLOWPOKE_COMMANDS", "").strip().lower() in ("0", "false", "no", "off"):
        return
    try:
        from django.core.management.base import BaseCommand
    except Exception:
        return
    if getattr(BaseCommand.execute, "_slowpoke", False):
        _state["commands"] = True
        return
    original = BaseCommand.execute

    @functools.wraps(original)
    def execute(self, *args, **options):
        name = command_name(self)
        if name in LONG_RUNNING:
            return original(self, *args, **options)
        with slowpoke.command(name):
            return original(self, *args, **options)

    execute._slowpoke = True
    BaseCommand.execute = execute
    _state["commands"] = True


def command_name(cmd):
    """The name a person would type: "close_orders", from myapp.management.commands.close_orders."""
    module = getattr(type(cmd), "__module__", "") or ""
    return module.rsplit(".", 1)[-1] or type(cmd).__name__


def _wrap_open_connections():
    try:
        for conn in connections.all(initialized_only=True):
            _wrap(conn)
    except TypeError:  # before Django 4.1
        for conn in connections.all():
            _wrap(conn)


def _on_connection_created(sender, connection, **kwargs):
    _wrap(connection)


def _wrap(conn):
    wrappers = conn.execute_wrappers
    if _execute_wrapper not in wrappers:
        # First, not last: connection.execute_wrapper() pops the last one when its block ends.
        wrappers.insert(0, _execute_wrapper)


def _execute_wrapper(execute, sql, params, many, context):
    if current() is None:
        return execute(sql, params, many, context)
    started = time.perf_counter()
    try:
        return execute(sql, params, many, context)
    finally:
        seconds = time.perf_counter() - started
        tracer = slowpoke.get_tracer()
        if tracer is not None:
            try:
                vendor = context["connection"].vendor
            except Exception:
                vendor = "unknown"
            # sql only, never params: Django passes %s placeholders to the driver
            tracer.record_query(sql, seconds, vendor)


class SlowpokeMiddleware:
    """Opens one trace per request. Put it first in MIDDLEWARE so the timing covers the others."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.is_async = iscoroutinefunction(get_response)
        if self.is_async:
            markcoroutinefunction(self)
        install()

    def __call__(self, request):
        if self.is_async:
            return self.__acall__(request)
        tracer, trace = _start(request)
        try:
            response = self.get_response(request)
        except BaseException:
            _finish(tracer, trace, request, 500)
            raise
        _finish(tracer, trace, request, getattr(response, "status_code", 200))
        return response

    async def __acall__(self, request):
        tracer, trace = _start(request)
        try:
            response = await self.get_response(request)
        except BaseException:
            _finish(tracer, trace, request, 500)
            raise
        _finish(tracer, trace, request, getattr(response, "status_code", 200))
        return response


def _start(request):
    tracer = slowpoke.get_tracer()
    if tracer is None:
        return None, None
    try:
        _wrap_open_connections()
    except Exception:
        pass
    return tracer, tracer.start_request(request.method)


def _finish(tracer, trace, request, status):
    if trace is None:
        return
    try:
        match = getattr(request, "resolver_match", None)
        route = route_template(match.route) if match is not None and getattr(match, "route", None) else None
        path = request.path
    except Exception:
        route, path = None, "/"
    tracer.finish_request(trace, route, path, status)


def route_template(route):
    """path() routes already read like templates ("orders/<int:pk>/", the agent normalizes them);
    re_path() gives a regular expression, turned here into "legacy/{slug}/"."""
    if not route or ("(" not in route and not route.startswith("^") and not route.endswith("$")):
        return route or ""
    if route.startswith("^"):
        route = route[1:]
    for end in ("\\Z", "$"):
        if route.endswith(end) and not route.endswith("\\" + end):
            route = route[: -len(end)]
            break
    out = []
    i, n = 0, len(route)
    while i < n:
        c = route[i]
        if c == "\\" and i + 1 < n:
            out.append(route[i + 1])
            i += 2
        elif c == "(":
            name = "param"
            if route.startswith("(?P<", i):
                close = route.find(">", i)
                if close > 0:
                    name = route[i + 4:close]
            i = _group_end(route, i)
            while i < n and route[i] in "?*+":
                i += 1
            out.append("{%s}" % name)
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _group_end(pattern, i):
    """Index after the group that opens at pattern[i], skipping escapes and character classes."""
    depth, in_class, n = 0, False, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n
