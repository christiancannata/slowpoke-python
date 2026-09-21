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


def test_a_map_over_a_task_is_named_after_that_task(app, tracer, sender):
    # task.map(), .starmap() and .chunks() run as the built-ins celery.map / celery.starmap /
    # celery.chunks, which call the real task in-process for every item: the work is that task's.
    @app.task(name="app.tasks.resize")
    def resize(path, width=100):
        tracer.record_query("select 1", 0.001, "sqlite")
        return width

    resize.starmap([("mario.rossi.jpg", 100), ("b.jpg", 200)]).delay().get()
    resize.chunks([("mario.rossi.jpg", 100), ("b.jpg", 200)], 1).delay()
    resize.map(["mario.rossi.jpg"]).delay()

    # chunks() is sent as a group of starmaps, one per chunk.
    names = [split(p)[0]["name"] for p in sender.decoded()]
    assert names == ["app.tasks.resize (starmap)"] * 3 + ["app.tasks.resize (map)"]
    assert all("mario.rossi" not in (p.decode() if isinstance(p, bytes) else str(p)) for p in sender.payloads)


def test_housekeeping_built_ins_keep_their_own_name(app, tracer, sender):
    # celery.backend_cleanup, celery.chord_unlock and celery.accumulate are Celery's own work, with a
    # clear name: reported as they are, never hidden.
    app.tasks["celery.backend_cleanup"].delay()
    assert split(sender.decoded()[0])[0]["name"] == "celery.backend_cleanup"


def test_task_name_helper_falls_back_on_anything_odd():
    from slowpoke.celery import _name

    class Task:
        name = "celery.starmap"

    assert _name(Task(), None, None) == "celery.starmap"
    assert _name(Task(), ({"task": "has spaces in it"}, []), {}) == "celery.starmap"
    assert _name(Task(), ({"task": "app.tasks.add"}, []), {}) == "app.tasks.add (starmap)"
    assert _name(Task(), (), {"task": {"task": "app.tasks.add"}, "it": []}) == "app.tasks.add (starmap)"
    assert _name(object(), None, None) == "task"
