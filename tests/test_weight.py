"""This package runs inside other people's applications. Weight is a promise, so it is a test."""

import os

import pytest

# tomllib arrived in 3.11 and this package supports 3.9: the file it reads is the same on every
# version, so checking it where the standard library can read it is enough.
tomllib = pytest.importorskip("tomllib")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def pyproject():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)


def test_it_installs_nothing_of_its_own():
    project = pyproject()["project"]
    assert project["dependencies"] == [], "a runtime dependency would become the application's too"
    # The extras only name what the application already has; installing one must not pull a helper in.
    for name, requirements in project.get("optional-dependencies", {}).items():
        for requirement in requirements:
            assert requirement.split(">")[0].split("[")[0].strip().lower() in (
                "django", "sqlalchemy", "flask", "starlette", "celery",
            ), f"the {name} extra asks for {requirement}"


def test_it_is_small_enough_to_read():
    lines = 0
    for folder, _, files in os.walk(os.path.join(ROOT, "src")):
        for name in files:
            if name.endswith(".py"):
                with open(os.path.join(folder, name)) as f:
                    lines += sum(1 for _ in f)
    # Room to grow, and a wall before it becomes a library nobody reads.
    assert lines < 1800, f"{lines} lines in src/, the limit is 1800"


def test_it_says_which_pythons_it_supports_and_under_which_licence():
    project = pyproject()["project"]
    assert project["requires-python"].startswith(">=3."), "the oldest Python is part of the contract"
    assert project["license"] == "MIT"
