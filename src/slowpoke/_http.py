"""Outbound HTTP calls made with requests and httpx, one span each, inside the trace that is open.

Patched at the highest level each library has - requests' Session.send, httpx's Client.send and
AsyncClient.send - and never urllib3 or httpcore underneath, so one call is one span. requests follows
redirects by calling Session.send again: the calls inside a call already being measured are skipped,
so a redirected call is one span too, named after the host the application asked for. Calls made with
urllib3, aiohttp or http.client directly are not seen; the package's own sender uses http.client, so
it is never traced (and the tracer skips the agent's address anyway).
"""

import contextvars
import functools
import importlib
import importlib.util

import slowpoke

from ._tracer import current

# True while a call is being measured in this thread or task: nested sends belong to it.
_inside = contextvars.ContextVar("slowpoke_http_inside", default=False)


def install():
    """Idempotent. Imports requests and httpx only when they are installed."""
    for module, owner, name, is_async in (
        ("requests", "Session", "send", False),
        ("httpx", "Client", "send", False),
        ("httpx", "AsyncClient", "send", True),
    ):
        try:
            if importlib.util.find_spec(module) is None:
                continue
            cls = getattr(importlib.import_module(module), owner)
            original = cls.__dict__.get(name)
            if original is None or getattr(original, "_slowpoke", False):
                continue
            wrapper = _async_wrap(original) if is_async else _wrap(original)
            wrapper._slowpoke = True
            setattr(cls, name, wrapper)
        except Exception:
            pass  # a library that cannot be patched is a library that is not traced, nothing else


def _begin(request):
    """(tracer, call, token) when this call is to be measured, None otherwise. Cheap when no trace is open."""
    if current() is None or _inside.get():
        return None
    try:
        tracer = slowpoke.get_tracer()
        if tracer is None or not tracer.http_client:
            return None
        call = tracer.start_http_call(request.method, str(request.url))
        if call is None:
            return None
        return tracer, call, _inside.set(True)
    except Exception:
        return None


def _end(measuring, response, failed):
    tracer, call, token = measuring
    try:
        _inside.reset(token)
    except Exception:
        pass
    try:
        tracer.finish_http_call(call, None if failed else getattr(response, "status_code", None))
    except Exception:
        pass


def _wrap(send):
    @functools.wraps(send)
    def traced_send(self, request, *args, **kwargs):
        measuring = _begin(request)
        if measuring is None:
            return send(self, request, *args, **kwargs)
        try:
            response = send(self, request, *args, **kwargs)
        except BaseException:
            _end(measuring, None, True)
            raise
        _end(measuring, response, False)
        return response
    return traced_send


def _async_wrap(send):
    @functools.wraps(send)
    async def traced_send(self, request, *args, **kwargs):
        measuring = _begin(request)
        if measuring is None:
            return await send(self, request, *args, **kwargs)
        try:
            response = await send(self, request, *args, **kwargs)
        except BaseException:
            _end(measuring, None, True)
            raise
        _end(measuring, response, False)
        return response
    return traced_send
