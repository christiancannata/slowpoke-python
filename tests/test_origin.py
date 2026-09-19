import importlib.util
import os
import sysconfig

from conftest import ROOT, line_of

from slowpoke._origin import OriginFinder

STDLIB = sysconfig.get_paths()["stdlib"]
PACKAGE = os.path.dirname(os.path.abspath(importlib.import_module("slowpoke").__file__))


def finder(**kw):
    kw.setdefault("code_root", "/srv/app")
    return OriginFinder(**kw)


def test_application_file_is_relative_to_the_code_root():
    assert finder().classify("/srv/app/shop/views.py") == "shop/views.py"
    assert finder(code_root="/srv/app/").classify("/srv/app/shop/views.py") == "shop/views.py"


def test_file_outside_the_root_keeps_its_absolute_path():
    assert finder().classify("/opt/other/job.py") == "/opt/other/job.py"


def test_third_party_code_is_never_an_origin():
    f = finder()
    for path in [
        "/srv/app/.venv/lib/python3.12/site-packages/django/db/models/query.py",
        "/usr/lib/python3/dist-packages/sqlalchemy/engine/base.py",
        "/srv/app/node_modules/x/y.py",
        "/srv/app/vendor/lib.py",
        os.path.join(STDLIB, "contextlib.py"),
        os.path.join(PACKAGE, "django.py"),
        "<frozen importlib._bootstrap>",
        "<string>",
        "",
    ]:
        assert f.classify(path) is None, path


def test_a_project_inside_a_folder_named_like_the_stdlib_is_still_application_code():
    assert finder(code_root=STDLIB + "-app").classify(STDLIB + "-app/views.py") == "views.py"


def fake_library(tmp_path):
    """A module living in a site-packages folder, like an ORM calling back into the finder."""
    lib = tmp_path / "site-packages" / "fakeorm.py"
    lib.parent.mkdir()
    lib.write_text(
        "def run(cb):\n    return cb()\n\n"
        "def deep(finder, n):\n    return finder.find() if n == 0 else deep(finder, n - 1)\n\n"
        "def find_for(finder, task):\n    return finder.find(task)\n"
    )
    spec = importlib.util.spec_from_file_location("fakeorm", str(lib))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_first_application_frame_from_inside_a_library(tmp_path):
    fakeorm = fake_library(tmp_path)
    f = OriginFinder(code_root=ROOT)
    origin = fakeorm.run(lambda: f.find())  # origin: lambda
    assert origin == ("tests/test_origin.py", line_of(__file__, "lambda"))


def test_frame_budget(tmp_path):
    fakeorm = fake_library(tmp_path)
    assert fakeorm.deep(OriginFinder(code_root=ROOT, limit=5), 10) is None
    assert fakeorm.deep(OriginFinder(code_root=ROOT, limit=50), 10) == (  # origin: deep
        "tests/test_origin.py", line_of(__file__, "deep"))


def test_cache_is_bounded():
    f = finder(cache_size=10)
    for i in range(100):
        f.classify("/srv/app/f%d.py" % i)
    assert len(f._cache) <= 10


def test_statement_run_in_a_thread_for_a_suspended_coroutine(tmp_path):
    import asyncio

    fakeorm = fake_library(tmp_path)
    f = OriginFinder(code_root=ROOT)

    async def view():
        loop = asyncio.get_running_loop()
        # only stdlib and library frames in the worker thread: the line is where the view awaits
        return await loop.run_in_executor(None, fakeorm.find_for, f, asyncio.current_task())  # origin: await

    assert asyncio.run(view()) == ("tests/test_origin.py", line_of(__file__, "await"))
