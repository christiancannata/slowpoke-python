"""SQLAlchemy integration (1.4 and 2.x):

    import slowpoke.sqlalchemy
    slowpoke.sqlalchemy.instrument(engine)   # one Engine or AsyncEngine
    slowpoke.sqlalchemy.instrument()         # every Engine, including the ones created later

Statements are recorded inside a request (Flask, FastAPI, Starlette, Django middleware) or a
slowpoke.job(); elsewhere the listeners cost one context variable lookup.
"""

import time

from sqlalchemy import event
from sqlalchemy.engine import Engine

import slowpoke
from slowpoke._tracer import current

_STARTED = "_slowpoke_started"
_EVENTS = (("before_cursor_execute", "_before"), ("after_cursor_execute", "_after"), ("handle_error", "_error"))


def _target(engine):
    if engine is None:
        return Engine
    return getattr(engine, "sync_engine", engine)  # AsyncEngine wraps a regular Engine


def instrument(engine=None):
    """Idempotent. Listening on one engine and on every engine at once still records a statement once."""
    target = _target(engine)
    for name, fn in _EVENTS:
        handler = globals()[fn]
        if not event.contains(target, name, handler):
            event.listen(target, name, handler)


def uninstrument(engine=None):
    target = _target(engine)
    for name, fn in _EVENTS:
        handler = globals()[fn]
        if event.contains(target, name, handler):
            event.remove(target, name, handler)


def _before(conn, cursor, statement, parameters, context, executemany):
    if context is not None and current() is not None:
        setattr(context, _STARTED, time.perf_counter())


def _after(conn, cursor, statement, parameters, context, executemany):
    _record(context, statement)


def _error(exception_context):
    _record(exception_context.execution_context, exception_context.statement)


def _record(context, statement):
    started = getattr(context, _STARTED, None) if context is not None else None
    if started is None:
        return
    try:
        delattr(context, _STARTED)  # a second listener (engine and Engine class) finds nothing
        seconds = time.perf_counter() - started
        tracer = slowpoke.get_tracer()
        if tracer is not None and statement:
            # the statement as the driver receives it, never its parameters
            tracer.record_query(statement, seconds, context.dialect.name)
    except Exception:
        pass
