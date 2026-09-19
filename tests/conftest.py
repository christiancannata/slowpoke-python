import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # packages/python: origins are relative to it in these tests
sys.path.insert(0, HERE)


class FakeSender:
    """Collects payloads instead of posting them: tests read them back as dicts."""

    def __init__(self):
        self.payloads = []

    def send(self, body):
        self.payloads.append(body)
        return True

    def decoded(self):
        return [json.loads(p) for p in self.payloads]

    def only_trace(self):
        """[root span, query spans] of the only trace sent."""
        if len(self.payloads) != 1:
            raise AssertionError("%d payloads sent, expected 1" % len(self.payloads))
        return split(self.decoded()[0])


class ThrowingSender:
    def send(self, body):
        raise RuntimeError("agent exploded")


def split(payload):
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root = [s for s in spans if s["kind"] != 3][0]
    return root, [s for s in spans if s["kind"] == 3]


def attr(span, key):
    for kv in span["attributes"]:
        if kv["key"] == key:
            return list(kv["value"].values())[0]
    return None


def line_of(path, marker):
    """Line number of the "# origin: <marker>" comment, so tests do not hard-code line numbers."""
    with open(path) as f:
        for n, text in enumerate(f, 1):
            if text.rstrip().endswith("# origin: " + marker):
                return n
    raise AssertionError("marker %r not found in %s" % (marker, path))


def rel(path):
    return os.path.relpath(path, ROOT)


@pytest.fixture
def sender():
    return FakeSender()


@pytest.fixture
def tracer(sender):
    """The global tracer, synchronous and pointed at a fake agent; reset afterwards."""
    import slowpoke

    t = slowpoke.configure(sender=sender, background=False, code_root=ROOT, service="shop")
    yield t
    slowpoke.reset()
