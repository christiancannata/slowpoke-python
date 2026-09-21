<h1 align="center">slowpoke (Python)</h1>

<p align="center"><b>Which line of your code is slow. Not which query — which line.</b></p>

<p align="center">
<a href="https://github.com/christiancannata/slowpoke-python/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/christiancannata/slowpoke-python/actions/workflows/tests.yml/badge.svg"></a>
<a href="https://pypi.org/project/slowpoke-python/"><img alt="PyPI" src="https://img.shields.io/pypi/v/slowpoke-python"></a>
<a href="https://pypi.org/project/slowpoke-python/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/slowpoke-python"></a>
<a href="https://github.com/christiancannata/slowpoke-python/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/christiancannata/slowpoke-python/actions/workflows/codeql.yml/badge.svg"></a>
<a href="#performance"><img alt="runtime dependencies: 0" src="https://img.shields.io/badge/runtime%20dependencies-0-brightgreen"></a>
<a href="https://scorecard.dev/viewer/?uri=github.com/christiancannata/slowpoke-python"><img alt="OpenSSF Scorecard" src="https://api.scorecard.dev/projects/github.com/christiancannata/slowpoke-python/badge"></a>
<a href="LICENSE"><img alt="MIT" src="https://img.shields.io/pypi/l/slowpoke-python"></a>
</p>

---

A slow query tells you *what* is slow. It never tells you **where**, and a tool that points at
`site-packages/django/db/models/sql/compiler.py:1398` has told you nothing at all.

This package sends [Slowpoke](https://github.com/christiancannata/slowpoke) the file and line of **your**
code behind every query — for every request, every Celery task and every command cron runs:

```
GET /orders/<int:pk>/                               820 ms · 34 queries
  SELECT * FROM orders WHERE id = %s                  4 ms   shop/views.py:42
  SELECT * FROM customers WHERE id = %s               3 ms   shop/models.py:88   ← ×31, one per order
  SELECT SUM(total) FROM invoices WHERE order_id=%s   9 ms   shop/serializers.py:17
```

That last column is the whole point. Slowpoke turns it into N+1 detection and missions that name a file,
each with a price in seconds of waiting per day — so the argument about what to fix first is over.

**One query, however the driver writes it.** psycopg2 sends `%(id)s`, MySQLdb sends `%s`, SQLite sends `?`:
the same statement is one query in Slowpoke, not three.

**Cron and workers too.** A Celery task and a management command are not endpoints, and this package does
not pretend they are: they go to the Jobs page with how long they took, how often they failed and the same
`file:line` for their queries. Nobody is waiting for them, which is exactly why nobody notices when they get
slower.

## Install

```sh
pip install slowpoke-python
```

The name on PyPI carries the language, like the other packages of this project; what you import is
`slowpoke`. (Plain `slowpoke` on PyPI is a different thing, published once in 2012 and never again.)

No SDK, no extension, no key to carry, no account anywhere. The package talks to the Slowpoke agent on the
same machine, which needs one line in `/etc/slowpoke/agent.yaml`:

```yaml
sources:
  - type: otlp          # the agent listens on 127.0.0.1:4318
```

### Django

```python
INSTALLED_APPS = [..., "slowpoke.django"]
MIDDLEWARE = ["slowpoke.django.SlowpokeMiddleware", ...]   # first, so the timing covers the others
```

Every database connection is hooked with `connection.execute_wrapper`, including the ones opened later in
other threads. Sync and async views both work. **Management commands are traced as well** — that is cron —
except the ones that never end (`runserver`, `runworker`, `rqworker`, `qcluster`…); `SLOWPOKE_COMMANDS=false`
turns them off. File paths are relative to `BASE_DIR`.

### SQLAlchemy (with Flask, FastAPI, or a script)

```python
import slowpoke.sqlalchemy

slowpoke.sqlalchemy.instrument(engine)    # an Engine or an AsyncEngine
slowpoke.sqlalchemy.instrument()          # or every Engine, including the ones created later
```

### Flask · FastAPI / Starlette

```python
import slowpoke.flask
slowpoke.flask.init_app(app)
```

```python
from slowpoke.asgi import SlowpokeMiddleware
app.add_middleware(SlowpokeMiddleware)   # add it last: it becomes the outermost middleware
```

### Celery

```python
import slowpoke.celery

slowpoke.celery.install()      # wherever the Celery app is created
```

One trace per task the worker runs, with its queue and whether it failed. A task dispatched from a request
is traced where it runs, not where it was sent from.

### Scripts and anything else

```python
import slowpoke

@slowpoke.command("import_orders")       # cron, a script: nobody is waiting
def import_orders(): ...

with slowpoke.job("rebuild_search_index", queue="nightly"):   # your own worker loop
    ...
```

A short script can call `slowpoke.flush()` before exiting; otherwise the last traces get at most half a
second at interpreter exit.

## Performance

The rule this package is built on is the one the whole project follows: **never make the application
slower**. Measured, not claimed, and you can run it yourself with `./bin/bench` — everything the package
does *while a request is running*: recording each query, finding the line behind it, building the trace.

| | Python 3.9 | Python 3.13 |
|---|---|---|
| per query | 4.6 µs | 3.5 µs |
| **a request with 50 queries** | **0.23 ms** | **0.17 ms** |

For scale: a request that spends 800 ms in your code and your database pays about **two ten-thousandths** of
that to be measured. Everything else happens off the request:

| | |
|---|---|
| **Sent from a background thread** | the request only appends to a list and drops the finished trace into a bounded queue with `put_nowait`. Encoding and the HTTP call happen on one daemon thread |
| **Never waits** | a hard time budget (`SLOWPOKE_TIMEOUT`, 0.1 s) and every error swallowed: an agent that is missing, slow or broken costs one trace, never a request. A full queue drops the trace |
| **Never copies your data** | the stack is walked with `sys._getframe` to a bounded depth: no argument, no local variable, ever |
| **Bounded** | 500 queries and 200 outbound calls described per request at most, the rest counted; statements over 10 000 characters cut; a bounded per-file cache |
| **Quiet when idle** | queries outside a request, a task or a command — a worker polling its broker, a shell you opened — cost one context variable lookup and are not recorded |
| **Async-safe** | the trace lives in `contextvars`: it follows `sync_to_async`, `run_in_threadpool` and asyncio tasks, and a statement run in a worker thread is attributed to the line that awaited it |

About 1000 lines of Python. **No runtime dependency at all**: only the standard library. A test in the suite fails the day that stops being true,
and another one fails if anything but source and documentation ends up in a published copy.

## What is sent, and what never is

Sent only to the agent on your machine or private network:

- **per request** — method, route template (`/orders/<int:pk>/`, `/orders/{order_id}`), status code, start
  and end time. When no route matched, the path without its query string;
- **per task** — the task name, the queue it came from, whether it failed;
- **per command** — the command name (`close_orders`), how long it took, whether it raised;
- **per query** — the SQL **with placeholders** exactly as the driver receives it, the database engine, the
  real duration, and the first application file and line on the stack, outside `site-packages/`,
  `dist-packages/`, the standard library and this package;
- **per outbound HTTP call** made with `requests` or `httpx` (sync and async) — the method, the remote
  **host** (and its port when it is not 80/443), the response status, how long the call took, whether it
  failed (an exception or a 5xx), and the line of your code that made it. Never the URL path, the query
  string, headers or bodies: they carry tokens and personal data. The package patches `Session.send`,
  `Client.send` and `AsyncClient.send` only when those libraries are installed, and never `urllib3` or
  `httpcore` underneath, so one call is one span (a redirect followed by `requests` included). Calls
  made with `urllib3`, `aiohttp` or `http.client` directly are not seen; the package's own delivery to
  the agent is never traced.

**Never sent** — parameter values, request parameters, headers, cookies, session, the user, exception
messages. If you write literal values into raw SQL yourself, they are part of the statement, and the agent
redacts them before anything leaves the machine.

## Configuration

Everything has a default that works. Nothing has to be set.

| Variable | Default | |
|---|---|---|
| `SLOWPOKE_ENABLED` | `true` | `false` turns everything off: nothing recorded, nothing sent |
| `SLOWPOKE_OTLP_ENDPOINT` | `http://127.0.0.1:4318/v1/traces` | plain http to a local or private host only (private IPs, `localhost`, a Docker service name, `.local`/`.internal`); anything else disables the package |
| `SLOWPOKE_TIMEOUT` | `0.1` | seconds the background thread gives the agent, connect and write together |
| `SLOWPOKE_SERVICE` | the code root folder's name | the name of this application in Slowpoke |
| `SLOWPOKE_COMMANDS` | `true` | trace Django management commands |
| `SLOWPOKE_MAX_QUERIES` | `500` | queries described per request, task or command; the rest are counted |
| `SLOWPOKE_HTTP_CLIENT` | `true` | record outbound calls made with `requests` and `httpx`; `false` does not even patch them |
| `SLOWPOKE_MAX_HTTP_CALLS` | `200` | outbound calls described per request, task or command; the rest are counted |
| `SLOWPOKE_MAX_SQL_LENGTH` | `10000` | longer statements are cut |
| `SLOWPOKE_BACKTRACE_LIMIT` | `100` | stack frames inspected to find your line |
| `SLOWPOKE_CODE_ROOT` | `BASE_DIR` in Django, else the working directory | file paths are sent relative to it |
| `SLOWPOKE_QUEUE_SIZE` | `256` | traces waiting for the agent; past that they are dropped |

The same settings can be passed in code: `slowpoke.configure(service="shop", code_root="/srv/app")`.

## Compatibility

| Python | Django | SQLAlchemy | Flask | FastAPI / Starlette | Celery |
|---|---|---|---|---|---|
| 3.9 – 3.13 | 4.2, 5.2, 6 | 1.4, 2.x | 2.x, 3.x | current | 5.x |

The oldest combination (Python 3.9 with Django 4.2, SQLAlchemy 1.4, Flask 2.3, FastAPI 0.100) and the newest
both run the full suite on every push and every week.

## Quality

| | |
|---|---|
| **96 tests** | unit tests and integration tests on real Django, Flask, FastAPI and Celery applications, on every combination above |
| **Same wire, both sides** | `spec/python_otlp_fixtures.json` in the Slowpoke repository holds payloads exactly as this package sends them, with what the agent must read from each — including the fingerprint that folds `%s`, `%(name)s` and `?` into one query. The agent's Go tests replay that file: a change here the agent cannot read fails there |
| **ruff** | in CI, on every push |
| **CodeQL** and **OpenSSF Scorecard** | on the code and on the workflows, which are pinned by commit |
| **Signed provenance** | every release archive carries a Sigstore attestation |

```sh
./bin/test 3.13                                  # newest Django, SQLAlchemy, Flask, FastAPI
./bin/test 3.9 'Django~=4.2.0' 'SQLAlchemy~=1.4.0' 'Flask~=2.3.0' 'Werkzeug<3' 'fastapi~=0.100.0' 'httpx<0.28'
./bin/test 3.13 -k origin tests/test_django.py   # arguments starting with "-" or containing "/" go to pytest
./bin/test-matrix                                # every supported combination, one after the other
./bin/bench                                      # the numbers in Performance, on your machine
```

Everything runs in Docker. Nothing is installed on your machine.

## Security

It reads no request data, sends nothing outside your machine or private network, and cannot break or slow a
request. What it does and never does, how to report a vulnerability and how to verify a release are in
[SECURITY.md](SECURITY.md).

Do not run it together with OpenTelemetry auto-instrumentation (`opentelemetry-instrument`,
`DjangoInstrumentor`, `SQLAlchemyInstrumentor`) pointed at the same agent: every request and query would
arrive twice. This package replaces it, and gives the origin it cannot.

## License

MIT, see [LICENSE](LICENSE). Slowpoke itself is free and self-hosted: the measures stay on your machines,
and nothing about your application ever leaves them.
