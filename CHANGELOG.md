# Changelog

## 0.1.2 - 2026-09-19

- The version the package reports to the agent is the version of the release: 0.1.1 still said
  0.1.0, and on PyPI the release would have carried the wrong number altogether, because the
  version is read from that same constant.
- `license` in pyproject.toml is a plain string, as PEP 639 asks: setuptools warned on every build.

## 0.1.1 - 2026-09-19

Nothing changes in the package itself: the first public run of the test matrix could not install
the oldest supported set at all. The constraints were written straight into the workflow's shell,
where `Werkzeug<3` is a redirection and not a version: they travel through the environment now.

## 0.1.0 - 2026-09-18

First release.

- Every request is one trace: the route template (`/orders/<int:pk>/`, `/orders/{order_id}`), the status,
  and each query with the `file:line` of the application code that ran it. Django, Flask, FastAPI and
  Starlette for requests; Django's ORM and SQLAlchemy 1.4/2 (sync and async) for queries.
- The N+1 that hides in a loop is visible as what it is: the same statement, counted, with the one line
  behind it. Statements written `%s`, `%(name)s` or `?` by different drivers are folded into one query.
- Work nobody waits for is traced too: Celery tasks (`slowpoke.celery.install()`), Django management
  commands, which on a server means cron, and anything you mark with `slowpoke.job` or `slowpoke.command`.
  The commands that never end (`runserver`, workers) are left alone.
- Python 3.9 to 3.13. No runtime dependency: only the standard library.
- Statement text only, never the parameter values. Plain `http` to the agent on the same machine or a
  private network, from a background thread with a bounded queue: a missing or slow agent costs a trace,
  never a request.
