"""Tells Slowpoke which line of your Python app ran each query.

One trace per HTTP request (Django, Flask, FastAPI/Starlette) or job, with the route template, the status
and every query with its file:line, sent as OTLP/JSON to the Slowpoke agent on the same machine.
"""

import functools
import inspect
import os
import threading

from ._config import Config
from ._origin import OriginFinder
from ._sender import BackgroundSender, HttpSender
from ._tracer import VERSION, Tracer, current

__version__ = VERSION
__all__ = ["configure", "get_tracer", "set_defaults", "reset", "flush", "job", "command", "current"]

_lock = threading.Lock()
_state = {"built": False, "tracer": None, "explicit": {}, "defaults": {}}


def configure(**overrides):
    """Builds the tracer now from SLOWPOKE_* variables, with these keyword arguments on top: any Config
    field (enabled, endpoint, timeout, service, max_queries, max_sql_length, backtrace_limit, code_root,
    queue_size), plus `sender` (an object with send(bytes) -> bool) and `background` (False sends inline,
    for tests). Returns None when Slowpoke is disabled."""
    with _lock:
        _state["explicit"] = dict(overrides)
        return _build()


def get_tracer():
    """The tracer the integrations use, built from the environment on first use; None when disabled."""
    if _state["built"]:
        return _state["tracer"]
    with _lock:
        return _state["tracer"] if _state["built"] else _build()


def set_defaults(**defaults):
    """Framework defaults (code_root, service) that apply only where the environment is silent."""
    with _lock:
        merged = dict(_state["defaults"], **{k: v for k, v in defaults.items() if v})
        if merged != _state["defaults"]:
            _state["defaults"] = merged
            _state["built"] = False


def reset():
    with _lock:
        _state.update(built=False, tracer=None, explicit={}, defaults={})


def flush(timeout=1.0):
    """Waits up to `timeout` seconds for queued traces to reach the agent. Never needed in a web app."""
    tracer = get_tracer()
    background = getattr(tracer, "background", None)
    return background.drain(timeout) if background is not None else True


def _build():
    _state["built"] = True
    _state["tracer"] = None
    try:
        explicit = _state["explicit"]
        defaults = _state["defaults"]
        cfg = Config.from_env()
        for key, value in explicit.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        if not cfg.enabled:
            return None
        sender = explicit.get("sender") or HttpSender.from_url(cfg.endpoint, cfg.timeout)
        if sender is None:
            return None  # not a local or private endpoint: nothing may be sent, so nothing is recorded
        code_root = os.path.abspath(str(cfg.code_root or defaults.get("code_root") or os.getcwd()))
        service = cfg.service or defaults.get("service") or os.path.basename(code_root.rstrip(os.sep)) or "app"
        tracer = Tracer(
            origin=OriginFinder(code_root, cfg.backtrace_limit),
            submit=None,
            service=service,
            max_queries=cfg.max_queries,
            max_sql_length=cfg.max_sql_length,
        )
        if explicit.get("background", True):
            tracer.background = BackgroundSender(sender, encode=tracer.encode_json, maxsize=cfg.queue_size)
            tracer.submit = tracer.background.submit
        else:
            tracer.submit = lambda trace: sender.send(tracer.encode_json(trace))
        _state["tracer"] = tracer
        return tracer
    except Exception:
        return None  # a broken configuration disables Slowpoke, it never stops the app from booting


class job:
    """Traces work outside HTTP requests: a script, a cron command, a custom worker loop.

        with slowpoke.job("import_orders", queue="nightly"):
            ...

        @slowpoke.job()
        def send_invoices(): ...

    Inside a request or another job it does nothing: the queries already belong to that trace."""

    def __init__(self, name=None, queue=None):
        self.name = name
        self.queue = queue
        self._tracer = None
        self._trace = None

    def _start(self, tracer):
        return tracer.start_job(self.name or "job", self.queue)

    def __enter__(self):
        try:
            self._tracer = get_tracer()
            if self._tracer is not None:
                self._trace = self._start(self._tracer)
        except Exception:
            self._trace = None
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._trace is not None:
            self._tracer.finish_job(self._trace, failed=exc_type is not None)
            self._trace = None
        return False

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb):
        return self.__exit__(exc_type, exc, tb)

    def __call__(self, fn):
        name = self.name or "%s.%s" % (fn.__module__, fn.__qualname__)
        queue = self.queue
        again = type(self)

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                with again(name, queue):
                    return await fn(*args, **kwargs)
            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with again(name, queue):
                return fn(*args, **kwargs)
        return wrapper


class command(job):
    """Traces a command run by cron, or any script nobody is waiting for.

        with slowpoke.command("import_orders"):
            ...

        @slowpoke.command()
        def import_orders(): ...

    Same trace as a job, filed under its own kind: on the Jobs page a nightly command and a queued
    job are different things. Inside a request or another job it does nothing."""

    def __init__(self, name=None, queue=None):
        super().__init__(name, None)  # a command has no queue: nothing dispatched it

    def _start(self, tracer):
        return tracer.start_command(self.name or "command")
