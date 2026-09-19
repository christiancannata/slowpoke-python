import pytest

celery = pytest.importorskip("celery")

from conftest import attr, split  # noqa: E402

import slowpoke  # noqa: E402
import slowpoke.celery  # noqa: E402


@pytest.fixture
def app(tracer):
    slowpoke.celery._state["installed"] = False
    slowpoke.celery._open.clear()
    application = celery.Celery("shop", broker="memory://", backend="cache+memory://")
    application.conf.task_always_eager = True
    slowpoke.celery.install()
    return application


def test_a_task_is_a_trace_of_its_own(app, tracer, sender):
    @app.task(name="app.tasks.send_invoices")
    def send_invoices():
        tracer.record_query("select * from invoices where sent_at is null", 0.25, "postgresql")
        return 7

    assert send_invoices.delay().get() == 7

    root, (query,) = sender.only_trace()
    assert root["kind"] == 5 and root["name"] == "app.tasks.send_invoices"
    assert attr(root, "slowpoke.kind") == "job"
    assert root.get("status") is None
    assert attr(query, "db.query.text") == "select * from invoices where sent_at is null"


def test_a_task_that_raises_is_a_failed_run(app, tracer, sender):
    @app.task(name="app.tasks.broken")
    def broken():
        raise ValueError("no mail server")

    with pytest.raises(ValueError):
        broken.delay().get()

    root, _ = sender.only_trace()
    assert root["status"] == {"code": 2}
    payload = sender.payloads[0]
    text = payload.decode() if isinstance(payload, bytes) else str(payload)
    assert "mail server" not in text, "the exception message must never leave the machine"


def test_each_task_is_sent_as_soon_as_it_is_done(app, tracer, sender):
    @app.task(name="app.tasks.tick")
    def tick():
        tracer.record_query("select 1", 0.001, "sqlite")

    tick.delay()
    assert len(sender.payloads) == 1
    tick.delay()
    assert len(sender.payloads) == 2
    names = [split(p)[0]["name"] for p in sender.decoded()]
    assert names == ["app.tasks.tick", "app.tasks.tick"]


def test_install_is_idempotent_and_never_raises(app):
    assert slowpoke.celery.install() is True
    assert slowpoke.celery.install() is True
