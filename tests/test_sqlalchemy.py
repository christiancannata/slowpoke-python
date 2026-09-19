import asyncio

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")

from apps import sa_shop  # noqa: E402
from conftest import attr, line_of, rel, split  # noqa: E402

import slowpoke  # noqa: E402
from slowpoke.sqlalchemy import instrument  # noqa: E402

SHOP = sa_shop.__file__


@pytest.fixture
def engine():
    engine = sa_shop.make_engine()
    with engine.begin() as conn:
        sa_shop.seed(conn)
    yield engine
    slowpoke.sqlalchemy.uninstrument(engine)
    slowpoke.sqlalchemy.uninstrument()
    engine.dispose()


def origins(queries):
    return [(attr(q, "code.file.path"), int(attr(q, "code.line.number"))) for q in queries]


def test_script_job_with_n_plus_one(tracer, sender, engine):
    instrument(engine)
    with slowpoke.job("nightly_report"):
        assert len(sa_shop.customer_names(engine)) == 6
    root, queries = sender.only_trace()
    assert root["kind"] == 5 and root["name"] == "nightly_report"
    assert origins(queries) == [(rel(SHOP), line_of(SHOP, "sa list"))] + [(rel(SHOP), line_of(SHOP, "sa n+1"))] * 6
    assert rel(SHOP) == "tests/apps/sa_shop.py"
    repeated = {attr(q, "db.query.text") for q in queries[1:]}
    assert len(repeated) == 1
    # the sqlite3 driver takes qmark placeholders
    assert repeated.pop().endswith("WHERE customers.id = ?")
    assert {attr(q, "db.system.name") for q in queries} == {"sqlite"}


def test_orm_session_and_text(tracer, sender, engine):
    instrument(engine)
    with slowpoke.job("total"):
        assert sa_shop.order_total(engine, 3) == 20
    _, queries = sender.only_trace()
    stmts = [attr(q, "db.query.text") for q in queries]
    assert "SELECT total FROM orders WHERE id = ?" in stmts
    (q,) = [q for q in queries if attr(q, "db.query.text") == "SELECT total FROM orders WHERE id = ?"]
    assert int(attr(q, "code.line.number")) == line_of(SHOP, "sa orm")


def test_parameter_values_never_leave_the_app(tracer, sender, engine):
    instrument(engine)
    with slowpoke.job("lookup"):
        assert sa_shop.find_by_email(engine, "c2@secret.example") == "Customer 2"
    assert b"secret" not in sender.payloads[0] and b"Customer 2" not in sender.payloads[0]


def test_failed_statements_are_recorded_too(tracer, sender, engine):
    instrument(engine)
    with pytest.raises(sqlalchemy.exc.OperationalError):
        with slowpoke.job("broken"):
            sa_shop.broken(engine)
    root, (q,) = sender.only_trace()
    assert root["status"] == {"code": 2}
    assert attr(q, "db.query.text") == "SELECT nope FROM missing_table"
    assert int(attr(q, "code.line.number")) == line_of(SHOP, "sa broken")


def test_nothing_recorded_outside_requests_and_jobs(tracer, sender, engine):
    instrument(engine)
    sa_shop.customer_names(engine)
    assert sender.payloads == []


def test_global_instrumentation_covers_engines_created_later_and_never_counts_twice(tracer, sender):
    instrument()
    engine = sa_shop.make_engine()
    instrument(engine)
    instrument(engine)
    try:
        with engine.begin() as conn:
            sa_shop.seed(conn)
        with slowpoke.job("twice"):
            sa_shop.customer_names(engine)
        _, queries = sender.only_trace()
        assert len(queries) == 7
    finally:
        slowpoke.sqlalchemy.uninstrument(engine)
        engine.dispose()


def test_async_engine_origin_is_the_awaiting_line(tracer, sender):
    pytest.importorskip("aiosqlite")
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    async def main():
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        instrument(engine)
        async with engine.begin() as conn:
            await conn.run_sync(sa_shop.seed)
        async with slowpoke.job("async_report"):
            names = await sa_shop.async_customer_names(engine)
        slowpoke.sqlalchemy.uninstrument(engine)
        await engine.dispose()
        return names

    assert len(asyncio.run(main())) == 6
    _, queries = sender.only_trace()
    assert origins(queries) == [(rel(SHOP), line_of(SHOP, "sa async list"))] + [
        (rel(SHOP), line_of(SHOP, "sa async n+1"))] * 6


def test_concurrent_async_jobs_keep_their_own_queries(tracer, sender):
    pytest.importorskip("aiosqlite")
    from sqlalchemy.ext.asyncio import create_async_engine

    async def main(tmp):
        engine = create_async_engine("sqlite+aiosqlite:///" + tmp)
        instrument(engine)
        async with engine.begin() as conn:
            await conn.run_sync(sa_shop.seed)

        async def one(name):
            async with slowpoke.job(name):
                await sa_shop.async_customer_names(engine)

        await asyncio.gather(one("a"), one("b"), one("c"))
        slowpoke.sqlalchemy.uninstrument(engine)
        await engine.dispose()

    import os
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        asyncio.run(main(os.path.join(d, "shop.db")))
    assert sorted(len(split(p)[1]) for p in sender.decoded()) == [7, 7, 7]


def test_async_origin_from_the_greenlet_alone(tracer, sender, monkeypatch):
    # an AsyncEngine used outside the task that opened the trace still finds the awaiting line
    import slowpoke._origin as origin

    monkeypatch.setattr(origin.OriginFinder, "_from_task", lambda self, task, budget: None)
    test_async_engine_origin_is_the_awaiting_line(tracer, sender)


def test_a_running_task_never_gives_a_misleading_origin(tracer, sender, monkeypatch):
    # Without the greenlet the statement runs while its task is running: the task cannot tell the
    # line, and the outer coroutine in this file must not be reported instead.
    import slowpoke._origin as origin

    monkeypatch.setattr(origin, "_greenlet_parent_frame", lambda: None)
    with pytest.raises(TypeError):  # int(None): no line number
        test_async_engine_origin_is_the_awaiting_line(tracer, sender)
    _, queries = sender.only_trace()
    assert [attr(q, "code.file.path") for q in queries] == [None] * 7
