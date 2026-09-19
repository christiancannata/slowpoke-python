# Changelog

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
