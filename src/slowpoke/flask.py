"""Flask integration (2.x and 3.x):

    import slowpoke.flask, slowpoke.sqlalchemy
    slowpoke.flask.init_app(app)
    slowpoke.sqlalchemy.instrument(db.engine)   # or instrument() for every engine
"""

from flask import request

import slowpoke

_KEY = "slowpoke.trace"
_STATUS = "slowpoke.status"


def init_app(app):
    """Idempotent. The trace opens before every other before_request hook, so a hook that answers
    early (authentication, rate limits) is traced too."""
    before = app.before_request_funcs.setdefault(None, [])
    if _before not in before:
        before.insert(0, _before)
    if _after not in app.after_request_funcs.setdefault(None, []):
        app.after_request(_after)
    if _teardown not in app.teardown_request_funcs.setdefault(None, []):
        app.teardown_request(_teardown)
    return app


def _before():
    try:
        tracer = slowpoke.get_tracer()
        if tracer is not None:
            request.environ[_KEY] = (tracer, tracer.start_request(request.method))
    except Exception:
        pass


def _after(response):
    try:
        request.environ[_STATUS] = response.status_code
    except Exception:
        pass
    return response


def _teardown(exc):
    try:
        tracer, trace = request.environ.pop(_KEY, (None, None))
        if trace is None:
            return
        status = request.environ.get(_STATUS) or (500 if exc is not None else 200)
        rule = request.url_rule
        tracer.finish_request(trace, rule.rule if rule is not None else None, request.path, status, request.host)
    except Exception:
        pass
