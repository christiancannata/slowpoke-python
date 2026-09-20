"""ASGI integration for FastAPI and Starlette:

    from slowpoke.asgi import SlowpokeMiddleware
    app.add_middleware(SlowpokeMiddleware)   # add it last: it becomes the outermost middleware

Plain ASGI, not BaseHTTPMiddleware: the endpoint runs in the same task and context as the trace.
"""

import slowpoke


class SlowpokeMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        tracer = slowpoke.get_tracer() if scope.get("type") == "http" else None
        trace = tracer.start_request(scope.get("method", "GET")) if tracer is not None else None
        if trace is None:
            return await self.app(scope, receive, send)

        path = scope.get("path", "/")
        root_path = scope.get("root_path", "")
        status = []

        async def send_with_status(message):
            if message.get("type") == "http.response.start" and not status:
                status.append(message.get("status", 200))
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        except BaseException:
            _finish(tracer, trace, scope, root_path, path, 500)
            raise
        _finish(tracer, trace, scope, root_path, path, status[0] if status else 500)


def _finish(tracer, trace, scope, root_path, path, status):
    try:
        route = route_template(scope, root_path)
    except Exception:
        route = None
    tracer.finish_request(trace, route, path, status, _host(scope))


def _host(scope):
    """The Host header of the request, or the host the server answered on."""
    try:
        for name, value in scope.get("headers") or ():
            if name.lower() == b"host":
                return value.decode("latin-1")
        server = scope.get("server")
        return server[0] if server else None
    except Exception:
        return None


def route_template(scope, initial_root_path=""):
    """The template of the route that handled the request, read after routing: FastAPI leaves the route
    in scope["route"]; plain Starlette only the endpoint, looked up in the app's routes."""
    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str):
        # A mounted sub-application knows its routes without the mount prefix.
        final_root = scope.get("root_path", "") or ""
        prefix = final_root[len(initial_root_path):] if final_root.startswith(initial_root_path) else ""
        if prefix and not template.startswith(prefix + "/"):
            template = prefix.rstrip("/") + template
        return template
    endpoint = scope.get("endpoint")
    app = scope.get("app")
    if endpoint is None or app is None:
        return None
    return _find_endpoint(getattr(app, "routes", None) or [], endpoint, "", 0)


def _find_endpoint(routes, endpoint, prefix, depth):
    if depth > 8:
        return None
    for r in routes:
        if getattr(r, "endpoint", None) is endpoint and isinstance(getattr(r, "path", None), str):
            return prefix + r.path
        children = getattr(r, "routes", None)
        if children and isinstance(getattr(r, "path", None), str):
            found = _find_endpoint(children, endpoint, prefix + r.path, depth + 1)
            if found is not None:
                return found
    return None
